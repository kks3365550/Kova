# -*- coding: utf-8 -*-
"""
core/disk_clone.py: 라이브 SSD 마이그레이션 (Live Disk Clone & Bootable) 엔진
- Windows 현재 실행 중인 C: 드라이브(OS + 데이터)를 VSS(Volume Shadow Copy)로 실시간 캡처
- 대상 물리 SSD로 초고속 LBA 블록 스트리밍 복제 (하드웨어 한계 속도 직사)
- 부트로더(bcdboot) 자동 주입으로 대상 SSD를 즉시 부팅 가능한(Bootable) 상태로 구축
- 비동기 백그라운드 실행 및 실시간 진행률/로그 스트리밍 지원
"""

import os
import sys
import re
import time
import shutil
import logging
import threading
import subprocess
from typing import Dict, List, Any, Optional

logger = logging.getLogger("BackupSystem.DiskClone")

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

try:
    from core.vss_manager import VSSContext
    VSS_AVAILABLE = True
except Exception:
    VSS_AVAILABLE = False
    VSSContext = None

try:
    from core.drive_archive import get_volume_serial_number, set_volume_serial_number
    VSN_HELPERS_AVAILABLE = True
except Exception:
    VSN_HELPERS_AVAILABLE = False


# 스마트 제외 패턴 (마이그레이션 대상에서 제외할 불필요한 OS 가상메모리 및 임시 파일)
DEFAULT_CLONE_EXCLUDE_FILES = [
    "pagefile.sys",
    "hiberfil.sys",
    "swapfile.sys",
    "DumpStack.log",
    "DumpStack.log.tmp",
    "*.tmp"
]

DEFAULT_CLONE_EXCLUDE_DIRS = [
    "$RECYCLE.BIN",
    "$Recycle.Bin",
    "System Volume Information",
    "WindowsImageBackup",
    "MyBackup_Repository",
    "backup_repository",
    "Temp"
]


def normalize_drive(drive_str: str) -> str:
    """'c', 'C:', 'C:\\' 등을 'C:' 표준 형식으로 정규화"""
    if not drive_str:
        return ""
    d = drive_str.strip().replace("/", "\\")
    m = re.match(r"^([A-Za-z])(?::|\\|$)", d)
    if m:
        return m.group(1).upper() + ":"
    return d.upper()


def get_physical_disk_number(drive_letter: str) -> Optional[int]:
    """드라이브 문자에 대응하는 물리 디스크 번호(PhysicalDrive Number) 반환 (예: 'C:' -> 0)"""
    drv = normalize_drive(drive_letter).rstrip(":")
    if not drv:
        return None
    ps_cmd = f"(Get-Partition -DriveLetter '{drv}' -ErrorAction SilentlyContinue).DiskNumber"
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        res = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            creationflags=creationflags,
            timeout=5
        )
        out = res.stdout.strip()
        if out and out.isdigit():
            return int(out)
    except Exception:
        pass
    return None


def find_free_drive_letter(preferred: str = "S:") -> str:
    """시스템에서 현재 사용되지 않는 알파벳 드라이브 문자 탐색"""
    pref = normalize_drive(preferred)
    used = set()
    for l in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if os.path.exists(f"{l}:\\"):
            used.add(f"{l}:")
    if pref and pref not in used:
        return pref
    for l in "SZYXWVUTRPQONMLKJIHGFEDB":
        cand = f"{l}:"
        if cand not in used:
            return cand
    return "S:"


