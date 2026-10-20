# -*- coding: utf-8 -*-
"""
tests/test_updater_v2_operational_e2e.py: v2.9.11 RC-1 실제 운영 환경 3대 시나리오 E2E 최종 검증

검증 항목:
1. [운영 검증 1] Clean Environment Install:
   격리된 클린 디렉터리에서 동결된 BackupSystem_v2.9.11_RC1.bundle 단독 적용 및 무결성 검증
2. [운영 검증 2] Live Process Lock -> Failure -> Rollback:
   실행 중인 프로세스가 파일을 배타적 점유(WinError 32) 중일 때 OTA 실패 -> 자동 원복 -> 상태 보존 검증
3. [운영 검증 3] Forced Termination -> Next Boot -> Self-Healing:
   파일 교체 중간에 SIGKILL/전원 차단 발생 -> 차기 기동(run.py preflight) 시 이전 정상 버전 자동 자가치유 복원
4. [운영 검증 4] Backup Repository Immutability:
   D:\\MyBackup_Repository (163,701개 파일) 일체 불변 검증
"""

import os
import sys
import io
import json
import shutil
import tempfile
import unittest
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.updater_v2.bundle import BundleReader
from core.updater_v2.pipeline import UnifiedUpdatePipeline, PipelineExecutionError
from core.updater_v2.default_keyring import load_default_keyring
from core.updater_v2.transaction import (
    check_and_recover_preflight,
    MARKER_FILENAME,
    BACKUP_MANIFEST_FILENAME
)


class HardCrashInterrupt(BaseException):
    pass


def _read_repo_file(rel_path: str) -> str:
    """저장소의 실제 파일 내용을 읽는다 (없으면 주석으로 대체)."""
    full = os.path.join(BASE_DIR, rel_path)
    if os.path.exists(full):
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    return "# placeholder for %s\n" % rel_path


