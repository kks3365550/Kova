# -*- coding: utf-8 -*-
"""
v2.9.21 Clean Push and GitHub Release Automator
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
    
    # 3. root files
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
    print("[*] Committing and pushing v2.9.21 to GitHub...")
    run_cmd("git add .")
    run_cmd('git commit -m "fix(core): DEF-02 resolve duplicate startup & integrate GitHub OTA (v2.9.21)"')
    run_cmd('git tag -a v2.9.21 -m "BackupSystem v2.9.21 Patch Release"')
    run_cmd("git push origin main --tags")

def publish_github_release():
    print("[*] Creating GitHub Release v2.9.21 and uploading assets...")
    gh_exe = r"C:\Program Files\GitHub CLI\gh.exe"
    body_file = os.path.join(DEPLOY_DIR, "github_release_v2921_body.md")
    
    assets = [
        os.path.join(DEPLOY_DIR, "BackupSystem_Setup_v2.9.21.exe"),
        os.path.join(DEPLOY_DIR, "release_v2.9.21.zip"),
        os.path.join(DEPLOY_DIR, "release_v2.9.21.zip.sig"),
        os.path.join(DEPLOY_DIR, "SHA256SUMS.txt")
    ]
    
    assets_arg = " ".join([f'"{a}"' for a in assets])
    cmd = f'"{gh_exe}" release create v2.9.21 --repo kks3365550/BackupSystem --title "BackupSystem v2.9.21 Patch Release" --notes-file "{body_file}" {assets_arg}'
    
    res = subprocess.run(cmd, cwd=BASE_DIR, shell=True, capture_output=True, text=True, errors="replace")
    if res.returncode == 0:
        print(f"[SUCCESS] v2.9.21 Release published:\n{res.stdout.strip()}")
        return True
    else:
        print(f"[-] Failed to publish release:\n{res.stderr}\n{res.stdout}")
        return False

if __name__ == "__main__":
    sync_clean_repo()
    git_commit_and_push()
    publish_github_release()
