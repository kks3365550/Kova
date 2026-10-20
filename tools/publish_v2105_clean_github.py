# -*- coding: utf-8 -*-
"""
v2.10.5 Clean Push and Official GitHub Release Publisher
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
    run_cmd('git commit -m "release: v2.10.5 - Switch update detection to direct GitHub Releases API"')
    run_cmd("git tag -fa v2.10.5 -m \"v2.10.5 Official Release\"")
    run_cmd("git push origin main --force")
    run_cmd("git push origin v2.10.5 --force")

def publish_github_release():
    print("[*] Creating GitHub Release v2.10.5 and uploading assets...")
    gh_exe = r"C:\Program Files\GitHub CLI\gh.exe"
    body = (
        "## 🛡️ BackupSystem v2.10.5 Official Release\n\n"
        "> **GitHub 공식 Releases 기반 독립 업데이트 감지 전환 릴리즈**\n\n"
        "### ⚡ 주요 업데이트 내역 (Key Highlights)\n"
        "1. **GitHub 공식 Releases 직접 연동 (미니피씨 의존성 100% 제거)**:\n"
        "   - 사내 마스터 P2P(`100.72.224.71`) 조회를 완전히 배제하고, 글로벌 GitHub Releases API를 직접 조회\n"
        "   - 미니피씨 전원/가동 상태와 무관하게 모든 노드(데스크탑 등)가 인터넷만 되면 최신 버전을 실시간 감지\n"
        "   - 마스터 서버 연결 거부 시 '최신 버전'으로 오인되던 프론트엔드 예외 처리 결함 완벽 교정\n\n"
        "2. **원클릭 공식 릴리즈 페이지 직결**:\n"
        "   - 새 릴리즈 감지 배너에서 즉시 최신 인스톨러 및 릴리즈 노트를 확인할 수 있도록 GitHub 링크 직결 연동\n\n"
        "### 📦 공식 배포 자산 (Official Assets)\n"
        "- `BackupSystem_Setup_v2.10.5.exe` (Windows 공식 원클릭 설치 프로그램)\n"
        "- `release_v2.10.5.zip` (자동 OTA 업데이트 패키지)\n"
        "- `release_v2.10.5.zip.sig` (Ed25519 디지털 서명)\n"
        "- `SHA256SUMS.txt` (무결성 검증 체크섬)\n"
    )
    body_file = os.path.join(DIST_DIR, "github_release_v2105_body.md")
    with open(body_file, "w", encoding="utf-8") as bf:
        bf.write(body)

    assets = [
        os.path.join(DIST_DIR, "BackupSystem_Setup_v2.10.5.exe"),
        os.path.join(DIST_DIR, "release_v2.10.5.zip"),
        os.path.join(DIST_DIR, "release_v2.10.5.zip.sig"),
        os.path.join(DIST_DIR, "SHA256SUMS.txt")
    ]
    for a in assets:
        assert os.path.exists(a), f"Asset {a} not found!"

    run_cmd(f'"{gh_exe}" release delete v2.10.5 -y', cwd=BASE_DIR)

    cmd = (
        f'"{gh_exe}" release create v2.10.5 '
        f'"{assets[0]}" "{assets[1]}" "{assets[2]}" "{assets[3]}" '
        f'--title "v2.10.5 - Standalone GitHub Releases Update Detection" '
        f'--notes-file "{body_file}"'
    )
    res = run_cmd(cmd, cwd=BASE_DIR)
    if res.returncode == 0:
        print("[+] GitHub Release v2.10.5 successfully published!")
    else:
        print("[-] Release creation returned non-zero.")

if __name__ == "__main__":
    sync_clean_repo()
    git_commit_and_push()
    publish_github_release()
