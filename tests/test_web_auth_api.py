"""
tests/test_web_auth_api.py
FastAPI 웹 대시보드 인증 미들웨어 및 /api/auth/* 엔드포인트 통합 테스트
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch
from starlette.testclient import TestClient

from core import auth
from web.app import app


class TestWebAuthApi(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.auth_file = os.path.join(self.temp_dir, "auth_config.json")
        self.patcher = patch("core.auth.AUTH_CONFIG_FILE", self.auth_file)
        self.patcher.start()
        with auth._lock:
            auth._sessions.clear()
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_status_unconfigured(self):
        resp = self.client.get("/api/auth/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertFalse(data["data"]["configured"])

    def test_setup_and_login_flow(self):
        # 1. 암호 설정 (바이패스 비활성화)
        setup_resp = self.client.post("/api/auth/setup", json={
            "password": "masterpassword123",
            "allow_localhost_bypass": False
        })
        self.assertEqual(setup_resp.status_code, 200)
        self.assertTrue(setup_resp.json()["success"])

        # 2. 미인증 상태로 보호된 API 호출 -> 401
        protected_resp = self.client.get("/api/system-info")
        self.assertEqual(protected_resp.status_code, 401)
        self.assertTrue(protected_resp.json().get("auth_required"))

        # 3. 잘못된 비밀번호 로그인 -> 401
        bad_login = self.client.post("/api/auth/login", json={"password": "wrongpassword"})
        self.assertEqual(bad_login.status_code, 401)

        # 4. 올바른 비밀번호 로그인 -> 200 및 쿠키 수령
        login_resp = self.client.post("/api/auth/login", json={"password": "masterpassword123"})
        self.assertEqual(login_resp.status_code, 200)
        self.assertTrue(login_resp.json()["success"])
        self.assertIn("backup_session", login_resp.cookies)

        # 5. 쿠키를 담아 보호된 API 호출 -> 200 성공
        ok_resp = self.client.get("/api/system-info", cookies=login_resp.cookies)
        self.assertEqual(ok_resp.status_code, 200)

        # 6. 로그아웃 수행 -> 세션 만료
        logout_resp = self.client.post("/api/auth/logout", cookies=login_resp.cookies)
        self.assertEqual(logout_resp.status_code, 200)

        # 7. 로그아웃 후 다시 API 호출 -> 401
        re_resp = self.client.get("/api/system-info", cookies=login_resp.cookies)
        self.assertEqual(re_resp.status_code, 401)

    def test_localhost_bypass(self):
        # 바이패스 허용 상태로 설정
        self.client.post("/api/auth/setup", json={
            "password": "masterpassword123",
            "allow_localhost_bypass": True
        })

        # 로컬 루프백 접속자는 세션 쿠키 없이도 통과
        resp = self.client.get("/api/system-info")
        self.assertEqual(resp.status_code, 200)

        # 바이패스 비활성화 토글
        self.client.post("/api/auth/toggle-bypass", json={"enabled": False})

        # 비활성화 후에는 401
        resp2 = self.client.get("/api/system-info")
        self.assertEqual(resp2.status_code, 401)


if __name__ == "__main__":
    unittest.main()
