# -*- coding: utf-8 -*-
"""
tests/test_release_security.py
- Release Package Zero-Leak Security Audit Test
- Verifies that private keys (*.key, release_ed25519.key, *.pem) are strictly excluded from distribution packages
- Verifies that only public keys (*.pub) are included
"""

import os
import sys
import unittest
import zipfile

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from tools.release import build_self_extracting_updater


class TestReleasePackageSecurity(unittest.TestCase):

    def setUp(self):
        self.test_version = "9.9.9_test_audit"
        self.dist_dir = os.path.join(BASE_DIR, "dist")
        self.zip_path = os.path.join(self.dist_dir, f"release_v{self.test_version}.zip")
        self.sig_path = self.zip_path + ".sig"

    def tearDown(self):
        if os.path.exists(self.zip_path):
            try:
                os.remove(self.zip_path)
            except OSError:
                pass
        if os.path.exists(self.sig_path):
            try:
                os.remove(self.sig_path)
            except OSError:
                pass

    def test_private_key_zero_leak(self):
        """배포 ZIP 파일 내에 어떠한 개인키(*.key, *.pem, private)도 포함되지 않아야 함."""
        bat_path, sig_hex, raw_bytes = build_self_extracting_updater(self.test_version)

        self.assertTrue(os.path.exists(self.zip_path), f"ZIP 파일이 생성되지 않음: {self.zip_path}")
        self.assertTrue(len(sig_hex) > 0, "Ed25519 전자 서명이 생성되어야 함")

        with zipfile.ZipFile(self.zip_path, 'r') as zf:
            namelist = zf.namelist()
            print(f"\n[Security Audit] Total files in package: {len(namelist)}")

            for name in namelist:
                name_lower = name.lower()
                # 1. 절대 금지: .key 확장자
                self.assertFalse(
                    name_lower.endswith('.key'),
                    f"CRITICAL LEAK: Private key file included in package: {name}"
                )
                # 2. 절대 금지: .pem 확장자
                self.assertFalse(
                    name_lower.endswith('.pem'),
                    f"CRITICAL LEAK: PEM key file included in package: {name}"
                )
                # 3. 절대 금지: private 키워드
                self.assertFalse(
                    'private' in name_lower,
                    f"CRITICAL LEAK: Private keyword file included in package: {name}"
                )
                # 4. 절대 금지: release_ed25519.key 파일
                self.assertNotEqual(
                    name, "keys/release_ed25519.key",
                    "CRITICAL LEAK: release_ed25519.key MUST NOT be in release package!"
                )

            # 5. 필수 포함: release_ed25519_v2.pub (활성 공개키)는 존재해야 함
            self.assertIn(
                "keys/release_ed25519_v2.pub", namelist,
                "Mandatory active public key 'keys/release_ed25519_v2.pub' must be present in package"
            )

        print("[Security Audit] PASSED: All private keys strictly excluded, public key verified.")


if __name__ == '__main__':
    unittest.main()
