# -*- coding: utf-8 -*-
"""
tests/test_diagnostic_bundle.py

원클릭 진단 로그 패키징 (ZIP) 및 진단 개요 API 단위 테스트
"""

import io
import json
import zipfile
import unittest

from fastapi.testclient import TestClient

from core.diagnostic import (
    sanitize_data,
    collect_system_metadata,
    collect_snapshots_summary,
    collect_recent_logs,
    generate_diagnostic_summary_text,
    build_diagnostic_package,
    get_diagnostic_quick_overview,
)
from web.app import app


class TestDiagnosticDataSanitization(unittest.TestCase):
    """민감 정보 마스킹 및 개인정보 보호 로직 검증"""

    def test_sanitize_sensitive_keys(self):
        dirty = {
            "normal_key": "safe_value",
            "db_password": "super_secret_password",
            "api_token": "token_xyz123",
            "private_key": "dummy_secret_key_value",
            "auth_credential": {"secret": "inner_secret", "username": "admin"},
            "items": [
                {"cookie": "session_id_abc"},
                {"name": "valid_item"}
            ]
        }

        cleaned = sanitize_data(dirty)
        self.assertEqual(cleaned["normal_key"], "safe_value")
        self.assertEqual(cleaned["db_password"], "***MASKED***")
        self.assertEqual(cleaned["api_token"], "***MASKED***")
        self.assertEqual(cleaned["private_key"], "***MASKED***")
        self.assertEqual(cleaned["auth_credential"], "***MASKED***")
        self.assertEqual(cleaned["items"][0]["cookie"], "***MASKED***")
        self.assertEqual(cleaned["items"][1]["name"], "valid_item")


class TestDiagnosticCollectors(unittest.TestCase):
    """시스템 메타데이터, 스냅샷 요약 및 로그 수집 검증"""

    def test_collect_system_metadata(self):
        meta = collect_system_metadata()
        self.assertIn("report_generated_at", meta)
        self.assertIn("software_version", meta)
        self.assertIn("operating_system", meta)
        self.assertIn("hardware", meta)
        self.assertIn("python", meta)
        self.assertGreater(meta["hardware"]["cpu_cores_logical"], 0)
        self.assertGreater(meta["hardware"]["memory_total_gb"], 0)

    def test_collect_snapshots_summary(self):
        summary = collect_snapshots_summary()
        self.assertIn("total_snapshots", summary)
        self.assertIn("repositories", summary)
        self.assertIsInstance(summary["total_snapshots"], int)

    def test_collect_recent_logs(self):
        logs = collect_recent_logs(max_tail_bytes=1024)
        self.assertIsInstance(logs, dict)
        self.assertIn("logs/in_memory_recent_tasks.log", logs)


class TestDiagnosticReportGeneration(unittest.TestCase):
    """텍스트 요약 보고서 및 ZIP 패키지 생성 검증"""

    def test_generate_diagnostic_summary_text(self):
        sys_meta = collect_system_metadata()
        storage_health = {"status": "healthy", "disks": [], "volumes": []}
        snapshots_summary = {"total_snapshots": 3, "repositories": [], "latest_snapshot": None}
        profiles = [{"name": "테스트 프로필", "id": "p1", "sources": ["C:\\test"]}]

        text = generate_diagnostic_summary_text(sys_meta, storage_health, snapshots_summary, profiles)
        self.assertIn("KOVA VaultOS", text)
        self.assertIn("[1. 시스템 및 하드웨어 제원]", text)
        self.assertIn("[2. 스토리지 볼륨 및 물리 디스크 상태]", text)
        self.assertIn("[3. 백업 저장소 및 스냅샷 이력]", text)
        self.assertIn("[4. 자동 스케줄러 및 놓친 백업 정책]", text)
        self.assertIn("[5. 구성 프로필", text)

    def test_build_diagnostic_package(self):
        zip_bytes = build_diagnostic_package()
        self.assertTrue(len(zip_bytes) > 0)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(zip_bytes)))

        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            names = zf.namelist()
            self.assertIn("DIAGNOSTIC_SUMMARY.txt", names)
            self.assertIn("system_info.json", names)
            self.assertIn("storage_health.json", names)
            self.assertIn("snapshots_summary.json", names)
            self.assertIn("config_sanitized.json", names)

            # JSON 파일 파싱 유효성 확인
            sys_info = json.loads(zf.read("system_info.json").decode("utf-8"))
            self.assertIn("software_version", sys_info)

            config = json.loads(zf.read("config_sanitized.json").decode("utf-8"))
            self.assertIn("profiles", config)

    def test_get_diagnostic_quick_overview(self):
        overview = get_diagnostic_quick_overview()
        self.assertTrue(overview["success"])
        self.assertIn("summary_text", overview)
        self.assertIn("software_version", overview)


class TestDiagnosticAPIEndpoints(unittest.TestCase):
    """FastAPI 진단 엔드포인트 계약 감사"""

    def setUp(self):
        self.client = TestClient(app)

    def test_get_diagnostic_overview_endpoint(self):
        resp = self.client.get("/api/system/diagnostic-overview")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("success"))
        self.assertIn("summary_text", data)

    def test_download_diagnostic_bundle_endpoint(self):
        resp = self.client.get("/api/system/diagnostic-bundle")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("content-type"), "application/zip")
        self.assertIn("attachment; filename=", resp.headers.get("content-disposition", ""))
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(resp.content)))


if __name__ == "__main__":
    unittest.main()
