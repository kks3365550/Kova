# -*- coding: utf-8 -*-
"""Rust 엔진 format_version 2: 여러 원본(상대경로 충돌 포함)의 원위치 복원과 별도 폴더 복원."""
import json
import os
import threading

import pytest

from core.rust_adapter import find_rust_engine_binary, run_rust_backup

pytestmark = pytest.mark.skipif(find_rust_engine_binary() is None, reason="Rust 엔진 exe 없음")


def _engine_supports_v2():
    # 새 엔진만 --in-place 를 안다
    import subprocess
    exe = find_rust_engine_binary()
    if not exe:
        return False
    out = subprocess.run([exe, "restore", "--help"], capture_output=True).stdout or b""
    return b"--in-place" in out


@pytest.fixture
def workspace(tmp_path):
    a, b, f = tmp_path / "A", tmp_path / "B", tmp_path / "F"
    (a / "sub").mkdir(parents=True)
    b.mkdir()
    f.mkdir()
    (a / "x.txt").write_text("A-x", encoding="utf-8")
    (a / "sub" / "y.txt").write_text("A-y", encoding="utf-8")
    (b / "x.txt").write_text("B-x-different", encoding="utf-8")   # A 와 같은 상대경로
    (f / "f.txt").write_text("single", encoding="utf-8")
    repo = tmp_path / "repo"
    return tmp_path, [str(a), str(b), str(f / "f.txt")], repo


def _run_restore(ab, repo, sid, **kw):
    logs = []
    orig = ab.append_task_log
    ab.append_task_log = lambda msg, level="INFO": logs.append((level, msg))
    ab.current_task.update({"running": True, "cancel_event": threading.Event()})
    try:
        ab._background_restore_task_impl({"snapshot_id": sid, "repo_dir": str(repo), **kw})
    finally:
        ab.current_task["running"] = False
        ab.append_task_log = orig
    return logs


@pytest.mark.skipif(not _engine_supports_v2(), reason="구형 엔진 exe")
def test_manifest_records_sources_and_src(workspace):
    tmp, sources, repo = workspace
    run_rust_backup(sources, str(repo), session_id="snap_t1")
    m = json.loads((repo / "snapshots" / "snap_t1.json").read_text(encoding="utf-8"))
    assert m["format_version"] == 2
    assert [os.path.normpath(s) for s in m["sources"]] == [os.path.normpath(s) for s in sources]
    assert m["source_kinds"] == ["dir", "dir", "file"]
    assert all("src" in e for e in m["files"])


@pytest.mark.skipif(not _engine_supports_v2(), reason="구형 엔진 exe")
def test_in_place_restore_puts_colliding_files_back(workspace):
    import web.api_backup as ab
    tmp, sources, repo = workspace
    run_rust_backup(sources, str(repo), session_id="snap_t2")
    a, b, f = tmp / "A", tmp / "B", tmp / "F"
    for p in (a / "x.txt", a / "sub" / "y.txt", b / "x.txt", f / "f.txt"):
        p.unlink()
    logs = _run_restore(ab, repo, "snap_t2", in_place=True)
    assert not any(lv == "ERROR" for lv, _ in logs), logs
    assert (a / "x.txt").read_text(encoding="utf-8") == "A-x"
    assert (a / "sub" / "y.txt").read_text(encoding="utf-8") == "A-y"
    assert (b / "x.txt").read_text(encoding="utf-8") == "B-x-different"
    assert (f / "f.txt").read_text(encoding="utf-8") == "single"


@pytest.mark.skipif(not _engine_supports_v2(), reason="구형 엔진 exe")
def test_target_restore_keeps_sources_apart(workspace):
    import web.api_backup as ab
    tmp, sources, repo = workspace
    run_rust_backup(sources, str(repo), session_id="snap_t3")
    out = tmp / "out"
    logs = _run_restore(ab, repo, "snap_t3", target_dir=str(out))
    assert not any(lv == "ERROR" for lv, _ in logs), logs
    assert (out / "001_A" / "x.txt").read_text(encoding="utf-8") == "A-x"
    assert (out / "002_B" / "x.txt").read_text(encoding="utf-8") == "B-x-different"
    assert (out / "003_f.txt" / "f.txt").read_text(encoding="utf-8") == "single"
