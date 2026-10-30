# -*- coding: utf-8 -*-
"""
tests/test_rescue_usb.py - 긴급 재해 복구(Rescue DR) USB 제작 및 하드웨어 타입 가드레일 단위 테스트
"""
import json
from unittest.mock import patch
from tools.make_rescue_usb import check_target_drive, format_target_drive, is_safe_removable_drive
from web.api_rescue import MakeUsbRequest


def test_format_target_drive_blocks_system_drives():
    """C: 및 D: 시스템 드라이브 포맷 시도는 무조건 즉시 거부되어야 함"""
    assert format_target_drive("C:") is False
    assert format_target_drive("c") is False
    assert format_target_drive("D:") is False
    assert format_target_drive("D") is False


def test_check_target_drive_blocks_system_drives():
    """C: 및 D: 시스템 드라이브 검증은 무조건 즉시 거부되어야 함"""
    assert check_target_drive("C:") is False
    assert check_target_drive("D:") is False


def test_is_safe_removable_drive_blocks_system_letters():
    """드라이브 문자 수준에서 C:, D: 즉시 거부"""
    ok, reason = is_safe_removable_drive("C:")
    assert ok is False
    assert "보호" in reason

    ok, reason = is_safe_removable_drive("D:")
    assert ok is False
    assert "보호" in reason


@patch("tools.make_rescue_usb.run_ps")
def test_is_safe_removable_drive_blocks_nvme_fixed(mock_run_ps):
    """E 드라이브 같은 고정 NVMe/SATA 디스크는 차단되어야 함"""
    mock_run_ps.return_value = json.dumps({
        "DriveLetter": "E",
        "BusType": "NVMe",
        "DriveType": "Fixed",
        "IsBoot": False,
        "IsSystem": False
    })
    ok, reason = is_safe_removable_drive("E:")
    assert ok is False
    assert "고정 내장 디스크" in reason


@patch("tools.make_rescue_usb.run_ps")
def test_is_safe_removable_drive_allows_usb(mock_run_ps):
    """실제 USB 드라이브는 정상 통과해야 함"""
    mock_run_ps.return_value = json.dumps({
        "DriveLetter": "G",
        "BusType": "USB",
        "DriveType": "Removable",
        "IsBoot": False,
        "IsSystem": False
    })
    ok, reason = is_safe_removable_drive("G:")
    assert ok is True
    assert "이동식 USB 확인됨" in reason


def test_make_usb_request_model_defaults():
    """MakeUsbRequest 기본값 검증"""
    req = MakeUsbRequest(drive_letter="G:")
    assert req.drive_letter == "G:"
    assert req.iso_path is None
    assert req.auto_format is False

    req_with_format = MakeUsbRequest(drive_letter="G:", auto_format=True)
    assert req_with_format.auto_format is True


@patch("tools.make_rescue_usb.is_safe_removable_drive")
@patch("tools.make_rescue_usb.run_ps")
def test_check_target_drive_rejects_non_fat32_without_auto_format(mock_run_ps, mock_is_safe):
    """auto_format=False 일 때 NTFS 드라이브는 거부되어야 함"""
    mock_is_safe.return_value = (True, "OK")
    mock_volume_json = json.dumps({
        "DriveLetter": "G",
        "DriveType": "Removable",
        "FileSystem": "NTFS",
        "SizeRemaining": 20 * (1024 ** 3),
        "Size": 32 * (1024 ** 3)
    })
    mock_run_ps.return_value = mock_volume_json

    result = check_target_drive("G:", auto_format=False)
    assert result is False


@patch("tools.make_rescue_usb.is_safe_removable_drive")
@patch("tools.make_rescue_usb.format_target_drive")
@patch("tools.make_rescue_usb.run_ps")
def test_check_target_drive_formats_when_auto_format_true(mock_run_ps, mock_format, mock_is_safe):
    """auto_format=True 일 때 포맷 함수를 호출하고 성공 시 통과해야 함"""
    mock_is_safe.return_value = (True, "OK")
    mock_volume_ntfs = json.dumps({
        "DriveLetter": "G",
        "DriveType": "Removable",
        "FileSystem": "NTFS",
        "SizeRemaining": 20 * (1024 ** 3),
        "Size": 32 * (1024 ** 3)
    })
    mock_volume_fat32 = json.dumps({
        "DriveLetter": "G",
        "DriveType": "Removable",
        "FileSystem": "FAT32",
        "SizeRemaining": 28 * (1024 ** 3),
        "Size": 32 * (1024 ** 3)
    })
    # 1회차: NTFS 감지 -> format_target_drive 호출 -> 2회차: 포맷 후 볼륨 갱신 (FAT32)
    mock_run_ps.side_effect = [mock_volume_ntfs, mock_volume_fat32]
    mock_format.return_value = True

    result = check_target_drive("G:", auto_format=True)
    assert result is True
    mock_format.assert_called_once_with("G")
