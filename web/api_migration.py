# -*- coding: utf-8 -*-
"""
web/api_migration.py: 실시간 SSD 마이그레이션(클론 & 부트로더 주입) APIRouter
- C: 드라이브를 다른 물리 SSD로 라이브 캡처 및 클로닝 관리
- 후보 드라이브 조회, 마이그레이션 시작/중단, 상태/로그 조회 API 제공
"""
import os
import re
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from core.disk_clone import DiskCloneManager, get_candidate_target_drives
from core.drive_archive import DriveArchiveManager
from web.state import current_task, task_lock

router = APIRouter(prefix="/api/migration", tags=["migration"])

_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]?$")
_CMD_META = set('&|^<>%"' + chr(13) + chr(10))
_ARCHIVE_EXTS = (".qblk", ".wim", ".vhdx", ".raw.zst")


def _check_drive(value: str, field: str = "드라이브") -> str:
    v = (value or "").strip()
    if not _DRIVE_RE.fullmatch(v):
        raise HTTPException(status_code=400, detail=f"{field} 형식이 올바르지 않습니다(예: D:).")
    return v


def _is_system_drive(value: str) -> bool:
    return (value or "").strip()[:1].upper() == os.environ.get("SystemDrive", "C:")[:1].upper()


def _check_path_safe(value: Optional[str], field: str = "경로") -> None:
    if value and any(c in _CMD_META for c in value):
        raise HTTPException(status_code=400, detail=f"{field}에 사용할 수 없는 문자(& | ^ < > % \" 줄바꿈)가 있습니다.")


def _ensure_no_other_task() -> None:
    """일반 백업/복원 작업이 실행 중이면 디스크 단위 작업을 시작하지 않는다."""
    with task_lock:
        if current_task.get("running"):
            raise HTTPException(status_code=409, detail="다른 백업 또는 복원 작업이 실행 중입니다.")
    # 디스크 클론/통백업 작업끼리도 동시에 돌지 않게 한다
    if getattr(DiskCloneManager.get_instance(), "is_running", False) or getattr(DriveArchiveManager.get_instance(), "is_running", False):
        raise HTTPException(status_code=409, detail="다른 마이그레이션 또는 통백업 작업이 실행 중입니다.")


def _is_archive_target(path: str) -> bool:
    p = os.path.abspath(path).lower()
    parts = p.split("\\")
    return p.endswith(_ARCHIVE_EXTS) or "archives" in parts[:-1] or "windowsimagebackup" in parts


class StartMigrationRequest(BaseModel):
    target_drive: str
    auto_prepare_esp: bool = True


class StartArchiveRequest(BaseModel):
    source_drive: str = "C:"
    target_drive: Optional[str] = None
    target_wim_path: Optional[str] = None
    compress: str = "fast"
    auto_verify: bool = True
    upload_gdrive: bool = False



class VerifyArchiveRequest(BaseModel):
    wim_path: str


class RestoreArchiveRequest(BaseModel):
    wim_path: str
    target_drive: str
    auto_bcdboot: bool = True
    auto_restore_vsn: bool = True


@router.get("/disks")
async def api_migration_disks():
    """마이그레이션 후보 타겟 SSD/드라이브 목록 조회"""
    try:
        drives = get_candidate_target_drives()
        return {"success": True, "drives": drives}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"후보 드라이브 조회 실패: {str(e)}")


@router.post("/start")
async def api_migration_start(req: StartMigrationRequest):
    """실시간 SSD 마이그레이션 작업 시작"""
    if not req.target_drive:
        raise HTTPException(status_code=400, detail="target_drive 필드는 필수입니다.")
    _check_drive(req.target_drive, "target_drive")
    if _is_system_drive(req.target_drive):
        raise HTTPException(status_code=400, detail="시스템 드라이브는 마이그레이션 대상이 될 수 없습니다.")
    _ensure_no_other_task()

    manager = DiskCloneManager.get_instance()
    res = manager.start_clone(req.target_drive, auto_prepare_esp=req.auto_prepare_esp)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "마이그레이션 시작 실패"))

    return {"success": True, "message": res.get("message"), "target_drive": req.target_drive, "auto_prepare_esp": req.auto_prepare_esp}


@router.get("/status")
async def api_migration_status():
    """현재 마이그레이션 진행 상태 조회"""
    manager = DiskCloneManager.get_instance()
    status = manager.get_status()
    return {"success": True, "status": status}


@router.get("/baremetal/layout")
async def api_baremetal_layout(disk_number: int = 0, volume: str = "C:"):
    """삼성 매지션(WinClon) 방식 GPT 레이아웃 및 $Bitmap 스파스 분석 정보 조회"""
    try:
        from core.baremetal_clonix import BareMetalClonixEngine
        manifest = BareMetalClonixEngine.create_baremetal_manifest(disk_number, volume)
        return {"success": True, "manifest": manifest}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"베어메탈 레이아웃 분석 실패: {str(e)}")


@router.get("/logs")
async def api_migration_logs(limit: int = 100):
    """마이그레이션 실시간 로그 조회"""
    manager = DiskCloneManager.get_instance()
    logs = manager.get_logs(limit=limit)
    return {"success": True, "logs": logs}


@router.post("/stop")
async def api_migration_stop():
    """진행 중인 마이그레이션 작업 중단"""
    manager = DiskCloneManager.get_instance()
    res = manager.stop_clone()
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "마이그레이션 중단 실패"))
    return {"success": True, "message": res.get("message")}


