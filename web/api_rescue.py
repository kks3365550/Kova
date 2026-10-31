# -*- coding: utf-8 -*-
"""
web/api_rescue.py - 긴급 재해 복구(Rescue DR) UEFI 부팅 USB 제작 API 라우터
"""

import logging
import os
import sys
import time
import shutil
import threading
import subprocess
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/rescue", tags=["Rescue USB"])

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ISO = r"D:\Rufus\Win11_25H2_Korean_x64_v2.iso"

# 전역 USB 제작 상태
_usb_lock = threading.Lock()
_usb_task_state = {
    "is_running": False,
    "progress": 0,
    "phase": "IDLE",       # IDLE, VALIDATING, MOUNTING, COPYING_BOOT, INJECTING_PE, COPYING_TOOLS, COMPLETED, ERROR
    "phase_text": "대기 중",
    "target_drive": "",
    "log": [],
    "error": None,
    "started_at": None,
    "completed_at": None,
}


class MakeUsbRequest(BaseModel):
    drive_letter: str
    iso_path: Optional[str] = None
    auto_format: bool = False


def append_usb_log(msg: str):
    ts = time.strftime("[%H:%M:%S]")
    line = f"{ts} {msg}"
    with _usb_lock:
        _usb_task_state["log"].append(line)
        if len(_usb_task_state["log"]) > 300:
            _usb_task_state["log"].pop(0)


def scan_removable_drives() -> List[Dict[str, Any]]:
    """현재 시스템에 연결된 이동식 USB 드라이브만 탐색 (하드웨어 BusType/DriveType 검증으로 고정 NVMe/SATA 완전 배제)"""
    import psutil
    import ctypes
    from tools.make_rescue_usb import is_safe_removable_drive

    res = []
    try:
        partitions = psutil.disk_partitions(all=False)
        for p in partitions:
            letter = p.mountpoint.rstrip("\\").upper()
            if letter in ("C:", "D:"):
                continue

            # 하드웨어 BusType / 미디어 타입 기반 엄격한 검증 (NVMe, 고정 SATA, 시스템 디스크 완전 배제)
            clean_letter = letter.replace(":", "")
            is_safe, reason = is_safe_removable_drive(clean_letter)
            if not is_safe:
                continue

            try:
                usage = psutil.disk_usage(p.mountpoint)
                
                # 볼륨 라벨 추출
                vol_name = ctypes.create_unicode_buffer(260)
                fs_name = ctypes.create_unicode_buffer(260)
                label = "이동식 USB"
                if ctypes.windll.kernel32.GetVolumeInformationW(p.mountpoint, vol_name, 260, None, None, None, fs_name, 260):
                    if vol_name.value:
                        label = vol_name.value

                res.append({
                    "DriveLetter": letter,
                    "Label": label,
                    "FileSystem": p.fstype,
                    "SizeGB": round(usage.total / (1024 ** 3), 2),
                    "FreeGB": round(usage.free / (1024 ** 3), 2),
                    "DriveType": "USB 이동식",
                    "IsRemovable": True,
                    "IsFat32": (p.fstype.upper() == "FAT32")
                })
            except Exception:
                pass
    except Exception as e:
        logging.getLogger("BackupSystem").warning("드라이브 스캔 오류: %s", e)
    return res


