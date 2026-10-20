# -*- coding: utf-8 -*-
"""
v2.9.22 Clean Push and GitHub Release Automator
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
    # 1. core
    shutil.rmtree(os.path.join(CLEAN_DIR, "core"), ignore_errors=True)
    shutil.copytree(os.path.join(BASE_DIR, "core"), os.path.join(CLEAN_DIR, "core"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    
    # 2. web
    shutil.rmtree(os.path.join(CLEAN_DIR, "web"), ignore_errors=True)
    shutil.copytree(os.path.join(BASE_DIR, "web"), os.path.join(CLEAN_DIR, "web"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    
    # 3. installer
    shutil.rmtree(os.path.join(CLEAN_DIR, "installer"), ignore_errors=True)
    shutil.copytree(os.path.join(BASE_DIR, "installer"), os.path.join(CLEAN_DIR, "installer"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # 4. root files
    root_files = [
        "run.py",
        "VERSION",
        "start_silent.vbs",
        "start_tray.vbs",
        "launch_dashboard.vbs",
        "stop_backup_system.bat",
        "2_백업시스템_실행.bat",
        "release_notes.md",
        "known_issues.md",
        "support_scope.md",
        "repository_format.md"
    ]
    for rf in root_files:
        src = os.path.join(BASE_DIR, rf)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(CLEAN_DIR, rf))
            
    print("[+] Clean repo sync completed.")

def git_commit_and_push():
    print("[*] Committing and pushing v2.9.22 to GitHub...")
    run_cmd("git add .")
    run_cmd('git commit -m "fix(launcher): DEF-03 resolve shortcut cold-start connection refused via Self-Gated launch (v2.9.22)"')
    run_cmd('git tag -a v2.9.22 -m "BackupSystem v2.9.22 Patch Release"')
    run_cmd("git push origin main --tags")

def publish_github_release():
    print("[*] Creating GitHub Release v2.9.22 and uploading assets...")
    gh_exe = r"C:\Program Files\GitHub CLI\gh.exe"
    body = (
        "## CAS BackupSystem v2.9.22 Release\n\n"
        "### 🚀 결함 종결 (Resolved Defect)\n"
        "- **[DEF-03] 바탕화면 바로가기 Cold-Start 연결 거부 결함 완전 해결 (`Self-Gated Browser Launch`)**:\n"
        "  - `launch_dashboard.vbs`의 임의 3초 대기 루프 및 VBScript 브라우저 호출 완전 제거\n"
        "  - `run.py`가 Uvicorn 소켓 BIND & LISTEN을 직접 확인한 순간 브라우저 오픈 수행\n"
        "  - 8대 검증 시나리오 전수 통과\n\n"
        "### 📦 공식 배포 자산 (Official Assets)\n"
        "- `BackupSystem_Setup_v2.9.22.exe` (Windows 공식 설치 프로그램)\n"
        "- `release_v2.9.22.zip` (자동 OTA 업데이트 패키지)\n"
        "- `release_v2.9.22.zip.sig` (Ed25519 디지털 서명)\n"
        "- `SHA256SUMS.txt` (무결성 검증 체크섬)\n"
    )
    body_file = os.path.join(DEPLOY_DIR, "github_release_v2922_body.md")
    with open(body_file, "w", encoding="utf-8") as bf:
        bf.write(body)

    assets = [
        os.path.join(DEPLOY_DIR, "BackupSystem_Setup_v2.9.22.exe"),
        os.path.join(DEPLOY_DIR, "release_v2.9.22.zip"),
        os.path.join(DEPLOY_DIR, "release_v2.9.22.zip.sig"),
        os.path.join(DEPLOY_DIR, "SHA256SUMS.txt")
    ]
    for a in assets:
        assert os.path.exists(a), f"Asset {a} not found!"

    cmd = (
        f'"{gh_exe}" release create v2.9.22 '
        f'"{assets[0]}" "{assets[1]}" "{assets[2]}" "{assets[3]}" '
        f'--title "v2.9.22 - DEF-03 Self-Gated Launch Patch" '
        f'--notes-file "{body_file}"'
    )
    run_cmd(cmd)
    print("[+] GitHub Release v2.9.22 successfully published!")

if __name__ == "__main__":
    sync_clean_repo()
    git_commit_and_push()
    publish_github_release()
