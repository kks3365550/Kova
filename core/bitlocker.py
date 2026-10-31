# -*- coding: utf-8 -*-
"""
core/bitlocker.py
Windows BitLocker 및 장치 암호화(Device Encryption) 상태 감지,
48자리 비상 복구 키(Recovery Password) 조회 및 사용자 안전 백업 가이드 모듈.
"""

import os
import sys
import re
import socket
import datetime
import subprocess
from typing import Dict, Any, Optional

def _run_cmd(cmd_list, timeout=5) -> tuple[int, str, str]:
    creationflags = 0
    if hasattr(subprocess, 'CREATE_NO_WINDOW'):
        creationflags = subprocess.CREATE_NO_WINDOW
    try:
        res = subprocess.run(
            cmd_list,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout,
            creationflags=creationflags
        )
        return res.returncode, res.stdout or "", res.stderr or ""
    except Exception as e:
        return -1, "", str(e)


def parse_bitlocker_status(output: str) -> Dict[str, Any]:
    """manage-bde.exe -status 출력 텍스트 파싱 (한글/영문 지원)"""
    data: Dict[str, Any] = {
        "is_encrypted": False,
        "protection_status": "off",
        "protection_label": "보호 꺼짐 (암호화 안 됨)",
        "conversion_status": "알 수 없음",
        "lock_status": "Unlocked",
        "key_protectors": [],
        "raw_status": output.strip()
    }

    if not output:
        return data

    # 1. Protection Status (보호 상태)
    # 한글: "보호 상태:           보호 켜짐" / "보호 꺼짐"
    # 영문: "Protection Status:   Protection On" / "Protection Off"
    if re.search(r'(Protection Status|보호\s*상태)\s*:\s*(Protection\s*On|보호\s*켜짐)', output, re.IGNORECASE):
        data["protection_status"] = "on"
        data["protection_label"] = "보호 켜짐 (BitLocker 암호화 활성)"
        data["is_encrypted"] = True
    elif re.search(r'(Protection Status|보호\s*상태)\s*:\s*(Protection\s*Off|보호\s*꺼짐)', output, re.IGNORECASE):
        data["protection_status"] = "off"
        data["protection_label"] = "보호 꺼짐 (암호화 해제 상태)"
        data["is_encrypted"] = False

    # 2. Conversion Status (변환 상태)
    conv_match = re.search(r'(Conversion Status|변환\s*상태)\s*:\s*([^\r\n]+)', output, re.IGNORECASE)
    if conv_match:
        conv_val = conv_match.group(2).strip()
        data["conversion_status"] = conv_val
        if any(w in conv_val.lower() for w in ["encrypt", "암호화"]) and "decrypted" not in conv_val.lower() and "암호 해독" not in conv_val:
            data["is_encrypted"] = True

    # 3. Lock Status (잠금 상태)
    lock_match = re.search(r'(Lock Status|잠금\s*상태)\s*:\s*([^\r\n]+)', output, re.IGNORECASE)
    if lock_match:
        data["lock_status"] = lock_match.group(2).strip()

    # 4. Key Protectors (키 보호기)
    protectors = []
    if re.search(r'\bTPM\b', output, re.IGNORECASE):
        protectors.append("TPM 보안 칩")
    if re.search(r'(Numerical Password|숫자\s*암호)', output, re.IGNORECASE):
        protectors.append("48자리 숫자 복구 키 (Numerical Password)")
    if re.search(r'(Password|암호)', output, re.IGNORECASE) and not re.search(r'(Numerical Password|숫자\s*암호)', output, re.IGNORECASE):
        protectors.append("사용자 암호")
    data["key_protectors"] = protectors

    return data


def parse_recovery_key(output: str) -> tuple[Optional[str], Optional[str]]:
    """manage-bde.exe -protectors -get 출력에서 48자리 숫자 패스워드와 키 ID 파싱"""
    # 48자리 숫자: 123456-123456-123456-123456-123456-123456-123456-123456
    key_match = re.search(r'(\d{6}-\d{6}-\d{6}-\d{6}-\d{6}-\d{6}-\d{6}-\d{6})', output)
    recovery_key = key_match.group(1) if key_match else None

    # ID: {GUID}
    id_match = re.search(r'(ID|암호\s*ID)\s*:\s*(\{[0-9a-fA-F-]+\})', output, re.IGNORECASE)
    key_id = id_match.group(2) if id_match else None

    return recovery_key, key_id


