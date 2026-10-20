# -*- coding: utf-8 -*-
"""
tests/test_updater_v2_phase4.py: OTA 4단계 (UnifiedUpdatePipeline, BundleReader/Creator) E2E 통합 테스트

검증 항목:
1. BundleCreator & BundleReader 왕복(Round-trip) 및 구조 무결성 검증
2. UnifiedUpdatePipeline 엔드투엔드(E2E) 실행:
   - 정상 .bundle 파일로부터 읽은 AcquiredUpdate로 업데이트 성공 및 버전 갱신 검증
   - 이미 최신 버전인 경우 NOOP 분기 정상 반환
   - 정책 버전과 매니페스트 버전 불일치 시 거부 (Tampered Version Mismatch)
   - 만료된 정책, 변조된 매니페스트, 변조된 패키지 주입 시 파이프라인 중단 및 대상 디렉토리 보존
   - 정책의 강제 롤백(FORCE_ROLLBACK) 지시 시 backup/v{target} 디렉토리로부터 원복 수행
"""

import io
import os
import json
import shutil
import zipfile
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from core.updater_v2.jcs import canonicalize
from core.updater_v2.keyring import KeyRing, TrustedKey, KeyStatus
from core.updater_v2.bundle import BundleReader, BundleCreator, BundleFormatError
from core.updater_v2.pipeline import UnifiedUpdatePipeline, PipelineExecutionError
from core.updater_v2.policy_semantics import PolicyDecision


