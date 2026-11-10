# -*- coding: utf-8 -*-
"""보안 하드닝 회귀 테스트: 업로드 경로 탐색, Origin(CSRF), 스냅샷 ID, 아카이브 삭제 가드."""
import os
import tempfile

from fastapi.testclient import TestClient

from web.app import app
from core.drive_archive import DriveArchiveManager

client = TestClient(app, base_url="http://127.0.0.1:8765")


def test_cross_origin_post_blocked():
    r = client.post("/api/maintenance/clean-incomplete", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/maintenance/clean-incomplete", headers={"Origin": "null"})
    assert r.status_code == 403


def test_upload_creds_rejects_traversal_and_non_json():
    r = client.post("/api/offsite/upload_creds", files={"file": ("..\\..\\x.bat", b"x")})
    assert r.status_code == 400
    r = client.post("/api/offsite/upload_creds", files={"file": ("../evil.json.exe", b"{}")})
    assert r.status_code == 400


def test_snapshot_id_traversal_rejected():
    r = client.post("/api/verify/run", json={"snapshot_id": "..\\..\\x"})
    assert r.status_code == 422
    r = client.post("/api/restore/run", json={"snapshot_id": "../x"})
    assert r.status_code == 422
    r = client.post("/api/snapshots/..%5Cx/drill", json={})
    assert r.status_code in (400, 404, 422)


def test_delete_archive_guard():
    mgr = DriveArchiveManager.get_instance()
    with tempfile.TemporaryDirectory(prefix="kovaX") as d:
        repo = os.path.join(d, "MyBackup_Repository")
        os.makedirs(os.path.join(repo, "snapshots"))
        snap = os.path.join(repo, "snapshots", "snap_x.json")
        open(snap, "w").write("{}")
        # 'temp' 가 경로에 있어도 가드가 먼저 막으므로 별도 비-temp 경로가 필요 -> 가드 로직 자체는 temp 포함 시 거부
        res = mgr.delete_archive(repo)
        assert not res["success"] and os.path.exists(snap)
        res = mgr.delete_archive(snap)
        assert not res["success"] and os.path.exists(snap)


def test_key_pair_regenerated_when_private_missing(tmp_path):
    from core.crypto_sign import Ed25519Signer
    s1 = Ed25519Signer(str(tmp_path))
    s1.ensure_key_pair()
    os.remove(s1.private_key_path)  # 개인키만 삭제
    s2 = Ed25519Signer(str(tmp_path))
    s2.ensure_key_pair()
    manifest = {"id": "snap_x", "files": []}
    sig = s2.sign_manifest(dict(manifest))
    signed = dict(manifest)
    signed["ed25519_signature"] = sig
    assert s2.verify_manifest(signed)


def test_restore_task_always_releases_running_flag(monkeypatch):
    import web.api_backup as ab
    ab.current_task["running"] = True
    ab.current_task["cancel_event"] = __import__("threading").Event()
    monkeypatch.setattr(ab, "_find_snapshot_repo", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    ab._background_restore_task({"snapshot_id": "snap_x", "target_dir": "C:\nonexistent_kova_dir"})
    assert ab.current_task["running"] is False


def test_prune_zero_means_off_not_default(monkeypatch, tmp_path):
    from core.snapshot import SnapshotEngine
    import core.snapshot as ret
    seen = {}

    class FakeMgr:
        def __init__(self, repo_dir):
            pass

        def apply_policy(self, **kw):
            seen.update(kw)
            return {"deleted": []}

    monkeypatch.setattr(ret, "RetentionManager", FakeMgr)
    try:
        SnapshotEngine.prune_snapshots(str(tmp_path), retention_count=0, max_age_days=0, authorized=True)
    except Exception:
        pass
    assert seen, "apply_policy 가 호출되어야 한다"
    assert seen["retention_count"] == 0 and seen["retention_days"] == 0
