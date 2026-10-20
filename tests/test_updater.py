# -*- coding: utf-8 -*-
"""
tests/test_updater.py
- Automated Unit & Security Test Suite for core/updater.py
- Tests version parsing, SHA-256 integrity, Ed25519 digital signature, tampered files, sensitive key rejection, and fail-safe error handling
"""

import os
import sys
import unittest
import tempfile
import zipfile
import json
import hashlib
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.updater import (
    parse_version,
    verify_update,
    check_for_update,
    download_update
)
from core.crypto_sign import sign_bytes_ed25519


def _make_test_key_pair(temp_dir: str):
    """
    테스트 전용 Ed25519 키 페어를 임시 디렉토리에 만든다.

    왜 이렇게 하는가:
        이전에는 keys/release_ed25519.key (배포 서명용 개인키) 를 썼다.
        그 파일은 .gitignore 대상이라 CI 에 없는데,
        2026-10-07 CI 2차 실행에서 test_updater.py 7건이
        FileNotFoundError 로 죽었다. 로컬에는 키가 있어서 통과했고.

        배포용 개인키에 테스트가 의존하면 두 가지 문제가 생긴다:
          1) 깨끗한 클론/CI 에서 테스트가 돌지 않는다
          2) 테스트가 실제 배포 키를 만진다 (오염 위험)

        서명/검증 로직 자체는 어떤 키로든 동일하게 동작하므로,
        테스트는 테스트용 키를 직접 만들어 쓴다.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    priv = Ed25519PrivateKey.generate()

    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    priv_path = os.path.join(temp_dir, "test_ed25519.key")
    pub_path = os.path.join(temp_dir, "test_ed25519.pub")
    with open(priv_path, "wb") as f:
        f.write(priv_pem)
    with open(pub_path, "wb") as f:
        f.write(pub_pem)

    return priv_path, pub_path


class TestUpdaterSecurityAndIntegrity(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_updater_")
        # 배포용 키 대신 이 테스트에서 만든 키를 쓴다.
        self.priv_key_path, self.pub_key_path = _make_test_key_pair(self.temp_dir)

        # Create a valid test ZIP
        self.valid_zip_path = os.path.join(self.temp_dir, "valid_test.zip")
        with zipfile.ZipFile(self.valid_zip_path, 'w') as zf:
            zf.writestr("VERSION", "2.9.2")
            zf.writestr("core/test.py", "print('hello')")
            zf.writestr("keys/release_ed25519.pub", "DUMMY_PUB_KEY")

        with open(self.valid_zip_path, 'rb') as f:
            self.valid_bytes = f.read()

        self.valid_sha256 = hashlib.sha256(self.valid_bytes).hexdigest()
        self.valid_sig = sign_bytes_ed25519(self.valid_bytes, self.priv_key_path)

    def tearDown(self):
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_version_parsing(self):
        """시맨틱 버전 파싱 및 순서 비교 검증"""
        self.assertEqual(parse_version("v2.9.1"), (2, 9, 1))
        self.assertEqual(parse_version("2.10.0"), (2, 10, 0))
        self.assertTrue(parse_version("v2.10.0") > parse_version("v2.9.9"))
        self.assertTrue(parse_version("2.9.2") > parse_version("2.9.1"))
        self.assertFalse(parse_version("2.9.1") > parse_version("2.9.1"))

    def test_verify_update_success(self):
        """정상적인 해시 및 서명에 대해 검증 성공 검증"""
        result = verify_update(
            zip_path=self.valid_zip_path,
            expected_sha256=self.valid_sha256,
            expected_signature=self.valid_sig,
            pub_key_path=self.pub_key_path
        )
        self.assertTrue(result, "정상 패키지, 해시, 서명은 검증을 통과해야 함")

    def test_verify_update_tampered_hash(self):
        """ZIP 내용이 1바이트라도 변경되면 SHA-256 불일치로 차단 검증"""
        tampered_zip = os.path.join(self.temp_dir, "tampered.zip")
        # Modify last byte
        corrupted_bytes = self.valid_bytes[:-1] + (b'\x00' if self.valid_bytes[-1:] != b'\x00' else b'\x01')
        with open(tampered_zip, 'wb') as f:
            f.write(corrupted_bytes)

        result = verify_update(
            zip_path=tampered_zip,
            expected_sha256=self.valid_sha256,
            expected_signature=self.valid_sig,
            pub_key_path=self.pub_key_path
        )
        self.assertFalse(result, "변조된 파일은 SHA-256 검증에서 즉시 탈락해야 함")

    def test_verify_update_invalid_signature(self):
        """전자 서명이 변조되었거나 가짜인 경우 설치 차단 검증"""
        fake_signature = "a" * len(self.valid_sig)
        result = verify_update(
            zip_path=self.valid_zip_path,
            expected_sha256=self.valid_sha256,
            expected_signature=fake_signature,
            pub_key_path=self.pub_key_path
        )
        self.assertFalse(result, "잘못된 전자 서명은 검증에서 즉시 탈락해야 함")

    def test_verify_update_rejects_sensitive_key_injection(self):
        """악의적으로 ZIP 내부에 .key 개인키 파일이 유입된 경우 즉각 거부 검증"""
        malicious_zip = os.path.join(self.temp_dir, "malicious.zip")
        with zipfile.ZipFile(malicious_zip, 'w') as zf:
            zf.writestr("VERSION", "2.9.2")
            zf.writestr("keys/leak_private.key", "MALICIOUS_KEY")

        with open(malicious_zip, 'rb') as f:
            raw = f.read()

        h = hashlib.sha256(raw).hexdigest()
        sig = sign_bytes_ed25519(raw, self.priv_key_path)

        result = verify_update(
            zip_path=malicious_zip,
            expected_sha256=h,
            expected_signature=sig,
            pub_key_path=self.pub_key_path
        )
        self.assertFalse(result, "민감키(.key)가 포함된 아카이브는 검증을 통과할 수 없음")

    def test_check_for_update_offline_failsafe(self):
        """네트워크 또는 Firebase 장애 시 예외 발생 없이 안전하게 None 반환 (Fail-Safe)"""
        with patch('urllib.request.urlopen', side_effect=Exception("Network Timeout")):
            result = check_for_update(current_ver="2.9.1")
            self.assertIsNone(result, "오프라인/장애 시 Fail-Safe로 None을 반환해야 함")

    def test_check_for_update_detection(self):
        """
        신규 버전 감지 시 정확한 release_info 반환 검증.

        NOTE: v2.10.1에서 Firebase -> GitHub Releases API로 전환되었다.
              이 테스트는 현행 GitHub API 응답 형식(tag_name + assets[])을 사용한다.
        """
        github_release = {
            "tag_name": "v2.9.2",
            "assets": [
                {"name": "release_2.9.2.zip",
                 "browser_download_url": "https://example.com/release_2.9.2.zip"},
                {"name": "release_2.9.2.zip.sig",
                 "browser_download_url": "https://example.com/release_2.9.2.zip.sig"},
            ]
        }

        def _fake_urlopen(req, *a, **kw):
            resp = MagicMock()
            resp.read.return_value = json.dumps(github_release).encode('utf-8')
            resp.__enter__.return_value = resp
            resp.__exit__.return_value = False
            return resp

        with patch('urllib.request.urlopen', side_effect=_fake_urlopen):
            # 1. 현재 버전이 2.9.1일 때 -> 업데이트 감지됨
            info = check_for_update(current_ver="2.9.1")
            self.assertIsNotNone(info, "신규 버전이 있으면 release_info 를 반환해야 함")
            self.assertTrue(info["update_available"])
            self.assertEqual(info["latest_version"], "2.9.2")
            self.assertTrue(info["download_url"].endswith("release_2.9.2.zip"))

            # 2. 현재 버전이 이미 2.9.2일 때 -> 업데이트 없음
            info_same = check_for_update(current_ver="2.9.2")
            self.assertIsNone(info_same)

            # 3. 현재 버전이 3.0.0일 때 -> 업데이트 없음
            info_higher = check_for_update(current_ver="3.0.0")
            self.assertIsNone(info_higher)


if __name__ == '__main__':
    unittest.main()