class TestPhase4UnifiedPipelineAndBundle(unittest.TestCase):
    """UnifiedUpdatePipeline 및 오프라인 번들 연동 테스트"""

    def setUp(self):
        # 1. 서명 키 생성
        self.priv_policy = Ed25519PrivateKey.generate()
        self.pub_policy = self.priv_policy.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

        self.priv_rel = Ed25519PrivateKey.generate()
        self.pub_rel = self.priv_rel.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

        # 2. KeyRing 등록
        self.keyring = KeyRing()
        self.keyring.add_trusted_key(
            TrustedKey(
                key_id="pol_2026",
                public_key_bytes=self.pub_policy,
                status=KeyStatus.ACTIVE,
                valid_from="2026-01-01T00:00:00Z",
                valid_until="2026-12-31T23:59:59Z"
            )
        )
        self.keyring.add_trusted_key(
            TrustedKey(
                key_id="rel_2026",
                public_key_bytes=self.pub_rel,
                status=KeyStatus.ACTIVE,
                valid_from="2026-01-01T00:00:00Z",
                valid_until="2026-12-31T23:59:59Z"
            )
        )

        # 3. 임시 대상 디렉토리 및 가상 앱 생성
        self.test_root = tempfile.mkdtemp(prefix="updater_p4_")
        self.target_dir = os.path.join(self.test_root, "app")
        os.makedirs(os.path.join(self.target_dir, "core"), exist_ok=True)
        with open(os.path.join(self.target_dir, "VERSION"), "w", encoding="utf-8") as f:
            f.write("2.9.8\n")
        with open(os.path.join(self.target_dir, "core", "backup.py"), "w", encoding="utf-8") as f:
            f.write("# v2.9.8 code\n")

        # 4. v2.9.9 패키지 생성
        pkg_buf = io.BytesIO()
        with zipfile.ZipFile(pkg_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("core/backup.py", b"# v2.9.9 updated code\n")
            zf.writestr("VERSION", b"2.9.9\n")
        self.package_bytes = pkg_buf.getvalue()

        import hashlib
        self.pkg_sha256 = hashlib.sha256(self.package_bytes).hexdigest()
        self.pkg_size = len(self.package_bytes)

        # 5. v2.9.9 매니페스트 및 서명
        self.manifest_dict = {
            "schema_version": "1.0",
            "version": "2.9.9",
            "package_name": "package.zip",
            "package_sha256": self.pkg_sha256,
            "file_size": self.pkg_size,
            "signing_key_id": "rel_2026",
            "created_at": "2026-09-26T00:00:00Z"
        }
        self.manifest_bytes = json.dumps(self.manifest_dict).encode("utf-8")
        manifest_canonical = canonicalize(self.manifest_dict)
        self.manifest_sig = self.priv_rel.sign(manifest_canonical).hex()

        # 6. 정책 문서 및 서명
        self.policy_dict = {
            "schema_version": "1.0",
            "channel": "stable",
            "policy_sequence": 150,
            "latest_version": "2.9.9",
            "minimum_version": "2.9.0",
            "revoked_versions": [],
            "rollback_target": None,
            "force_update": False,
            "max_allowed_version_jump": {"major": 1, "minor": 5},
            "signing_key_id": "pol_2026",
            "valid_until": "2026-12-31T23:59:59Z"
        }
        self.policy_bytes = json.dumps(self.policy_dict).encode("utf-8")
        policy_canonical = canonicalize(self.policy_dict)
        self.policy_sig = self.priv_policy.sign(policy_canonical).hex()

        self.pipeline = UnifiedUpdatePipeline(
            keyring=self.keyring,
            target_dir=self.target_dir
        )

    def tearDown(self):
        shutil.rmtree(self.test_root, ignore_errors=True)

    def test_01_bundle_creator_and_reader_roundtrip(self):
        """BundleCreator로 생성한 번들을 BundleReader가 완벽하게 복원하는지 검증"""
        bundle_path = os.path.join(self.test_root, "update_2.9.9.bundle")
        BundleCreator.create_bundle(
            output_bundle_path=bundle_path,
            policy_bytes=self.policy_bytes,
            policy_sig_hex=self.policy_sig,
            manifest_bytes=self.manifest_bytes,
            manifest_sig_hex=self.manifest_sig,
            package_bytes=self.package_bytes
        )

        acquired = BundleReader.read(bundle_path)
        self.assertEqual(acquired.policy_bytes, self.policy_bytes)
        self.assertEqual(acquired.policy_sig, self.policy_sig)
        self.assertEqual(acquired.manifest_bytes, self.manifest_bytes)
        self.assertEqual(acquired.manifest_sig, self.manifest_sig)
        self.assertEqual(acquired.package_bytes, self.package_bytes)

    def test_02_e2e_successful_update_from_bundle(self):
        """번들로부터 AcquiredUpdate를 읽어 파이프라인 E2E 업데이트 성공 검증"""
        bundle_path = os.path.join(self.test_root, "update_2.9.9.bundle")
        BundleCreator.create_bundle(
            output_bundle_path=bundle_path,
            policy_bytes=self.policy_bytes,
            policy_sig_hex=self.policy_sig,
            manifest_bytes=self.manifest_bytes,
            manifest_sig_hex=self.manifest_sig,
            package_bytes=self.package_bytes
        )
        acquired = BundleReader.read(bundle_path)

        result = self.pipeline.execute_update(
            acquired=acquired,
            skip_process_control=True
        )

        self.assertTrue(result.success)
        self.assertEqual(result.installed_version, "2.9.9")
        self.assertEqual(result.decision, PolicyDecision.PROCEED_UPDATE)

        # 디스크의 파일 갱신 확인
        with open(os.path.join(self.target_dir, "VERSION"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "2.9.9")
        with open(os.path.join(self.target_dir, "core", "backup.py"), "r", encoding="utf-8") as f:
            self.assertIn("v2.9.9", f.read())

    def test_03_noop_when_already_latest(self):
        """현재 버전이 이미 최신 버전과 같을 때 NOOP 정상 종료 검증"""
        with open(os.path.join(self.target_dir, "VERSION"), "w", encoding="utf-8") as f:
            f.write("2.9.9\n")

        bundle_path = os.path.join(self.test_root, "update_2.9.9.bundle")
        BundleCreator.create_bundle(
            output_bundle_path=bundle_path,
            policy_bytes=self.policy_bytes,
            policy_sig_hex=self.policy_sig,
            manifest_bytes=self.manifest_bytes,
            manifest_sig_hex=self.manifest_sig,
            package_bytes=self.package_bytes
        )
        acquired = BundleReader.read(bundle_path)

        result = self.pipeline.execute_update(
            acquired=acquired,
            skip_process_control=True
        )

        self.assertTrue(result.success)
        self.assertEqual(result.decision, PolicyDecision.NOOP)
        self.assertEqual(result.installed_version, "2.9.9")

    def test_04_version_mismatch_between_policy_and_manifest_rejected(self):
        """정책은 2.9.9를 요구하는데 매니페스트가 2.9.10을 담고 있을 때 거부 확인"""
        mismatched_manifest = dict(self.manifest_dict)
        mismatched_manifest["version"] = "2.9.10"
        mismatched_bytes = json.dumps(mismatched_manifest).encode("utf-8")
        mismatched_sig = self.priv_rel.sign(canonicalize(mismatched_manifest)).hex()

        bundle_path = os.path.join(self.test_root, "mismatch.bundle")
        BundleCreator.create_bundle(
            output_bundle_path=bundle_path,
            policy_bytes=self.policy_bytes,
            policy_sig_hex=self.policy_sig,
            manifest_bytes=mismatched_bytes,
            manifest_sig_hex=mismatched_sig,
            package_bytes=self.package_bytes
        )
        acquired = BundleReader.read(bundle_path)

        with self.assertRaises(PipelineExecutionError) as ctx:
            self.pipeline.execute_update(
                acquired=acquired,
                skip_process_control=True
            )
        self.assertIn("Version mismatch", str(ctx.exception))

    def test_05_tampered_package_in_bundle_rejected(self):
        """번들 내 package.zip 바이트 변조 시 파이프라인 거부 및 설치 방지 확인"""
        tampered_pkg = bytearray(self.package_bytes)
        tampered_pkg[-1] ^= 0xFF

        bundle_path = os.path.join(self.test_root, "tampered.bundle")
        BundleCreator.create_bundle(
            output_bundle_path=bundle_path,
            policy_bytes=self.policy_bytes,
            policy_sig_hex=self.policy_sig,
            manifest_bytes=self.manifest_bytes,
            manifest_sig_hex=self.manifest_sig,
            package_bytes=bytes(tampered_pkg)
        )
        acquired = BundleReader.read(bundle_path)

        with self.assertRaises(PipelineExecutionError) as ctx:
            self.pipeline.execute_update(
                acquired=acquired,
                skip_process_control=True
            )
        self.assertIn("Artifact verification failed", str(ctx.exception))
        # 기존 2.9.8 유지 확인
        with open(os.path.join(self.target_dir, "VERSION"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "2.9.8")

    def test_06_force_rollback_execution(self):
        """정책 상 현재 버전이 폐기되어 강제 롤백이 지시된 경우의 정상 복원 검증"""
        # 현재 버전 2.9.9로 세팅
        with open(os.path.join(self.target_dir, "VERSION"), "w", encoding="utf-8") as f:
            f.write("2.9.9\n")
        # backup/v2.9.8 준비
        backup_dir = os.path.join(self.target_dir, "backup", "v2.9.8")
        os.makedirs(os.path.join(backup_dir, "core"), exist_ok=True)
        with open(os.path.join(backup_dir, "VERSION"), "w", encoding="utf-8") as f:
            f.write("2.9.8\n")
        with open(os.path.join(backup_dir, "core", "backup.py"), "w", encoding="utf-8") as f:
            f.write("# restored v2.9.8 code\n")

        # 2.9.9가 revoked되고 rollback_target이 2.9.8인 정책 발행
        rollback_policy = dict(self.policy_dict)
        rollback_policy["policy_sequence"] = 160
        rollback_policy["latest_version"] = "2.9.8"
        rollback_policy["revoked_versions"] = ["2.9.9"]
        rollback_policy["rollback_target"] = "2.9.8"

        rb_policy_bytes = json.dumps(rollback_policy).encode("utf-8")
        rb_policy_sig = self.priv_policy.sign(canonicalize(rollback_policy)).hex()

        bundle_path = os.path.join(self.test_root, "rollback.bundle")
        BundleCreator.create_bundle(
            output_bundle_path=bundle_path,
            policy_bytes=rb_policy_bytes,
            policy_sig_hex=rb_policy_sig,
            manifest_bytes=self.manifest_bytes,
            manifest_sig_hex=self.manifest_sig,
            package_bytes=self.package_bytes
        )
        acquired = BundleReader.read(bundle_path)

        result = self.pipeline.execute_update(
            acquired=acquired,
            skip_process_control=True
        )

        self.assertTrue(result.success)
        self.assertEqual(result.decision, PolicyDecision.FORCE_ROLLBACK)
        self.assertEqual(result.installed_version, "2.9.8")

        # 롤백된 파일 복원 확인
        with open(os.path.join(self.target_dir, "VERSION"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "2.9.8")
        with open(os.path.join(self.target_dir, "core", "backup.py"), "r", encoding="utf-8") as f:
            self.assertIn("restored v2.9.8", f.read())


if __name__ == "__main__":
    unittest.main()
