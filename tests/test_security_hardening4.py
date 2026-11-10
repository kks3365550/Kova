# -*- coding: utf-8 -*-
"""4차 하드닝 회귀 테스트: SSR XSS 이스케이프, 프로필 병합 저장, 읽기 불가 프로필 보호."""
import json

import core.config as cfg
from core.config import ConfigManager


def _use_tmp_profiles(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cfg, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(cfg, "SETTINGS_FILE", str(tmp_path / "app_settings.json"))
    return tmp_path / "profiles.json"


def test_ssr_json_escapes_script_breakout():
    import web.app as webapp
    evil = "</script><script>alert(1)</script>"
    snaps = [{"id": "s1", "profile_name": evil, "created_at": 1.0, "summary": {}}]
    import core.snapshot as snap_mod
    orig = snap_mod.SnapshotEngine.list_snapshots
    try:
        snap_mod.SnapshotEngine.list_snapshots = staticmethod(lambda repo: list(snaps))
        out = webapp._build_ssr_dashboard_data()
    finally:
        snap_mod.SnapshotEngine.list_snapshots = orig
    assert out["total_snaps"] == "1"
    assert "</script>" not in out["snapshots_json"]
    assert "<script>" not in out["recents_html"]
    assert "<script>" not in out["chart_html"]


def test_update_profile_fields_keeps_other_edits(monkeypatch, tmp_path):
    pf = _use_tmp_profiles(monkeypatch, tmp_path)
    pf.write_text(json.dumps([{"id": "p1", "name": "A", "retention_count": 5}]), encoding="utf-8")
    # 실행 중 사용자가 이름을 바꿨다고 가정 -> 오래된 사본으로 저장해도 name 은 유지되어야 한다
    stale = {"id": "p1", "name": "OLD", "retention_count": 5, "last_status": "success"}
    pf.write_text(json.dumps([{"id": "p1", "name": "NEW", "retention_count": 5}]), encoding="utf-8")
    assert ConfigManager.update_profile_fields("p1", {"last_status": stale["last_status"]})
    saved = json.loads(pf.read_text(encoding="utf-8"))
    assert saved[0]["name"] == "NEW" and saved[0]["last_status"] == "success"


def test_save_profile_refuses_when_file_unreadable(monkeypatch, tmp_path):
    pf = _use_tmp_profiles(monkeypatch, tmp_path)
    pf.write_text("{ not json", encoding="utf-8")
    try:
        ConfigManager.save_profile({"id": "p2", "name": "B"})
        raised = False
    except RuntimeError:
        raised = True
    assert raised
    assert pf.read_text(encoding="utf-8") == "{ not json"  # 손상 파일을 덮어쓰지 않음


def test_chunked_entry_is_not_self_healed(tmp_path):
    from core.verify import IntegrityVerifier
    import inspect
    src = inspect.getsource(IntegrityVerifier.verify_restore_sampling)
    assert "not (isinstance(chunk_ids, list) and chunk_ids)" in src


def test_encryption_requires_passphrase(tmp_path):
    import pytest
    from core.snapshot import SnapshotEngine
    src = tmp_path / "s"
    src.mkdir()
    (src / "a.txt").write_text("x")
    with pytest.raises(ValueError):
        SnapshotEngine.create_snapshot(str(tmp_path / "repo"), [str(src)], encryption_enabled=True,
                                       min_free_disk_gb=0, use_vss=False)


def test_ed25519_verify_fails_closed_without_cryptography(monkeypatch):
    import core.crypto_sign as cs
    monkeypatch.setattr(cs, "HAS_CRYPTOGRAPHY", False)
    assert cs.verify_bytes_ed25519(b"x", "ab", b"k") is False
    assert cs.verify_with_candidates(b"x", "ab", [b"k"]) == (False, None) if hasattr(cs, "verify_with_candidates") else True


def test_skipped_files_counted_in_summary(tmp_path, monkeypatch):
    import core.snapshot as sm
    src = tmp_path / "s"
    src.mkdir()
    (src / "ok.txt").write_text("ok")
    (src / "bad.txt").write_text("bad")
    real = sm.BlobStorage.put_file_blob_onepass

    def flaky(self, path, *a, **k):
        if path.endswith("bad.txt"):
            raise PermissionError("locked")
        return real(self, path, *a, **k)

    monkeypatch.setattr(sm.BlobStorage, "put_file_blob_onepass", flaky)
    m = sm.SnapshotEngine.create_snapshot(str(tmp_path / "repo"), [str(src)], min_free_disk_gb=0, use_vss=False)
    assert m["summary"]["skipped_files"] == 1


def test_prerelease_is_older_than_release():
    from core.updater_v2.policy_semantics import _ver_key
    assert _ver_key("2.17.74-rc1") < _ver_key("2.17.74")
    assert _ver_key("2.17.74") < _ver_key("2.17.75-rc1")
    assert _ver_key("v2.17.74") == _ver_key("2.17.74")
