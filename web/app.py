import os
import sys
import time
import shutil
import threading
import psutil
import subprocess
import logging
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional
from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from core.config import ConfigManager
from core.storage import BlobStorage
from core.scheduler import BackupScheduler
from core.storage_health import get_system_storage_health
from core.app_scanner import get_installed_applications
# 인증 미들웨어에서 필요한 심볼만 남긴다.
# 나머지(설정/로그인/변경/우회 토글)는 web/api_auth.py 로 분리되었다.
from core.auth import is_auth_configured, is_auth_corrupted, get_auth_status, validate_session

# 전역 실행 상태는 web/state.py 가 유일한 소유자다.
# 라우터들이 상태를 공유해야 하므로 app.py 가 직접 들지 않는다.
from web.state import current_task, task_lock, append_task_log

from web.paths import STATIC_DIR, TEMPLATES_DIR
from web.version import get_current_version, VERSION
from web.api_snapshots import (
    router as snapshots_router,
    _get_all_candidate_repos,
)
from web.api_update import (
    router as update_router,
    _background_update_check,
)

__all__ = ["app", "current_task", "task_lock"]


# 모듈 로거 (예외 경로에서 사용)
logger = logging.getLogger("BackupSystem")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
from core.logging_setup import install_namespace_logging  # noqa: E402
install_namespace_logging()  # 모든 프로젝트 로거를 logs/backup.log 에도 기록

# Fix #13: Background CPU monitor — samples every 0.5s so /api/system-info returns instantly
_cpu_percent_cache = [0.0]
def _cpu_monitor():
    while True:
        try:
            _cpu_percent_cache[0] = psutil.cpu_percent(interval=0.5)
        except Exception:
            time.sleep(0.5)
_cpu_monitor_thread = threading.Thread(target=_cpu_monitor, daemon=True)
_cpu_monitor_thread.start()

# 전역 실행 상태(current_task, task_lock, append_task_log)는 web/state.py 에 있다.
# 라우터 분리 시 상태가 갈라지지 않도록 한 곳에서만 소유한다.
scheduler = BackupScheduler()

scheduler.register_log_callback(append_task_log)

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    append_task_log("백업 스케줄러 서비스가 시작되었습니다.")
    # Durable Replication Queue: auto-resume interrupted tasks on startup
    try:
        from core.replication_queue import ReplicationQueueManager
        candidate_repos = _get_all_candidate_repos()
        for cand in candidate_repos:
            rq = ReplicationQueueManager(cand)
            resumed = rq.resume_all_interrupted()
            if resumed > 0:
                append_task_log(f"[Durable Queue] 미완료 오프사이트 복제 {resumed}건 자동 재개 (저장소: {os.path.basename(cand)})")
            rq.start_background_worker()
    except Exception as e_rq:
        append_task_log(f"[Durable Queue] 초기화 알림: {e_rq}", level="WARN")

    # Software Auto-Update Check
    try:
        threading.Thread(target=_background_update_check, daemon=True).start()
    except Exception:
        pass

    yield
    scheduler.stop()

# version 은 VERSION 파일에서 읽는다 (web/version.py).
# 여기서 하드코딩하면 릴리즈마다 OpenAPI 문서와 실제 버전이 어긋난다.
# v2.13.7 까지 "2.9.22" 로 굳어 있었다.
app = FastAPI(title="Server & System Backup Manager", version=VERSION, lifespan=lifespan)


# ==================== Auth HTTP Middleware ====================
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    
    # 0. 인증 설정 파일 손상 상태 검사 (Fail-Closed: 모든 API 요청 차단)
    if is_auth_corrupted():
        if not (path.startswith("/static") or path == "/favicon.ico" or path == "/api/auth/status"):
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": "보안 경고(Fail-Closed): auth_config.json 설정 파일이 손상되었습니다. 시스템 보호를 위해 모든 API 요청이 차단되었습니다.",
                    "corrupted": True
                }
            )

    # 인증 불필요 경로 (정적 리소스, 인증 API, favicon, 원격 릴리즈 배포 API)
    if (
        path.startswith("/static") or
        path.startswith("/api/auth/") or
        path == "/favicon.ico" or
        path in ("/api/system/release-info", "/api/system/update-package")
    ):
        return await call_next(request)
    
    # 마스터 비밀번호 미설정 상태: 진입 허용 (UI에서 셋업 모달 표시)
    if not is_auth_configured():
        return await call_next(request)
    
    # 클라이언트 IP 기반 로컬 루프백 바이패스 검사
    client_ip = request.client.host if request.client else ""
    auth_st = get_auth_status(client_ip)
    if auth_st.get("bypassed"):
        return await call_next(request)
    
    # 세션 토큰 추출 (쿠키 우선, Bearer 헤더 보조)
    token = request.cookies.get("backup_session")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
    
    # 유효한 세션인 경우 통과
    if token and validate_session(token):
        return await call_next(request)
    
    # API 요청인데 인증 실패 시 401 JSON 응답 반환
    if path.startswith("/api/"):
        return JSONResponse(
            status_code=401,
            content={
                "success": False,
                "error": "인증이 필요합니다.",
                "auth_required": True
            }
        )
    
    # 대시보드 페이지(/) 등 일반 요청은 통과 (프론트엔드에서 로그인 모달 표시)
    return await call_next(request)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# 분리된 라우터 등록
