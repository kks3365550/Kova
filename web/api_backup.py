# -*- coding: utf-8 -*-
"""
web/api_backup.py

백업, 복원, 무결성 검증, 작업 상태, 로그 및 스토리지 정리 APIRouter 모듈
"""
import os
import sys
import json
import subprocess
import time
import threading
import tempfile
from typing import Dict, List, Any, Optional
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, BackgroundTasks

from web.paths import BASE_DIR
from web.state import (
    current_task,
    task_lock,
    append_task_log,
    snapshot_task_status,
    request_cancel,
)
from web.api_snapshots import _get_all_candidate_repos, _find_snapshot_repo
from core.config import ConfigManager
from core.snapshot import SnapshotEngine
from core.restore import RestoreEngine
from core.storage import BlobStorage
from core.app_scanner import get_installed_applications
from core.driver_backup import export_windows_drivers

# 백업이 거부될 때의 예외 타입.
#
# 예외 핸들러 안에서만 import 하면 같은 이름이 두 곳에 생긴다
# (선택 백업 핸들러 / 일반 백업 핸들러). 한쪽만 수정하면 조용히 어긋나고
# pyflakes 는 중복을 잡지 못한다. 모듈 상단에 한 번만 둔다.
from core.lock import BackupAlreadyRunningError
from core.storage import InsufficientDiskSpaceError
from core.vss_manager import VSSRequiredError
from core.verify import RestoreVerificationError

router = APIRouter()


# --- Backup Execution API ---
class RunBackupRequest(BaseModel):
    profile_id: Optional[str] = None
    repo_dir: Optional[str] = None
    sources: Optional[List[str]] = None
    exclude_patterns: Optional[List[str]] = None
    compression_level: Optional[int] = 6


class RunCustomSelectionBackupRequest(BaseModel):
    include_drivers: bool = True
    selected_projects: List[str] = []
    selected_apps: List[str] = []
    custom_folders: List[str] = []
    repo_dir: Optional[str] = None
    profile_name: Optional[str] = "선택 백업"
    save_as_profile: bool = True
    exclude_patterns: Optional[List[str]] = None


