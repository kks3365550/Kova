# -*- coding: utf-8 -*-
"""retention: created_at 을 읽을 수 없는 스냅샷은 기간 만료로 삭제하면 안 된다 (fail-closed)."""
import time
from unittest import mock

import pytest

from core.retention import RetentionManager

DAY = 86400


def _run(snapshots, retention_count=30, retention_days=30):
    with mock.patch("core.snapshot.SnapshotEngine.list_snapshots", return_value=snapshots), \
         mock.patch("core.snapshot.SnapshotEngine.delete_snapshot", return_value=True), \
         mock.patch("core.snapshot.SnapshotEngine.prune_storage", return_value={}), \
         mock.patch("core.retention.get_disk_free_gb", return_value=100.0):
        result = RetentionManager(".").apply_policy(
            retention_count=retention_count,
            retention_days=retention_days,
            authorized=True,
        )
    return set(result["deleted_snapshots"])


@pytest.mark.parametrize("bad", ["garbage", "", "   ", None, "2026-13-99T99:99:99"])
def test_unparseable_created_at_is_not_expired(bad):
    now = time.time()
    snaps = [
        {"id": "newest", "created_at": now},
        {"id": "bad", "created_at": bad},
    ]
    assert "bad" not in _run(snaps)


def test_missing_created_at_key_is_not_expired():
    now = time.time()
    snaps = [{"id": "newest", "created_at": now}, {"id": "nokey"}]
    assert "nokey" not in _run(snaps)


def test_truly_old_snapshot_is_still_expired():
    now = time.time()
    snaps = [
        {"id": "newest", "created_at": now},
        {"id": "old", "created_at": now - 100 * DAY},
    ]
    assert _run(snaps) == {"old"}


def test_old_iso_string_is_still_expired():
    now = time.time()
    old_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 100 * DAY))
    snaps = [
        {"id": "newest", "created_at": now},
        {"id": "old_iso", "created_at": old_iso},
    ]
    assert _run(snaps) == {"old_iso"}


def test_recent_snapshots_are_kept():
    now = time.time()
    snaps = [
        {"id": "a", "created_at": now},
        {"id": "b", "created_at": now - 5 * DAY},
        {"id": "c", "created_at": now - 10 * DAY},
    ]
    assert _run(snaps) == set()


def test_newest_is_never_deleted_even_if_old():
    now = time.time()
    snaps = [
        {"id": "newest", "created_at": now - 200 * DAY},
        {"id": "older", "created_at": now - 300 * DAY},
    ]
    assert "newest" not in _run(snaps)


def test_count_rule_still_trims_excess():
    now = time.time()
    snaps = [{"id": "s%d" % i, "created_at": now - i * 60} for i in range(5)]
    assert _run(snaps, retention_count=3, retention_days=None) == {"s3", "s4"}
