# -*- coding: utf-8 -*-
"""
설치/업그레이드 E2E: 릴리즈 빌더가 만든 업데이트 패키지가 실제로 서명 검증을 통과하고,
구버전 설치본 위에 적용돼도 사용자 데이터가 보존되며 새 버전이 기동 가능한지 확인한다.

실제 서버·스케줄러·포트 8765 는 건드리지 않는다. 모든 작업은 임시 폴더 안에서 한다.
"""
import base64
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core import updater

NEW_VER = "9.9.9"
OLD_VER = "1.0.0"


def _load_release_module():
    spec = importlib.util.spec_from_file_location(
        "kova_release_tool", os.path.join(BASE_DIR, "tools", "release.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_keypair(dirpath, name):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.generate()
    priv_path = os.path.join(dirpath, name + ".key")
    pub_path = os.path.join(dirpath, name + ".pub")
    with open(priv_path, "wb") as f:
        f.write(priv.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
    with open(pub_path, "wb") as f:
        f.write(priv.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return priv_path, pub_path


class TestInstallUpgradeE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="kova_e2e_")
        # APPLY_UPDATE.bat 은 프로젝트 폴더의 부모에 생성되므로 한 단계 안쪽에 둔다.
        cls.proj = os.path.join(cls.work, "proj")
        os.makedirs(os.path.join(cls.proj, "keys"))
        shutil.copytree(os.path.join(BASE_DIR, "core"), os.path.join(cls.proj, "core"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        os.makedirs(os.path.join(cls.proj, "web"))
        for fn in os.listdir(os.path.join(BASE_DIR, "web")):
            if fn.endswith(".py"):
                shutil.copy2(os.path.join(BASE_DIR, "web", fn), os.path.join(cls.proj, "web", fn))
        shutil.copy2(os.path.join(BASE_DIR, "run.py"), os.path.join(cls.proj, "run.py"))
        with open(os.path.join(cls.proj, "VERSION"), "w") as f:
            f.write(NEW_VER)
        # 테스트용 릴리즈 키쌍 (개인키는 패키지에 들어가면 안 된다)
        cls.priv, cls.pub = _make_keypair(os.path.join(cls.proj, "keys"), "release_ed25519_v2")

        rel = _load_release_module()
        rel.BASE_DIR = cls.proj
        rel.VERSION_FILE = os.path.join(cls.proj, "VERSION")
        cls.bat, cls.sig, cls.raw = rel.build_self_extracting_updater(NEW_VER)
        cls.zip_path = os.path.join(cls.proj, "dist", "release_v%s.zip" % NEW_VER)
        cls.counter = 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def _make_old_install(self):
        type(self).counter += 1
        inst = os.path.join(self.work, "installed_%d" % self.counter)
        os.makedirs(os.path.join(inst, "core"))
        with open(os.path.join(inst, "VERSION"), "w") as f:
            f.write(OLD_VER)
        with open(os.path.join(inst, "core", "legacy_only.py"), "w") as f:
            f.write("# old file\n")
        # 사용자 데이터: 프로필 설정과 백업 저장소 블롭
        os.makedirs(os.path.join(inst, "config"))
        with open(os.path.join(inst, "config", "profiles.json"), "w", encoding="utf-8") as f:
            f.write('{"profiles": [{"name": "keep-me"}]}')
        os.makedirs(os.path.join(inst, "repo", "blobs"))
        with open(os.path.join(inst, "repo", "blobs", "abc"), "wb") as f:
            f.write(b"user-backup-data")
        return inst

    @staticmethod
    def _apply(zip_path, inst):
        """업데이터와 같은 의미: staging 에 풀고 설치 폴더 위에 덮어쓴다."""
        staging = tempfile.mkdtemp(prefix="kova_stage_")
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(staging)
            shutil.copytree(staging, inst, dirs_exist_ok=True)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def test_01_package_verifies_with_release_signature(self):
        self.assertTrue(self.sig, "서명이 생성되지 않음")
        sha = hashlib.sha256(self.raw).hexdigest()
        self.assertTrue(updater.verify_update(
            self.zip_path, sha, self.sig, trusted_pub_paths=[self.pub]))

    def test_02_tampered_package_is_rejected(self):
        bad = os.path.join(self.work, "tampered.zip")
        with open(bad, "wb") as f:
            f.write(self.raw + b"x")
        sha = hashlib.sha256(self.raw).hexdigest()
        self.assertFalse(updater.verify_update(bad, sha, self.sig, trusted_pub_paths=[self.pub]))
        # 해시를 맞춰도 서명이 다르면 거부
        sha2 = hashlib.sha256(self.raw + b"x").hexdigest()
        self.assertFalse(updater.verify_update(bad, sha2, self.sig, trusted_pub_paths=[self.pub]))

    def test_03_package_contains_no_private_key(self):
        with zipfile.ZipFile(self.zip_path) as zf:
            names = zf.namelist()
        self.assertFalse([n for n in names if n.lower().endswith((".key", ".pem"))])
        self.assertIn("keys/release_ed25519_v2.pub", names)
        self.assertIn("VERSION", names)
        self.assertIn("run.py", names)

    def test_04_apply_bat_embeds_identical_payload(self):
        with open(self.bat, "r", encoding="utf-8") as f:
            text = f.read()
        body = text.split("-----BEGIN CERTIFICATE-----", 1)[1].split("-----END CERTIFICATE-----", 1)[0]
        payload = base64.b64decode("".join(body.split()))
        self.assertEqual(payload, self.raw)
        self.assertIn("Ed25519-Signature: " + self.sig, text)
        self.assertIn("certutil -decode", text)

    def test_05_upgrade_preserves_user_data_and_updates_code(self):
        inst = self._make_old_install()
        self._apply(self.zip_path, inst)
        with open(os.path.join(inst, "VERSION")) as f:
            self.assertEqual(f.read().strip(), NEW_VER)
        with open(os.path.join(inst, "config", "profiles.json"), encoding="utf-8") as f:
            self.assertIn("keep-me", f.read())
        with open(os.path.join(inst, "repo", "blobs", "abc"), "rb") as f:
            self.assertEqual(f.read(), b"user-backup-data")
        self.assertTrue(os.path.exists(os.path.join(inst, "core", "updater.py")))
        self.assertTrue(os.path.exists(os.path.join(inst, "run.py")))

    def test_06_upgraded_install_is_importable(self):
        inst = self._make_old_install()
        self._apply(self.zip_path, inst)
        # 설치 폴더 안의 core 를 독립 프로세스로 import (격리 모드 + 설치 폴더만 경로에 추가)
        code = (
            "import sys; sys.path.insert(0, %r);"
            "import core, core.updater as u;"
            "print(u.parse_version('9.9.9'))" % inst
        )
        r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                           text=True, cwd=inst, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("(9, 9, 9)", r.stdout)
        # 진입 스크립트가 구문 오류 없이 컴파일돼야 한다
        r2 = subprocess.run([sys.executable, "-I", "-m", "py_compile",
                             os.path.join(inst, "run.py")], capture_output=True, text=True)
        self.assertEqual(r2.returncode, 0, r2.stderr)

    def test_07_reapplying_same_package_is_idempotent(self):
        inst = self._make_old_install()
        self._apply(self.zip_path, inst)
        self._apply(self.zip_path, inst)
        with open(os.path.join(inst, "VERSION")) as f:
            self.assertEqual(f.read().strip(), NEW_VER)
        with open(os.path.join(inst, "repo", "blobs", "abc"), "rb") as f:
            self.assertEqual(f.read(), b"user-backup-data")


if __name__ == "__main__":
    unittest.main()