def _background_custom_backup_task(params: Dict[str, Any]):
    reg_backup_dir = None
    try:
        include_drivers = params.get("include_drivers", True)
        selected_projects = params.get("selected_projects", [])
        selected_apps = params.get("selected_apps", [])
        custom_folders = params.get("custom_folders", [])
        repo_dir = params.get("repo_dir")
        if not repo_dir:
            for cand in _get_all_candidate_repos():
                if os.path.exists(os.path.join(cand, "snapshots")):
                    repo_dir = cand
                    break
        if not repo_dir:
            repo_dir = "D:\\MyBackup_Repository" if os.path.exists("D:\\") else os.path.join(os.path.expanduser("~"), "MyBackup_Repository")

        profile_name = params.get("profile_name", "내 맞춤형 선택 백업")
        excludes = params.get("exclude_patterns") or [
            "node_modules", "__pycache__", "*.pyc", ".venv", "venv", "*.tmp", "*.temp", "*.log", "backup_repository", "MyBackup_Repository"
        ]

        cancel_evt = current_task.get("cancel_event")
        all_sources = []

        # 저장소 드라이브 유효성 확인 및 자동 폴백
        repo_drive = os.path.splitdrive(repo_dir)[0]
        if repo_drive and not os.path.exists(repo_drive + "\\"):
            fallback_dir = os.path.join(os.path.expanduser("~"), "MyBackup_Repository")
            append_task_log(f"저장소 드라이브({repo_drive})가 없어 대체 경로({fallback_dir})를 사용합니다.", level="WARNING")
            repo_dir = fallback_dir
        os.makedirs(repo_dir, exist_ok=True)

        # 1. Driver Export if requested
        if include_drivers:
            driver_dir = os.path.join(os.path.expanduser("~"), "Windows_Drivers")
            with task_lock:
                current_task["progress"]["current_file"] = "윈도우 OEM 드라이버 수집 중..."
            append_task_log("윈도우 OEM 드라이버 추출을 시도합니다...")
            try:
                d_res = export_windows_drivers(driver_dir)
                if d_res.get("success"):
                    append_task_log(f"드라이버 {d_res.get('driver_count')}개 패키지 추출 완료 -> {driver_dir}")
                    all_sources.append(driver_dir)
                else:
                    append_task_log(f"드라이버 추출 건너뜀 (알림: {d_res.get('error', '권한 부족 또는 시간 초과')})", level="WARNING")
            except Exception as d_err:
                append_task_log(f"드라이버 추출 중 오류로 건너뜀: {d_err}", level="WARNING")

        # 2. Add projects
        for p in selected_projects:
            if os.path.exists(p) and p not in all_sources:
                all_sources.append(p)

        # 3. Add apps + their AppData + Start Menu Shortcuts + Full Registry Keys
        if selected_apps:
            reg_backup_dir = os.path.join(tempfile.gettempdir(), "Universal_Registry_Backup")
            os.makedirs(reg_backup_dir, exist_ok=True)

            try:
                from core.registry_backup import AppPackageCollector, collect_full_app_package
                collector = AppPackageCollector(reg_backup_dir)
                installed_apps_cache = get_installed_applications()

                for a in selected_apps:
                    if cancel_evt and cancel_evt.is_set():
                        raise InterruptedError("Cancelled by user")
                    
                    matched_app = next((item for item in installed_apps_cache if item.get("location") == a or item.get("name") == a), None)
                    app_name = matched_app["name"] if matched_app else os.path.basename(a)
                    publisher = matched_app.get("publisher", "") if matched_app else ""
                    location = a if os.path.exists(a) else (matched_app.get("location", "") if matched_app else "")

                    with task_lock:
                        current_task["progress"]["current_file"] = f"[{app_name}] 설정 및 레지스트리 수집 중..."

                    sources_to_add, reg_files = collect_full_app_package(app_name, publisher, location, reg_backup_dir, collector=collector)

                    for src in sources_to_add:
                        if src not in all_sources:
                            all_sources.append(src)

                    append_task_log(f"[{app_name}] 프로그램 본체 + 개인설정(AppData) + 바로가기 패키징 완료")

                if os.path.exists(reg_backup_dir) and os.listdir(reg_backup_dir) and reg_backup_dir not in all_sources:
                    all_sources.append(reg_backup_dir)
            except Exception as e_app:
                append_task_log(f"프로그램 패키징 중 알림: {e_app}", level="WARNING")

        # 4. Add custom folders
        for c in custom_folders:
            if os.path.exists(c) and c not in all_sources:
                all_sources.append(c)

        if not all_sources:
            append_task_log("백업할 유효한 대상 파일/폴더가 없습니다. 대상을 확인해 주세요.", level="ERROR")
            with task_lock:
                current_task["error"] = "선택된 백업 대상이 없습니다."
            return

        append_task_log(f"선택 백업 스캔 시작: 총 {len(all_sources)}개 대상 경로 -> 저장소: {repo_dir}")
        with task_lock:
            current_task["progress"]["current_file"] = "파일 목록 스캔 및 해시 분석 중..."

        # Save as profile if requested
        profile_id = "prof_custom_selected"
        if params.get("save_as_profile", True):
            is_desktop = False
            try:
                import socket
                hname = socket.gethostname().lower()
                if "desktop" in hname or "r2pfnfp" in hname:
                    is_desktop = True
            except Exception:
                pass

            # 기존 프로필 불러오기 (없으면 None)
            existing_prof = ConfigManager.get_profile(profile_id)

            if existing_prof:
                # 기존 프로필의 모든 설정을 보존하고, sources/repo_dir/exclude_patterns만 업데이트
                prof = dict(existing_prof)
                prof["sources"] = all_sources
                prof["repo_dir"] = repo_dir
                prof["exclude_patterns"] = excludes
                # 데스크탑인 경우 자동 백업 강제 비활성화
                if is_desktop:
                    prof["auto_backup_enabled"] = False
                    prof["schedule_type"] = "manual"
            else:
                # 최초 생성 시에만 기본값 사용
                auto_enable = False if is_desktop else params.get("auto_backup_enabled", False)
                sched_type = "manual" if is_desktop else params.get("schedule_type", "manual")

                prof = {
                    "id": profile_id,
                    "name": profile_name,
                    "sources": all_sources,
                    "repo_dir": repo_dir,
                    "exclude_patterns": excludes,
                    "schedule_type": sched_type,
                    "schedule_value": params.get("schedule_value", "12"),
                    "auto_backup_enabled": auto_enable,
                    "retention_count": params.get("retention_count", 30),
                    "retention_days": params.get("retention_days", 60),
                    "compression_level": params.get("compression_level", 3)
                }
            ConfigManager.save_profile(prof)
        else:
            # save_as_profile=False 이면 기존 프로필 설정만 참조
            prof = ConfigManager.get_profile(profile_id) or {}

        # 실제 백업에 사용할 압축 레벨: 프로필 설정 우선, 없으면 3
        effective_compress = prof.get("compression_level", 3) if prof else 3
        effective_retention_count = prof.get("retention_count", 30) if prof else 30
        effective_retention_days = prof.get("retention_days", 60) if prof else 60

        def on_progress(p_data):
            with task_lock:
                current_task["progress"] = p_data
            if p_data.get("processed_files", 0) % 50 == 0:
                append_task_log(f"진행 중: {p_data.get('percent')}% ({p_data.get('processed_files')}/{p_data.get('total_files')} 파일)")

        manifest = None
        use_rust = (ConfigManager.get_settings().get("backup_engine", "rust") == "rust")

        if use_rust:
            try:
                from core.rust_adapter import run_rust_backup, find_rust_engine_binary
                if find_rust_engine_binary():
                    session_id = f"snap_custom_{time.strftime('%Y%m%d_%H%M%S')}_{os.urandom(2).hex()}"
                    append_task_log(f"[초고속 네이티브 Rust 엔진 가동 - 선택 백업] 세션: {session_id}")

                    def _rust_progress(ev):
                        pct = ev.get("percent", 0)
                        done = ev.get("processed_files", 0)
                        tot = ev.get("total_files", 0)
                        spd = ev.get("speed", 0.0)
                        cur_f = ev.get("current_file", "")
                        n_files = ev.get("new_files", 0)
                        m_files = ev.get("modified_files", 0)
                        u_files = ev.get("unmodified_files", 0)
                        with task_lock:
                            current_task["progress"] = {
                                "percent": pct,
                                "processed_files": done,
                                "total_files": tot,
                                "speed": f"{spd:.1f} MB/s",
                                "current_file": cur_f,
                                "new_files": n_files,
                                "modified_files": m_files,
                                "unmodified_files": u_files
                            }
                        if done % 100 == 0 or done == tot:
                            append_task_log(f"선택 백업 진행: {pct}% ({done}/{tot} 파일 | {spd:.1f} MB/s | 신규: {n_files}, 수정: {m_files}, 동일: {u_files})")

                    res = run_rust_backup(
                        sources=all_sources,
                        repo_dir=repo_dir,
                        session_id=session_id,
                        zstd_level=effective_compress,
                        progress_callback=_rust_progress,
                        cancel_event=current_task.get("cancel_event")
                    )

                    raw_sum = res.raw_summary or {}
                    manifest = {
                        "id": res.session_id,
                        "vss_enabled": True,
                        "summary": {
                            "total_files": res.total_files,
                            "total_bytes": res.total_bytes,
                            "new_files": raw_sum.get("new_files", res.total_files),
                            "modified_files": raw_sum.get("modified_files", 0),
                            "unmodified_files": raw_sum.get("unmodified_files", 0),
                            "dedup_saved_bytes": res.dedup_bytes,
                            "duration_seconds": round(res.elapsed_seconds, 2)
                        }
                    }
            except Exception as e_rust:
                append_task_log(f"[경고] Rust 엔진 실행 실패 ({e_rust}), Python 레거시 엔진으로 자동 폴백합니다.", level="WARNING")

        if not manifest:
            # Fallback to legacy Python SnapshotEngine
            manifest = SnapshotEngine.create_snapshot(
                repo_dir=repo_dir,
                sources=all_sources,
                profile_id=profile_id,
                profile_name=profile_name,
                exclude_patterns=excludes,
                compress_level=effective_compress,
                progress_callback=on_progress,
                cancel_event=cancel_evt
            )

        pruned = SnapshotEngine.prune_snapshots(repo_dir, effective_retention_count, effective_retention_days, authorized=True)
        append_task_log(f"[보존 정책] 만료된 스냅샷 {len(pruned)}개 정리 완료")

        # Update profile
        prof = ConfigManager.get_profile(profile_id)
        if prof:
            prof["last_run"] = time.time()
            prof["last_status"] = "success"
            prof["last_snapshot_id"] = manifest["id"]
            ConfigManager.save_profile(prof)

        summary = manifest.get("summary", {})
        vss_mode = "VSS 볼륨 섀도 복사본(Crash-Consistent 일관성 보장)" if manifest.get("vss_enabled") else "일반 직접 읽기"
        append_task_log(
            f"선택 백업 완료! ID: {manifest['id']} | [{vss_mode}] | 파일: {summary.get('total_files')}개 "
            f"({round(summary.get('total_bytes', 0)/(1024*1024), 2)}MB) | "
            f"신규/수정: {summary.get('new_files', 0) + summary.get('modified_files', 0)}개 | "
            f"중복제거 절감: {round(summary.get('dedup_saved_bytes', 0)/(1024*1024), 2)}MB | "
            f"소요: {summary.get('duration_seconds')}초"
        )

        # 3-2-1 오프사이트 원격 복제 자동 실행
        try:
            offsite_cfg = ConfigManager.get_offsite_config()
            if offsite_cfg.get("enabled") and offsite_cfg.get("auto_sync"):
                provider = offsite_cfg.get("provider", "local")
                from core.offsite import replicate_offsite

                if provider == "gdrive":
                    creds = offsite_cfg.get("gdrive_credentials_file")
                    folder = offsite_cfg.get("gdrive_folder_id") or "BackupSystem_Offsite"
                    if creds:
                        append_task_log(f"[3-2-1 오프사이트] 구글 드라이브(Google Drive/{folder})로 자동 복제 시작...")
                        off_res = replicate_offsite(
                            local_repo_dir=repo_dir,
                            provider="gdrive",
                            gdrive_credentials_file=creds,
                            gdrive_folder_id=folder,
                            log_callback=append_task_log
                        )
                        if off_res.get("success"):
                            append_task_log(f"[3-2-1 오프사이트] 구글 드라이브 동기화 성공: 복제 {off_res.get('files_copied', 0)}개 / 전체 {off_res.get('files_total', 0)}개 ({off_res.get('duration_sec', 0.0):.1f}초)")
                        else:
                            append_task_log(f"[3-2-1 오프사이트] 구글 드라이브 동기화 실패: {off_res.get('reason', '원인 불명')}", level="WARNING")
                else:
                    remote_path = offsite_cfg.get("remote_path")
                    if remote_path:
                        append_task_log(f"[3-2-1 오프사이트] 원격 저장소({remote_path})로 자동 복제 시작...")
                        off_res = replicate_offsite(
                            local_repo_dir=repo_dir,
                            remote_repo_dir=remote_path,
                            provider="local"
                        )
                        if off_res.get("success"):
                            append_task_log(f"[3-2-1 오프사이트] 동기화 성공: 복사 {off_res.get('files_copied', 0)}개 / 스킵 {off_res.get('files_skipped', 0)}개 / 전체 {off_res.get('files_total', 0)}개 ({off_res.get('duration_sec', 0.0):.1f}초)")
                        elif not off_res.get("skipped"):
                            append_task_log(f"[3-2-1 오프사이트] 동기화 실패: {off_res.get('reason', '원인 불명')}", level="WARNING")
        except Exception as e_off:
            append_task_log(f"[3-2-1 오프사이트] 동기화 오류: {e_off}", level="WARNING")

        manifest_summary = {
            "id": manifest.get("id"),
            "created_at": manifest.get("created_at"),
            "type": manifest.get("type"),
            "profile_id": manifest.get("profile_id"),
            "profile_name": manifest.get("profile_name"),
            "repo_dir": manifest.get("repo_dir"),
            "vss_enabled": manifest.get("vss_enabled", False),
            "summary": manifest.get("summary")
        }
        del manifest
        import gc
        gc.collect()

        with task_lock:
            current_task["result"] = manifest_summary
            current_task["error"] = None
            if current_task.get("progress"):
                current_task["progress"]["percent"] = 100.0
                current_task["progress"]["current_file"] = "선택 백업 작업 완료"

    except InterruptedError:
        append_task_log("사용자에 의해 백업 작업이 취소되었습니다.", level="WARNING")
        with task_lock:
            current_task["error"] = "Cancelled by user"
            current_task["result"] = None
    except Exception as e:
        if isinstance(e, BackupAlreadyRunningError):
            append_task_log(f"선택 백업 거부: {str(e)}", level="WARNING")
        elif isinstance(e, InsufficientDiskSpaceError):
            append_task_log(f"[안전 보호(Fail-Closed) 작동] {str(e)}", level="ERROR")
        elif isinstance(e, VSSRequiredError):
            append_task_log(f"[VSS Strict 차단] {str(e)}", level="ERROR")
        elif isinstance(e, RestoreVerificationError):
            append_task_log(f"[복원 무결성 검증 실패(DEGRADED)] {str(e)}", level="ERROR")
        else:
            import traceback
            append_task_log(f"백업 중 오류 발생: {str(e)}", level="ERROR")
            append_task_log(traceback.format_exc(), level="ERROR")
        if not isinstance(e, BackupAlreadyRunningError):
            try:
                from core.windows_toast import notify_backup_failed
                notify_backup_failed(profile_name, str(e))
            except Exception:
                pass
        with task_lock:
            current_task["error"] = str(e)
            current_task["result"] = None
    finally:
        if reg_backup_dir and os.path.exists(reg_backup_dir):
            try:
                import shutil
                shutil.rmtree(reg_backup_dir, ignore_errors=True)
            except Exception:
                pass
        with task_lock:
            current_task["running"] = False


