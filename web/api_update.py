# -*- coding: utf-8 -*-
"""
web/api_update.py

소프트웨어 자동 업데이트 및 원격 릴리즈 동기화 APIRouter 모듈
"""
import os
import io
import sys
import time
import base64
import zipfile
import tempfile
import threading
import subprocess
import urllib.request
import logging

from fastapi import APIRouter, Request, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from web.paths import BASE_DIR
from web.version import VERSION, _cached_update_info
from web.state import append_task_log

# 릴리즈 확인에 쓰는 함수.
#
# 라우터 본문 안에서 import 하면 같은 이름이 세 곳에 생긴다
# (백그라운드 체크 / 원격 릴리즈 조회 / 업데이트 상태).
# 한쪽만 고치면 조용히 어긋나고 pyflakes 는 중복을 잡지 못한다.
from core.updater import check_for_update, get_current_installed_version

logger = logging.getLogger("BackupSystem.update")
router = APIRouter()


def _background_update_check():
    time.sleep(5)
    try:
        info = check_for_update()
        _cached_update_info["checked_at"] = time.time()
        _cached_update_info["data"] = info
        if info and info.get("update_available"):
            append_task_log(f"[소프트웨어 업데이트] 새 버전 v{info['latest_version']} 배포가 감지되었습니다. (현재: v{info['current_version']})")
    except Exception as e_up:
        append_task_log(f"[소프트웨어 업데이트] 업데이트 확인 알림: {e_up}", level="WARN")


