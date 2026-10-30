# -*- coding: utf-8 -*-
"""core/windows_task.py 와 core/registry_backup.py 단위 테스트.

schtasks / reg 같은 외부 명령은 subprocess.run 을 대체해 실제 시스템을 바꾸지 않는다.
"""
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core import windows_task as wt
from core import registry_backup as rb

pytestmark = pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows 전용")


# ------------------------------------------------------------ windows_task

def _capture_schtasks(monkeypatch, result=(0, "", "")):
    calls = []

    def fake(cmd, timeout=15):
        calls.append(cmd)
        return result

    monkeypatch.setattr(wt, "_run_schtasks_cmd", fake)
    return calls


def test_register_daily_uses_given_time(monkeypatch):
    calls = _capture_schtasks(monkeypatch)
    r = wt.register_windows_scheduled_task(schedule_type="daily", schedule_value="21:30")
    cmd = calls[0]
    assert r["success"] and cmd[:2] == ["schtasks", "/Create"]
    assert cmd[cmd.index("/SC") + 1] == "DAILY" and cmd[cmd.index("/ST") + 1] == "21:30"
    assert "/F" in cmd and cmd[cmd.index("/TN") + 1] == wt.TASK_NAME


def test_register_daily_hour_only_is_zero_padded(monkeypatch):
    calls = _capture_schtasks(monkeypatch)
    wt.register_windows_scheduled_task(schedule_type="daily", schedule_value="9")
    assert calls[0][calls[0].index("/ST") + 1] == "09:00"


def test_register_interval_hours_under_24_is_hourly(monkeypatch):
    calls = _capture_schtasks(monkeypatch)
    wt.register_windows_scheduled_task(schedule_type="interval_hours", schedule_value="6")
    cmd = calls[0]
    assert cmd[cmd.index("/SC") + 1] == "HOURLY" and cmd[cmd.index("/MO") + 1] == "6"


def test_register_interval_24h_or_more_becomes_daily(monkeypatch):
    calls = _capture_schtasks(monkeypatch)
    wt.register_windows_scheduled_task(schedule_type="interval_hours", schedule_value="48")
    cmd = calls[0]
    assert cmd[cmd.index("/SC") + 1] == "DAILY" and cmd[cmd.index("/ST") + 1] == "09:00"


@pytest.mark.parametrize("stype,sval", [("interval_hours", "abc"), ("weekly", "mon"), ("", "")])
def test_register_bad_input_falls_back_to_daily_0900(monkeypatch, stype, sval):
    calls = _capture_schtasks(monkeypatch)
    wt.register_windows_scheduled_task(schedule_type=stype, schedule_value=sval)
    cmd = calls[0]
    assert cmd[cmd.index("/SC") + 1] == "DAILY" and cmd[cmd.index("/ST") + 1] == "09:00"


def test_register_includes_profile_and_cli_path(monkeypatch):
    calls = _capture_schtasks(monkeypatch)
    r = wt.register_windows_scheduled_task(profile_id="p1")
    run_cmd = calls[0][calls[0].index("/TR") + 1]
    assert 'cli_backup.py' in run_cmd and '--profile "p1"' in run_cmd
    assert r["command"] == run_cmd


def test_register_failure_returns_error(monkeypatch):
    _capture_schtasks(monkeypatch, result=(1, "", "액세스가 거부되었습니다"))
    r = wt.register_windows_scheduled_task()
    assert r["success"] is False and "거부" in r["error"]


def test_unregister_success_and_failure(monkeypatch):
    calls = _capture_schtasks(monkeypatch, result=(0, "", ""))
    assert wt.unregister_windows_scheduled_task()["success"] is True
    assert calls[0][:2] == ["schtasks", "/Delete"]
    _capture_schtasks(monkeypatch, result=(1, "", "없음"))
    assert wt.unregister_windows_scheduled_task()["success"] is False


def test_status_parses_korean_and_english_labels(monkeypatch):
    kr = "작업 이름: \\X\n다음 실행 시간: 2026-10-24 09:00:00\n상태: 준비\n마지막 결과: 0"
    _capture_schtasks(monkeypatch, result=(0, kr, ""))
    st = wt.get_windows_scheduled_task_status()
    assert st["registered"] and st["status"] == "준비"
    assert st["next_run"].startswith("2026-10-24") and st["last_result"] == "0"

    en = "Status: Ready\nNext Run Time: soon\nLast Run Time: before\nLast Result: 0"
    _capture_schtasks(monkeypatch, result=(0, en, ""))
    st = wt.get_windows_scheduled_task_status()
    assert st["status"] == "Ready" and st["last_run"] == "before"


def test_status_unregistered(monkeypatch):
    _capture_schtasks(monkeypatch, result=(1, "", "not found"))
    assert wt.get_windows_scheduled_task_status() == {"registered": False, "status": "미등록"}


def _fake_run(stdout=b"", stderr=b"", returncode=0):
    return lambda cmd, **kw: SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def test_run_schtasks_decodes_utf8_and_cp949(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout="성공".encode("utf-8")))
    assert wt._run_schtasks_cmd(["schtasks"]) == (0, "성공", "")
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout="성공".encode("cp949"), returncode=2))
    assert wt._run_schtasks_cmd(["schtasks"]) == (2, "성공", "")


def test_run_schtasks_exception_returns_minus_one(monkeypatch):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(subprocess, "run", boom)
    code, out, err = wt._run_schtasks_cmd(["schtasks"])
    assert code == -1 and out == "" and err


