import os
import json
import tempfile
import unittest
from core.metadata_db import MetadataDB
from core.snapshot import SnapshotEngine

class TestRustManifestSchemaCompatibility(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.snap_dir = os.path.join(self.tmp, "snapshots")
        os.makedirs(self.snap_dir, exist_ok=True)

    def test_rust_manifest_parsing_and_auto_healing(self):
        snap_id = "snap_custom_20261020_220307_a0fd"
        snap_file = os.path.join(self.snap_dir, f"{snap_id}.json")

        # Simulate exact Rust manifest output
        rust_manifest = {
            "id": snap_id,
            "session_id": snap_id,
            "created_at": "2026-10-20T22:08:14.123456+09:00",
            "source_root": "C:\\Data",
            "total_files": 359602,
            "total_bytes": 84728512512,
            "dedup_bytes": 54498877440,
            "compressed_bytes": 12345678,
            "files": [
                {
                    "path": "test_folder/doc.txt",
                    "size": 1024,
                    "modified": 1729430000,
                    "sha256": "abcdef123456"
                }
            ]
        }
        with open(snap_file, "w", encoding="utf-8") as f:
            json.dump(rust_manifest, f)

        # 1. Verify MetadataDB syncs it with correct values
        db = MetadataDB(self.tmp)
        snaps = db.sync_snapshots()
        self.assertEqual(len(snaps), 1)
        s = snaps[0]
        self.assertEqual(s["id"], snap_id)
        self.assertEqual(s["summary"]["total_files"], 359602)
        self.assertEqual(s["summary"]["total_bytes"], 84728512512)
        self.assertEqual(s["summary"]["dedup_saved_bytes"], 54498877440)
        self.assertEqual(s["iso_time"], "2026-10-20T22:08:14.123456+09:00")
        self.assertEqual(s["profile_name"], "선택 백업")

        # 2. Simulate previously corrupted 0-value cache in SQLite
        with db._lock, db._get_connection() as conn:
            conn.execute("UPDATE snapshots_meta SET total_files = 0, total_bytes = 0 WHERE id = ?", (snap_id,))
            conn.commit()

        # Re-sync should auto-heal
        healed_snaps = db.sync_snapshots()
        self.assertEqual(healed_snaps[0]["summary"]["total_files"], 359602)
        self.assertEqual(healed_snaps[0]["summary"]["total_bytes"], 84728512512)

        # 3. Test browsing Rust files
        browse = SnapshotEngine.browse_snapshot_directory(self.tmp, snap_id, subpath="")
        self.assertIn("items", browse)
        self.assertTrue(any(d["name"] == "test_folder" for d in browse["items"]))

        # 4. Test tree building
        tree = SnapshotEngine.build_snapshot_tree(rust_manifest)
        self.assertEqual(tree["type"], "directory")

if __name__ == '__main__':
    unittest.main()
