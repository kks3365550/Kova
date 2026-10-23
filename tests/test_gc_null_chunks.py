# -*- coding: utf-8 -*-
"""GC(prune_storage): 매니페스트에 "chunks": null 이 있어도 예외 없이 활성 해시를 수집하는지 검증."""

import json
import os
import shutil
import tempfile
import unittest

from core.snapshot import SnapshotEngine
from core.storage import BlobStorage


class TestPruneStorageNullChunks(unittest.TestCase):
    def setUp(self):
        self.repo_dir = tempfile.mkdtemp(prefix="backup_test_gc_nullchunks_")
        self.storage = BlobStorage(self.repo_dir)

    def tearDown(self):
        shutil.rmtree(self.repo_dir, ignore_errors=True)

    def _write_manifest(self, name, entries):
        path = os.path.join(self.storage.snapshots_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"entries": entries}, f)

    def test_null_chunks_does_not_abort_gc(self):
        self._write_manifest("snap_a.json", [
            {"rel_path": "a.txt", "sha256": "aa", "chunks": None},
            {"rel_path": "b.txt", "sha256": "bb", "chunks": [{"hash": "cc"}]},
        ])
        result = SnapshotEngine.prune_storage(self.repo_dir)
        self.assertEqual(result["deleted_blobs"], 0)

    def test_corrupt_manifest_still_fails_closed(self):
        path = os.path.join(self.storage.snapshots_dir, "snap_bad.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertRaises(RuntimeError):
            SnapshotEngine.prune_storage(self.repo_dir)


if __name__ == "__main__":
    unittest.main()
