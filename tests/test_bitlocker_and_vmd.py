# -*- coding: utf-8 -*-
"""
tests/test_bitlocker_and_vmd.py

BitLocker 암호화 상태 파싱, 48자리 복구키 추출, 비상 안내서 생성,
FastAPI API 엔드포인트 및 Intel VMD WinPE drvload 탑재 무결성 검증 테스트.
"""

import os
import pytest
from fastapi.testclient import TestClient

from core.bitlocker import (
    parse_bitlocker_status,
    parse_recovery_key,
    build_bitlocker_guide_text,
    get_bitlocker_info,
    export_bitlocker_guide_file,
)
from web.app import app


# ---------------------------------------------------------------------------
# 1. BitLocker manage-bde Status Parsing Tests (English & Korean)
# ---------------------------------------------------------------------------
def test_parse_bitlocker_status_english_encrypted():
    sample_en = """
BitLocker Drive Encryption: Configuration Tool version 10.0.26100
Volume C: [OS]
[OS Volume]
    Size:                 475.69 GB
    BitLocker Version:    2.0
    Conversion Status:    Fully Encrypted
    Percentage Encrypted: 100.0%
    Encryption Method:    XTS-AES 128
    Protection Status:    Protection On
    Lock Status:          Unlocked
    Identification Field: None
    Key Protectors:
        TPM
        Numerical Password
"""
    parsed = parse_bitlocker_status(sample_en)
    assert parsed["is_encrypted"] is True
    assert parsed["protection_status"] == "on"
    assert "TPM 보안 칩" in parsed["key_protectors"]
    assert "48자리 숫자 복구 키 (Numerical Password)" in parsed["key_protectors"]
    assert parsed["lock_status"] == "Unlocked"


def test_parse_bitlocker_status_korean_encrypted():
    sample_ko = """
BitLocker 드라이브 암호화: 구성 도구 버전 10.0.26100
볼륨 C: [로컬 디스크]
[OS 볼륨]
    크기:                 475.69 GB
    BitLocker 버전:       2.0
    변환 상태:            완전 암호화됨
    암호화된 비율:        100.0%
    암호화 방법:          XTS-AES 128
    보호 상태:            보호 켜짐
    잠금 상태:            잠금 해제됨
    식별 필드:            없음
    키 보호기:
        TPM
        숫자 암호
"""
    parsed = parse_bitlocker_status(sample_ko)
    assert parsed["is_encrypted"] is True
    assert parsed["protection_status"] == "on"
    assert "TPM 보안 칩" in parsed["key_protectors"]
    assert "48자리 숫자 복구 키 (Numerical Password)" in parsed["key_protectors"]


def test_parse_bitlocker_status_decrypted():
    sample_off = """
Volume C: [OS]
    Conversion Status:    Fully Decrypted
    Percentage Encrypted: 0.0%
    Protection Status:    Protection Off
    Lock Status:          Unlocked
"""
    parsed = parse_bitlocker_status(sample_off)
    assert parsed["is_encrypted"] is False
    assert parsed["protection_status"] == "off"


# ---------------------------------------------------------------------------
# 2. 48-Digit Recovery Key Parsing Tests
# ---------------------------------------------------------------------------
def test_parse_recovery_key_success():
    sample_protector = """
BitLocker Drive Encryption: Configuration Tool version 10.0.26100
Volume C:
Numerical Password:
  ID: {A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
  Password:
    111222-333444-555666-777888-999000-123456-654321-987654
"""
    key, key_id = parse_recovery_key(sample_protector)
    assert key == "111222-333444-555666-777888-999000-123456-654321-987654"
    assert key_id == "{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}"


def test_parse_recovery_key_korean():
    sample_protector_ko = """
볼륨 C:
숫자 암호:
  암호 ID: {12345678-1234-1234-1234-1234567890AB}
  암호:
    999888-777666-555444-333222-111000-222333-444555-666777
"""
    key, key_id = parse_recovery_key(sample_protector_ko)
    assert key == "999888-777666-555444-333222-111000-222333-444555-666777"
    assert key_id == "{12345678-1234-1234-1234-1234567890AB}"


def test_parse_recovery_key_none():
    sample_empty = "No key protectors of the specified type were found."
    key, key_id = parse_recovery_key(sample_empty)
    assert key is None
    assert key_id is None


# ---------------------------------------------------------------------------
# 3. Guide Text & Export File Tests
# ---------------------------------------------------------------------------
def test_build_bitlocker_guide_text():
    info_with_key = {
        "protection_label": "보호 켜짐 (암호화 활성)",
        "recovery_key": "111222-333444-555666-777888-999000-123456-654321-987654",
        "key_id": "{TEST-KEY-ID}"
    }
    txt = build_bitlocker_guide_text("C:", info_with_key)
    assert "111222-333444-555666-777888-999000-123456-654321-987654" in txt
    assert "manage-bde -unlock C: -RecoveryPassword" in txt

    info_without_key = {
        "protection_label": "보호 꺼짐"
    }
    txt_no_key = build_bitlocker_guide_text("C:", info_without_key)
    assert "https://account.microsoft.com/devices/recoverykey" in txt_no_key


def test_export_bitlocker_guide_file(tmp_path):
    target = os.path.join(tmp_path, "test_guide.txt")
    saved = export_bitlocker_guide_file(target)
    assert os.path.exists(saved)
    with open(saved, "r", encoding="utf-8") as f:
        content = f.read()
    assert "KOVA 백업시스템" in content
    assert "BitLocker" in content


def test_get_bitlocker_info_schema():
    info = get_bitlocker_info("C:")
    assert "drive" in info
    assert "is_encrypted" in info
    assert "protection_status" in info
    assert "guide_text" in info


# ---------------------------------------------------------------------------
# 4. FastAPI Web API Tests
# ---------------------------------------------------------------------------
@pytest.fixture
def client():
    return TestClient(app)


def test_api_bitlocker_status(client):
    res = client.get("/api/system/bitlocker-status?drive=C:")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert "drive" in data["data"]
    assert "protection_status" in data["data"]
    assert "guide_text" in data["data"]


def test_api_bitlocker_save_guide(client, monkeypatch, tmp_path):
    dummy_target = os.path.join(tmp_path, "saved_guide.txt")
    monkeypatch.setattr("web.api_bitlocker.export_bitlocker_guide_file", lambda: dummy_target)

    # 직접 파일 생성 흉내
    with open(dummy_target, "w", encoding="utf-8") as f:
        f.write("test bitlocker guide")

    res = client.post("/api/system/bitlocker-save-guide")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert dummy_target in data["saved_path"]


# ---------------------------------------------------------------------------
# 5. Intel VMD drvload WinPE Pipeline Static Tests
# ---------------------------------------------------------------------------
def test_inject_autostart_to_wim_has_drvload():
    inject_script = os.path.join(os.path.dirname(__file__), "..", "tools", "inject_autostart_to_wim.py")
    with open(inject_script, "r", encoding="utf-8") as f:
        content = f.read()
    assert "drvload" in content
    assert "%FOUND_DRIVE%\\Drivers" in content or "%FOUND_DRIVE%\\\\Drivers" in content


def test_make_rescue_usb_has_drvload_and_drivers_readme():
    make_script = os.path.join(os.path.dirname(__file__), "..", "tools", "make_rescue_usb.py")
    with open(make_script, "r", encoding="utf-8") as f:
        content = f.read()
    assert "drvload" in content
    assert "Drivers" in content
    assert "README_드라이버안내.txt" in content