# --- Remote Self-Update API (P0 Security: Ed25519 Signature Verification & Anti-Zip-Slip) ---
@router.post("/api/system/self-update")
async def self_update(request: Request = None, custom_body: bytes = None, custom_signature: str = ""):
    """
    Receives raw zip binary of latest code, verifies Ed25519 cryptographic signature,
    extracts it safely over BASE_DIR with Zip-Slip protection, and restarts the service.
    """
    if custom_body is not None:
        body = custom_body
    elif request:
        body = await request.body()
    else:
        body = b""

    if not body:
        raise HTTPException(status_code=400, detail="업데이트 페이로드가 비어 있습니다.")

    # 1. Ed25519 Signature Verification
    signature_raw = custom_signature.strip() if custom_signature else (request.headers.get("X-Package-Signature", "").strip() if request else "")
    if not signature_raw:
        raise HTTPException(
            status_code=403,
            detail="업데이트 패키지 서명 누락: X-Package-Signature 헤더가 필요합니다."
        )

    # Convert Base64 or Hex to Hex format for verify_bytes_ed25519
    signature_hex = signature_raw
    if len(signature_raw) == 88 or signature_raw.endswith("="):
        try:
            signature_hex = base64.b64decode(signature_raw).hex()
        except Exception:
            raise HTTPException(status_code=403, detail="서명 인코딩 형식 오류")

    # 키링 검증: keys/*.pub 를 모두 신뢰한다.
    # 단일 경로만 보면 키 교체 전환 기간에 신 키로 서명한 패키지가 거부된다.
    # 신뢰 목록은 core/updater.get_trusted_public_key_paths 가 유일하게 소유한다.
    from core.updater import get_trusted_public_key_paths
    trusted_pub_paths = get_trusted_public_key_paths()
    if not trusted_pub_paths:
        raise HTTPException(
            status_code=403,
            detail="업데이트 거부: 서버에 Ed25519 릴리즈 공개키가 등록되지 않았습니다."
        )

    try:
        from core.crypto_sign import verify_bytes_ed25519_any
        _ok, _matched = verify_bytes_ed25519_any(body, signature_hex, trusted_pub_paths)
        if not _ok:
            raise HTTPException(
                status_code=403,
                detail="패키지 전자서명 검증 실패: 유효하지 않거나 변조된 업데이트 패키지입니다."
            )
        try:
            _label = os.path.basename(trusted_pub_paths[_matched]) if isinstance(trusted_pub_paths[_matched], str) else "key-%d" % _matched
        except Exception:
            _label = "unknown"
        logger.info("SELF_UPDATE_SIGNATURE_OK key=%s", _label)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=403,
            detail=f"전자서명 검증 중 오류 발생: {str(e)}"
        )

    # 2. Safe Zip Extraction with Path Traversal (Zip-Slip) Protection
    base_dir_abs = os.path.abspath(BASE_DIR)
    try:
        with zipfile.ZipFile(io.BytesIO(body), "r") as zf:
            file_names = zf.namelist()
            if not any("core" in fn or "web" in fn or "run.py" in fn for fn in file_names):
                raise HTTPException(status_code=400, detail="유효하지 않은 업데이트 패키지: 핵심 구성요소가 누락되었습니다.")

            for member in zf.infolist():
                dest_path = os.path.abspath(os.path.join(base_dir_abs, member.filename))
                if not (dest_path == base_dir_abs or dest_path.startswith(base_dir_abs + os.sep)):
                    raise HTTPException(
                        status_code=403,
                        detail=f"Zip-Slip 보안 위협 탐지: 허용되지 않은 경로 탈출 ({member.filename})"
                    )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"압축 패키지 검증 오류: {str(e)}")

    temp_zip = os.path.join(tempfile.gettempdir(), f"backup_update_{int(time.time())}.zip")
    with open(temp_zip, "wb") as f:
        f.write(body)

    updater_bat = os.path.join(tempfile.gettempdir(), f"run_updater_{int(time.time())}.bat")
    pyw_candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    launch_py = pyw_candidate if os.path.exists(pyw_candidate) else sys.executable

    # Windows CRLF & Ghost/Silent compliant batch script
    bat_content = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "ping 127.0.0.1 -n 2 >nul\r\n"
        f"\"{launch_py}\" -m zipfile -e \"{temp_zip}\" \"{BASE_DIR}\"\r\n"
        f"cd /d \"{BASE_DIR}\"\r\n"
        f"start \"\" \"{launch_py}\" run.py\r\n"
        f"del \"{temp_zip}\" >nul 2>&1\r\n"
        "del \"%~f0\" >nul 2>&1\r\n"
        "exit /b 0\r\n"
    )
    with open(updater_bat, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(bat_content)

    def _trigger_update_and_restart():
        time.sleep(1.2)
        flags = 0x00000200
        if sys.platform.startswith("win") and hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags |= subprocess.CREATE_NO_WINDOW
        else:
            flags |= 0x00000008
        subprocess.Popen(
            ["cmd.exe", "/c", updater_bat],
            creationflags=flags,
            close_fds=True
        )
        time.sleep(0.2)
        os._exit(0)

    threading.Thread(target=_trigger_update_and_restart, daemon=True).start()
    return {
        "success": True,
        "message": f"Ed25519 서명 검증 통과! 최신 패키지({len(body):,} 바이트)를 안전하게 수신했습니다. 1초 후 자동으로 적용되고 재시작됩니다."
    }


# =========================================================================
# Remote Master Release Origin & One-Click Sync Endpoints
# =========================================================================

@router.get("/api/system/release-info")
def get_release_info():
    """
    Returns latest release package metadata and Ed25519 signature for remote clients.
    """
    dist_dir = os.path.join(BASE_DIR, "dist")
    zip_path = os.path.join(dist_dir, f"release_v{VERSION}.zip")
    sig_path = zip_path + ".sig"

    has_pkg = os.path.exists(zip_path)
    pkg_size = os.path.getsize(zip_path) if has_pkg else 0
    signature = ""
    if os.path.exists(sig_path):
        try:
            with open(sig_path, "r", encoding="utf-8") as f:
                signature = f.read().strip()
        except Exception:
            pass

    return {
        "success": True,
        "data": {
            "version": VERSION,
            "has_package": has_pkg,
            "package_name": f"release_v{VERSION}.zip",
            "package_size": pkg_size,
            "signature": signature,
            "updated_at": os.path.getmtime(zip_path) if has_pkg else time.time()
        }
    }


@router.get("/api/system/update-package")
def download_update_package():
    """
    Streams the signed release zip package to client machines.
    """
    dist_dir = os.path.join(BASE_DIR, "dist")
    zip_path = os.path.join(dist_dir, f"release_v{VERSION}.zip")
    sig_path = zip_path + ".sig"

    if not os.path.exists(zip_path):
        raise HTTPException(status_code=404, detail=f"릴리즈 패키지(v{VERSION})가 준비되지 않았습니다.")

    signature = ""
    if os.path.exists(sig_path):
        try:
            with open(sig_path, "r", encoding="utf-8") as f:
                signature = f.read().strip()
        except Exception:
            pass

    return FileResponse(
        zip_path,
        media_type="application/octet-stream",
        filename=f"release_v{VERSION}.zip",
        headers={"X-Package-Signature": signature}
    )


@router.get("/api/system/check-remote-release")
def check_remote_release():
    """
    GitHub 공식 Releases를 직접 조회하여 최신 버전 감지 여부를 반환합니다.
    (미니피씨 P2P 의존성 제거, 글로벌 GitHub 릴리즈 직결)
    """
    cur_ver = get_current_installed_version()

    try:
        info = check_for_update()
        if info and info.get("update_available"):
            latest_v = str(info.get("latest_version", "")).lstrip("vV")
            inst_url = info.get("installer_download_url") or "/api/system/download-installer"
            return {
                "success": True,
                "data": {
                    "update_available": True,
                    "current_version": cur_ver,
                    "remote_version": f"v{latest_v}",
                    "remote_url": "https://github.com/kks3365550/Kova/releases/latest",
                    "package_size": 0,
                    "signature": info.get("signature", ""),
                    "changelog": info.get("changelog", ""),
                    "download_url": info.get("download_url", ""),
                    "installer_download_url": inst_url
                }
            }
        else:
            return {
                "success": True,
                "data": {
                    "update_available": False,
                    "current_version": cur_ver,
                    "remote_version": f"v{cur_ver}",
                    "remote_url": "https://github.com/kks3365550/Kova/releases/latest",
                    "installer_download_url": "/api/system/download-installer"
                }
            }
    except Exception as e:
        logger.warning("GitHub check_remote_release failed: %s", e)
        return {
            "success": True,
            "data": {
                "update_available": False,
                "current_version": cur_ver,
                "error": str(e),
                "installer_download_url": "/api/system/download-installer"
            }
        }


@router.post("/api/system/sync-remote-release")
async def sync_remote_release(request: Request):
    """
    Downloads signed release from master server and applies self-update automatically.
    """
    try:
        body_json = await request.json()
    except Exception:
        body_json = {}

    master_url = body_json.get("master_url", "").rstrip("/")
    if not master_url:
        raise HTTPException(status_code=400, detail="master_url이 지정되지 않았습니다.")

    # 1. Fetch package from master server
    try:
        req = urllib.request.Request(f"{master_url}/api/system/update-package", headers={"User-Agent": "BackupSystem-Client"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            sig_header = resp.headers.get("X-Package-Signature", "")
            pkg_bytes = resp.read()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"마스터 서버 패키지 다운로드 실패: {str(e)}")

    if not pkg_bytes:
        raise HTTPException(status_code=502, detail="다운로드된 패키지가 비어있습니다.")

    # 2. Invoke self_update pipeline directly
    return await self_update(request, custom_body=pkg_bytes, custom_signature=sig_header)


@router.get("/api/system/download-installer")
def download_installer():
    """
    dist/ 또는 로컬 배포 폴더 내의 최신 *Setup*.exe 파일을 직접 제공하거나,
    GitHub 릴리즈 인스톨러 바이너리 다운로드 URL(browser_download_url)로 직접 리다이렉트합니다.
    """
    import glob
    import json
    
    # 1. 로컬 배포본 / 빌드본 탐색 (dist 및 설정된 백업 저장소)
    search_dirs = [os.path.join(BASE_DIR, "dist")]
    try:
        from core.config import ConfigManager
        for p in ConfigManager.get_profiles():
            rd = p.get("repo_dir")
            if rd and os.path.exists(rd) and rd not in search_dirs:
                search_dirs.append(rd)
    except Exception:
        pass
    if os.path.exists(r"D:\백업시스템_설치용"):
        search_dirs.append(r"D:\백업시스템_설치용")
    for d in search_dirs:
        if os.path.exists(d):
            exe_files = glob.glob(os.path.join(d, "*Setup*.exe"))
            if exe_files:
                latest_exe = max(exe_files, key=os.path.getmtime)
                return FileResponse(
                    latest_exe,
                    media_type="application/vnd.microsoft.portable-executable",
                    filename=os.path.basename(latest_exe)
                )

    # 2. 캐시된 GitHub 릴리즈 에셋 정보 확인
    info = _cached_update_info.get("data")
    if not info or not info.get("installer_download_url"):
        try:
            info = check_for_update()
            if info:
                _cached_update_info["data"] = info
        except Exception:
            pass

    if info and info.get("installer_download_url"):
        return RedirectResponse(info["installer_download_url"], status_code=307)

    # 3. GitHub Releases API 직접 조회를 통한 .exe 다이렉트 다운로드 에셋 링크 확보
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/kks3365550/Kova/releases/latest",
            headers={"User-Agent": "BackupSystemUpdater/2.9", "Accept": "application/vnd.github.v3+json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for asset in data.get("assets", []):
                name = asset.get("name", "")
                if name.endswith(".exe") and ("Setup" in name or "Kova" in name):
                    return RedirectResponse(asset.get("browser_download_url"), status_code=307)
    except Exception as e_gh:
        logger.warning("Failed to resolve direct installer asset from GitHub: %s", e_gh)

    return RedirectResponse("https://github.com/kks3365550/Kova/releases/latest", status_code=307)


# ==================== Software Auto-Update API ====================
@router.get("/api/update/status")
def get_software_update_status(force_check: bool = False):
    """최신 소프트웨어 릴리즈 업데이트 상태 반환 (온라인/오프라인 지원)"""
    now = time.time()
    # Cache for 10 minutes unless force_check is requested
    if force_check or _cached_update_info.get("data") is None or (now - _cached_update_info.get("checked_at", 0) > 600):
        try:
            info = check_for_update()
            _cached_update_info["checked_at"] = now
            _cached_update_info["data"] = info
        except Exception as e:
            logger.warning("Update check failed: %s", e)
            info = None
    else:
        info = _cached_update_info.get("data")

    cur_ver = get_current_installed_version()
    if info and info.get("update_available"):
        inst_url = info.get("installer_download_url") or "/api/system/download-installer"
        return {
            "success": True,
            "update_available": True,
            "current_version": cur_ver,
            "latest_version": info.get("latest_version"),
            "mandatory": info.get("mandatory", False),
            "changelog": info.get("changelog", ""),
            "download_url": info.get("download_url", ""),
            "installer_download_url": inst_url
        }
    return {
        "success": True,
        "update_available": False,
        "current_version": cur_ver,
        "latest_version": cur_ver,
        "message": "최신 버전을 사용 중입니다.",
        "installer_download_url": "/api/system/download-installer"
    }


@router.post("/api/update/apply")
def trigger_software_update(background_tasks: BackgroundTasks):
    """클라우드에서 최신 패키지를 다운로드/검증 후 백그라운드 안전 설치"""
    from core.updater import perform_full_update_pipeline
    def _run_pipeline():
        append_task_log("[소프트웨어 업데이트] 최신 버전 다운로드 및 무결성 검증 착수...")
        res = perform_full_update_pipeline()
        append_task_log(f"[소프트웨어 업데이트] 결과: {res.get('message', res.get('status'))}")

    background_tasks.add_task(_run_pipeline)
    return {
        "success": True,
        "message": "자동 업데이트 프로세스가 백그라운드에서 시작되었습니다."
    }


@router.post("/api/update/bundle")
async def apply_offline_bundle(file: UploadFile = File(...)):
    """
    오프라인 .bundle 패키지를 업로드받아 UnifiedUpdatePipeline을 통해
    암호학적 서명, 정책 의미론, 무해성 검증 후 안전하게 적용합니다.
    """
    if not file.filename.endswith(".bundle"):
        raise HTTPException(status_code=400, detail="오프라인 배포 파일은 .bundle 확장자여야 합니다.")

    try:
        content = await file.read()
        if len(content) > 300 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="업로드된 번들 파일이 300MB를 초과합니다.")

        from core.updater_v2.bundle import BundleReader
        from core.updater_v2.default_keyring import load_default_keyring
        from core.updater_v2.pipeline import UnifiedUpdatePipeline

        keyring = load_default_keyring(BASE_DIR)
        pipeline = UnifiedUpdatePipeline(keyring=keyring, target_dir=BASE_DIR)

        acquired = BundleReader.read(content)
        result = pipeline.execute_update(acquired=acquired, skip_process_control=False)

        if not result.success:
            append_task_log(f"[오프라인 업데이트 실패] {result.message}", level="ERROR")
            return JSONResponse(status_code=400, content={"success": False, "error": result.message})

        append_task_log(f"[오프라인 업데이트] 성공: {result.message}")
        return {
            "success": True,
            "installed_version": result.installed_version,
            "message": result.message
        }
    except Exception as e:
        logger.error("Offline bundle update failed: %s", e)
        append_task_log(f"[오프라인 업데이트 실패] {str(e)}", level="ERROR")
        return JSONResponse(status_code=400, content={"success": False, "error": str(e)})