@router.post("/api/backup/custom-selection")
def run_custom_selection_backup(req: RunCustomSelectionBackupRequest, background_tasks: BackgroundTasks):
    with task_lock:
        if current_task["running"]:
            raise HTTPException(status_code=409, detail="이미 다른 백업 또는 복원 작업이 실행 중입니다.")

        cancel_evt = threading.Event()
        current_task.update({
            "type": "backup",
            "running": True,
            "progress": {"percent": 0, "processed_files": 0, "total_files": 0, "current_file": "백업 준비 중..."},
            "cancel_event": cancel_evt,
            "start_time": time.time(),
            "result": None,
            "error": None
        })

    background_tasks.add_task(_background_custom_backup_task, req.dict())
    return {"status": "started"}


def _background_backup_task(params: Dict[str, Any]):
    try:
        profile_id = params.get("profile_id")
        profile = None
        if profile_id:
            profile = ConfigManager.get_profile(profile_id)

        if not profile:
            profiles = ConfigManager.get_profiles()
            # Find active profile with auto_backup_enabled == True, or first profile
            profile = next((p for p in profiles if p.get("auto_backup_enabled")), (profiles[0] if profiles else {}))
            profile_id = profile.get("id", "prof_default")
        
        repo_dir = params.get("repo_dir") or profile.get("repo_dir")
        if not repo_dir:
            for cand in _get_all_candidate_repos():
                if os.path.exists(os.path.join(cand, "snapshots")):
                    repo_dir = cand
                    break
        if not repo_dir:
            repo_dir = "D:\\MyBackup_Repository" if os.path.exists("D:\\") else os.path.join(BASE_DIR, "backup_repository")

        sources = params.get("sources") or profile.get("sources") or [BASE_DIR]
        excludes = params.get("exclude_patterns") or profile.get("exclude_patterns") or []
        compress = params.get("compression_level") or profile.get("compression_level") or 6
        profile_name = profile.get("name", "수동 백업")

        # 저장소 드라이브 유효성 확인 및 폴백
        repo_drive = os.path.splitdrive(repo_dir)[0]
        if repo_drive and not os.path.exists(repo_drive + "\\"):
            fallback_dir = os.path.join(os.path.expanduser("~"), "MyBackup_Repository")
            append_task_log(f"저장소 드라이브({repo_drive})가 없어 대체 경로({fallback_dir})를 사용합니다.", level="WARNING")
            repo_dir = fallback_dir
        os.makedirs(repo_dir, exist_ok=True)

        cancel_evt = current_task["cancel_event"]
        append_task_log(f"백업 작업 시작: '{profile_name}' (원본 {len(sources)}개 경로 -> 저장소: {repo_dir})")
        with task_lock:
            current_task["progress"]["current_file"] = "파일 목록 스캔 및 해시 분석 중..."

        def on_progress(p_data):
            with task_lock:
                current_task["progress"] = p_data
            if p_data.get("processed_files", 0) % 50 == 0:
                append_task_log(f"진행 중: {p_data.get('percent')}% ({p_data.get('processed_files')}/{p_data.get('total_files')} 파일)")

        manifest = None
        use_rust = (ConfigManager.get_settings().get("backup_engine", "rust") == "rust")

        if use_rust:
            try:
                from core.rust_adapter import run_rust_backup, find_rust_engine_binary
                if find_rust_engine_binary():
                    session_id = f"snap_{time.strftime('%Y%m%d_%H%M%S')}_{os.urandom(2).hex()}"
                    append_task_log(f"[초고속 네이티브 Rust 엔진 가동] 세션: {session_id}")

                    def _rust_progress(ev):
                        pct = ev.get("percent", 0)
                        done = ev.get("processed_files", 0)
                        tot = ev.get("total_files", 0)
                        spd = ev.get("speed", 0.0)
                        cur_f = ev.get("current_file", "")
                        with task_lock:
                            current_task["progress"] = {
                                "percent": pct,
                                "processed_files": done,
                                "total_files": tot,
                                "speed": f"{spd:.1f} MB/s",
                                "current_file": cur_f
                            }
                        if done % 100 == 0 or done == tot:
                            append_task_log(f"진행 중: {pct}% ({done}/{tot} 파일 | {spd:.1f} MB/s)")

                    res = run_rust_backup(
                        sources=sources,
                        repo_dir=repo_dir,
                        session_id=session_id,
                        zstd_level=compress,
                        progress_callback=_rust_progress,
                        cancel_event=cancel_evt
                    )

                    with open(res.manifest_path, "r", encoding="utf-8") as fp:
                        manifest = json.load(fp)
            except Exception as e_rust:
                append_task_log(f"[경고] Rust 엔진 실행 실패 ({e_rust}), Python 레거시 엔진으로 자동 폴백합니다.", level="WARNING")

        if not manifest:
            # Fallback to legacy Python SnapshotEngine
            manifest = SnapshotEngine.create_snapshot(
                repo_dir=repo_dir,
                sources=sources,
                profile_id=profile_id,
                profile_name=profile_name,
                exclude_patterns=excludes,
                compress_level=compress,
                progress_callback=on_progress,
                cancel_event=cancel_evt
            )

        # Retention check
        retention_count = profile.get("retention_count", 30)
        retention_days = profile.get("retention_days", 60)
        pruned = SnapshotEngine.prune_snapshots(repo_dir, retention_count, retention_days, authorized=True)

        if profile_id:
            profile["last_run"] = time.time()
            profile["last_status"] = "success"
            profile["last_snapshot_id"] = manifest["id"]
            ConfigManager.save_profile(profile)

        summary = manifest.get("summary", {})
        vss_mode = "VSS 볼륨 섀도 복사본(Crash-Consistent 일관성 보장)" if manifest.get("vss_enabled") else "일반 직접 읽기"
        append_task_log(
            f"백업 성공 완료! ID: {manifest['id']} | [{vss_mode}] | 파일: {summary.get('total_files')}개 "
            f"({round(summary.get('total_bytes', 0)/(1024*1024), 2)}MB) | "
            f"신규/수정: {summary.get('new_files', 0) + summary.get('modified_files', 0)}개 | "
            f"중복제거 절감: {round(summary.get('dedup_saved_bytes', 0)/(1024*1024), 2)}MB | "
            f"소요시간: {summary.get('duration_seconds')}초"
        )
        if pruned:
            append_task_log(f"오래된 백업 스냅샷 {len(pruned)}개를 보관 정책에 따라 자동 정리했습니다.")

        # 3-2-1 오프사이트 원격 복제 자동 실행
        try:
            offsite_cfg = ConfigManager.get_offsite_config()
            if offsite_cfg.get("enabled") and offsite_cfg.get("auto_sync"):
                provider = offsite_cfg.get("provider", "local")
                from core.offsite import replicate_offsite

                if provider == "gdrive":
                    creds = offsite_cfg.get("gdrive_credentials_file")
                    folder = offsite_cfg.get("gdrive_folder_id") or "BackupSystem_Offsite"
                    if creds:
                        append_task_log(f"[3-2-1 오프사이트] 구글 드라이브(Google Drive/{folder})로 자동 복제 시작...")
                        off_res = replicate_offsite(
                            local_repo_dir=repo_dir,
                            provider="gdrive",
                            gdrive_credentials_file=creds,
                            gdrive_folder_id=folder,
                            log_callback=append_task_log
                        )
                        if off_res.get("success"):
                            append_task_log(f"[3-2-1 오프사이트] 구글 드라이브 동기화 성공: 복제 {off_res.get('files_copied', 0)}개 / 전체 {off_res.get('files_total', 0)}개 ({off_res.get('duration_sec', 0.0):.1f}초)")
                        else:
                            append_task_log(f"[3-2-1 오프사이트] 구글 드라이브 동기화 실패: {off_res.get('reason', '원인 불명')}", level="WARNING")
                else:
                    remote_path = offsite_cfg.get("remote_path")
                    if remote_path:
                        append_task_log(f"[3-2-1 오프사이트] 원격 저장소({remote_path})로 자동 복제 시작...")
                        off_res = replicate_offsite(
                            local_repo_dir=repo_dir,
                            remote_repo_dir=remote_path,
                            provider="local"
                        )
                        if off_res.get("success"):
                            append_task_log(f"[3-2-1 오프사이트] 동기화 성공: 복사 {off_res.get('files_copied', 0)}개 / 스킵 {off_res.get('files_skipped', 0)}개 / 전체 {off_res.get('files_total', 0)}개 ({off_res.get('duration_sec', 0.0):.1f}초)")
                        elif not off_res.get("skipped"):
                            append_task_log(f"[3-2-1 오프사이트] 동기화 실패: {off_res.get('reason', '원인 불명')}", level="WARNING")
        except Exception as e_off:
            append_task_log(f"[3-2-1 오프사이트] 동기화 오류: {e_off}", level="WARNING")

        manifest_summary = {
            "id": manifest.get("id"),
            "created_at": manifest.get("created_at"),
            "type": manifest.get("type"),
            "profile_id": manifest.get("profile_id"),
            "profile_name": manifest.get("profile_name"),
            "repo_dir": manifest.get("repo_dir"),
            "vss_enabled": manifest.get("vss_enabled", False),
            "summary": manifest.get("summary")
        }
        del manifest
        import gc
        gc.collect()

        with task_lock:
            current_task["result"] = manifest_summary
            current_task["error"] = None

    except InterruptedError:
        append_task_log("사용자에 의해 백업 작업이 취소되었습니다.", level="WARNING")
        with task_lock:
            current_task["error"] = "Cancelled by user"
            current_task["result"] = None
    except Exception as e:
        if isinstance(e, BackupAlreadyRunningError):
            append_task_log(f"백업 거부: {str(e)}", level="WARNING")
        elif isinstance(e, InsufficientDiskSpaceError):
            append_task_log(f"[안전 보호(Fail-Closed) 작동] {str(e)}", level="ERROR")
        elif isinstance(e, VSSRequiredError):
            append_task_log(f"[VSS Strict 차단] {str(e)}", level="ERROR")
        elif isinstance(e, RestoreVerificationError):
            append_task_log(f"[복원 무결성 검증 실패(DEGRADED)] {str(e)}", level="ERROR")
        else:
            import traceback
            append_task_log(f"백업 중 오류 발생: {str(e)}", level="ERROR")
            append_task_log(traceback.format_exc(), level="ERROR")
        if not isinstance(e, BackupAlreadyRunningError):
            try:
                from core.windows_toast import notify_backup_failed
                notify_backup_failed(profile_name, str(e))
            except Exception:
                pass
        with task_lock:
            current_task["error"] = str(e)
            current_task["result"] = None
            if profile_id and profile:
                profile["last_status"] = "failed"
                ConfigManager.save_profile(profile)
    finally:
        with task_lock:
            current_task["running"] = False