# 경로 계약(/api/*)은 app.py 내부 라우터와 동일하게 유지된다.
from web.api_auth import router as auth_router
app.include_router(auth_router)
app.include_router(snapshots_router)
app.include_router(update_router)
from web.api_backup import router as backup_router
app.include_router(backup_router)
from web.api_rescue import router as rescue_router
app.include_router(rescue_router)
from web.api_migration import router as migration_router
app.include_router(migration_router)

# --- Web UI Route ---
@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request, "v_ts": int(time.time()), "version": get_current_version()})

# --- System & Storage API ---
@app.get("/api/system-info")
def get_system_info():
    # Collect drive information
    drives = []
    try:
        partitions = psutil.disk_partitions(all=False)
        for p in partitions:
            try:
                usage = psutil.disk_usage(p.mountpoint)
                drives.append({
                    "device": p.device,
                    "mountpoint": p.mountpoint,
                    "fstype": p.fstype,
                    "total": usage.total,
                    "used": usage.used,
                    "free": usage.free,
                    "percent": usage.percent
                })
            except (PermissionError, OSError):
                pass
    except Exception:
        pass

    return {
        "cpu_percent": _cpu_percent_cache[0],  # Fix #13: use background-sampled value (no blocking)
        "memory": {
            "total": psutil.virtual_memory().total,
            "used": psutil.virtual_memory().used,
            "free": psutil.virtual_memory().free,
            "percent": psutil.virtual_memory().percent
        },
        "drives": drives
    }

@app.get("/api/system/storage-health")
def get_storage_health(force: bool = False):
    """물리 디스크 SMART 및 볼륨 사용률 종합 헬스 상태 조회 (Sprint 1)"""
    return get_system_storage_health(force_refresh=force)

@app.get("/api/storage-stats")
def get_storage_stats(repo_dir: Optional[str] = None):
    repos = [repo_dir] if repo_dir and os.path.exists(repo_dir) else _get_all_candidate_repos(repo_dir)

    total_blobs = 0
    stored_bytes = 0
    logical_bytes = 0
    total_snapshots = 0
    total_chunks = 0
    chunked_files_count = 0
    chunk_strategies = {}

    for r in repos:
        if os.path.exists(r):
            try:
                st = BlobStorage(r).get_storage_stats()
                total_blobs += st["total_blobs"]
                stored_bytes += st["stored_bytes"]
                logical_bytes += st["logical_bytes"]
                total_snapshots += st["total_snapshots"]
                total_chunks += st.get("total_chunks", 0)
                chunked_files_count += st.get("chunked_files_count", 0)
                for strat, count in st.get("chunk_strategies", {}).items():
                    chunk_strategies[strat] = chunk_strategies.get(strat, 0) + count
            except Exception as e:
                import logging
                logging.getLogger("BackupSystem").warning(f"Failed to query storage stats for repo {r}: {e}")

    saved_bytes = max(0, logical_bytes - stored_bytes)
    ratio = round((saved_bytes / logical_bytes * 100), 1) if logical_bytes > 0 else 0.0

    return {
        "total_blobs": total_blobs,
        "stored_bytes": stored_bytes,
        "logical_bytes": logical_bytes,
        "total_snapshots": total_snapshots,
        "dedup_saved_bytes": saved_bytes,
        "savings_percentage": ratio,
        "total_chunks": total_chunks,
        "chunked_files_count": chunked_files_count,
        "chunk_strategies": chunk_strategies
    }

# --- Directory Browser API ---
class BrowseDirRequest(BaseModel):
    current_path: Optional[str] = None

@app.post("/api/browse-dir")
def browse_directory(req: BrowseDirRequest):
    path = req.current_path
    if not path or not os.path.exists(path):
        # Default to root drives on Windows or root /
        if sys.platform.startswith("win"):
            drives = []
            for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                d = f"{letter}:\\"
                if os.path.exists(d):
                    drives.append({"name": f"로컬 디스크 ({letter}:)", "path": d, "is_dir": True})
            return {
                "current_path": "",
                "parent_path": None,
                "items": drives
            }
        else:
            path = "/"

    path = os.path.abspath(path)
    parent_path = os.path.dirname(path) if os.path.dirname(path) != path else None

    items = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        items.append({
                            "name": entry.name,
                            "path": os.path.abspath(entry.path),
                            "is_dir": True
                        })
                except (PermissionError, OSError):
                    pass
    except (PermissionError, OSError):
        pass

    items.sort(key=lambda x: x["name"].lower())
    return {
        "current_path": path,
        "parent_path": parent_path,
        "items": items
    }

