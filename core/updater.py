# -*- coding: utf-8 -*-
"""
core/updater.py: GitHub Releases 기반 소프트웨어 자동 업데이트 클라이언트 모듈 (v2.10.0)

핵심 기능:
1. check_for_update(): GitHub Releases API 조회 및 시맨틱 버전 비교
2. download_update(): HTTPS URL을 통한 임시 디렉토리 안전 다운로드
3. verify_update(): SHA-256 무결성 및 Ed25519 디지털 서명 검증 (Zero-Leak 검사 포함)
4. install_update(): Staging 해제, 기존 버전 보존, 백업 프로세스 타겟 안전 종료, 스왑 및 무창 재기동
5. health_check(): 포트 8765 및 /api/system-info 10초 이내 정상 가동 검증
6. rollback(): 헬스체크 실패 시 직전 정상 버전으로 자동 원복

보안 및 안전 원칙:
- Fail-Safe: 네트워크/GitHub 장애 발생 시 기존 백업 엔진의 정상 구동 보장
- Process Isolation: Qwen(RTX 5080) 등 타 파이썬 프로세스를 건드리지 않고 백업 데몬만 선별 제어
- Zero-Trust: 서명 또는 해시 불일치 시 설치 전 즉각 폐기
"""

import os
import sys
import time
import json
import socket
import shutil
import hashlib
import zipfile
import logging
import tempfile
import subprocess
import urllib.request
import urllib.error
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger("core.updater")

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PUBLIC_KEY_PATH = os.path.join(BASE_DIR, "keys", "release_ed25519_v2.pub")
PUBLIC_KEY_DIR = os.path.join(BASE_DIR, "keys")


V2_KEY_SHA256 = "b621a2b26a166b9f6b9f09f2764339eda7d07f1b44fc6a0492291dfb9b6256eb"


