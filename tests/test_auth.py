"""
tests/test_auth.py
core/auth.py 모듈 단위 테스트
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch
from core import auth


class TestAuth(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.auth_file = os.path.join(self.temp_dir, "auth_config.json")
        self.patcher = patch("core.auth.AUTH_CONFIG_FILE", self.auth_file)
        self.patcher.start()
        with auth._lock:
            auth._sessions.clear()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_initial_state_unconfigured(self):
        self.assertFalse(auth.is_auth_configured())
        status = auth.get_auth_status(client_ip="127.0.0.1")
        self.assertFalse(status["configured"])
        self.assertTrue(status["is_localhost"])

    def test_setup_master_password(self):
        res = auth.setup_master_password("admin1234", allow_localhost_bypass=True)
        self.assertTrue(res)
        self.assertTrue(auth.is_auth_configured())
        self.assertTrue(auth.verify_master_password("admin1234"))
        self.assertFalse(auth.verify_master_password("wrongpassword"))

        # 중복 설정 시 예외 발생
        with self.assertRaises(ValueError):
            auth.setup_master_password("another1234")

    def test_change_master_password(self):
        auth.setup_master_password("initial1234")
        token = auth.create_session()
        self.assertTrue(auth.validate_session(token))

        # 잘못된 이전 비밀번호
        with self.assertRaises(ValueError):
            auth.change_master_password("wrong", "newpass1234")

        # 올바른 비밀번호 변경
        res = auth.change_master_password("initial1234", "newpass1234")
        self.assertTrue(res)
        self.assertTrue(auth.verify_master_password("newpass1234"))
        self.assertFalse(auth.verify_master_password("initial1234"))

        # 비밀번호 변경 후 기존 세션 무효화 확인
        self.assertFalse(auth.validate_session(token))

    def test_session_lifecycle(self):
        auth.setup_master_password("admin1234")
        token = auth.create_session()
        self.assertTrue(auth.validate_session(token))

        # 세션 폐기(로그아웃)
        auth.revoke_session(token)
        self.assertFalse(auth.validate_session(token))


if __name__ == "__main__":
    unittest.main()