# --- Profiles API ---
@app.get("/api/profiles")
def list_profiles():
    return ConfigManager.get_profiles()

@app.post("/api/profiles")
def save_profile(profile: Dict[str, Any]):
    saved = ConfigManager.save_profile(profile)
    append_task_log(f"백업 프로필 '{saved.get('name')}'이 저장되었습니다.")
    return saved

@app.put("/api/profiles/{profile_id}")
def update_profile(profile_id: str, profile: Dict[str, Any]):
    profile = dict(profile or {})
    profile["id"] = profile_id
    saved = ConfigManager.save_profile(profile)
    append_task_log(f"백업 프로필 '{saved.get('name')}'이 수정되었습니다.")
    return saved

@app.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: str):
    success = ConfigManager.delete_profile(profile_id)
    if not success:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"success": True}

# --- System Settings API ---
@app.get("/api/settings")
def get_system_settings():
    return {"success": True, "settings": ConfigManager.get_settings()}

@app.post("/api/settings")
def save_system_settings(settings: Dict[str, Any]):
    current = ConfigManager.get_settings()
    current.update(settings)
    ConfigManager.save_settings(current)
    append_task_log(f"시스템 전역 설정이 갱신되었습니다 (백업 엔진: {current.get('backup_engine', 'rust')})")
    return {"success": True, "settings": current}

# --- Path Validation & Folder Browse API ---
@app.post("/api/validate-path")
def validate_path(req: Dict[str, Any]):
    raw_path = req.get("path", "")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return {
            "path": "",
            "exists": False,
            "is_dir": False,
            "readable": False,
            "writable": False,
            "free_bytes": 0
        }

    path = os.path.abspath(raw_path.strip())
    exists = os.path.exists(path)
    is_dir = os.path.isdir(path) if exists else False
    readable = os.access(path, os.R_OK) if exists else False
    writable = os.access(path, os.W_OK) if exists else False

    free_bytes = 0
    try:
        check_target = path if exists else os.path.dirname(path)
        if os.path.exists(check_target):
            _, _, free_bytes = shutil.disk_usage(check_target)
    except Exception:
        free_bytes = 0

    return {
        "path": path,
        "exists": exists,
        "is_dir": is_dir,
        "readable": readable,
        "writable": writable,
        "free_bytes": free_bytes
    }

@app.get("/api/browse-folder")
def browse_folder(path: Optional[str] = None):
    return browse_directory(BrowseDirRequest(current_path=path))

# --- Installed Applications & Projects Discovery API ---
@app.get("/api/apps/installed")
def list_installed_apps(refresh: bool = False):
    return get_installed_applications(force_refresh=refresh)

@app.get("/api/projects/list")
def list_project_items():
    return []

# --- Windows Task Scheduler API (On/Off Smart Execution) ---
from core.windows_task import register_windows_scheduled_task, unregister_windows_scheduled_task, get_windows_scheduled_task_status

class WindowsTaskRegisterRequest(BaseModel):
    profile_id: Optional[str] = None
    schedule_type: str = "daily"
    schedule_value: str = "03:00"

@app.get("/api/windows-task/status")
def get_windows_task_status():
    return get_windows_scheduled_task_status()

@app.post("/api/windows-task/register")
def register_windows_task(req: WindowsTaskRegisterRequest):
    res = register_windows_scheduled_task(req.profile_id, req.schedule_type, req.schedule_value)
    if res.get("success"):
        append_task_log(f"Windows 작업 스케줄러 등록 완료: {res.get('message')}")
    else:
        append_task_log(f"Windows 작업 스케줄러 등록 실패: {res.get('error')}", level="ERROR")
    return res

@app.post("/api/windows-task/unregister")
def unregister_windows_task():
    res = unregister_windows_scheduled_task()
    append_task_log("Windows 작업 스케줄러에서 등록 해제되었습니다.")
    return res

# --- Windows System Image (Bare-Metal) API ---
from core.system_image import SystemImageManager

class SystemImageStartRequest(BaseModel):
    target_drive: str = "D:"
    engine: str = "kova"  # 기본. "wbadmin"은 표준 옵션. KOVA Engine 검증 실패 시 wbadmin으로 대체

@app.get("/api/system-image/status")
def get_system_image_status(target_drive: str = "D:"):
    return SystemImageManager.get_status(target_drive)

@app.post("/api/system-image/start")
def start_system_image(req: SystemImageStartRequest):
    res = SystemImageManager.start_backup(
        req.target_drive,
        log_callback=lambda msg: append_task_log(f"[시스템이미지] {msg}"),
        engine=req.engine
    )
    return res

@app.get("/api/system-image/logs")
def get_system_image_logs():
    return SystemImageManager.get_logs()

@app.post("/api/system-image/stop")
def stop_system_image():
    return SystemImageManager.stop_backup()

