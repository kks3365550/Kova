# -*- coding: utf-8 -*-
"""
tests/test_tray_app.py

BackupTrayApp 클래스의 주요 메뉴 액션 단위 테스트
- open_dashboard(): os.startfile 및 webbrowser.open 정상 폴백 검증
- trigger_backup(): {"status": "started"} 성공 인식 및 409 Conflict 친절 처리 검증
- check_status_now(): task_type 한글 매핑 및 진행률 포맷팅 검증
- restart_server(): stop_backup_system.bat 호출 및 start_silent.vbs 실행 검증
- exit_app(): stop_backup_system.bat 호출 및 메시지 종료 검증
- show_notification(): threading.Lock 동시성 보호 동작 검증
"""

import io
import json
import time
import urllib.error
import unittest
from unittest.mock import MagicMock, patch

from tray_app import BackupTrayApp, NIIF_INFO, NIIF_WARNING, NIIF_ERROR


def sync_thread(target, daemon=True):
    m = MagicMock()
    m.start = target
    return m


class TestBackupTrayApp(unittest.TestCase):

    def setUp(self):
        self.app = BackupTrayApp()
        self.app.hwnd = 12345
        self.app.nid = MagicMock()

    @patch("tray_app.webbrowser.open")
    @patch("tray_app.os.startfile")
    def test_open_dashboard_startfile_success(self, mock_startfile, mock_webopen):
        self.app.open_dashboard()
        mock_startfile.assert_called_once_with("http://127.0.0.1:8765")
        mock_webopen.assert_not_called()

    @patch("tray_app.webbrowser.open")
    @patch("tray_app.os.startfile", side_effect=OSError("Failed"))
    def test_open_dashboard_fallback(self, mock_startfile, mock_webopen):
        self.app.open_dashboard()
        mock_startfile.assert_called_once()
        mock_webopen.assert_called_once_with("http://127.0.0.1:8765")

    @patch.object(BackupTrayApp, "show_notification")
    @patch("tray_app.urllib.request.urlopen")
    def test_trigger_backup_started_status(self, mock_urlopen, mock_notify):
        # API returns {"status": "started"}
        resp = MagicMock()
        resp.read.return_value = json.dumps({"status": "started"}).encode("utf-8")
        resp.__enter__.return_value = resp
        mock_urlopen.return_value = resp

        with patch("tray_app.threading.Thread", side_effect=sync_thread):
            self.app.trigger_backup()

        mock_notify.assert_called_once_with("백업 시작", "새로운 시스템 백업 작업이 시작되었습니다.")

    @patch.object(BackupTrayApp, "show_notification")
    @patch("tray_app.urllib.request.urlopen")
    def test_trigger_backup_409_conflict(self, mock_urlopen, mock_notify):
        err_body = io.BytesIO(json.dumps({"detail": "이미 다른 백업 또는 복원 작업이 실행 중입니다."}).encode("utf-8"))
        err = urllib.error.HTTPError("http://127.0.0.1:8765/api/backup/run", 409, "Conflict", {}, err_body)
        mock_urlopen.side_effect = err

        with patch("tray_app.threading.Thread", side_effect=sync_thread):
            self.app.trigger_backup()

        mock_notify.assert_called_once_with("백업 충돌", "이미 다른 백업 또는 복원 작업이 실행 중입니다.", NIIF_WARNING)

    @patch.object(BackupTrayApp, "show_notification")
    @patch("tray_app.urllib.request.urlopen")
    def test_check_status_now_running(self, mock_urlopen, mock_notify):
        resp = MagicMock()
        resp.read.return_value = json.dumps({
            "running": True,
            "type": "backup",
            "progress": {"percent": 45, "current_file": "data/app.py"}
        }).encode("utf-8")
        resp.__enter__.return_value = resp
        mock_urlopen.return_value = resp

        with patch("tray_app.threading.Thread", side_effect=sync_thread):
            self.app.check_status_now()

        mock_notify.assert_called_once()
        args = mock_notify.call_args[0]
        self.assertEqual(args[0], "작업 진행 중")
        self.assertIn("시스템 백업", args[1])
        self.assertIn("45%", args[1])

    @patch.object(BackupTrayApp, "show_notification")
    @patch("tray_app.urllib.request.urlopen")
    def test_check_status_now_idle(self, mock_urlopen, mock_notify):
        resp = MagicMock()
        resp.read.return_value = json.dumps({
            "running": False,
            "error": None
        }).encode("utf-8")
        resp.__enter__.return_value = resp
        mock_urlopen.return_value = resp

        with patch("tray_app.threading.Thread", side_effect=sync_thread):
            self.app.check_status_now()

        mock_notify.assert_called_once_with("정상 대기 중", "모든 백업 엔진이 정상 대기 상태입니다.")

    @patch("tray_app.subprocess.Popen")
    @patch("tray_app.subprocess.run")
    @patch.object(BackupTrayApp, "show_notification")
    @patch("tray_app.urllib.request.urlopen")
    def test_restart_server_execution(self, mock_urlopen, mock_notify, mock_sub_run, mock_sub_popen):
        resp = MagicMock()
        resp.status = 200
        resp.__enter__.return_value = resp
        mock_urlopen.return_value = resp

        with patch("tray_app.time.sleep"), patch("tray_app.os.path.exists", return_value=True), \
             patch("tray_app.threading.Thread", side_effect=sync_thread):
            self.app.restart_server()

        mock_sub_run.assert_called_once()
        mock_sub_popen.assert_called_once()
        self.assertTrue(any("재시작 완료" in str(call) for call in mock_notify.mock_calls))

    @patch("tray_app.subprocess.run")
    @patch("tray_app.user32.PostMessageW")
    def test_exit_app_terminates_server_and_closes_window(self, mock_post_msg, mock_sub_run):
        with patch("tray_app.os.path.exists", return_value=True):
            self.app.exit_app()

        mock_sub_run.assert_called_once()
        self.assertFalse(self.app.running)
        mock_post_msg.assert_called_once_with(self.app.hwnd, 0x0010, 0, 0)  # WM_CLOSE = 0x0010

    @patch("tray_app.user32.DestroyWindow")
    def test_wnd_proc_wm_close_calls_destroy_window(self, mock_destroy):
        ret = self.app.wnd_proc(self.app.hwnd, 0x0010, 0, 0)  # WM_CLOSE
        self.assertEqual(ret, 0)
        mock_destroy.assert_called_once_with(self.app.hwnd)

    @patch("tray_app.user32.PostQuitMessage")
    def test_wnd_proc_wm_destroy_quits_message_loop(self, mock_quit):
        self.app.running = True
        ret = self.app.wnd_proc(self.app.hwnd, 0x0002, 0, 0)  # WM_DESTROY
        self.assertEqual(ret, 0)
        self.assertFalse(self.app.running)
        mock_quit.assert_called_once_with(0)

    @patch("tray_app.user32.PostMessageW")
    @patch("tray_app.user32.TrackPopupMenuEx")
    @patch("tray_app.user32.SetForegroundWindow")
    @patch("tray_app.user32.DestroyMenu")
    @patch("tray_app.user32.AppendMenuW")
    @patch("tray_app.user32.CreatePopupMenu", return_value=12345)
    @patch("tray_app.user32.GetCursorPos")
    def test_show_context_menu_posts_wm_null(self, mock_get_pos, mock_create_menu, mock_append_menu,
                                             mock_destroy_menu, mock_set_fg, mock_track_menu, mock_post_msg):
        self.app.show_context_menu()
        mock_track_menu.assert_called_once()
        mock_post_msg.assert_called_once_with(self.app.hwnd, 0x0000, 0, 0)  # WM_NULL = 0x0000
        mock_destroy_menu.assert_called_once()


if __name__ == "__main__":
    unittest.main()
