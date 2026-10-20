# -*- coding: utf-8 -*-
"""
web/api_snapshots.py

스냅샷 관리 및 조회 APIRouter 모듈
"""
import os
import re
from typing import Optional, List
import psutil
from fastapi import APIRouter, HTTPException

from core.config import ConfigManager
from core.snapshot import SnapshotEngine
from core.retention import _parse_timestamp
from web.paths import BASE_DIR
from web.state import append_task_log

router = APIRouter()


def _validate_snapshot_id(snapshot_id: str) -> str:
    if not snapshot_id or not re.match(r'^[a-zA-Z0-9_\-]+$', snapshot_id):
        raise HTTPException(status_code=400, detail="유효하지 않은 스냅샷 식별자 형식입니다.")
    return snapshot_id


def _get_all_candidate_repos(repo_dir: Optional[str] = None) -> List[str]:
    """Auto-discovers all active backup repositories across all drives and profiles."""
    candidates = set()
    if repo_dir:
        candidates.add(os.path.abspath(repo_dir))

    profiles = ConfigManager.get_profiles()
    for p in profiles:
        r = p.get("repo_dir")
        if r:
            candidates.add(os.path.abspath(r))

    # Standard default repository locations
    candidates.add(os.path.abspath(os.path.join(BASE_DIR, "backup_repository")))
    candidates.add(os.path.abspath(os.path.join(os.path.expanduser("~"), "MyBackup_Repository")))

    # Scan all valid mounted drive letters for drive root, MyBackup_Repository, or backup_repository
    mounted_roots = set()
    try:
        for part in psutil.disk_partitions(all=False):
            if part.mountpoint:
                mounted_roots.add(part.mountpoint)
    except Exception:
        pass

    if not mounted_roots:
        # Fallback to standard drive letters if psutil query fails
        for letter in "CDEF":
            d = f"{letter}:\\"
            if os.path.exists(d):
                mounted_roots.add(d)

    for m_root in mounted_roots:
        for repo_cand in (os.path.join(m_root, "MyBackup_Repository"), os.path.join(m_root, "backup_repository")):
            try:
                if os.path.exists(os.path.join(repo_cand, "snapshots")) and os.path.exists(os.path.join(repo_cand, "blobs")):
                    candidates.add(os.path.abspath(repo_cand))
            except (PermissionError, OSError):
                pass

    valid = [r for r in candidates if os.path.exists(r)]

    def _latest_snap_time(r: str) -> float:
        s_dir = os.path.join(r, "snapshots")
        try:
            files = [os.path.join(s_dir, f) for f in os.listdir(s_dir) if f.endswith(".json")]
            return max([os.path.getmtime(f) for f in files]) if files else 0.0
        except Exception:
            return 0.0

    valid.sort(key=_latest_snap_time, reverse=True)
    return valid


def _find_snapshot_repo(snapshot_id: str, repo_dir: Optional[str] = None) -> Optional[str]:
    if repo_dir and os.path.exists(repo_dir):
        return repo_dir
    repos = _get_all_candidate_repos(repo_dir)
    for r in repos:
        snap_file = os.path.join(r, "snapshots", f"{snapshot_id}.json")
        if os.path.exists(snap_file):
            return r
    return None


@router.get("/api/snapshots")
def list_snapshots(repo_dir: Optional[str] = None):
    repos = [repo_dir] if repo_dir and os.path.exists(repo_dir) else _get_all_candidate_repos(repo_dir)
    all_snaps = []
    seen_ids = set()

    for r in repos:
        try:
            snaps = SnapshotEngine.list_snapshots(r)
        except Exception:
            snaps = []
        
        # 1. 스냅샷 ID 수집 및 중복 제거
        repo_snap_ids = []
        for raw_s in snaps:
            s = raw_s.copy()
            sid = s.get("id")
            if sid and sid not in seen_ids:
                seen_ids.add(sid)
                repo_snap_ids.append(sid)
                s["repo_dir"] = r
                s["is_local_protected"] = s.get("is_verified", True)
                all_snaps.append(s)

        # 2. 리포지토리별 1회 배치 조회 (원격지 실제 저장소 검증 + 레플리케이션 큐 통합)
        if repo_snap_ids:
            remote_repo = None
            try:
                from core.config import ConfigManager
                off_cfg = ConfigManager.get_offsite_config()
                if off_cfg.get("enabled") and off_cfg.get("remote_path"):
                    cand_remote = off_cfg.get("remote_path")
                    if os.path.exists(cand_remote):
                        remote_repo = cand_remote
            except Exception:
                pass

            remote_snaps_set = set()
            if remote_repo:
                remote_snap_dir = os.path.join(remote_repo, "snapshots")
                if os.path.isdir(remote_snap_dir):
                    try:
                        remote_snaps_set = {
                            fname[:-5] for fname in os.listdir(remote_snap_dir) if fname.endswith(".json")
                        }
                    except Exception:
                        pass

            try:
                from core.replication_queue import ReplicationQueueManager
                rq = ReplicationQueueManager(r)
                batch_status = rq.get_status_batch(repo_snap_ids)
            except Exception:
                batch_status = {}

            for s in all_snaps:
                if s.get("repo_dir") == r:
                    sid = s.get("id")
                    q_info = batch_status.get(sid)
                    q_state = q_info.get("state") if q_info else None

                    # 원격지 저장소에 실제 파일이 존재하거나 큐에서 커밋 완료된 경우
                    if sid in remote_snaps_set or q_state == "COMMITTED":
                        s["offsite_status"] = "COMMITTED"
                        s["is_offsite_protected"] = True
                    elif q_state in ("TRANSFERRING", "PENDING"):
                        s["offsite_status"] = q_state
                        s["is_offsite_protected"] = False
                    elif remote_repo:
                        s["offsite_status"] = "UNSYNCED"
                        s["is_offsite_protected"] = False
                    else:
                        s["offsite_status"] = "DISABLED"
                        s["is_offsite_protected"] = False

    all_snaps.sort(key=lambda x: _parse_timestamp(x.get("created_at")), reverse=True)
    return all_snaps


