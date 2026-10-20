# -*- coding: utf-8 -*-
"""
tests/test_sprint1_durable_queue.py
Sprint 1 전수 검증 유닛 테스트 스위트:
1. Durable Replication Queue 생명주기 (PENDING -> TRANSFERRING -> VERIFIED -> COMMITTED)
2. 비정상 종료(Crash/Power-off) 모의 및 auto-resume 재개 멱등성 검증
3. WORM Prune 권한 분리: 인가되지 않은(authorized=False) 스냅샷 삭제/Prune 차단 및 관리자 승인 시 정상 처리 검증
4. LOCAL vs OFFSITE 2단계 보호 상태 분리 및 조회 검증
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.replication_queue import ReplicationQueueManager
from core.snapshot import SnapshotEngine
from core.storage import BlobStorage
from core.worm import WORMAuthorizationError, WORMManager


class TestSprint1DurableQueue(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="sprint1_test_")
        self.repo_dir = os.path.join(self.temp_dir, "local_repo")
        self.remote_repo_dir = os.path.join(self.temp_dir, "remote_repo")
        self.src_dir = os.path.join(self.temp_dir, "source")

        os.makedirs(self.repo_dir, exist_ok=True)
        os.makedirs(self.remote_repo_dir, exist_ok=True)
        os.makedirs(self.src_dir, exist_ok=True)

        self.worm_manager = WORMManager()

    def tearDown(self):
        try:
            self.worm_manager.unprotect_directory(self.temp_dir, authorized=True)
        except Exception:
            pass


        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_durable_queue_lifecycle(self):
        """1. Durable Replication Queue 전체 생명주기 및 원격지 COMMITTED 검증"""
        # Create test source
        src_file = os.path.join(self.src_dir, "file1.txt")
        with open(src_file, 'w', encoding='utf-8') as f:
            f.write("Payload for durable queue test")

        snap = SnapshotEngine.create_snapshot(
            repo_dir=self.repo_dir,
            sources=[self.src_dir],
            profile_id="p_durable",
            min_free_disk_gb=0.0,
            use_vss=False
        )

        rq = ReplicationQueueManager(self.repo_dir)
        task_id = rq.enqueue(snap["id"], self.repo_dir, self.remote_repo_dir)
        self.assertGreater(task_id, 0)

        status = rq.get_status(snap["id"])
        self.assertIsNotNone(status)
        self.assertEqual(status["state"], "PENDING")
        self.assertFalse(status["is_offsite_protected"])

        # Process the task
        processed = rq.process_next_task()
        self.assertTrue(processed)

        status_after = rq.get_status(snap["id"])
        self.assertEqual(status_after["state"], "COMMITTED")
        self.assertTrue(status_after["is_offsite_protected"])

        # Verify remote storage has the blob and snapshot manifest
        remote_storage = BlobStorage(self.remote_repo_dir)
        remote_blob = remote_storage.get_blob_abs_path(snap["entries"][0]["blob_id"])
        self.assertTrue(os.path.exists(remote_blob))

        remote_manifest = os.path.join(remote_storage.snapshots_dir, f"{snap['id']}.json")
        self.assertTrue(os.path.exists(remote_manifest))

        rq.close()

    def test_crash_and_auto_resume(self):
        """2. 전송 중 강제 중단(Crash) 모의 및 auto-resume 복구 멱등성 검증"""
        src_file = os.path.join(self.src_dir, "crash_target.bin")
        with open(src_file, 'wb') as f:
            f.write(b"Unique crash recovery test data " * 50)

        snap = SnapshotEngine.create_snapshot(
            repo_dir=self.repo_dir,
            sources=[self.src_dir],
            profile_id="p_crash",
            min_free_disk_gb=0.0,
            use_vss=False
        )

        rq = ReplicationQueueManager(self.repo_dir)
        rq.enqueue(snap["id"], self.repo_dir, self.remote_repo_dir)

        # Simulate crash during transfer: manually set state to TRANSFERRING
        with rq._lock:
            conn = rq._get_connection()
            conn.execute(
                "UPDATE replication_queue SET state='TRANSFERRING' WHERE snapshot_id=?",
                (snap["id"],)
            )
            conn.commit()

        # Check interrupted state
        interrupted_status = rq.get_status(snap["id"])
        self.assertEqual(interrupted_status["state"], "TRANSFERRING")
        self.assertFalse(interrupted_status["is_offsite_protected"])

        # Simulate system reboot / app restart
        resumed_count = rq.resume_all_interrupted()
        self.assertEqual(resumed_count, 1)

        resumed_status = rq.get_status(snap["id"])
        self.assertEqual(resumed_status["state"], "PENDING")

        # Process resumed task
        self.assertTrue(rq.process_next_task())

        committed_status = rq.get_status(snap["id"])
        self.assertEqual(committed_status["state"], "COMMITTED")
        self.assertTrue(committed_status["is_offsite_protected"])

        rq.close()

    def test_worm_prune_authorization_enforcement(self):
        """3. WORM Prune 권한 분리: authorized=False 차단 및 authorized=True 허용 검증"""
        src_file = os.path.join(self.src_dir, "worm_auth_test.txt")
        with open(src_file, 'w', encoding='utf-8') as f:
            f.write("WORM authorization enforcement data")

        snap = SnapshotEngine.create_snapshot(
            repo_dir=self.repo_dir,
            sources=[self.src_dir],
            profile_id="p_worm_auth",
            min_free_disk_gb=0.0,
            use_vss=False,
            worm_protect=True
        )

        storage = BlobStorage(self.repo_dir)
        snap_file = os.path.join(storage.snapshots_dir, f"{snap['id']}.json")
        self.assertTrue(os.path.exists(snap_file))

        # Attempt 1: Unauthorized deletion (authorized=False) must raise WORMAuthorizationError
        with self.assertRaises(WORMAuthorizationError):
            SnapshotEngine.delete_snapshot(self.repo_dir, snap["id"], authorized=False)

        # Verify file is still strictly protected and exists
        self.assertTrue(os.path.exists(snap_file))

        # Attempt 2: Unauthorized pruning (authorized=False) must raise WORMAuthorizationError
        with self.assertRaises(WORMAuthorizationError):
            SnapshotEngine.prune_snapshots(self.repo_dir, retention_count=1, authorized=False)

        self.assertTrue(os.path.exists(snap_file))

        # Attempt 3: Authorized deletion (authorized=True) succeeds
        deleted = SnapshotEngine.delete_snapshot(self.repo_dir, snap["id"], authorized=True)
        self.assertTrue(deleted)
        self.assertFalse(os.path.exists(snap_file))

    def test_end_to_end_snapshot_with_durable_queue_and_worker(self):
        """4. SnapshotEngine create_snapshot 연동 Durable Queue 자동 등록 및 비동기 복제 완료 검증"""
        src_file = os.path.join(self.src_dir, "e2e_durable.txt")
        with open(src_file, 'w', encoding='utf-8') as f:
            f.write("End-to-end durable replication automated worker test")

        snap = SnapshotEngine.create_snapshot(
            repo_dir=self.repo_dir,
            sources=[self.src_dir],
            profile_id="p_e2e",
            min_free_disk_gb=0.0,
            use_vss=False,
            offsite_repo_dir=self.remote_repo_dir,
            worm_protect=True
        )

        # Allow background worker to process queue item
        rq = ReplicationQueueManager(self.repo_dir)
        max_wait = 10.0
        start = time.time()
        committed = False
        while time.time() - start < max_wait:
            st = rq.get_status(snap["id"])
            if st and st["state"] == "COMMITTED":
                committed = True
                break
            time.sleep(0.2)

        self.assertTrue(committed, "Durable replication queue worker must commit task to COMMITTED state")

        status = rq.get_status(snap["id"])
        self.assertTrue(status["is_offsite_protected"])
        rq.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
