# -*- coding: utf-8 -*-
"""
tests/test_v239_verification.py: v2.3.9 검증 테스트
1. VSS Strict Mode: Silent Fallback 차단 및 VSSRequiredError 발생 검증
2. Manifest SHA-256 서명 위변조 탐지 검증
3. Automated Restore Verification: 계층화 샘플링 실제 디컴프레스 및 해시/크기 일치 검증
4. Bit Rot / 손상 블롭 발생 시 RestoreVerificationError 즉시 발생 검증
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from core.vss_manager import VSSContext, VSSRequiredError
from core.verify import (
    IntegrityVerifier, RestoreVerificationError,
    generate_manifest_signature, verify_manifest_signature
)
from core.snapshot import SnapshotEngine
from core.storage import BlobStorage, unlock_file_writable


class TestV239Verification(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="backup_v239_test_")
        self.repo_dir = os.path.join(self.test_dir, "repo")
        self.src_dir = os.path.join(self.test_dir, "src")
        os.makedirs(self.repo_dir, exist_ok=True)
        os.makedirs(self.src_dir, exist_ok=True)

        # 다양한 테스트 파일 생성 (소형, 대형, 유니코드 한글 경로)
        # 1. 소형 파일
        with open(os.path.join(self.src_dir, "small.txt"), "w", encoding="utf-8") as f:
            f.write("Small text file for testing.")

        # 2. 한글 및 공백 포함 파일
        with open(os.path.join(self.src_dir, "재무 제표_2026.csv"), "w", encoding="utf-8") as f:
            f.write("날짜,항목,금액\n2026-09-15,엔진 개발,1000000\n")

        # 3. 2MB 크기 데이터 파일
        with open(os.path.join(self.src_dir, "data.bin"), "wb") as f:
            f.write(b"A" * (2 * 1024 * 1024))

    def tearDown(self):
        for root, dirs, files in os.walk(self.test_dir):
            for f in files:
                try:
                    os.chmod(os.path.join(root, f), 0o777)
                except Exception:
                    pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_vss_strict_mode_blocks_unauthorized_backup(self):
        """VSS Strict 모드: 관리자 권한 미부여 시 Silent Fallback 없이 VSSRequiredError 발생"""
        with patch("core.vss_manager.is_admin", return_value=False):
            # strict=True 일 때 즉시 VSSRequiredError 발생해야 함
            with self.assertRaises(VSSRequiredError):
                with VSSContext(["C:\\Users\\test"], strict=True):
                    pass

    def test_vss_optional_mode_allows_fallback(self):
        """VSS Optional 모드: 관리자 권한 미부여 시 일반 직접 읽기 모드로 fallback 허용"""
        with patch("core.vss_manager.is_admin", return_value=False):
            with VSSContext(["C:\\Users\\test"], strict=False) as ctx:
                self.assertFalse(ctx.vss_active)
                self.assertTrue(len(ctx.warnings) > 0)

    def test_manifest_signature_and_tamper_detection(self):
        """Manifest 서명 생성 및 위변조 탐지 검증"""
        entries = [
            {"rel_path": "a.txt", "sha256": "1111111111111111111111111111111111111111111111111111111111111111"},
            {"rel_path": "b.txt", "sha256": "2222222222222222222222222222222222222222222222222222222222222222"}
        ]
        sig = generate_manifest_signature(entries)
        manifest = {"manifest_signature": sig, "entries": entries}

        # 원본 상태에서는 서명 검증 통과
        self.assertTrue(verify_manifest_signature(manifest))

        # 엔트리 위변조 시 검증 실패 확인
        tampered_manifest = {
            "manifest_signature": sig,
            "entries": [
                {"rel_path": "a.txt", "sha256": "9999999999999999999999999999999999999999999999999999999999999999"},
                {"rel_path": "b.txt", "sha256": "2222222222222222222222222222222222222222222222222222222222222222"}
            ]
        }
        self.assertFalse(verify_manifest_signature(tampered_manifest))

    def test_automated_restore_verification_pipeline(self):
        """백업 생성 시 Automated Restore Verification 및 Manifest Signature 자동 적용 검증"""
        snap = SnapshotEngine.create_snapshot(
            repo_dir=self.repo_dir,
            sources=[self.src_dir],
            profile_id="p1",
            min_free_disk_gb=0.0,
            use_vss=False,
            strict_vss=False
        )

        # 1. Manifest 서명 존재 확인
        self.assertIn("manifest_signature", snap)
        self.assertTrue(len(snap["manifest_signature"]) == 64)
        self.assertTrue(verify_manifest_signature(snap))

        # 2. Automated Restore Verification 결과 확인
        self.assertIn("restore_verification", snap)
        rv = snap["restore_verification"]
        self.assertEqual(rv.get("status"), "passed")
        self.assertGreaterEqual(rv.get("samples_verified"), 3)
        self.assertTrue(snap.get("is_verified"))

    def test_restore_verification_detects_corrupted_blob(self):
        """블롭 파일이 손상되었을 때 verify_restore_sampling이 RestoreVerificationError를 감지하는지 검증"""
        snap = SnapshotEngine.create_snapshot(
            repo_dir=self.repo_dir,
            sources=[self.src_dir],
            profile_id="p1",
            min_free_disk_gb=0.0,
            use_vss=False,
            strict_vss=False
        )

        # 하나의 블롭을 고의로 손상시킴
        target_entry = snap["entries"][0]
        blob_id = target_entry["blob_id"]
        storage = BlobStorage(self.repo_dir)
        blob_path = storage.get_blob_abs_path(blob_id)

        unlock_file_writable(blob_path, authorized=True)
        with open(blob_path, "wb") as f:
            f.write(b"CORRUPTED_GARBAGE_BYTES")

        # 손상된 상태에서 복원 샘플링 검증 실행 시 RestoreVerificationError 발생해야 함 (자가 치유 비활성화 모드)
        verifier = IntegrityVerifier(self.repo_dir)
        with self.assertRaises(RestoreVerificationError):
            verifier.verify_restore_sampling(snap, sample_count=20, self_heal=False)


if __name__ == "__main__":
    unittest.main()