# --- 드라이브 통백업(WIM 단일 아카이브) & 4단계 실측 검증 API ---

@router.post("/archive/start")
async def api_archive_start(req: StartArchiveRequest):
    """보험용 드라이브 통백업(WIM/qblk) 캡처 및 자동 실측 검증 시작"""
    _check_drive(req.source_drive, "source_drive")
    if req.target_drive:
        _check_drive(req.target_drive, "target_drive")
    _check_path_safe(req.target_wim_path, "target_wim_path")
    _ensure_no_other_task()
    manager = DriveArchiveManager.get_instance()
    target_path = req.target_wim_path
    if not target_path and req.target_drive:
        import time, os
        now_str = time.strftime("%Y%m%d_%H%M%S")
        drive_root = req.target_drive.rstrip("\\/") + "\\"
        archive_dir = os.path.join(drive_root, "MyBackup_Repository", "archives")
        os.makedirs(archive_dir, exist_ok=True)
        target_path = os.path.join(archive_dir, f"System_Drive_{req.source_drive[0]}_{now_str}.qblk")

    res = manager.start_capture(
        source_drive=req.source_drive,
        target_wim_path=target_path,
        compress=req.compress,
        auto_verify=req.auto_verify,
        upload_gdrive=req.upload_gdrive
    )
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "통백업 시작 실패"))
    return res


class UploadArchiveRequest(BaseModel):
    archive_path: str
    transfers: int = 6


@router.post("/archive/upload-gdrive")
async def api_archive_upload_gdrive(req: UploadArchiveRequest):
    """보관 중인 아카이브 파일을 구글 드라이브로 6청크 병렬 업로드"""
    import os
    from core.offsite import upload_baremetal_to_gdrive
    if not req.archive_path or not os.path.isfile(req.archive_path):
        raise HTTPException(status_code=400, detail="유효한 아카이브 파일 경로가 아닙니다.")
    if not _is_archive_target(req.archive_path):
        raise HTTPException(status_code=400, detail="아카이브 파일(.qblk/.wim/.vhdx/.raw.zst) 또는 archives 폴더의 파일만 업로드할 수 있습니다.")
    res = upload_baremetal_to_gdrive(archive_path=req.archive_path, transfers=req.transfers)
    return res



@router.get("/archive/status")
async def api_archive_status():
    """통백업 및 실측 검증 진행 상태 조회"""
    manager = DriveArchiveManager.get_instance()
    return {"success": True, "status": manager.get_status()}


@router.get("/archive/logs")
async def api_archive_logs(limit: int = 100):
    """통백업 및 검증 실시간 로그 조회"""
    manager = DriveArchiveManager.get_instance()
    return {"success": True, "logs": manager.get_logs(limit=limit)}


@router.post("/archive/stop")
async def api_archive_stop():
    """진행 중인 아카이브 캡처/복원 작업 중단"""
    manager = DriveArchiveManager.get_instance()
    return manager.cancel()


@router.post("/archive/verify")
async def api_archive_verify(req: VerifyArchiveRequest):
    """지정된 WIM 아카이브의 4단계 실측 검증 단독 실행"""
    if not req.wim_path:
        raise HTTPException(status_code=400, detail="wim_path 필드는 필수입니다.")
    manager = DriveArchiveManager.get_instance()
    v_res = manager.verify_archive(req.wim_path)
    return {"success": True, "result": v_res}


@router.get("/archive/list")
async def api_archive_list(repo_dir: Optional[str] = None):
    """보관 중인 WIM 아카이브 및 검증 상태 목록 조회"""
    manager = DriveArchiveManager.get_instance()
    archives = manager.list_archives(repo_dir=repo_dir)
    return {"success": True, "archives": archives}


@router.post("/archive/restore")
async def api_archive_restore(req: RestoreArchiveRequest):
    """WIM 아카이브를 대상 드라이브로 복원 + BCD 부트로더 주입 + VSN 복원"""
    if not req.wim_path or not req.target_drive:
        raise HTTPException(status_code=400, detail="wim_path와 target_drive는 필수입니다.")
    _check_drive(req.target_drive, "target_drive")
    _check_path_safe(req.wim_path, "wim_path")
    if _is_system_drive(req.target_drive):
        raise HTTPException(status_code=400, detail="실행 중인 시스템 드라이브로는 복원할 수 없습니다. WinPE/복구 USB에서 진행해 주세요.")
    _ensure_no_other_task()
    manager = DriveArchiveManager.get_instance()
    res = manager.start_restore(
        wim_path=req.wim_path,
        target_drive=req.target_drive,
        auto_bcdboot=req.auto_bcdboot,
        auto_restore_vsn=req.auto_restore_vsn
    )
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "복원 시작 실패"))
    return res


class DeleteArchiveRequest(BaseModel):
    archive_path: str


@router.delete("/archive")
async def api_archive_delete(req: DeleteArchiveRequest):
    """보관 중인 통백업 아카이브 및 메타데이터 안전 삭제"""
    if not req.archive_path:
        raise HTTPException(status_code=400, detail="archive_path 필드는 필수입니다.")
    manager = DriveArchiveManager.get_instance()
    res = manager.delete_archive(req.archive_path)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "아카이브 삭제 실패"))
    return res
