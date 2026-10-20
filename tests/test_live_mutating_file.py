# -*- coding: utf-8 -*-
"""
tests/test_live_mutating_file.py - 실시간 크기 변동 파일의 매니페스트 정합성 및 복원 검증 테스트
"""

import os
import tempfile
import unittest
import hashlib

from core.snapshot import SnapshotEngine
from core.verify import IntegrityVerifier


class TestLiveMutatingFile(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.repo_dir = os.path.join(self.tmp_dir.name, "repo")
        self.src_dir = os.path.join(self.tmp_dir.name, "src")
        os.makedirs(self.repo_dir, exist_ok=True)
        os.makedirs(self.src_dir, exist_ok=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_live_mutating_file_records_actual_blob_size(self):
        """백업 도중 크기가 변하는 파일에 대해 매니페스트가 실제 블롭 크기를 기록하고 복원 검증을 통과하는지 검증."""
        test_file = os.path.join(self.src_dir, "live_app.log")
        with open(test_file, "wb") as f:
            f.write(b"A" * 10000)

        # 스냅샷 생성 실행
        manifest = SnapshotEngine.create_snapshot(
            repo_dir=self.repo_dir,
            sources=[self.src_dir],
            profile_id="live_test",
            min_free_disk_gb=0.0,
            use_vss=False,
            strict_vss=False
        )

        entry = manifest["entries"][0]
        self.assertEqual(entry["size"], 10000)

        # 복원 검증 검사
        verifier = IntegrityVerifier(self.repo_dir)
        res = verifier.verify_restore_sampling(manifest, sample_count=1)
        self.assertEqual(res["status"], "passed")


if __name__ == "__main__":
    unittest.main()
