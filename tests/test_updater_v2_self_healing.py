# -*- coding: utf-8 -*-
"""
tests/test_updater_v2_self_healing.py: OTA 자가치유(Self-Healing) 및 크래시 경계 주입(KILL-01 ~ KILL-09) 전수 테스트 스위트

검증 항목:
1. KILL-01 ~ KILL-09: 트랜잭션 경계마다 SIGKILL/전원 차단 모의(HardCrashInterrupt) 주입
   -> 차기 기동(preflight) 단계에서 자동 감지 -> 백업 무결성 검증 -> 롤백 -> 이전 버전(v2.9.10) 안전 복원 -> 마커 정리 -> 정상 기동 허용
2. Fail-Closed 보장: 백업 매니페스트 또는 파일 손상 시 마커를 삭제하지 않고 기동을 원천 차단(FAIL-CLOSED)
3. 클린 기동: 마커 부재 시 0ms Fast-Path 정상 통과
"""

import os
import sys
import io
import json
import zipfile
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.updater_v2.transaction import (
    UpdatePhase,
    UpdateTransactionManager,
    check_and_recover_preflight,
    MARKER_FILENAME,
    BACKUP_MANIFEST_FILENAME
)
from core.updater_v2.installer import AtomicInstaller, InstallerError


class HardCrashInterrupt(BaseException):
    """
    SIGKILL 및 전원 단절을 100% 모의하기 위한 BaseException.
    일반 'except Exception' 블록을 우회하여 디스크 상에 마커와 부분 상태를 그대로 보존.
    """
    pass