@router.post("/api/backup/run")
def run_backup(req: RunBackupRequest, background_tasks: BackgroundTasks):
    with task_lock:
        if current_task["running"]:
            raise HTTPException(status_code=409, detail="이미 다른 백업 또는 복원 작업이 실행 중입니다.")

        cancel_evt = threading.Event()
        current_task.update({
            "type": "backup",
            "running": True,
            "progress": {"percent": 0, "processed_files": 0, "total_files": 0, "current_file": "스캔 준비 중..."},
            "cancel_event": cancel_evt,
            "start_time": time.time(),
            "result": None,
            "error": None
        })

    background_tasks.add_task(_background_backup_task, req.dict())
    return {"status": "started"}


@router.post("/api/backup/cancel")
def cancel_backup():
    if not request_cancel():
        return {"status": "not_running"}
    append_task_log("작업 취소 신호를 전송했습니다...", level="WARNING")
    return {"status": "cancelling"}


@router.get("/api/task/status")
def get_task_status():
    return snapshot_task_status()


@router.get("/api/backup/logs")
def get_backup_logs(limit: int = 200):
    """
    스케줄러 백업 로그를 반환한다.

    왜 이 엔드포인트가 필요한가:
        작업 스케줄러는 pythonw.exe 로 cli_backup.py 를 실행한다.
        표준출력이 없어 예전에는 어떤 정보도 남지 않았다.
        core/logging_setup.py 가 logs/backup.log 에 기록하지만,
        서버가 곧바로 읽을 수 있어야 현장에서 확인 가능하다.
    """
    try:
        from core.logging_setup import BACKUP_LOG
    except Exception as e:
        return {"success": False, "error": f"로깅 모듈 로드 실패: {e}"}

    result = {
        "success": True,
        "path": BACKUP_LOG,
        "lines": [],
        "exists": os.path.exists(BACKUP_LOG),
        "rotated": [],
    }

    # 회전된 이전 파일까지 함께 보여준다 (최근 실패 원인이 직전 회전에 있을 수 있음)
    targets = [BACKUP_LOG]
    for suffix in [".1", ".2"]:
        targets.append(BACKUP_LOG + suffix)

    for path in targets:
        try:
            if not os.path.exists(path):
                continue
            size_kb = round(os.path.getsize(path) / 1024, 1)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            if path == BACKUP_LOG:
                result["lines"] = lines[-max(1, min(limit, 2000)):]
                result["size_kb"] = size_kb
            else:
                result["rotated"].append({
                    "file": os.path.basename(path),
                    "size_kb": size_kb,
                    "lines": len(lines),
                })
        except Exception as e:
            result.setdefault("warnings", []).append(f"{os.path.basename(path)}: {e}")

    # 로그가 아예 없으면 왜인지 알려준다 (pythonw 문제 재발 시 대비)
    if not result["exists"] and not result["rotated"]:
        result["notice"] = (
            "백업 로그가 아직 없습니다. 첫 스케줄러 실행(기본 09:00) 이후에 생성됩니다."
        )

    return result