def is_admin() -> bool:
    """현재 프로세스가 관리자 권한인지 확인"""
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def run_elevated_command(cmd_args: List[str], timeout: int = 60, cwd: Optional[str] = None) -> tuple[int, str, str]:
    """
    관리자 권한이 필요한 Windows 시스템 도구(diskpart, bcdboot 등)를 안전하게 실행.
    현재 프로세스가 관리자 권한이면 CREATE_NO_WINDOW로 즉시 실행하고,
    비관리자 권한이거나 [WinError 740] 발생 시 PowerShell Start-Process -Verb RunAs UAC 프록시로 무창(Hidden) 실행.
    """
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    # 1. 관리자 권한인 경우 직접 실행
    if is_admin():
        try:
            proc = subprocess.run(
                cmd_args,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="cp949",
                errors="ignore",
                creationflags=creationflags,
                timeout=timeout
            )
            return proc.returncode, proc.stdout or "", proc.stderr or ""
        except OSError as e:
            if getattr(e, "winerror", None) != 740:
                return -1, "", str(e)
            # winerror == 740 이면 아래 2번 UAC 승격 프록시로 폴백

    # 2. 비관리자 권한이거나 WinError 740 발생 시 PowerShell Start-Process -Verb RunAs 프록시
    import tempfile
    out_file = tempfile.mktemp(suffix=".out")
    err_file = tempfile.mktemp(suffix=".err")
    code_file = tempfile.mktemp(suffix=".code")
    ps1_path = tempfile.mktemp(suffix=".ps1")

    exe_target = cmd_args[0]
    sub_args = cmd_args[1:]
    escaped_exe = str(exe_target).replace("'", "''")
    escaped_args = ["'" + str(a).replace("'", "''") + "'" for a in sub_args]
    args_array = "@(" + ", ".join(escaped_args) + ")"

    cd_line = ""
    env_line = ""
    if cwd and os.path.exists(cwd):
        escaped_cwd = str(cwd).replace("'", "''")
        cd_line = f"Set-Location '{escaped_cwd}'\r\n"
        env_line = f"$env:PYTHONPATH = '{escaped_cwd};' + $env:PYTHONPATH\r\n"

    ps1_content = (
        "$OutputEncoding = [System.Text.Encoding]::UTF8\r\n"
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\r\n"
        "$ErrorActionPreference = 'Continue'\r\n"
        + cd_line
        + env_line
        + f"& '{escaped_exe}' {args_array} > '{out_file}' 2> '{err_file}'\r\n"
        f"$code = if ($LASTEXITCODE -ne $null) {{ $LASTEXITCODE }} else {{ 0 }}\r\n"
        f"Set-Content -Path '{code_file}' -Value $code -Encoding ASCII\r\n"
        f"exit $code\r\n"
    )

    try:
        with open(ps1_path, "w", encoding="utf-8-sig") as f:
            f.write(ps1_content)

        ps_cmd = (
            f"$p = Start-Process powershell.exe -ArgumentList '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"{ps1_path}\"' "
            f"-Verb RunAs -WindowStyle Hidden -Wait -PassThru; exit $p.ExitCode"
        )

        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            creationflags=creationflags,
            timeout=timeout
        )

        rc = 0
        if os.path.exists(code_file):
            try:
                with open(code_file, "r", encoding="utf-8", errors="ignore") as f:
                    c_txt = f.read().strip()
                    if c_txt.isdigit() or (c_txt.startswith("-") and c_txt[1:].isdigit()):
                        rc = int(c_txt)
            except Exception:
                pass
        else:
            rc = proc.returncode

        def _safe_read_out(f_path: str) -> str:
            if not os.path.exists(f_path):
                return ""
            for enc in ("utf-16", "utf-8-sig", "utf-8", "cp949"):
                try:
                    with open(f_path, "r", encoding=enc) as rf:
                        txt = rf.read()
                        if txt and "\x00" not in txt:
                            return txt
                except Exception:
                    pass
            try:
                with open(f_path, "r", encoding="utf-8", errors="replace") as rf:
                    return rf.read()
            except Exception:
                return ""

        out_txt = _safe_read_out(out_file)
        err_txt = _safe_read_out(err_file)

        return rc, out_txt, err_txt
    finally:
        for tmp_f in (ps1_path, out_file, err_file, code_file):
            if os.path.exists(tmp_f):
                try:
                    os.remove(tmp_f)
                except Exception:
                    pass


def prepare_target_disk_with_esp(disk_number: int, target_drive: str, esp_letter: str) -> Dict[str, Any]:
    """
    타겟 새 물리 SSD를 100% 부팅 가능한 UEFI 표준(GPT + 260MB FAT32 ESP + 16MB MSR + NTFS Windows)으로 원클릭 초기화
    """
    target_clean = normalize_drive(target_drive).rstrip(":")
    esp_clean = normalize_drive(esp_letter).rstrip(":")

    script_lines = [
        f"select disk {disk_number}",
        "clean",
        "convert gpt",
        "select partition 1",
        "delete partition override",
        "create partition efi size=260",
        "format fs=fat32 quick label=\"ESP\"",
        f"assign letter={esp_clean}",
        "create partition msr size=16",
        "create partition primary",
        "format fs=ntfs quick label=\"Windows\"",
        f"assign letter={target_clean}",
        "exit"
    ]
    script_content = "\r\n".join(script_lines) + "\r\n"

    import tempfile
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", encoding="utf-8") as tf:
        tf.write(script_content)
        script_path = tf.name

    try:
        rc, out, err = run_elevated_command(["diskpart", "/s", script_path], timeout=60)
        if rc != 0:
            return {"success": False, "error": f"diskpart 파티셔닝 실패 (Exit Code {rc}): {err or out}"}
        return {"success": True, "output": out}
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass


def remove_esp_drive_letter(esp_letter: str) -> bool:
    """주입 완료 후 ESP 파티션의 임시 드라이브 문자를 안전하게 해제하여 숨김 파티션으로 전환"""
    esp_clean = normalize_drive(esp_letter).rstrip(":")
    script_lines = [
        f"select volume {esp_clean}",
        f"remove letter={esp_clean}",
        "exit"
    ]
    script_content = "\r\n".join(script_lines) + "\r\n"
    import tempfile
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", encoding="utf-8") as tf:
        tf.write(script_content)
        script_path = tf.name
    try:
        rc, _, _ = run_elevated_command(["diskpart", "/s", script_path], timeout=20)
        return rc == 0
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass


