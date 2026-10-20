# -*- coding: utf-8 -*-
"""
v2.10.4 Clean Push and Official GitHub Release Publisher
Zero-leak public repository initializer and GitHub release publisher.
"""
import os
import shutil
import subprocess
import sys

BASE_DIR = r"c:\Users\kksjmj\Desktop\ai\백업시스템"
TEMP_DIR = os.environ.get("TEMP", r"C:\Windows\Temp")
CLEAN_DIR = os.path.join(TEMP_DIR, "BackupSystem_Public_Release")
DIST_DIR = os.path.join(BASE_DIR, "dist")
DEPLOY_DIR = r"D:\백업시스템_설치용"

def run_cmd(cmd, cwd=CLEAN_DIR):
    print(f"[*] Running: {cmd}")
    res = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, errors="replace")
    if res.returncode != 0:
        print(f"[-] Error (code {res.returncode}):\n{res.stderr}\n{res.stdout}")
    else:
        print(f"[+] Success:\n{res.stdout.strip()}")
    return res

def sync_clean_repo():
    print("[*] Syncing updated source files to clean repo...")
    if not os.path.exists(CLEAN_DIR):
        print(f"[*] Initializing clean repo at {CLEAN_DIR}...")
        os.makedirs(CLEAN_DIR, exist_ok=True)
        run_cmd("git init -b main", cwd=CLEAN_DIR)
        run_cmd('git remote add origin "https://github.com/kks3365550/BackupSystem.git"', cwd=CLEAN_DIR)

    # 1. core
    shutil.rmtree(os.path.join(CLEAN_DIR, "core"), ignore_errors=True)
    shutil.copytree(os.path.join(BASE_DIR, "core"), os.path.join(CLEAN_DIR, "core"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    
    # 2. web
    shutil.rmtree(os.path.join(CLEAN_DIR, "web"), ignore_errors=True)
    shutil.copytree(os.path.join(BASE_DIR, "web"), os.path.join(CLEAN_DIR, "web"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    
    # 3. installer
    shutil.rmtree(os.path.join(CLEAN_DIR, "installer"), ignore_errors=True)
    shutil.copytree(os.path.join(BASE_DIR, "installer"), os.path.join(CLEAN_DIR, "installer"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # 4. docs
    shutil.rmtree(os.path.join(CLEAN_DIR, "docs"), ignore_errors=True)
    if os.path.exists(os.path.join(BASE_DIR, "docs")):
        shutil.copytree(os.path.join(BASE_DIR, "docs"), os.path.join(CLEAN_DIR, "docs"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # 5. keys (only public key)
    keys_clean = os.path.join(CLEAN_DIR, "keys")
    os.makedirs(keys_clean, exist_ok=True)
    pub_key = os.path.join(BASE_DIR, "keys", "release_ed25519.pub")
    if os.path.exists(pub_key):
        shutil.copy2(pub_key, os.path.join(keys_clean, "release_ed25519.pub"))

    # 6. root files
    root_files = [
        "README.md",
        "run.py",
        "VERSION",
        "start_silent.vbs",
        "start_tray.vbs",
        "launch_dashboard.vbs",
        "stop_backup_system.bat",
        "requirements.txt",
        "disaster_recovery.py",
        "cli.py",
        "cli_backup.py",
        "release_notes.md",
        "release_build_manifest.md"
    ]
    for rf in root_files:
        src = os.path.join(BASE_DIR, rf)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(CLEAN_DIR, rf))
            
    print("[+] Clean repo sync completed.")

def git_commit_and_push():
    print("[*] Committing and pushing updates to GitHub...")
    run_cmd("git add -A")
    run_cmd('git commit -m "release: v2.10.4 - Fix non-elevated data/logs ACL and graceful tray termination on uninstall"')
    run_cmd("git tag -fa v2.10.4 -m \"v2.10.4 Official Release\"")
    run_cmd("git push origin main --force")
    run_cmd("git push origin v2.10.4 --force")

def publish_github_release():
    print("[*] Creating GitHub Release v2.10.4 and uploading assets...")
    gh_exe = r"C:\Program Files\GitHub CLI\gh.exe"
    body = (
        "## 🛡️ BackupSystem v2.10.4 Official Release\n\n"
        "> **Windows 일반 사용자 권한 ACL 정밀 패치 및 프로세스 안전 종료/완전 언인스톨 릴리즈**\n\n"
        "### ⚡ 주요 업데이트 내역 (Key Highlights)\n"
        "1. **`data/` 및 `logs/` 디렉터리 권한 정밀 패치 (WinError 5 해결)**:\n"
        "   - `installer/BackupSystem.iss`에 `[Dirs] Permissions: users-modify` 추가\n"
        "   - 프로그램 바이너리(`Program Files\\백업시스템\\`)는 관리자 읽기/실행 전용으로 보호 유지\n"
        "   - 기존 설치본의 잔존 파일에 대해서도 `icacls`로 재귀적 `users-modify` ACL 상속 보정 적용\n"
        "   - 일반 사용자 권한으로 실행되는 웹 서버 및 트레이 에이전트의 안정적 기동 보장\n\n"
        "2. **프로세스 안전 선별 종료 및 파일 잠금(Lock) 없는 완전 제거**:\n"
        "   - `stop_backup_system.bat` 고도화: 설치 디렉터리 스코핑 기반으로 타 Python/WScript 프로세스 100% 보호\n"
        "   - 언인스톨 시작 시(`InitializeUninstall` 및 `[UninstallRun]`) 트레이 에이전트와 백그라운드 서버 선제 종료\n"
        "   - 파일 잠금 해제 후 `C:\\Program Files\\백업시스템\\`을 잔여 파일 없이 깨끗하게 제거\n\n"
        "3. **권한 경계 확립 (Runtime Data vs Binaries)**:\n"
        "   - 런타임 데이터는 일반 사용자 쓰기 가능, 바이너리는 관리자 인스톨러(Setup.exe) 전용 교체 모델 정립\n\n"
        "### 📦 공식 배포 자산 (Official Assets)\n"
        "- `BackupSystem_Setup_v2.10.4.exe` (Windows 공식 원클릭 설치 프로그램)\n"
        "- `release_v2.10.4.zip` (자동 OTA 업데이트 패키지)\n"
        "- `release_v2.10.4.zip.sig` (Ed25519 디지털 서명)\n"
        "- `SHA256SUMS.txt` (무결성 검증 체크섬)\n"
    )
    body_file = os.path.join(DIST_DIR, "github_release_v2104_body.md")
    with open(body_file, "w", encoding="utf-8") as bf:
        bf.write(body)

    assets = [
        os.path.join(DIST_DIR, "BackupSystem_Setup_v2.10.4.exe"),
        os.path.join(DIST_DIR, "release_v2.10.4.zip"),
        os.path.join(DIST_DIR, "release_v2.10.4.zip.sig"),
        os.path.join(DIST_DIR, "SHA256SUMS.txt")
    ]
    for a in assets:
        assert os.path.exists(a), f"Asset {a} not found!"

    # Delete existing release if it exists to allow clean re-creation
    run_cmd(f'"{gh_exe}" release delete v2.10.4 -y', cwd=BASE_DIR)

    cmd = (
        f'"{gh_exe}" release create v2.10.4 '
        f'"{assets[0]}" "{assets[1]}" "{assets[2]}" "{assets[3]}" '
        f'--title "v2.10.4 - Non-Elevated data/logs ACL Patch & Graceful Tray Termination" '
        f'--notes-file "{body_file}"'
    )
    res = run_cmd(cmd, cwd=BASE_DIR)
    if res.returncode == 0:
        print("[+] GitHub Release v2.10.4 successfully published!")
    else:
        print("[-] Release creation returned non-zero.")

if __name__ == "__main__":
    sync_clean_repo()
    git_commit_and_push()
    publish_github_release()
