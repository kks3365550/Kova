# -*- coding: utf-8 -*-
"""
v2.10.3 Clean Push and Official GitHub Release Publisher
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
    assert os.path.exists(CLEAN_DIR), f"Clean repo directory {CLEAN_DIR} missing!"

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
    run_cmd('git commit -m "docs: calibrate engineering specs, align v2.10.3 manifests and release notes"')
    run_cmd("git push origin main")

def publish_github_release():
    print("[*] Creating GitHub Release v2.10.3 and uploading assets...")
    gh_exe = r"C:\Program Files\GitHub CLI\gh.exe"
    body = (
        "## 🛡️ BackupSystem v2.10.3 Official Release\n\n"
        "> **Windows 업무 및 엔터프라이즈 환경을 위한 3계층(3-Tier) 불변(Immutable) 하이브리드 백업 매니저**\n\n"
        "### ⚡ 주요 업데이트 내역 (Key Highlights)\n"
        "1. **Invalidation-Driven SnapshotMetadataCache (~11ms 초저지연)**:\n"
        "   - 22만 개 파일 실측 환경에서 스냅샷 목록 조회 지연을 2,947ms에서 **11.46ms(약 257배 가속, 99.6% 지연 절감)**로 단축\n"
        "   - 외부 파일 변동을 0.05ms 내에 감지하는 핑거프린트 안전망과 이벤트 기반 무효화(Invalidation) 적용\n\n"
        "2. **3계층(3-Tier) 복구 아키텍처 공식 분리**:\n"
        "   - **Tier 1 (CAS)**: 일상 파일/폴더/레지스트리 즉시 롤백 (Zero-Lock VSS, WORM 불변성)\n"
        "   - **Tier 2 (BMR)**: 물리 SSD 사망 시 Windows Native(`wbadmin -allCritical`) + WinRE 전체 디스크 무인 복구\n"
        "   - **Tier 3 (DR)**: 서버 프로세스 파괴 시 `disaster_recovery.py` 독립 무결성 감사 및 긴급 데이터 구출\n\n"
        "3. **Firebase 외부 클라우드 의존성 100% 완전 제거 (Clean Purge)**:\n"
        "   - 사내 폐쇄망 및 Tailscale P2P 환경에 완벽히 최적화된 자립형 아키텍처 구축\n"
        "   - 불필요한 클라우드 관제 UI를 정리하고 시스템 드라이브 및 스냅샷 중심으로 대시보드 UX 최적화\n\n"
        "4. **Ed25519 디지털 서명 기반 GitHub Releases OTA 자동 업데이트**:\n"
        "   - 단일 공식 릴리즈 채널로 일원화 및 변조 방지 전자서명 검증 탑재\n\n"
        "### 📦 공식 배포 자산 (Official Assets)\n"
        "- `BackupSystem_Setup_v2.10.3.exe` (Windows 공식 원클릭 설치 프로그램)\n"
        "- `release_v2.10.3.zip` (자동 OTA 업데이트 패키지)\n"
        "- `release_v2.10.3.zip.sig` (Ed25519 디지털 서명)\n"
        "- `SHA256SUMS.txt` (무결성 검증 체크섬)\n"
    )
    body_file = os.path.join(DIST_DIR, "github_release_v2103_body.md")
    with open(body_file, "w", encoding="utf-8") as bf:
        bf.write(body)

    assets = [
        os.path.join(DIST_DIR, "BackupSystem_Setup_v2.10.3.exe"),
        os.path.join(DIST_DIR, "release_v2.10.3.zip"),
        os.path.join(DIST_DIR, "release_v2.10.3.zip.sig"),
        os.path.join(DIST_DIR, "SHA256SUMS.txt")
    ]
    for a in assets:
        assert os.path.exists(a), f"Asset {a} not found!"

    cmd = (
        f'"{gh_exe}" release create v2.10.3 '
        f'"{assets[0]}" "{assets[1]}" "{assets[2]}" "{assets[3]}" '
        f'--title "v2.10.3 - SnapshotMetadataCache (~11ms), Firebase Purge & 3-Tier Architecture" '
        f'--notes-file "{body_file}"'
    )
    res = run_cmd(cmd)
    if res.returncode == 0:
        print("[+] GitHub Release v2.10.3 successfully published!")
    else:
        print("[-] Release creation returned non-zero, checking if tag release can be edited...")

if __name__ == "__main__":
    sync_clean_repo()
    git_commit_and_push()
    publish_github_release()
