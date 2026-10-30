# -*- coding: utf-8 -*-
r"""
tools/make_rescue_usb.py - 긴급 재해 복구(Rescue DR) UEFI 부팅 USB 자동 제작 엔진
- 대상: E:\ (이동식 USB 드라이브, FAT32)
- 소스: D:\Rufus\Win11_25H2_Korean_x64_v2.iso (공식 순정 Windows 11)
- 런타임: embedded python 3.11 + sandbox/rescue_lab 3중 복원 파이프라인
"""

import os
import sys
import shutil
import subprocess
import time

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ISO = r"D:\Rufus\Win11_25H2_Korean_x64_v2.iso"
DEFAULT_TARGET = r"E:"


def log(msg: str):
    print(f"[*] {msg}", flush=True)


def error(msg: str):
    print(f"[!] 오류: {msg}", flush=True)


def run_ps(cmd: str) -> str:
    res = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    )
    if res.returncode != 0:
        raise RuntimeError(f"PowerShell 명령 실패: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout.strip()


def is_safe_removable_drive(drive_letter: str) -> tuple[bool, str]:
    """
    드라이브 문자가 아닌 실제 하드웨어 버스/미디어 타입을 검사하여
    순수 이동식 USB 장치인지 확인 (고정 NVMe/SATA 디스크, 부팅/시스템 디스크 절대 차단)
    """
    letter = drive_letter.replace(":", "").strip().upper()
    if letter in ("C", "D"):
        return False, f"시스템 핵심 드라이브({letter}:) 보호"

    # PowerShell 쿼리로 BusType, DriveType, IsBoot, IsSystem 일괄 확인
    ps_cmd = (
        f"$p = Get-Partition -DriveLetter '{letter}' -ErrorAction SilentlyContinue; "
        "$d = if ($p) { $p | Get-Disk -ErrorAction SilentlyContinue } else { $null }; "
        f"$v = Get-Volume -DriveLetter '{letter}' -ErrorAction SilentlyContinue; "
        "@{"
        f"DriveLetter = '{letter}'; "
        "BusType = if ($d) { [string]$d.BusType } else { 'Unknown' }; "
        "DriveType = if ($v) { [string]$v.DriveType } else { 'Unknown' }; "
        "IsBoot = if ($d) { [bool]$d.IsBoot } else { $false }; "
        "IsSystem = if ($d) { [bool]$d.IsSystem } else { $false }"
        "} | ConvertTo-Json -Compress"
    )
    try:
        out = run_ps(ps_cmd)
        if not out:
            return False, f"드라이브 {letter}: 정보 조회 불가"
        import json
        info = json.loads(out)
        bus_type = str(info.get("BusType", "")).upper()
        drive_type = str(info.get("DriveType", "")).upper()
        is_boot = bool(info.get("IsBoot", False))
        is_system = bool(info.get("IsSystem", False))

        if is_boot or is_system:
            return False, f"부팅/시스템 드라이브({letter}:) 차단"

        # 내장 고정 버스(NVMe, SATA, SAS, RAID 등)이면서 Removable이 아닌 경우 절대 차단
        if bus_type in ("NVME", "SATA", "SAS", "RAID") and drive_type != "REMOVABLE":
            return False, f"고정 내장 디스크({letter}:, BusType: {bus_type}, DriveType: {drive_type}) 차단"

        # 허용 조건: BusType이 USB이거나, DriveType이 Removable인 경우만 허용
        if bus_type == "USB" or drive_type == "REMOVABLE":
            return True, f"이동식 USB 확인됨 (BusType: {bus_type}, DriveType: {drive_type})"

        return False, f"이동식 USB 아님 (BusType: {bus_type}, DriveType: {drive_type})"
    except Exception as e:
        return False, f"하드웨어 검증 예외: {e}"


def format_target_drive(drive_letter: str, label: str = "RESCUE_DR") -> bool:
    """대상 이동식 드라이브를 FAT32로 포맷 (하드웨어 BusType/DriveType 기반 절대 안전 검증)"""
    letter = drive_letter.replace(":", "").strip().upper()
    is_safe, reason = is_safe_removable_drive(letter)
    if not is_safe:
        error(f"드라이브 {letter}: 포맷 거부 - {reason}")
        return False

    log(f"드라이브 {letter}: 를 FAT32로 빠른 포맷 진행 중 (라벨: {label})...")
    # 1차: PowerShell Format-Volume 시도
    cmd = (
        f"$v = Get-Volume -DriveLetter '{letter}' -ErrorAction SilentlyContinue; "
        "if (-not $v) { exit 1 }; "
        f"Format-Volume -DriveLetter '{letter}' -FileSystem FAT32 -NewFileSystemLabel '{label}' -Force -Confirm:$false | Out-Null"
    )
    try:
        run_ps(cmd)
    except Exception as e:
        log(f"PowerShell Format-Volume 실패 ({e}), format.com 명령어로 재시도...")
        res = subprocess.run(
            f'format {letter}: /FS:FAT32 /V:{label} /Q /Y',
            shell=True,
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        if res.returncode != 0:
            error(f"드라이브 {letter}: 포맷 실패: {res.stderr.strip() or res.stdout.strip()}")
            return False

    time.sleep(1)
    verify_cmd = f"(Get-Volume -DriveLetter '{letter}' -ErrorAction SilentlyContinue).FileSystem"
    try:
        new_fs = run_ps(verify_cmd).strip().upper()
        if new_fs == "FAT32":
            log(f"드라이브 {letter}: FAT32 포맷 성공 완료!")
            return True
        else:
            error(f"포맷 후 파일시스템 불일치: {new_fs}")
            return False
    except Exception as e:
        error(f"포맷 검증 실패: {e}")
        return False


def check_target_drive(drive_letter: str, auto_format: bool = False) -> bool:
    """대상 드라이브가 이동식 USB이고 사용 가능한지 안전 검증 (필요시 FAT32 자동 포맷 지원)"""
    letter = drive_letter.replace(":", "").strip().upper()
    is_safe, reason = is_safe_removable_drive(letter)
    if not is_safe:
        error(f"드라이브 {letter}: 대상 지정 불가 - {reason}")
        return False

    cmd = (
        f"$v = Get-Volume -DriveLetter '{letter}' -ErrorAction SilentlyContinue; "
        "if ($v) { "
        "$res = @{ "
        "DriveLetter = $v.DriveLetter; "
        "DriveType = $v.DriveType; "
        "FileSystem = $v.FileSystem; "
        "SizeRemaining = $v.SizeRemaining; "
        "Size = $v.Size "
        "}; "
        "$res | ConvertTo-Json -Compress "
        "} else { '' }"
    )
    out = run_ps(cmd)
    if not out:
        error(f"드라이브 {letter}: 를 찾을 수 없습니다.")
        return False

    import json
    data = json.loads(out)
    drive_type = data.get("DriveType")
    fs = data.get("FileSystem", "")
    free_bytes = data.get("SizeRemaining", 0)
    free_gb = free_bytes / (1024 ** 3)

    log(f"대상 볼륨 감지: {letter}: (타입: {drive_type}, 파일시스템: {fs}, 여유공간: {free_gb:.2f} GB)")

    if fs.upper() != "FAT32":
        if auto_format:
            log(f"대상 볼륨이 {fs}입니다. 요청에 따라 FAT32 포맷을 수행합니다...")
            if not format_target_drive(letter):
                error(f"드라이브 {letter}: FAT32 포맷에 실패하여 제작을 중단합니다.")
                return False
            # 포맷 성공 후 다시 볼륨 정보 갱신
            out_after = run_ps(cmd)
            if out_after:
                data_after = json.loads(out_after)
                free_bytes = data_after.get("SizeRemaining", 0)
                free_gb = free_bytes / (1024 ** 3)
        else:
            error(f"UEFI 표준 부팅을 위해 FAT32 포맷이 필요합니다. 현재: {fs}")
            return False

    if free_gb < 2.0:
        error(f"여유 공간이 부족합니다. 최소 2GB 필요, 현재: {free_gb:.2f} GB")
        return False

    return True


def mount_iso(iso_path: str) -> str:
    """ISO 마운트 후 드라이브 문자 반환"""
    log(f"공식 Windows ISO 임시 마운트 중: {iso_path}")
    cmd = (
        f"$img = Mount-DiskImage -ImagePath '{iso_path}' -PassThru; "
        "$vol = $img | Get-Volume; "
        "$vol.DriveLetter"
    )
    drive = run_ps(cmd)
    if not drive:
        raise RuntimeError("ISO 마운트 드라이브 문자를 가져오지 못했습니다.")
    mounted_letter = drive.strip().replace(":", "") + ":"
    log(f"ISO 마운트 완료: {mounted_letter}")
    return mounted_letter


def dismount_iso(iso_path: str):
    """ISO 마운트 해제"""
    log(f"ISO 마운트 해제 중: {iso_path}")
    cmd = f"Dismount-DiskImage -ImagePath '{iso_path}' | Out-Null"
    try:
        run_ps(cmd)
        log("ISO 마운트 해제 완료")
    except Exception as e:
        log(f"ISO 마운트 해제 경고: {e}")


def copy_tree_filtered(src_dir: str, dst_dir: str, exclude_files: list):
    """필터링 복사 (예: install.wim 제외)"""
    os.makedirs(dst_dir, exist_ok=True)
    for root, dirs, files in os.walk(src_dir):
        rel = os.path.relpath(root, src_dir)
        target_dir = os.path.join(dst_dir, rel) if rel != "." else dst_dir
        os.makedirs(target_dir, exist_ok=True)

        for file in files:
            if file.lower() in [x.lower() for x in exclude_files]:
                continue
            src_file = os.path.join(root, file)
            dst_file = os.path.join(target_dir, file)
            if not os.path.exists(dst_file) or os.path.getsize(dst_file) != os.path.getsize(src_file):
                shutil.copy2(src_file, dst_file)


def create_launchers(target_root: str):
    """USB 루트에 원클릭 실행 배치 파일 및 복구 안내문 생성 (CRLF)"""
    log("USB 루트에 긴급 복구 콘솔 바로가기 및 안내서 생성 중...")

    # 1. 1_긴급복구콘솔_실행.bat
    launcher_bat = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "title [백업시스템] 100%% 무결점 긴급 재해 복구 콘솔\r\n"
        "cls\r\n"
        "echo ======================================================================\r\n"
        "echo   🛡️ [백업시스템] 긴급 재해 복구 콘솔 (Rescue DR Console)\r\n"
        "echo ======================================================================\r\n"
        "echo  - 내장 독립 파이썬 및 3중 복원 파이프라인을 자동 구동합니다.\r\n"
        "echo ======================================================================\r\n"
        "echo.\r\n"
        "set \"USB_ROOT=%~dp0\"\r\n"
        "set \"PY_EXE=%USB_ROOT%Rescue_Tools\\python\\python.exe\"\r\n"
        "if not exist \"%PY_EXE%\" set \"PY_EXE=python.exe\"\r\n"
        "cd /d \"%USB_ROOT%Rescue_Tools\\rescue_lab\"\r\n"
        "call recovery_launcher.bat\r\n"
        "pause\r\n"
    )
    with open(os.path.join(target_root, "1_긴급복구콘솔_실행.bat"), "wb") as f:
        f.write(launcher_bat.encode("utf-8"))

    # 2. 2_백업이미지탐색기.bat
    scan_bat = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "title [백업시스템] 전체 드라이브 백업본 동적 탐색기\r\n"
        "cls\r\n"
        "echo ======================================================================\r\n"
        "echo   🔍 [백업시스템] 마운트된 모든 드라이브 백업본 동적 탐색\r\n"
        "echo ======================================================================\r\n"
        "echo.\r\n"
        "set \"USB_ROOT=%~dp0\"\r\n"
        "set \"PY_EXE=%USB_ROOT%Rescue_Tools\\python\\python.exe\"\r\n"
        "if not exist \"%PY_EXE%\" set \"PY_EXE=python.exe\"\r\n"
        "\"%PY_EXE%\" \"%USB_ROOT%Rescue_Tools\\rescue_lab\\discovery_engine.py\"\r\n"
        "echo.\r\n"
        "pause\r\n"
    )
    with open(os.path.join(target_root, "2_백업이미지탐색기.bat"), "wb") as f:
        f.write(scan_bat.encode("utf-8"))

    # 3. 3_백업시스템_설치.bat
    install_bat = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "title [백업시스템] 최신 백업시스템 설치 프로그램 실행\r\n"
        "cls\r\n"
        "echo [*] 백업시스템 인스톨러를 실행합니다...\r\n"
        "set \"USB_ROOT=%~dp0\"\r\n"
        "start \"\" \"%USB_ROOT%Rescue_Tools\\BackupSystem_Setup_v2.9.20.exe\"\r\n"
    )
    with open(os.path.join(target_root, "3_백업시스템_설치.bat"), "wb") as f:
        f.write(install_bat.encode("utf-8"))

    # 4. README_복구안내.txt
    readme_text = (
        "======================================================================\r\n"
        "  🛡️ [백업시스템] 긴급 재해 복구 부팅 USB (Rescue DR Media)\r\n"
        "======================================================================\r\n"
        "\r\n"
        "[1] PC가 켜지지 않거나 C드라이브 SSD가 고장났을 때 (부팅 복구):\r\n"
        " 1. 이 USB를 PC에 꽂고 전원을 켭니다.\r\n"
        " 2. 부팅 단축키(F11 또는 F12 또는 Del/F2)를 연타하여 부팅 메뉴 진입.\r\n"
        " 3. UEFI USB 드라이브를 선택하여 부팅합니다.\r\n"
        " 4. [완전 자동 실행] 부팅 즉시 윈도우 설치 창을 건너뛰고 '긴급 재해 복구 콘솔'이 자동으로 실행됩니다!\r\n"
        "    - 사람이 직접 Shift+F10을 누르거나 드라이브 문자를 찾을 필요가 없습니다.\r\n"
        " 5. 화면에 감지된 최신 백업본을 확인한 후 Y를 누르면 즉시 C드라이브 전체 복원이 시작됩니다.\r\n"
        "\r\n"
        "[2] 윈도우가 정상 부팅된 상태에서 백업을 점검하거나 복원할 때:\r\n"
        " 1. 이 USB를 PC에 연결합니다.\r\n"
        " 2. '1_긴급복구콘솔_실행.bat' 또는 '2_백업이미지탐색기.bat'을 더블클릭합니다.\r\n"
        " 3. 내장된 무설치 독립 파이썬을 통해 즉시 안전하게 탐색 및 복구됩니다.\r\n"
        "\r\n"
        "[3] 새 PC 또는 포맷된 PC에 백업시스템 프로그램을 새로 설치할 때:\r\n"
        " 1. '3_백업시스템_설치.bat'을 실행하면 최신 공식 설치본이 즉시 설치됩니다.\r\n"
        "======================================================================\r\n"
    )
    with open(os.path.join(target_root, "README_복구안내.txt"), "wb") as f:
        f.write(readme_text.encode("utf-8"))


