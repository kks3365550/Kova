#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
C:\Users\kksjmj\Desktop\ai\백업시스템\loop\run_goal14_boundary.py
========================================================================
GOAL 14: AC-RESTORE-01 Empty Snapshot Boundary Fix Verification

목적:
1. backup_contract.yaml 변경 범위 확인
2. CASE A (Empty Snapshot: total_files=0, restored=0, failed=0)
   CASE B (Non-empty Zero-restore: total_files=1, restored=0, failed=1)
   경계값 평가
3. 기존 core/ 및 RestoreAdapter 무변경(0 diff) 상태 유지 검증
4. 기존 회귀 테스트 스위트 4종 재실행
5. 운영 저장소 무변경성 검증
"""

from __future__ import annotations

import os
import sys
import json
import time
import subprocess
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from loop.restore_adapter import RestoreAdapter

PRODUCTION_REPO = Path(r"D:\MyBackup_Repository")
CORE_DIR = PROJECT_ROOT / "core"
LOOP_DIR = PROJECT_ROOT / "loop"
CONTRACT_PATH = LOOP_DIR / "backup_contract.yaml"
ADAPTER_PATH = LOOP_DIR / "restore_adapter.py"
RUNS_DIR = LOOP_DIR / "runs"


def capture_repo_inventory(repo_path: Path) -> dict:
    inventory = {"files": {}, "snapshot_hashes": {}}
    if not repo_path.exists():
        return inventory
    for root, _, files in os.walk(repo_path):
        for f in files:
            fpath = Path(root) / f
            rel = fpath.relative_to(repo_path).as_posix()
            stat = fpath.stat()
            inventory["files"][rel] = (stat.st_size, stat.st_mtime)
    snaps_dir = repo_path / "snapshots"
    if snaps_dir.exists():
        for sf in snaps_dir.glob("*.json"):
            inventory["snapshot_hashes"][sf.name] = hashlib.sha256(sf.read_bytes()).hexdigest()
    return inventory


def capture_core_hashes(core_path: Path) -> dict:
    hashes = {}
    for pyfile in core_path.rglob("*.py"):
        if "__pycache__" not in pyfile.parts:
            hashes[pyfile.as_posix()] = hashlib.sha256(pyfile.read_bytes()).hexdigest()
    return hashes


def evaluate_contract_updated_rule(restore_result: Dict[str, Any]) -> Tuple[str, Dict[str, bool]]:
    """Evaluates result according to updated contract v0.3 logic (empty snapshot boundary)."""
    total_files = restore_result.get("total_files", 0)
    restored_files = restore_result.get("restored_files", 0)
    skipped_files = restore_result.get("skipped_files", 0)
    failed_files = restore_result.get("failed_files", [])

    # AC-RESTORE-01 updated:
    # IF total_files > 0: restored_files > 0
    # ELSE IF total_files == 0: restored_files == 0
    if not isinstance(restored_files, int) or total_files < 0:
        ac1 = False
    elif total_files > 0:
        ac1 = (restored_files > 0)
    else:  # total_files == 0
        ac1 = (restored_files == 0)

    # AC-RESTORE-02: failed_files == []
    ac2 = (isinstance(failed_files, list) and len(failed_files) == 0)

    # AC-RESTORE-03: restored_files + skipped_files == total_files
    ac3 = ((restored_files + skipped_files) == total_files)

    criteria = {
        "AC-RESTORE-01": ac1,
        "AC-RESTORE-02": ac2,
        "AC-RESTORE-03": ac3
    }
    status = "PASS" if all(criteria.values()) else "FAIL"
    return status, criteria


def run_regression_suite() -> Dict[str, Any]:
    suites = [
        ("REG-01", "tools/test_normal_regression.py", PROJECT_ROOT / "tools" / "test_normal_regression.py"),
        ("REG-02", "tests/test_v239_verification.py", PROJECT_ROOT / "tests" / "test_v239_verification.py"),
        ("REG-03", "tests/test_disaster_scenarios.py", PROJECT_ROOT / "tests" / "test_disaster_scenarios.py"),
        ("REG-04", "tests/test_p0_fail_closed.py", PROJECT_ROOT / "tests" / "test_p0_fail_closed.py")
    ]
    results = {}
    all_pass = True
    print("\n[Step 3] 기존 회귀 테스트 스위트 4종 재실행:")
    for code, name, path in suites:
        if not path.exists():
            print(f" >> [{code}] {name}: SKIPPED (File not found)")
            results[code] = {"status": "SKIP", "duration_seconds": 0.0}
            continue
        t0 = time.perf_counter()
        if code in ("REG-02", "REG-03", "REG-04"):
            cmd = [sys.executable, "-m", "unittest", name]
        else:
            cmd = [sys.executable, name]
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        t1 = time.perf_counter()
        dur = round(t1 - t0, 3)
        passed = (proc.returncode == 0)
        status_str = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f" >> [{code}] {name}: {status_str} ({dur}s)")
        results[code] = {
            "status": status_str,
            "returncode": proc.returncode,
            "duration_seconds": dur
        }
    return {"all_pass": all_pass, "suites": results}


def main():
    print("=" * 80)
    print(" 🎯 [GOAL 14] AC-RESTORE-01 Empty Snapshot Boundary Fix Verification")
    print("=" * 80 + "\n")

    # Step 0: 사전 불변성 캡처
    pre_core = capture_core_hashes(CORE_DIR)
    pre_repo = capture_repo_inventory(PRODUCTION_REPO)
    adapter_hash_pre = hashlib.sha256(ADAPTER_PATH.read_bytes()).hexdigest()

    # Step 1: backup_contract.yaml 검증
    contract_text = CONTRACT_PATH.read_text(encoding="utf-8")
    print("[Step 1] backup_contract.yaml 내용 확인:")
    has_boundary_rule = "total_files == 0" in contract_text or "total_files > 0" in contract_text
    print(f" >> AC-RESTORE-01 경계값 명시 여부: {has_boundary_rule}")

    # Step 2: 경계 테스트 실행
    print("\n[Step 2] 경계 테스트 (CASE A & CASE B) 실행:")
    case_a = {
        "snapshot_id": "snap_empty_fixture",
        "target_dir": "C:/sandbox/target",
        "total_files": 0,
        "restored_files": 0,
        "skipped_files": 0,
        "failed_files": [],
        "restored_bytes": 0,
        "duration_seconds": 0.01
    }

    case_b = {
        "snapshot_id": "snap_failed_fixture",
        "target_dir": "C:/sandbox/target",
        "total_files": 1,
        "restored_files": 0,
        "skipped_files": 0,
        "failed_files": [("doc1.txt", "Hash mismatch")],
        "restored_bytes": 0,
        "duration_seconds": 0.05
    }

    # 2-A: Unmodified RestoreAdapter.evaluate_contract()
    adapter = RestoreAdapter()
    status_a_adapt, _, _, crit_a_adapt = adapter.evaluate_contract(case_a)
    status_b_adapt, _, _, crit_b_adapt = adapter.evaluate_contract(case_b)

    # 2-B: Updated Contract Specification rule
    status_a_rule, crit_a_rule = evaluate_contract_updated_rule(case_a)
    status_b_rule, crit_b_rule = evaluate_contract_updated_rule(case_b)

    print(f" >> [CASE A - EMPTY SNAPSHOT]")
    print(f"    - 미수정 RestoreAdapter: status={status_a_adapt}, AC-01={crit_a_adapt['AC-RESTORE-01']} (기존 하드코딩 restored>0 기준)")
    print(f"    - 업데이트된 계약 규칙:  status={status_a_rule}, AC-01={crit_a_rule['AC-RESTORE-01']} (원하는 기대값: PASS)")

    print(f" >> [CASE B - NON-EMPTY ZERO RESTORE]")
    print(f"    - 미수정 RestoreAdapter: status={status_b_adapt}, AC-01={crit_b_adapt['AC-RESTORE-01']}")
    print(f"    - 업데이트된 계약 규칙:  status={status_b_rule}, AC-01={crit_b_rule['AC-RESTORE-01']} (원하는 기대값: FAIL)")

    # Step 3: 회귀 테스트 재실행
    reg_results = run_regression_suite()

    # Step 4: 사후 불변성 대조
    print("\n[Step 4] 무결성 및 불변성 대조:")
    post_core = capture_core_hashes(CORE_DIR)
    core_mutations = sum(1 for k in pre_core if post_core.get(k) != pre_core[k])
    print(f" >> core/ 변경 파일 수: {core_mutations}개 (0 필수)")

    post_repo = capture_repo_inventory(PRODUCTION_REPO)
    repo_mutations = len(set(post_repo["files"].keys()) ^ set(pre_repo["files"].keys()))
    print(f" >> 운영 저장소 변경 파일 수: {repo_mutations}개 (0 필수)")

    adapter_hash_post = hashlib.sha256(ADAPTER_PATH.read_bytes()).hexdigest()
    adapter_mutated = (adapter_hash_pre != adapter_hash_post)
    print(f" >> restore_adapter.py 수정 여부: {'수정됨' if adapter_mutated else '무수정(0 diff 보존)'}")

    # Step 5: 종합 결과 리포트 저장
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "goal": "GOAL_14",
        "contract_updated": has_boundary_rule,
        "restore_adapter_modified": adapter_mutated,
        "case_a_empty_snapshot": {
            "under_unmodified_adapter": {"status": status_a_adapt, "criteria": crit_a_adapt},
            "under_updated_contract_rule": {"status": status_a_rule, "criteria": crit_a_rule}
        },
        "case_b_non_empty_zero_restore": {
            "under_unmodified_adapter": {"status": status_b_adapt, "criteria": crit_b_adapt},
            "under_updated_contract_rule": {"status": status_b_rule, "criteria": crit_b_rule}
        },
        "regression": reg_results,
        "immutability": {
            "core_changed": core_mutations,
            "repository_changed": repo_mutations
        }
    }
    report_path = RUNS_DIR / "goal14_boundary_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n >> 결과 리포트 저장: {report_path}")


if __name__ == "__main__":
    main()
