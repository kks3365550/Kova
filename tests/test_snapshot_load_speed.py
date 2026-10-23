import json
import os
import tempfile
import unittest

from core.metadata_db import MetadataDB
from core.snapshot import SnapshotEngine


def _old_browse(entries, subpath):
    """Reference implementation (previous full-scan behaviour)."""
    subpath_norm = subpath.replace('\\', '/').strip('/')
    prefix = subpath_norm + '/' if subpath_norm else ''
    dirs, files = {}, []
    for e in entries:
        rp = (e.get("rel_path") or e.get("path") or "").replace('\\', '/').strip('/')
        if prefix:
            if not rp.startswith(prefix):
                continue
            remain = rp[len(prefix):]
        else:
            remain = rp
        parts = remain.split('/')
        if len(parts) == 1:
            files.append({
                "name": parts[0], "type": "file", "size": e.get("size", 0),
                "mtime": e.get("mtime") or e.get("modified", 0), "rel_path": rp,
                "status": e.get("status", "unmodified"),
                "chunk_strategy": e.get("chunk_strategy"),
                "chunk_count": len(e.get("chunk_ids", [])) if e.get("chunk_ids") else 1,
            })
        else:
            d = dirs.setdefault(parts[0], {"name": parts[0], "type": "directory",
                                           "rel_path": prefix + parts[0], "file_count": 0, "size": 0})
            d["file_count"] += 1
            d["size"] += e.get("size", 0)
    dl = sorted(dirs.values(), key=lambda x: x["name"].lower())
    fl = sorted(files, key=lambda x: x["name"].lower())
    return dl + fl


ENTRIES = [
    {"path": "a/b/c.txt", "size": 10, "modified": 1},
    {"path": "a/b/d.txt", "size": 5, "modified": 2, "chunk_ids": ["x", "y"]},
    {"rel_path": "a\\e.txt", "size": 7, "mtime": 3, "status": "modified"},
    {"path": "top.txt", "size": 1, "modified": 4},
    {"path": "Zed/q/r/s.bin", "size": 100, "modified": 5},
    {"path": "a/b", "size": 0, "modified": 6, "is_dir": True},
]


class SnapshotLoadSpeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "snapshots"))
        self.sid = "snap_custom_20260101_000000_aaaa"
        self.path = os.path.join(self.tmp, "snapshots", f"{self.sid}.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({
                "id": self.sid, "created_at_ts": 1.0, "iso_time": "2026-01-01T00:00:00",
                "profile_name": "P", "profile_id": "pid", "backup_type": "incremental",
                "total_files": 6, "total_bytes": 123, "sources": ["C:/x"], "files": ENTRIES,
            }, f)

    def test_browse_index_matches_old_scan(self):
        for sub in ["", "a", "a/b", "a\\b", "Zed", "Zed/q", "Zed/q/r", "nope", "a/b/c.txt"]:
            got = SnapshotEngine.browse_snapshot_directory(self.tmp, self.sid, subpath=sub)["items"]
            self.assertEqual(got, _old_browse(ENTRIES, sub), f"subpath={sub!r}")

    def test_upsert_from_data_avoids_reparse_and_meta_lookup(self):
        db = MetadataDB(self.tmp)
        data = json.load(open(self.path, encoding="utf-8"))
        db.upsert_snapshot_from_data(self.sid, data, os.path.getmtime(self.path))
        meta = db.get_snapshot_meta(self.sid)
        self.assertEqual(meta["profile_name"], "P")
        self.assertEqual(meta["backup_type"], "incremental")
        self.assertEqual(meta["summary"]["total_files"], 6)
        self.assertIsNone(db.get_snapshot_meta("missing"))

    def test_api_fast_path_only_when_row_in_sync(self):
        from web.api_snapshots import _snapshot_meta_from_db
        db = MetadataDB(self.tmp)
        db.sync_snapshots()
        self.assertEqual(_snapshot_meta_from_db(self.tmp, self.sid)["profile_id"], "pid")
        os.utime(self.path, (1, 1))  # file changed since sync -> must fall back
        self.assertIsNone(_snapshot_meta_from_db(self.tmp, self.sid))
        self.assertIsNone(_snapshot_meta_from_db(self.tmp, "missing"))


if __name__ == "__main__":
    unittest.main()
