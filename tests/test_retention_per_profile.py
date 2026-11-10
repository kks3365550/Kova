# -*- coding: utf-8 -*-
"""보존 정책이 프로필별로 적용되어 다른 프로필의 스냅샷을 지우지 않는지 확인한다."""
import time

import core.snapshot as snap_mod
from core.retention import RetentionManager


def _snap(sid, profile, age_sec):
    return {"id": sid, "profile_id": profile, "created_at": time.time() - age_sec}


def test_retention_count_applies_per_profile(tmp_path, monkeypatch):
    # A 프로필은 최근 백업이 5개, B 프로필은 오래된 1개
    snaps = [_snap(f"a{i}", "A", i * 10) for i in range(5)] + [_snap("b0", "B", 5000)]
    deleted = []
    monkeypatch.setattr(snap_mod.SnapshotEngine, "list_snapshots", staticmethod(lambda repo: list(snaps)))
    monkeypatch.setattr(
        snap_mod.SnapshotEngine, "delete_snapshot",
        staticmethod(lambda repo, sid, prune_orphaned_blobs=False, authorized=False: deleted.append(sid) or True),
    )
    monkeypatch.setattr(snap_mod.SnapshotEngine, "prune_storage", staticmethod(lambda repo: {}))
    RetentionManager(str(tmp_path)).apply_policy(retention_count=2, retention_days=0, authorized=True)
    assert "b0" not in deleted           # 다른 프로필의 유일한 스냅샷은 보존
    assert sorted(deleted) == ["a2", "a3", "a4"]
