# -*- coding: utf-8 -*-
"""복원 엔진 선택: Python 형식 스냅샷은 Rust 복원 대상이 아니다."""
import json
import os

from web.api_backup import _is_rust_manifest


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_python_manifest_is_not_rust(tmp_path):
    p = _write(tmp_path, "py.json", json.dumps({
        "id": "snap_x", "session_id": "snap_x", "created_at": "2026-11-10T13:30:10+00:00",
        "created_at_ts": 1794317410.69, "iso_time": "2026-11-10T13:30:10+00:00",
        "source_root": "C:\\a;C:\\b", "entries": [],
    }))
    assert _is_rust_manifest(p) is False


def test_python_manifest_with_signature_only_is_not_rust(tmp_path):
    p = _write(tmp_path, "py2.json", '{"id": "s", "manifest_signature": "abc", "entries": []}')
    assert _is_rust_manifest(p) is False


def test_rust_manifest_with_entries_null_is_rust(tmp_path):
    # Rust 엔진은 `"entries":null` 도 함께 쓴다 - 이것만으로 Python 으로 오판하면 안 된다
    p = _write(tmp_path, "rs.json", json.dumps({
        "id": "snap_t1", "session_id": "snap_t1", "created_at": "2026-11-11T12:33:26+09:00",
        "source_root": "rt/src", "sources": None, "total_files": 1, "total_bytes": 18,
        "files": [{"path": "f1.txt", "size": 18, "chunks": [{"hash": "ab" * 32}]}],
        "entries": None,
    }))
    assert _is_rust_manifest(p) is True


def test_unreadable_manifest_falls_back_to_legacy(tmp_path):
    assert _is_rust_manifest(str(tmp_path / "missing.json")) is False


def test_real_python_snapshot_is_detected(tmp_path):
    from core.snapshot import SnapshotEngine
    src = tmp_path / "src"
    src.mkdir()
    for i in range(5):
        (src / f"f{i}.txt").write_text(f"file {i}", encoding="utf-8")
    repo = tmp_path / "repo"
    m = SnapshotEngine.create_snapshot(repo_dir=str(repo), sources=[str(src)])
    sid = m.get("id") or m.get("session_id")
    assert _is_rust_manifest(os.path.join(str(repo), "snapshots", f"{sid}.json")) is False
