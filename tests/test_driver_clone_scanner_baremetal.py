# -*- coding: utf-8 -*-
"""driver_backup, clone_worker, app_scanner(캐시), baremetal_clonix 단위 테스트.

pnputil / powershell / bcdboot 는 모두 대체하며 실제 시스템을 바꾸지 않는다.
"""
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core import app_scanner, baremetal_clonix as bm, clone_worker, driver_backup as db


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ------------------------------------------------------------- driver_backup

def test_export_reuses_manifest_without_calling_pnputil(tmp_path, monkeypatch):
    (tmp_path / ".driver_manifest.json").write_text(
        json.dumps({"success": True, "driver_count": 7}), encoding="utf-8")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("pnputil 호출됨"))
    r = db.export_windows_drivers(str(tmp_path))
    assert r["reused"] is True and r["driver_count"] == 7


def test_export_reuses_existing_inf_files_and_writes_manifest(tmp_path, monkeypatch):
    for i in range(10):
        (tmp_path / ("d%d.inf" % i)).write_text("x")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("pnputil 호출됨"))
    r = db.export_windows_drivers(str(tmp_path))
    assert r["reused"] and r["driver_count"] == 10
    assert (tmp_path / ".driver_manifest.json").exists()


def test_export_runs_pnputil_and_counts_drivers(tmp_path, monkeypatch):
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        (tmp_path / "a.inf").write_text("x")
        return _proc(0, "내보내기 완료")

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = db.export_windows_drivers(str(tmp_path))
    assert seen[0][:2] == ["pnputil", "/export-driver"]
    assert r["success"] and r["driver_count"] == 1 and r["reused"] is False
    assert (tmp_path / ".driver_manifest.json").exists()


def test_export_failure_does_not_write_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _proc(1, "오류"))
    r = db.export_windows_drivers(str(tmp_path))
    assert r["success"] is False
    assert not (tmp_path / ".driver_manifest.json").exists()


def test_export_force_ignores_manifest(tmp_path, monkeypatch):
    (tmp_path / ".driver_manifest.json").write_text(json.dumps({"success": True}), encoding="utf-8")
    called = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: called.append(cmd) or _proc(0))
    db.export_windows_drivers(str(tmp_path), force=True)
    assert called


def test_export_subprocess_exception_is_reported(tmp_path, monkeypatch):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 180)

    monkeypatch.setattr(subprocess, "run", boom)
    r = db.export_windows_drivers(str(tmp_path))
    assert r["success"] is False and r["error"]


def test_install_missing_dir():
    r = db.install_windows_drivers(os.path.join(BASE_DIR, "no_such_drivers_dir"))
    assert r["success"] is False and "not found" in r["error"]


@pytest.mark.parametrize("code,ok,reboot", [(0, True, False), (259, True, True),
                                            (3010, True, True), (1, False, False)])
def test_install_return_codes(tmp_path, monkeypatch, code, ok, reboot):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _proc(code, "out"))
    r = db.install_windows_drivers(str(tmp_path))
    assert r["success"] is ok and r["reboot_required"] is reboot


def test_install_exception_is_reported(tmp_path, monkeypatch):
    def boom(cmd, **kw):
        raise OSError("pnputil 없음")

    monkeypatch.setattr(subprocess, "run", boom)
    r = db.install_windows_drivers(str(tmp_path))
    assert r["success"] is False and "pnputil" in r["error"]


# -------------------------------------------------------------- clone_worker

