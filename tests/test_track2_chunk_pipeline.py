# -*- coding: utf-8 -*-
"""
tests/test_track2_chunk_pipeline.py
Track 2-12: Full Unit and Integration Test Suite for Chunk Ingest & Multi-chunk Assembly
"""

import os
import shutil
import tempfile
import unittest
import hashlib
from pathlib import Path

from core.snapshot import SnapshotEngine
from core.restore import RestoreEngine
from core.verify import IntegrityVerifier
from core.storage import BlobStorage
from core.chunk_engine import ChunkPolicySelector, FixedBlockAdapter, FastCDCAdapter, WholeFileAdapter


class TestTrack2ChunkPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="chunk_pipe_test_")
        self.repo_dir = os.path.join(self.tmp_dir, "repo")
        self.src_dir = os.path.join(self.tmp_dir, "src")
        self.restore_dir = os.path.join(self.tmp_dir, "restore")

        os.makedirs(self.repo_dir, exist_ok=True)
        os.makedirs(self.src_dir, exist_ok=True)
        os.makedirs(self.restore_dir, exist_ok=True)

        # 1. Small file (< 16MB) -> WholeFile CAS test
        self.small_file = os.path.join(self.src_dir, "small.txt")
        with open(self.small_file, "wb") as f:
            f.write(b"SMALL_FILE_PAYLOAD_FOR_CAS" * 50)

        # 2. Large database file (> 16MB) -> Fixed 4MB Chunking test
        # We create a 18MB structured binary file (.db extension)
        self.large_file = os.path.join(self.src_dir, "test_database.db")
        self.large_content = os.urandom(18 * 1024 * 1024)
        self.large_sha256 = hashlib.sha256(self.large_content).hexdigest()
        with open(self.large_file, "wb") as f:
            f.write(self.large_content)


    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_chunk_policy_selector(self):
        """정책 선택기: 크기와 확장자에 따라 올바른 어댑터를 선택하는지 검증"""
        selector = ChunkPolicySelector(threshold_bytes=16 * 1024 * 1024)

        # 1MB 파일 -> WholeFile
        adapter_small = selector.select_adapter("doc.pdf", 1024 * 1024)
        self.assertEqual(adapter_small.strategy_id, "whole_file")

        # 20MB .vhdx -> Fixed Block
        adapter_vhdx = selector.select_adapter("disk.vhdx", 20 * 1024 * 1024)
        self.assertTrue(adapter_vhdx.strategy_id.startswith("fixed_"))

    def test_end_to_end_chunk_ingest_and_restore(self):
        """
        [엔드투엔드 파이프라인 검증]
        1. Snapshot 생성: 18MB 파일이 multi-chunk로 분할 저장되는지 확인
        2. Manifest 엔트리 검증: chunk_ids 및 chunk_strategy 확인
        3. Automated Restore Verification 통과 확인
        4. Restore 실행: Multi-chunk가 순서대로 온전하게 조립 복원되는지 검증
        5. 복원된 파일 바이트 및 SHA-256 100% 일치 확인
        """
        snap = SnapshotEngine.create_snapshot(
            repo_dir=self.repo_dir,
            sources=[self.src_dir],
            profile_id="p_chunk",
            min_free_disk_gb=0.0,
            use_vss=False,
            strict_vss=False
        )

        self.assertIn("manifest_signature", snap)
        self.assertTrue(snap.get("is_verified"))

        # Find the large database entry
        large_entry = None
        for entry in snap.get("entries", []):
            if entry.get("rel_path") == "test_database.db":
                large_entry = entry
                break

        self.assertIsNotNone(large_entry, "test_database.db entry must exist in manifest")
        self.assertIn("chunk_ids", large_entry, "Large file must have chunk_ids")
        self.assertGreater(len(large_entry["chunk_ids"]), 1, "18MB file must be split into multiple chunks")
        self.assertTrue(large_entry["chunk_strategy"].startswith("fixed_"))

        # Run Restore
        restore_result = RestoreEngine.restore_snapshot(
            repo_dir=self.repo_dir,
            snapshot_id=snap["id"],
            target_dir=self.restore_dir,
            overwrite=True
        )


        self.assertEqual(restore_result["restored_files"], 2, "Both small and large files must restore successfully")
        self.assertEqual(len(restore_result["failed_files"]), 0)


        # Bit-for-bit check
        restored_large_path = os.path.join(self.restore_dir, "test_database.db")
        self.assertTrue(os.path.exists(restored_large_path))
        with open(restored_large_path, "rb") as f:
            restored_bytes = f.read()

        self.assertEqual(len(restored_bytes), len(self.large_content))
        self.assertEqual(hashlib.sha256(restored_bytes).hexdigest(), self.large_sha256)


    def test_integrity_verifier_audit_with_chunks(self):
        """저장소 전수 감사(Deep Audit) 시 다중 청크가 정상 인식되는지 검증"""
        snap = SnapshotEngine.create_snapshot(
            repo_dir=self.repo_dir,
            sources=[self.src_dir],
            profile_id="p_chunk",
            min_free_disk_gb=0.0,
            use_vss=False,
            strict_vss=False
        )

        verifier = IntegrityVerifier(self.repo_dir)
        audit_res = verifier.audit_entire_repository()

        self.assertEqual(audit_res["status"], "healthy")
        self.assertEqual(audit_res["valid_snapshots"], 1)
        self.assertEqual(len(audit_res["missing_blobs"]), 0)
        self.assertEqual(len(audit_res["corrupted_blobs"]), 0)


if __name__ == "__main__":
    unittest.main()
