# -*- coding: utf-8 -*-
"""
tests/test_p0_fail_closed.py: P0-1 Fail-Closed 디스크 보호 정책 검증 단위 테스트
- 최소 여유 디스크 공간 미달 시 즉각 백업 거부 (InsufficientDiskSpaceError) 확인
- 과거의 위험했던 Fail-Open 스냅샷 강제 삭제 로직이 완전히 차단되어 기존 스냅샷이 100% 보존되는지 검증
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from core.storage import (
    BlobStorage, InsufficientDiskSpaceError, verify_disk_space_or_fail
)
from core.snapshot import SnapshotEngine
from core.retention import RetentionManager


class TestFailClosedDiskPolicy(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="backup_test_failclosed_")
        self.repo_dir = os.path.join(self.test_dir, "repo")
        self.src_dir = os.path.join(self.test_dir, "src")
        os.makedirs(self.repo_dir, exist_ok=True)
        os.makedirs(self.src_dir, exist_ok=True)

        # 소스 파일 생성
        self.sample_file = os.path.join(self.src_dir, "document.txt")
        with open(self.sample_file, "w", encoding="utf-8") as f:
            f.write("Important financial data that must not be deleted!")

    def tearDown(self):
        # 읽기 전용 해제 후 정리
        for root, dirs, files in os.walk(self.test_dir):
            for f in files:
                try:
                    os.chmod(os.path.join(root, f), 0o777)
                except Exception:
                    pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_verify_disk_space_or_fail_insufficient(self):
        """여유 공간이 부족할 때 InsufficientDiskSpaceError 발생 확인"""
        with patch("shutil.disk_usage", return_value=(100*1024**3, 95*1024**3, 5*1024**3)):
            # 5GB 여유, 10GB 요구 -> 에러 발생해야 함
            with self.assertRaises(InsufficientDiskSpaceError) as ctx:
                verify_disk_space_or_fail(self.repo_dir, min_free_gb=10.0)
            self.assertIn("저장소 여유 공간 부족", str(ctx.exception))

    def test_verify_disk_space_or_fail_sufficient(self):
        """여유 공간이 충분할 때 정상 통과 확인"""
        with patch("shutil.disk_usage", return_value=(100*1024**3, 50*1024**3, 50*1024**3)):
            # 50GB 여유, 10GB 요구 -> 정상 통과
            free_gb = verify_disk_space_or_fail(self.repo_dir, min_free_gb=10.0)
            self.assertGreaterEqual(free_gb, 10.0)

    def test_create_snapshot_fail_closed_preserves_existing_snapshots(self):
        """
        [가장 중요한 P0 테스트]
        기존 스냅샷들이 존재하는 상태에서 새 백업 실행 시 디스크 부족이 감지되면,
        기존 스냅샷을 단 하나도 삭제하지 않고 즉시 예외를 발생시키며 중단(Fail-Closed)되는지 확인.
        """
        # 1. 첫 번째 정상 백업 생성
        snap1 = SnapshotEngine.create_snapshot(
            repo_dir=self.repo_dir,
            sources=[self.src_dir],
            profile_id="test_prof",
            profile_name="Test Profile",
            min_free_disk_gb=0.0, # 첫 번째는 통과
            use_vss=False
        )
        self.assertIsNotNone(snap1.get("id"))

        # 2. 두 번째 정상 백업 생성
        with open(self.sample_file, "a", encoding="utf-8") as f:
            f.write("\nUpdated record #2")

        snap2 = SnapshotEngine.create_snapshot(
            repo_dir=self.repo_dir,
            sources=[self.src_dir],
            profile_id="test_prof",
            profile_name="Test Profile",
            min_free_disk_gb=0.0,
            use_vss=False
        )
        self.assertIsNotNone(snap2.get("id"))

        # 현재 2개의 스냅샷이 저장소에 존재
        before_snaps = SnapshotEngine.list_snapshots(self.repo_dir)
        self.assertEqual(len(before_snaps), 2)
        snap_ids_before = {s["id"] for s in before_snaps}

        # 3. 디스크 잔여 공간이 2GB로 극히 부족한 상황을 모의(Mock)
        with patch("shutil.disk_usage", return_value=(100*1024**3, 98*1024**3, 2*1024**3)):
            # min_free_disk_gb=10.0 요구 시 백업 시작 전 Fail-Closed로 차단되어야 함
            with self.assertRaises(InsufficientDiskSpaceError):
                SnapshotEngine.create_snapshot(
                    repo_dir=self.repo_dir,
                    sources=[self.src_dir],
                    profile_id="test_prof",
                    profile_name="Test Profile",
                    min_free_disk_gb=10.0,
                    use_vss=False
                )

        # 4. 검증: 기존 2개의 스냅샷이 단 하나도 삭제되지 않고 온전히 100% 보존되었는가?
        after_snaps = SnapshotEngine.list_snapshots(self.repo_dir)
        self.assertEqual(len(after_snaps), 2, "Fail-Closed 정책에 따라 기존 스냅샷이 절대 삭제되어서는 안 됩니다!")
        snap_ids_after = {s["id"] for s in after_snaps}
        self.assertEqual(snap_ids_before, snap_ids_after)

    def test_retention_manager_does_not_prune_due_to_disk_space(self):
        """보존 관리자(RetentionManager)가 디스크 공간 부족으로 임의 삭제를 실행하지 않는지 검증"""
        # 2개의 스냅샷 생성
        SnapshotEngine.create_snapshot(self.repo_dir, [self.src_dir], profile_id="p1", min_free_disk_gb=0.0, use_vss=False)
        with open(self.sample_file, "a") as f:
            f.write("mod")
        SnapshotEngine.create_snapshot(self.repo_dir, [self.src_dir], profile_id="p1", min_free_disk_gb=0.0, use_vss=False)

        mgr = RetentionManager(self.repo_dir)
        # min_free_gb=100.0을 주더라도 스냅샷을 임의 삭제하지 않음
        with patch("shutil.disk_usage", return_value=(100*1024**3, 99*1024**3, 1*1024**3)):
            result = mgr.apply_policy(retention_count=30, retention_days=60, min_free_gb=100.0)
            self.assertEqual(len(result["deleted_snapshots"]), 0)

        remaining = SnapshotEngine.list_snapshots(self.repo_dir)
        self.assertEqual(len(remaining), 2)


if __name__ == "__main__":
    unittest.main()