class TestUpdaterV2SelfHealing(unittest.TestCase):

    def setUp(self):
        self.test_root = tempfile.mkdtemp(prefix="self_healing_test_")
        self.app_dir = os.path.join(self.test_root, "app")
        os.makedirs(os.path.join(self.app_dir, "core"), exist_ok=True)
        os.makedirs(os.path.join(self.app_dir, "web"), exist_ok=True)

        # Baseline v2.9.10 설치본 구성
        with open(os.path.join(self.app_dir, "VERSION"), "w", encoding="utf-8") as f:
            f.write("2.9.10\n")
        with open(os.path.join(self.app_dir, "core", "backup.py"), "w", encoding="utf-8") as f:
            f.write("# v2.9.10 stable backup logic\n")
        with open(os.path.join(self.app_dir, "run.py"), "w", encoding="utf-8") as f:
            f.write("# v2.9.10 run script\n")

        # Target v2.9.11 업데이트 패키지 생성
        pkg_buf = io.BytesIO()
        with zipfile.ZipFile(pkg_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("core/backup.py", "# v2.9.11 updated backup logic\n".encode("utf-8"))
            zf.writestr("VERSION", "2.9.11\n".encode("utf-8"))
            zf.writestr("run.py", "# v2.9.11 run script\n".encode("utf-8"))
        self.raw_pkg_bytes = pkg_buf.getvalue()

    def tearDown(self):
        shutil.rmtree(self.test_root, ignore_errors=True)

    def test_01_preflight_clean_fastpath_without_marker(self):
        """마커가 없을 때 0ms Fast-Path로 정상 통과하는지 검증"""
        ok, msg = check_and_recover_preflight(self.app_dir)
        self.assertTrue(ok)
        self.assertIn("No pending", msg)

        # 버전 및 파일 불변
        with open(os.path.join(self.app_dir, "VERSION"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "2.9.10")

    def test_02_kill_01_to_09_crash_recovery_matrix(self):
        """
        [핵심 검증] KILL-01부터 KILL-09까지 전수 트랜잭션 경계 크래시 주입 및 자가치유 복원 검증
        KILL-01: 마커 생성 직후
        KILL-02: 백업 완료 직후
        KILL-03: swap(UPDATE_IN_PROGRESS) 시작 직후
        KILL-04: 파일 일부 교체 후
        KILL-05: swap(SWAP_COMPLETE) 완료 직후
        KILL-06: healthcheck 직전
        KILL-07: healthcheck(HEALTHCHECK_OK) 성공 직후
        KILL-08: commit 직전
        KILL-09: 마커 삭제 직전
        """
        kill_steps = [
            "KILL-01",
            "KILL-02",
            "KILL-03",
            "KILL-04",
            "KILL-05",
            "KILL-06",
            "KILL-07",
            "KILL-08",
            "KILL-09",
        ]

        for step in kill_steps:
            with self.subTest(crash_step=step):
                # 1. 환경 재초기화
                self.setUp()
                installer = AtomicInstaller(target_dir=self.app_dir)

                def _crash_hook(hook_id: str, ctx: dict):
                    if hook_id == step:
                        raise HardCrashInterrupt(f"Simulated abrupt SIGKILL/power-cut at {hook_id}")

                # 2. 업데이트 실행 중 해당 경계에서 하드 크래시 발생
                crash_occurred = False
                try:
                    installer.install(
                        raw_package_bytes=self.raw_pkg_bytes,
                        new_version="2.9.11",
                        skip_process_control=True,
                        debug_crash_hook=_crash_hook
                    )
                except HardCrashInterrupt:
                    crash_occurred = True

                self.assertTrue(crash_occurred, f"Crash hook {step} was not triggered!")

                # 3. 크래시 직후 디스크 상태 확인 (KILL-01 이후라면 마커가 디스크에 남아있어야 함)
                marker_path = os.path.join(self.app_dir, MARKER_FILENAME)
                self.assertTrue(os.path.exists(marker_path), f"Marker must exist after crash at {step}")

                # 4. 차기 기동 (run.py preflight) 시 자가 치유(Self-Healing) 실행
                rec_ok, rec_msg = check_and_recover_preflight(self.app_dir)
                self.assertTrue(rec_ok, f"Preflight recovery failed for {step}: {rec_msg}")
                self.assertIn("restored to v2.9.10", rec_msg)

                # 5. 자가 치유 완료 후 검증
                # 5.1 마커가 안전하게 제거되었는가?
                self.assertFalse(os.path.exists(marker_path), f"Marker was not cleaned up after recovery at {step}")

                # 5.2 VERSION 파일이 2.9.10으로 안전 복원되었는가?
                with open(os.path.join(self.app_dir, "VERSION"), "r", encoding="utf-8") as f:
                    self.assertEqual(f.read().strip(), "2.9.10", f"VERSION mismatch after recovery for {step}")

                # 5.3 핵심 파일이 v2.9.10 코드로 보존되었는가?
                with open(os.path.join(self.app_dir, "core", "backup.py"), "r", encoding="utf-8") as f:
                    self.assertIn("v2.9.10", f.read(), f"Code mismatch after recovery for {step}")

    def test_03_fail_closed_on_corrupted_backup_manifest(self):
        """백업 매니페스트가 위변조되거나 손상된 경우 Fail-Closed로 기동을 원천 차단하는지 검증"""
        installer = AtomicInstaller(target_dir=self.app_dir)

        def _crash_hook(hook_id: str, ctx: dict):
            if hook_id == "KILL-04":
                raise HardCrashInterrupt("Crash after partial swap")

        try:
            installer.install(
                raw_package_bytes=self.raw_pkg_bytes,
                new_version="2.9.11",
                skip_process_control=True,
                debug_crash_hook=_crash_hook
            )
        except HardCrashInterrupt:
            pass

        marker_path = os.path.join(self.app_dir, MARKER_FILENAME)
        self.assertTrue(os.path.exists(marker_path))

        with open(marker_path, "r", encoding="utf-8") as f:
            marker_data = json.load(f)

        backup_dir = marker_data["backup_dir"]
        manifest_file = os.path.join(backup_dir, BACKUP_MANIFEST_FILENAME)

        # 악의적 또는 디스크 손상으로 인한 매니페스트 파일 변조 주입
        with open(manifest_file, "w", encoding="utf-8") as f:
            f.write("corrupted content that does not match sha256\n")

        # Preflight 복구 시도 -> FAIL-CLOSED 발동
        rec_ok, rec_msg = check_and_recover_preflight(self.app_dir)
        self.assertFalse(rec_ok)
        self.assertIn("FAIL-CLOSED", rec_msg)

        # 마커가 삭제되지 않고 유지되어야 함 (장애 은폐 방지)
        self.assertTrue(os.path.exists(marker_path))

    def test_04_fail_closed_on_missing_or_tampered_backup_file(self):
        """백업 디렉터리 내의 파일이 삭제되거나 내용이 변조된 경우 Fail-Closed 검증"""
        installer = AtomicInstaller(target_dir=self.app_dir)

        def _crash_hook(hook_id: str, ctx: dict):
            if hook_id == "KILL-03":
                raise HardCrashInterrupt("Crash at swap start")

        try:
            installer.install(
                raw_package_bytes=self.raw_pkg_bytes,
                new_version="2.9.11",
                skip_process_control=True,
                debug_crash_hook=_crash_hook
            )
        except HardCrashInterrupt:
            pass

        marker_path = os.path.join(self.app_dir, MARKER_FILENAME)
        with open(marker_path, "r", encoding="utf-8") as f:
            marker_data = json.load(f)

        backup_dir = marker_data["backup_dir"]
        target_backup_file = os.path.join(backup_dir, "core", "backup.py")

        # 백업 파일 임의 변조
        with open(target_backup_file, "w", encoding="utf-8") as f:
            f.write("# tampered corrupted backup bytes\n")

        rec_ok, rec_msg = check_and_recover_preflight(self.app_dir)
        self.assertFalse(rec_ok)
        self.assertIn("FAIL-CLOSED", rec_msg)
        self.assertTrue(os.path.exists(marker_path))


if __name__ == "__main__":
    unittest.main()
