# -*- coding: utf-8 -*-
"""
tests/test_missed_backup_and_update.py

Unit tests for:
1. Windows Task Scheduler missed-backup recovery & sleep wake policies (StartWhenAvailable, WakeToRun)
2. PowerSleepGuard Windows OS sleep suspension prevention
3. Web UI 1-Click update endpoints & status integration
"""
import sys
import unittest
from unittest.mock import patch, MagicMock

from core.system_power import PowerSleepGuard
import core.windows_task as wt
from fastapi.testclient import TestClient
from web.app import app


class TestMissedBackupPolicy(unittest.TestCase):
    """Tests Windows Task Scheduler policy enforcement for missed backups."""

    @patch("sys.platform", "win32")
    @patch("core.windows_task._run_schtasks_cmd")
    def test_enhance_task_settings_success(self, mock_run):
        mock_run.return_value = (0, "Ready", "")
        res = wt._enhance_task_settings("TestTask")
        self.assertTrue(res)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertIn("powershell", cmd[0])
        self.assertIn("-StartWhenAvailable", cmd[-1])
        self.assertIn("-WakeToRun", cmd[-1])
        self.assertIn("-AllowStartIfOnBatteries", cmd[-1])

    @patch("sys.platform", "linux")
    def test_enhance_task_settings_non_windows(self):
        res = wt._enhance_task_settings("TestTask")
        self.assertFalse(res)

    @patch("sys.platform", "win32")
    @patch("core.windows_task.get_windows_scheduled_task_status")
    @patch("core.windows_task._enhance_task_settings")
    def test_ensure_missed_backup_policy(self, mock_enhance, mock_status):
        mock_status.return_value = {"registered": True}
        mock_enhance.return_value = True

        self.assertTrue(wt.ensure_missed_backup_policy("TestTask"))
        mock_enhance.assert_called_once_with("TestTask")

        mock_status.return_value = {"registered": False}
        self.assertFalse(wt.ensure_missed_backup_policy("TestTask"))

    @patch("sys.platform", "win32")
    @patch("core.windows_task.get_python_executable", return_value="C:\\python.exe")
    @patch("core.windows_task._run_schtasks_cmd")
    @patch("core.windows_task._enhance_task_settings", return_value=True)
    def test_register_windows_scheduled_task_includes_missed_backup(self, mock_enhance, mock_run, mock_py):
        mock_run.return_value = (0, "SUCCESS", "")
        res = wt.register_windows_scheduled_task(profile_id="p1", schedule_type="daily", schedule_value="09:00")
        self.assertTrue(res["success"])
        self.assertTrue(res.get("missed_backup_recovery"))
        mock_enhance.assert_called_once_with(wt.TASK_NAME)


class TestPowerSleepGuard(unittest.TestCase):
    """Tests Windows kernel execution state suspension lock."""

    def test_power_sleep_guard_context(self):
        with PowerSleepGuard(reason="Test Backup") as guard:
            if sys.platform.startswith("win"):
                self.assertTrue(guard.active)
            else:
                self.assertFalse(guard.active)
        self.assertFalse(guard.active)

    @patch("sys.platform", "win32")
    def test_power_sleep_guard_mocked_success(self):
        mock_kernel32 = MagicMock()
        mock_kernel32.SetThreadExecutionState.return_value = 0x80000001

        with patch("ctypes.windll.kernel32", mock_kernel32):
            guard = PowerSleepGuard(reason="Mocked Windows Backup", keep_display_on=True)
            with guard:
                self.assertTrue(guard.active)
                mock_kernel32.SetThreadExecutionState.assert_called_once()
            self.assertFalse(guard.active)
            self.assertEqual(mock_kernel32.SetThreadExecutionState.call_count, 2)

    @patch("sys.platform", "win32")
    def test_power_sleep_guard_handles_ctypes_failure(self):
        mock_kernel32 = MagicMock()
        mock_kernel32.SetThreadExecutionState.side_effect = RuntimeError("Kernel32 fault")

        with patch("ctypes.windll.kernel32", mock_kernel32):
            guard = PowerSleepGuard(reason="Fault Test")
            with guard:
                self.assertFalse(guard.active)
            self.assertFalse(guard.active)


class TestUpdateUIEndpoints(unittest.TestCase):
    """Tests update endpoints used by the 1-Click Update UI."""

    def setUp(self):
        self.client = TestClient(app)

    @patch("web.api_update.check_for_update")
    @patch("web.api_update.get_current_installed_version", return_value="2.17.47")
    def test_get_update_status_available(self, mock_ver, mock_check):
        mock_check.return_value = {
            "update_available": True,
            "latest_version": "2.18.0",
            "changelog": "부팅 시 놓친 백업 자동 실행 & 1클릭 업데이트",
            "download_url": "https://example.com/update.zip",
            "installer_download_url": "/api/system/download-installer"
        }
        resp = self.client.get("/api/update/status?force_check=true")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("update_available"))
        self.assertEqual(data.get("latest_version"), "2.18.0")
        self.assertIn("1클릭", data.get("changelog"))

    @patch("web.api_update.check_for_update")
    @patch("web.api_update.get_current_installed_version", return_value="2.17.47")
    def test_get_update_status_latest(self, mock_ver, mock_check):
        mock_check.return_value = {
            "update_available": False,
            "latest_version": "2.17.47"
        }
        resp = self.client.get("/api/update/status?force_check=true")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data.get("update_available"))
        self.assertEqual(data.get("current_version"), "2.17.47")

    @patch("core.updater.perform_full_update_pipeline")
    def test_trigger_software_update(self, mock_pipeline):
        mock_pipeline.return_value = {"status": "SUCCESS", "message": "Updated"}
        resp = self.client.post("/api/update/apply")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("success"))


if __name__ == "__main__":
    unittest.main()
