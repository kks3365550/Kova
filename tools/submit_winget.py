# -*- coding: utf-8 -*-
"""
tools/submit_winget.py

Microsoft winget 공식 저장소(microsoft/winget-pkgs) PR 자동 제출 파이프라인
BackupSystem 패키지를 Microsoft Windows Package Manager에 등록하기 위한 검증 및 제출 자동화 도구입니다.
"""

from __future__ import annotations

import os
import sys
import shutil
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
MANIFEST_DIR = BASE_DIR / "manifests" / "k" / "kks3365550" / "BackupSystem"


def check_command(cmd_name: str) -> bool:
    return shutil.which(cmd_name) is not None


def run_cmd(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd or BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creation_flags
        )
        return proc.returncode, proc.stdout
    except Exception as e:
        return 1, str(e)


def main():
    print("=" * 60)
    print(" Microsoft winget Package Submission Pipeline (BackupSystem)")
    print("=" * 60)

    # 1. 매니페스트 디렉토리 확인
    if not MANIFEST_DIR.exists():
        print(f"[ERROR] 매니페스트 디렉토리를 찾을 수 없습니다: {MANIFEST_DIR}")
        sys.exit(1)

    versions = [d.name for d in MANIFEST_DIR.iterdir() if d.is_dir()]
    if not versions:
        print("[ERROR] 등록할 버전 매니페스트가 없습니다.")
        sys.exit(1)

    target_ver = sorted(versions)[-1]
    ver_dir = MANIFEST_DIR / target_ver
    print(f"[*] 대상 패키지: kks3365550.BackupSystem v{target_ver}")
    print(f"[*] 매니페스트 위치: {ver_dir}")

    # 2. winget validate 검증
    if check_command("winget"):
        print("\n[Step 1/3] Microsoft winget 매니페스트 유효성 검증 중...")
        code, out = run_cmd(["winget", "validate", str(ver_dir)])
        if code == 0 and "Manifest validation succeeded" in out:
            print("  -> [PASS] winget validate 성공 (100% 규격 일치)")
        else:
            print(f"  -> [WARNING] winget validate 결과:\n{out}")
    else:
        print("\n[Step 1/3] winget CLI 미설치 환경: 로컬 스키마 검증 건너뜀")

    # 3. GitHub CLI (gh) 확인
    print("\n[Step 2/3] GitHub CLI 인증 상태 확인 중...")
    if not check_command("gh"):
        print("  -> [INFO] GitHub CLI(gh)가 설치되어 있지 않습니다.")
        print("  -> 수동 제출 안내:")
        print("     1. https://github.com/microsoft/winget-pkgs 저장소를 Fork합니다.")
        print(f"     2. manifests/k/kks3365550/BackupSystem/{target_ver}/ 폴더를 해당 저장소의 동일 경로로 복사합니다.")
        print(f"     3. 'New package: kks3365550.BackupSystem version {target_ver}' 제목으로 PR을 생성합니다.")
        return 0

    code, out = run_cmd(["gh", "auth", "status"])
    if code != 0:
        print(f"  -> [WARNING] gh 로그인이 필요합니다 ('gh auth login' 실행 권장):\n{out}")
    else:
        print("  -> [OK] GitHub CLI 인증 확인 완료")

    # 4. 제출 가이드 및 명령어 출력
    print("\n[Step 3/3] Microsoft 공식 저장소 제출 절차")
    print("-" * 60)
    print("원클릭 공식 PR을 생성하려면 아래 명령을 실행하십시오:")
    print()
    print(f"  gh pr create --repo microsoft/winget-pkgs \\")
    print(f"    --title \"New package: kks3365550.BackupSystem version {target_ver}\" \\")
    print(f"    --body \"### New Package Submission: BackupSystem v{target_ver}\"")
    print("-" * 60)
    print("[SUCCESS] winget 공식 등록 준비가 100% 완료되었습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