def _background_usb_worker(target_drive: str, iso_path: str, auto_format: bool = False):
    try:
        from tools.make_rescue_usb import (
            check_target_drive, mount_iso, dismount_iso,
            copy_tree_filtered, create_launchers
        )

        with _usb_lock:
            _usb_task_state["phase"] = "VALIDATING"
            _usb_task_state["phase_text"] = "대상 드라이브 및 ISO 사전 검증 중"
            _usb_task_state["progress"] = 5
        append_usb_log(f"대상 USB 검증 시작: {target_drive} (FAT32 자동 포맷: {'활성화' if auto_format else '비활성화'})")

        if not check_target_drive(target_drive, auto_format=auto_format):
            raise RuntimeError(f"대상 드라이브 {target_drive}가 유효하지 않거나 FAT32 포맷이 아닙니다.")

        if not os.path.exists(iso_path):
            raise RuntimeError(f"Windows 11 ISO 파일을 찾을 수 없습니다: {iso_path}")

        target_root = target_drive.rstrip("\\") + "\\"
        iso_mount_letter = None

        try:
            with _usb_lock:
                _usb_task_state["phase"] = "MOUNTING"
                _usb_task_state["phase_text"] = "Windows 11 공식 ISO 마운트 중"
                _usb_task_state["progress"] = 15
            append_usb_log(f"ISO 마운트 중: {iso_path}")
            iso_mount_letter = mount_iso(iso_path)
            iso_root = iso_mount_letter.rstrip("\\") + "\\"
            append_usb_log(f"ISO 마운트 성공: {iso_mount_letter}")

            # 1. UEFI 필수 디렉터리 복사
            with _usb_lock:
                _usb_task_state["phase"] = "COPYING_BOOT"
                _usb_task_state["phase_text"] = "UEFI 부팅 레코드 및 BCD 복사 중 (1/4)"
                _usb_task_state["progress"] = 25
            append_usb_log("UEFI 부팅 파일 (efi, boot) 복사 중...")
            shutil.copytree(os.path.join(iso_root, "efi"), os.path.join(target_root, "efi"), dirs_exist_ok=True)
            shutil.copytree(os.path.join(iso_root, "boot"), os.path.join(target_root, "boot"), dirs_exist_ok=True)

            for boot_file in ["bootmgr", "bootmgr.efi", "bootmgfw.efi", "autorun.inf"]:
                src = os.path.join(iso_root, boot_file)
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(target_root, boot_file))

            # 2. sources 폴더 복사
            with _usb_lock:
                _usb_task_state["phase"] = "COPYING_BOOT"
                _usb_task_state["phase_text"] = "WinPE 램디스크(boot.wim) 복사 중 (2/4)"
                _usb_task_state["progress"] = 45
            append_usb_log("WinPE 램디스크(sources/boot.wim 614MB) 복사 중 (install.wim 제외)...")
            copy_tree_filtered(
                os.path.join(iso_root, "sources"),
                os.path.join(target_root, "sources"),
                exclude_files=["install.wim"]
            )
            append_usb_log("WinPE 부팅 램디스크 복사 완료")

            # 3. boot.wim 자동 부팅 직행 주입 (setup.exe 우회)
            with _usb_lock:
                _usb_task_state["phase"] = "INJECTING_PE"
                _usb_task_state["phase_text"] = "WinPE 복구 콘솔 100% 자동 실행 로직 주입 중 (3/4)"
                _usb_task_state["progress"] = 70
            append_usb_log("WinPE 자동 실행 로직 주입 중 (setup.exe 차단 및 콘솔 직행)...")

            inject_script = os.path.join(PROJECT_ROOT, "tools", "inject_autostart_to_wim.py")
            if os.path.exists(inject_script):
                res = subprocess.run([sys.executable, inject_script, os.path.join(target_root, "sources", "boot.wim")], capture_output=True, text=True, encoding="utf-8", errors="replace")
                if res.returncode == 0:
                    append_usb_log("WinPE boot.wim 자동 실행 주입 성공! (부팅 시 0초 만에 콘솔 직행)")
                else:
                    append_usb_log(f"[경고] WIM 주입 경고: {res.stderr.strip() or res.stdout.strip()}")

            # 4. 백업시스템 독립 런타임 및 도구 배치
            with _usb_lock:
                _usb_task_state["phase"] = "COPYING_TOOLS"
                _usb_task_state["phase_text"] = "독립 파이썬 및 3중 재해 복구 런타임 배치 중 (4/4)"
                _usb_task_state["progress"] = 85
            append_usb_log("무설치 독립 Python 3.11 및 3중 복구 엔진 배치 중...")

            rescue_tools_dir = os.path.join(target_root, "Rescue_Tools")
            os.makedirs(rescue_tools_dir, exist_ok=True)

            src_python = os.path.join(PROJECT_ROOT, "emergency_restore", "python")
            if os.path.exists(src_python):
                shutil.copytree(src_python, os.path.join(rescue_tools_dir, "python"), dirs_exist_ok=True)

            src_rescue_lab = os.path.join(PROJECT_ROOT, "sandbox", "rescue_lab")
            if os.path.exists(src_rescue_lab):
                shutil.copytree(src_rescue_lab, os.path.join(rescue_tools_dir, "rescue_lab"), dirs_exist_ok=True)

            for fname in ["disaster_recovery.py", "emergency_restore.py"]:
                src_f = os.path.join(PROJECT_ROOT, fname)
                if os.path.exists(src_f):
                    shutil.copy2(src_f, os.path.join(rescue_tools_dir, fname))

            src_core = os.path.join(PROJECT_ROOT, "core")
            if os.path.exists(src_core):
                shutil.copytree(src_core, os.path.join(rescue_tools_dir, "core"), dirs_exist_ok=True)

            # 초고속 네이티브 Rust 복구 엔진 패키징
            src_rust_engine = os.path.join(PROJECT_ROOT, "engine_rust", "kova_fast_engine.exe")
            if os.path.exists(src_rust_engine):
                shutil.copy2(src_rust_engine, os.path.join(rescue_tools_dir, "kova_fast_engine.exe"))
                append_usb_log("초고속 네이티브 Rust 복구 엔진(kova_fast_engine.exe) 패키징 완료")

            # 최신 인스톨러 배포본
            for f in os.listdir(os.path.join(PROJECT_ROOT, "web", "static")):
                if f.startswith("BackupSystem_Setup_") and f.endswith(".exe"):
                    shutil.copy2(os.path.join(PROJECT_ROOT, "web", "static", f), os.path.join(rescue_tools_dir, f))
                    break

            create_launchers(target_root)
            append_usb_log("원클릭 바로가기 및 복구 안내서 생성 완료")

        finally:
            if iso_mount_letter:
                dismount_iso(iso_path)
                append_usb_log("ISO 마운트 정상 해제 완료")

        with _usb_lock:
            _usb_task_state["phase"] = "COMPLETED"
            _usb_task_state["phase_text"] = "제작 완료 (사용 준비 완료)"
            _usb_task_state["progress"] = 100
            _usb_task_state["is_running"] = False
            _usb_task_state["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

        append_usb_log("🎉 긴급 재해 복구 UEFI 부팅 USB가 성공적으로 완성되었습니다!")

    except Exception as e:
        with _usb_lock:
            _usb_task_state["phase"] = "ERROR"
            _usb_task_state["phase_text"] = "복구 USB 만들기가 중단됐어요. USB를 확인한 뒤 다시 시도해 주세요."
            _usb_task_state["is_running"] = False
            _usb_task_state["error"] = str(e)
        append_usb_log(f"[!] 제작 중단 (오류): {str(e)}")


@router.get("/usb-targets")
def get_usb_targets():
    """현재 연결된 이동식 USB 드라이브 목록 반환"""
    targets = scan_removable_drives()
    iso_exists = os.path.exists(DEFAULT_ISO)
    return {
        "success": True,
        "targets": targets,
        "iso_ready": iso_exists,
        "iso_path": DEFAULT_ISO if iso_exists else None
    }


@router.post("/make-usb")
def start_make_usb(req: MakeUsbRequest):
    """긴급 복구 USB 제작 시작"""
    with _usb_lock:
        if _usb_task_state["is_running"]:
            raise HTTPException(status_code=400, detail="이미 USB 제작 작업이 진행 중입니다.")

        drive = req.drive_letter.upper().strip()
        if drive in ("C:", "D:"):
            raise HTTPException(status_code=400, detail="시스템 드라이브(C/D)는 선택할 수 없습니다.")

        iso = req.iso_path or DEFAULT_ISO
        if not os.path.exists(iso):
            raise HTTPException(status_code=400, detail=f"Windows 11 ISO 파일을 찾을 수 없습니다: {iso}")

        _usb_task_state["is_running"] = True
        _usb_task_state["progress"] = 0
        _usb_task_state["phase"] = "STARTING"
        _usb_task_state["phase_text"] = "작업 초기화 중"
        _usb_task_state["target_drive"] = drive
        _usb_task_state["log"] = []
        _usb_task_state["error"] = None
        _usb_task_state["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _usb_task_state["completed_at"] = None

    t = threading.Thread(target=_background_usb_worker, args=(drive, iso, req.auto_format), daemon=True)
    t.start()

    return {
        "success": True,
        "message": f"{drive} 드라이브로 긴급 복구 USB 제작을 시작했습니다."
    }


@router.get("/status")
def get_usb_status():
    """현재 USB 제작 진행 상태 및 로그 반환"""
    with _usb_lock:
        return dict(_usb_task_state)


@router.get("/guide")
def get_rescue_guide():
    """복구 USB 사용법 및 부팅 안내 데이터"""
    return {
        "success": True,
        "title": "긴급 재해 복구 부팅 USB 사용 안내서",
        "features": [
            "100% 자동 실행: 윈도우 설치 화면을 완전히 건너뛰고 0초 만에 복구 콘솔 자동 실행",
            "동적 백업 탐색: D:\\WindowsImageBackup 및 모든 외장하드의 백업 이미지를 100% 자동 인식",
            "무설치 독립형 런타임: 복구 USB 자체에 내장된 Python 3.11 엔진으로 환경 영향 제로",
            "UEFI & Secure Boot: 마이크로소프트 공식 서명으로 최신 PC 및 메인보드 100% 호환"
        ],
        "steps": [
            {
                "step": 1,
                "title": "USB 삽입 및 전원 켜기",
                "desc": "제작된 긴급 복구 USB를 PC 본체(가급적 후면 USB 3.0 포트)에 꽂고 전원을 켭니다."
            },
            {
                "step": 2,
                "title": "부팅 메뉴 키 연타 (F11 / F12)",
                "desc": "화면이 켜지자마자 메인보드 부팅 단축키(F11 또는 F12)를 1초 간격으로 연타하여 부팅 장치 선택 창을 엽니다."
            },
            {
                "step": 3,
                "title": "UEFI USB 선택",
                "desc": "목록에서 'UEFI: [USB 이름]'을 화살표 키로 선택하고 Enter를 누릅니다."
            },
            {
                "step": 4,
                "title": "자동 실행 및 원클릭 복원",
                "desc": "잠시 후 화면 가득 한글 복구 콘솔이 자동으로 뜹니다. 탐색된 백업본을 확인한 후 'Y'를 입력하면 C드라이브가 100% 복원됩니다!"
            }
        ],
        "boot_keys": [
            {"brand": "삼성 / LG 노트북/데스크탑", "key": "F12 또는 F10"},
            {"brand": "ASUS (에이수스)", "key": "F8"},
            {"brand": "MSI / 애즈락(ASRock)", "key": "F11"},
            {"brand": "기가바이트(Gigabyte)", "key": "F12"},
            {"brand": "레노버(Lenovo)", "key": "F12 또는 Fn+F12"},
            {"brand": "HP / 델(Dell)", "key": "F9 (HP) / F12 (Dell)"}
        ]
    }
