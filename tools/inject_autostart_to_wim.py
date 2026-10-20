# -*- coding: utf-8 -*-
"""
tools/inject_autostart_to_wim.py
wimlib-imagex를 사용하여 관리자 권한(UAC) 없이 순수 사용자 영역에서
E:\sources\boot.wim (Index 1 및 Index 2)에 winpeshl.ini 및 autorun_rescue.bat을 주입.
"""

import os
import sys
import subprocess

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIMLIB_EXE = os.path.join(PROJECT_ROOT, "tools", "wimlib-imagex.exe")
TARGET_WIM = r"E:\sources\boot.wim"
STAGE_DIR = os.path.join(PROJECT_ROOT, "tools", "_wim_stage")

WINPESHL_INI = """[LaunchApps]
%SYSTEMDRIVE%\\Windows\\System32\\wpeinit.exe
%SYSTEMDRIVE%\\Windows\\System32\\cmd.exe, /c %SYSTEMDRIVE%\\Windows\\System32\\autorun_rescue.bat
"""

AUTORUN_BAT = """@echo off
chcp 65001 >nul
title [백업시스템] 100%% 무결점 긴급 재해 복구 자동 시작기

echo.
echo ======================================================================
echo   🛡️ [백업시스템] WinPE 부팅 완료 - 긴급 재해 복구 콘솔 자동 실행 중
echo ======================================================================
echo [*] USB 드라이브 및 백업 시스템 자동 탐색 중...
echo.

set "TARGET_BAT=1_긴급복구콘솔_실행.bat"
set "FOUND_DRIVE="

:: 1차 즉시 탐색 (A~Z, X: 제외)
for %%d in (C D E F G H I J K L M N O P Q R S T U V W Y Z) do (
    if exist "%%d:\\%TARGET_BAT%" (
        set "FOUND_DRIVE=%%d:"
        goto :launch
    )
)

:retry
echo [!] USB 드라이브 장치 초기화 대기 중 (5초)...
ping 127.0.0.1 -n 6 >nul
for %%d in (C D E F G H I J K L M N O P Q R S T U V W Y Z) do (
    if exist "%%d:\\%TARGET_BAT%" (
        set "FOUND_DRIVE=%%d:"
        goto :launch
    )
)
echo [!] %TARGET_BAT% 파일을 찾지 못했습니다. 수동 명령 프롬프트를 유지합니다.
goto :end

:launch
echo [*] 긴급 복구 USB 감지: %FOUND_DRIVE%\\
echo [*] 복구 콘솔을 즉시 구동합니다...
echo.
cd /d "%FOUND_DRIVE%\\"
call "%FOUND_DRIVE%\\%TARGET_BAT%"

:end
cmd /k
"""


def main():
    print("=" * 65)
    print("  🛡️ [백업시스템] WinPE boot.wim 자동 부팅 직행 주입기")
    print("=" * 65)

    if not os.path.exists(WIMLIB_EXE):
        print(f"[!] wimlib-imagex.exe 없음: {WIMLIB_EXE}")
        sys.exit(1)

    if not os.path.exists(TARGET_WIM):
        print(f"[!] 대상 boot.wim 없음: {TARGET_WIM}")
        sys.exit(1)

    # 읽기 전용 속성 해제 (ISO에서 복사 시 기본 ReadOnly 상태 해제)
    import stat
    current_attr = os.stat(TARGET_WIM).st_mode
    os.chmod(TARGET_WIM, current_attr | stat.S_IWRITE)
    print(f"[*] {TARGET_WIM} 읽기 전용 해제 완료")

    os.makedirs(STAGE_DIR, exist_ok=True)

    # 1. winpeshl.ini 작성
    ini_file = os.path.join(STAGE_DIR, "winpeshl.ini")
    with open(ini_file, "w", encoding="ascii") as f:
        f.write(WINPESHL_INI.replace("\n", "\r\n"))

    # 2. autorun_rescue.bat 작성
    bat_file = os.path.join(STAGE_DIR, "autorun_rescue.bat")
    with open(bat_file, "wb") as f:
        f.write(AUTORUN_BAT.replace("\n", "\r\n").encode("utf-8"))

    cmd_file = os.path.join(STAGE_DIR, "update_commands.txt")
    # wimlib-imagex update 명령어 파일 생성
    # Windows 경로 역슬래시 처리
    ini_escaped = ini_file.replace("\\", "/")
    bat_escaped = bat_file.replace("\\", "/")
    
    with open(cmd_file, "w", encoding="utf-8") as f:
        f.write(f'add "{ini_escaped}" /Windows/System32/winpeshl.ini\n')
        f.write(f'add "{bat_escaped}" /Windows/System32/autorun_rescue.bat\n')

    # Index 1과 Index 2 모두 주입
    for idx in [1, 2]:
        print(f"[*] [Index {idx}] winpeshl.ini 및 autorun_rescue.bat 주입 중...")
        cmd = [
            WIMLIB_EXE,
            "update",
            TARGET_WIM,
            str(idx)
        ]
        stdin_commands = f'add "{ini_file}" /Windows/System32/winpeshl.ini\nadd "{bat_file}" /Windows/System32/autorun_rescue.bat\n'
        res = subprocess.run(cmd, input=stdin_commands, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode != 0:
            print(f"[!] Index {idx} 주입 실패:\n{res.stderr.strip() or res.stdout.strip()}")
            sys.exit(1)
        else:
            print(f"[+] [Index {idx}] 성공적으로 주입 완료!")

    # 정리
    try:
        import shutil
        shutil.rmtree(STAGE_DIR, ignore_errors=True)
    except Exception:
        pass

    print("=" * 65)
    print("  🎉 완벽 성공: WinPE 부팅 시 setup.exe 없이 즉시 긴급 복구 콘솔 자동 실행!")
    print("=" * 65)


if __name__ == "__main__":
    main()