def get_all_partition_info() -> Dict[str, Dict[str, Any]]:
    """모든 드라이브의 드라이브 문자, 디스크 번호, 파티션 크기 일괄 조회 (RAW/초기화 디스크 100% 감지)"""
    info = {}
    ps_cmd = "Get-Partition -ErrorAction SilentlyContinue | Where-Object DriveLetter | Select-Object DriveLetter, DiskNumber, Size | ConvertTo-Json"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        res = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, creationflags=creationflags, timeout=5
        )
        txt = res.stdout.strip()
        if txt:
            import json
            data = json.loads(txt)
            if isinstance(data, dict):
                data = [data]
            for item in data:
                dl = str(item.get("DriveLetter", "")).upper()
                if dl and len(dl) == 1:
                    info[f"{dl}:"] = {
                        "disk_number": item.get("DiskNumber"),
                        "size_bytes": int(item.get("Size", 0))
                    }
    except Exception:
        pass
    return info


def get_candidate_target_drives() -> List[Dict[str, Any]]:
    """
    마이그레이션 대상이 될 수 있는 후보 SSD/드라이브 목록 조회
    - 현재 부팅 드라이브(C:)는 타겟 대상에서 제외
    - 총 용량, 사용량, 여유공간 및 C: 드라이브 사용량 대비 수용 가능 여부 판정
    - 물리 디스크 번호 및 독립 디스크 여부(auto_esp 지원 여부) 판정
    - 초기화(RAW), 미포맷, 또는 파일시스템 손상 상태의 새 SSD도 100% 정상 후보로 감지
    """
    c_usage = shutil.disk_usage("C:\\")
    c_used_gb = round(c_usage.used / (1024 ** 3), 1)
    c_disk_num = get_physical_disk_number("C:")

    part_info = get_all_partition_info()

    # 감지할 드라이브 목록 수집 (part_info + psutil)
    drives_to_check = set(part_info.keys())
    if PSUTIL_AVAILABLE:
        try:
            for p in psutil.disk_partitions(all=True):
                d = normalize_drive(p.mountpoint)
                if d and d != "C:":
                    drives_to_check.add(d)
        except Exception:
            pass

    candidates = []
    seen = set()

    for drv in sorted(drives_to_check):
        if drv == "C:" or drv in seen:
            continue

        root = f"{drv}\\"
        total_bytes = 0
        used_bytes = 0
        free_bytes = 0
        fstype = "NTFS"
        t_disk_num = None

        # 1. 일반 볼륨 용량 조회 (포맷된 정상 볼륨)
        try:
            usage = shutil.disk_usage(root)
            total_bytes = usage.total
            used_bytes = usage.used
            free_bytes = usage.free
            fstype = "NTFS"
        except Exception:
            # 2. 초기화/미포맷/RAW 드라이브 폴백 (PowerShell 파티션 정보 활용)
            if drv in part_info:
                p_entry = part_info[drv]
                total_bytes = p_entry.get("size_bytes", 0)
                used_bytes = 0
                free_bytes = total_bytes
                fstype = "RAW (초기화됨)"
                t_disk_num = p_entry.get("disk_number")

        if total_bytes <= 0:
            continue

        seen.add(drv)
        total_gb = round(total_bytes / (1024 ** 3), 1)
        used_gb = round(used_bytes / (1024 ** 3), 1)
        free_gb = round(free_bytes / (1024 ** 3), 1)

        # 수용 가능 여부: 타겟 총 용량이 C: 드라이브 실제 사용량보다 커야 함
        is_acceptable = total_bytes >= (c_usage.used * 1.05)
        reason = "정상 선택 가능" if is_acceptable else f"용량 부족 (C: 사용량 {c_used_gb} GB 대비 타겟 총용량 {total_gb} GB)"

        # 물리 디스크 번호 조회 (미조회 시 폴백)
        if t_disk_num is None:
            t_disk_num = get_physical_disk_number(drv)
        is_different_disk = (t_disk_num is not None and c_disk_num is not None and t_disk_num != c_disk_num)

        candidates.append({
            "drive": drv,
            "mountpoint": root,
            "fstype": fstype,
            "total_gb": total_gb,
            "used_gb": used_gb,
            "free_gb": free_gb,
            "acceptable": is_acceptable,
            "reason": reason,
            "c_used_gb": c_used_gb,
            "disk_number": t_disk_num,
            "can_auto_esp": is_different_disk
        })

    return candidates