@router.get("/api/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: str, repo_dir: Optional[str] = None, include_entries: bool = False):
    snapshot_id = _validate_snapshot_id(snapshot_id)
    r = _find_snapshot_repo(snapshot_id, repo_dir)
    if not r:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    data = SnapshotEngine.get_snapshot(r, snapshot_id)
    if not data:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    # 오프사이트 복제 검증
    is_offsite_protected = False
    offsite_status = "DISABLED"
    try:
        from core.config import ConfigManager
        off_cfg = ConfigManager.get_offsite_config()
        if off_cfg.get("enabled") and off_cfg.get("remote_path"):
            cand_remote = off_cfg.get("remote_path")
            remote_snap_file = os.path.join(cand_remote, "snapshots", f"{snapshot_id}.json")
            if os.path.exists(remote_snap_file):
                is_offsite_protected = True
                offsite_status = "COMMITTED"
            else:
                offsite_status = "UNSYNCED"
    except Exception:
        pass

    if not include_entries:
        # Strip massive entries array to send lightweight metadata (0.001s response, ~500 bytes)
        return {
            "id": data.get("id"),
            "created_at": data.get("created_at"),
            "iso_time": data.get("iso_time"),
            "profile_id": data.get("profile_id"),
            "profile_name": data.get("profile_name"),
            "backup_type": data.get("backup_type"),
            "base_snapshot_id": data.get("base_snapshot_id"),
            "sources": data.get("sources", []),
            "summary": data.get("summary", {}),
            "is_offsite_protected": is_offsite_protected,
            "offsite_status": offsite_status
        }
    data["is_offsite_protected"] = is_offsite_protected
    data["offsite_status"] = offsite_status
    return data


@router.get("/api/snapshots/{snapshot_id}/browse")
def browse_snapshot(snapshot_id: str, subpath: str = "", repo_dir: Optional[str] = None):
    snapshot_id = _validate_snapshot_id(snapshot_id)
    if ".." in subpath or subpath.startswith("/") or subpath.startswith("\\"):
        raise HTTPException(status_code=400, detail="유효하지 않은 탐색 경로입니다.")
    r = _find_snapshot_repo(snapshot_id, repo_dir)
    if not r:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    try:
        return SnapshotEngine.browse_snapshot_directory(r, snapshot_id, subpath=subpath)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/snapshots/{snapshot_id}/tree")
def get_snapshot_tree(snapshot_id: str, repo_dir: Optional[str] = None):
    snapshot_id = _validate_snapshot_id(snapshot_id)
    r = _find_snapshot_repo(snapshot_id, repo_dir)
    if not r:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    data = SnapshotEngine.get_snapshot(r, snapshot_id)
    if not data:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return SnapshotEngine.build_snapshot_tree(data)


@router.delete("/api/snapshots/{snapshot_id}")
def delete_snapshot(snapshot_id: str, repo_dir: Optional[str] = None):
    snapshot_id = _validate_snapshot_id(snapshot_id)
    r = _find_snapshot_repo(snapshot_id, repo_dir)
    if not r:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    success = SnapshotEngine.delete_snapshot(r, snapshot_id, authorized=True)
    if not success:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    append_task_log(f"스냅샷 '{snapshot_id}'이 삭제되었습니다.")
    return {"success": True}
