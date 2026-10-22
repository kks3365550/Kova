# -*- coding: utf-8 -*-
"""core/archive_worker.py 단위 테스트. 실제 VSS·디스크는 건드리지 않고 블록 스트림 함수를 대체한다."""
import json
import os
import sys

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core import archive_worker as aw
from core.block_stream import HEADER_SIZE, pack_header


def _read_ipc(path):
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ------------------------------------------------------------- emit_ipc

def test_emit_ipc_appends_jsonl_with_korean(tmp_path):
    ipc = str(tmp_path / "ipc.jsonl")
    aw.emit_ipc(ipc, {"type": "log", "message": "한글 메시지"})
    aw.emit_ipc(ipc, {"type": "done"})
    msgs = _read_ipc(ipc)
    assert msgs[0]["message"] == "한글 메시지" and msgs[1]["type"] == "done"


def test_emit_ipc_noop_without_file_and_swallows_io_errors(tmp_path):
    aw.emit_ipc("", {"type": "log"})  # 예외 없이 무시
    aw.emit_ipc(str(tmp_path / "no_such_dir" / "ipc.jsonl"), {"type": "log"})


# ------------------------------------------------- verify_archive_header

def _write_archive(path, **kw):
    hdr = pack_header(kw.get("cluster_size", 4096), 1000, 400, vsn="ABCD-1234", source_label="System_C")
    with open(path, "wb") as f:
        f.write(hdr + b"payload")


def test_verify_archive_header_ok(tmp_path):
    p = str(tmp_path / "a.qblk")
    _write_archive(p)
    info = aw.verify_archive_header(p)
    assert info["is_valid"] and info["magic"] == "QBLKSTRM"
    assert info["cluster_size"] == 4096 and info["used_clusters"] == 400
    assert info["vsn"] == "ABCD-1234" and info["source_label"] == "System_C"


def test_verify_archive_header_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        aw.verify_archive_header(str(tmp_path / "none.qblk"))


def test_verify_archive_header_too_small(tmp_path):
    p = tmp_path / "tiny.qblk"
    p.write_bytes(b"x" * (HEADER_SIZE - 1))
    with pytest.raises(ValueError):
        aw.verify_archive_header(str(p))


def test_verify_archive_header_detects_corruption(tmp_path):
    p = str(tmp_path / "bad.qblk")
    _write_archive(p)
    data = bytearray(open(p, "rb").read())
    data[20] ^= 0xFF  # 헤더 본문 한 바이트 변조 -> CRC32 불일치
    open(p, "wb").write(bytes(data))
    with pytest.raises(ValueError):
        aw.verify_archive_header(p)


# ------------------------------------------------------------------ main

def _run_main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["archive_worker.py"] + argv)
    aw.main()


def test_backup_success_writes_meta_and_done(tmp_path, monkeypatch):
    target = str(tmp_path / "out" / "disk.qblk")
    ipc = str(tmp_path / "ipc.jsonl")

    monkeypatch.setattr(aw, "get_volume_serial_number", lambda drv: "1234-ABCD")

    def fake_backup(source_drive, output_image_path, vsn, source_label, progress_callback):
        progress_callback({"percent": 100.0, "processed_bytes": 1, "total_bytes": 1,
                           "speed_mb_s": 1.0, "chunk_index": 1, "total_chunks": 1})
        _write_archive(output_image_path)
        return {"original_bytes": 1024 ** 3, "average_speed_mb_s": 50.0, "elapsed_seconds": 3}

    monkeypatch.setattr(aw, "create_block_stream_backup", fake_backup)
    _run_main(monkeypatch, ["--action", "backup", "--src", "c:", "--target-archive", target,
                            "--auto-verify", "--ipc-file", ipc])

    msgs = _read_ipc(ipc)
    assert msgs[-1]["type"] == "done"
    assert not [m for m in msgs if m["type"] == "error"]
    meta = json.load(open(target + ".meta.json", encoding="utf-8"))
    assert meta["backup_type"] == "block_stream" and meta["source_volume"] == "C:"
    assert meta["volume_serial_number"] == "1234-ABCD" and meta["is_verified"] is True
    assert meta["size_bytes"] == os.path.getsize(target)


