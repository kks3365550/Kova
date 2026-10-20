# -*- coding: utf-8 -*-
"""
tests/test_key_rotation.py - 다중 공개키 검증 및 키 회전 회귀 테스트

배경
----
배포 서명 개인키가 공개 저장소 이력에 유출됐다
(docs/SECURITY_20261008.md). 키를 교체하려면 전환 기간에 구 키와 신 키를
동시에 신뢰해야 한다. core/updater.py 는 이제 keys/*.pub 를 자동 탐색하고,
core/crypto_sign.verify_bytes_ed25519_any 가 OR 검증을 한다.

이 테스트는 그 계약을 고정한다.
"""
import os
import sys
import tempfile
import unittest
import zipfile
import hashlib
import shutil

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.crypto_sign import (
    sign_bytes_ed25519,
    verify_bytes_ed25519,
    verify_bytes_ed25519_any,
)


def _make_keypair(tmpdir, name):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
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
    priv_path = os.path.join(tmpdir, name + ".key")
    pub_path = os.path.join(tmpdir, name + ".pub")
    with open(priv_path, "wb") as f:
        f.write(priv_pem)
    with open(pub_path, "wb") as f:
        f.write(pub_pem)
    return priv_path, pub_path


def _make_zip(tmpdir, name="payload.zip"):
    zp = os.path.join(tmpdir, name)
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("VERSION", "9.9.9")
        zf.writestr("core/ok.py", "print('ok')")
    with open(zp, "rb") as f:
        raw = f.read()
    return zp, raw, hashlib.sha256(raw).hexdigest()


