# -*- coding: utf-8 -*-
"""core/hasher.py 와 core/snapshot_cache.py 단위 테스트."""
import hashlib
import json
import os
import sys

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core import hasher
from core.snapshot_cache import SnapshotMetadataCache, get_snapshot_cache


# ---------------------------------------------------------------- hasher

def test_sha256_matches_hashlib_across_block_boundary(tmp_path):
    data = os.urandom(3 * 1024 + 7)
    p = tmp_path / "f.bin"
    p.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()
    # 블록 크기보다 작게/같게/크게 모두 같은 해시여야 한다
    for bs in (1, 1024, 4096, 1048576):
        assert hasher.calculate_sha256(str(p), block_size=bs) == expected


def test_sha256_empty_file(tmp_path):
    p = tmp_path / "empty"
    p.write_bytes(b"")
    assert hasher.calculate_sha256(str(p)) == hashlib.sha256(b"").hexdigest()


def test_sha256_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        hasher.calculate_sha256(str(tmp_path / "nope"))


def test_bytes_sha256():
    assert hasher.calculate_bytes_sha256(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_get_file_stat_regular_file_and_dir(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"12345")
    st = hasher.get_file_stat(str(f))
    assert st["size"] == 5 and st["is_file"] and not st["is_dir"] and not st["is_symlink"]
    d = hasher.get_file_stat(str(tmp_path))
    assert d["is_dir"] and not d["is_file"]


def test_get_file_stat_missing_returns_none(tmp_path):
    assert hasher.get_file_stat(str(tmp_path / "missing")) is None


def test_get_file_stat_symlink_follows_target(tmp_path):
    target = tmp_path / "t.txt"
    target.write_bytes(b"xyz")
    link = tmp_path / "l.txt"
    try:
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("심볼릭 링크 생성 권한 없음")
    st = hasher.get_file_stat(str(link))
    assert st["is_symlink"] and st["is_file"] and st["size"] == 3


# ---------------------------------------------------------- snapshot cache

def _repo_with_snapshots(tmp_path, n):
    snaps = tmp_path / "repo" / "snapshots"
    snaps.mkdir(parents=True)
    for i in range(n):
        (snaps / ("snap_%d.json" % i)).write_text(json.dumps({"id": i}), encoding="utf-8")
    return str(tmp_path / "repo")


def _scanner(calls):
    def scan(repo_dir):
        calls.append(repo_dir)
        snaps = os.path.join(repo_dir, "snapshots")
        return [{"id": f} for f in sorted(os.listdir(snaps)) if f.endswith(".json")]
    return scan


def test_cache_hit_avoids_rescan(tmp_path):
    repo = _repo_with_snapshots(tmp_path, 2)
    cache, calls = SnapshotMetadataCache(), []
    first = cache.list_snapshots(repo, _scanner(calls))
    second = cache.list_snapshots(repo, _scanner(calls))
    assert first == second and len(calls) == 1
    assert cache.stats["hits"] == 1 and cache.stats["misses"] == 1


def test_cache_returns_copies_so_callers_cannot_corrupt_it(tmp_path):
    repo = _repo_with_snapshots(tmp_path, 1)
    cache, calls = SnapshotMetadataCache(), []
    a = cache.list_snapshots(repo, _scanner(calls))
    a[0]["id"] = "tampered"
    b = cache.list_snapshots(repo, _scanner(calls))
    assert b[0]["id"] != "tampered"


def test_new_snapshot_file_invalidates_cache(tmp_path):
    repo = _repo_with_snapshots(tmp_path, 1)
    cache, calls = SnapshotMetadataCache(), []
    assert len(cache.list_snapshots(repo, _scanner(calls))) == 1
    (tmp_path / "repo" / "snapshots" / "snap_new.json").write_text("{}", encoding="utf-8")
    assert len(cache.list_snapshots(repo, _scanner(calls))) == 2
    assert len(calls) == 2
    assert cache.stats["fingerprint_invalidations"] == 1


def test_deleted_snapshot_file_invalidates_cache(tmp_path):
    repo = _repo_with_snapshots(tmp_path, 2)
    cache, calls = SnapshotMetadataCache(), []
    cache.list_snapshots(repo, _scanner(calls))
    os.remove(os.path.join(repo, "snapshots", "snap_0.json"))
    assert len(cache.list_snapshots(repo, _scanner(calls))) == 1


def test_force_refresh_rescans(tmp_path):
    repo = _repo_with_snapshots(tmp_path, 1)
    cache, calls = SnapshotMetadataCache(), []
    cache.list_snapshots(repo, _scanner(calls))
    cache.list_snapshots(repo, _scanner(calls), force_refresh=True)
    assert len(calls) == 2


def test_evict_and_clear(tmp_path):
    repo = _repo_with_snapshots(tmp_path, 1)
    cache, calls = SnapshotMetadataCache(), []
    cache.list_snapshots(repo, _scanner(calls))
    cache.evict(repo)
    cache.list_snapshots(repo, _scanner(calls))
    assert len(calls) == 2 and cache.stats["evictions"] == 1
    cache.clear()
    cache.list_snapshots(repo, _scanner(calls))
    assert len(calls) == 3


def test_scanner_failure_raises_and_leaves_no_stale_entry(tmp_path):
    repo = _repo_with_snapshots(tmp_path, 1)
    cache = SnapshotMetadataCache()

    def boom(_):
        raise RuntimeError("disk scan failed")

    with pytest.raises(RuntimeError):
        cache.list_snapshots(repo, boom)
    assert cache.stats["errors"] == 1
    # 실패 뒤 정상 스캐너로 다시 읽으면 복구돼야 한다
    calls = []
    assert len(cache.list_snapshots(repo, _scanner(calls))) == 1


def test_missing_snapshots_dir_does_not_crash(tmp_path):
    repo = str(tmp_path / "empty_repo")
    os.makedirs(repo)
    cache = SnapshotMetadataCache()
    assert cache.list_snapshots(repo, lambda r: []) == []
    # fingerprint 이 None 이므로 다음 호출도 안전하게 다시 스캔한다
    assert cache.list_snapshots(repo, lambda r: []) == []


def test_global_singleton():
    assert get_snapshot_cache() is get_snapshot_cache()
