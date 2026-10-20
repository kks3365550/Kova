# -*- coding: utf-8 -*-
"""
tests/test_offsite_feature.py
3-2-1 오프사이트 원격 복제 기능 및 대시보드 API 엔드포인트 단위 테스트
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch
from starlette.testclient import TestClient

from core.config import ConfigManager, DEFAULT_SETTINGS
from web.app import app


class TestOffsiteFeature(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.settings_file = os.path.join(self.temp_dir, "app_settings.json")
        self.patcher_settings = patch("core.config.SETTINGS_FILE", self.settings_file)
        self.patcher_settings.start()

        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        self.patcher_settings.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_config_manager_get_save_offsite(self):
        """ConfigManager를 통한 오프사이트 설정 읽기/쓰기 검증"""
        cfg = ConfigManager.get_offsite_config()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["remote_path"], "")
        self.assertTrue(cfg["auto_sync"])

        # 저장
        updated = ConfigManager.save_offsite_config({
            "enabled": True,
            "remote_path": "\\\\100.90.20.59\\BackupShare",
            "auto_sync": True
        })
        self.assertTrue(updated["enabled"])
        self.assertEqual(updated["remote_path"], "\\\\100.90.20.59\\BackupShare")
        self.assertTrue(updated["auto_sync"])

        # 다시 읽었을 때도 보존되는지 확인
        cfg2 = ConfigManager.get_offsite_config()
        self.assertTrue(cfg2["enabled"])
        self.assertEqual(cfg2["remote_path"], "\\\\100.90.20.59\\BackupShare")

    def test_api_get_and_post_offsite(self):
        """GET /api/offsite 및 POST /api/offsite 엔드포인트 동작 검증"""
        # 1. 초기 조회
        res = self.client.get("/api/offsite")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertFalse(data["enabled"])

        # 2. 설정 저장
        target_dir = os.path.join(self.temp_dir, "remote_target")
        os.makedirs(target_dir, exist_ok=True)
        save_res = self.client.post("/api/offsite", json={
            "enabled": True,
            "remote_path": target_dir,
            "auto_sync": True
        })
        self.assertEqual(save_res.status_code, 200)
        save_data = save_res.json()
        self.assertTrue(save_data["success"])

        # 3. 변경 후 재조회 및 status reachable 확인
        res2 = self.client.get("/api/offsite")
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        self.assertTrue(data2["enabled"])
        self.assertEqual(data2["remote_path"], target_dir)
        self.assertTrue(data2["status"]["reachable"])
        self.assertGreater(data2["status"]["total_gb"], 0)

    def test_api_test_offsite_connection(self):
        """POST /api/offsite/test 연결 테스트 검증"""
        # 정상 존재하는 경로
        test_dir = os.path.join(self.temp_dir, "test_share")
        os.makedirs(test_dir, exist_ok=True)
        res = self.client.post("/api/offsite/test", json={"remote_path": test_dir})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertTrue(data["status"]["reachable"])

        # 빈 경로 테스트
        res_empty = self.client.post("/api/offsite/test", json={"remote_path": ""})
        self.assertEqual(res_empty.status_code, 200)
        self.assertFalse(res_empty.json()["success"])

    def test_api_trigger_offsite_sync(self):
        """POST /api/offsite/sync 수동 복제 트리거 검증"""
        local_repo = os.path.join(self.temp_dir, "local_repo")
        os.makedirs(os.path.join(local_repo, "snapshots"), exist_ok=True)
        remote_repo = os.path.join(self.temp_dir, "remote_repo")
        os.makedirs(remote_repo, exist_ok=True)

        with patch("core.offsite.OffsiteReplicator.replicate", return_value={"success": True, "files_copied": 5, "files_total": 5, "duration_sec": 0.5}):
            res = self.client.post("/api/offsite/sync", json={
                "remote_path": remote_repo,
                "source_repo_dir": local_repo
            })
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data["success"])
            self.assertEqual(data["source_repo"], local_repo)
            self.assertEqual(data["remote_path"], remote_repo)


if __name__ == "__main__":
    unittest.main()
