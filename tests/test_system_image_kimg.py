# -*- coding: utf-8 -*-
"""SystemImageManager kimg 선택 엔진: 검증 통과 시에만 성공, 실패하면 wbadmin으로 대체."""
import os
import time
import subprocess

from core.system_image import SystemImageManager as M


class FakeProc:
    def __init__(self, rc, lines=()):
        self.returncode = rc
        self.stdout = iter([(x + "\n").encode() for x in lines])

    def wait(self):
        return self.returncode


import pytest


@pytest.fixture(autouse=True)
def _reset_state():
    M._process = None
    yield
    M._process = None
    M._is_running = False
    M._status = "idle"


def _setup(monkeypatch, tmp_path, write_rc=0, verify_rc=0, exe=True, admin=True):
    calls = []
    monkeypatch.setattr(M, "_find_kimg_exe", staticmethod(lambda: "kimg.exe" if exe else None))
    monkeypatch.setattr(M, "is_admin", classmethod(lambda cls: admin))
    monkeypatch.setattr("core.system_image.shutil.disk_usage", lambda p: (10**12, 10**9, 10**12))

    def fake_run(cmd, **kw):
        calls.append(("run", cmd))
        out = "abc-id|SHADOWDEV" if "Create" in cmd[-1] else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    def fake_popen(cmd, **kw):
        calls.append(("popen", cmd))
        target = cmd[cmd.index("--target") + 1] if "--target" in cmd else cmd[cmd.index("--image") + 1]
        if cmd[1] == "write" and write_rc == 0:
            with open(target, "wb") as f:
                f.write(b"x")
        return FakeProc(write_rc if cmd[1] == "write" else verify_rc, ["msg"])

    monkeypatch.setattr("core.system_image.subprocess.run", fake_run)
    monkeypatch.setattr("core.system_image.subprocess.Popen", fake_popen)
    M._status = "running"
    return calls


def _images(tmp_path):
    d = tmp_path / "KovaImages"
    return sorted(os.listdir(d)) if d.exists() else []


def test_kimg_ok(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path)
    assert M._run_kimg_backup(str(tmp_path), time.time()) == "ok"
    assert M._status == "completed"
    files = [f for f in _images(tmp_path) if f.endswith(".kimg")]
    assert len(files) == 1
    assert [c[1][1] for c in calls if c[0] == "popen"] == ["write", "verify"]
    assert "Delete" in calls[-1][1][-1]  # 스냅샷 정리
    create = [c[1][-1] for c in calls if c[0] == "run"][0]
    assert "Create('C:" + chr(92) + "','ClientAccessible')" in create  # 백슬래시 유실 방지
    assert "TrimEnd('" + chr(92) + "')" in create


def test_kimg_verify_fail(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, verify_rc=1)
    assert M._run_kimg_backup(str(tmp_path), time.time()) == "failed"
    assert _images(tmp_path) == []  # .part 제거
    assert "Delete" in calls[-1][1][-1]


def test_kimg_write_fail(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, write_rc=1)
    assert M._run_kimg_backup(str(tmp_path), time.time()) == "failed"
    assert [c[1][1] for c in calls if c[0] == "popen"] == ["write"]


def test_kimg_missing_exe_or_not_admin(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, exe=False)
    assert M._run_kimg_backup(str(tmp_path), time.time()) == "failed"
    _setup(monkeypatch, tmp_path, admin=False)
    assert M._run_kimg_backup(str(tmp_path), time.time()) == "failed"


def test_kimg_cancelled(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    def cancel_popen(cmd, **kw):
        M._status = "cancelled"
        return FakeProc(1)

    monkeypatch.setattr("core.system_image.subprocess.Popen", cancel_popen)
    assert M._run_kimg_backup(str(tmp_path), time.time()) == "cancelled"
    assert _images(tmp_path) == []


def _run_start(monkeypatch, kimg_result):
    seen = {"wbadmin": False}
    monkeypatch.setattr(M, "_run_kimg_backup", classmethod(lambda cls, *a, **k: kimg_result))
    monkeypatch.setattr(M, "is_admin", classmethod(lambda cls: True))

    def fake_popen(cmd, **kw):
        seen["wbadmin"] = True
        raise OSError("stop")

    monkeypatch.setattr("core.system_image.subprocess.Popen", fake_popen)
    M._is_running = False
    M.start_backup("D:", engine="kimg")
    for _ in range(100):
        if not M._is_running:
            break
        time.sleep(0.05)
    return seen["wbadmin"]


def test_fallback_to_wbadmin_when_kimg_failed(monkeypatch):
    assert _run_start(monkeypatch, "failed") is True


def test_no_wbadmin_when_kimg_ok(monkeypatch):
    assert _run_start(monkeypatch, "ok") is False


def test_kimg_meta_written(monkeypatch, tmp_path):
    import json
    _setup(monkeypatch, tmp_path)
    layout = {"partition_style": "GPT", "firmware": "UEFI", "partitions": [{"number": 1}]}
    monkeypatch.setattr(M, "_collect_disk_layout", staticmethod(lambda: layout))
    assert M._run_kimg_backup(str(tmp_path), time.time()) == "ok"
    names = _images(tmp_path)
    img = [n for n in names if n.endswith(".kimg")][0]
    assert img + ".json" in names and not any(n.endswith(".tmp") for n in names)
    meta = json.load(open(tmp_path / "KovaImages" / (img + ".json"), encoding="utf-8"))
    assert meta["format"] == 1 and meta["layout"] == layout and meta["image_bytes"] == 1


def test_kimg_meta_layout_none_still_ok(monkeypatch, tmp_path):
    import json
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(M, "_collect_disk_layout", staticmethod(lambda: None))
    assert M._run_kimg_backup(str(tmp_path), time.time()) == "ok"
    js = [n for n in _images(tmp_path) if n.endswith(".json")][0]
    assert json.load(open(tmp_path / "KovaImages" / js, encoding="utf-8"))["layout"] is None


def test_kimg_meta_failure_keeps_ok(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise OSError("disk")
    monkeypatch.setattr(M, "_write_kimg_meta", staticmethod(boom))
    assert M._run_kimg_backup(str(tmp_path), time.time()) == "ok"
    assert any(n.endswith(".kimg") for n in _images(tmp_path))


def test_kimg_no_meta_on_failure(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, verify_rc=1)
    assert M._run_kimg_backup(str(tmp_path), time.time()) == "failed"
    assert _images(tmp_path) == []
