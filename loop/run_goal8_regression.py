#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
C:\Users\kksjmj\Desktop\ai\백업시스템\loop\run_goal8_regression.py
========================================================================
GOAL 8: 기존 BackupSystem 전체 Regression

검증 내용:
1. tools/test_normal_regression.py (정상 경로 및 False Positive 0 회귀)
2. tests/test_v239_verification.py (VSS, Manifest 서명, 샘플링 검증)
3. tests/test_disaster_scenarios.py (DB 손상 복구, 부분 쓰기 감지)
4. tests/test_p0_fail_closed.py (디스크 부족 시 Fail-Closed 차단)
5. 각 테스트별 PASS/FAIL 여부, 테스트 수, 소요시간 계측
"""

from __future__ import annotations

import os
import sys
import json
import time
import subprocess
from pathlib import Path
from datetime import datetime, timezone

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOOP_DIR = PROJECT_ROOT / "loop"
RUNS_DIR = LOOP_DIR / "runs"

TESTS = [
    {
        "id": "REG-01",
        "name": "Normal Path Regression Gate",
        "cmd": [sys.executable, "tools/test_normal_regression.py"]
    },
    {
        "id": "REG-02",
        "name": "v2.3.9 Verification Suite",
        "cmd": [sys.executable, "-m", "unittest", "tests/test_v239_verification.py"]
    },
    {
        "id": "REG-03",
        "name": "Disaster Scenarios Suite",
        "cmd": [sys.executable, "-m", "unittest", "tests/test_disaster_scenarios.py"]
    },
    {
        "id": "REG-04",
        "name": "P0 Fail-Closed Safeguard Suite",
        "cmd": [sys.executable, "-m", "unittest", "tests/test_p0_fail_closed.py"]
    }
]


def main():
    run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_goal8_regression"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    report_file = run_dir / "goal8_regression_report.json"

    print("=" * 80)
    print(" 🧪 [GOAL 8] 기존 BackupSystem 전체 Regression 재실행")
    print(f" >> Run ID: {run_id}")
    print("=" * 80 + "\n")

    results = []
    all_passed = True

    for test in TESTS:
        t_name = test["name"]
        cmd = test["cmd"]
        print(f"[{test['id']}] 실행 중: {t_name} ...")
        print(f" >> 명령: {' '.join(cmd)}")

        t0 = time.perf_counter()
        proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, errors="replace")
        t1 = time.perf_counter()
        duration_sec = round(t1 - t0, 3)

        passed = (proc.returncode == 0)
        status_str = "PASS" if passed else "FAIL"
        print(f" >> 결과: {status_str} (소요시간: {duration_sec}s, ReturnCode: {proc.returncode})\n")

        if not passed:
            all_passed = False
            print(f"    [ERROR STDERR]:\n{proc.stderr}\n")

        results.append({
            "id": test["id"],
            "name": t_name,
            "cmd": cmd,
            "passed": passed,
            "returncode": proc.returncode,
            "duration_sec": duration_sec,
            "stdout_tail": proc.stdout[-500:] if proc.stdout else "",
            "stderr": proc.stderr if not passed else ""
        })

    report = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_suites": len(TESTS),
        "passed_suites": sum(1 for r in results if r["passed"]),
        "failed_suites": sum(1 for r in results if not r["passed"]),
        "all_passed": all_passed,
        "results": results
    }

    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    assert all_passed, "일부 회귀 테스트가 실패했습니다!"
    print("=" * 80)
    print(" 🎉 [SUCCESS] GOAL 8 전체 Regression Suite 100% ALL GREEN 통과!")
    print(f" >> Report: {report_file}")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