class VhdxMountRequest(BaseModel):
    vhdx_path: str
    read_only: bool = True

class VhdxDismountRequest(BaseModel):
    vhdx_path: str

@app.get("/api/system-image/vhdx-list")
def get_system_image_vhdx_list(target_drive: str = "D:"):
    try:
        return {"success": True, "vhdx_list": SystemImageManager.list_vhdx_images(target_drive)}
    except Exception as e:
        logger.error(f"VHDX 목록 조회 중 예외 발생: {e}")
        return {"success": False, "error": f"VHDX 목록 조회 실패: {e}", "vhdx_list": []}

@app.post("/api/system-image/mount")
def mount_system_image_vhdx(req: VhdxMountRequest):
    try:
        return SystemImageManager.mount_vhdx(req.vhdx_path, read_only=req.read_only)
    except Exception as e:
        logger.error(f"VHDX 마운트 중 예외 발생: {e}")
        return {"success": False, "error": f"VHDX 마운트 처리 실패: {e}"}

@app.post("/api/system-image/dismount")
def dismount_system_image_vhdx(req: VhdxDismountRequest):
    try:
        return SystemImageManager.dismount_vhdx(req.vhdx_path)
    except Exception as e:
        logger.error(f"VHDX 마운트 해제 중 예외 발생: {e}")
        return {"success": False, "error": f"VHDX 마운트 해제 처리 실패: {e}"}

class VhdxDeleteRequest(BaseModel):
    vhdx_path: str

@app.delete("/api/system-image/delete")
def delete_system_image_vhdx(req: VhdxDeleteRequest):
    try:
        return SystemImageManager.delete_vhdx(req.vhdx_path)
    except Exception as e:
        logger.error(f"VHDX 삭제 중 예외 발생: {e}")
        return {"success": False, "error": f"VHDX 삭제 처리 실패: {e}"}


# --- 3-2-1 Offsite Remote Replication API ---
offsite_sync_lock = threading.Lock()
is_offsite_syncing = False
class OffsiteSaveRequest(BaseModel):
    enabled: bool = False
    provider: Optional[str] = "local"
    remote_path: Optional[str] = ""
    auto_sync: bool = True
    gdrive_credentials_file: Optional[str] = ""
    gdrive_folder_id: Optional[str] = ""

class OffsiteTestRequest(BaseModel):
    provider: Optional[str] = None
    remote_path: Optional[str] = None
    gdrive_credentials_file: Optional[str] = None
    gdrive_folder_id: Optional[str] = None

class OffsiteSyncRequest(BaseModel):
    provider: Optional[str] = None
    remote_path: Optional[str] = None
    source_repo_dir: Optional[str] = None
    gdrive_credentials_file: Optional[str] = None
    gdrive_folder_id: Optional[str] = None

def _check_remote_path(remote_path: Optional[str]):
    path = (remote_path or "").strip()
    if not path:
        return {
            "reachable": False,
            "exists": False,
            "total_gb": 0.0,
            "used_gb": 0.0,
            "free_gb": 0.0,
            "free_percent": 0.0,
            "error": "원격 저장소 경로가 설정되지 않았습니다."
        }
    try:
        if not os.path.exists(path):
            try:
                os.makedirs(path, exist_ok=True)
            except Exception as e_mkdir:
                return {
                    "reachable": False,
                    "exists": False,
                    "total_gb": 0.0,
                    "used_gb": 0.0,
                    "free_gb": 0.0,
                    "free_percent": 0.0,
                    "error": f"원격 경로에 접근/생성할 수 없습니다: {e_mkdir}"
                }
        total, used, free = shutil.disk_usage(path)
        total_gb = round(total / (1024 ** 3), 1)
        used_gb = round(used / (1024 ** 3), 1)
        free_gb = round(free / (1024 ** 3), 1)
        free_pct = round((free / total) * 100, 1) if total > 0 else 0.0
        return {
            "reachable": True,
            "exists": True,
            "total_gb": total_gb,
            "used_gb": used_gb,
            "free_gb": free_gb,
            "free_percent": free_pct,
            "error": None
        }
    except Exception as e:
        return {
            "reachable": False,
            "exists": False,
            "total_gb": 0.0,
            "used_gb": 0.0,
            "free_gb": 0.0,
            "free_percent": 0.0,
            "error": str(e)
        }