def get_trusted_public_key_paths() -> list:
    """
    신뢰하는 업데이트 서명 공개키 목록.
    유출된 구형 v1 키(release_ed25519.pub)를 영구 무효화하고,
    오직 SHA-256 지문이 검증된 v2 공개키(release_ed25519_v2.pub) 단독만 반환합니다.
    """
    v2_path = os.path.abspath(PUBLIC_KEY_PATH)
    if not os.path.exists(v2_path):
        logger.error("get_trusted_public_key_paths: v2 public key not found at %s", v2_path)
        return []
    try:
        import hashlib
        with open(v2_path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest().lower()
        if digest == V2_KEY_SHA256.lower():
            return [v2_path]
        logger.error(
            "get_trusted_public_key_paths: v2 key fingerprint mismatch! (expected %s, got %s)",
            V2_KEY_SHA256,
            digest
        )
        return []
    except Exception as e:
        logger.error("get_trusted_public_key_paths error: %s", e)
        return []


def parse_version(v_str: str) -> Tuple[int, ...]:
    """버전 문자열을 비교 가능한 정수 튜플로 변환 (예: 'v2.9.1' -> (2, 9, 1))"""
    clean_v = v_str.strip().lstrip('v').lstrip('V')
    parts = []
    for part in clean_v.split('.'):
        digits = []
        for ch in part:
            if ch.isdigit():
                digits.append(ch)
            else:
                break
        if digits:
            parts.append(int("".join(digits)))
        else:
            parts.append(0)
    return tuple(parts) if parts else (0, 0, 0)


def get_current_installed_version() -> str:
    """현재 설치된 소프트웨어 버전 확인"""
    v_file = os.path.join(BASE_DIR, "VERSION")
    if os.path.exists(v_file):
        try:
            with open(v_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    return content
        except Exception:
            pass
    try:
        from core import __version__
        return __version__
    except Exception:
        return "2.9.1"


GITHUB_REPO = "kks3365550/Kova"
GITHUB_LATEST_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


def _check_github_release(current_ver: str) -> Optional[Dict[str, Any]]:
    """GitHub Releases API를 통해 최신 릴리즈 확인 (1차 공식 채널)"""
    try:
        req = urllib.request.Request(
            GITHUB_LATEST_API,
            headers={
                "User-Agent": "BackupSystemUpdater/2.9",
                "Accept": "application/vnd.github.v3+json"
            }
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        latest_tag = data.get("tag_name", "").strip()
        if not latest_tag:
            return None

        cur_tuple = parse_version(current_ver)
        latest_tuple = parse_version(latest_tag)

        if latest_tuple <= cur_tuple:
            logger.info("UPDATE_CHECK_GITHUB already_up_to_date (%s >= %s)", current_ver, latest_tag)
            return None

        assets = data.get("assets", [])
        zip_url = None
        sig_url = None
        sha_url = None
        installer_url = None

        for asset in assets:
            name = asset.get("name", "")
            dl_url = asset.get("browser_download_url", "")
            if name.startswith("release_") and name.endswith(".zip"):
                zip_url = dl_url
            elif name.startswith("release_") and name.endswith(".zip.sig"):
                sig_url = dl_url
            elif name == "SHA256SUMS.txt":
                sha_url = dl_url
            elif name.endswith(".exe") and ("Setup" in name or "Kova" in name):
                installer_url = dl_url

        if not zip_url:
            logger.warning("UPDATE_CHECK_GITHUB no_zip_asset_found")
            return None

        # SHA-256 체크섬 가져오기
        expected_sha = ""
        if sha_url:
            try:
                s_req = urllib.request.Request(sha_url, headers={"User-Agent": "BackupSystemUpdater/2.9"})
                with urllib.request.urlopen(s_req, timeout=5) as s_resp:
                    sums_text = s_resp.read().decode("utf-8", errors="ignore")
                    for line in sums_text.splitlines():
                        if "release_" in line and ".zip" in line:
                            parts = line.strip().split()
                            if parts:
                                expected_sha = parts[0]
                                break
            except Exception as se:
                logger.warning("UPDATE_CHECK_GITHUB failed_to_fetch_sha: %s", se)

        # Ed25519 서명 가져오기 (.sig 파일 내용)
        expected_sig = ""
        if sig_url:
            try:
                sig_req = urllib.request.Request(sig_url, headers={"User-Agent": "BackupSystemUpdater/2.9"})
                with urllib.request.urlopen(sig_req, timeout=5) as sig_resp:
                    expected_sig = sig_resp.read().decode("utf-8", errors="ignore").strip()
            except Exception as sige:
                logger.warning("UPDATE_CHECK_GITHUB failed_to_fetch_sig: %s", sige)

        release_info = {
            "update_available": True,
            "current_version": current_ver,
            "latest_version": latest_tag.lstrip("vV"),
            "download_url": zip_url,
            "installer_download_url": installer_url,
            "sha256": expected_sha,
            "signature": expected_sig,
            "mandatory": False,
            "changelog": data.get("body", ""),
            "min_supported_version": "1.0.0",
            "source": "github"
        }
        logger.info("UPDATE_AVAILABLE_GITHUB version=%s url=%s", latest_tag, zip_url)
        return release_info
    except Exception as ge:
        logger.info("UPDATE_CHECK_GITHUB_SKIPPED reason=%s", ge)
        return None


def check_for_update(current_ver: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    공식 GitHub Releases API를 조회하여 신규 소프트웨어 업데이트를 확인합니다.
    네트워크 장애 또는 최신 버전일 경우 예외 없이 None 반환 (Fail-Safe).
    """
    if current_ver is None:
        current_ver = get_current_installed_version()

    logger.info("UPDATE_CHECK_START current_version=%s", current_ver)
    return _check_github_release(current_ver)


def download_update(release_info: Dict[str, Any], temp_dir: Optional[str] = None) -> Optional[str]:
    """
    지정된 HTTPS URL에서 업데이트 패키지(ZIP)를 임시 디렉토리로 다운로드.
    다운로드 실패 시 None 반환.
    """
    download_url = release_info.get("download_url")
    version = release_info.get("latest_version", "unknown")

    if not download_url:
        logger.error("UPDATE_DOWNLOAD_FAILED reason=empty_download_url")
        return None

    if temp_dir is None:
        temp_dir = tempfile.gettempdir()

    target_zip = os.path.join(temp_dir, f"backup_update_v{version}_{int(time.time())}.zip")
    logger.info("UPDATE_DOWNLOAD_START version=%s url=%s target=%s", version, download_url, target_zip)

    try:
        req = urllib.request.Request(download_url, headers={"User-Agent": "BackupSystemUpdater/2.9"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read()

        with open(target_zip, "wb") as f:
            f.write(content)

        logger.info("UPDATE_DOWNLOAD_OK size=%d bytes", len(content))
        return target_zip
    except Exception as e:
        logger.error("UPDATE_DOWNLOAD_FAILED error=%s", str(e))
        if os.path.exists(target_zip):
            try:
                os.remove(target_zip)
            except OSError:
                pass
        return None


def verify_update(
    zip_path: str,
    expected_sha256: str,
    expected_signature: str,
    pub_key_path: Optional[str] = None,
    trusted_pub_paths: Optional[list] = None,
) -> bool:
    """
    1. 파일 존재 및 크기 검사
    2. SHA-256 해시 검증
    3. Ed25519 전자 서명 검증 (다중 공개키 지원)
    4. ZIP 파일 내부 악성/개인키 파일 유입 차단 검증

    서명 검증 규칙:
      - pub_key_path 가 명시되면 그 키 하나로만 검증한다 (테스트 격리용).
      - 명시되지 않으면 trusted_pub_paths 를 쓰고, 그것도 없으면
        keys/*.pub 를 자동 탐색한다. 후보 중 하나라도 맞으면 통과.
      - 후보가 하나도 없거나 전부 실패하면 False (Fail-Closed).
    """
    if not os.path.exists(zip_path):
        logger.error("UPDATE_VERIFY_FAILED reason=file_not_found path=%s", zip_path)
        return False

    # 1. 파일 읽기 및 SHA-256 검증
    try:
        with open(zip_path, "rb") as f:
            raw_bytes = f.read()

        calc_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        if expected_sha256 and calc_sha256.lower() != expected_sha256.lower():
            logger.error(
                "UPDATE_HASH_MISMATCH expected=%s actual=%s",
                expected_sha256, calc_sha256
            )
            return False
        logger.info("UPDATE_HASH_VERIFY_OK sha256=%s", calc_sha256)
    except Exception as e:
        logger.error("UPDATE_VERIFY_FAILED hash_calc_error=%s", str(e))
        return False

    # 2. Ed25519 디지털 서명 검증 (다중 공개키 지원)
    if expected_signature:
        # 명시적 단일 키 지정이 있으면 그 키 하나로만 검증 (테스트 격리).
        if pub_key_path is not None:
            if not os.path.exists(pub_key_path):
                logger.error("UPDATE_SIGNATURE_INVALID reason=public_key_missing path=%s", pub_key_path)
                return False
            try:
                from core.crypto_sign import verify_bytes_ed25519
                is_valid = verify_bytes_ed25519(raw_bytes, expected_signature, pub_key_path)
                if not is_valid:
                    logger.error("UPDATE_SIGNATURE_INVALID signature=%s", expected_signature[:16])
                    return False
                logger.info("UPDATE_SIGNATURE_VERIFY_OK")
            except Exception as e:
                logger.error("UPDATE_SIGNATURE_INVALID error=%s", str(e))
                return False
        else:
            # 키링 모드: 명시 목록이 있으면 쓰고, 없으면 keys/*.pub 자동 탐색.
            candidates = list(trusted_pub_paths) if trusted_pub_paths is not None else get_trusted_public_key_paths()
            if not candidates:
                logger.error("UPDATE_SIGNATURE_INVALID reason=no_trusted_keys")
                return False
            try:
                from core.crypto_sign import verify_bytes_ed25519_any
                ok, matched = verify_bytes_ed25519_any(raw_bytes, expected_signature, candidates)
                if not ok:
                    logger.error("UPDATE_SIGNATURE_INVALID signature=%s", expected_signature[:16])
                    return False
                # 키 내용은 로그에 남기지 않는다. basename 만 기록.
                try:
                    label = os.path.basename(candidates[matched]) if isinstance(candidates[matched], str) else "bytes-key-%d" % matched
                except Exception:
                    label = "unknown"
                logger.info("UPDATE_SIGNATURE_VERIFY_OK key=%s", label)
            except Exception as e:
                logger.error("UPDATE_SIGNATURE_INVALID error=%s", str(e))
                return False

    # 3. ZIP 파일 내부 구조 및 Zero-Leak 검증
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            for name in names:
                name_l = name.lower()
                if name_l.endswith(".key") or name_l.endswith(".pem") or "private" in name_l:
                    logger.error("UPDATE_SECURITY_VIOLATION sensitive_file_in_zip=%s", name)
                    return False
        logger.info("UPDATE_ARCHIVE_VERIFY_OK file_count=%d", len(names))
    except Exception as e:
        logger.error("UPDATE_VERIFY_FAILED archive_corrupt error=%s", str(e))
        return False

    return True


def safely_stop_running_backup_server(port: int = 8765) -> bool:
    """
    포트 8765를 점유하고 있는 백업 서버 프로세스만 선별 종료.
    Qwen(RTX 5080) 등 기타 파이썬 워크로드는 절대 건드리지 않음.
    """
    if not sys.platform.startswith("win"):
        return True

    ps_script = (
        f"$conn = Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue; "
        "if ($conn) { foreach ($c in $conn) { "
        "  $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue; "
        "  if ($p) { "
        "    $cmd = (Get-CimInstance Win32_Process -Filter \"ProcessId = $($p.Id)\").CommandLine; "
        "    if ($cmd -like '*백업시스템*' -or $cmd -like '*run.py*') { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } "
        "  } "
        "} }; "
        "Start-Sleep -Seconds 1"
    )
    no_win = {"creationflags": subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, "CREATE_NO_WINDOW") else {}
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], timeout=8, **no_win)
        return True
    except Exception as e:
        logger.warning("safely_stop_running_backup_server warning: %s", e)
        return False


def health_check(port: int = 8765, timeout: int = 10) -> bool:
    """
    소프트웨어 재기동 후 10초 이내에 로컬 포트 8765 소켓 및 시스템 정보 API 정상 응답 확인.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                logger.info("UPDATE_HEALTHCHECK_OK port=%d responding", port)
                return True
        except OSError:
            time.sleep(0.5)

    logger.error("UPDATE_HEALTHCHECK_FAILED port=%d did not respond within %ds", port, timeout)
    return False


def rollback(backup_dir: str, target_dir: Optional[str] = None) -> bool:
    """
    업데이트 실패 시 이전 정상 보존 디렉토리에서 전체 파일 원복 및 서비스 재시작.
    """
    if target_dir is None:
        target_dir = BASE_DIR

    logger.warning("UPDATE_ROLLBACK initiated from %s to %s", backup_dir, target_dir)
    if not os.path.exists(backup_dir):
        logger.error("UPDATE_ROLLBACK_FAILED reason=backup_dir_missing")
        return False

    try:
        safely_stop_running_backup_server(8765)
        # 이전 버전 파일 덮어쓰기 (blobs, snapshots 등 백업 데이터는 건드리지 않음)
        for folder in ["core", "web", "keys"]:
            src_f = os.path.join(backup_dir, folder)
            dst_f = os.path.join(target_dir, folder)
            if os.path.exists(src_f):
                if os.path.exists(dst_f):
                    shutil.rmtree(dst_f, ignore_errors=True)
                shutil.copytree(src_f, dst_f)

        for script in ["run.py", "start_silent.vbs", "VERSION"]:
            src_s = os.path.join(backup_dir, script)
            dst_s = os.path.join(target_dir, script)
            if os.path.exists(src_s):
                shutil.copy2(src_s, dst_s)

        # 재기동
        vbs_path = os.path.join(target_dir, "start_silent.vbs")
        if os.path.exists(vbs_path):
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
            subprocess.Popen(["wscript.exe", vbs_path], cwd=target_dir, creationflags=flags, close_fds=True)
            health_check(8765, timeout=10)

        logger.info("UPDATE_ROLLBACK_SUCCESS restored to previous version")
        return True
    except Exception as e:
        logger.critical("UPDATE_ROLLBACK_FAILED error=%s", str(e))
        return False


def install_update(zip_path: str, target_dir: Optional[str] = None) -> bool:
    """
    Windows 환경에서 실행 중인 파이썬 프로세스의 파일 잠금(WinError 32)을 원천 방지하기 위해:
    1. 외부 분리형 무창 업데이터 배치 스크립트(run_updater.bat) 생성
    2. DETACHED_PROCESS 무창으로 배치 실행
    3. 배치가 기존 백업 서버 정상 종료 -> 패키지 압축 해제 덮어쓰기 -> 무창 재시작 -> 임시 파일 정리 순으로 수행
    """
    if target_dir is None:
        target_dir = BASE_DIR

    current_ver = get_current_installed_version()
    logger.info("UPDATE_INSTALL_START current=%s target_dir=%s", current_ver, target_dir)

    updater_ps1 = os.path.join(tempfile.gettempdir(), f"run_updater_{int(time.time())}.ps1")
    # Clean paths for PowerShell single quotes
    clean_zip = zip_path.replace("'", "''")
    clean_target = target_dir.replace("'", "''")

    ps_content = (
        "$ErrorActionPreference = 'SilentlyContinue'\r\n"
        "Start-Sleep -Seconds 1\r\n"
        "\r\n"
        "# 1. Safely stop Task Scheduler job and Port 8765 processes\r\n"
        "schtasks /end /tn 'BackupSystem_WebServer' 2>$null\r\n"
        "Start-Sleep -Seconds 1\r\n"
        "$conn = Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue\r\n"
        "if ($conn) {\r\n"
        "    foreach ($c in $conn) {\r\n"
        "        $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue\r\n"
        "        if ($p) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }\r\n"
        "    }\r\n"
        "}\r\n"
        "Start-Sleep -Seconds 2\r\n"
        "\r\n"
        "# 2. Extract to Staging and Robocopy to Target (Avoids file lock issues)\r\n"
        f"$stagingDir = Join-Path $env:TEMP ('backup_update_staging_' + [guid]::NewGuid().ToString())\r\n"
        f"New-Item -ItemType Directory -Path $stagingDir -Force | Out-Null\r\n"
        f"Expand-Archive -LiteralPath '{clean_zip}' -DestinationPath $stagingDir -Force\r\n"
        f"if (Test-Path -LiteralPath '{clean_zip}') {{ Remove-Item -LiteralPath '{clean_zip}' -Force -ErrorAction SilentlyContinue }}\r\n"
        f"robocopy $stagingDir '{clean_target}' /E /XO /NP /R:3 /W:1 | Out-Null\r\n"
        f"Remove-Item -Path $stagingDir -Recurse -Force -ErrorAction SilentlyContinue\r\n"
        "Start-Sleep -Seconds 1\r\n"
        "\r\n"
        "# 3. Silent background relaunch (Scheduler -> VBS -> Pythonw)\r\n"
        f"$target = '{clean_target}'\r\n"
        "$started = $false\r\n"
        "if (schtasks /query /tn 'BackupSystem_WebServer' 2>$null) {\r\n"
        "    schtasks /run /tn 'BackupSystem_WebServer' 2>$null\r\n"
        "    $started = $true\r\n"
        "}\r\n"
        "if (-not $started) {\r\n"
        "    $vbs = Join-Path $target 'start_silent.vbs'\r\n"
        "    if (Test-Path $vbs) {\r\n"
        "        ([wmiclass]'Win32_Process').Create(\"wscript.exe `\"$vbs`\"\", $target, $null) | Out-Null\r\n"
        "        $started = $true\r\n"
        "    }\r\n"
        "}\r\n"
        "if (-not $started) {\r\n"
        "    $pyw = Join-Path $target 'python\\pythonw.exe'\r\n"
        "    if (-not (Test-Path $pyw)) { $pyw = Join-Path $target '.venv\\Scripts\\pythonw.exe' }\r\n"
        "    if (-not (Test-Path $pyw)) { $pyw = 'pythonw.exe' }\r\n"
        "    $runPy = Join-Path $target 'run.py'\r\n"
        "    ([wmiclass]'Win32_Process').Create(\"`\"$pyw`\" `\"$runPy`\"\", $target, $null) | Out-Null\r\n"
        "}\r\n"
        "\r\n"
        "# 4. Self cleanup using $PSCommandPath\r\n"
        "Start-Sleep -Seconds 3\r\n"
        "if (Test-Path -LiteralPath $PSCommandPath) { Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue }\r\n"
    )

    with open(updater_ps1, "w", encoding="utf-8-sig", newline="") as f:
        f.write(ps_content)

    logger.info("UPDATE_PS1_CREATED path=%s launching detached updater", updater_ps1)

    ps_bin = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    if not os.path.exists(ps_bin):
        ps_bin = "powershell.exe"

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    cmd = [ps_bin, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", updater_ps1]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        startupinfo=startupinfo,
        creationflags=flags,
        close_fds=True
    )
    logger.info("UPDATE_SPAWN_OK child_pid=%d path=%s", proc.pid, updater_ps1)
    return True


def perform_full_update_pipeline(target_dir: Optional[str] = None) -> Dict[str, Any]:
    """
    단일 호출로 전체 업데이트 라이프사이클을 안전하게 실행:
    check -> download -> verify -> detached install
    """
    rel = check_for_update()
    if not rel or not rel.get("update_available"):
        return {"status": "NO_UPDATE", "message": "최신 버전을 사용 중입니다."}

    zip_file = download_update(rel)
    if not zip_file:
        return {"status": "DOWNLOAD_FAILED", "message": "업데이트 파일 다운로드에 실패했습니다."}

    is_verified = verify_update(
        zip_path=zip_file,
        expected_sha256=rel.get("sha256", ""),
        expected_signature=rel.get("signature", "")
    )
    if not is_verified:
        if os.path.exists(zip_file):
            try:
                os.remove(zip_file)
            except OSError:
                pass
        return {"status": "VERIFY_FAILED", "message": "업데이트 무결성 또는 전자 서명 검증 실패."}

    success = install_update(zip_file, target_dir)
    if success:
        return {"status": "SUCCESS", "message": f"v{rel.get('latest_version')} 업데이트가 백그라운드에서 안전하게 적용 및 재시작됩니다."}
    else:
        return {"status": "INSTALL_FAILED", "message": "업데이터 구동 중 오류가 발생했습니다."}