def test_get_python_executable_exists():
    exe = wt.get_python_executable()
    assert os.path.exists(exe)
    # pythonw.exe 가 존재하는 환경이라면 반드시 pythonw.exe 가 우선되어야 함 (무창 백그라운드)
    if os.path.exists(os.path.join(os.path.dirname(sys.executable), "pythonw.exe")):
        assert exe.lower().endswith("pythonw.exe")


# ---------------------------------------------------------- registry_backup

def _collector(tmp_path, shortcuts=(), appdata=(), uninstall=()):
    c = object.__new__(rb.AppPackageCollector)  # 실제 시스템 색인을 건너뛴다
    c.dest_dir = str(tmp_path)
    c.seen_reg_keys = set()
    c.shortcuts_index = list(shortcuts)
    c.appdata_index = list(appdata)
    c.uninstall_index = list(uninstall)
    return c


def test_find_shortcuts_matches_by_name_or_publisher(tmp_path):
    c = _collector(tmp_path, shortcuts=[
        ("kova.lnk", "c:\\sm\\kova", "C:\\sm\\kova\\Kova.lnk"),
        ("other.lnk", "c:\\sm\\acme", "C:\\sm\\acme\\Other.lnk"),
        ("zzz.lnk", "c:\\sm\\misc", "C:\\sm\\misc\\zzz.lnk"),
    ])
    assert rb.find_app_start_menu_shortcuts("Kova", collector=c) == ["C:\\sm\\kova\\Kova.lnk"]
    found = rb.find_app_start_menu_shortcuts("Nope", publisher="Acme", collector=c)
    assert found == ["C:\\sm\\acme\\Other.lnk"]


def test_find_with_empty_tokens_returns_empty(tmp_path):
    c = _collector(tmp_path)
    assert rb.find_app_start_menu_shortcuts("", collector=c) == []
    assert rb.find_app_data_directories("", collector=c) == []


def test_find_appdata_deduplicates(tmp_path):
    c = _collector(tmp_path, appdata=[("kova", "C:\\u\\AppData\\Roaming\\Kova")])
    # 이름과 첫 단어가 같아도 결과는 한 번만
    assert rb.find_app_data_directories("Kova", collector=c) == ["C:\\u\\AppData\\Roaming\\Kova"]


def test_export_uninstall_key_runs_reg_export_and_dedups(tmp_path, monkeypatch):
    c = _collector(tmp_path, uninstall=[("kova backup", "HKLM\\Software\\X\\Kova", "HKLM")])
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        out = cmd[3]
        if "Uninstall" in out:
            with open(out, "w") as f:
                f.write("Windows Registry Editor")
        return SimpleNamespace(returncode=0 if "Uninstall" in out else 1, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    files = rb.export_universal_app_registry("Kova", collector=c)
    assert len(files) == 1 and files[0].endswith("Kova_Uninstall_HKLM_0.reg")
    assert cmds[0][:3] == ["reg", "export", "HKLM\\Software\\X\\Kova"]
    # 같은 키는 두 번째 호출에서 다시 내보내지 않는다
    before = len(cmds)
    assert rb.export_universal_app_registry("Kova", collector=c) == []
    assert all("Uninstall" not in cm[3] for cm in cmds[before:])


def test_export_ignores_failed_and_empty_exports(tmp_path, monkeypatch):
    c = _collector(tmp_path, uninstall=[("kova", "HKCU\\Software\\Kova", "HKCU")])
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=b"", stderr=b""))
    # 명령은 성공 코드를 돌려주지만 파일이 만들어지지 않으면 결과에 넣지 않는다
    assert rb.export_universal_app_registry("Kova", collector=c) == []


def test_export_survives_subprocess_exception(tmp_path, monkeypatch):
    c = _collector(tmp_path, uninstall=[("kova", "HKCU\\Software\\Kova", "HKCU")])

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(subprocess, "run", boom)
    assert rb.export_universal_app_registry("Kova", collector=c) == []


def test_import_registry_file(tmp_path, monkeypatch):
    f = tmp_path / "a.reg"
    f.write_text("x")
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert rb.import_registry_file(str(f)) is True
    assert seen[0][:2] == ["reg", "import"]
    # 파일이 없으면 reg 를 실행하지 않는다
    seen.clear()
    assert rb.import_registry_file(str(tmp_path / "none.reg")) is False and not seen


def test_import_registry_failure_and_exception(tmp_path, monkeypatch):
    f = tmp_path / "a.reg"
    f.write_text("x")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=1))
    assert rb.import_registry_file(str(f)) is False

    def boom(cmd, **kw):
        raise OSError("reg 없음")

    monkeypatch.setattr(subprocess, "run", boom)
    assert rb.import_registry_file(str(f)) is False


def test_collect_full_app_package_gathers_sources(tmp_path, monkeypatch):
    install = tmp_path / "install"
    install.mkdir()
    appdata = tmp_path / "appdata_kova"
    appdata.mkdir()
    lnk = tmp_path / "Kova.lnk"
    lnk.write_text("x")
    c = _collector(tmp_path,
                   shortcuts=[("kova.lnk", "root", str(lnk))],
                   appdata=[("kova", str(appdata))])
    monkeypatch.setattr(rb, "export_universal_app_registry", lambda *a, **k: [])
    sources, reg_files = rb.collect_full_app_package(
        "Kova", install_location=str(install), collector=c)
    assert sources == [os.path.abspath(str(install)), str(appdata), str(lnk)]
    assert reg_files == []
