# -*- coding: utf-8 -*-
"""
Clean GitHub Publisher for BackupSystem v2.13.6
Zero-leak public repository synchronizer and GitHub release publisher.
"""
import os
import shutil
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = r"c:\Users\kksjmj\Desktop\ai\백업시스템"
TEMP_DIR = os.environ.get("TEMP", r"C:\Windows\Temp")
CLEAN_DIR = os.path.join(TEMP_DIR, "BackupSystem_Public_Release")
DIST_DIR = os.path.join(BASE_DIR, "dist")

def get_current_version() -> str:
    ver_file = os.path.join(BASE_DIR, "VERSION")
    if os.path.exists(ver_file):
        with open(ver_file, "r", encoding="utf-8") as f:
            v = f.read().strip()
            return f"v{v}" if not v.startswith("v") else v
    return "v2.13.6"

VERSION = get_current_version()
COMMIT_MSG = f"release: {VERSION} - Multi-chunk ingest, robocopy offsite replication & zero-leak clean release"

def run_cmd(cmd, cwd=CLEAN_DIR):
    print(f"[*] Running: {cmd}")
    res = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, errors="replace")
    if res.returncode != 0:
        print(f"[-] Error (code {res.returncode}):\n{res.stderr.strip()}\n{res.stdout.strip()}")
    else:
        out = res.stdout.strip()
        if out:
            print(f"[+] Output:\n{out}")
    return res

def sync_clean_repo():
    print("=" * 60)
    print(f"🚀 백업시스템 {VERSION} 클린 공개 패키지 스테이징")
    print("=" * 60)

    if not os.path.exists(CLEAN_DIR):
        print(f"[*] Initializing clean repo at {CLEAN_DIR}...")
        os.makedirs(CLEAN_DIR, exist_ok=True)
        run_cmd("git init -b main", cwd=CLEAN_DIR)
        run_cmd('git remote add origin "https://github.com/kks3365550/BackupSystem.git"', cwd=CLEAN_DIR)
    else:
        # Check if remote exists
        check_rem = subprocess.run("git remote get-url origin", cwd=CLEAN_DIR, shell=True, capture_output=True, text=True)
        if check_rem.returncode != 0:
            run_cmd('git remote add origin "https://github.com/kks3365550/BackupSystem.git"', cwd=CLEAN_DIR)

    # 1. core
    print("[*] Copying core/ ...")
    core_clean = os.path.join(CLEAN_DIR, "core")
    shutil.rmtree(core_clean, ignore_errors=True)
    shutil.copytree(
        os.path.join(BASE_DIR, "core"),
        core_clean,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "notifier.py")
    )

    # 2. web
    print("[*] Copying web/ ...")
    web_clean = os.path.join(CLEAN_DIR, "web")
    shutil.rmtree(web_clean, ignore_errors=True)
    shutil.copytree(
        os.path.join(BASE_DIR, "web"),
        web_clean,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )

    # 3. installer
    print("[*] Copying installer/ ...")
    inst_clean = os.path.join(CLEAN_DIR, "installer")
    shutil.rmtree(inst_clean, ignore_errors=True)
    shutil.copytree(
        os.path.join(BASE_DIR, "installer"),
        inst_clean,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "Output", "runtime")
    )

    # 4. docs
    print("[*] Copying docs/ ...")
    docs_clean = os.path.join(CLEAN_DIR, "docs")
    shutil.rmtree(docs_clean, ignore_errors=True)
    if os.path.exists(os.path.join(BASE_DIR, "docs")):
        shutil.copytree(
            os.path.join(BASE_DIR, "docs"),
            docs_clean,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )

    # 5. keys (Only public keys, NEVER private keys)
    print("[*] Copying public keys ...")
    keys_clean = os.path.join(CLEAN_DIR, "keys")
    os.makedirs(keys_clean, exist_ok=True)
    pub_key = os.path.join(BASE_DIR, "keys", "release_ed25519.pub")
    if os.path.exists(pub_key):
        shutil.copy2(pub_key, os.path.join(keys_clean, "release_ed25519.pub"))

    # 6. Root files
    print("[*] Copying root files ...")
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

    # 7. Create safe .gitignore for public repository
    public_gitignore = os.path.join(CLEAN_DIR, ".gitignore")
    with open(public_gitignore, "w", encoding="utf-8") as f:
        f.write(
            "__pycache__/\n"
            "*.pyc\n"
            "*.tmp\n"
            "*.log\n"
            ".venv/\n"
            "data/\n"
            "keys/*.key\n"
            "keys/*private*\n"
            ".ai/\n"
            "scratch/\n"
        )

    print("[+] Clean staging repository sync completed.")

