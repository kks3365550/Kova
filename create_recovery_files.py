import os

repo_dir = r"D:\MyBackup_Repository"
proj_dir = r"c:\Users\kksjmj\Desktop\ai\백업시스템"

bat_auto = """@echo off
chcp 65001 > nul
title 긴급 비상 재해 복구 - 원클릭 C드라이브 전체 복구

:: 관리자 권한 확인 및 자동 승격
openfiles >nul 2>&1
if '%errorlevel%' NEQ '0' (
    echo [안내] C드라이브 시스템 및 사용자 폴더 복구를 위해 관리자 권한으로 승격합니다...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"
set "REPO_DIR=%~dp0"
set "EMERGENCY_DIR=%REPO_DIR%emergency_restore"

set "PY_EXE="
if exist "%EMERGENCY_DIR%\\python\\python.exe" (
    set "PY_EXE=%EMERGENCY_DIR%\\python\\python.exe"
) else if exist "%REPO_DIR%.venv\\Scripts\\python.exe" (
    set "PY_EXE=%REPO_DIR%.venv\\Scripts\\python.exe"
) else (
    where python >nul 2>&1
    if '%errorlevel%' EQU '0' (
        set "PY_EXE=python"
    ) else (
        echo [오류] 파이썬 실행기를 찾을 수 없습니다.
        pause
        exit /b 1
    )
)

set "PY_SCRIPT=%EMERGENCY_DIR%\\emergency_restore.py"
if not exist "%PY_SCRIPT%" (
    if exist "%REPO_DIR%emergency_restore.py" (
        set "PY_SCRIPT=%REPO_DIR%emergency_restore.py"
    )
)

echo ======================================================================
echo   🛡️  원클릭 C드라이브 전체 자동 복구
echo ======================================================================
echo  - 백업 저장소의 가장 최신 스냅샷을 원본 C드라이브 경로로 복원합니다.
echo  - 프로그램, 소스코드, 드라이버, 레지스트리, 바탕화면 파일이 복구됩니다.
echo ======================================================================
echo.

"%PY_EXE%" "%PY_SCRIPT%" --auto

echo.
echo 복구 작업이 종료되었습니다.
pause
"""

bat_interactive = """@echo off
chcp 65001 > nul
title 긴급 비상 재해 복구 - 선택형 복구 매니저

:: 관리자 권한 확인 및 자동 승격
openfiles >nul 2>&1
if '%errorlevel%' NEQ '0' (
    echo [안내] C드라이브 폴더 복구를 위해 관리자 권한으로 승격합니다...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"
set "REPO_DIR=%~dp0"
set "EMERGENCY_DIR=%REPO_DIR%emergency_restore"

set "PY_EXE="
if exist "%EMERGENCY_DIR%\\python\\python.exe" (
    set "PY_EXE=%EMERGENCY_DIR%\\python\\python.exe"
) else if exist "%REPO_DIR%.venv\\Scripts\\python.exe" (
    set "PY_EXE=%REPO_DIR%.venv\\Scripts\\python.exe"
) else (
    where python >nul 2>&1
    if '%errorlevel%' EQU '0' (
        set "PY_EXE=python"
    ) else (
        echo [오류] 파이썬 실행기를 찾을 수 없습니다.
        pause
        exit /b 1
    )
)

set "PY_SCRIPT=%EMERGENCY_DIR%\\emergency_restore.py"
if not exist "%PY_SCRIPT%" (
    if exist "%REPO_DIR%emergency_restore.py" (
        set "PY_SCRIPT=%REPO_DIR%emergency_restore.py"
    )
)

"%PY_EXE%" "%PY_SCRIPT%"

echo.
echo 복구 작업이 종료되었습니다.
pause
"""

readme_text = """======================================================================
  🛡️ D드라이브 비상 재해 복구(Disaster Recovery) 가이드 [2-Track 하이브리드]
======================================================================

이 시스템은 'OS+오피스 베어메탈 이미지'와 '실시간 증분 스냅샷'의 2-Track 하이브리드 구조로 동작합니다.
재해 상황(윈도우 부팅 불가, C드라이브 포맷, 랜섬웨어 감염 등) 발생 시 다음 순서로 복구합니다:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
■ [트랙 1] 윈도우 OS + MS Office 베어메탈 뼈대 복원 (약 5~7분 소요)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 대상 상황: 윈도우가 부팅조차 안 되거나, C드라이브 완전 포맷 후 오피스까지 한 번에 복원할 때
- 복구 방법:
  1. 윈도우 고급 부팅 옵션 진입 (Shift 키를 누른 채 '다시 시작' 클릭, 또는 윈도우 설치 USB 부팅)
  2. [문제 해결] ➔ [고급 옵션] ➔ [시스템 이미지 복구] 선택
  3. D드라이브에 저장된 'D:\\WindowsImageBackup' 이미지가 자동 감지됩니다.
  4. 복구 진행 시 Windows OS, 부팅(EFI) 파티션, MS Office, 정품인증이 100% 원형 그대로 복구됩니다!
  ※ 이 작업 후 오피스를 다시 설치하거나 시리얼을 입력할 필요가 전혀 없습니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
■ [트랙 2] 최신 소스코드 / 작업환경 / 드라이버 증분 복원 (약 1~2분 소요)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 대상 상황: OS 뼈대 복구 완료 후, 또는 일상 작업 중 소스코드/파일이 유실/변조되었을 때
- 복구 방법 (파이썬 사전 설치 불필요!):
  1. 윈도우 부팅 후 탐색기에서 'D:\\MyBackup_Repository' 폴더를 엽니다.
  2. 다음 배치 파일 중 원하는 것을 더블 클릭하여 실행합니다:

     [방법 1] '원클릭_C드라이브_전체복구.bat'
       - 가장 최신 백업 시점의 모든 소스코드, AI 프로젝트, 가상환경, 하드웨어 드라이버,
         레지스트리, 바탕화면 파일들을 C드라이브 원래 위치로 즉시 자동 복원합니다.

     [방법 2] '선택복구_대화형.bat'
       - 원하는 백업 시점(스냅샷)을 직접 번호로 고르거나,
       - 특정 프로젝트/특정 파일만 골라서 복구하고 싶을 때 사용합니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
■ 기술적 원리:
  - 본 저장소의 'emergency_restore\\python' 폴더 내에 완전한 독립형 무설치 런타임(Python 3.11 + zstandard)이
    함께 내장되어 있어, 외부 인터넷 다운로드 없이 100% 오프라인 고속 복구가 가능합니다.
======================================================================
"""

def write_crlf(filepath: str, text: str):
    crlf_text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\r\n')
    with open(filepath, "wb") as f:
        f.write(crlf_text.encode("utf-8"))

for target_dir in [repo_dir, proj_dir]:
    if os.path.exists(target_dir):
        write_crlf(os.path.join(target_dir, "원클릭_C드라이브_전체복구.bat"), bat_auto)
        write_crlf(os.path.join(target_dir, "선택복구_대화형.bat"), bat_interactive)
        write_crlf(os.path.join(target_dir, "README_재해복구_가이드.txt"), readme_text)

print("Emergency restore bat files and guides generated successfully!")