def _read_ipc(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def test_clone_worker_success_passes_args_and_emits_done(tmp_path, monkeypatch):
    import core.block_stream as bs
    ipc = str(tmp_path / "ipc.jsonl")
    seen = {}

    def fake_stream(**kw):
        seen.update(kw)
        kw["progress_callback"]({"percent": 100.0})
        return {"bytes": 1}

    monkeypatch.setattr(bs, "stream_volume_to_volume", fake_stream)
    monkeypatch.setattr(sys, "argv", ["clone_worker.py", "--src", "C:", "--dst", "E:",
                                      "--shadow", "  ", "--ipc-file", ipc, "--chunk-mb", "8"])
    with pytest.raises(SystemExit) as ei:
        clone_worker.main()
    assert ei.value.code == 0
    assert seen["src_shadow_path"] is None and seen["chunk_bytes"] == 8 * 1024 * 1024
    types = [m["type"] for m in _read_ipc(ipc)]
    assert types[0] == "status" and "progress" in types and types[-1] == "done"


def test_clone_worker_failure_emits_error_and_exits_1(tmp_path, monkeypatch):
    import core.block_stream as bs
    ipc = str(tmp_path / "ipc.jsonl")

    def boom(**kw):
        raise PermissionError("WinError 5")

    monkeypatch.setattr(bs, "stream_volume_to_volume", boom)
    monkeypatch.setattr(sys, "argv", ["clone_worker.py", "--dst", "E:", "--ipc-file", ipc])
    with pytest.raises(SystemExit) as ei:
        clone_worker.main()
    assert ei.value.code == 1
    assert _read_ipc(ipc)[-1] == {"type": "error", "error": "WinError 5"}


# --------------------------------------------------------------- app_scanner

@pytest.fixture
def reset_scanner(monkeypatch):
    monkeypatch.setattr(app_scanner, "_cached_apps", None)
    monkeypatch.setattr(app_scanner, "_cached_apps_time", 0)


def test_scanner_caches_result(monkeypatch, reset_scanner):
    calls = []
    monkeypatch.setattr(app_scanner, "_scan_installed_applications_impl",
                        lambda: calls.append(1) or [{"name": "A"}])
    assert app_scanner.get_installed_applications() == [{"name": "A"}]
    assert app_scanner.get_installed_applications() == [{"name": "A"}]
    assert len(calls) == 1


def test_scanner_force_refresh_and_ttl(monkeypatch, reset_scanner):
    calls = []
    monkeypatch.setattr(app_scanner, "_scan_installed_applications_impl",
                        lambda: calls.append(1) or [])
    app_scanner.get_installed_applications()
    app_scanner.get_installed_applications(force_refresh=True)
    assert len(calls) == 2
    monkeypatch.setattr(app_scanner, "_cached_apps_time", 0)  # TTL 만료
    app_scanner.get_installed_applications()
    assert len(calls) == 3


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows 전용")
def test_scanner_real_scan_returns_sorted_dicts():
    apps = app_scanner._scan_installed_applications_impl()
    assert isinstance(apps, list)
    keys = [(0 if a["has_location"] else 1, a["name"].lower()) for a in apps]
    assert keys == sorted(keys)


# ----------------------------------------------------------- baremetal_clonix

def test_disk_layout_parses_json(monkeypatch):
    payload = {"DiskNumber": 0, "PartitionStyle": "GPT", "Partitions": []}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _proc(0, json.dumps(payload) + "\n"))
    assert bm.BareMetalClonixEngine.get_disk_layout(0) == payload


def test_disk_layout_failure_and_bad_json(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _proc(1, "", "액세스 거부"))
    with pytest.raises(RuntimeError, match="조회 실패"):
        bm.BareMetalClonixEngine.get_disk_layout(2)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _proc(0, "not json"))
    with pytest.raises(RuntimeError, match="JSON"):
        bm.BareMetalClonixEngine.get_disk_layout(0)


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows 전용")
def test_volume_allocation_info_on_system_drive():
    drive = os.environ.get("SystemDrive", "C:")
    info = bm.BareMetalClonixEngine.get_volume_allocation_info(drive + "\\")
    assert info["volume"] == drive and info["cluster_size"] > 0
    assert info["total_bytes"] == info["used_bytes"] + info["free_bytes"]
    assert 0 <= info["sparse_skip_ratio"] <= 100


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows 전용")
def test_volume_allocation_info_invalid_drive_raises():
    with pytest.raises(OSError):
        bm.BareMetalClonixEngine.get_volume_allocation_info("9:")


def test_repair_uefi_builds_bcdboot_command(tmp_path, monkeypatch):
    (tmp_path / "Windows").mkdir()
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: seen.append(cmd) or _proc(0))
    assert bm.BareMetalClonixEngine.repair_uefi_bootloader(str(tmp_path), "S:\\") is True
    cmd = seen[0]
    assert cmd[0] == "bcdboot.exe" and cmd[1] == os.path.join(str(tmp_path), "Windows")
    assert cmd[cmd.index("/s") + 1] == "S:" and cmd[cmd.index("/f") + 1] == "UEFI"


def test_repair_uefi_falls_back_to_dir_and_reports_failure(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: seen.append(cmd) or _proc(1))
    assert bm.BareMetalClonixEngine.repair_uefi_bootloader(str(tmp_path), "S") is False
    assert seen[0][1] == str(tmp_path) and seen[0][3] == "S:"


def test_manifest_combines_layout_and_allocation(monkeypatch):
    eng = bm.BareMetalClonixEngine
    monkeypatch.setattr(eng, "get_disk_layout", staticmethod(lambda n: {"PartitionStyle": "GPT"}))
    monkeypatch.setattr(eng, "get_volume_allocation_info", staticmethod(lambda v: {"volume": v}))
    m = eng.create_baremetal_manifest(3, "D:")
    assert m["engine"] == "Kova-BareMetal-Clonix" and m["source_disk"] == 3
    assert m["os_volume_allocation"] == {"volume": "D:"} and m["uefi_enabled"] is True
    monkeypatch.setattr(eng, "get_disk_layout", staticmethod(lambda n: {"PartitionStyle": "MBR"}))
    assert eng.create_baremetal_manifest()["uefi_enabled"] is False