class TestVerifyAny(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_keyring_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_single_key_match(self):
        priv, pub = _make_keypair(self.tmp, "a")
        payload = b"hello rotation"
        sig = sign_bytes_ed25519(payload, priv)
        ok, idx = verify_bytes_ed25519_any(payload, sig, [pub])
        self.assertTrue(ok)
        self.assertEqual(idx, 0)

    def test_old_and_new_both_trusted(self):
        """전환 기간: 구 키 서명도 신 키 서명도 통과해야 한다."""
        old_priv, old_pub = _make_keypair(self.tmp, "old")
        new_priv, new_pub = _make_keypair(self.tmp, "new")
        payload = b"release payload"
        sig_old = sign_bytes_ed25519(payload, old_priv)
        sig_new = sign_bytes_ed25519(payload, new_priv)
        ring = [old_pub, new_pub]
        ok1, i1 = verify_bytes_ed25519_any(payload, sig_old, ring)
        ok2, i2 = verify_bytes_ed25519_any(payload, sig_new, ring)
        self.assertTrue(ok1)
        self.assertEqual(i1, 0)
        self.assertTrue(ok2)
        self.assertEqual(i2, 1)

    def test_revoked_old_key_rejected(self):
        """구 공개키 제거 후: 구 키 서명은 거부, 신 키 서명은 통과."""
        old_priv, _old_pub = _make_keypair(self.tmp, "old")
        new_priv, new_pub = _make_keypair(self.tmp, "new")
        payload = b"release payload"
        sig_old = sign_bytes_ed25519(payload, old_priv)
        sig_new = sign_bytes_ed25519(payload, new_priv)
        # 구 키 제거된 링에는 신 키만 있다
        ok_old, _ = verify_bytes_ed25519_any(payload, sig_old, [new_pub])
        ok_new, _ = verify_bytes_ed25519_any(payload, sig_new, [new_pub])
        self.assertFalse(ok_old, "폐기된 키 서명이 통과하면 회전이 무의미하다")
        self.assertTrue(ok_new)

    def test_empty_candidates_fail_closed(self):
        priv, _pub = _make_keypair(self.tmp, "a")
        sig = sign_bytes_ed25519(b"x", priv)
        ok, idx = verify_bytes_ed25519_any(b"x", sig, [])
        self.assertFalse(ok)
        self.assertIsNone(idx)

    def test_tampered_payload_rejected(self):
        priv, pub = _make_keypair(self.tmp, "a")
        sig = sign_bytes_ed25519(b"original", priv)
        ok, _ = verify_bytes_ed25519_any(b"tampered", sig, [pub])
        self.assertFalse(ok)


class TestVerifyUpdateKeyring(unittest.TestCase):
    """verify_update 의 키링 모드"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_verify_ring_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_explicit_single_key_path_preserved(self):
        """pub_key_path 명시 = 기존 동작 그대로 (하위 호환)."""
        from core.updater import verify_update
        old_priv, old_pub = _make_keypair(self.tmp, "old")
        zp, _raw, sha = _make_zip(self.tmp)
        sig = sign_bytes_ed25519(open(zp, "rb").read(), old_priv)
        self.assertTrue(verify_update(zp, sha, sig, pub_key_path=old_pub))

    def test_trusted_list_accepts_either_key(self):
        from core.updater import verify_update
        old_priv, old_pub = _make_keypair(self.tmp, "old")
        new_priv, new_pub = _make_keypair(self.tmp, "new")
        zp, _raw, sha = _make_zip(self.tmp)
        raw = open(zp, "rb").read()
        sig_old = sign_bytes_ed25519(raw, old_priv)
        sig_new = sign_bytes_ed25519(raw, new_priv)
        ring = [old_pub, new_pub]
        self.assertTrue(verify_update(zp, sha, sig_old, trusted_pub_paths=ring))
        self.assertTrue(verify_update(zp, sha, sig_new, trusted_pub_paths=ring))

    def test_trusted_list_rejects_unknown_key(self):
        from core.updater import verify_update
        old_priv, old_pub = _make_keypair(self.tmp, "old")
        _new_priv, new_pub = _make_keypair(self.tmp, "new")
        attacker_priv, _attacker_pub = _make_keypair(self.tmp, "attacker")
        zp, _raw, sha = _make_zip(self.tmp)
        raw = open(zp, "rb").read()
        sig_attack = sign_bytes_ed25519(raw, attacker_priv)
        ring = [old_pub, new_pub]
        self.assertFalse(verify_update(zp, sha, sig_attack, trusted_pub_paths=ring))

    def test_no_keys_fail_closed(self):
        from core.updater import verify_update
        old_priv, _old_pub = _make_keypair(self.tmp, "old")
        zp, _raw, sha = _make_zip(self.tmp)
        sig = sign_bytes_ed25519(open(zp, "rb").read(), old_priv)
        self.assertFalse(verify_update(zp, sha, sig, trusted_pub_paths=[]))

    def test_get_trusted_paths_returns_list(self):
        from core.updater import get_trusted_public_key_paths
        paths = get_trusted_public_key_paths()
        self.assertIsInstance(paths, list)
        # 현재 저장소에는 최소 release_ed25519.pub 가 있다
        self.assertTrue(any(p.endswith(".pub") for p in paths))


class TestRotateTool(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        import subprocess
        tmpkeys = os.path.join(self.tmp if hasattr(self, "tmp") else tempfile.mkdtemp(), "k")
        os.makedirs(tmpkeys, exist_ok=True)
        r = subprocess.run(
            [sys.executable, os.path.join(BASE_DIR, "tools", "rotate_release_key.py"),
             "--keys-dir", tmpkeys],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
        )
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        left = os.listdir(tmpkeys)
        self.assertEqual(left, [], "dry-run 인데 파일이 생기면 안 된다: %s" % left)

    def test_apply_creates_versioned_pair(self):
        import subprocess
        tmpkeys = tempfile.mkdtemp(prefix="test_rotate_apply_")
        try:
            r = subprocess.run(
                [sys.executable, os.path.join(BASE_DIR, "tools", "rotate_release_key.py"),
                 "--apply", "--key-id", "v9test", "--keys-dir", tmpkeys],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
            )
            self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
            priv = os.path.join(tmpkeys, "release_ed25519_v9test.key")
            pub = os.path.join(tmpkeys, "release_ed25519_v9test.pub")
            self.assertTrue(os.path.exists(priv))
            self.assertTrue(os.path.exists(pub))
            # 생성된 쌍으로 서명/검증이 돌아가야 한다
            payload = b"rotation tool check"
            sig = sign_bytes_ed25519(payload, priv)
            self.assertTrue(verify_bytes_ed25519(payload, sig, pub))
        finally:
            shutil.rmtree(tmpkeys, ignore_errors=True)


class TestSelfUpdateKeyring(unittest.TestCase):
    """
    POST /api/system/self-update 의 키링 배선.

    왜 필요한가:
        self-update 엔드포인트가 단일 경로
        (keys/release_ed25519.pub)로 하드코딩돼 있었다.
        키 교체 전환 기간에 신 키 서명 패키지가 거부된다.
        core/updater.get_trusted_public_key_paths 로 동기화했고,
        이 테스트가 그 배선을 고정한다.

    안전 장치:
        실제 keys/ 는 건드리지 않는다. 키링 함수를 임시 키로 패치한다.
        프로브 zip 에는 core/web/run.py 가 없어 서명 통과 후 구조 검사에서
        400으로 멈춘다. 추출은 절대 일어나지 않는다.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_selfupdate_")
        self.old_priv, self.old_pub = _make_keypair(self.tmp, "old")
        self.new_priv, self.new_pub = _make_keypair(self.tmp, "new")

        zp = os.path.join(self.tmp, "probe.zip")
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("README.txt", "no core files here")
        with open(zp, "rb") as f:
            self.body = f.read()
        self.sig_old = sign_bytes_ed25519(self.body, self.old_priv)
        self.sig_new = sign_bytes_ed25519(self.body, self.new_priv)

        import core.updater as updater_mod
        self._orig = updater_mod.get_trusted_public_key_paths
        updater_mod.get_trusted_public_key_paths = lambda: [self.old_pub, self.new_pub]
        self._updater_mod = updater_mod
        self.addCleanup(self._restore)

        from starlette.testclient import TestClient
        from web.app import app
        self.client = TestClient(app, raise_server_exceptions=False)

    def _restore(self):
        self._updater_mod.get_trusted_public_key_paths = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, sig):
        headers = {}
        if sig is not None:
            headers["X-Package-Signature"] = sig
        return self.client.post("/api/system/self-update",
                                content=self.body, headers=headers)

    def test_old_key_signature_passes_verification(self):
        # 400 = 서명 통과 후 구조 검사에서 멈춤 (추출 없음)
        r = self._post(self.sig_old)
        self.assertEqual(r.status_code, 400, r.text[:300])

    def test_new_key_signature_passes_verification(self):
        r = self._post(self.sig_new)
        self.assertEqual(r.status_code, 400, r.text[:300])

    def test_forged_signature_rejected(self):
        r = self._post("00" * 64)
        self.assertEqual(r.status_code, 403)

    def test_missing_signature_rejected(self):
        r = self._post(None)
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)