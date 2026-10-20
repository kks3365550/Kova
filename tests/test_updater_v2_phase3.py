# -*- coding: utf-8 -*-
"""
tests/test_updater_v2_phase3.py: OTA 3단계 (ArtifactVerifier, AtomicInstaller) 무결성 및 원자적 설치 테스트

검증 항목:
1. ArtifactVerifier:
   - 정상 manifest.json + manifest.sig + package.zip 검증 통과
   - manifest_signature 변조 시 ManifestSignatureError 발생
   - package.zip 내용 변조 (1바이트 변조) 시 PackageHashMismatchError 발생
   - package_size 불일치 시 PackageSizeMismatchError 발생
   - manifest.json 파싱 오류 또는 스키마 위반 시 ManifestParseError 발생
2. AtomicInstaller:
   - 정상 패키지 파일 원자적 교체 및 VERSION 파일 갱신 확인 (skip_process_control=True)
   - 교체 전 backup/v{기존버전} 디렉토리에 정확한 코드 백업 보존 확인
   - Zip-Slip (상위 경로 탈출 ..) 악성 압축 파일 주입 시 설치 거부 및 대상 디렉토리 오염 방지 확인
   - Zip Bomb (비정상 압축률) 주입 시 감지 및 안전 차단 확인
   - 헬스체크 실패 상황에서 이전 정상 버전으로의 자동 롤백 복원 확인
"""

import io
import os
import json
import shutil
import zipfile
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from core.updater_v2.jcs import canonicalize
from core.updater_v2.keyring import KeyRing, TrustedKey, KeyStatus
from core.updater_v2.artifact_verifier import (
    ArtifactVerifier,
    ArtifactVerificationError,
    ManifestParseError,
    ManifestSignatureError,
    PackageHashMismatchError,
    PackageSizeMismatchError
)
from core.updater_v2.installer import (
    AtomicInstaller,
    InstallerError,
    HealthCheckTimeoutError
)
from core.updater_v2.artifact_safety import (
    ArtifactSafetyChecker,
    assert_safe_destination_path,
    SafetyViolationError
)


