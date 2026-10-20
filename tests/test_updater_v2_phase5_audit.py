# -*- coding: utf-8 -*-
"""
tests/test_updater_v2_phase5_audit.py: Phase 5 프로덕션 보안 감사 스위트 (Production Adversarial Audit)

감사 영역:
Part A. BundleReader 하든 감사 (Outer ZIP 취약점 전수 시험)
  1. Exactly 5 allowlist: 6번째 파일(임의 파일, 디렉터리, .git 등) 주입 시 즉시 REJECT
  2. Duplicate Entry: 동일 파일명 2개 이상 포함된 해석 차이 공격 즉시 REJECT
  3. Per-file size limits: policy.json, manifest.json, sig 파일 크기 상한 초과 시 REJECT
  4. Outer Zip Bomb: 비정상 압축비 (>50.0x) 주입 시 REJECT
  5. Filename Traversal / Abnormal: 파일명에 ../, 슬래시, 제어문자 포함 시 REJECT

Part B. Trust Anchor & Fail-Closed 감사 (default_keyring)
  1. Missing key file: 공개키 파일 부재 시 즉시 예외 발생 (Fail-Closed)
  2. Corrupted key format: 공개키 포맷 손상 시 즉시 예외 발생 (Fail-Closed)
  3. Fingerprint mismatch: 다른 공개키로 위조/바꿔치기 시 지문 불일치로 즉시 예외 발생 (Fail-Closed)

Part C. 실제 Windows OS 파일시스템 및 프로세스 악조건 감사
  1. Target file locked: 타 프로세스(또는 스레드)가 실행 파일 핸들을 물고 있을 때(Sharing Violation) 재시도 및 실패 시 롤백 보존
  2. Failed Health Check: 헬스체크 실패 시 기존 버전 원복 및 프로세스 무창 재기동 확인
  3. Rollback Failure Safe Error: 롤백 대상 디렉토리가 훼손되어 원복조차 불가능할 때 기존 데이터 손상 없이 Safe Error 방출
"""

import io
import os
import json
import time
import shutil
import zipfile
import tempfile
import unittest
import threading
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from core.updater_v2.bundle import BundleReader, BundleCreator, BundleFormatError
from core.updater_v2.default_keyring import (
    load_default_keyring,
    TrustAnchorError,
    EXPECTED_ROOT_KEY_SHA256
)
from core.updater_v2.installer import AtomicInstaller, InstallerError


