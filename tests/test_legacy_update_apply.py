# -*- coding: utf-8 -*-
"""레거시 업데이트 적용기: 정상 적용 / 새 버전이 기동 실패하면 롤백 / 포트가 안 닫히면 중단."""
import os
import socket
import sys
import time
import zipfile

from core.legacy_update_apply import apply_update

FAKE_SERVER = '''import os, socket, sys, time
base = os.path.dirname(os.path.abspath(__file__))
port = int(open(os.path.join(base, "port.txt")).read())
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port)); s.listen(5); s.settimeout(0.3)
while not os.path.exists(os.path.join(base, "stop.flag")):
    try:
        c, _ = s.accept(); c.close()
    except socket.timeout:
        pass
'''
BROKEN = "raise SystemExit(1)\n"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_base(tmp_path, port):
    base = tmp_path / "app"
    (base / "core").mkdir(parents=True)
    (base / "run.py").write_text(FAKE_SERVER, encoding="utf-8")
    (base / "core" / "mod.py").write_text("VERSION = 'old'\n", encoding="utf-8")
    (base / "port.txt").write_text(str(port))
    return base


def _make_zip(tmp_path, files):
    z = tmp_path / "upd.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return z


def _stop(base):
    (base / "stop.flag").write_text("x")
    time.sleep(1.0)


def test_good_update_is_applied(tmp_path):
    port = _free_port()
    base = _make_base(tmp_path, port)
    z = _make_zip(tmp_path, {"core/mod.py": "VERSION = 'new'\n", "core/extra.py": "x=1\n"})
    try:
        res = apply_update(str(z), str(base), port, sys.executable, start_timeout=10, stop_timeout=2)
        assert res == "ok"
        assert "new" in (base / "core" / "mod.py").read_text()
        assert not z.exists()
    finally:
        _stop(base)


def test_broken_update_is_rolled_back(tmp_path):
    port = _free_port()
    base = _make_base(tmp_path, port)
    z = _make_zip(tmp_path, {"run.py": BROKEN, "core/mod.py": "VERSION = 'new'\n", "core/extra.py": "x=1\n"})
    try:
        res = apply_update(str(z), str(base), port, sys.executable, start_timeout=6, stop_timeout=2)
        assert res == "rolled_back"
        assert "old" in (base / "core" / "mod.py").read_text()
        assert not (base / "core" / "extra.py").exists()      # 새로 생긴 파일은 제거
        assert "SystemExit" not in (base / "run.py").read_text()  # 원래 run.py 복원
        with socket.create_connection(("127.0.0.1", port), timeout=2):  # 이전 버전 서버가 다시 응답
            pass
    finally:
        _stop(base)


def test_aborts_without_touching_files_when_port_stays_open(tmp_path):
    port = _free_port()
    base = _make_base(tmp_path, port)
    z = _make_zip(tmp_path, {"core/mod.py": "VERSION = 'new'\n"})
    srv = socket.socket()
    srv.bind(("127.0.0.1", port))
    srv.listen(5)
    srv.settimeout(0.3)
    import threading
    stop = threading.Event()

    def _accept_loop():
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                c.close()
            except (socket.timeout, OSError):
                pass
    th = threading.Thread(target=_accept_loop, daemon=True)
    th.start()
    try:
        res = apply_update(str(z), str(base), port, sys.executable, start_timeout=3, stop_timeout=1)
        assert res == "aborted"
        assert "old" in (base / "core" / "mod.py").read_text()
    finally:
        stop.set()
        th.join(2)
        srv.close()
