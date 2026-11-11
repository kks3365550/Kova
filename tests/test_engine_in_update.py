# -*- coding: utf-8 -*-
"""자동업데이트가 Rust 엔진(engine_rust/kova_fast_engine.exe)을 앱과 함께 교체한다."""
import io
import os
import socket
import sys
import time
import zipfile

from core.legacy_update_apply import apply_update
from core.updater_v2.artifact_safety import ArtifactSafetyChecker
from core.updater_v2.installer import AtomicInstaller

ENGINE = "engine_rust/kova_fast_engine.exe"
FAKE_SERVER = '''import os, socket
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


def _zip_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_safety_allows_only_the_engine_exe():
    ok, errs = ArtifactSafetyChecker().check_archive(_zip_bytes({"core/a.py": "x=1\n", ENGINE: b"MZ" + os.urandom(5000)}))
    assert ok, errs
    for bad in ("engine_rust/other.exe", "engine_rust/kova_fast_engine.dll", "core/kova_fast_engine.exe", "evil.exe"):
        ok, errs = ArtifactSafetyChecker().check_archive(_zip_bytes({"core/a.py": "x=1\n", bad: b"MZ" + os.urandom(5000)}))
        assert not ok, bad


def test_installer_backs_up_and_rolls_back_engine(tmp_path):
    target = tmp_path / "app"
    (target / "core").mkdir(parents=True)
    (target / "engine_rust").mkdir()
    (target / "engine_rust" / "kova_fast_engine.exe").write_bytes(b"OLD-ENGINE")
    inst = AtomicInstaller(str(target))
    backup = tmp_path / "bk"
    inst.backup_current_installation(str(backup))
    assert (backup / "engine_rust" / "kova_fast_engine.exe").read_bytes() == b"OLD-ENGINE"
    (target / "engine_rust" / "kova_fast_engine.exe").write_bytes(b"NEW-ENGINE")
    assert inst.rollback(str(backup), skip_process_control=True)
    assert (target / "engine_rust" / "kova_fast_engine.exe").read_bytes() == b"OLD-ENGINE"


def test_legacy_apply_replaces_engine_and_rolls_back(tmp_path):
    port = _free_port()
    base = tmp_path / "app"
    (base / "core").mkdir(parents=True)
    (base / "engine_rust").mkdir()
    (base / "run.py").write_text(FAKE_SERVER, encoding="utf-8")
    (base / "port.txt").write_text(str(port))
    (base / "engine_rust" / "kova_fast_engine.exe").write_bytes(b"OLD-ENGINE")
    z = tmp_path / "upd.zip"
    z.write_bytes(_zip_bytes({"core/mod.py": "V=1\n", ENGINE: b"NEW-ENGINE"}))
    try:
        assert apply_update(str(z), str(base), port, sys.executable, start_timeout=10, stop_timeout=2) == "ok"
        assert (base / "engine_rust" / "kova_fast_engine.exe").read_bytes() == b"NEW-ENGINE"
    finally:
        (base / "stop.flag").write_text("x")
        time.sleep(1.0)

    # 새 버전이 기동하지 않으면 엔진도 이전 파일로 되돌아간다
    port2 = _free_port()
    base2 = tmp_path / "app2"
    (base2 / "core").mkdir(parents=True)
    (base2 / "engine_rust").mkdir()
    (base2 / "run.py").write_text(FAKE_SERVER, encoding="utf-8")
    (base2 / "port.txt").write_text(str(port2))
    (base2 / "engine_rust" / "kova_fast_engine.exe").write_bytes(b"OLD-ENGINE")
    z2 = tmp_path / "upd2.zip"
    z2.write_bytes(_zip_bytes({"run.py": "raise SystemExit(1)\n", ENGINE: b"NEW-ENGINE"}))
    try:
        assert apply_update(str(z2), str(base2), port2, sys.executable, start_timeout=6, stop_timeout=2) == "rolled_back"
        assert (base2 / "engine_rust" / "kova_fast_engine.exe").read_bytes() == b"OLD-ENGINE"
    finally:
        (base2 / "stop.flag").write_text("x")
        time.sleep(1.0)