def test_backup_wim_target_is_renamed_to_qblk(tmp_path, monkeypatch):
    target = str(tmp_path / "x.wim")
    ipc = str(tmp_path / "ipc.jsonl")
    seen = {}
    monkeypatch.setattr(aw, "get_volume_serial_number", lambda drv: None)

    def fake_backup(source_drive, output_image_path, vsn, source_label, progress_callback):
        seen["path"], seen["vsn"] = output_image_path, vsn
        _write_archive(output_image_path)
        return {}

    monkeypatch.setattr(aw, "create_block_stream_backup", fake_backup)
    _run_main(monkeypatch, ["--target-wim", target, "--ipc-file", ipc])
    assert seen["path"].endswith(".qblk")
    assert seen["vsn"] == "0000-0000"  # VSN 을 못 읽으면 기본값


def test_backup_failure_emits_error_and_exits_1(tmp_path, monkeypatch):
    ipc = str(tmp_path / "ipc.jsonl")
    monkeypatch.setattr(aw, "get_volume_serial_number", lambda drv: "1111-2222")

    def boom(**kwargs):
        raise RuntimeError("VSS 생성 실패")

    monkeypatch.setattr(aw, "create_block_stream_backup", boom)
    with pytest.raises(SystemExit) as ei:
        _run_main(monkeypatch, ["--target-archive", str(tmp_path / "o.qblk"), "--ipc-file", ipc])
    assert ei.value.code == 1
    err = [m for m in _read_ipc(ipc) if m["type"] == "error"]
    assert err and "VSS 생성 실패" in err[0]["error"]


def test_restore_success_emits_done(tmp_path, monkeypatch):
    ipc = str(tmp_path / "ipc.jsonl")
    archive = str(tmp_path / "a.qblk")
    _write_archive(archive)
    seen = {}

    def fake_restore(image_file_path, target_drive, auto_bcdboot, auto_restore_vsn, progress_callback):
        seen.update(drive=target_drive, bcd=auto_bcdboot, vsn=auto_restore_vsn)
        progress_callback({"percent": 50.0, "processed_bytes": 1, "total_bytes": 2,
                           "speed_mb_s": 1.0, "chunk_index": 1})
        return {"restored_bytes": 2 * 1024 ** 3, "average_speed_mb_s": 10.0, "elapsed_seconds": 5}

    monkeypatch.setattr(aw, "restore_block_stream", fake_restore)
    _run_main(monkeypatch, ["--action", "restore", "--archive", archive, "--target-drive", "e:\\",
                            "--auto-bcdboot", "--ipc-file", ipc])
    assert seen == {"drive": "E:", "bcd": True, "vsn": False}
    msgs = _read_ipc(ipc)
    assert msgs[-1]["type"] == "done" and msgs[-1]["result"]["restored_bytes"] == 2 * 1024 ** 3
    # 진행률은 5~95 구간으로 매핑된다
    pcts = [m["percent"] for m in msgs if m["type"] == "progress"]
    assert pcts and all(5.0 <= p <= 100.0 for p in pcts)


def test_restore_failure_emits_error_and_exits_1(tmp_path, monkeypatch):
    ipc = str(tmp_path / "ipc.jsonl")

    def boom(**kwargs):
        raise OSError("볼륨 잠금 실패")

    monkeypatch.setattr(aw, "restore_block_stream", boom)
    with pytest.raises(SystemExit) as ei:
        _run_main(monkeypatch, ["--action", "restore", "--archive", str(tmp_path / "a.qblk"),
                                "--target-drive", "E:", "--ipc-file", ipc])
    assert ei.value.code == 1
    assert [m for m in _read_ipc(ipc) if m["type"] == "error"]