def get_bitlocker_info(drive: str = "C:") -> Dict[str, Any]:
    """
    드라이브(기본 C:)의 BitLocker 암호화 상태 및 48자리 복구키 조회.
    비관리자 권한이거나 BitLocker 미적용 시 안전하게 대체 안내 반환.
    """
    drive_letter = drive.rstrip(":\\/").upper() + ":"
    result: Dict[str, Any] = {
        "drive": drive_letter,
        "is_encrypted": False,
        "protection_status": "off",
        "protection_label": "확인 중...",
        "conversion_status": "알 수 없음",
        "lock_status": "Unlocked",
        "key_protectors": [],
        "recovery_key": None,
        "key_id": None,
        "ms_account_url": "https://account.microsoft.com/devices/recoverykey",
        "requires_admin": False,
        "error": None
    }

    if not sys.platform.startswith("win"):
        result["protection_status"] = "off"
        result["protection_label"] = "Windows 외 운영체제 (해당 없음)"
        return result

    # 1. manage-bde -status <drive>
    code, stdout, stderr = _run_cmd(["manage-bde.exe", "-status", drive_letter], timeout=6)
    if code != 0:
        if "access ... denied" in (stderr + stdout).lower() or "권한" in (stderr + stdout):
            result["requires_admin"] = True
            result["protection_status"] = "unknown"
            result["protection_label"] = "관리자 권한 필요 (상태 확인 불가)"
            result["error"] = "Access Denied: 관리자 권한이 필요합니다."
        else:
            # 드라이브가 BitLocker를 지원하지 않거나 해제된 경우
            result["protection_status"] = "off"
            result["protection_label"] = "보호 꺼짐 (암호화 안 됨)"
        result["guide_text"] = build_bitlocker_guide_text(drive=drive_letter, status_info=result)
        return result

    parsed = parse_bitlocker_status(stdout)
    result.update(parsed)

    # 2. 만약 BitLocker가 켜져 있다면, 48자리 복구키 조회 시도
    if result["is_encrypted"] or result["protection_status"] == "on":
        code_p, stdout_p, _ = _run_cmd(
            ["manage-bde.exe", "-protectors", "-get", drive_letter, "-type", "RecoveryPassword"],
            timeout=6
        )
        if code_p == 0 and stdout_p:
            key, key_id = parse_recovery_key(stdout_p)
            result["recovery_key"] = key
            result["key_id"] = key_id

    # 가이드 텍스트 자동 동봉
    result["guide_text"] = build_bitlocker_guide_text(
        drive=drive_letter,
        status_info=result
    )
    return result


def build_bitlocker_guide_text(drive: str, status_info: Dict[str, Any]) -> str:
    """사용자가 출력하거나 텍스트 파일로 저장할 수 있는 안내문 생성"""
    pc_name = socket.gethostname()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status_label = status_info.get("protection_label", "보호 꺼짐")
    rec_key = status_info.get("recovery_key")
    key_id = status_info.get("key_id", "알 수 없음")

    lines = [
        "======================================================================",
        "  🛡️ [KOVA 백업시스템] 긴급 재해 복구용 BitLocker 복구 키 안전 보관서",
        "======================================================================",
        f"■ PC 이름       : {pc_name}",
        f"■ 드라이브 문자 : {drive}",
        f"■ 생성 일시     : {now_str}",
        f"■ 암호화 상태   : {status_label}",
        "----------------------------------------------------------------------"
    ]

    if rec_key:
        lines.extend([
            "★ [중요] 48자리 긴급 복구 키 (BitLocker Recovery Key):",
            f"   >> {rec_key} <<",
            f"   (식별자 ID: {key_id})",
            "",
            "■ 복구 키 사용 방법:",
            "  PC 부팅 불능으로 KOVA 긴급 복구 USB(WinPE)로 부팅했을 때,",
            "  BitLocker로 C: 드라이브가 잠겨 있으면 명령 프롬프트에서 아래 명령을 입력하세요:",
            f"  manage-bde -unlock {drive} -RecoveryPassword {rec_key}",
            "",
            "■ 안전 보관 권장 사항:",
            "  1. 이 텍스트 파일을 종이로 인쇄하여 보관하거나 스마트폰으로 촬영하세요.",
            "  2. 복구 USB의 루트 폴더나 외장 드라이브에 복사해 두면 비상 시 즉시 확인 가능합니다."
        ])
    else:
        lines.extend([
            "■ 48자리 복구 키 온라인 확인 방법 (마이크로소프트 공식):",
            "  마이크로소프트 계정 로그인 후 아래 링크에서 48자리 복구키를 언제든 확인하실 수 있습니다:",
            "  https://account.microsoft.com/devices/recoverykey",
            "",
            "■ Windows 명령 프롬프트(관리자 권한)에서 직접 확인하는 법:",
            f"  manage-bde -protectors -get {drive} -type RecoveryPassword"
        ])

    lines.extend([
        "======================================================================"
    ])
    return "\r\n".join(lines)


def export_bitlocker_guide_file(target_path: Optional[str] = None) -> str:
    """바탕화면 또는 지정 경로에 BitLocker 복구키 안내 텍스트 파일 저장"""
    info = get_bitlocker_info("C:")
    guide_text = build_bitlocker_guide_text("C:", info)

    if not target_path:
        desktop_dir = os.path.join(os.path.expanduser("~"), "Desktop")
        if not os.path.exists(desktop_dir):
            desktop_dir = os.path.abspath(".")
        target_path = os.path.join(desktop_dir, "BitLocker_C_복구키_보관용.txt")

    with open(target_path, "wb") as f:
        f.write(guide_text.encode("utf-8"))

    return target_path
