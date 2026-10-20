# -*- coding: utf-8 -*-
"""
tests/test_self_healing.py - 손상 블롭 자동 자가 치유(Self-Healing) 검증 테스트
"""

import os
import json
import stat
import tempfile
import unittest
import hashlib

from core.storage import BlobStorage, lock_file_immutable
from core.verify import IntegrityVerifier, RestoreVerificationError


class TestSelfHealing(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.repo_dir = os.path.join(self.tmp_dir.name, "repo")
        self.source_dir = os.path.join(self.tmp_dir.name, "source")
        os.makedirs(self.repo_dir, exist_ok=True)
        os.makedirs(self.source_dir, exist_ok=True)

        self.storage = BlobStorage(self.repo_dir)
        self.verifier = IntegrityVerifier(self.repo_dir)

    def tearDown(self):
        try:
            for root, dirs, files in os.walk(self.tmp_dir.name):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        os.chmod(fp, stat.S_IWRITE)
                    except OSError:
                        pass
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_self_healing_corrupted_blob_with_valid_source(self):
        """원본 소스가 온전한 경우 손상된 블롭이 복원 검증 시 자동 자가 치유(Self-Heal)되는지 검증."""
        test_file = os.path.join(self.source_dir, "sample.dat")
        content = b"CRITICAL_DATA_FOR_SELF_HEALING" * 500
        with open(test_file, "wb") as f:
            f.write(content)

        file_sha256 = hashlib.sha256(content).hexdigest()
        file_size = len(content)

        # 블롭 저장
        h, orig_sz, stored_sz, is_new = self.storage.put_file_blob_onepass(test_file)
        self.assertTrue(is_new)

        blob_path = self.storage.get_blob_abs_path(h)
        self.assertTrue(os.path.exists(blob_path))

        # 1. 인위적으로 블롭 파일 내용 손상 (Bit-Rot 시뮬레이션)
        os.chmod(blob_path, stat.S_IWRITE)
        with open(blob_path, "r+b") as f:
            f.seek(10)
            f.write(b"\x00\xFF\x00\xFF\x00\xFF") # 6바이트 파괴
        lock_file_immutable(blob_path)

        manifest = {
            "snapshot_id": "snap_self_heal_001",
            "entries": [
                {
                    "source_root": self.source_dir,
                    "rel_path": "sample.dat",
                    "size": file_size,
                    "sha256": file_sha256,
                    "blob_id": file_sha256,
                    "status": "new"
                }
            ]
        }

        # 2. 복원 검증 실행 -> 원본 소스를 기반으로 자동 자가 치유되어 passed 반환해야 함!
        result = self.verifier.verify_restore_sampling(manifest, sample_count=1)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["samples_verified"], 1)

        # 3. 치료된 블롭이 이제 정상적으로 압축 해제 및 해시 일치하는지 확인
        sz, actual_h = self.verifier._decompress_and_hash_blob(blob_path)
        self.assertEqual(sz, file_size)
        self.assertEqual(actual_h, file_sha256)

    def test_self_healing_fails_when_source_missing(self):
        """원본 소스가 없는 상태에서 블롭이 손상되었으면 정상적으로 RestoreVerificationError 발생 검증."""
        test_file = os.path.join(self.source_dir, "lost_source.dat")
        content = b"DATA_WITHOUT_SOURCE_BACKUP" * 500
        with open(test_file, "wb") as f:
            f.write(content)

        file_sha256 = hashlib.sha256(content).hexdigest()
        file_size = len(content)

        h, orig_sz, stored_sz, is_new = self.storage.put_file_blob_onepass(test_file)
        blob_path = self.storage.get_blob_abs_path(h)

        # 원본 파일 삭제
        os.remove(test_file)

        # 블롭 손상
        os.chmod(blob_path, stat.S_IWRITE)
        with open(blob_path, "r+b") as f:
            f.seek(10)
            f.write(b"\x00\xFF\x00\xFF")
        lock_file_immutable(blob_path)

        manifest = {
            "snapshot_id": "snap_self_heal_002",
            "entries": [
                {
                    "source_root": self.source_dir,
                    "rel_path": "lost_source.dat",
                    "size": file_size,
                    "sha256": file_sha256,
                    "blob_id": file_sha256,
                    "status": "new"
                }
            ]
        }

        with self.assertRaises(RestoreVerificationError):
            self.verifier.verify_restore_sampling(manifest, sample_count=1)


if __name__ == "__main__":
    unittest.main()
