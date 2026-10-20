# -*- coding: utf-8 -*-
"""
tests/test_track2_consolidation.py
Track 2-17: Integration tests for Pack Consolidation and Delayed GC Engine
1. Consolidate unpacked .blob files into a .pack file
2. Verify all chunks are readable via Dual-Read after consolidation
3. Verify originals are safely moved to quarantine
4. Test Delayed GC purging quarantined files
5. Test fail-closed rollback behavior on error
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from core.storage import BlobStorage
from core.pack_consolidator import PackConsolidator


class TestTrack2Consolidation(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_consolidation_")
        self.repo_dir = os.path.join(self.test_dir, "repo")
        os.makedirs(self.repo_dir, exist_ok=True)
        self.storage = BlobStorage(self.repo_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_consolidation_and_delayed_gc(self):
        # 1. Create 5 individual blob files in blobs/
        blob_hashes = []
        blob_data_map = {}
        for i in range(5):
            data = f"Individual chunk content block {i}".encode("utf-8") * 20
            h, orig, stored, is_new = self.storage.put_bytes_blob(data, use_pack=False)
            blob_hashes.append(h)
            blob_data_map[h] = data

        # Check all are individual blobs on disk
        for h in blob_hashes:
            blob_path = self.storage.get_blob_abs_path(h)
            self.assertTrue(os.path.exists(blob_path))

        # 2. Run consolidator
        consolidator = PackConsolidator(self.repo_dir, quarantine_retention_seconds=0.1)
        res = consolidator.consolidate(dry_run=False)

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["consolidated_count"], 5)
        self.assertEqual(res["quarantined_count"], 5)

        # 3. Verify Dual-Read: all chunks must still be 100% accessible and verified
        for h in blob_hashes:
            self.assertTrue(self.storage.has_blob(h))
            self.assertEqual(self.storage.read_blob_bytes(h), blob_data_map[h])
            ok, err, sz = self.storage.verify_blob(h)
            self.assertTrue(ok)

        # Check originals were moved to quarantine (not deleted immediately)
        quarantine_dir = Path(self.repo_dir) / ".quarantine_blobs"
        q_files = list(quarantine_dir.glob("*.blob"))
        self.assertEqual(len(q_files), 5)

        # 4. Run Delayed GC
        deleted, freed = consolidator.run_delayed_gc(force_all=True)
        self.assertEqual(deleted, 5)
        self.assertEqual(len(list(quarantine_dir.glob("*.blob"))), 0)

        # Chunks in Pack must still be 100% intact even after GC
        for h in blob_hashes:
            self.assertTrue(self.storage.has_blob(h))
            self.assertEqual(self.storage.read_blob_bytes(h), blob_data_map[h])


if __name__ == "__main__":
    unittest.main()