def _check_gdrive_connection(credentials_file: Optional[str], folder_id: Optional[str], interactive: bool = False):
    try:
        from core.offsite import GoogleDriveOffsiteReplicator
        cands = _get_all_candidate_repos()
        repo = cands[0] if cands else os.getcwd()
        creds_clean = (credentials_file or "").strip().strip('"').strip("'")
        folder_clean = (folder_id or "BackupSystem_Offsite").strip().strip('"').strip("'")
        rep = GoogleDriveOffsiteReplicator(
            local_repo_dir=repo,
            credentials_file=creds_clean,
            folder_name_or_id=folder_clean or "BackupSystem_Offsite",
            interactive=interactive
        )
        pf = rep.preflight()
        return {
            "reachable": bool(pf.get("reachable")),
            "exists": True,
            "user_email": pf.get("user_email", ""),
            "target_folder_id": pf.get("target_folder_id", ""),
            "total_gb": pf.get("total_gb", 0.0),
            "used_gb": pf.get("used_gb", 0.0),
            "free_gb": pf.get("free_gb", 0.0),
            "free_percent": pf.get("free_percent", 0.0),
            "error": pf.get("reason") if not pf.get("ok") else None
        }
    except Exception as e:
        return {
            "reachable": False,
            "exists": False,
            "total_gb": 0.0,
            "used_gb": 0.0,
            "free_gb": 0.0,
            "free_percent": 0.0,
            "error": str(e)
        }

@app.get("/api/offsite")
def get_offsite_status():
    cfg = ConfigManager.get_offsite_config()
    provider = cfg.get("provider", "local")
    remote = (cfg.get("remote_path", "") or "").strip().strip('"').strip("'")
    gdrive_creds = (cfg.get("gdrive_credentials_file", "") or "").strip().strip('"').strip("'")
    gdrive_folder = (cfg.get("gdrive_folder_id", "") or "").strip().strip('"').strip("'")

    if provider == "gdrive":
        status = _check_gdrive_connection(gdrive_creds, gdrive_folder, interactive=False)
    else:
        status = _check_remote_path(remote)

    return {
        "success": True,
        "enabled": cfg.get("enabled", False),
        "provider": provider,
        "remote_path": remote,
        "gdrive_credentials_file": gdrive_creds,
        "gdrive_folder_id": gdrive_folder,
        "auto_sync": cfg.get("auto_sync", True),
        "status": status
    }

@app.post("/api/offsite")
def save_offsite_settings(req: OffsiteSaveRequest):
    remote_clean = (req.remote_path or "").strip().strip('"').strip("'")
    creds_clean = (req.gdrive_credentials_file or "").strip().strip('"').strip("'")
    folder_clean = (req.gdrive_folder_id or "").strip().strip('"').strip("'")
    saved = ConfigManager.save_offsite_config({
        "enabled": req.enabled,
        "provider": (req.provider or "local").strip().lower(),
        "remote_path": remote_clean,
        "auto_sync": req.auto_sync,
        "gdrive_credentials_file": creds_clean,
        "gdrive_folder_id": folder_clean
    })
    append_task_log(f"[3-2-1 설정] 오프사이트 원격 복제 설정 저장: 제공자={saved['provider']}, 활성={saved['enabled']}, 경로={saved['remote_path']}, GoogleDrive키={saved['gdrive_credentials_file']}")
    return {"success": True, "data": saved}

@app.post("/api/offsite/upload_creds")
async def upload_gdrive_credentials(file: UploadFile = File(...)):
    """구글 드라이브 인증 JSON 파일(OAuth 클라이언트 또는 서비스 계정)을 업로드하여 data 디렉터리에 저장합니다."""
    try:
        base_data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(base_data_dir, exist_ok=True)
        file_path = os.path.join(base_data_dir, file.filename)
        content = await file.read()
        with open(file_path, "wb") as f:
            f.write(content)
        # 배포 및 설치 디렉터리 동기화
        for target_dir in [r"D:\백업시스템_설치용\data", r"C:\Users\kksjmj\AppData\Local\Programs\백업시스템\data"]:
            try:
                os.makedirs(target_dir, exist_ok=True)
                with open(os.path.join(target_dir, file.filename), "wb") as f:
                    f.write(content)
            except Exception:
                pass
        return {"success": True, "file_path": file_path, "filename": file.filename}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/offsite/test")
