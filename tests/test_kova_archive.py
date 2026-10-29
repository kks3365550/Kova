# -*- coding: utf-8 -*-
"""KOVA Engine 이미지(.kova)가 아카이브 목록에 보이고, GUI 복원은 막히는지 확인."""
import os

from core.drive_archive import DriveArchiveManager


def test_list_archives_finds_kova_images(tmp_path, monkeypatch):
    kdir = tmp_path / "KovaImages"
    kdir.mkdir()
    (kdir / "C_20261030_010101.kova").write_bytes(b"x" * 10)
    (kdir / "C_20261030_010101.kova.json").write_text("{}", encoding="utf-8")
    (kdir / "old.kimg").write_bytes(b"x")
    real_isdir, real_scandir = os.path.isdir, os.scandir

    def fake_isdir(p):
        return True if str(p).endswith("KovaImages") else real_isdir(p)

    def fake_scandir(p):
        return real_scandir(kdir if str(p).endswith("KovaImages") else p)

    monkeypatch.setattr("core.drive_archive.os.path.isdir", fake_isdir)
    monkeypatch.setattr("core.drive_archive.os.scandir", fake_scandir)
    monkeypatch.setattr("core.drive_archive.json.dump", lambda *a, **k: None)  # 실제 캐시 오염 방지
    got = [a for a in DriveArchiveManager.get_instance().list_archives() if a["backup_type"] == "kova_image"]
    names = {a["name"] for a in got}
    assert {"C_20261030_010101.kova", "old.kimg"} <= names
    assert not any(n.endswith(".json") for n in names)


def test_start_restore_rejects_kova(tmp_path):
    img = tmp_path / "a.kova"
    img.write_bytes(b"x")
    res = DriveArchiveManager.get_instance().start_restore(str(img), "D:")
    assert res["success"] is False and "WinPE" in res["error"]
