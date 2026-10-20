# -*- coding: utf-8 -*-
"""
tests/test_v241_features.py
v2.4.1 신규 기능 전수 검증 유닛 테스트 스위트:
1. WORMManager 파일 및 디렉토리 WORM 잠금/해제 및 수정/삭제 차단 검증
2. audit_entire_repository 저장소 정상 상태 검증
3. audit_entire_repository 물리적 블롭 Bit Rot (비트 손상) 탐지 격리 검증
4. audit_entire_repository 스냅샷 서명/매니페스트 위변조 탐지 검증
5. SnapshotEngine.create_snapshot 연동 WORM 보호 및 비동기 오프사이트 증분 복제 검증
"""

import os
import sys
import json
import time
import shutil
import hashlib
import tempfile
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.worm import WORMManager
from core.verify import IntegrityVerifier, generate_manifest_signature
from core.storage import BlobStorage
from core.snapshot import SnapshotEngine
from core.crypto_sign import Ed25519Signer


class TestV241Features(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="v241_test_")
        self.repo_dir = os.path.join(self.temp_dir, "repo")
        self.offsite_repo_dir = os.path.join(self.temp_dir, "offsite_repo")
        self.src_dir = os.path.join(self.temp_dir, "source")

        os.makedirs(self.repo_dir, exist_ok=True)
        os.makedirs(self.offsite_repo_dir, exist_ok=True)
        os.makedirs(self.src_dir, exist_ok=True)

        self.worm_manager = WORMManager()
        self.verifier = IntegrityVerifier(self.repo_dir)

    def tearDown(self):
        # Clean up any WORM protections before removing temp directory
        try:
            self.worm_manager.unprotect_directory(self.temp_dir, authorized=True)
        except Exception:
            pass


        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_worm_manager_file_and_directory(self):
        """1. WORMManager를 통한 파일 및 디렉토리 WORM 잠금/해제 및 수정/삭제 차단 검증"""
        test_file = os.path.join(self.repo_dir, "worm_test.txt")
        with open(test_file, 'w', encoding='utf-8') as f:
            f.write("Immutable WORM Payload")

        # Protect File
        self.assertTrue(self.worm_manager.protect_file(test_file, use_ntfs_acl=True))

        # Write attempt must be blocked
        try:
            with open(test_file, 'a', encoding='utf-8') as f:
                f.write(" Tampered")
            self.fail("보호된 WORM 파일에 쓰기가 성공했습니다 (실패해야 정상).")
        except (PermissionError, OSError):
            pass

        # Delete attempt must be blocked
        try:
            os.remove(test_file)
            self.fail("보호된 WORM 파일 삭제가 성공했습니다 (실패해야 정상).")
        except (PermissionError, OSError):
            pass

        # Unprotect File
        self.assertTrue(self.worm_manager.unprotect_file(test_file, authorized=True))

        # Modification and deletion after unprotect must succeed
        with open(test_file, 'a', encoding='utf-8') as f:
            f.write(" Legitimate modification")
        os.remove(test_file)
        self.assertFalse(os.path.exists(test_file))

        # Test Directory Protection
        test_sub_dir = os.path.join(self.repo_dir, "worm_subdir")
        os.makedirs(test_sub_dir, exist_ok=True)
        inner_file = os.path.join(test_sub_dir, "inner.txt")
        with open(inner_file, 'w', encoding='utf-8') as f:
            f.write("inner data")

        self.assertTrue(self.worm_manager.protect_directory(test_sub_dir))
        self.assertTrue(self.worm_manager.unprotect_directory(test_sub_dir, authorized=True))
        os.remove(inner_file)
        os.rmdir(test_sub_dir)

    def test_audit_entire_repository_healthy(self):
        """2. 정상 스냅샷 생성 후 audit_entire_repository() 수행 시 healthy 상태 검증"""
        # Create source file
        f1 = os.path.join(self.src_dir, "sample.txt")
        with open(f1, 'w', encoding='utf-8') as f:
            f.write("Normal file content for deep audit test")

        snap = SnapshotEngine.create_snapshot(
            repo_dir=self.repo_dir,
            sources=[self.src_dir],
            profile_id="audit_prof",
            min_free_disk_gb=0.0,
            use_vss=False
        )
        self.assertIsNotNone(snap)

        audit_res = self.verifier.audit_entire_repository()
        self.assertEqual(audit_res["status"], "healthy")
        self.assertEqual(len(audit_res["corrupted_blobs"]), 0)
        self.assertEqual(len(audit_res["corrupted_snapshots"]), 0)
        self.assertEqual(len(audit_res["missing_blobs"]), 0)
        self.assertGreater(audit_res["valid_blobs"], 0)
        self.assertEqual(audit_res["scanned_snapshots"], 1)

    def test_audit_entire_repository_detects_bit_rot(self):
        """3. 블롭 파일 강제 바이트 변조(Bit Rot) 시 audit_entire_repository() 탐지 검증"""
        f1 = os.path.join(self.src_dir, "bitrot_target.bin")
        with open(f1, 'wb') as f:
            f.write(b"Unique bitrot test payload " * 100)

        snap = SnapshotEngine.create_snapshot(
            repo_dir=self.repo_dir,
            sources=[self.src_dir],
            profile_id="bitrot_prof",
            min_free_disk_gb=0.0,
            use_vss=False
        )

        storage = BlobStorage(self.repo_dir)
        target_blob_id = snap["entries"][0]["blob_id"]
        blob_path = storage.get_blob_abs_path(target_blob_id)

        # Unlock and corrupt 1 byte
        self.worm_manager.unprotect_file(blob_path, authorized=True)
        with open(blob_path, 'r+b') as f:
            f.seek(15)
            f.write(b'\x00' if f.read(1) != b'\x00' else b'\xFF')
        self.worm_manager.protect_file(blob_path)

        audit_res = self.verifier.audit_entire_repository()
        self.assertEqual(audit_res["status"], "corrupted")
        corrupted_ids = [c["blob_id"] for c in audit_res["corrupted_blobs"]]
        self.assertIn(target_blob_id.lower(), corrupted_ids)

    def test_audit_entire_repository_detects_signature_tampering(self):
        """4. 스냅샷 manifest 내용 위조 시 audit_entire_repository() 서명 위변조 탐지 검증"""
        f1 = os.path.join(self.src_dir, "tamper_target.txt")
        with open(f1, 'w', encoding='utf-8') as f:
            f.write("Tamper test payload")

        snap = SnapshotEngine.create_snapshot(
            repo_dir=self.repo_dir,
            sources=[self.src_dir],
            profile_id="tamper_prof",
            min_free_disk_gb=0.0,
            use_vss=False
        )

        snap_file = os.path.join(self.repo_dir, "snapshots", f"{snap['id']}.json")
        self.worm_manager.unprotect_file(snap_file, authorized=True)

        with open(snap_file, 'r', encoding='utf-8') as f:
            manifest = json.load(f)

        # Tamper manifest payload
        manifest["summary"]["total_files"] = 9999

        with open(snap_file, 'w', encoding='utf-8') as f:
            json.dump(manifest, f)
        self.worm_manager.protect_file(snap_file)

        audit_res = self.verifier.audit_entire_repository()
        self.assertEqual(audit_res["status"], "corrupted")
        corrupted_snaps = [c["snapshot"] for c in audit_res["corrupted_snapshots"]]
        self.assertIn(f"{snap['id']}.json", corrupted_snaps)

    def test_snapshot_engine_with_worm_and_offsite_replication(self):
        """5. SnapshotEngine WORM 보호 및 비동기 오프사이트 복제 파이프라인 검증"""
        f1 = os.path.join(self.src_dir, "replicate_test.txt")
        with open(f1, 'w', encoding='utf-8') as f:
            f.write("Offsite replication automated test")

        snap = SnapshotEngine.create_snapshot(
            repo_dir=self.repo_dir,
            sources=[self.src_dir],
            profile_id="repl_prof",
            min_free_disk_gb=0.0,
            use_vss=False,
            offsite_repo_dir=self.offsite_repo_dir,
            worm_protect=True
        )
        self.assertIsNotNone(snap)

        # Wait briefly for background replication thread to finish
        time.sleep(0.5)

        # Verify offsite repository has blobs and manifest
        offsite_storage = BlobStorage(self.offsite_repo_dir)
        target_blob_id = snap["entries"][0]["blob_id"]
        offsite_blob_path = offsite_storage.get_blob_abs_path(target_blob_id)
        self.assertTrue(os.path.exists(offsite_blob_path), f"Offsite blob {offsite_blob_path} must exist")

        offsite_snap_path = os.path.join(offsite_storage.snapshots_dir, f"{snap['id']}.json")
        self.assertTrue(os.path.exists(offsite_snap_path), f"Offsite manifest {offsite_snap_path} must exist")

        # Verify offsite manifest integrity
        with open(offsite_snap_path, 'r', encoding='utf-8') as f:
            offsite_manifest = json.load(f)
        self.assertEqual(offsite_manifest["id"], snap["id"])


if __name__ == '__main__':
    unittest.main(verbosity=2)
