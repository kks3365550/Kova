# -*- coding: utf-8 -*-
"""2차 보안 하드닝 회귀 테스트: 입력 검증, 인증 우회, 삭제 저장소 제한, 로그 마스킹."""
import os

import pytest
from fastapi.testclient import TestClient

import web.app as webapp
from core.auth import resolve_client_ip
from core.diagnostic import _mask_log_secrets

client = TestClient(webapp.app, base_url="http://127.0.0.1:8765")


def test_resolve_client_ip_prefers_forwarded_headers():
    assert resolve_client_ip({}, "127.0.0.1") == "127.0.0.1"
    assert resolve_client_ip({"x-forwarded-for": "203.0.113.9, 10.0.0.1"}, "127.0.0.1") == "203.0.113.9"
    assert resolve_client_ip({"cf-connecting-ip": "198.51.100.7"}, "127.0.0.1") == "198.51.100.7"


def test_rescue_make_usb_rejects_injected_drive_letter():
    r = client.post("/api/rescue/make-usb", json={"drive_letter": "E';Start-Process calc;'"})
    assert r.status_code == 400


def test_migration_rejects_bad_and_system_drive():
    r = client.post("/api/migration/start", json={"target_drive": "E:\\x"})
    assert r.status_code == 400
    sysdrive = os.environ.get("SystemDrive", "C:")
    r = client.post("/api/migration/start", json={"target_drive": sysdrive})
    assert r.status_code == 400


def test_archive_restore_blocks_system_drive_and_cmd_metachar():
    sysdrive = os.environ.get("SystemDrive", "C:")
    r = client.post("/api/migration/archive/restore", json={"wim_path": "D:\\a.wim", "target_drive": sysdrive})
    assert r.status_code == 400
    r = client.post("/api/migration/archive/restore", json={"wim_path": "D:\\a&calc.wim", "target_drive": "E:"})
    assert r.status_code == 400


def test_upload_gdrive_rejects_non_archive_file(tmp_path):
    f = tmp_path / "id_rsa"
    f.write_text("secret")
    r = client.post("/api/migration/archive/upload-gdrive", json={"archive_path": str(f)})
    assert r.status_code == 400


def test_snapshot_delete_rejects_unknown_repo_dir(tmp_path):
    r = client.delete("/api/snapshots/snap_x", params={"repo_dir": str(tmp_path)})
    assert r.status_code == 403


def test_unconfigured_auth_blocks_remote_clients(monkeypatch):
    monkeypatch.setattr(webapp, "is_auth_configured", lambda: False)
    remote = client.get("/api/snapshots", headers={"X-Forwarded-For": "192.168.0.50"})
    assert remote.status_code == 403
    local = client.get("/api/snapshots")
    assert local.status_code != 403


def test_forwarded_request_does_not_get_localhost_bypass(monkeypatch):
    monkeypatch.setattr(webapp, "is_auth_configured", lambda: True)
    r = client.get("/api/snapshots", headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 401


def test_log_mask_recovery_key():
    key = "-".join(["123456"] * 8)
    assert key not in _mask_log_secrets(f"key={key} end")