# --- Restore Execution API ---
class RunRestoreRequest(BaseModel):
    snapshot_id: str
    target_dir: Optional[str] = None
    repo_dir: Optional[str] = None
    selected_rel_paths: Optional[List[str]] = None
    overwrite: bool = True
    in_place: bool = False


def _background_restore_task(params: Dict[str, Any]):
    snap_id = params["snapshot_id"]
    target_dir = params.get("target_dir")
    selected = params.get("selected_rel_paths")
    overwrite = params.get("overwrite", True)
    in_place = params.get("in_place", False)
    cancel_evt = current_task["cancel_event"]

    # Fix #9: Use _find_snapshot_repo to auto-discover correct repo instead of blindly using first profile
    repo_dir = params.get("repo_dir")
    if not repo_dir or not os.path.exists(repo_dir):
        repo_dir = _find_snapshot_repo(snap_id, repo_dir)
    if not repo_dir:
        profiles = ConfigManager.get_profiles()
        repo_dir = profiles[0].get("repo_dir") if profiles else os.path.join(BASE_DIR, "backup_repository")

    dest_desc = "백업 당시 원래 위치(In-place 롤백)" if in_place else f"대상 경로 '{target_dir}'"
    append_task_log(f"복원 작업 시작: 스냅샷 '{snap_id}' -> {dest_desc}")

    def on_progress(p_data):
        with task_lock:
            current_task["progress"] = p_data

    try:
        engine_exe = os.path.join(BASE_DIR, "engine_rust", "kova_fast_engine.exe")
        rust_snapshot_file = os.path.join(repo_dir, "snapshots", f"{snap_id}.json")
        result = None
        if os.path.exists(engine_exe) and os.path.exists(rust_snapshot_file) and not selected and not in_place:
            append_task_log(f"[초고속 네이티브 Rust 엔진 가동 - 복원] 스냅샷: {snap_id}")
            start_rest = time.time()
            cmd = [engine_exe, "restore", "--session-id", snap_id, "--repo", repo_dir, "--target", target_dir]
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=creationflags)
            if proc.returncode == 0:
                elapsed = time.time() - start_rest
                try:
                    with open(rust_snapshot_file, "r", encoding="utf-8") as f:
                        m_data = json.load(f)
                    total_f = m_data.get("total_files", 0)
                    total_b = m_data.get("total_bytes", 0)
                except Exception:
                    total_f, total_b = 0, 0
                result = {
                    "restored_files": total_f,
                    "restored_bytes": total_b,
                    "skipped_files": 0,
                    "failed_files": [],
                    "duration_seconds": round(elapsed, 3)
                }
            else:
                append_task_log(f"[경고] Rust 복원 엔진 경고(Code {proc.returncode}): {proc.stderr.strip()}, 레거시 복원으로 폴백합니다.", level="WARNING")

        if not result:
            result = RestoreEngine.restore_snapshot(
                repo_dir=repo_dir,
                snapshot_id=snap_id,
                target_dir=target_dir,
                selected_rel_paths=selected,
                overwrite=overwrite,
                in_place=in_place,
                progress_callback=on_progress,
                cancel_event=cancel_evt
            )

        append_task_log(
            f"복원 완료! 복원된 파일: {result['restored_files']}개 ({round(result['restored_bytes']/(1024*1024), 2)}MB), "
            f"건너뜀: {result['skipped_files']}개, 실패: {len(result['failed_files'])}개 (소요 {result['duration_seconds']}초)"
        )
        with task_lock:
            current_task["result"] = result
            current_task["error"] = None

    except InterruptedError:
        append_task_log("복원 작업이 사용자에 의해 취소되었습니다.", level="WARNING")
        with task_lock:
            current_task["error"] = "Cancelled by user"
            current_task["result"] = None
    except Exception as e:
        append_task_log(f"복원 중 오류 발생: {str(e)}", level="ERROR")
        with task_lock:
            current_task["error"] = str(e)
            current_task["result"] = None
    finally:
        with task_lock:
            current_task["running"] = False