def test_offsite_connection(req: OffsiteTestRequest):
    cfg = ConfigManager.get_offsite_config()
    provider = req.provider or cfg.get("provider", "local")

    if provider == "gdrive":
        creds = req.gdrive_credentials_file if req.gdrive_credentials_file is not None else cfg.get("gdrive_credentials_file", "")
        folder = req.gdrive_folder_id if req.gdrive_folder_id is not None else cfg.get("gdrive_folder_id", "")
        creds = (creds or "").strip().strip('"').strip("'")
        folder = (folder or "").strip().strip('"').strip("'")

        base_data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        token_path = os.path.join(base_data_dir, "gdrive_oauth_token.json")

        # 키 파일 경로가 비어있거나 없는 경우 자동 탐색
        if not creds or not os.path.exists(creds):
            for sdir in [base_data_dir, root_dir]:
                if os.path.exists(sdir):
                    for fname in os.listdir(sdir):
                        if fname.startswith("client_secret") and fname.endswith(".json"):
                            creds = os.path.join(sdir, fname)
                            break
                if creds and os.path.exists(creds):
                    break

        is_oauth_client = False
        if creds and os.path.exists(creds):
            try:
                import json
                with open(creds, "r", encoding="utf-8") as f_cred:
                    cdata = json.load(f_cred)
                if "installed" in cdata or "web" in cdata:
                    is_oauth_client = True
            except Exception:
                pass

        needs_oauth = False
        if is_oauth_client:
            if not os.path.exists(token_path):
                needs_oauth = True
            else:
                # 기존 토큰의 연결 유효성 선검증
                status = _check_gdrive_connection(creds, folder, interactive=False)
                if status.get("reachable"):
                    return {
                        "success": True,
                        "provider": "gdrive",
                        "needs_oauth": False,
                        "status": status
                    }
                # 토큰 만료/취소(invalid_grant/revoked/expired) 등 인증 실패 시 자동 재인증 플로우 개시
                err_str = str(status.get("error", ""))
                if any(k in err_str for k in ("invalid_grant", "expired", "revoked", "구글 로그인", "PermissionError")):
                    logger.info("[Google Drive] 기존 OAuth 토큰 만료 또는 재인증 필요 감지 -> 브라우저 로그인 플로우 개시")
                    try:
                        if os.path.exists(token_path):
                            os.remove(token_path)
                    except Exception:
                        pass
                    needs_oauth = True
                else:
                    return {
                        "success": False,
                        "provider": "gdrive",
                        "needs_oauth": False,
                        "status": status
                    }

        if needs_oauth:
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(
                creds, scopes=["https://www.googleapis.com/auth/drive"]
            )
            flow.redirect_uri = "http://localhost:8766/"
            auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

            import http.server
            import urllib.parse

            SUCCESS_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>구글 드라이브 인증 완료</title>
    <style>
        body {
            background-color: #0f172a;
            color: #f8fafc;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            display: flex;
            align-items: center;
            justify-content: center;
            height: 100vh;
            margin: 0;
        }
        .card {
            background: rgba(30, 41, 59, 0.95);
            border: 1px solid #334155;
            border-radius: 16px;
            padding: 32px 40px;
            text-align: center;
            box-shadow: 0 10px 25px rgba(0,0,0,0.5);
            max-width: 440px;
        }
        h2 { color: #38bdf8; margin: 12px 0 8px 0; font-size: 20px; }
        p { color: #94a3b8; font-size: 14px; line-height: 1.6; margin: 0; }
        .badge {
            display: inline-block;
            background: #059669;
            color: #ecfdf5;
            padding: 6px 14px;
            border-radius: 9999px;
            font-weight: 600;
            font-size: 13px;
        }
    </style>
    <script>
        window.onload = function() {
            setTimeout(function() {
                try { window.close(); } catch(e){}
            }, 1500);
        };
    </script>
</head>
<body>
    <div class="card">
        <div class="badge">✓ 연동 성공</div>
        <h2>구글 드라이브 인증 완료</h2>
        <p>Google Drive OAuth 연동이 정상적으로 완료되었습니다.<br>이 창은 잠시 후 자동으로 닫힙니다.</p>
    </div>
</body>
</html>"""

            class _OAuthHandler(http.server.BaseHTTPRequestHandler):
                def log_message(self, format, *args):
                    pass
                def do_GET(self):
                    query = urllib.parse.urlparse(self.path).query
                    params = urllib.parse.parse_qs(query)
                    code = params.get("code", [None])[0]
                    if code:
                        try:
                            flow.fetch_token(code=code)
                            tok_json = flow.credentials.to_json()
                            os.makedirs(base_data_dir, exist_ok=True)
                            with open(token_path, "w", encoding="utf-8") as tf:
                                tf.write(tok_json)
                            for target_dir in [r"D:\백업시스템_설치용\data", r"C:\Users\kksjmj\AppData\Local\Programs\백업시스템\data"]:
                                try:
                                    os.makedirs(target_dir, exist_ok=True)
                                    with open(os.path.join(target_dir, "gdrive_oauth_token.json"), "w", encoding="utf-8") as tf:
                                        tf.write(tok_json)
                                except Exception:
                                    pass
                            self.send_response(200)
                            self.send_header("Content-Type", "text/html; charset=utf-8")
                            self.end_headers()
                            self.wfile.write(SUCCESS_HTML.encode("utf-8"))
                        except Exception as e:
                            self.send_response(500)
                            self.send_header("Content-Type", "text/html; charset=utf-8")
                            self.end_headers()
                            self.wfile.write(f"<h3>인증 처리 오류: {e}</h3>".encode("utf-8"))
                    else:
                        self.send_response(400)
                        self.end_headers()

            def _run_server():
                try:
                    server = http.server.HTTPServer(("localhost", 8766), _OAuthHandler)
                    server.timeout = 180
                    server.handle_request()
                    server.server_close()
                except Exception as ex:
                    logger.error(f"[Google OAuth] 서버 오류: {ex}")

            threading.Thread(target=_run_server, daemon=True).start()

            return {
                "success": True,
                "needs_oauth": True,
                "auth_url": auth_url,
                "provider": "gdrive",
                "message": "사용자 브라우저에서 구글 로그인 팝업 창이 열립니다."
            }

        # 이미 토큰이 있거나 서비스 계정인 경우 정상 연결 검증 수행
        status = _check_gdrive_connection(creds, folder, interactive=False)
        return {
            "success": bool(status.get("reachable")),
            "provider": "gdrive",
            "needs_oauth": False,
            "status": status
        }
    else:
        target = req.remote_path if req.remote_path is not None else cfg.get("remote_path", "")
        target = (target or "").strip().strip('"').strip("'")
        status = _check_remote_path(target)
        return {
            "success": bool(status.get("reachable")),
            "provider": "local",
            "remote_path": target,
            "status": status
        }

@app.post("/api/offsite/sync")
def trigger_manual_offsite_sync(req: OffsiteSyncRequest):
    global is_offsite_syncing
    with offsite_sync_lock:
        if is_offsite_syncing:
            append_task_log("[3-2-1 오프사이트] 이미 동기화 작업이 실행 중입니다. 중복 실행을 방지합니다.", level="WARNING")
            return {"success": False, "message": "이미 동기화 작업이 진행 중입니다."}
        is_offsite_syncing = True

    cfg = ConfigManager.get_offsite_config()
    provider = req.provider or cfg.get("provider", "local")
    remote = (req.remote_path or cfg.get("remote_path", "")).strip().strip('"').strip("'")
    gdrive_creds = (req.gdrive_credentials_file or cfg.get("gdrive_credentials_file", "")).strip().strip('"').strip("'")
    gdrive_folder = (req.gdrive_folder_id or cfg.get("gdrive_folder_id", "")).strip().strip('"').strip("'")

    if provider != "gdrive" and not remote:
        with offsite_sync_lock:
            is_offsite_syncing = False
        raise HTTPException(status_code=400, detail="오프사이트 원격 경로가 지정되지 않았습니다.")

    source_repo = req.source_repo_dir
    if not source_repo:
        cands = _get_all_candidate_repos()
        for cand in cands:
            if os.path.exists(os.path.join(cand, "snapshots")):
                source_repo = cand
                break
        if not source_repo and cands:
            source_repo = cands[0]
    if not source_repo or not os.path.isdir(source_repo):
        with offsite_sync_lock:
            is_offsite_syncing = False
        raise HTTPException(status_code=400, detail="로컬 백업 저장소(Source Repo)를 찾을 수 없습니다.")

    dest_desc = f"Google Drive ({gdrive_folder or 'BackupSystem_Offsite'})" if provider == "gdrive" else remote

    def _async_sync():
        global is_offsite_syncing
        try:
            append_task_log(f"[3-2-1 오프사이트] 수동 동기화 시작: {source_repo} -> {dest_desc}")
            from core.offsite import replicate_offsite
            res = replicate_offsite(
                local_repo_dir=source_repo,
                remote_repo_dir=remote,
                provider=provider,
                gdrive_credentials_file=gdrive_creds,
                gdrive_folder_id=gdrive_folder,
                log_callback=append_task_log
            )
            if res.get("success"):
                append_task_log(f"[3-2-1 오프사이트] 동기화 성공: 복사/업로드 {res.get('files_copied', 0)}개 / 전체 {res.get('files_total', 0)}개 ({res.get('duration_sec', 0.0):.1f}초)")
            else:
                append_task_log(f"[3-2-1 오프사이트] 동기화 실패: {res.get('reason', '알 수 없음')}", level="WARNING")
        except Exception as e_sync:
            append_task_log(f"[3-2-1 오프사이트] 동기화 중 오류: {e_sync}", level="ERROR")
        finally:
            with offsite_sync_lock:
                is_offsite_syncing = False

    threading.Thread(target=_async_sync, daemon=True).start()
    return {
        "success": True,
        "message": f"오프사이트 복제가 백그라운드에서 시작되었습니다. ({source_repo} -> {dest_desc})",
        "source_repo": source_repo,
        "provider": provider,
        "remote_path": remote
    }

# --- System Shutdown API ---
@app.post("/api/system/shutdown")
def shutdown_system():
    def _kill():
        try:
            scheduler.stop()
        except Exception:
            pass
        time.sleep(1.0)
        os._exit(0)
    threading.Thread(target=_kill, daemon=True).start()
    return {"success": True, "message": "백업 시스템 서비스가 완전히 종료됩니다."}

# --- Real-time Global Alert Summary API (Red Alert System) ---
@app.get("/api/alerts/summary")
def get_alerts_summary():
    """
    Aggregates recent backup failures, active repository free disk space,
    and replication queue backlogs to compute real-time alert status for UI banner.
    """
    alerts = []
    status = "ok"

    # 1. Check Backup History (Last 24h)
    try:
        profiles = ConfigManager.get_profiles()
        now_ts = time.time()
        for profile in profiles:
            last_status = profile.get("last_status")
            last_run = profile.get("last_run", 0)
            p_name = profile.get("name", "기본 프로필")

            if last_status == "failed" and (now_ts - last_run < 86400):
                alerts.append({
                    "type": "backup_failure",
                    "severity": "critical",
                    "title": "백업 실패 감지",
                    "message": f"프로필 '{p_name}'의 최근 백업이 실패했습니다. 저장소 상태 및 로그를 확인하세요."
                })
                status = "critical"
    except Exception as e:
        alerts.append({
            "type": "config_error",
            "severity": "warning",
            "title": "설정 조회 오류",
            "message": f"프로필 상태 점검 중 오류: {str(e)}"
        })
        if status != "critical":
            status = "warning"

    # 2. Check All Repository Disk Spaces
    disk_info = {"total_gb": 0.0, "free_gb": 0.0, "free_percent": 100.0}
    try:
        profiles = ConfigManager.get_profiles()
        checked_drives = set()
        for p in profiles:
            repo_cand = p.get("repo_dir")
            if repo_cand and os.path.exists(repo_cand):
                drive = os.path.splitdrive(os.path.abspath(repo_cand))[0]
                if drive and drive not in checked_drives:
                    checked_drives.add(drive)
                    total, used, free = shutil.disk_usage(drive)
                    total_gb = round(total / (1024 ** 3), 1)
                    free_gb = round(free / (1024 ** 3), 1)
                    free_pct = round((free / total) * 100, 1) if total > 0 else 0.0
                    disk_info = {"total_gb": total_gb, "free_gb": free_gb, "free_percent": free_pct}

                    if free_pct < 5.0:
                        alerts.append({
                            "type": "disk_critical",
                            "severity": "critical",
                            "title": "디스크 용량 고갈 위험",
                            "message": f"저장소 드라이브({drive}) 여유 공간이 5% 미만({free_pct}%, 잔여 {free_gb} GB)입니다."
                        })
                        status = "critical"
                    elif free_pct < 10.0:
                        alerts.append({
                            "type": "disk_warning",
                            "severity": "warning",
                            "title": "디스크 용량 부족 경고",
                            "message": f"저장소 드라이브({drive}) 여유 공간이 10% 미만({free_pct}%, 잔여 {free_gb} GB)입니다."
                        })
                        if status != "critical":
                            status = "warning"
    except Exception:
        pass

    # 3. Check Replication Queue
    # ReplicationQueueManager 는 이 함수 스코프에서 import 한다.
    # (lifespan 안의 같은 이름 import 는 다른 함수 스코프라 충돌이 아니다)
    try:
        from core.replication_queue import ReplicationQueueManager
        candidate_repos = _get_all_candidate_repos()
        total_interrupted = 0
        total_pending = 0
        for cand in candidate_repos:
            rq = ReplicationQueueManager(cand)
            status_map = rq.get_status_summary()
            total_pending += status_map.get("pending", 0)
            total_interrupted += status_map.get("interrupted", 0)

        if total_interrupted > 0:
            alerts.append({
                "type": "replication_interrupted",
                "severity": "warning",
                "title": "오프사이트 복제 지연",
                "message": f"네트워크 단절로 중단된 복제 작업이 {total_interrupted}건 있습니다. 대기열에서 자동 재시도됩니다."
            })
            if status != "critical":
                status = "warning"
    except Exception:
        pass

    return {
        "success": True,
        "data": {
            "status": status,
            "badge_color": "red" if status == "critical" else ("yellow" if status == "warning" else "green"),
            "alerts": alerts,
            "disk": disk_info
        }
    }


@app.post("/api/maintenance/clean-incomplete")
async def api_clean_incomplete():
    """D: 드라이브 내 미완료 백업 및 불필요한 임시 파일 정리 (관리자 권한 프로세스)"""
    results = []
    
    # D:\WindowsImageBackup은 wbadmin 증분 백업의 기준 이미지이므로 삭제하지 않는다.
    # (삭제하면 다음 백업이 매번 전체 백업이 된다.)

    tmp_path = r"D:\DumpStack.log.tmp"
    if os.path.exists(tmp_path):
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(["cmd.exe", "/c", f'del /f /q /a "{tmp_path}"'], capture_output=True, creationflags=creationflags)
            results.append("DumpStack.log.tmp 삭제 성공")
        except Exception as e:
            results.append(f"DumpStack.log.tmp 오류: {e}")

    lock_path = r"D:\MyBackup_Repository\backup.lock"
    if os.path.exists(lock_path):
        try:
            os.remove(lock_path)
            results.append("backup.lock 삭제 성공")
        except Exception as e:
            results.append(f"backup.lock 오류: {e}")

    return {"success": True, "results": results}