def git_commit_and_push():
    print("=" * 60)
    print("[*] Committing and pushing updates to GitHub origin/main...")
    print("=" * 60)
    run_cmd("git add -A")
    # Commit if changes exist
    status_res = subprocess.run("git status --porcelain", cwd=CLEAN_DIR, shell=True, capture_output=True, text=True)
    if status_res.stdout.strip():
        run_cmd(f'git commit -m "{COMMIT_MSG}"')
    else:
        print("[*] No changes to commit, proceeding with tag/push...")

    run_cmd(f'git tag -fa {VERSION} -m "{VERSION} Official Release"')
    print("[*] Pushing branch main to GitHub...")
    p1 = run_cmd("git push origin main --force")
    print(f"[*] Pushing tag {VERSION} to GitHub...")
    p2 = run_cmd(f"git push origin {VERSION} --force")
    return p1.returncode == 0 and p2.returncode == 0

def publish_github_release_if_available():
    print("=" * 60)
    print("[*] Checking GitHub Release assets...")
    print("=" * 60)
    gh_exe = r"C:\Program Files\GitHub CLI\gh.exe"
    if not os.path.exists(gh_exe):
        gh_exe = "gh"

    body = (
        f"## 🛡️ BackupSystem {VERSION} Official Release\n\n"
        f"> **Ed25519 v2 Key Migration, Legacy Key Deprecation & Zero-Leak Hardening**\n\n"
        f"### ⚡ 주요 업데이트 내역 (Key Highlights)\n"
        f"1. **Ed25519 v2 키 단독 전환 및 구 키 폐기**:\n"
        f"   - **v2 키 체계 도입**: 새로운 Ed25519 v2 공개/비공개 키 쌍으로 완전 전환\n"
        f"   - **구 키 폐기**: 기존 v1 키 및 관련 서명 데이터 영구 무효화 및 삭제\n"
        f"   - **무결성 검증 강화**: v2 키 기반 서명 검증 로직 업데이트 및 호환성 확인 완료\n\n"
        f"2. **Inno Setup 공식 인스톨러(.exe) 배포 및 원클릭 다운로드**:\n"
        f"   - 릴리즈 에셋에 정식 윈도우 설치 파일(`BackupSystem_Setup_{VERSION}.exe`) 자동 빌드 및 첨부\n"
        f"   - 웹 대시보드 업데이트 감지 시 인스톨러 직접 다운로드 버튼 제공\n\n"
        f"3. **초고속 오프사이트 이중화 복제 파이프라인 (core/offsite.py)**:\n"
        f"   - 멀티스레드 Robocopy 엔진을 통한 고속 증분 복제 및 0.5초 무지연 헬스체크\n"
    )

    body_file = os.path.join(TEMP_DIR, f"github_release_{VERSION}_body.md")
    with open(body_file, "w", encoding="utf-8") as bf:
        bf.write(body)

    # Assets
    candidate_assets = [
        os.path.join(DIST_DIR, f"BackupSystem_Setup_{VERSION}.exe"),
        os.path.join(DIST_DIR, f"release_{VERSION}.zip"),
        os.path.join(DIST_DIR, f"release_{VERSION}.zip.sig"),
        os.path.join(DIST_DIR, "SHA256SUMS.txt")
    ]
    existing_assets = [a for a in candidate_assets if os.path.exists(a)]

    if existing_assets:
        print(f"[*] Found {len(existing_assets)} release assets. Publishing GitHub Release...")
        run_cmd(f'"{gh_exe}" release delete {VERSION} -y', cwd=BASE_DIR)
        asset_str = " ".join([f'"{a}"' for a in existing_assets])
        cmd = (
            f'"{gh_exe}" release create {VERSION} {asset_str} '
            f'--title "{VERSION} - Multi-chunk Ingest & Robocopy Offsite Replication" '
            f'--notes-file "{body_file}"'
        )
        res = run_cmd(cmd, cwd=BASE_DIR)
        if res.returncode == 0:
            print(f"[+] GitHub Release {VERSION} successfully published!")
    else:
        print("[*] Binary dist assets not present in dist/ directory. Tag and main branch push complete.")

def main():
    sync_clean_repo()
    success = git_commit_and_push()
    if success:
        publish_github_release_if_available()
        print("\n" + "=" * 60)
        print(f"🎉 SUCCESS: BackupSystem {VERSION} successfully published to GitHub!")
        print("=" * 60)
    else:
        print("\n[-] Push failed. Please check credentials or network.")

if __name__ == "__main__":
    main()