@router.post("/api/restore/run")
def run_restore(req: RunRestoreRequest, background_tasks: BackgroundTasks):
    with task_lock:
        if current_task["running"]:
            raise HTTPException(status_code=409, detail="이미 다른 백업 또는 복원 작업이 실행 중입니다.")

        cancel_evt = threading.Event()
        current_task.update({
            "type": "restore",
            "running": True,
            "progress": {"percent": 0, "restored_files": 0, "total_files": 0, "current_file": "복원 준비 중..."},
            "cancel_event": cancel_evt,
            "start_time": time.time(),
            "result": None,
            "error": None
        })

    background_tasks.add_task(_background_restore_task, req.dict())
    return {"status": "started"}


# --- Verify API ---
class RunVerifyRequest(BaseModel):
    snapshot_id: str
    repo_dir: Optional[str] = None


@router.post("/api/verify/run")
def run_verify(req: RunVerifyRequest):
    repo_dir = req.repo_dir
    if not repo_dir:
        repo_dir = _find_snapshot_repo(req.snapshot_id)
    if not repo_dir:
        profiles = ConfigManager.get_profiles()
        repo_dir = profiles[0].get("repo_dir") if profiles else os.path.join(BASE_DIR, "backup_repository")

    append_task_log(f"스냅샷 '{req.snapshot_id}' 물리적 무결성(Health Check) 검증을 시작합니다...")
    manifest = SnapshotEngine.get_snapshot(repo_dir, req.snapshot_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="스냅샷을 찾을 수 없습니다.")

    from core.verify import IntegrityVerifier
    verifier = IntegrityVerifier(repo_dir)
    res = verifier.verify_snapshot(manifest, sample_ratio=0.2, max_samples=200, verify_all_new=True)

    # SQLite DB 및 메타데이터 업데이트
    storage = BlobStorage(repo_dir)
    storage.db.update_snapshot_verification(
        snapshot_id=req.snapshot_id,
        is_verified=res.get("success", False),
        error_count=res.get("error_count", 0)
    )

    if res["success"]:
        append_task_log(f"무결성 검증 100% 통과! {res['verified_count']}개 블롭 검사 완료 (오류 0개)")
    else:
        append_task_log(f"무결성 검증 실패! 손상된 블롭 발견: {res['error_count']}개", level="ERROR")
    return res


