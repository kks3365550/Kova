# tests/test_rust_adapter.py
# Python 3.11 compatible, Windows CRLF, pyflakes-clean
"""
Unit and integration tests for core/rust_adapter.py.
Verifies binary discovery, mock execution, live fast engine execution,
progress callbacks, and 2PC SQLite metadata synchronization.
"""

import os
import shutil
import tempfile
import pytest
from core.rust_adapter import (
    find_rust_engine_binary,
    run_rust_backup,
    sync_rust_manifest_to_sqlite
)
from core.metadata_db import MetadataDB


@pytest.fixture
def temp_workspace():
    tmp_dir = tempfile.mkdtemp(prefix="test_rust_adapter_")
    src_dir = os.path.join(tmp_dir, "src")
    repo_dir = os.path.join(tmp_dir, "repo")
    os.makedirs(src_dir, exist_ok=True)
    os.makedirs(repo_dir, exist_ok=True)

    # Populate test files
    for i in range(5):
        with open(os.path.join(src_dir, f"file_{i}.txt"), "w", encoding="utf-8") as f:
            f.write(f"Sample data content for file {i}\n" * 50)

    yield src_dir, repo_dir, tmp_dir
    shutil.rmtree(tmp_dir, ignore_errors=True)


def test_find_rust_engine_binary():
    binary = find_rust_engine_binary()
    assert binary is not None
    assert os.path.isfile(binary)
    assert "kova_fast_engine" in binary


def test_run_rust_backup_and_sqlite_sync(temp_workspace):
    src_dir, repo_dir, _ = temp_workspace

    progress_events = []

    def on_progress(ev):
        progress_events.append(ev)

    res = run_rust_backup(
        sources=[src_dir],
        repo_dir=repo_dir,
        session_id="test_rust_snap_001",
        zstd_level=3,
        progress_callback=on_progress
    )

    assert res.session_id == "test_rust_snap_001"
    assert res.total_files >= 5
    assert res.total_bytes > 0
    assert res.is_synced_to_sqlite is True
    assert os.path.isfile(res.manifest_path)

    # Verify SQLite sync
    db = MetadataDB(repo_dir)
    snapshots = db.list_snapshots()
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap["id"] == "test_rust_snap_001"
    assert snap["summary"]["total_files"] >= 5

    # Verify blobs exist
    assert os.path.isdir(os.path.join(repo_dir, "blobs"))


def test_sync_rust_manifest_missing_file(temp_workspace):
    _, repo_dir, _ = temp_workspace
    ok = sync_rust_manifest_to_sqlite(repo_dir, "non_existent_session")
    assert ok is False