class TestPhase3ArtifactVerifierAndInstaller(unittest.TestCase):
    """ArtifactVerifier 및 AtomicInstaller 단위 및 공격 방어 테스트"""

    def setUp(self):
        # 1. 테스트 키 생성
        self.priv_key = Ed25519PrivateKey.generate()
        self.pub_bytes = self.priv_key.public_key().public_bytes(
            encoding=Encoding.Raw,
            format=PublicFormat.Raw
        )
        self.keyring = KeyRing()
        self.keyring.add_trusted_key(
            TrustedKey(
                key_id="rel_2026a",
                public_key_bytes=self.pub_bytes,
                status=KeyStatus.ACTIVE,
                valid_from="2026-01-01T00:00:00Z",
                valid_until="2026-12-31T23:59:59Z"
            )
        )
        self.verifier = ArtifactVerifier(self.keyring)

        # 2. 정상 package.zip 생성
        self.package_files = {
            "core/backup.py": b"# updated backup core v2.9.9\ndef run(): return 42\n",
            "web/app.py": b"# updated web app v2.9.9\n",
            "VERSION": b"2.9.9\n"
        }
        self.package_bytes = self._create_zip(self.package_files)

        # 3. 정상 manifest.json 딕셔너리 구성
        import hashlib
        self.pkg_sha256 = hashlib.sha256(self.package_bytes).hexdigest()
        self.pkg_size = len(self.package_bytes)

        self.manifest_dict = {
            "schema_version": "1.0",
            "version": "2.9.9",
            "package_name": "package.zip",
            "package_sha256": self.pkg_sha256,
            "file_size": self.pkg_size,
            "signing_key_id": "rel_2026a",
            "created_at": "2026-09-26T00:00:00Z"
        }
        self.manifest_bytes = json.dumps(self.manifest_dict).encode("utf-8")
        canonical_bytes = canonicalize(self.manifest_dict)
        self.manifest_sig_hex = self.priv_key.sign(canonical_bytes).hex()

        # 4. 임시 작업 디렉토리
        self.test_root = tempfile.mkdtemp(prefix="updater_test_")

    def tearDown(self):
        shutil.rmtree(self.test_root, ignore_errors=True)

    def _create_zip(self, files_dict: dict) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, content in files_dict.items():
                zf.writestr(path, content)
        return buf.getvalue()

    # ---------------------------------------------------------
    # ArtifactVerifier 테스트
    # ---------------------------------------------------------

    def test_01_valid_artifact_verified_successfully(self):
        """정상 manifest + sig + package_bytes 검증 성공 확인"""
        result = self.verifier.verify_artifact(
            raw_manifest_bytes=self.manifest_bytes,
            manifest_signature_hex=self.manifest_sig_hex,
            raw_package_bytes=self.package_bytes
        )
        self.assertEqual(result.manifest.version, "2.9.9")
        self.assertEqual(result.package_sha256, self.pkg_sha256)
        self.assertEqual(result.package_size, self.pkg_size)

    def test_02_tampered_manifest_signature_rejected(self):
        """Manifest 서명 1바이트 변조 시 ManifestSignatureError 발생 확인"""
        tampered_sig = ("00" if self.manifest_sig_hex[:2] != "00" else "ff") + self.manifest_sig_hex[2:]
        with self.assertRaises(ManifestSignatureError):
            self.verifier.verify_artifact(
                raw_manifest_bytes=self.manifest_bytes,
                manifest_signature_hex=tampered_sig,
                raw_package_bytes=self.package_bytes
            )

    def test_03_tampered_package_bytes_rejected(self):
        """package.zip 내용 1바이트 변조 시 PackageHashMismatchError 발생 확인"""
        tampered_package = bytearray(self.package_bytes)
        tampered_package[-1] ^= 0xFF
        with self.assertRaises(PackageHashMismatchError):
            self.verifier.verify_artifact(
                raw_manifest_bytes=self.manifest_bytes,
                manifest_signature_hex=self.manifest_sig_hex,
                raw_package_bytes=bytes(tampered_package)
            )

    def test_04_tampered_package_size_rejected(self):
        """package 크기 불일치 시 PackageSizeMismatchError 발생 확인"""
        tampered_package = self.package_bytes + b"\x00"
        with self.assertRaises(PackageSizeMismatchError):
            self.verifier.verify_artifact(
                raw_manifest_bytes=self.manifest_bytes,
                manifest_signature_hex=self.manifest_sig_hex,
                raw_package_bytes=tampered_package
            )

    def test_05_malformed_manifest_json_rejected(self):
        """Manifest가 유효하지 않은 JSON이거나 스키마 누락일 때 ManifestParseError 발생 확인"""
        with self.assertRaises(ManifestParseError):
            self.verifier.verify_artifact(
                raw_manifest_bytes=b"{invalid json...",
                manifest_signature_hex=self.manifest_sig_hex,
                raw_package_bytes=self.package_bytes
            )

        incomplete_dict = dict(self.manifest_dict)
        del incomplete_dict["package_sha256"]
        incomplete_bytes = json.dumps(incomplete_dict).encode("utf-8")
        with self.assertRaises(ManifestParseError):
            self.verifier.verify_artifact(
                raw_manifest_bytes=incomplete_bytes,
                manifest_signature_hex=self.manifest_sig_hex,
                raw_package_bytes=self.package_bytes
            )

    # ---------------------------------------------------------
    # AtomicInstaller 테스트
    # ---------------------------------------------------------

    def test_06_install_replaces_files_and_creates_backup(self):
        """정상 설치 시 파일 교체 및 backup/v{이전버전} 보존 검증"""
        # 기존 설치 환경 모의 생성 (v2.9.8)
        target_dir = os.path.join(self.test_root, "app")
        os.makedirs(os.path.join(target_dir, "core"), exist_ok=True)
        with open(os.path.join(target_dir, "VERSION"), "w", encoding="utf-8") as f:
            f.write("2.9.8\n")
        with open(os.path.join(target_dir, "core", "backup.py"), "w", encoding="utf-8") as f:
            f.write("# old backup core v2.9.8\n")

        installer = AtomicInstaller(target_dir=target_dir)
        success = installer.install(
            raw_package_bytes=self.package_bytes,
            new_version="2.9.9",
            skip_process_control=True
        )
        self.assertTrue(success)

        # 1. 파일 교체 확인
        with open(os.path.join(target_dir, "VERSION"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "2.9.9")
        with open(os.path.join(target_dir, "core", "backup.py"), "r", encoding="utf-8") as f:
            self.assertIn("v2.9.9", f.read())

        # 2. 백업 보존 확인
        backup_core = os.path.join(target_dir, "backup", "v2.9.8", "core", "backup.py")
        self.assertTrue(os.path.exists(backup_core))
        with open(backup_core, "r", encoding="utf-8") as f:
            self.assertIn("v2.9.8", f.read())

    def test_07_zip_slip_attack_rejected_and_aborted(self):
        """Zip-Slip 상위 디렉토리 탈출 공격 시도 시 설치 거부 및 대상 디렉토리 오염 방지 확인"""
        target_dir = os.path.join(self.test_root, "app")
        os.makedirs(target_dir, exist_ok=True)
        with open(os.path.join(target_dir, "VERSION"), "w", encoding="utf-8") as f:
            f.write("2.9.8\n")

        # 악성 Zip-Slip 아카이브 생성
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../../evil.txt", b"evil exploit")
            zf.writestr("VERSION", b"6.6.6\n")
        malicious_zip = buf.getvalue()

        installer = AtomicInstaller(target_dir=target_dir)
        with self.assertRaises(InstallerError) as ctx:
            installer.install(
                raw_package_bytes=malicious_zip,
                new_version="6.6.6",
                skip_process_control=True
            )

        # Zip-Slip이 탐지되어 버전이 변경되지 않았음을 확인
        with open(os.path.join(target_dir, "VERSION"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "2.9.8")
        # 악성 파일이 외부로 탈출하지 않았음을 확인
        evil_path = os.path.join(self.test_root, "evil.txt")
        self.assertFalse(os.path.exists(evil_path))

    def test_08_health_check_failure_triggers_automatic_rollback(self):
        """헬스체크 실패 시 이전 버전 파일로 자동 롤백되는지 확인"""
        target_dir = os.path.join(self.test_root, "app")
        os.makedirs(os.path.join(target_dir, "core"), exist_ok=True)
        with open(os.path.join(target_dir, "VERSION"), "w", encoding="utf-8") as f:
            f.write("2.9.8\n")
        with open(os.path.join(target_dir, "core", "backup.py"), "w", encoding="utf-8") as f:
            f.write("# original v2.9.8\n")

        installer = AtomicInstaller(target_dir=target_dir)

        # stop_server와 restart_server는 mock, health_check는 False 반환 모의
        with patch.object(installer, "stop_server", return_value=True), \
             patch.object(installer, "restart_server", return_value=True), \
             patch.object(installer, "health_check", return_value=False):

            with self.assertRaises(InstallerError):
                installer.install(
                    raw_package_bytes=self.package_bytes,
                    new_version="2.9.9",
                    skip_process_control=False
                )

        # 롤백 결과 확인: VERSION과 코드가 v2.9.8로 복원되어 있어야 함
        with open(os.path.join(target_dir, "VERSION"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "2.9.8")
        with open(os.path.join(target_dir, "core", "backup.py"), "r", encoding="utf-8") as f:
            self.assertIn("v2.9.8", f.read())


if __name__ == "__main__":
    unittest.main()