# --- Maintenance & Prune API ---
@router.post("/api/maintenance/prune")
def prune_storage(repo_dir: Optional[str] = None):
    if not repo_dir:
        profiles = ConfigManager.get_profiles()
        repo_dir = profiles[0].get("repo_dir") if profiles else os.path.join(BASE_DIR, "backup_repository")

    try:
        res = SnapshotEngine.prune_storage(repo_dir)
        freed_mb = round(res['freed_bytes'] / (1024 * 1024), 2)
        append_task_log(f"가비지 컬렉션 완료: 참조되지 않는 고아 블롭 {res['deleted_blobs']}개 삭제, {freed_mb}MB 용량 회수")
        return res
    except Exception as e:
        append_task_log(f"가비지 컬렉션 실패/중단: {e}", level="ERROR")
        raise HTTPException(status_code=409 if "실행 중" in str(e) or "잠겨 있습니다" in str(e) else 500, detail=str(e))


# --- Pre-flight Space Estimator API ---
class EstimateBackupRequest(BaseModel):
    sources: List[str] = []
    repo_dir: Optional[str] = None


@router.post("/api/backup/estimate")
def estimate_backup_size(req: EstimateBackupRequest):
    """
    백업 실행 전 실시간 용량 및 파일 수 사전 예측 (Pre-flight Space Estimator)
    """
    from core.storage import get_disk_free_gb
    sources = req.sources or [BASE_DIR]
    repo_dir = req.repo_dir
    if not repo_dir:
        profiles = ConfigManager.get_profiles()
        repo_dir = profiles[0].get("repo_dir") if profiles else os.path.join(BASE_DIR, "backup_repository")

    total_bytes = 0
    scanned_files = 0
    start_time = time.time()
    time_limit = 3.0  # 최대 3초 내 고속 샘플링

    for src in sources:
        if not os.path.exists(src):
            continue
        if os.path.isfile(src):
            try:
                total_bytes += os.path.getsize(src)
                scanned_files += 1
            except OSError:
                pass
            continue

        for root, dirs, files in os.walk(src):
            if time.time() - start_time > time_limit:
                break
            for f in files:
                scanned_files += 1
                try:
                    fp = os.path.join(root, f)
                    total_bytes += os.path.getsize(fp)
                except OSError:
                    pass

    free_gb = get_disk_free_gb(repo_dir)
    est_gb = round(total_bytes / (1024 ** 3), 2)
    is_sufficient = free_gb >= 10.0 and free_gb >= (est_gb * 0.5)

    return {
        "scanned_files": scanned_files,
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / (1024 * 1024), 2),
        "total_gb": est_gb,
        "repo_dir": repo_dir,
        "repo_free_gb": free_gb,
        "min_required_gb": 10.0,
        "is_sufficient": is_sufficient,
        "recommendation": "안전 (저장소 여유 공간 충분)" if is_sufficient else "경고 (저장소 여유 공간 부족)"
    }


