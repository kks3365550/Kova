# -*- coding: utf-8 -*-
"""
tests/test_drill_and_estimator.py

원클릭 복구 모의훈련 (1-Sec DR Drill) 및 사전 용량 예측기 (Pre-flight Estimator) 단위 테스트
"""

import os
import tempfile
import pytest
from fastapi.testclient import TestClient

from web.app import app
from core.snapshot import SnapshotEngine
from core.storage import BlobStorage


@pytest.fixture
def client():
    return TestClient(app)


def test_estimate_backup_size_basic(client):
    """실제 임시 폴더를 생성하여 용량 예측 API가 정상 계산하는지 검증"""
    with tempfile.TemporaryDirectory() as tmp_src, tempfile.TemporaryDirectory() as tmp_repo:
        # 1MB 파일 2개 생성
        for i in range(2):
            fp = os.path.join(tmp_src, f"test_{i}.bin")
            with open(fp, "wb") as f:
                f.write(b"X" * (1024 * 1024))

        payload = {
            "sources": [tmp_src],
            "repo_dir": tmp_repo
        }
        res = client.post("/api/backup/estimate", json=payload)
        assert res.status_code == 200

        data = res.json()
        assert data["scanned_files"] == 2
        assert data["total_bytes"] == 2 * 1024 * 1024
        assert data["total_mb"] == 2.0
        assert data["repo_free_gb"] >= 0
        assert "is_sufficient" in data
        assert "recommendation" in data


def test_estimate_backup_size_empty(client):
    """빈 폴더 스캔 시 0바이트, 0개 파일 반환 검증"""
    with tempfile.TemporaryDirectory() as tmp_src, tempfile.TemporaryDirectory() as tmp_repo:
        payload = {
            "sources": [tmp_src],
            "repo_dir": tmp_repo
        }
        res = client.post("/api/backup/estimate", json=payload)
        assert res.status_code == 200

        data = res.json()
        assert data["scanned_files"] == 0
        assert data["total_bytes"] == 0
        assert data["total_mb"] == 0.0


def test_dr_drill_with_snapshot(client):
    """실제 스냅샷을 생성한 후 1초 복구 모의훈련을 수행하여 무결성 인증서가 발급되는지 검증"""
    with tempfile.TemporaryDirectory() as tmp_src, tempfile.TemporaryDirectory() as tmp_repo:
        # 파일 3개 생성
        for i in range(3):
            fp = os.path.join(tmp_src, f"drill_sample_{i}.txt")
            with open(fp, "w", encoding="utf-8") as f:
                f.write(f"Sample content for drill test {i}")

        # 스냅샷 생성
        manifest = SnapshotEngine.create_snapshot(
            repo_dir=tmp_repo,
            sources=[tmp_src],
            profile_id="prof_drill_test",
            profile_name="모의훈련 테스트 프로필",
            compress_level=1
        )
        snap_id = manifest["id"]

        # 복구 모의훈련 호출
        payload = {
            "snapshot_id": snap_id,
            "repo_dir": tmp_repo,
            "sample_count": 3
        }
        res = client.post("/api/backup/drill", json=payload)
        assert res.status_code == 200

        data = res.json()
        assert data["success"] is True
        assert data["snapshot_id"] == snap_id
        assert data["tested_count"] == 3
        assert data["duration_ms"] >= 0
        assert data["certificate_id"].startswith("DR-CERT-")
        assert len(data["tested_files"]) == 3
        for tf in data["tested_files"]:
            assert tf["verified"] is True
            assert tf["size_bytes"] > 0
            assert tf["sha256"] != "N/A"


def test_dr_drill_no_snapshot(client):
    """존재하지 않는 스냅샷에 대해 모의훈련 시 404 에러 반환 검증"""
    with tempfile.TemporaryDirectory() as tmp_repo:
        payload = {
            "snapshot_id": "non_existent_snapshot_id",
            "repo_dir": tmp_repo
        }
        res = client.post("/api/backup/drill", json=payload)
        assert res.status_code == 404
