"""WinPE restore scripts: encoding rules and safety-order checks."""
import os
import re

import pytest

WINPE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "winpe")
FILES = ["restore.cmd", "layout_gpt_uefi.txt", "layout_mbr_bios.txt", "extend.txt"]


def _read(name):
    with open(os.path.join(WINPE, name), "rb") as f:
        return f.read()


@pytest.mark.parametrize("name", FILES)
def test_ascii_crlf_no_bom(name):
    data = _read(name)
    assert not data.startswith(b"\xef\xbb\xbf")
    data.decode("ascii")
    assert data.count(b"\n") == data.count(b"\r\n") > 0


@pytest.mark.parametrize("name", ["layout_gpt_uefi.txt", "layout_mbr_bios.txt", "extend.txt"])
def test_templates_do_not_hardcode_disk(name):
    lines = _read(name).lower().splitlines()
    assert not any(l.strip().startswith(b"select disk") for l in lines)


def test_layouts_assign_expected_letters():
    gpt = _read("layout_gpt_uefi.txt").decode()
    mbr = _read("layout_mbr_bios.txt").decode()
    for text in (gpt, mbr):
        assert "assign letter=S" in text and "assign letter=W" in text
    assert "convert gpt" in gpt and "convert mbr" in mbr


def test_restore_requires_erase_confirmation():
    text = _read("restore.cmd").decode()
    assert '"ERASE"' in text
    assert text.index("ERASE") < text.index("diskpart /s \"%TEMP%\\dp.txt\"")


def test_verify_runs_before_any_disk_change():
    text = _read("restore.cmd").decode()
    verify = text.index('" verify --image')
    wipe = text.index('diskpart /s "%TEMP%\\dp.txt"')
    restore = text.index('" restore --image')
    bcd = text.index("bcdboot W:")
    assert verify < wipe < restore < bcd
    # verify failure must stop the script
    assert re.search(r"verify --image[^\n]*\r?\n\s*if errorlevel 1", text)


def test_no_commands_missing_from_winpe():
    text = _read("restore.cmd").decode("ascii").lower()
    for cmd in ("findstr", "wmic", "powershell"):
        assert cmd not in text


def test_restore_target_is_device_path():
    text = _read("restore.cmd").decode("ascii")
    bs = chr(92)
    assert "--target " + bs * 2 + "." + bs + "W:" in text
