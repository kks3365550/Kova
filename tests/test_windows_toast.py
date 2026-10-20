# tests/test_windows_toast.py
# -*- coding: utf-8 -*-
"""
Windows 네이티브 토스트 알림 모듈 (core/windows_toast.py) 단위 테스트
"""

import base64
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from core.windows_toast import (
    _execute_toast_powershell,
    send_windows_toast,
    notify_backup_failed,
    notify_disk_space_warning,
    notify_backup_success,
)


def _decode_powershell_encoded_command(cmd_list):
    """PowerShell 명령 인자에서 -EncodedCommand Base64 payload를 추출하여 디코딩"""
    for i, token in enumerate(cmd_list):
        if token == "-EncodedCommand" and i + 1 < len(cmd_list):
            encoded = cmd_list[i + 1]
            return base64.b64decode(encoded).decode("utf-16le")
    return None


def test_execute_toast_powershell_success():
    """정상 실행 시 subprocess.run 호출 및 성공 여부 확인"""
    with patch("core.windows_toast.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)

        result = _execute_toast_powershell("백업 알림", "백업이 성공했습니다.", "BackupSystem")
        assert result is True
        assert mock_run.called

        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert "powershell.exe" in cmd[0]
        assert "-EncodedCommand" in cmd

        # CREATE_NO_WINDOW 검증
        creationflags = kwargs.get("creationflags", 0)
        assert creationflags == getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

        # 디코딩 내용 검증
        decoded_script = _decode_powershell_encoded_command(cmd)
        assert decoded_script is not None
        assert "백업 알림" in decoded_script
        assert "백업이 성공했습니다." in decoded_script


def test_execute_toast_powershell_fail_safe():
    """예외 또는 실패 발생 시 Fail-safe 동작 (예외 전파 없이 False 반환)"""
    with patch("core.windows_toast.subprocess.run") as mock_run:
        mock_run.side_effect = OSError("PowerShell not found")
        result = _execute_toast_powershell("제목", "내용", "BackupSystem")
        assert result is False


def test_send_windows_toast_sync():
    """동기(async_call=False) 모드 호출 검증"""
    with patch("core.windows_toast._execute_toast_powershell") as mock_exec:
        mock_exec.return_value = True
        res = send_windows_toast("제목", "내용", async_call=False)
        assert res is True
        mock_exec.assert_called_once_with("제목", "내용", "BackupSystem")


def test_send_windows_toast_async():
    """비동기(async_call=True) 모드 데몬 스레드 기동 검증"""
    with patch("threading.Thread") as mock_thread_cls:
        mock_thread_instance = MagicMock()
        mock_thread_cls.return_value = mock_thread_instance

        res = send_windows_toast("제목", "내용", async_call=True)
        assert res is True
        assert mock_thread_cls.called
        assert mock_thread_instance.start.called


def test_notify_backup_failed_truncates_long_message():
    """에러 메시지가 긴 경우 80자 이내로 안전하게 축약되는지 검증"""
    with patch("core.windows_toast.send_windows_toast") as mock_toast:
        long_err = "A" * 120
        notify_backup_failed("일간 백업", long_err)

        assert mock_toast.called
        args, kwargs = mock_toast.call_args
        title, msg = args[0], args[1]
        assert "🚨 백업 실패: 일간 백업" in title
        assert "..." in msg
        assert "A" * 120 not in msg
        assert kwargs.get("app_id") == "BackupSystem.Alert"


def test_notify_disk_space_warning():
    """디스크 공간 부족 경고 알림 포맷 검증"""
    with patch("core.windows_toast.send_windows_toast") as mock_toast:
        notify_disk_space_warning(free_gb=3.2, req_gb=10.0, drive_path="D:\\")

        assert mock_toast.called
        args, kwargs = mock_toast.call_args
        title, msg = args[0], args[1]
        assert "⚠️ 저장소 디스크 용량 부족" in title
        assert "3.2GB" in msg
        assert "10.0GB" in msg
        assert "D:\\" in msg
        assert kwargs.get("app_id") == "BackupSystem.Storage"
