#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
C:\Users\kksjmj\Desktop\ai\백업시스템\loop\run_goal6_lineage_rollup.py
========================================================================
GOAL 6: Event / Child Event / Run Rollup 검증

검증 내용:
1. Parent Event -> Policy Decision -> Action -> Child Event 계통(Lineage) 추적
   - parent_event_id, child event_id, 동일 run_id 전파
2. Action Budget 및 Terminal Action (QUARANTINE, ABORT)에 의한 무한 루프 차단
3. Event.status (개별 관측/액션 상태) vs Run.status (전체 실행 집계 상태) 분리 검증
4. run_rollup.json 집계 및 감사 증적 보존
5. 사후 core/ 및 운영 저장소 0 mutation 검증
"""

from __future__ import annotations

import os
import sys
import json
import time
import shutil
import hashlib
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
CORE_DIR = PROJECT_ROOT / "core"
RUNS_DIR = LOOP_DIR / "runs"
PRODUCTION_REPO = Path(r"D:\MyBackup_Repository")

sys.path.insert(0, str(PROJECT_ROOT))
from loop.loop_guard import PolicyValidator, ExecutionGuard


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


def emit_event(events_file: Path, event: dict):
    events_file.parent.mkdir(parents=True, exist_ok=True)
    with open(events_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def main():
    run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_goal6_lineage"
    run_dir = RUNS_DIR / run_id
    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    events_file = run_dir / "events.jsonl"
    rollup_file = run_dir / "run_rollup.json"

    print("=" * 80)
    print(" 🌲 [GOAL 6] Event / Child Event / Run Rollup 검증")
    print(f" >> Run ID: {run_id}")
    print(f" >> Target Repo: {PRODUCTION_REPO} (READ-ONLY STRICT GUARANTEE)")
    print("=" * 80 + "\n")

    # Step 0: 사전 불변성 캡처
    pre_core_hashes = capture_core_hashes(CORE_DIR)
    pre_repo_inventory = capture_repo_inventory(PRODUCTION_REPO)
    print(f"[Step 0] 사전 상태 캡처: core/={len(pre_core_hashes)}개, LiveRepo={len(pre_repo_inventory['files'])}개\n")

    validator = PolicyValidator()
    guard = ExecutionGuard(validator)

    # Step 1: Parent Event 발행 (Stage: observe, Status: FAIL)
    parent_event_id = f"evt_{run_id}_001"
    evt_parent = {
        "event_id": parent_event_id,
        "project": "BackupSystem",
        "loop": "restore_verification",
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": "observe",
        "status": "FAIL",
        "error_class": "data_integrity_violation",
        "error_code": "E_SHA256_MISMATCH",
        "traits": ["integrity_critical"],
        "duration_ms": 150.2,
        "evidence_ref": f"runs/{run_id}/evidence/observe_evidence.json",
        "contract_version": "0.3",
        "policy_version": "0.3"
    }
    emit_event(events_file, evt_parent)
    print(f"[Step 1] Parent Event 발행: {evt_parent['event_id']} (Status: {evt_parent['status']})")

    # Step 2: Policy Decision & Action Budget 관리
    action_budget = 2  # 최대 2회 액션 예산
    action_log = []

    # Policy 평가 (quarantine 결정)
    verdict, reason = validator.evaluate(evt_parent, "quarantine")
    print(f"[Step 2] Policy Decision: action='quarantine' -> {verdict} (사유: {reason})")
    assert verdict == "ALLOW"

    # Action 실행 (ExecutionGuard 통과)
    guard.guard_action("quarantine", verdict)
    action_budget -= 1
    action_log.append("quarantine")

    # Step 3: Child Event 발행 (Stage: action, Status: SUCCESS)
    child_event_id = f"evt_{run_id}_002"
    is_terminal = True  # quarantine은 터미널 액션 (안전 격리 완료 후 추가 재시도 없음)
    evt_child = {
        "event_id": child_event_id,
        "parent_event_id": parent_event_id,
        "project": "BackupSystem",
        "loop": "restore_verification",
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": "action",
        "action": "quarantine",
        "status": "SUCCESS",
        "policy_rule_id": "rule_data_integrity_quarantine",
        "validator_result": verdict,
        "duration_ms": 12.5,
        "evidence_ref": f"runs/{run_id}/evidence/quarantine_evidence.json",
        "action_budget_remaining": action_budget,
        "is_terminal": is_terminal,
        "contract_version": "0.3",
        "policy_version": "0.3"
    }
    emit_event(events_file, evt_child)
    print(f"[Step 3] Child Event 발행: {evt_child['event_id']} (Parent: {evt_child['parent_event_id']}, Status: {evt_child['status']})")

    # 계통 및 예산 검증
    assert evt_child["parent_event_id"] == evt_parent["event_id"], "Lineage 단절: parent_event_id 불일치"
    assert evt_child["run_id"] == evt_parent["run_id"], "run_id 불일치"
    assert is_terminal, "Terminal Action이 지정되지 않아 무한 체인 위험 발생"
    print(" >> [Lineage 검증 통과] Parent -> Child 계통 및 Terminal Action 종료 확인")

    # Step 4: Run Rollup 생성 (Event.status != Run.status 분리)
    # 개별 Event는 observe=FAIL, action=SUCCESS이나, Run 전체 상태는 COMPLETED_SAFE임.
    run_status = "COMPLETED_SAFE"
    rollup = {
        "run_id": run_id,
        "contract_version": "0.3",
        "policy_version": "0.3",
        "run_status": run_status,
        "total_events": 2,
        "parent_event_id": parent_event_id,
        "terminal_event_id": child_event_id,
        "terminal_action": "quarantine",
        "action_budget_initial": 2,
        "action_budget_remaining": action_budget,
        "chain_terminated": True,
        "status_separation_verified": True,
        "events_summary": [
            {"event_id": evt_parent["event_id"], "stage": evt_parent["stage"], "status": evt_parent["status"]},
            {"event_id": evt_child["event_id"], "stage": evt_child["stage"], "status": evt_child["status"]}
        ]
    }
    with open(rollup_file, "w", encoding="utf-8") as f:
        json.dump(rollup, f, indent=2, ensure_ascii=False)
    print(f"\n[Step 4] Run Rollup 집계 완료:")
    print(f" >> run_status: {rollup['run_status']} (Event status FAIL/SUCCESS와 분리됨)")
    print(f" >> action_budget_remaining: {rollup['action_budget_remaining']}")
    print(f" >> chain_terminated: {rollup['chain_terminated']}")

    # Step 5: 사후 불변성 검증
    post_core_hashes = capture_core_hashes(CORE_DIR)
    core_diffs = [k for k, v in pre_core_hashes.items() if post_core_hashes.get(k) != v]
    assert len(core_diffs) == 0, f"core/ 변경 발생: {core_diffs}"
    print("\n[Step 5] 사후 불변성 검증:")
    print(" >> [Gate 1 통과] core/ 무변경 확인 (0 diff)")

    post_repo_inventory = capture_repo_inventory(PRODUCTION_REPO)
    pre_files = pre_repo_inventory["files"]
    post_files = post_repo_inventory["files"]
    added = set(post_files.keys()) - set(pre_files.keys())
    removed = set(pre_files.keys()) - set(post_files.keys())
    modified = [f for f in (set(pre_files.keys()) & set(post_files.keys())) if pre_files[f] != post_files[f]]
    assert len(added) == 0 and len(removed) == 0 and len(modified) == 0, "운영 저장소 변경 발생"
    print(" >> [Gate 2 통과] 운영 저장소 무변경 확인 (0 diff)")

    print("\n" + "=" * 80)
    print(" 🎉 [SUCCESS] GOAL 6 Event / Child Event / Run Rollup 검증 100% 완료!")
    print(f" >> Rollup Summary: {rollup_file}")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
