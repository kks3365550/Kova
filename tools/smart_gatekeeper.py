#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smart_gatekeeper.py - 프로젝트 전용 스마트 게이트키퍼 & 수명주기/린트 감사기
사용법:
  python tools/smart_gatekeeper.py [--file <source_file> ...] [--lifecycle] [--all]
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, List

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent.parent


def is_crlf(raw: bytes) -> bool:
    return b"\r\n" in raw


def is_pure_ascii(raw: bytes) -> bool:
    return all(b <= 0x7F for b in raw)


def has_korean(raw: bytes) -> bool:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("cp949")
        except UnicodeDecodeError:
            return False
    for ch in text:
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7FF or 0x1100 <= code <= 0x11FF or 0x3100 <= code <= 0x31FF:
            return True
    return False


def validate_file(file_path: Path) -> Optional[str]:
    ext = file_path.suffix.lower()
    try:
        raw = file_path.read_bytes()
    except Exception as e:
        return f"FileReadError: {e}"

    if ext in (".bat", ".cmd"):
        if not is_pure_ascii(raw) or has_korean(raw):
            return "Windows 배치 파일은 한글이 없어야 하며 순수 ASCII여야 합니다."
        if b"\n" in raw and not is_crlf(raw):
            return "배치 파일은 반드시 CRLF 개행을 사용해야 합니다."

    if ext == ".py":
        if b"\n" in raw and not is_crlf(raw):
            return "파이썬 파일은 Windows 표준 CRLF 개행을 사용해야 합니다."
        try:
            source = raw.decode("utf-8-sig")
            ast.parse(source)
        except SyntaxError as e:
            return f"문법 오류(SyntaxError): {e}"
        except UnicodeDecodeError as e:
            return f"인코딩 오류(UTF-8 필요): {e}"

        import py_compile
        try:
            py_compile.compile(str(file_path), doraise=True)
        except Exception as e:
            return f"바이트코드 컴파일 오류: {e}"

    return None


def run_pyflakes() -> bool:
    print("[1/3] pyflakes 정적 린트 전수 검사...")
    targets = ["core", "web", "cli_backup.py", "cli.py", "run.py"]
    cmd = [sys.executable, "-m", "pyflakes"] + [t for t in targets if (BASE_DIR / t).exists()]
    res = subprocess.run(
        cmd,
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace"
    )
    if res.returncode != 0 or res.stdout.strip() or res.stderr.strip():
        print("❌ [FAIL] pyflakes 린트 오류 감지:")
        if res.stdout:
            print(res.stdout)
        if res.stderr:
            print(res.stderr)
        return False
    print("✅ [PASS] pyflakes 무결 (0 오류)")
    return True


def run_lifecycle_audit() -> bool:
    print("[2/3] 역방향 수명주기(Lifecycle / Cache Purge) 스트레스 감사...")
    test_target = BASE_DIR / "tests" / "test_deleted_image_cache_purge.py"
    if not test_target.exists():
        print("⚠️ [WARN] test_deleted_image_cache_purge.py 부재")
        return True

    cmd = [sys.executable, "-m", "pytest", str(test_target), "-v", "--tb=short"]
    res = subprocess.run(
        cmd,
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace"
    )
    if res.returncode != 0:
        print("❌ [FAIL] 역방향 수명주기 파괴 검증 실패 (캐시 오염 또는 삭제 잔여 감지):")
        print(res.stdout or res.stderr)
        return False
    print("✅ [PASS] 역방향 수명주기 무결성 통과 (외부 삭제 ➡️ 캐시 자동 정제 보증)")
    return True


def run_contract_audit() -> bool:
    print("[3/3] UI ↔ API 계약 일치(Contract) 감사...")
    sysimg_js = BASE_DIR / "web" / "static" / "js" / "backup_sysimage.js"
    app_py = BASE_DIR / "web" / "app.py"

    if sysimg_js.exists() and app_py.exists():
        js_text = sysimg_js.read_text(encoding="utf-8", errors="replace")
        py_text = app_py.read_text(encoding="utf-8", errors="replace")

        # VHDX 관련 필수 기능 매핑 검사
        if "deleteVhdx" not in js_text:
            print("❌ [FAIL] UI 계약 위반: backup_sysimage.js에 deleteVhdx UI 핸들러가 누락되었습니다.")
            return False
        if "/api/system-image/delete" not in py_text:
            print("❌ [FAIL] API 계약 위반: web/app.py에 /api/system-image/delete 엔드포인트가 누락되었습니다.")
            return False

    print("✅ [PASS] UI ↔ API 수명주기 필수 액션 계약 일치")
    return True


def main():
    parser = argparse.ArgumentParser(description="Smart Gatekeeper & Lifecycle Auditor")
    parser.add_argument("--file", nargs="*", help="Specific source files to validate")
    parser.add_argument("--lifecycle", action="store_true", help="Run lifecycle and contract audit only")
    parser.add_argument("--all", action="store_true", help="Run full gatekeeper suite")
    args = parser.parse_args()

    files = args.file or []
    if files:
        print(f"🔍 [Smart Gatekeeper] {len(files)}개 파일 무결성 및 개행/구문 검사 중...")
        failed = False
        for f in files:
            p = Path(f)
            if not p.is_absolute():
                p = BASE_DIR / p
            if p.exists():
                err = validate_file(p)
                if err:
                    print(f"❌ [REJECT] {p.name}: {err}")
                    failed = True
                else:
                    print(f"✓ {p.name}: OK")
        if failed:
            sys.exit(1)

    if args.lifecycle or args.all or not files:
        if not run_pyflakes():
            sys.exit(1)
        if not run_lifecycle_audit():
            sys.exit(1)
        if not run_contract_audit():
            sys.exit(1)

    print("\n🎉 [ALL CLEAR] 스마트 게이트키퍼 전 관문 통과!")
    sys.exit(0)


if __name__ == "__main__":
    main()
