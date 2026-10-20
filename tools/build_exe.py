"""
tools/build_exe.py
PyInstaller 기반 백업시스템 독립 실행형 바이너리 빌드 자동화 스크립트
- Python 환경이 없는 대상 PC에서도 구동 가능한 배포 번들 빌드
- web/templates, web/static, core 모듈 등 에셋 자동 번들링
- dist/BackupSystem/ 디렉토리에 실행 바이너리 패키지 생성
"""

import os
import sys
import shutil
import subprocess

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

def build_package():
    print("=" * 60)
    print("[*] [PyInstaller] 백업시스템 상용 단일 실행 패키지 빌드 시작")
    print("=" * 60)

    # 1. 빌드 대상 경로 정의
    run_script = os.path.join(BASE_DIR, "run.py")
    dist_dir = os.path.join(BASE_DIR, "dist")
    build_dir = os.path.join(BASE_DIR, "build")
    target_dist = os.path.join(dist_dir, "BackupSystem")

    # 2. PyInstaller 실행 인자 구성
    # Windows에서는 경로 구분자로 세미콜론(;) 사용
    add_data = [
        f"{os.path.join(BASE_DIR, 'web', 'templates')};web/templates",
        f"{os.path.join(BASE_DIR, 'web', 'static')};web/static",
        f"{os.path.join(BASE_DIR, 'VERSION')};."
    ]

    hidden_imports = [
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespans",
        "uvicorn.lifespans.on",
        "fastapi",
        "starlette",
        "pydantic",
        "cryptography",
        "zstandard",
        "psutil"
    ]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--name", "BackupSystem",
        "--clean"
    ]

    for d in add_data:
        cmd.extend(["--add-data", d])

    for h in hidden_imports:
        cmd.extend(["--hidden-import", h])

    cmd.append(run_script)

    print(f"[*] 실행 명령: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=BASE_DIR)

    if result.returncode != 0:
        print("[!] PyInstaller 빌드 실패!")
        sys.exit(result.returncode)

    # 3. 추가 실행 스크립트 및 에셋을 dist/BackupSystem 에 복사
    files_to_copy = [
        "start_silent.vbs",
        "start_tray.vbs",
        "tray_app.py",
        "stop_backup_system.bat",
        "VERSION"
    ]

    for fname in files_to_copy:
        src = os.path.join(BASE_DIR, fname)
        dst = os.path.join(target_dist, fname)
        if os.path.exists(src):
            shutil.copy2(src, dst)
            print(f"[*] 배포 에셋 복사: {fname} -> dist/BackupSystem/{fname}")

    print("\n" + "=" * 60)
    print(f"[OK] 빌드 완료! 배포 디렉토리: {target_dist}")
    print("=" * 60)


if __name__ == "__main__":
    build_package()
