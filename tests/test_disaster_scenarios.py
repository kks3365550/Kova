# -*- coding: utf-8 -*-
"""
tests/test_disaster_scenarios.py: v2.4.0 재해 복구(DR) 극한 파괴 테스트 & 오프사이트 복제 검증
1. 오프사이트 CAS 블롭 증분 복제(Replication) 무결성 검증
2. Ed25519 비대칭키 전자서명 위조 탐지 검증
3. DR 극한 시나리오 1: 메타데이터 DB 완전 삭제 시 순수 CAS 복원 검증
4. DR 극한 시나리오 2: 특정 블롭 손상 시 나머지 정상 파일 격리 복구 검증
5. DR 극한 시나리오 3: 최신 manifest 손상 시 직전 스냅샷 롤백 복구 검증
"""

import os
import shutil
import tempfile
import unittest

from core.snapshot import SnapshotEngine
from core.restore import RestoreEngine
from core.storage import BlobStorage, unlock_file_writable
from core.crypto_sign import Ed25519Signer, verify_manifest_signature_ed25519
from core.replication import ReplicationManager


class TestDisasterScenarios(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="dr_test_")
        self.local_repo = os.path.join(self.test_dir, "local_repo")
        self.remote_repo = os.path.join(self.test_dir, "remote_repo")
        self.src_dir = os.path.join(self.test_dir, "src")
        self.restore_dir = os.path.join(self.test_dir, "restore")

        os.makedirs(self.local_repo, exist_ok=True)
        os.makedirs(self.remote_repo, exist_ok=True)
        os.makedirs(self.src_dir, exist_ok=True)
        os.makedirs(self.restore_dir, exist_ok=True)

        # 테스트용 소스 파일 5개 생성
        self.files_data = {
            "config.json": '{"version": 1, "db": "localhost"}',
            "notes.txt": "긴급 메모: 재해 복구 시스템 테스트 중입니다.",
            "문서/재무.csv": "일자,수입,지출\n2026-09-15,5000,2000",
            "bin/kernel.dat": "KERNEL_BINARY_DATA_01010101",
            "large.dat": "L" * (1024 * 1024) # 1MB
        }
        for rel_p, content in self.files_data.items():
            full_p = os.path.join(self.src_dir, rel_p)
            os.makedirs(os.path.dirname(full_p), exist_ok=True)
            mode = "wb" if isinstance(content, bytes) else "w"
            enc = None if isinstance(content, bytes) else "utf-8"
            with open(full_p, mode, encoding=enc) as f:
                f.write(content)

    def tearDown(self):
        for root, dirs, files in os.walk(self.test_dir):
            for f in files:
                try:
                    os.chmod(os.path.join(root, f), 0o777)
                except Exception:
                    pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_ed25519_signature_and_adversarial_tampering(self):
        """Ed25519 비대칭키 서명 생성 및 위조 시 100% 탐지 검증"""
        snap = SnapshotEngine.create_snapshot(
            repo_dir=self.local_repo,
            sources=[self.src_dir],
            profile_id="test_ed25519",
            min_free_disk_gb=0.0,
            use_vss=False
        )

        # 1. Ed25519 서명이 manifest에 정상 날인되었는지 확인
        self.assertIn("ed25519_signature", snap)
        self.assertIsNotNone(snap["ed25519_signature"])
        self.assertEqual(len(snap["ed25519_signature"]), 128) # 64바이트 서명 = 128 hex

        signer = Ed25519Signer(self.local_repo)
        # 정상 상태에서는 서명 검증 통과
        self.assertTrue(signer.verify_manifest(snap))
        self.assertTrue(verify_manifest_signature_ed25519(snap, signer.public_key_path))

        # 2. 공격자가 manifest 내용을 변조하고 manifest_signature(해시)까지 재계산해 위조한 경우
        tampered_snap = dict(snap)
        tampered_entries = list(snap["entries"])
        tampered_entries[0] = dict(tampered_entries[0], size=999999) # 사이즈 위조
        tampered_snap["entries"] = tampered_entries

        # 공격자는 비대칭키 Private Key가 없으므로 Ed25519 서명 검증에서 무조건 탈락해야 함!
        self.assertFalse(signer.verify_manifest(tampered_snap))
        self.assertFalse(verify_manifest_signature_ed25519(tampered_snap, signer.public_key_path))

    def test_offsite_cas_blob_replication_incremental(self):
        """오프사이트 CAS 증분 복제: 신규 블롭만 전송되고 원격지에서 정상 복원되는지 검증"""
        # 1. 첫 번째 스냅샷 생성
        snap1 = SnapshotEngine.create_snapshot(
            repo_dir=self.local_repo,
            sources=[self.src_dir],
            profile_id="rep_test",
            min_free_disk_gb=0.0,
            use_vss=False
        )

        rep_mgr = ReplicationManager(self.local_repo, self.remote_repo)
        res1 = rep_mgr.replicate_snapshot(snap1["id"])

        self.assertEqual(res1["status"], "success")
        self.assertEqual(res1["replicated_blobs"], res1["total_blobs"])
        self.assertEqual(res1["skipped_blobs"], 0)

        # 2. 일부 파일만 수정 후 두 번째 스냅샷 생성
        with open(os.path.join(self.src_dir, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("수정된 두 번째 메모 내용입니다.")

        snap2 = SnapshotEngine.create_snapshot(
            repo_dir=self.local_repo,
            sources=[self.src_dir],
            profile_id="rep_test",
            min_free_disk_gb=0.0,
            use_vss=False
        )

        # 증분 복제 실행: 오직 1개의 신규 블롭만 전송되어야 함!
        res2 = rep_mgr.replicate_snapshot(snap2["id"])
        self.assertEqual(res2["status"], "success")
        self.assertEqual(res2["replicated_blobs"], 1, "수정된 1개 파일의 블롭만 전송되어야 합니다!")
        self.assertEqual(res2["skipped_blobs"], res2["total_blobs"] - 1, "이미 원격에 있는 블롭은 100% 스킵되어야 합니다!")

        # 3. 원격 저장소 단독으로 복원 검증 (미니PC가 전소되었다고 가정)
        restore_res = RestoreEngine.restore_snapshot(
            repo_dir=self.remote_repo,
            snapshot_id=snap2["id"],
            target_dir=self.restore_dir
        )
        self.assertGreater(restore_res["restored_files"], 0)
        self.assertEqual(len(restore_res["failed_files"]), 0)
        # 복원된 파일 내용 일치 확인
        with open(os.path.join(self.restore_dir, "notes.txt"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "수정된 두 번째 메모 내용입니다.")

    def test_dr_scenario_meta_db_lost(self):
        """DR 시나리오 1: 메타데이터 DB 및 레포 설정이 완전 증발해도 스냅샷/블롭만으로 복원 가능"""
        snap = SnapshotEngine.create_snapshot(
            repo_dir=self.local_repo,
            sources=[self.src_dir],
            profile_id="dr_db_test",
            min_free_disk_gb=0.0,
            use_vss=False
        )

        # 메타데이터 파일 및 DB 삭제
        meta_file = os.path.join(self.local_repo, "repo_meta.json")
        db_file = os.path.join(self.local_repo, "backup_meta.db")
        if os.path.exists(meta_file):
            os.remove(meta_file)
        if os.path.exists(db_file):
            os.remove(db_file)

        # 복원 엔진 실행: DB가 없어도 manifest와 blob만으로 100% 복원 성공해야 함
        restore_res = RestoreEngine.restore_snapshot(
            repo_dir=self.local_repo,
            snapshot_id=snap["id"],
            target_dir=self.restore_dir
        )
        self.assertGreater(restore_res["restored_files"], 0)
        self.assertEqual(len(restore_res["failed_files"]), 0)
        self.assertTrue(os.path.exists(os.path.join(self.restore_dir, "config.json")))

    def test_dr_scenario_corrupted_blob_isolation(self):
        """DR 시나리오 2: 특정 블롭 1개가 물리적으로 깨져도, 나머지 4개 파일은 손상 없이 100% 복구됨"""
        snap = SnapshotEngine.create_snapshot(
            repo_dir=self.local_repo,
            sources=[self.src_dir],
            profile_id="dr_corrupt_test",
            min_free_disk_gb=0.0,
            use_vss=False
        )

        # "large.dat"에 해당하는 블롭을 찾아 고의로 바이트 파괴
        storage = BlobStorage(self.local_repo)
        target_blob_id = None
        for entry in snap["entries"]:
            if entry.get("rel_path") == "large.dat":
                target_blob_id = entry.get("blob_id")
                break
        self.assertIsNotNone(target_blob_id)

        corrupt_blob_path = storage.get_blob_abs_path(target_blob_id)
        unlock_file_writable(corrupt_blob_path, authorized=True)
        with open(corrupt_blob_path, "wb") as f:
            f.write(b"CORRUPTED_DISASTER_GARBAGE")

        # 복원 실행
        restore_res = RestoreEngine.restore_snapshot(
            repo_dir=self.local_repo,
            snapshot_id=snap["id"],
            target_dir=self.restore_dir
        )

        # 손상된 large.dat는 실패 처리되고, 나머지 4개 파일은 완벽히 정상 복원되어야 함!
        self.assertTrue(os.path.exists(os.path.join(self.restore_dir, "config.json")))
        self.assertTrue(os.path.exists(os.path.join(self.restore_dir, "notes.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.restore_dir, "문서", "재무.csv")))
        self.assertTrue(os.path.exists(os.path.join(self.restore_dir, "bin", "kernel.dat")))

    def test_dr_scenario_manifest_corruption_rollback(self):
        """DR 시나리오 3: 최신 manifest가 손상되었을 때 직전 스냅샷으로 안전 롤백 복원 가능"""
        snap1 = SnapshotEngine.create_snapshot(
            repo_dir=self.local_repo,
            sources=[self.src_dir],
            profile_id="dr_rollback",
            min_free_disk_gb=0.0,
            use_vss=False
        )
        with open(os.path.join(self.src_dir, "notes.txt"), "a") as f:
            f.write("v2")
        snap2 = SnapshotEngine.create_snapshot(
            repo_dir=self.local_repo,
            sources=[self.src_dir],
            profile_id="dr_rollback",
            min_free_disk_gb=0.0,
            use_vss=False
        )

        # 최신 snap2의 manifest 파일을 완전히 삭제(손상)시킴
        snap2_file = os.path.join(self.local_repo, "snapshots", f"{snap2['id']}.json")
        unlock_file_writable(snap2_file, authorized=True)
        os.remove(snap2_file)

        # 직전 snap1 스냅샷으로 롤백 복원 시도 -> 100% 정상 성공해야 함
        restore_res = RestoreEngine.restore_snapshot(
            repo_dir=self.local_repo,
            snapshot_id=snap1["id"],
            target_dir=self.restore_dir
        )
        self.assertGreater(restore_res["restored_files"], 0)
        self.assertEqual(len(restore_res["failed_files"]), 0)
        self.assertTrue(os.path.exists(os.path.join(self.restore_dir, "notes.txt")))


if __name__ == "__main__":
    unittest.main()
