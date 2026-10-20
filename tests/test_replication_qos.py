# -*- coding: utf-8 -*-
"""
tests/test_replication_qos.py - QoS 대역폭 제한 및 진행률 콜백 테스트
TokenBucketLimiter와 ReplicationManager의 핵심 동작을 검증합니다.
"""

import os
import json
import time
import tempfile
import unittest
from typing import Any, Dict, List

from core.replication import TokenBucketLimiter, ReplicationManager


class TestTokenBucketLimiter(unittest.TestCase):
    """TokenBucketLimiter의 QoS 대역폭 제한 동작 테스트."""

    def test_token_bucket_no_limit(self):
        """bandwidth=0일 때 consume()이 즉시 반환되어 지연이 없음을 검증."""
        limiter = TokenBucketLimiter(max_mb_per_sec=0.0)
        self.assertEqual(limiter.rate_bytes, 0)
        
        start = time.monotonic()
        limiter.consume(1024 * 1024 * 50)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.1, f"No-limit mode should be instantaneous, but took {elapsed:.4f}s")

    def test_token_bucket_throttling(self):
        """bandwidth=2MB/s일 때 토큰 고갈 후 2MB 소비에 최소 0.5초 이상 걸림을 검증."""
        bandwidth_mb = 2.0
        limiter = TokenBucketLimiter(max_mb_per_sec=bandwidth_mb)
        
        # 1. 초기 버킷(capacity=4MB)을 소진시킴
        limiter.consume(4 * 1024 * 1024)
        
        # 2. 고갈 상태에서 추가 2MB consume -> 약 1.0초 슬립 예상
        start = time.monotonic()
        limiter.consume(2 * 1024 * 1024)
        elapsed = time.monotonic() - start
        
        self.assertGreaterEqual(elapsed, 0.6, 
                                f"Throttling should take >= 0.6s for 2MB at 2MB/s, but took {elapsed:.4f}s")
        self.assertLess(elapsed, 2.5, 
                        f"Throttling took excessively long: {elapsed:.4f}s")


class TestReplicationManagerQoS(unittest.TestCase):
    """ReplicationManager의 QoS 및 진행률 콜백 동작 테스트."""

    def setUp(self):
        """테스트용 임시 디렉토리 구조 생성."""
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.local_repo = os.path.join(self.tmp_dir.name, "local")
        self.remote_repo = os.path.join(self.tmp_dir.name, "remote")
        
        self.local_blobs = os.path.join(self.local_repo, "blobs")
        self.local_snapshots = os.path.join(self.local_repo, "snapshots")
        os.makedirs(self.local_blobs, exist_ok=True)
        os.makedirs(self.local_snapshots, exist_ok=True)
        
        self.remote_blobs = os.path.join(self.remote_repo, "blobs")
        self.remote_snapshots = os.path.join(self.remote_repo, "snapshots")

    def tearDown(self):
        """임시 디렉토리 정리."""
        try:
            # WORM 읽기 전용 해제 후 정리
            for root, dirs, files in os.walk(self.tmp_dir.name):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        os.chmod(fp, 0o777)
                    except OSError:
                        pass
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def _create_test_blob(self, blob_id: str, content: bytes) -> None:
        prefix = blob_id[:2]
        blob_dir = os.path.join(self.local_blobs, prefix)
        os.makedirs(blob_dir, exist_ok=True)
        blob_path = os.path.join(blob_dir, f"{blob_id}.blob")
        with open(blob_path, 'wb') as f:
            f.write(content)

    def _create_test_snapshot(self, snapshot_id: str, blob_ids: List[str]) -> None:
        manifest = {
            "snapshot_id": snapshot_id,
            "entries": [
                {"blob_id": bid} for bid in blob_ids
            ]
        }
        manifest_path = os.path.join(self.local_snapshots, f"{snapshot_id}.json")
        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump(manifest, f)

    def test_replication_with_progress_callback(self):
        """대역폭 제한 하에서 스냅샷 복제 시 진행률 콜백 및 결과 검증."""
        snapshot_id = "test_snap_qos_001"
        blob_ids = []
        total_content_size = 0
        
        # 4개의 블롭 생성 (각 256KB, 총 1MB)
        num_blobs = 4
        blob_size = 256 * 1024
        for i in range(num_blobs):
            blob_id = f"ab{i:06x}" + "0" * (64 - len(f"ab{i:06x}"))
            content = os.urandom(blob_size)
            self._create_test_blob(blob_id, content)
            blob_ids.append(blob_id)
            total_content_size += blob_size
        
        self._create_test_snapshot(snapshot_id, blob_ids)
        
        progress_calls = []
        def mock_progress(info: Dict[str, Any]):
            progress_calls.append(dict(info))
        
        # 대역폭 4MB/s 설정
        manager = ReplicationManager(
            local_repo_dir=self.local_repo,
            remote_repo_dir=self.remote_repo,
            bandwidth_limit_mb=4.0
        )
        
        result = manager.replicate_snapshot(
            snapshot_id=snapshot_id,
            max_workers=2,
            progress_callback=mock_progress
        )
        
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["snapshot_id"], snapshot_id)
        self.assertEqual(result["total_blobs"], num_blobs)
        self.assertEqual(result["replicated_blobs"], num_blobs)
        self.assertEqual(result["skipped_blobs"], 0)
        self.assertEqual(result["transferred_bytes"], total_content_size)
        
        # 진행률 콜백 호출 확인
        self.assertGreater(len(progress_calls), 0)
        last_call = progress_calls[-1]
        self.assertIn("percent", last_call)
        self.assertIn("speed_mb_s", last_call)
        self.assertIn("eta_seconds", last_call)
        self.assertGreaterEqual(last_call["percent"], 99.0)
        
        # 원격지 파일 존재 확인
        for blob_id in blob_ids:
            prefix = blob_id[:2]
            remote_blob_path = os.path.join(self.remote_blobs, prefix, f"{blob_id}.blob")
            self.assertTrue(os.path.exists(remote_blob_path))


if __name__ == '__main__':
    unittest.main()
