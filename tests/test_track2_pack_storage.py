# -*- coding: utf-8 -*-
"""
tests/test_track2_pack_storage.py
Track 2-16: Pack Container Production Activation & Dual-Read Integration Tests
1. Direct PackContainerWriter & Reader atomic commit and recovery
2. BlobStorage Dual-Read (Pack chunk + Individual blob in same repo)
3. Snapshot with multi-chunks stored into Pack Container, verified, and restored
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from core.storage import BlobStorage
from core.snapshot import SnapshotEngine
from core.restore import RestoreEngine
from core.verify import IntegrityVerifier
from core.pack_format import PackContainerWriter, PackContainerReader, PackRecoveryEngine
from core.composite_storage import CompositeStorageFacade


class TestTrack2PackStorage(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_pack_storage_")
        self.repo_dir = os.path.join(self.test_dir, "repo")
        self.source_dir = os.path.join(self.test_dir, "source")
        self.restore_dir = os.path.join(self.test_dir, "restore")
        os.makedirs(self.repo_dir, exist_ok=True)
        os.makedirs(self.source_dir, exist_ok=True)
        os.makedirs(self.restore_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_pack_format_atomic_commit_and_recovery(self):
        pack_path = Path(self.repo_dir) / "packs" / "test_01.pack"
        idx_path = Path(self.repo_dir) / "packs" / "test_01.idx"

        writer = PackContainerWriter(pack_path, idx_path)
        c1 = b"Chunk payload data 1" * 100
        c2 = b"Chunk payload data 2" * 200
        import hashlib
        h1 = hashlib.sha256(c1).hexdigest()
        h2 = hashlib.sha256(c2).hexdigest()

        writer.write_chunk(h1, c1)
        writer.write_chunk(h2, c2)
        writer.commit_index()
        writer.close()

        reader = PackContainerReader(pack_path, idx_path)
        self.assertTrue(reader.has_chunk(h1))
        self.assertTrue(reader.has_chunk(h2))
        self.assertEqual(reader.read_chunk(h1), c1)
        self.assertEqual(reader.read_chunk(h2), c2)

        # Corrupt/delete index and test RecoveryEngine
        os.remove(idx_path)
        rebuilt_count, valid_offset = PackRecoveryEngine.rebuild_index_from_pack(pack_path, idx_path)
        self.assertEqual(rebuilt_count, 2)
        reader2 = PackContainerReader(pack_path, idx_path)
        self.assertEqual(reader2.read_chunk(h1), c1)

    def test_dual_read_in_blob_storage(self):
        storage = BlobStorage(self.repo_dir)

        # 1. Store individual blob (<16MB style)
        b_data = b"Individual blob data test"
        b_hash, orig_sz, stored_sz, is_new = storage.put_bytes_blob(b_data, use_pack=False)
        self.assertTrue(storage.has_blob(b_hash))
        self.assertEqual(storage.read_blob_bytes(b_hash), b_data)

        # 2. Store pack chunk (use_pack=True)
        p_data = b"Pack chunk data test" * 50
        p_hash, p_orig_sz, p_stored_sz, p_is_new = storage.put_bytes_blob(p_data, use_pack=True)
        storage.commit_active_pack()

        # Both must be accessible via identical Dual-Read APIs
        self.assertTrue(storage.has_blob(p_hash))
        self.assertTrue(storage.has_blob(b_hash))
        self.assertEqual(storage.read_blob_bytes(p_hash), p_data)
        self.assertEqual(storage.read_blob_bytes(b_hash), b_data)

        # Both must verify cleanly
        ok1, err1, sz1 = storage.verify_blob(b_hash)
        self.assertTrue(ok1)
        self.assertEqual(sz1, len(b_data))

        ok2, err2, sz2 = storage.verify_blob(p_hash)
        self.assertTrue(ok2)
        self.assertEqual(sz2, len(p_data))

    def test_full_snapshot_and_restore_with_pack_storage(self):
        # Create a large 18MB file that triggers multi-chunking
        large_path = os.path.join(self.source_dir, "big_database.db")
        large_content = b"Large database content block!\n" * (600 * 1024)
        with open(large_path, "wb") as f:
            f.write(large_content)

        # Also create a small file (< 16MB)
        small_path = os.path.join(self.source_dir, "small_note.txt")
        small_content = b"Small note text content"
        with open(small_path, "wb") as f:
            f.write(small_content)

        # Snapshot creation with automated restore verification
        snap = SnapshotEngine.create_snapshot(
            repo_dir=self.repo_dir,
            sources=[self.source_dir],
            profile_name="PackStorageTest",
            use_vss=False
        )
        self.assertTrue(snap.get("is_verified", False))

        # Check pack files exist in repo
        packs_dir = os.path.join(self.repo_dir, "packs")
        pack_files = [f for f in os.listdir(packs_dir) if f.endswith(".pack")]
        self.assertGreater(len(pack_files), 0, "At least one .pack file should be created for multi-chunks")

        # Full restore test
        restore_res = RestoreEngine.restore_snapshot(
            repo_dir=self.repo_dir,
            snapshot_id=snap["id"],
            target_dir=self.restore_dir,
            overwrite=True
        )
        self.assertEqual(restore_res["restored_files"], 2)

        # Verify restored content matches byte-for-byte
        with open(os.path.join(self.restore_dir, "big_database.db"), "rb") as f:
            self.assertEqual(f.read(), large_content)
        with open(os.path.join(self.restore_dir, "small_note.txt"), "rb") as f:
            self.assertEqual(f.read(), small_content)


if __name__ == "__main__":
    unittest.main()
