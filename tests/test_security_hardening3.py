# -*- coding: utf-8 -*-
"""3차 하드닝 회귀 테스트: PBKDF2 업그레이드, 실패한 복제의 매니페스트 보류, USB 상태 복사."""
import json

import core.auth as auth
from core.replication import ReplicationManager


def _use_tmp_config(monkeypatch, tmp_path):
    store = {}
    monkeypatch.setattr(auth, "_load_auth_config", lambda: dict(store))
    monkeypatch.setattr(auth, "_save_auth_config", lambda cfg: (store.clear(), store.update(cfg)))
    return store


def test_new_password_uses_current_iterations(monkeypatch, tmp_path):
    store = _use_tmp_config(monkeypatch, tmp_path)
    auth.setup_master_password("password-1234", True)
    assert store["pbkdf2_iterations"] == auth._PBKDF2_ITER_CURRENT
    assert auth.verify_master_password("password-1234")
    assert not auth.verify_master_password("wrong-password")


def test_legacy_hash_still_verifies_and_is_upgraded(monkeypatch, tmp_path):
    store = _use_tmp_config(monkeypatch, tmp_path)
    salt = "00" * 16
    store.update({
        "configured": True,
        "password_hash": auth._hash_password("legacy-pass-1", salt, auth._PBKDF2_ITER_LEGACY),
        "salt": salt,
    })
    assert auth.verify_master_password("legacy-pass-1")
    assert store["pbkdf2_iterations"] == auth._PBKDF2_ITER_CURRENT
    assert store["salt"] != salt
    assert auth.verify_master_password("legacy-pass-1")  # 업그레이드 후에도 로그인 가능


def test_manifest_not_copied_when_blob_replication_failed(tmp_path):
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    (local / "blobs").mkdir(parents=True)
    (local / "snapshots").mkdir()
    remote.mkdir()
    blob_id = "ab" + "0" * 62  # 로컬에도 packs 에도 없음 -> 복제 실패
    manifest = {"id": "snap_f", "entries": [{"rel_path": "a.txt", "blob_id": blob_id, "sha256": blob_id}]}
    (local / "snapshots" / "snap_f.json").write_text(json.dumps(manifest), encoding="utf-8")
    res = ReplicationManager(str(local), str(remote)).replicate_snapshot("snap_f")
    assert res["status"] == "error"
    assert not (remote / "snapshots" / "snap_f.json").exists()


def test_usb_status_returns_copied_log_list():
    import web.api_rescue as rescue
    rescue._usb_task_state["log"] = ["a"]
    snap = rescue.get_usb_status()
    snap["log"].append("b")
    assert rescue._usb_task_state["log"] == ["a"]


def test_backup_lock_cleans_up_when_write_fails(monkeypatch, tmp_path):
    import os
    from core.lock import BackupLock
    real_write = os.write
    calls = {"n": 0}

    def flaky_write(fd, data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(28, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", flaky_write)
    lock = BackupLock(str(tmp_path), timeout_sec=1.0)
    try:
        lock.acquire()
        raised = False
    except OSError:
        raised = True
    assert raised
    # 실패 후에도 같은 저장소에서 다시 락을 잡을 수 있어야 한다
    lock2 = BackupLock(str(tmp_path), timeout_sec=1.0)
    assert lock2.acquire()
    lock2.release()


def test_replication_queue_shared_instance_per_repo(tmp_path):
    from core.replication_queue import ReplicationQueueManager
    a = ReplicationQueueManager.get_shared(str(tmp_path))
    b = ReplicationQueueManager.get_shared(str(tmp_path))
    assert a is b