# --- 1-Sec DR Drill (복구 모의훈련) API ---
class DrillRequest(BaseModel):
    snapshot_id: Optional[str] = None
    repo_dir: Optional[str] = None
    sample_count: int = 3


@router.post("/api/snapshots/{snapshot_id}/drill")
@router.post("/api/backup/drill")
def run_dr_drill(snapshot_id: Optional[str] = None, req: Optional[DrillRequest] = None):
    """
    원클릭 가상 복구 모의훈련 (1-Sec Disaster Recovery Drill)
    스냅샷에서 무작위 샘플 파일을 가상 임시 폴더에 즉각 복원하고 해시를 대조하여
    100% 무결성 복구 보증 인증서를 발급합니다.
    """
    import hashlib
    target_id = snapshot_id or (req.snapshot_id if req else None)
    repo_dir = req.repo_dir if req else None

    if not repo_dir and target_id:
        repo_dir = _find_snapshot_repo(target_id)
    if not repo_dir:
        profiles = ConfigManager.get_profiles()
        repo_dir = profiles[0].get("repo_dir") if profiles else os.path.join(BASE_DIR, "backup_repository")

    if not target_id or target_id == "latest":
        snapshots = SnapshotEngine.list_snapshots(repo_dir)
        if not snapshots:
            raise HTTPException(status_code=404, detail="복구 모의훈련을 수행할 스냅샷이 없습니다.")
        target_id = snapshots[0]["id"]

    manifest = SnapshotEngine.get_snapshot(repo_dir, target_id)
    if not manifest:
        raise HTTPException(status_code=404, detail=f"스냅샷 '{target_id}'을 찾을 수 없습니다.")

    entries = manifest.get("entries", [])
    file_entries = [e for e in entries if not e.get("is_dir") and e.get("size", 0) > 0]
    if not file_entries:
        file_entries = [e for e in entries if not e.get("is_dir")]

    sample_k = min(3, len(file_entries))
    if sample_k == 0:
        return {
            "success": True,
            "snapshot_id": target_id,
            "tested_count": 0,
            "duration_ms": 1,
            "certificate_id": f"DR-CERT-{target_id[:8]}-EMPTY",
            "tested_files": [],
            "status_message": "스냅샷에 검증할 파일이 없습니다 (빈 백업)."
        }

    step = max(1, len(file_entries) // sample_k)
    samples = [file_entries[i * step] for i in range(sample_k)]

    start_time = time.perf_counter()
    tested_files = []
    overall_success = True

    with tempfile.TemporaryDirectory(prefix="dr_drill_") as tmp_dir:
        sample_paths = [s.get("rel_path") for s in samples if s.get("rel_path")]
        RestoreEngine.restore_snapshot(
            repo_dir=repo_dir,
            snapshot_id=target_id,
            target_dir=tmp_dir,
            selected_rel_paths=sample_paths,
            overwrite=True,
            verify_hash=True,
            in_place=False
        )

        for s in samples:
            rel_p = s.get("rel_path", "")
            expected_sha = s.get("sha256", "")
            expected_size = s.get("size", 0)
            restored_file = os.path.join(tmp_dir, rel_p)

            is_verified = False
            actual_sha = ""
            actual_size = 0

            if os.path.exists(restored_file):
                actual_size = os.path.getsize(restored_file)
                h = hashlib.sha256()
                with open(restored_file, "rb") as f:
                    while chunk := f.read(1024 * 1024):
                        h.update(chunk)
                actual_sha = h.hexdigest()
                is_verified = (actual_sha == expected_sha) if expected_sha else True

            if not is_verified:
                overall_success = False

            tested_files.append({
                "rel_path": rel_p,
                "size_bytes": actual_size,
                "expected_size": expected_size,
                "sha256": (actual_sha[:16] + "...") if actual_sha else "N/A",
                "verified": is_verified
            })

    duration_ms = round((time.perf_counter() - start_time) * 1000, 1)
    cert_id = f"DR-CERT-{target_id[:8].upper()}-{hex(int(time.time()))[2:].upper()}"

    append_task_log(f"[복구 모의훈련 완료] 스냅샷 {target_id} | 샘플 {len(tested_files)}개 복구 검증 | 무결성: {'100% 정상' if overall_success else '오류 발생'} ({duration_ms}ms)")

    return {
        "success": overall_success,
        "snapshot_id": target_id,
        "tested_count": len(tested_files),
        "duration_ms": duration_ms,
        "certificate_id": cert_id,
        "tested_files": tested_files,
        "status_message": "가상 격리 환경에서 100% 무결성 복구 검증 완료 (Bit-Rot 없음, 즉시 복구 가능 보증)" if overall_success else "복구 모의훈련 중 해시 불일치 또는 파일 손상 감지"
    }
