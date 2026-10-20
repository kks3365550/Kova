# -*- coding: utf-8 -*-
"""
v2.9.23 Clean Push and GitHub Release Automator
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
    print("[*] Committing and pushing v2.9.23 to GitHub...")
    run_cmd("git add .")
    run_cmd('git commit -m "feat(perf): release v2.9.23 - Tier 1 low-risk optimizations (batch replication queue & st_mode reuse)"')
    run_cmd('git tag -a v2.9.23 -m "BackupSystem v2.9.23 Tier 1 Performance Release"')
    run_cmd("git push origin main --tags")

def publish_github_release():
    print("[*] Creating GitHub Release v2.9.23 and uploading assets...")
    gh_exe = r"C:\Program Files\GitHub CLI\gh.exe"
    body = (
        "## CAS BackupSystem v2.9.23 Release\n\n"
        "### ⚡ 성능 개선 (Performance Optimizations - Tier 1)\n"
        "- **`/api/snapshots` N+1 쿼리 병목 완전 제거 (배치 쿼리 도입)**:\n"
        "  - `core/replication_queue.py`: `ReplicationQueueManager.get_status_batch()` 신설 (CHUNK_SIZE=500 안전 분할)\n"
        "  - `web/app.py`: `list_snapshots()`에서 스냅샷마다 매번 수행하던 단건 쿼리를 저장소당 1회의 배치 조회로 전환\n"
        "- **`core/hasher.py` 중복 Win32 파일 타입 커널 조회 제거**:\n"
        "  - `get_file_stat()`에서 이미 획득한 `st.st_mode` 비트마스크(`stat.S_ISDIR`, `stat.S_ISREG`)를 재사용하여 파일 탐색 시 중복 `os.path.isdir`, `os.path.isfile` 시스템 콜 2회 제거\n"
        "  - 심볼릭 링크/정션 포인트는 `stat.S_ISLNK` 및 안전한 예외 처리를 결합하여 순환 참조 방어력 완벽 유지\n\n"
        "### 📦 공식 배포 자산 (Official Assets)\n"
        "- `BackupSystem_Setup_v2.9.23.exe` (Windows 공식 설치 프로그램)\n"
        "- `release_v2.9.23.zip` (자동 OTA 업데이트 패키지)\n"
        "- `release_v2.9.23.zip.sig` (Ed25519 디지털 서명)\n"
        "- `SHA256SUMS.txt` (무결성 검증 체크섬)\n"
    )
    body_file = os.path.join(DEPLOY_DIR, "github_release_v2923_body.md")
    with open(body_file, "w", encoding="utf-8") as bf:
        bf.write(body)

    assets = [
        os.path.join(DEPLOY_DIR, "BackupSystem_Setup_v2.9.23.exe"),
        os.path.join(DEPLOY_DIR, "release_v2.9.23.zip"),
        os.path.join(DEPLOY_DIR, "release_v2.9.23.zip.sig"),
        os.path.join(DEPLOY_DIR, "SHA256SUMS.txt")
    ]
    for a in assets:
        assert os.path.exists(a), f"Asset {a} not found!"

    cmd = (
        f'"{gh_exe}" release create v2.9.23 '
        f'"{assets[0]}" "{assets[1]}" "{assets[2]}" "{assets[3]}" '
        f'--title "v2.9.23 - Tier 1 Low-Risk Performance Optimizations" '
        f'--notes-file "{body_file}"'
    )
    run_cmd(cmd)
    print("[+] GitHub Release v2.9.23 successfully published!")

if __name__ == "__main__":
    sync_clean_repo()
    git_commit_and_push()
    publish_github_release()
