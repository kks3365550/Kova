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


def test_adapter_converted_rust_manifest_is_rust(tmp_path):
    # 어댑터가 변환한 Rust 매니페스트는 created_at_ts/iso_time/manifest_signature 키를 갖지만 Rust 형식이다
    p = _write(tmp_path, "adapter.json", json.dumps({
        "id": "snap_a", "session_id": "snap_a", "created_at": "2026-11-10T13:30:10+00:00",
        "created_at_ts": 1794317410.69, "iso_time": "2026-11-10T13:30:10+00:00",
        "source_root": "C:\a;C:\b", "total_files": 1, "total_bytes": 18,
        "files": [{"path": "f1.txt", "size": 18, "chunks": [{"hash": "ab" * 32}]}],
        "manifest_signature": None, "ed25519_signature": None, "engine": "rust_fast_engine_v1",
    }))
    assert _is_rust_manifest(p) is True


def test_strict_mode_only_true_for_files_array(tmp_path):
    rs = _write(tmp_path, "rs2.json", '{"id": "s", "files": [{"path": "a"}], "entries": null}')
    py = _write(tmp_path, "py3.json", '{"id": "s", "entries": []}')
    unknown = _write(tmp_path, "unk.json", '{"id": "s"}')
    assert _is_rust_manifest(rs, strict=True) is True
    assert _is_rust_manifest(py, strict=True) is False
    assert _is_rust_manifest(unknown, strict=True) is False


def test_in_place_restore_of_rust_snapshot_is_blocked(tmp_path, monkeypatch):
    import web.api_backup as ab
    repo = tmp_path / "repo"
    (repo / "snapshots").mkdir(parents=True)
    (repo / "snapshots" / "snap_r.json").write_text(
        '{"id": "snap_r", "created_at_ts": 1.0, "files": [{"path": "a.txt", "size": 1}]}', encoding="utf-8")
    logs = []
    monkeypatch.setattr(ab, "append_task_log", lambda msg, level="INFO": logs.append((level, msg)))
    ab.current_task.update({"running": True, "cancel_event": __import__("threading").Event()})
    try:
        ab._background_restore_task_impl({"snapshot_id": "snap_r", "repo_dir": str(repo), "in_place": True})
    finally:
        ab.current_task["running"] = False
    text = " ".join(m for _, m in logs)
    assert "복원할 수 없습니다" in text and "복원 완료" not in text