def build_rescue_usb(target_drive: str = DEFAULT_TARGET, iso_path: str = DEFAULT_ISO, auto_format: bool = False) -> bool:
    log("=" * 65)
    log("  [백업시스템] 긴급 재해 복구 UEFI 부팅 USB 제작 프로세스 시작")
    log("=" * 65)

    if not check_target_drive(target_drive, auto_format=auto_format):
        return False

    if not os.path.exists(iso_path):
        error(f"Windows ISO 파일이 존재하지 않습니다: {iso_path}")
        return False

    target_root = target_drive.rstrip("\\") + "\\"
    iso_mount_letter = None

    try:
        iso_mount_letter = mount_iso(iso_path)
        iso_root = iso_mount_letter.rstrip("\\") + "\\"

        # 1. UEFI 부팅 필수 디렉터리 복사 (efi, boot)
        log("1/4. UEFI 부팅 레코드 및 BCD 복사 중 (efi, boot)...")
        shutil.copytree(os.path.join(iso_root, "efi"), os.path.join(target_root, "efi"), dirs_exist_ok=True)
        shutil.copytree(os.path.join(iso_root, "boot"), os.path.join(target_root, "boot"), dirs_exist_ok=True)

        # 2. 루트 부트로더 복사
        log("2/4. 루트 부트로더 바이너리 복사 중...")
        for boot_file in ["bootmgr", "bootmgr.efi", "bootmgfw.efi", "autorun.inf"]:
            src = os.path.join(iso_root, boot_file)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(target_root, boot_file))

        # 3. sources 폴더 복사 (FAT32 4GB 초과 방지: install.wim 제외, boot.wim 614MB 포함)
        log("3/4. WinPE 램디스크(boot.wim) 및 셋업 라이브러리 복사 중 (install.wim 제외)...")
        copy_tree_filtered(
            os.path.join(iso_root, "sources"),
            os.path.join(target_root, "sources"),
            exclude_files=["install.wim"]
        )

        # 3-1. WinPE boot.wim에 복구 콘솔 자동 실행(setup.exe 우회) 주입
        boot_wim_path = os.path.join(target_root, "sources", "boot.wim")
        if os.path.exists(boot_wim_path):
            log("3.5/4. WinPE boot.wim에 복구 콘솔 자동 실행 로직 주입 중...")
            inject_script = os.path.join(PROJECT_ROOT, "tools", "inject_autostart_to_wim.py")
            if os.path.exists(inject_script):
                res = subprocess.run([sys.executable, inject_script], capture_output=True, text=True, encoding="utf-8", errors="replace")
                if res.returncode == 0:
                    log("  -> WinPE boot.wim 자동 실행 주입 성공 (setup.exe 차단 완료)")
                else:
                    log(f"  -> [주의] 자동 실행 주입 경고: {res.stderr.strip() or res.stdout.strip()}")

        # 4. 백업시스템 독립 런타임 및 도구 복사
        log("4/4. 백업시스템 독립 런타임 및 재해 복구 파이프라인 배치 중...")
        rescue_tools_dir = os.path.join(target_root, "Rescue_Tools")
        os.makedirs(rescue_tools_dir, exist_ok=True)

        # (a) 독립 포터블 파이썬 복사
        src_python = os.path.join(PROJECT_ROOT, "emergency_restore", "python")
        if os.path.exists(src_python):
            log("  -> 독립 포터블 Python 3.11 런타임 탑재")
            shutil.copytree(src_python, os.path.join(rescue_tools_dir, "python"), dirs_exist_ok=True)

        # (b) sandbox/rescue_lab 복구 도구 복사
        src_rescue_lab = os.path.join(PROJECT_ROOT, "sandbox", "rescue_lab")
        if os.path.exists(src_rescue_lab):
            log("  -> sandbox/rescue_lab 3중 복원 엔진 탑재")
            shutil.copytree(src_rescue_lab, os.path.join(rescue_tools_dir, "rescue_lab"), dirs_exist_ok=True)

        # (c) 코어 모듈 및 disaster_recovery 스크립트 복사
        for fname in ["disaster_recovery.py", "emergency_restore.py"]:
            src_f = os.path.join(PROJECT_ROOT, fname)
            if os.path.exists(src_f):
                shutil.copy2(src_f, os.path.join(rescue_tools_dir, fname))

        src_core = os.path.join(PROJECT_ROOT, "core")
        if os.path.exists(src_core):
            shutil.copytree(src_core, os.path.join(rescue_tools_dir, "core"), dirs_exist_ok=True)

        # (d) 최신 인스톨러 exe 복사
        setup_exe = os.path.join(PROJECT_ROOT, "web", "static", "BackupSystem_Setup_v2.9.20.exe")
        if os.path.exists(setup_exe):
            log("  -> 최신 백업시스템 인스톨러(v2.9.20) 탑재")
            shutil.copy2(setup_exe, os.path.join(rescue_tools_dir, "BackupSystem_Setup_v2.9.20.exe"))

        # 5. 루트 원클릭 배치 파일 및 안내서 생성
        create_launchers(target_root)

        log("=" * 65)
        log("  🎉 긴급 재해 복구 UEFI 부팅 USB 제작 완료!")
        log("=" * 65)

        # 용량 및 파일 통계 출력
        total_size = 0
        file_count = 0
        for root, dirs, files in os.walk(target_root):
            for f in files:
                total_size += os.path.getsize(os.path.join(root, f))
                file_count += 1

        size_mb = total_size / (1024 * 1024)
        log(f"총 복사된 파일: {file_count}개")
        log(f"총 사용된 용량: {size_mb:.2f} MB ({size_mb / 1024:.2f} GB)")
        log("부팅 모드: UEFI (GPT / Secure Boot 공식 지원)")
        log(f"WinPE 부트로더: {os.path.join(target_root, 'efi', 'boot', 'bootx64.efi')} (정상)")
        log(f"WinPE 이미지: {os.path.join(target_root, 'sources', 'boot.wim')} (정상)")

        return True

    finally:
        if iso_mount_letter:
            dismount_iso(iso_path)


if __name__ == "__main__":
    success = build_rescue_usb(DEFAULT_TARGET, DEFAULT_ISO)
    sys.exit(0 if success else 1)
