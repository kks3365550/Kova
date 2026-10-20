# -*- coding: utf-8 -*-
"""
tests/test_drive_archive.py: 드라이브 통백업(WIM) & 4단계 실측 검증 및 VSN 보존 단위 테스트
"""

import os
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from core.drive_archive import (
    DriveArchiveManager,
    get_volume_serial_number,
    set_volume_serial_number,
    ESSENTIAL_BOOT_ARTIFACTS
)


class TestDriveArchiveCore(unittest.TestCase):
    def setUp(self):
        self.manager = DriveArchiveManager.get_instance()
        self.test_dir = tempfile.mkdtemp(prefix="test_drive_archive_")

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_get_volume_serial_number_format(self):
        """실제 또는 폴백 C: 드라이브 VSN 형식이 XXXX-XXXX (8자리 HEX) 형태인지 검증"""
        vsn = get_volume_serial_number("C:")
        if vsn is not None:
            self.assertEqual(len(vsn), 9)
            self.assertEqual(vsn[4], "-")
            self.assertTrue(all(c in "0123456789ABCDEF-" for c in vsn))

    def test_get_volume_serial_number_invalid(self):
        """존재하지 않는 드라이브나 빈 문자열 전달 시 None 반환"""
        self.assertIsNone(get_volume_serial_number(""))
        self.assertIsNone(get_volume_serial_number(None))

    def test_set_volume_serial_number_invalid_format(self):
        """잘못된 VSN 형식 주입 시 즉시 거부 검증"""
        res1 = set_volume_serial_number("C:", "INVALID_VSN")
        self.assertFalse(res1["success"])
        self.assertIn("잘못된 VSN 형식", res1["error"])

        res2 = set_volume_serial_number("C:", "123-456")
        self.assertFalse(res2["success"])

    def test_list_archives_empty_and_populated(self):
        """아카이브 목록 조회 및 메타데이터 파싱 검증"""
        # 1. 빈 디렉토리
        archives = self.manager.list_archives(repo_dir=self.test_dir)
        self.assertEqual(archives, [])

        # 2. 가상 WIM 및 메타데이터 파일 생성
        dummy_wim = os.path.join(self.test_dir, "System_Drive_C_20261015.wim")
        with open(dummy_wim, "wb") as f:
            f.write(b"MSWIM\x00\x00" + b"\x00" * 1024)

        dummy_meta = {
            "source_volume": "C:",
            "volume_serial_number": "5AEF-9960",
            "created_at": "2026-10-15 11:00:00",
            "is_verified": True,
            "verification_stage": 4
        }
        with open(dummy_wim + ".meta.json", "w", encoding="utf-8") as f:
            json.dump(dummy_meta, f)

        archives = self.manager.list_archives(repo_dir=self.test_dir)
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0]["name"], "System_Drive_C_20261015.wim")
        self.assertEqual(archives[0]["vsn"], "5AEF-9960")
        self.assertTrue(archives[0]["is_verified"])
        self.assertEqual(archives[0]["verification_stage"], 4)

    @patch.object(DriveArchiveManager, "run_dism")
    def test_verify_archive_4_stage_success(self, mock_dism):
        """4단계 실측 검증 성공 시나리오: 헤더검사 -> 마운트 -> 5대 아티팩트 점검 -> 언마운트"""
        wim_path = os.path.join(self.test_dir, "test.wim")
        with open(wim_path, "wb") as f:
            f.write(b"MSWIM")

        # 가상 마운트 위치에 5대 아티팩트 생성 시뮬레이션
        def side_effect_mount(args, *a, **kwargs):
            if "/Mount-Image" in args:
                for arg in args:
                    if arg.startswith("/MountDir:"):
                        mdir = arg.split(":", 1)[1]
                        for rel, desc in ESSENTIAL_BOOT_ARTIFACTS:
                            fp = os.path.join(mdir, rel)
                            os.makedirs(os.path.dirname(fp), exist_ok=True)
                            with open(fp, "wb") as f_art:
                                f_art.write(b"TEST_BINARY_ARTIFACT_CONTENT")
            return 0, "SUCCESS"

        mock_dism.side_effect = side_effect_mount

        res = self.manager.verify_archive(wim_path)
        self.assertTrue(res["verified"])
        self.assertEqual(res["stage"], 4)
        self.assertEqual(len(res["artifacts"]), 5)
        for rel, data in res["artifacts"].items():
            self.assertEqual(data["status"], "OK")
            self.assertGreater(data["size_bytes"], 0)

    @patch.object(DriveArchiveManager, "run_dism")
    def test_verify_archive_missing_artifacts(self, mock_dism):
        """3단계 아티팩트 점검 중 핵심 파일(tokens.dat 등) 누락 시 실패 감지 검증"""
        wim_path = os.path.join(self.test_dir, "test_corrupt.wim")
        with open(wim_path, "wb") as f:
            f.write(b"MSWIM")

        def side_effect_mount_partial(args, *a, **kwargs):
            if "/Mount-Image" in args:
                for arg in args:
                    if arg.startswith("/MountDir:"):
                        mdir = arg.split(":", 1)[1]
                        fp = os.path.join(mdir, r"Windows\System32\ntoskrnl.exe")
                        os.makedirs(os.path.dirname(fp), exist_ok=True)
                        with open(fp, "wb") as f_art:
                            f_art.write(b"KERNEL")
            return 0, "SUCCESS"

        mock_dism.side_effect = side_effect_mount_partial

        res = self.manager.verify_archive(wim_path)
        self.assertFalse(res["verified"])
        self.assertIsNotNone(res["error"])
        self.assertIn("필수 부팅 아티팩트 일부 누락", res["error"])

    @patch("core.disk_clone.run_elevated_command")
    def test_worker_capture_flow(self, mock_run_elevated):
        """WIM 캡처 전체 워커 흐름 (VSS 스냅샷 + DISM + VSN 저장 + 실측 검증 연동)"""
        target_wim = os.path.join(self.test_dir, "System_Test.wim")

        def side_effect_worker(cmd, *args, **kwargs):
            ipc_idx = cmd.index("--ipc-file") + 1
            ipc_path = cmd[ipc_idx]
            with open(target_wim, "wb") as f:
                f.write(b"WIM_CAPTURE_DATA")
            meta = {
                "backup_type": "wim",
                "source_volume": "C:",
                "volume_serial_number": "5AEF-9960",
                "target_drive": "C:",
                "compress": "fast",
                "created_at": "2026-10-17 07:00:00",
                "wim_path": target_wim,
                "size_bytes": 16,
                "size_gb": 0.0,
                "is_verified": True,
                "verification_stage": 4,
                "artifacts_checked": {},
                "wim_file": os.path.basename(target_wim)
            }
            with open(target_wim + ".meta.json", "w", encoding="utf-8") as f:
                json.dump(meta, f)
            with open(ipc_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "progress", "percent": 50.0, "phase": "DISM 캡처 중"}) + "\n")
                f.write(json.dumps({"type": "done", "result": meta}) + "\n")
            return 0, "SUCCESS", ""

        mock_run_elevated.side_effect = side_effect_worker

        self.manager._worker_capture("C:", target_wim, compress="fast", auto_verify=True)

        self.assertTrue(os.path.exists(target_wim))
        self.assertTrue(os.path.exists(target_wim + ".meta.json"))

        with open(target_wim + ".meta.json", "r", encoding="utf-8") as f:
            meta = json.load(f)
            self.assertEqual(meta["source_volume"], "C:")
            self.assertEqual(meta["compress"], "fast")
            self.assertTrue(meta["is_verified"])

        st = self.manager.get_status()
        self.assertEqual(st["status"], "completed")
        self.assertEqual(st["progress_percent"], 100.0)

    @patch("core.drive_archive.is_admin", return_value=True)
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("core.drive_archive.set_volume_serial_number")
    def test_worker_restore_flow(self, mock_set_vsn, mock_run, mock_popen, mock_is_admin):
        """WIM 복원 전체 워커 흐름 (DISM Apply-Image + VSN 복원 + bcdboot 주입)"""
        wim_path = os.path.join(self.test_dir, "Restore_Test.wim")
        with open(wim_path, "wb") as f:
            f.write(b"WIM_DATA")

        meta = {"source_volume": "C:", "volume_serial_number": "5AEF-9960"}
        with open(wim_path + ".meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f)

        mock_p = MagicMock()
        mock_p.poll.side_effect = [None, 0]
        mock_p.returncode = 0
        mock_p.wait.return_value = 0
        mock_p.stdout.readline.side_effect = ["[  100.0%  ] 복원 완료", ""]
        mock_popen.return_value = mock_p

        mock_run.return_value = MagicMock(returncode=0, stdout="부팅 파일 생성 완료", stderr="")
        mock_set_vsn.return_value = {"success": True, "method": "raw_vbr_patch", "vsn": "5AEF-9960"}

        self.manager._worker_restore(wim_path, "E:", auto_bcdboot=True, auto_restore_vsn=True)

        mock_set_vsn.assert_called_with("E:", "5AEF-9960")

        st = self.manager.get_status()
        self.assertEqual(st["status"], "completed")
        self.assertEqual(st["progress_percent"], 100.0)

    @patch("core.drive_archive.is_admin", return_value=True)
    @patch("subprocess.Popen")
    def test_run_dism_admin_direct(self, mock_popen, mock_is_admin):
        """관리자 권한 시 direct Popen 스트리밍 실행 검증"""
        mock_p = MagicMock()
        mock_p.poll.side_effect = [None, 0]
        mock_p.returncode = 0
        mock_p.wait.return_value = 0
        mock_p.stdout.readline.side_effect = ["[  20.0%  ] 캡처 중...", ""]
        mock_popen.return_value = mock_p

        logs = []
        code, out = self.manager.run_dism(["/Capture-Image"], log_callback=lambda l: logs.append(l))
        self.assertEqual(code, 0)
        self.assertTrue(any("20.0%" in l for l in logs))

    @patch("core.drive_archive.is_admin", return_value=False)
    @patch("subprocess.run")
    def test_run_dism_non_admin_elevation(self, mock_subproc_run, mock_is_admin):
        """비관리자 권한 시 PowerShell Start-Process -Verb RunAs 승격 프록시 실행 검증"""
        def fake_run(cmd, *a, **kw):
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_subproc_run.side_effect = fake_run

        # We also mock the loop by creating exit file quickly or checking launch
        with patch.object(self.manager, "_run_dism_elevated", return_value=(0, "ELEVATED_SUCCESS")):
            code, out = self.manager.run_dism(["/Capture-Image"])
            self.assertEqual(code, 0)
            self.assertEqual(out, "ELEVATED_SUCCESS")

    @patch("core.drive_archive.is_admin", return_value=True)
    @patch("subprocess.Popen")
    def test_run_wbadmin_admin_direct(self, mock_popen, mock_is_admin):
        """관리자 권한 시 wbadmin 직접 스트리밍 실행 검증"""
        mock_p = MagicMock()
        mock_p.poll.side_effect = [None, 0]
        mock_p.returncode = 0
        mock_p.wait.return_value = 0
        mock_p.stdout.read.side_effect = [b"1", b"5", b"%", b"\r", b""]
        mock_popen.return_value = mock_p

        logs = []
        code, out = self.manager.run_wbadmin("D:", log_callback=lambda l: logs.append(l))
        self.assertEqual(code, 0)
        self.assertTrue(any("15%" in l for l in logs))

    def test_verify_vhdx_archive(self):
        """wbadmin VHDX 4단계 실측 검증 흐름 검증"""
        vhdx_path = os.path.join(self.test_dir, "test_backup.vhdx")
        with open(vhdx_path, "wb") as f:
            f.write(b"VHDX_DATA_HEADER" + b"\x00" * 4096)

        res = self.manager.verify_archive(vhdx_path)
        self.assertTrue(res["verified"])
        self.assertEqual(res["stage"], 4)
        self.assertEqual(len(res["artifacts"]), 5)

    @patch("core.drive_archive.is_admin", return_value=True)
    @patch("core.drive_archive.mount_vhdx_volume", return_value="M:")
    @patch("core.drive_archive.dismount_vhdx_volume", return_value=True)
    @patch("core.block_stream.stream_volume_to_volume", return_value={"success": True, "elapsed_seconds": 1.2, "average_speed_mb_s": 2500.0, "copied_bytes": 1073741824})
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("core.drive_archive.set_volume_serial_number")
    def test_restore_from_vhdx_flow(self, mock_set_vsn, mock_run, mock_popen, mock_stream, mock_dismount, mock_mount, mock_is_admin):
        """VHDX 오프라인 마운트 -> LBA 블록 스트리밍 복제(Robocopy 배제) -> VSN/BCD 주입 -> 언마운트 흐름 검증"""
        vhdx_path = os.path.join(self.test_dir, "Restore_Target.vhdx")
        with open(vhdx_path, "wb") as f:
            f.write(b"VHDX_PAYLOAD" + b"\x00" * 2048)

        meta = {
            "backup_type": "wbadmin_vhdx",
            "vhdx_path": vhdx_path,
            "volume_serial_number": "5AEF-9960"
        }
        with open(vhdx_path + ".meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f)

        mock_run.return_value = MagicMock(returncode=0, stdout="부팅 파일 생성 완료", stderr="")
        mock_set_vsn.return_value = {"success": True, "method": "raw_vbr_patch", "vsn": "5AEF-9960"}

        self.manager._worker_restore(vhdx_path, "E:", auto_bcdboot=True, auto_restore_vsn=True)

        mock_mount.assert_called_with(vhdx_path)
        mock_dismount.assert_called_with(vhdx_path)
        mock_set_vsn.assert_called_with("E:", "5AEF-9960")

        # LBA 블록 스트리밍 복제가 100% 호출되고 Robocopy(subprocess.Popen)는 호출되지 않았음을 검증
        mock_stream.assert_called_once()
        mock_popen.assert_not_called()

        st = self.manager.get_status()
        self.assertEqual(st["status"], "completed")
        self.assertEqual(st["progress_percent"], 100.0)


class TestDriveArchiveAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from web.app import app
        cls.client = TestClient(app)

    def test_api_archive_status(self):
        """GET /api/migration/archive/status 엔드포인트 응답 검증"""
        res = self.client.get("/api/migration/archive/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertIn("status", data)
        self.assertIn("progress_percent", data["status"])

    def test_api_archive_list(self):
        """GET /api/migration/archive/list 엔드포인트 응답 검증"""
        res = self.client.get("/api/migration/archive/list")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertIsInstance(data["archives"], list)

    def test_api_archive_logs(self):
        """GET /api/migration/archive/logs 엔드포인트 응답 검증"""
        res = self.client.get("/api/migration/archive/logs?limit=10")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertIsInstance(data["logs"], list)

    def test_api_archive_verify_missing_file(self):
        """POST /api/migration/archive/verify 에 존재하지 않는 경로 전달 시 실패 결과 반환"""
        res = self.client.post("/api/migration/archive/verify", json={"wim_path": "Z:\\non_existent.wim"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertFalse(data["result"]["verified"])
        self.assertIn("존재하지 않습니다", data["result"]["error"])


if __name__ == "__main__":
    unittest.main()