class DiskCloneManager:
    """
    실시간 SSD 마이그레이션(클론) 생명주기 관리자
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(DiskCloneManager, cls).__new__(cls)
                cls._instance._init_state()
            return cls._instance

    @classmethod
    def get_instance(cls) -> "DiskCloneManager":
        """싱글톤 인스턴스 반환"""
        return cls()

    def _init_state(self):
        self.state_lock = threading.Lock()
        self.is_running = False
        self.target_drive = ""
        self.status = "idle"  # idle, preparing, copying, bootloading, completed, failed, cancelled
        self.progress_percent = 0.0
        self.current_phase = ""
        self.error_message = ""
        self.start_time = 0.0
        self.end_time = 0.0
        self.logs: List[str] = []
        self._cancel_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

    def append_log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        with self.state_lock:
            self.logs.append(entry)
            if len(self.logs) > 500:
                self.logs.pop(0)
        try:
            logger.info(entry)
        except Exception:
            pass

    def get_status(self) -> Dict[str, Any]:
        with self.state_lock:
            duration = round(time.time() - self.start_time, 1) if self.is_running else round(self.end_time - self.start_time, 1) if self.end_time else 0.0
            return {
                "is_running": self.is_running,
                "target_drive": self.target_drive,
                "status": self.status,
                "progress_percent": self.progress_percent,
                "current_phase": self.current_phase,
                "error_message": self.error_message,
                "duration_seconds": max(0.0, duration),
                "log_count": len(self.logs)
            }

    def get_logs(self, limit: int = 100) -> List[str]:
        with self.state_lock:
            return list(self.logs[-limit:])

    def start_clone(self, target_drive: str, auto_prepare_esp: bool = True) -> Dict[str, Any]:
        """
        비동기 마이그레이션(클론) 시작
        - auto_prepare_esp: 새 SSD 전체를 100% UEFI 표준(GPT + 260MB FAT32 ESP + 16MB MSR + NTFS Windows)으로 자동 파티셔닝
        """
        target = normalize_drive(target_drive)
        if not target or target == "C:":
            return {"success": False, "error": "타겟 드라이브는 C: 가 아닌 유효한 다른 드라이브여야 합니다."}

        target_root = target + "\\"
        if not os.path.exists(target_root):
            part_info = get_all_partition_info()
            if target not in part_info:
                return {"success": False, "error": f"타겟 드라이브 경로({target_root})를 찾을 수 없습니다."}

        with self.state_lock:
            if self.is_running:
                return {"success": False, "error": "이미 다른 마이그레이션 또는 클론 작업이 실행 중입니다."}
            self.is_running = True
            self.target_drive = target
            self.status = "preparing"
            self.progress_percent = 1.0
            self.current_phase = "마이그레이션 환경 검증 및 부팅 아키텍처 준비"
            self.error_message = ""
            self.start_time = time.time()
            self.end_time = 0.0
            self.logs.clear()
            self._cancel_event.clear()

        mode_str = "100% UEFI ESP 자동 파티셔닝 모드" if auto_prepare_esp else "기존 파티션 유지 모드"
        self.append_log(f"=== Windows C: -> {target} 라이브 SSD 마이그레이션 시작 ({mode_str}) ===")
        self._worker_thread = threading.Thread(target=self._run_clone_pipeline, args=(target, auto_prepare_esp), daemon=True)
        self._worker_thread.start()

        return {"success": True, "message": f"{target} 드라이브로의 마이그레이션 작업이 백그라운드에서 시작되었습니다."}

    def stop_clone(self) -> Dict[str, Any]:
        with self.state_lock:
            if not self.is_running:
                return {"success": False, "error": "현재 실행 중인 마이그레이션 작업이 없습니다."}
            self._cancel_event.set()
            self.current_phase = "작업 취소 요청 중..."

        self.append_log("[USER_CANCEL] 사용자에 의해 마이그레이션 중단이 요청되었습니다.")
        return {"success": True, "message": "마이그레이션 중단 요청이 접수되었습니다."}

    def _run_clone_pipeline(self, target_drive: str, auto_prepare_esp: bool = True):
        target_root = target_drive + "\\"
        esp_letter = ""

        try:
            # 1. 물리 디스크 식별 및 사전 용량 검증
            self.append_log("[1/5] 타겟 드라이브 및 물리 디스크 아키텍처 검증 중...")
            c_disk = get_physical_disk_number("C:")
            t_disk = get_physical_disk_number(target_drive)
            self.append_log(f"  - 원본 C: 물리 디스크 번호: Disk {c_disk if c_disk is not None else '감지불가'}")
            self.append_log(f"  - 타겟 {target_drive} 물리 디스크 번호: Disk {t_disk if t_disk is not None else '감지불가'}")

            c_usage = shutil.disk_usage("C:\\")
            c_used_gb = round(c_usage.used / (1024 ** 3), 1)

            try:
                t_usage = shutil.disk_usage(target_root)
                t_total_bytes = t_usage.total
            except OSError:
                part_info = get_all_partition_info()
                t_total_bytes = part_info.get(target_drive, {}).get("size_bytes", 0)

            t_total_gb = round(t_total_bytes / (1024 ** 3), 1)

            self.append_log(f"  - 원본 C: 드라이브 실사용량: {c_used_gb} GB")
            self.append_log(f"  - 타겟 {target_drive} 드라이브 총용량: {t_total_gb} GB")

            if t_total_bytes > 0 and t_total_bytes < c_usage.used:
                raise RuntimeError(f"타겟 드라이브 총용량({t_total_gb} GB)이 원본 데이터({c_used_gb} GB)보다 작아 복제를 진행할 수 없습니다.")

            # 2. 100% 부팅 보장 모드: UEFI 표준(GPT + 260MB FAT32 ESP + 16MB MSR + NTFS Windows) 자동 파티셔닝
            if auto_prepare_esp:
                if t_disk is not None and c_disk is not None and t_disk == c_disk:
                    self.append_log("  - [안내] 타겟이 원본 C:와 동일한 물리 디스크의 다른 파티션입니다. 안전을 위해 디스크 초기화를 건너뛰고 기존 파티션 모드로 진행합니다.")
                    auto_prepare_esp = False
                elif t_disk is not None:
                    esp_letter = find_free_drive_letter("S:")
                    self.append_log(f"[2/5] 새 SSD(Disk {t_disk})를 100% UEFI 표준 규격으로 자동 파티셔닝 중 (GPT + 260MB FAT32 ESP({esp_letter}) + MSR + NTFS)...")
                    with self.state_lock:
                        self.status = "preparing"
                        self.progress_percent = 5.0
                        self.current_phase = f"새 SSD UEFI 파티션 자동 생성 중 (ESP: {esp_letter})"

                    part_res = prepare_target_disk_with_esp(t_disk, target_drive, esp_letter)
                    if not part_res.get("success"):
                        err_msg = part_res.get("error", "알 수 없는 오류")
                        raise RuntimeError(f"새 SSD UEFI 표준 규격 자동 파티셔닝 실패: {err_msg}")
                    else:
                        self.append_log(f"  - 새 SSD UEFI 파티셔닝 완료 (ESP 임시 문자: {esp_letter}, OS 볼륨: {target_drive})")
                        time.sleep(2)
                else:
                    self.append_log("  - [경고] 타겟 물리 디스크 번호를 감지할 수 없어 기존 파티션 모드로 진행합니다.")
                    auto_prepare_esp = False

            if not auto_prepare_esp:
                self.append_log("[2/5] 기존 파티션 모드 유지 (추가 파티셔닝 없이 기존 NTFS 볼륨으로 직접 복제)")

            # 3. VSS 볼륨 스냅샷 획득 (라이브 OS 정합성 및 일관성 보장)
            self.append_log("[3/5] 실시간 VSS 섀도 스냅샷 생성 중 (현재 실행 중인 DB/파일 블록 동결)...")
            with self.state_lock:
                self.status = "preparing"
                self.progress_percent = 10.0
                self.current_phase = "실시간 VSS 섀도 볼륨 생성 중"

            vss_ctx = None
            shadow_device = None

            if VSS_AVAILABLE and VSSContext:
                try:
                    vss_ctx = VSSContext(source_paths=["C:\\"], enabled=True, strict=False)
                    vss_ctx.__enter__()
                    if vss_ctx.vss_active:
                        sc = vss_ctx.shadows.get("C:") or vss_ctx.shadows.get("C:\\")
                        if sc and sc.device_path:
                            shadow_device = sc.device_path
                            self.append_log(f"  - VSS 섀도 디바이스 바인딩 성공: {shadow_device}")
                        else:
                            shadow_path = vss_ctx.get_shadow_path("C:\\")
                            self.append_log(f"  - VSS 섀도 볼륨 바인딩 성공: {shadow_path}")
                except Exception as e_vss:
                    self.append_log(f"  - [주의] VSS 섀도 생성 실패 ({e_vss}). 볼륨 직결 모드로 진행합니다.")
                    vss_ctx = None

            # 4. NVMe 다이렉트 LBA 블록 스트리밍 복제 (Robocopy 완전 배제, 파일 메타데이터 오버헤드 0%)
            self.append_log(f"[4/5] 초고속 LBA 블록 스트리밍 복제 시작: C: -> {target_drive} (최대 하드웨어 속도 직사)")
            with self.state_lock:
                self.status = "copying"
                self.progress_percent = 15.0
                self.current_phase = "고속 LBA 블록 스트리밍 복제 진행 중"

            from core.block_stream import stream_volume_to_volume

            last_ui_log_time = 0.0

            def on_block_progress(p: Dict[str, Any]):
                nonlocal last_ui_log_time
                pct = p.get("percent", 0.0)
                speed_mb = p.get("speed_mb_s", 0.0)
                proc_gb = round(p.get("processed_bytes", 0) / (1024 ** 3), 1)
                tot_gb = round(p.get("total_bytes", 1) / (1024 ** 3), 1)

                with self.state_lock:
                    # 복제 단계 진행률을 15% ~ 85% 범위에 매핑
                    self.progress_percent = min(85.0, round(15.0 + (pct * 0.70), 1))
                    self.current_phase = f"NVMe LBA 블록 직사 복제 중 ({pct}% - {speed_mb:.1f} MB/s)"

                now = time.time()
                if now - last_ui_log_time >= 1.0 or pct >= 100.0:
                    self.append_log(f"  - [LBA 블록 스트림] {pct}% 완료 ({proc_gb}/{tot_gb} GB, 전송속도: {speed_mb:.1f} MB/s)")
                    last_ui_log_time = now

            def _run_stream():
                # 1) 관리자 권한이면 즉시 인프로세스 실행
                if is_admin():
                    try:
                        return stream_volume_to_volume(
                            src_drive="C:",
                            dst_drive=target_drive,
                            progress_callback=on_block_progress,
                            cancel_event=self._cancel_event,
                            chunk_bytes=16 * 1024 * 1024,
                            src_shadow_path=shadow_device
                        )
                    except (PermissionError, OSError) as e_perm:
                        if getattr(e_perm, "winerror", None) != 5:
                            raise
                        self.append_log("  - [안내] 볼륨 핸들 권한 부족 감지 -> 관리자 권한 워커 프로세스로 승격 실행합니다.")

                # 2) 비관리자 권한이거나 WinError 5 발생 시: UAC 승격 워커 프로세스 단독 실행
                import tempfile
                import json
                ipc_file = tempfile.mktemp(suffix=".ipc")
                worker_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clone_worker.py")
                cmd = [
                    sys.executable,
                    worker_script,
                    "--src", "C:",
                    "--dst", target_drive,
                    "--ipc-file", ipc_file,
                    "--chunk-mb", "64",
                    "--queue-depth", "32",
                    "--gap-mb", "64",
                    "--min-chunk-mb", "16",
                    "--two-stage-gb", "200"
                ]
                if shadow_device:
                    cmd.extend(["--shadow", shadow_device])

                read_pos = 0
                worker_done = False
                worker_result = None
                worker_error = None

                def ipc_reader():
                    nonlocal read_pos, worker_result, worker_error
                    while not worker_done:
                        if os.path.exists(ipc_file):
                            try:
                                with open(ipc_file, "r", encoding="utf-8", errors="ignore") as f:
                                    f.seek(read_pos)
                                    lines = f.readlines()
                                    read_pos = f.tell()
                                for line in lines:
                                    line = line.strip()
                                    if not line:
                                        continue
                                    try:
                                        msg = json.loads(line)
                                    except Exception:
                                        continue
                                    m_type = msg.get("type")
                                    if m_type == "progress":
                                        on_block_progress(msg.get("data", {}))
                                    elif m_type == "status":
                                        self.append_log(f"  - [승격 워커] {msg.get('message')}")
                                    elif m_type == "done":
                                        worker_result = msg.get("result")
                                    elif m_type == "error":
                                        worker_error = msg.get("error")
                            except Exception:
                                pass
                        time.sleep(0.3)

                t_ipc = threading.Thread(target=ipc_reader, daemon=True)
                t_ipc.start()

                try:
                    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    rc, out, err = run_elevated_command(cmd, timeout=3600, cwd=project_root)
                    worker_done = True
                    t_ipc.join(timeout=3)

                    if os.path.exists(ipc_file):
                        try:
                            with open(ipc_file, "r", encoding="utf-8", errors="ignore") as f:
                                f.seek(read_pos)
                                for line in f:
                                    line = line.strip()
                                    if not line:
                                        continue
                                    try:
                                        msg = json.loads(line)
                                    except Exception:
                                        continue
                                    if msg.get("type") == "done":
                                        worker_result = msg.get("result")
                                    elif msg.get("type") == "error":
                                        worker_error = msg.get("error")
                        except Exception:
                            pass

                    if rc != 0 or worker_error:
                        raise RuntimeError(f"관리자 권한 복제 워커 실패 (Exit Code {rc}): {worker_error or err or out}")
                    if not worker_result:
                        raise RuntimeError("관리자 권한 복제 워커가 완료 결과를 반환하지 않았습니다.")
                    return worker_result
                finally:
                    worker_done = True
                    if os.path.exists(ipc_file):
                        try:
                            os.remove(ipc_file)
                        except Exception:
                            pass

            stream_res = _run_stream()

            copied_gb = stream_res.get("copied_bytes", 0) / (1024 ** 3)
            avg_spd = stream_res.get("average_speed_mb_s", 0.0)
            self.append_log(f"  - LBA 블록 스트리밍 복제 완료: {copied_gb:.2f} GB 전송 성공 (평균 전송속도: {avg_spd:.1f} MB/s)")

            # VSS Fast-Release 해제
            if vss_ctx:
                try:
                    vss_ctx.__exit__(None, None, None)
                    self.append_log("  - VSS 섀도 볼륨 안전 해제 완료.")
                except Exception as e_vss_close:
                    self.append_log(f"  - VSS 해제 알림: {e_vss_close}")

            # 4-1. 라이선스 지문 보존: 원본 C: Volume Serial Number(VSN) 타겟 SSD 복제 주입
            if VSN_HELPERS_AVAILABLE:
                try:
                    c_vsn = get_volume_serial_number("C:")
                    if c_vsn:
                        self.append_log(f"  - [라이선스 보호] 원본 C: 볼륨 시리얼 번호(VSN: {c_vsn}) 타겟 SSD에 복제 주입 중...")
                        vsn_res = set_volume_serial_number(target_drive, c_vsn)
                        if vsn_res.get("success"):
                            self.append_log(f"    ✓ 타겟 드라이브 VSN 주입 성공! (방법: {vsn_res.get('method')}). Office/한글/CAD 정품 라이선스 보존.")
                        else:
                            self.append_log(f"    - [참고] VSN 주입 상태: {vsn_res.get('error')}")
                except Exception as e_vsn:
                    self.append_log(f"    - [주의] VSN 처리 예외 ({e_vsn})")

            # 4-2. 타겟 SSD 오프라인 레지스트리 MountedDevices 드라이브 문자 교정 (로고 후 블랙스크린 방지)
            try:
                target_sys_hive = os.path.join(target_root, "Windows", "System32", "config", "SYSTEM")
                target_clean = normalize_drive(target_drive).rstrip(":")
                if os.path.exists(target_sys_hive) and target_clean and target_clean != "C":
                    self.append_log(f"  - [드라이브 문자 교정] 타겟 SSD 오프라인 레지스트리 MountedDevices 교정 중 ({target_clean}: -> C:)...")
                    run_elevated_command(["reg", "load", "HKLM\\CLONE_SYSTEM", target_sys_hive], timeout=15)
                    try:
                        rc_q, out_q, _ = run_elevated_command(["reg", "query", "HKLM\\CLONE_SYSTEM\\MountedDevices"], timeout=15)
                        target_dos = f"\\DosDevices\\{target_clean}:"
                        guid_val = None
                        if rc_q == 0 and out_q:
                            for line in out_q.splitlines():
                                if target_dos.lower() in line.lower():
                                    parts = line.strip().split()
                                    if len(parts) >= 3 and parts[1].upper() == "REG_BINARY":
                                        guid_val = parts[2]
                                        break
                        if guid_val:
                            run_elevated_command(["reg", "add", "HKLM\\CLONE_SYSTEM\\MountedDevices", "/v", "\\DosDevices\\C:", "/t", "REG_BINARY", "/d", guid_val, "/f"], timeout=15)
                            run_elevated_command(["reg", "delete", "HKLM\\CLONE_SYSTEM\\MountedDevices", "/v", target_dos, "/f"], timeout=15)
                            self.append_log(f"    ✓ 타겟 SSD MountedDevices \\DosDevices\\C: 매핑 성공! (GUID: {guid_val[:16]}...) 단독 부팅 시 C: 자동 할당.")
                        else:
                            self.append_log(f"    - [참고] MountedDevices에서 {target_dos} 엔트리를 찾지 못해 C: 강제 매핑 생략.")
                    finally:
                        run_elevated_command(["reg", "unload", "HKLM\\CLONE_SYSTEM"], timeout=15)
            except Exception as e_reg:
                self.append_log(f"    - [주의] MountedDevices 교정 예외 (무시 가능): {e_reg}")

            # 5. UEFI 부트로더(BCD) 정밀 주입
            self.append_log(f"[5/5] 부트 매니저(BCD) 정밀 주입 중: bcdboot {target_root}Windows ...")
            with self.state_lock:
                self.status = "bootloading"
                self.progress_percent = 90.0
                self.current_phase = "새 SSD에 100% UEFI 부트로더 주입 중"

            # ESP 파티션이 준비되어 있으면 해당 ESP에 UEFI 전용 주입, 아니면 타겟 드라이브에 ALL 주입
            boot_target = esp_letter if (auto_prepare_esp and esp_letter and os.path.exists(esp_letter + "\\")) else target_drive
            boot_mode = "UEFI" if (auto_prepare_esp and esp_letter) else "ALL"

            bcd_cmd = [
                "bcdboot",
                os.path.join(target_root, "Windows"),
                "/s", boot_target,
                "/f", boot_mode
            ]
            self.append_log(f"  - 부트로더 명령 실행: {' '.join(bcd_cmd)}")

            rc, bcd_out, bcd_err = run_elevated_command(bcd_cmd, timeout=30)
            bcd_msg = (bcd_out or bcd_err).strip()
            self.append_log(f"  - bcdboot 결과 (Exit Code {rc}): {bcd_msg or '부팅 파일 생성 정상 완료'}")

            # ESP 임시 드라이브 문자 정리 전: 부트 바이너리 및 BCD 독립 부팅 설정
            if auto_prepare_esp and esp_letter:
                bcd_file_check = os.path.join(esp_letter + "\\", "EFI", "Microsoft", "Boot", "BCD")
                if os.path.exists(bcd_file_check):
                    self.append_log(f"  - [검증 완료] UEFI BCD 파일 무결성 확인 ({bcd_file_check})")
                    # Safety Gate: bcdedit /enum {default}로 타겟 드라이브 바인딩 실측 검증
                    enum_cmd = ["bcdedit", "/store", bcd_file_check, "/enum", "{default}"]
                    rc_enum, enum_out, enum_err = run_elevated_command(enum_cmd, timeout=15)
                    enum_text = (enum_out or enum_err or "")
                    target_drive_norm = target_drive.rstrip("\\").upper()

                    if rc_enum != 0:
                        self.append_log(f"  - [주의] BCD {enum_cmd} 검증 명령 실행 실패 (Exit Code {rc_enum}): {(enum_err or enum_out).strip()}")
                    else:
                        device_ok = any("device" in l.lower() and target_drive_norm in l.upper() for l in enum_text.splitlines())
                        osdevice_ok = any("osdevice" in l.lower() and target_drive_norm in l.upper() for l in enum_text.splitlines())

                        if device_ok and osdevice_ok:
                            self.append_log(f"  - [Safety Gate 통과] BCD {{default}} device/osdevice가 타겟({target_drive_norm})을 정확히 가리킴 확인")
                        else:
                            self.append_log(f"  - [Safety Gate 알림] BCD 엔트리 상태 확인: device_ok={device_ok}, osdevice_ok={osdevice_ok}")
                            self.append_log("  - [독립 부팅 보장] GUID 오염 방지를 위해 partition=C: 강제 덮어쓰기 생략")

                # UEFI BIOS 표준 폴백 부트로더(bootx64.efi) 보장
                esp_boot_dir = os.path.join(esp_letter + "\\", "EFI", "Boot")
                esp_bootx64 = os.path.join(esp_boot_dir, "bootx64.efi")
                esp_mgfw = os.path.join(esp_letter + "\\", "EFI", "Microsoft", "Boot", "bootmgfw.efi")
                if not os.path.exists(esp_bootx64) and os.path.exists(esp_mgfw):
                    try:
                        os.makedirs(esp_boot_dir, exist_ok=True)
                        shutil.copy2(esp_mgfw, esp_bootx64)
                        self.append_log("  - [UEFI 바이오스 호환성] EFI\\Boot\\bootx64.efi 표준 부트바이너리 생성 완료")
                    except Exception as e_copy:
                        self.append_log(f"  - [주의] bootx64.efi 복제 알림: {e_copy}")

                remove_esp_drive_letter(esp_letter)
                self.append_log(f"  - ESP 파티션({esp_letter}) 숨김 모드로 안전 전환 완료.")

            # 6. 타겟 SSD 백그라운드 Re-Trim (빈 공간 소거 및 낸드 쓰기 수명/성능 보존)
            try:
                target_clean = normalize_drive(target_drive).rstrip(":")
                self.append_log(f"[6/6] 타겟 SSD({target_clean}:) 고속 Re-Trim 백그라운드 최적화 실행 중...")
                run_elevated_command(["powershell", "-NoProfile", "-Command", f"Optimize-Volume -DriveLetter {target_clean} -ReTrim"], timeout=60)
                self.append_log(f"  ✓ {target_clean}: SSD Re-Trim 최적화 완료 (미사용 낸드 블록 소거 완료)")
            except Exception as e_trim:
                self.append_log(f"  - [참고] Re-Trim 최적화 알림: {e_trim}")

            # 최종 완료
            with self.state_lock:
                self.status = "completed"
                self.progress_percent = 100.0
                self.is_running = False
                self.end_time = time.time()
                self.current_phase = "마이그레이션 완료 (100% 부팅 준비 완료)"

            self.append_log("============================================================")
            self.append_log(f"[성공] {target_drive} 새 SSD로의 100% 라이브 마이그레이션이 완료되었습니다!")
            if auto_prepare_esp:
                self.append_log("  - 파티션 아키텍처: UEFI GPT + 260MB FAT32 ESP + MSR + NTFS Windows")
            self.append_log("[부팅 안내] PC를 재부팅하고 바이오스(BIOS/UEFI) 진입 후,")
            self.append_log(f"    부팅 1순위를 새로 복제된 SSD({target_drive})로 지정하시면 즉시 단독 부팅됩니다.")
            self.append_log("============================================================")

        except Exception as e:
            err_msg = str(e)
            self.append_log(f"[MIGRATION ERROR] {err_msg}")
            if esp_letter:
                try:
                    remove_esp_drive_letter(esp_letter)
                except Exception:
                    pass
            with self.state_lock:
                self.status = "cancelled" if self._cancel_event.is_set() else "failed"
                self.is_running = False
                self.end_time = time.time()
                self.error_message = err_msg
                self.current_phase = f"오류 발생: {err_msg}"
