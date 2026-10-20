# -*- coding: utf-8 -*-
"""
core/retention.py: 스마트 저장소 롤링 및 세대 보존 정책 관리자 (Retention Policy Engine)
- 세대 수(Max Count) 기반 롤링
- 보관 기간(Max Age Days) 기반 만료
- 디스크 잔여 여유공간(Min Free GB) 기반 오래된 스냅샷 선제적 정리
- 안전 장치: 최소 1개의 최신 베이스라인 스냅샷은 절대 삭제하지 않음
"""

import os
import time
import logging
from datetime import datetime
from typing import Dict, Any, Optional
from core.storage import get_disk_free_gb

log = logging.getLogger(__name__)


def _parse_timestamp(val: Any) -> float:
    """created_at 값을 float 초 단위 타임스탬프로 안전하게 변환 (ISO 8601 문자열 및 float/int 지원)"""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return 0.0
        try:
            return float(val)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(val).timestamp()
        except Exception:
            pass
    return 0.0


class RetentionManager:
    """스토리지 보존 주기 및 여유 공간을 관리하는 엔진"""

    def __init__(self, repo_dir: str):
        self.repo_dir = os.path.abspath(repo_dir)

    def apply_policy(
        self,
        retention_count: Optional[int] = 30,
        retention_days: Optional[int] = 60,
        min_free_gb: Optional[float] = 20.0,
        authorized: bool = False
    ) -> Dict[str, Any]:
        """
        저장소에 보존 정책을 적용하여 만료된 스냅샷을 정리하고 고아 청크를 회수.
        반환: 정리 결과 통계 딕셔너리
        """
        from core.snapshot import SnapshotEngine

        try:
            snapshots = SnapshotEngine.list_snapshots(self.repo_dir)
        except Exception as e:
            # 스냅샷 목록을 못 읽으면 정리를 수행하지 않는다 (삭제 안전).
            # "no_snapshots" 라는 상태명은 실제 원인(읽기 실패)을 감춘다.
            # 조용히 넘어가면 디스크가 계속 차는데 아무도 모른다.
            log.warning(
                "스냅샷 목록 읽기 실패, 보존 정책 적용 중단 (repo=%s): %s",
                self.repo_dir, e, exc_info=True,
            )
            return {
                "deleted_snapshots": [],
                "freed_bytes": 0,
                "status": "list_failed",
                "error": str(e)[:300],
            }

        if not snapshots or len(snapshots) <= 1:
            # 안전 장치: 스냅샷이 1개 이하일 때는 절대 삭제하지 않음
            return {"deleted_snapshots": [], "freed_bytes": 0, "status": "safe_minimum"}

        # 최신순 정렬 (newest first, float/ISO 문자열 안전 변환)
        snapshots.sort(key=lambda s: _parse_timestamp(s.get("created_at")), reverse=True)

        deleted_ids = set()
        # 안전 보장: 가장 최신 스냅샷(index 0)은 어떤 경우에도 삭제 대상에서 제외
        protected_id = snapshots[0].get("id")

        # 1. 세대 수(retention_count) 초과분 제거
        if retention_count and len(snapshots) > retention_count:
            excess = snapshots[retention_count:]
            for s in excess:
                sid = s.get("id")
                if sid and sid != protected_id:
                    deleted_ids.add(sid)

        # 2. 보관 기간(retention_days) 만료분 제거
        if retention_days and retention_days > 0:
            cutoff_ts = time.time() - (retention_days * 86400)
            for s in snapshots[1:]: # 최신 스냅샷 제외
                if _parse_timestamp(s.get("created_at")) < cutoff_ts:
                    sid = s.get("id")
                    if sid:
                        deleted_ids.add(sid)

        # Note: Fail-Closed Policy:
        # Disk space shortage MUST NEVER cause arbitrary deletion of valid historical snapshots.
        # Insufficient disk space is handled at backup entry time by rejecting new backups (Fail-Closed).
        # Retention policy only prunes expired snapshots based on configured generation count and age days.

        # 실제 삭제 실행
        actually_deleted = []
        for sid in deleted_ids:
            try:
                if SnapshotEngine.delete_snapshot(self.repo_dir, sid, prune_orphaned_blobs=False, authorized=authorized):
                    actually_deleted.append(sid)
            except Exception as e_del:
                log.error("만료 스냅샷 삭제 실패 (repo=%s, snapshot_id=%s): %s", self.repo_dir, sid, e_del, exc_info=True)

        # 고아 블롭 가비지 컬렉션 (GC)
        gc_result = {"deleted_blobs": 0, "freed_bytes": 0}
        if actually_deleted:
            try:
                gc_result = SnapshotEngine.prune_storage(self.repo_dir)
            except Exception as e_gc:
                import logging
                logging.getLogger("BackupSystem").error(f"보존 정책 후 고아 블롭 GC 중단/실패: {e_gc}")

        return {
            "deleted_snapshots": actually_deleted,
            "deleted_blobs": gc_result.get("deleted_blobs", 0),
            "freed_bytes": gc_result.get("freed_bytes", 0),
            "remaining_snapshots": len(snapshots) - len(actually_deleted),
            "free_disk_gb": get_disk_free_gb(self.repo_dir)
        }
