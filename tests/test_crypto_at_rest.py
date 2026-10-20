#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_crypto_at_rest.py - CAS Dedup 100% 보존 및 At-Rest 암호화 단위 테스트 스위트
"""

import os
import shutil
import tempfile
import unittest
import concurrent.futures
import hashlib
import zstandard as zstd

from core.crypto_at_rest import (
    CryptoAtRestEngine,
    StorageKeyDeriver,
    MAGIC_V0_ZSTD,
    MAGIC_V1_ENC,
    ENCRYPTION_VERSION_V0,
    ENCRYPTION_VERSION_V1
)


class TestCryptoAtRest(unittest.TestCase):
    def setUp(self):
        self.test_root = tempfile.mkdtemp(prefix="crypto_test_")
        self.repo_dir = os.path.join(self.test_root, "repo")
        os.makedirs(self.repo_dir, exist_ok=True)
        self.passphrase = "UltraSecurePassphrase2026!#"
        self.engine = CryptoAtRestEngine.from_passphrase(self.passphrase, self.repo_dir)

    def tearDown(self):
        def _onerror(func, path, exc_info):
            try:
                import stat
                os.chmod(path, stat.S_IWRITE)
                func(path)
            except Exception:
                pass
        shutil.rmtree(self.test_root, onerror=_onerror)

    def test_01_cas_dedup_100_percent(self):
        """동일 데이터 암호화 저장 시 단 1개의 블롭 파일만 생성되고 신규 쓰기가 스킵되는지 검증 (CAS Dedup 보존)"""
        sample_data = b"Repeated identical file content for CAS dedup testing ABCXYZ 12345"
        sha256 = hashlib.sha256(sample_data).hexdigest()

        # 1차 저장 (신규 쓰기 발생)
        path1, is_new1 = self.engine.save_blob_atomic(self.repo_dir, sample_data, sha256)
        self.assertTrue(is_new1, "첫 번째 저장은 신규 쓰기여야 합니다.")
        self.assertTrue(os.path.exists(path1))
        initial_mtime = os.path.getmtime(path1)

        # 2차 저장 (동일 평문 데이터 저장 시도)
        path2, is_new2 = self.engine.save_blob_atomic(self.repo_dir, sample_data, sha256)
        self.assertFalse(is_new2, "두 번째 저장은 Dedup되어 신규 쓰기가 일어나지 않아야 합니다.")
        self.assertEqual(path1, path2, "경로가 완전히 일치해야 합니다.")
        self.assertEqual(os.path.getmtime(path2), initial_mtime, "기존 블롭 파일이 덮어쓰여지지 않아야 합니다.")

        # 복호화 검증
        with open(path2, "rb") as f:
            blob_bytes = f.read()
        restored = self.engine.decrypt_blob_data(blob_bytes, sha256)
        self.assertEqual(restored, sample_data)

    def test_02_v0_v1_backward_compatibility(self):
        """v0 평문 블롭(Zstd)과 v1 암호화 블롭(ENC\\x01)이 공존할 때 둘 다 100% 정상 복원되는지 검증"""
        data_v0 = b"Legacy plaintext v0 backup blob content"
        sha256_v0 = hashlib.sha256(data_v0).hexdigest()

        # v0 평문 블롭 생성 (Zstd 압축만 수행)
        cctx = zstd.ZstdCompressor(level=1)
        compressed_v0 = cctx.compress(data_v0)
        v0_dir = os.path.join(self.repo_dir, "blobs", sha256_v0[:2])
        os.makedirs(v0_dir, exist_ok=True)
        v0_path = os.path.join(v0_dir, f"{sha256_v0}.blob")
        with open(v0_path, "wb") as f:
            f.write(compressed_v0)

        # v1 암호화 블롭 생성
        data_v1 = b"New encrypted v1 backup blob content with AES-GCM"
        sha256_v1 = hashlib.sha256(data_v1).hexdigest()
        v1_path, _ = self.engine.save_blob_atomic(self.repo_dir, data_v1, sha256_v1)

        # 복호화 엔진으로 두 블롭 모두 복원 검증
        with open(v0_path, "rb") as f:
            b0 = f.read()
        with open(v1_path, "rb") as f:
            b1 = f.read()

        # v0는 평문 Zstd 압축 해제로 복원
        res0 = self.engine.decrypt_blob_data(b0, sha256_v0)
        self.assertEqual(res0, data_v0)

        # v1은 AES-256-GCM 복호화 + Zstd 압축 해제로 복원
        res1 = self.engine.decrypt_blob_data(b1, sha256_v1)
        self.assertEqual(res1, data_v1)

    def test_03_storage_key_derivation_and_scrypt_metadata(self):
        """동일 마스터키 + 메타데이터로부터 Storage Key가 100% 일치하게 재생성되는지 검증"""
        engine1 = CryptoAtRestEngine.from_passphrase(self.passphrase, self.repo_dir)
        engine2 = CryptoAtRestEngine.from_passphrase(self.passphrase, self.repo_dir)
        self.assertEqual(engine1.storage_key, engine2.storage_key)

        # 잘못된 비밀번호 입력 시 다른 키 파생 확인
        engine_wrong = CryptoAtRestEngine.from_passphrase("WrongPassphrase123", self.repo_dir)
        self.assertNotEqual(engine1.storage_key, engine_wrong.storage_key)

        # 엔진1로 암호화한 데이터를 잘못된 엔진으로 복호화 시도 시 실패(InvalidTag) 검증
        data = b"Secret data to test wrong key"
        h = hashlib.sha256(data).hexdigest()
        enc = engine1.encrypt_blob_data(data, h)
        with self.assertRaises(ValueError):
            engine_wrong.decrypt_blob_data(enc, h)

    def test_04_concurrent_worker_same_blob_race(self):
        """16개 워커 스레드가 동시에 동일한 블롭 저장을 시도할 때 충돌 없이 단 1개만 온전히 저장되는지 검증"""
        shared_data = b"Highly contested concurrent blob content data 999" * 100
        h = hashlib.sha256(shared_data).hexdigest()

        results = []
        def worker():
            path, is_new = self.engine.save_blob_atomic(self.repo_dir, shared_data, h)
            return path, is_new

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            futures = [executor.submit(worker) for _ in range(16)]
            for fut in concurrent.futures.as_completed(futures):
                results.append(fut.result())

        # 모든 스레드가 동일한 파일 경로를 반환해야 함
        paths = [r[0] for r in results]
        self.assertEqual(len(set(paths)), 1)

        # 신규 생성(is_new=True)은 단 한 번만 발생해야 함
        new_counts = [r[1] for r in results if r[1] is True]
        self.assertEqual(len(new_counts), 1)

        # 저장된 블롭의 복호화 무결성 검증
        final_path = paths[0]
        with open(final_path, "rb") as f:
            blob_bytes = f.read()
        self.assertEqual(self.engine.decrypt_blob_data(blob_bytes, h), shared_data)

    def test_05_tamper_evident_aad_and_nonce(self):
        """암호문, Nonce, 또는 AAD 해시 중 1바이트라도 변조되면 복호화가 즉각 거부되는지 검증"""
        data = b"Original pristine file content for integrity tampering test"
        h = hashlib.sha256(data).hexdigest()
        enc_blob = bytearray(self.engine.encrypt_blob_data(data, h))

        # 1. Nonce 1바이트 변조
        enc_nonce_corrupted = bytearray(enc_blob)
        enc_nonce_corrupted[5] ^= 0xFF
        with self.assertRaises(ValueError):
            self.engine.decrypt_blob_data(bytes(enc_nonce_corrupted), h)

        # 2. Ciphertext 1바이트 변조
        enc_cipher_corrupted = bytearray(enc_blob)
        enc_cipher_corrupted[-5] ^= 0xFF
        with self.assertRaises(ValueError):
            self.engine.decrypt_blob_data(bytes(enc_cipher_corrupted), h)

        # 3. AAD (Expected SHA-256) 불일치 변조
        wrong_hash = "0" * 64
        with self.assertRaises(ValueError):
            self.engine.decrypt_blob_data(bytes(enc_blob), wrong_hash)


if __name__ == "__main__":
    unittest.main()