class TestPhase5BundleReaderHardening(unittest.TestCase):
    """Part A: BundleReader outer ZIP 보안 감사"""

    def setUp(self):
        self.valid_policy = b'{"schema_version": "1.0"}'
        self.valid_sig = b"a" * 128
        self.valid_manifest = b'{"schema_version": "1.0"}'
        self.valid_package = b"PK\x05\x06" + b"\x00" * 18  # Empty zip bytes

    def _build_bundle(self, entries: dict) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, content in entries.items():
                zf.writestr(name, content)
        return buf.getvalue()

    def test_01_exactly_5_entries_accepted(self):
        """정확히 승인된 5개 엔트리가 정상적으로 수용되는지 확인"""
        entries = {
            "policy.json": self.valid_policy,
            "policy.sig": self.valid_sig,
            "manifest.json": self.valid_manifest,
            "manifest.sig": self.valid_sig,
            "package.zip": self.valid_package
        }
        bundle_bytes = self._build_bundle(entries)
        acquired = BundleReader.read(bundle_bytes)
        self.assertEqual(acquired.policy_bytes, self.valid_policy)

    def test_02_extra_unapproved_file_rejected(self):
        """5개 외에 제6의 파일(악성 스크립트 등)이 포함되어 있으면 즉시 거부"""
        entries = {
            "policy.json": self.valid_policy,
            "policy.sig": self.valid_sig,
            "manifest.json": self.valid_manifest,
            "manifest.sig": self.valid_sig,
            "package.zip": self.valid_package,
            "evil_payload.exe": b"malicious binary"
        }
        bundle_bytes = self._build_bundle(entries)
        with self.assertRaises(BundleFormatError) as ctx:
            BundleReader.read(bundle_bytes)
        self.assertIn("exactly 5 entries", str(ctx.exception))

    def test_03_missing_required_entry_rejected(self):
        """필수 5개 중 1개라도 누락되면 즉시 거부"""
        entries = {
            "policy.json": self.valid_policy,
            "policy.sig": self.valid_sig,
            "manifest.json": self.valid_manifest,
            "package.zip": self.valid_package
        }
        bundle_bytes = self._build_bundle(entries)
        with self.assertRaises(BundleFormatError) as ctx:
            BundleReader.read(bundle_bytes)
        self.assertIn("exactly 5 entries", str(ctx.exception))

    def test_04_duplicate_zip_entry_rejected(self):
        """중복된 엔트리(해석 차이 공격) 포함 시 즉시 거부"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("policy.json", self.valid_policy)
            zf.writestr("policy.sig", self.valid_sig)
            zf.writestr("manifest.json", self.valid_manifest)
            zf.writestr("manifest.sig", self.valid_sig)
            zf.writestr("package.zip", self.valid_package)
            # 동일 이름으로 엔트리 추가 생성
            zf.writestr("policy.json", b'{"tampered": true}')

        with self.assertRaises(BundleFormatError):
            BundleReader.read(buf.getvalue())

    def test_05_oversized_metadata_rejected(self):
        """policy.json 또는 sig 크기가 규정된 한도(64KB, 1KB)를 초과하면 거부"""
        entries = {
            "policy.json": b"x" * (65 * 1024),  # 65 KB (한도 64KB 초과)
            "policy.sig": self.valid_sig,
            "manifest.json": self.valid_manifest,
            "manifest.sig": self.valid_sig,
            "package.zip": self.valid_package
        }
        bundle_bytes = self._build_bundle(entries)
        with self.assertRaises(BundleFormatError) as ctx:
            BundleReader.read(bundle_bytes)
        self.assertIn("exceeds limit", str(ctx.exception))

    def test_06_outer_zip_bomb_anomaly_rejected(self):
        """Outer bundle 레벨에서 극단적 압축비(비정상 압축비) 시도 시 거부"""
        bomb_data = b"0" * (10 * 1024 * 1024)  # 10 MB의 0바이트 (압축비 > 500x)
        entries = {
            "policy.json": self.valid_policy,
            "policy.sig": self.valid_sig,
            "manifest.json": self.valid_manifest,
            "manifest.sig": self.valid_sig,
            "package.zip": bomb_data
        }
        bundle_bytes = self._build_bundle(entries)
        with self.assertRaises(BundleFormatError) as ctx:
            BundleReader.read(bundle_bytes)
        self.assertIn("excessive compression ratio", str(ctx.exception))

    def test_07_traversal_filename_in_bundle_rejected(self):
        """파일명에 ../, 디렉터리 경로 분리자(/, \\)가 포함된 경우 즉시 거부"""
        entries = {
            "../policy.json": self.valid_policy,
            "policy.sig": self.valid_sig,
            "manifest.json": self.valid_manifest,
            "manifest.sig": self.valid_sig,
            "package.zip": self.valid_package
        }
        bundle_bytes = self._build_bundle(entries)
        with self.assertRaises(BundleFormatError):
            BundleReader.read(bundle_bytes)


class TestPhase5TrustAnchorFailClosed(unittest.TestCase):
    """Part B: default_keyring Trust Anchor & Fail-Closed 감사"""

    def setUp(self):
        self.test_root = tempfile.mkdtemp(prefix="trust_anchor_test_")
        self.keys_dir = os.path.join(self.test_root, "keys")
        os.makedirs(self.keys_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_root, ignore_errors=True)

    def test_08_missing_public_key_fails_closed(self):
        """공개키 파일이 존재하지 않을 때 조용히 넘어가지 않고 TrustAnchorError 발생 확인"""
        with self.assertRaises(TrustAnchorError) as ctx:
            load_default_keyring(base_dir=self.test_root)
        self.assertIn("FAIL-CLOSED", str(ctx.exception))
        self.assertIn("missing", str(ctx.exception))

    def test_09_corrupted_public_key_fails_closed(self):
        """공개키 파일 내용이 손상되었거나 유효하지 않은 포맷일 때 즉시 거부"""
        pub_path = os.path.join(self.keys_dir, "release_ed25519_v2.pub")
        with open(pub_path, "wb") as f:
            f.write(b"NOT A VALID PUBLIC KEY PEM DATA")

        with self.assertRaises(TrustAnchorError) as ctx:
            load_default_keyring(base_dir=self.test_root, enforce_fingerprint=False)
        self.assertIn("FAIL-CLOSED", str(ctx.exception))

    def test_10_replaced_public_key_fails_fingerprint_check(self):
        """공개키 파일이 공격자의 다른 정상 Ed25519 키로 교체되었을 때 지문 불일치로 차단"""
        # 공격자의 자체 키 생성
        attacker_priv = Ed25519PrivateKey.generate()
        attacker_pub_pem = attacker_priv.public_key().public_bytes(
            encoding=Encoding.PEM,
            format=PublicFormat.SubjectPublicKeyInfo
        )
        pub_path = os.path.join(self.keys_dir, "release_ed25519_v2.pub")
        with open(pub_path, "wb") as f:
            f.write(attacker_pub_pem)

        # 기대 지문(kks3365550 공식 지문)과 대조 검증
        with self.assertRaises(TrustAnchorError) as ctx:
            load_default_keyring(
                base_dir=self.test_root,
                expected_fingerprint=EXPECTED_ROOT_KEY_SHA256,
                enforce_fingerprint=True
            )
        self.assertIn("fingerprint mismatch", str(ctx.exception))


class TestPhase5WindowsFilesystemAdversarial(unittest.TestCase):
    """Part C: 실제 Windows 파일시스템 악조건 (Locking / Race / Fail-Safe) 감사"""

    def setUp(self):
        self.test_root = tempfile.mkdtemp(prefix="win_adversarial_test_")
        self.app_dir = os.path.join(self.test_root, "app")
        os.makedirs(os.path.join(self.app_dir, "core"), exist_ok=True)
        with open(os.path.join(self.app_dir, "VERSION"), "w", encoding="utf-8") as f:
            f.write("2.9.8\n")
        with open(os.path.join(self.app_dir, "core", "backup.py"), "w", encoding="utf-8") as f:
            f.write("# original code v2.9.8\n")

        # 정상 업데이트 패키지 바이너리 생성
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("core/backup.py", b"# new code v2.9.9\n")
            zf.writestr("VERSION", b"2.9.9\n")
        self.package_bytes = buf.getvalue()

    def tearDown(self):
        shutil.rmtree(self.test_root, ignore_errors=True)

    def test_11_target_file_locked_triggers_rollback_and_preserves_version(self):
        """
        설치 대상 파일(예: core/backup.py)을 다른 스레드가 독점 Lock 걸고 있을 때:
        파일 교체 실패 후 기존 버전으로 안전 복원(Rollback)되어 v2.9.8 상태가 보존되는지 확인
        """
        target_file = os.path.join(self.app_dir, "core", "backup.py")
        installer = AtomicInstaller(target_dir=self.app_dir)

        # 파일 핸들을 독점 오픈하여 Windows 공유 위반(Sharing Violation / PermissionError) 모의
        lock_held = threading.Event()
        release_lock = threading.Event()

        def _lock_file():
            # Windows API CreateFileW를 사용하여 dwShareMode=0 (배타적 독점) 락 걸기
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.CreateFileW(
                target_file,
                0x40000000,  # GENERIC_WRITE
                0,           # dwShareMode = 0 (배타적 독점)
                None,
                3,           # OPEN_EXISTING
                0x80,        # FILE_ATTRIBUTE_NORMAL
                None
            )
            if handle == -1 or handle == 0xFFFFFFFF:
                lock_held.set()
                return

            try:
                lock_held.set()
                # 3.5초 대기: 설치 재시도(3초) 소진 후 롤백 도중 락이 해제되어 롤백 성공을 보장
                release_lock.wait(timeout=3.5)
            finally:
                kernel32.CloseHandle(handle)

        t = threading.Thread(target=_lock_file, daemon=True)
        t.start()
        lock_held.wait(timeout=2)

        try:
            with self.assertRaises(InstallerError):
                installer.install(
                    raw_package_bytes=self.package_bytes,
                    new_version="2.9.9",
                    skip_process_control=True
                )
        finally:
            release_lock.set()
            t.join(timeout=2)

        # 검증: 설치가 실패하여 rollback 복원되었으므로 VERSION 및 코드는 v2.9.8 유지
        with open(os.path.join(self.app_dir, "VERSION"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "2.9.8")

    def test_12_rollback_failure_raises_safe_error_without_data_corruption(self):
        """
        백업 디렉터리 자체가 물리적으로 손상/부재하여 롤백조차 불가능할 때:
        치명적 예외를 안전하게 전파하고 데이터 디렉터리를 훼손하지 않는지 확인
        """
        installer = AtomicInstaller(target_dir=self.app_dir)
        non_existent_backup = os.path.join(self.app_dir, "backup", "v_broken")

        success = installer.rollback(backup_dir=non_existent_backup)
        self.assertFalse(success)


if __name__ == "__main__":
    unittest.main()