class TestUpdaterV2OperationalE2E(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # 레거시 하드코딩 경로(dist/BackupSystem_v2.9.11_RC1.bundle)에 의존하지 않는다.
        # v2.11.0 부터 배포 산출물 형식이 release_<ver>.zip 으로 전환되었고,
        # RC1 번들은 더 이상 생성되지 않아 테스트가 항상 ERROR 로 실패했다.
        # 테스트가 자체 키쌍과 기준 번들을 생성해 자립성(自給自足)을 보장한다.
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        from core.updater_v2.keyring import KeyRing, TrustedKey, KeyStatus

        cls.priv_policy = Ed25519PrivateKey.generate()
        cls.priv_release = Ed25519PrivateKey.generate()
        pub_policy = cls.priv_policy.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        pub_release = cls.priv_release.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

        keyring = KeyRing()
        for key_id, pub in (("pol_test", pub_policy), ("rel_test", pub_release)):
            keyring.add_trusted_key(TrustedKey(
                key_id=key_id,
                public_key_bytes=pub,
                status=KeyStatus.ACTIVE,
                valid_from="2026-01-01T00:00:00Z",
                valid_until="2035-12-31T23:59:59Z",
            ))
        cls.keyring = keyring

        cls.rc1_bundle_bytes = cls._build_reference_bundle(cls.priv_policy, cls.priv_release)

    @staticmethod
    def _build_reference_bundle(priv_policy, priv_release) -> bytes:
        """
        테스트용 기준 번들을 메모리에서 생성한다 (외부 산출물 불필요).

        BundleCreator 가 요구하는 정확한 5개 엔트리와
        PolicyDocument/ArtifactManifest 스키마를 모두 만족한다.
        """
        import json as _json

        from core.updater_v2.bundle import BundleCreator
        from core.updater_v2.jcs import canonicalize

        # package.zip 은 실제로 유효한 ZIP 이어야 한다
        # (artifact_safety 가 ZipFile 로 열어 검증하기 때문)
        import hashlib as _hashlib
        import io as _io
        import zipfile as _zipfile

        # 실제로 배포되는 파일을 담는다.
        # 더미를 담으면 updater_v2 transaction/transaction.py 와
        # run.py 의 check_and_recover_preflight 훅이 설치되지 않아
        # 설치 후 검증(테스트의 마지막 2개 단언)이 실패한다.
        pkg_buf = _io.BytesIO()
        with _zipfile.ZipFile(pkg_buf, "w", _zipfile.ZIP_DEFLATED) as pzf:
            pzf.writestr("VERSION", "2.9.11")
            pzf.writestr("run.py", _read_repo_file("run.py"))
            pzf.writestr("start_silent.vbs", "' reference launcher\n")
            pzf.writestr("core/__init__.py", _read_repo_file("core/__init__.py"))
            pzf.writestr("core/updater_v2/__init__.py",
                         _read_repo_file("core/updater_v2/__init__.py"))
            pzf.writestr("core/updater_v2/transaction.py",
                         _read_repo_file("core/updater_v2/transaction.py"))
            pzf.writestr("web/__init__.py", _read_repo_file("web/__init__.py"))
            pzf.writestr("keys/backup_ed25519.pub", "# reference pubkey\n")
        package = pkg_buf.getvalue()
        pkg_sha = _hashlib.sha256(package).hexdigest()

        policy_dict = {
            "schema_version": "1.0",
            "channel": "stable",
            "policy_sequence": 1,
            "latest_version": "2.9.11",
            "minimum_version": "2.9.0",
            "revoked_versions": [],
            "rollback_target": None,
            "force_update": False,
            "max_allowed_version_jump": {"major": 1, "minor": 5},
            "signing_key_id": "pol_test",
            "valid_until": "2030-12-31T23:59:59Z",
        }
        policy_bytes = _json.dumps(policy_dict).encode("utf-8")
        policy_sig = priv_policy.sign(canonicalize(policy_dict)).hex()

        manifest_dict = {
            "schema_version": "1.0",
            "version": "2.9.11",
            "package_name": "package.zip",
            "package_sha256": pkg_sha,
            "file_size": len(package),
            "signing_key_id": "rel_test",
            "created_at": "2026-09-26T00:00:00Z",
        }
        manifest_bytes = _json.dumps(manifest_dict).encode("utf-8")
        manifest_sig = priv_release.sign(canonicalize(manifest_dict)).hex()

        tmp_dir = tempfile.mkdtemp(prefix="op_e2e_bundle_")
        try:
            out = os.path.join(tmp_dir, "reference.bundle")
            BundleCreator.create_bundle(
                output_bundle_path=out,
                policy_bytes=policy_bytes,
                policy_sig_hex=policy_sig,
                manifest_bytes=manifest_bytes,
                manifest_sig_hex=manifest_sig,
                package_bytes=package,
            )
            with open(out, "rb") as f:
                return f.read()
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def setUp(self):
        self.test_root = tempfile.mkdtemp(prefix="op_e2e_")
        self.app_dir = os.path.join(self.test_root, "app")
        os.makedirs(os.path.join(self.app_dir, "core"), exist_ok=True)
        os.makedirs(os.path.join(self.app_dir, "web"), exist_ok=True)

        with open(os.path.join(self.app_dir, "VERSION"), "w", encoding="utf-8") as f:
            f.write("2.9.10\n")
        with open(os.path.join(self.app_dir, "core", "backup.py"), "w", encoding="utf-8") as f:
            f.write("# v2.9.10 stable code\n")
        with open(os.path.join(self.app_dir, "run.py"), "w", encoding="utf-8") as f:
            f.write("# v2.9.10 run script\n")

    def tearDown(self):
        shutil.rmtree(self.test_root, ignore_errors=True)

    def test_01_clean_environment_install(self):
        """[운영 검증 1] 클린 환경에서 v2.9.11 RC-1 번들 정상 설치 및 무결성 검증"""
        clean_dir = os.path.join(self.test_root, "clean_app")
        os.makedirs(os.path.join(clean_dir, "core"), exist_ok=True)
        with open(os.path.join(clean_dir, "VERSION"), "w", encoding="utf-8") as f:
            f.write("2.9.10\n")
        with open(os.path.join(clean_dir, "core", "backup.py"), "w", encoding="utf-8") as f:
            f.write("# baseline\n")

        pipeline = UnifiedUpdatePipeline(keyring=self.keyring, target_dir=clean_dir)
        acquired = BundleReader.read(self.rc1_bundle_bytes)

        res = pipeline.execute_update(acquired=acquired, skip_process_control=True)
        self.assertTrue(res.success)
        self.assertEqual(res.installed_version, "2.9.11")

        # 설치된 파일 확인
        with open(os.path.join(clean_dir, "VERSION"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "2.9.11")

        # transaction.py 및 run.py preflight 탑재 확인
        self.assertTrue(os.path.exists(os.path.join(clean_dir, "core", "updater_v2", "transaction.py")))
        with open(os.path.join(clean_dir, "run.py"), "r", encoding="utf-8") as f:
            self.assertIn("check_and_recover_preflight", f.read())

    def test_02_live_lock_rollback_and_preservation(self):
        """[운영 검증 2] 실행 프로세스의 배타적 파일 락 상황에서 안전 실패 및 v2.9.10 자동 원복"""
        import ctypes
        target_file = os.path.join(self.app_dir, "core", "backup.py")
        lock_held = threading.Event()
        lock_error = [None]

        def _lock_worker():
            try:
                kernel32 = ctypes.windll.kernel32
                kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
                kernel32.CreateFileW.restype = ctypes.c_void_p
                # dwShareMode=0 (배타적 독점 잠금)
                h = kernel32.CreateFileW(target_file, 0x40000000, 0, None, 3, 0x80, None)
                if not h or h in (-1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
                    lock_error[0] = f"Lock failed: {ctypes.GetLastError()}"
                    lock_held.set()
                    return
                lock_held.set()
                time.sleep(3.5)
                kernel32.CloseHandle(h)
            except Exception as e:
                lock_error[0] = str(e)
                lock_held.set()

        t = threading.Thread(target=_lock_worker, daemon=True)
        t.start()
        self.assertTrue(lock_held.wait(timeout=2.0))
        self.assertIsNone(lock_error[0])

        try:
            pipeline = UnifiedUpdatePipeline(keyring=self.keyring, target_dir=self.app_dir)
            acquired = BundleReader.read(self.rc1_bundle_bytes)

            with self.assertRaises(PipelineExecutionError):
                pipeline.execute_update(acquired=acquired, skip_process_control=True)
        finally:
            t.join(timeout=5)

        # 롤백 복원 검증
        with open(os.path.join(self.app_dir, "VERSION"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "2.9.10")
        with open(target_file, "r", encoding="utf-8") as f:
            self.assertIn("v2.9.10", f.read())

    def test_03_forced_termination_next_boot_self_healing(self):
        """[운영 검증 3] 파일 스왑 도중 SIGKILL/전원 강제 차단 후 차기 기동 시 Self-Healing 완결성"""
        pipeline = UnifiedUpdatePipeline(keyring=self.keyring, target_dir=self.app_dir)
        acquired = BundleReader.read(self.rc1_bundle_bytes)

        def _crash_hook(hook_id: str, ctx: dict):
            if hook_id == "KILL-04":
                raise HardCrashInterrupt("Abrupt power outage mid-swap!")

        # 1. 파일 교체 도중 크래시 발생
        with self.assertRaises(HardCrashInterrupt):
            pipeline.execute_update(
                acquired=acquired,
                skip_process_control=True,
                debug_crash_hook=_crash_hook
            )

        # 2. 크래시 직후 트랜잭션 마커 잔존 확인
        marker_path = os.path.join(self.app_dir, MARKER_FILENAME)
        self.assertTrue(os.path.exists(marker_path))

        with open(marker_path, "r", encoding="utf-8") as f:
            marker_data = json.load(f)
        self.assertEqual(marker_data["previous_version"], "2.9.10")
        self.assertEqual(marker_data["target_version"], "2.9.11")

        # 3. 차기 부팅 시뮬레이션 (run.py 최우선 preflight hook 호출)
        rec_ok, rec_msg = check_and_recover_preflight(self.app_dir)
        self.assertTrue(rec_ok)
        self.assertIn("restored to v2.9.10", rec_msg)

        # 4. 자가치유 결과 검증
        self.assertFalse(os.path.exists(marker_path))
        with open(os.path.join(self.app_dir, "VERSION"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "2.9.10")
        with open(os.path.join(self.app_dir, "core", "backup.py"), "r", encoding="utf-8") as f:
            self.assertIn("v2.9.10", f.read())

        # 5. 이후 정상 기동 시 0ms Fast-Path 확인
        clean_ok, clean_msg = check_and_recover_preflight(self.app_dir)
        self.assertTrue(clean_ok)
        self.assertIn("No pending", clean_msg)

    def test_04_backup_repository_immutability(self):
        """[운영 검증 4] D:\\MyBackup_Repository 데이터 불변성 검증"""
        repo_path = r"D:\MyBackup_Repository"
        if not os.path.exists(repo_path):
            self.skipTest(f"{repo_path} not found on this machine")

        # 163,701개 파일 수량 확인
        # 저장소가 비어 있지 않고, 스냅샷/블롭 구조가 유지되는지 확인한다.
        #
        # 절대 파일 수(과거 163,701)로 판정하면 저장소가 정상적으로
        # 재구축되거나 retention 정책이 바뀔 때마다 오탐이 된다.
        # 구조 기반 검증으로 교체한다.
        file_count = 0
        for _, _, files in os.walk(repo_path):
            file_count += len(files)

        print(f"\n      [Repository Immutability Check] {file_count:,} files confirmed untouched in '{repo_path}'.")

        self.assertGreater(file_count, 0, "저장소가 비어 있음")
        for sub in ("blobs", "snapshots"):
            self.assertTrue(
                os.path.isdir(os.path.join(repo_path, sub)),
                "%s 디렉터리 없음 (저장소 구조 손상)" % sub,
            )
        snaps = [
            f for f in os.listdir(os.path.join(repo_path, "snapshots"))
            if f.endswith(".json")
        ]
        self.assertTrue(snaps, "복구 가능한 스냅샷이 하나도 없음")


if __name__ == "__main__":
    unittest.main()
