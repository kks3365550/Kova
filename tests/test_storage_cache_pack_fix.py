# -*- coding: utf-8 -*-
"""Regression tests: stale _blob_cache pre-fill and pack commit failure handling."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from core.storage import BlobStorage
from core.snapshot import SnapshotEngine


class TestStorageCachePackFix(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_cache_pack_fix_")
        self.repo_dir = os.path.join(self.test_dir, "repo")
        self.source_dir = os.path.join(self.test_dir, "source")
        os.makedirs(self.repo_dir, exist_ok=True)
        os.makedirs(self.source_dir, exist_ok=True)
        BlobStorage._class_blob_caches.pop(os.path.abspath(self.repo_dir), None)
        self.storage = BlobStorage(self.repo_dir)
        self.storage._blob_cache.clear()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_bulk_add_ignores_nonexistent_ids(self):
        fake = "ab" + "0" * 62
        self.storage.bulk_add_blob_cache([fake, "", None])
        self.assertFalse(self.storage.has_blob(fake))

    def test_bulk_add_keeps_existing_blob(self):
        data = b"existing blob payload"
        h, _, _, _ = self.storage.put_bytes_blob(data, use_pack=False)
        self.storage._blob_cache.clear()
        self.storage.bulk_add_blob_cache([h])
        self.assertIn(h, self.storage._blob_cache)
        self.assertTrue(self.storage.has_blob(h))

    def test_stale_id_is_rewritten(self):
        data = b"payload that goes missing" * 10
        h, _, _, _ = self.storage.put_bytes_blob(data, use_pack=False)
        path = self.storage.get_blob_abs_path(h)
        os.chmod(path, 0o666)
        os.remove(path)
        self.storage._blob_cache.clear()
        self.storage.bulk_add_blob_cache([h])
        h2, _, _, is_new = self.storage.put_bytes_blob(data, use_pack=False)
        self.assertEqual(h, h2)
        self.assertTrue(is_new)
        self.assertTrue(os.path.exists(self.storage.get_blob_abs_path(h)))

    def test_commit_failure_cleans_up(self):
        data = b"pack chunk before failed commit" * 20
        h, _, _, _ = self.storage.put_bytes_blob(data, use_pack=True)
        writer = self.storage._active_pack_writer
        self.assertIsNotNone(writer)
        self.assertTrue(self.storage.has_blob(h))
        with mock.patch.object(writer, "commit_index", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.storage.commit_active_pack()
        self.assertIsNone(self.storage._active_pack_writer)
        self.assertTrue(writer.pack_file.closed)
        self.assertFalse(self.storage.has_blob(h))
        # next put creates a new writer and succeeds
        h2, _, _, is_new = self.storage.put_bytes_blob(data, use_pack=True)
        self.assertTrue(is_new)
        self.storage.commit_active_pack()
        self.assertTrue(self.storage.has_blob(h2))

    def test_commit_without_writer_is_noop(self):
        self.storage.commit_active_pack()
        self.storage.commit_active_pack()

    def test_pipeline_exception_still_commits_idx(self):
        data = b"chunk written before pipeline abort" * 20
        captured = {}

        def fake_pipeline(*args, **kwargs):
            st = kwargs["storage"]
            captured["h"], _, _, _ = st.put_bytes_blob(data, use_pack=True)
            raise RuntimeError("pipeline aborted")

        with open(os.path.join(self.source_dir, "a.txt"), "wb") as f:
            f.write(b"x")
        with mock.patch.object(SnapshotEngine, "_execute_backup_pipeline", side_effect=fake_pipeline):
            with self.assertRaises(RuntimeError):
                SnapshotEngine.create_snapshot(
                    repo_dir=self.repo_dir, sources=[self.source_dir],
                    profile_name="X", use_vss=False)
        packs_dir = os.path.join(self.repo_dir, "packs")
        packs = [f for f in os.listdir(packs_dir) if f.endswith(".pack")]
        idxs = [f for f in os.listdir(packs_dir) if f.endswith(".idx")]
        self.assertTrue(packs)
        self.assertEqual(len(packs), len(idxs))


if __name__ == "__main__":
    unittest.main()
