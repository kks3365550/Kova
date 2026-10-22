# -*- coding: utf-8 -*-
"""스케줄러 자동 재시도 정책 및 Rust 매니페스트 복원 검증 테스트."""
import time
import pytest

from core.scheduler import BackupScheduler, MAX_AUTO_RETRIES, RETRY_COOLDOWN_SEC


def _profile(**kw):
    p = {"auto_backup_enabled": True, "schedule_type": "interval_hours",
         "schedule_value": "24", "last_run": time.time()}
    p.update(kw)
    return p


@pytest.fixture
def sched():
    return BackupScheduler()


def test_failed_profile_retries_after_cooldown(sched):
    p = _profile(last_status="failed", retry_count=0,
                 last_run=time.time() - RETRY_COOLDOWN_SEC - 5)
    assert sched._should_run_profile(p) is True


def test_failed_profile_waits_during_cooldown(sched):
    p = _profile(last_status="failed", retry_count=0, last_run=time.time() - 10)
    assert sched._should_run_profile(p) is False


def test_retry_limit_falls_back_to_normal_schedule(sched):
    p = _profile(last_status="failed", retry_count=MAX_AUTO_RETRIES,
                 last_run=time.time() - RETRY_COOLDOWN_SEC - 5)
    assert sched._should_run_profile(p) is False


def test_disabled_profile_never_retries(sched):
    p = _profile(auto_backup_enabled=False, last_status="failed", retry_count=0,
                 last_run=time.time() - RETRY_COOLDOWN_SEC - 5)
    assert sched._should_run_profile(p) is False


def test_verify_rust_snapshot_skips_without_entries(sched):
    logs = []
    sched.register_log_callback(lambda m, lv: logs.append((lv, m)))
    try:
        sched._verify_rust_snapshot("p", "nonexistent", {"entries": []})
    finally:
        sched.log_callbacks.pop()
    assert any(lv == "WARNING" for lv, _ in logs)


def test_verify_rust_snapshot_raises_on_failed_status(sched, monkeypatch):
    from core import verify

    class FakeVerifier:
        def __init__(self, repo_dir): pass
        def verify_restore_sampling(self, manifest, sample_count=20):
            return {"status": "failed", "detail": "hash mismatch"}

    monkeypatch.setattr(verify, "IntegrityVerifier", FakeVerifier)
    with pytest.raises(verify.RestoreVerificationError):
        sched._verify_rust_snapshot("p", "r", {"entries": [{"path": "a"}]})


def test_verify_rust_snapshot_passes(sched, monkeypatch):
    from core import verify

    class FakeVerifier:
        def __init__(self, repo_dir): pass
        def verify_restore_sampling(self, manifest, sample_count=20):
            return {"status": "passed"}

    monkeypatch.setattr(verify, "IntegrityVerifier", FakeVerifier)
    sched._verify_rust_snapshot("p", "r", {"entries": [{"path": "a"}]})
