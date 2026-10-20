#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
C:\Users\kksjmj\Desktop\ai\백업시스템\loop\run_adversarial_policy_validation.py
========================================================================
Loop Engineering Standard v0.2/v0.3 — Adversarial / Negative Policy Validation

검증 목표:
1. 정상/기대 동작 검증을 넘어, 정책 엔진 및 실행 경계(Execution Gate)가
   부적절한 액션, 불변식 위반, 버전 드리프트, 우회 시도를 원천 차단(Fail-Secure)하는지 실측.
2. 6대 적대적 테스트 벡터 전수 검증:
   Vector 1: file_not_found + quarantine 시도 ➔ BLOCK (INV-6: 존재하지 않는 객체 격리 차단)
   Vector 2: access_denied + quarantine 시도 ➔ BLOCK (INV-5: 권한 오류는 데이터 손상 아님)
   Vector 3: bitrot (integrity_critical) + retry 시도 ➔ BLOCK (INV-2: 손상 블롭 재시도 차단)
   Vector 4: PASS 상태 + abort/quarantine 시도 ➔ BLOCK (정상 상태에서 부적절 액션 차단)
   Vector 5: Execution Gate Bypass 시도 ➔ PolicyViolationError 발생 및 실행 차단
   Vector 6: Policy Drift 시도 (구버전 0.1 또는 변조된 rule_id) ➔ BLOCK
3. 사후 불변성 검증:
   - core/ 27개 파일 0 변경
   - 운영 저장소 51,038개 파일 0 변경
"""

from __future__ import annotations

import sys
import os
import json
import time
import shutil
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOOP_DIR = PROJECT_ROOT / "loop"
CORE_DIR = PROJECT_ROOT / "core"
PRODUCTION_REPO = Path(r"D:\MyBackup_Repository")
POLICY_FILE = LOOP_DIR / "backup_policy.yaml"
CONTRACT_FILE = LOOP_DIR / "backup_contract.yaml"
RUNS_DIR = LOOP_DIR / "runs"


def calc_sha256(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(64 * 1024):
            h.update(chunk)
    return h.hexdigest()


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
            inventory["snapshot_hashes"][sf.name] = calc_sha256(sf)

    return inventory


def capture_core_hashes(core_path: Path) -> dict:
    hashes = {}
    for pyfile in core_path.glob("*.py"):
        hashes[pyfile.name] = calc_sha256(pyfile)
    return hashes


def emit_event(events_file: Path, event_data: dict):
    events_file.parent.mkdir(parents=True, exist_ok=True)
    with open(events_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(event_data, ensure_ascii=False) + "\n")


class PolicyViolationError(Exception):
    """Raised when an execution gate bypass is attempted."""
    pass


class PolicyEngine:
    def __init__(self, policy_path: Path):
        self.policy_path = policy_path
        self.policy_data = self._load_policy()
        self.version = self.policy_data.get("policy_version", "0.0")
        self.rules = {r["error_class"]: r for r in self.policy_data.get("rules", [])}
        self.invariants = {inv["id"]: inv for inv in self.policy_data.get("invariants", [])}

    def _load_policy(self) -> dict:
        import yaml
        with open(self.policy_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def evaluate(self, event: dict, proposed_action: str, proposed_policy_version: Optional[str] = None) -> tuple[str, str]:
        """
        Evaluate proposed action against Policy and Invariants.
        Returns: (verdict: ALLOW | BLOCK, reason: str)
        """
        # 1. Version / Drift Check
        effective_version = proposed_policy_version or event.get("policy_version")
        if effective_version != self.version:
            return "BLOCK", f"Policy drift detected: event version '{effective_version}' != active policy '{self.version}'"

        status = event.get("status")
        error_class = event.get("error_class")
        traits = event.get("traits", [])

        # 2. Healthy State Check (PASS 상태에 대한 부적절 방어 액션 차단)
        if status == "PASS":
            if proposed_action in ["quarantine", "abort", "escalate"]:
                return "BLOCK", f"Inappropriate action '{proposed_action}' proposed for healthy state (PASS)"

        # 3. Invariants Verification
        # INV-2: Integrity / Security Non-Retryable
        if "integrity_critical" in traits or "security_critical" in traits:
            if proposed_action == "retry":
                return "BLOCK", "INV-2 violation: retry is strictly FORBIDDEN for integrity_critical / security_critical traits"

        # INV-5: No Quarantine on Access Denial
        if "access_restricted" in traits:
            if proposed_action == "quarantine":
                return "BLOCK", "INV-5 violation: quarantine is strictly FORBIDDEN for access_restricted traits"

        # INV-6: No Quarantine on Existence Failure
        if "missing_resource" in traits:
            if proposed_action == "quarantine":
                return "BLOCK", "INV-6 violation: quarantine is strictly FORBIDDEN for missing_resource traits"

        # 4. Registered Rules Check
        if error_class:
            rule = self.rules.get(error_class)
            if not rule:
                return "BLOCK", f"Unregistered error_class: '{error_class}'"

            forbidden = rule.get("forbidden_actions", [])
            if proposed_action in forbidden:
                return "BLOCK", f"Rule for '{error_class}' explicitly forbids '{proposed_action}'"

            allowed = rule.get("allowed_actions", [])
            if proposed_action not in allowed:
                return "BLOCK", f"Rule for '{error_class}' does not permit '{proposed_action}'"

        return "ALLOW", "Action compliant with active policy and all invariants"


class SafeExecutor:
    """Enforces that only ALLOWED actions can cross the Execution Gate."""
    def __init__(self, policy_engine: PolicyEngine):
        self.engine = policy_engine
        self.executed_mutations = 0

    def execute_action(self, event: dict, action: str, validator_verdict: str) -> dict:
        if validator_verdict != "ALLOW":
            raise PolicyViolationError(
                f"[EXECUTION_GATE_BLOCKED] Attempted to execute action '{action}' "
                f"with validator verdict '{validator_verdict}'"
            )
        
        # 실제 안전한 액션 수행 (시뮬레이션)
        self.executed_mutations += 1
        return {"action": action, "status": "EXECUTED"}


def run_experiment():
    run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_adversarial"
    run_dir = RUNS_DIR / run_id
    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    events_file = run_dir / "events.jsonl"
    rollup_file = run_dir / "run_rollup.json"

    print("=" * 80)
    print(" 🛡️ [Loop Engineering Standard v0.2/0.3 — Adversarial Policy Validation]")
    print(f" >> Run ID: {run_id}")
    print(f" >> Policy Version: 0.3 | Active Invariants: INV-1 ~ INV-6")
    print("=" * 80 + "\n")

    # Step 0: 사전 불변성 캡처
    print("[Step 0] 사전 불변성 인벤토리 캡처...")
    pre_core_hashes = capture_core_hashes(CORE_DIR)
    print(f" >> core/ 대상 파일 수: {len(pre_core_hashes)}개")

    pre_repo_inventory = capture_repo_inventory(PRODUCTION_REPO)
    print(f" >> {PRODUCTION_REPO} 대상 파일 수: {len(pre_repo_inventory['files'])}개\n")

    engine = PolicyEngine(POLICY_FILE)
    executor = SafeExecutor(engine)
    test_results = []

    # --------------------------------------------------------------------------
    # Vector 1: file_not_found + quarantine ➔ BLOCK (INV-6)
    # --------------------------------------------------------------------------
    print("[Vector 1] file_not_found + quarantine 시도 (INV-6 검증)...")
    evt1 = {
        "event_id": f"evt_{run_id}_v1_001",
        "status": "FAIL",
        "error_class": "file_not_found",
        "traits": ["missing_resource"],
        "policy_version": "0.3"
    }
    v1_verdict, v1_reason = engine.evaluate(evt1, proposed_action="quarantine")
    print(f" >> 판정 결과: {v1_verdict} (사유: {v1_reason})")
    assert v1_verdict == "BLOCK", f"Vector 1 실패: BLOCK 기대했으나 {v1_verdict}"
    assert "INV-6" in v1_reason, f"Vector 1 실패: INV-6 사유 누락: {v1_reason}"
    test_results.append({"vector": 1, "name": "file_not_found_quarantine", "verdict": v1_verdict, "pass": True})
    emit_event(events_file, {"vector": 1, "event": evt1, "proposed_action": "quarantine", "verdict": v1_verdict, "reason": v1_reason})

    # --------------------------------------------------------------------------
    # Vector 2: access_denied + quarantine ➔ BLOCK (INV-5)
    # --------------------------------------------------------------------------
    print("\n[Vector 2] access_denied + quarantine 시도 (INV-5 검증)...")
    evt2 = {
        "event_id": f"evt_{run_id}_v2_001",
        "status": "FAIL",
        "error_class": "access_denied",
        "traits": ["access_restricted"],
        "policy_version": "0.3"
    }
    v2_verdict, v2_reason = engine.evaluate(evt2, proposed_action="quarantine")
    print(f" >> 판정 결과: {v2_verdict} (사유: {v2_reason})")
    assert v2_verdict == "BLOCK", f"Vector 2 실패: BLOCK 기대했으나 {v2_verdict}"
    assert "INV-5" in v2_reason, f"Vector 2 실패: INV-5 사유 누락: {v2_reason}"
    test_results.append({"vector": 2, "name": "access_denied_quarantine", "verdict": v2_verdict, "pass": True})
    emit_event(events_file, {"vector": 2, "event": evt2, "proposed_action": "quarantine", "verdict": v2_verdict, "reason": v2_reason})

    # --------------------------------------------------------------------------
    # Vector 3: bitrot + retry ➔ BLOCK (INV-2)
    # --------------------------------------------------------------------------
    print("\n[Vector 3] bitrot (integrity_critical) + retry 시도 (INV-2 검증)...")
    evt3 = {
        "event_id": f"evt_{run_id}_v3_001",
        "status": "FAIL",
        "error_class": "data_integrity_violation",
        "traits": ["integrity_critical"],
        "policy_version": "0.3"
    }
    v3_verdict, v3_reason = engine.evaluate(evt3, proposed_action="retry")
    print(f" >> 판정 결과: {v3_verdict} (사유: {v3_reason})")
    assert v3_verdict == "BLOCK", f"Vector 3 실패: BLOCK 기대했으나 {v3_verdict}"
    assert "INV-2" in v3_reason, f"Vector 3 실패: INV-2 사유 누락: {v3_reason}"
    test_results.append({"vector": 3, "name": "bitrot_retry", "verdict": v3_verdict, "pass": True})
    emit_event(events_file, {"vector": 3, "event": evt3, "proposed_action": "retry", "verdict": v3_verdict, "reason": v3_reason})

    # --------------------------------------------------------------------------
    # Vector 4: PASS 상태 + abort/quarantine ➔ BLOCK (Healthy State Protection)
    # --------------------------------------------------------------------------
    print("\n[Vector 4] 정상 파일(PASS 상태) + abort/quarantine 시도...")
    evt4 = {
        "event_id": f"evt_{run_id}_v4_001",
        "status": "PASS",
        "error_class": None,
        "traits": [],
        "policy_version": "0.3"
    }
    v4_verdict_q, v4_reason_q = engine.evaluate(evt4, proposed_action="quarantine")
    v4_verdict_a, v4_reason_a = engine.evaluate(evt4, proposed_action="abort")
    print(f" >> quarantine 판정: {v4_verdict_q} (사유: {v4_reason_q})")
    print(f" >> abort 판정: {v4_verdict_a} (사유: {v4_reason_a})")
    assert v4_verdict_q == "BLOCK" and v4_verdict_a == "BLOCK", "Vector 4 실패: 정상 상태에서 방어 액션이 허용됨"
    test_results.append({"vector": 4, "name": "healthy_state_defensive_action", "verdict": "BLOCK", "pass": True})
    emit_event(events_file, {"vector": 4, "event": evt4, "actions": ["quarantine", "abort"], "verdict": "BLOCK"})

    # --------------------------------------------------------------------------
    # Vector 5: Execution Gate Bypass 시도 ➔ PolicyViolationError 발생 및 차단
    # --------------------------------------------------------------------------
    print("\n[Vector 5] Execution Gate Bypass 시도 (BLOCK된 액션의 강제 실행 차단)...")
    bypass_caught = False
    try:
        # v1_verdict는 BLOCK임. 이를 강제로 execute_action으로 넘김
        executor.execute_action(evt1, action="quarantine", validator_verdict=v1_verdict)
    except PolicyViolationError as e:
        bypass_caught = True
        print(f" >> [안전 차단 성공] Execution Gate가 침해를 감지하고 예외 발생: {e}")

    assert bypass_caught, "Vector 5 실패: Execution Gate가 BLOCK 판정을 무시하고 액션을 실행함!"
    assert executor.executed_mutations == 0, "Vector 5 실패: 차단되었음에도 mutation 카운트가 증가함!"
    test_results.append({"vector": 5, "name": "execution_gate_bypass_prevention", "verdict": "EXCEPTION_RAISED", "pass": True})
    emit_event(events_file, {"vector": 5, "bypass_attempted": True, "bypass_caught": True})

    # --------------------------------------------------------------------------
    # Vector 6: Policy Drift 시도 (구버전 0.1 및 변조된 버전 9.9) ➔ BLOCK
    # --------------------------------------------------------------------------
    print("\n[Vector 6] Policy Drift 시도 (구버전 '0.1' 및 변조 버전 '9.9' 전달)...")
    
    # 6a: 구버전 (0.1) 드리프트 검증
    evt6a = {
        "event_id": f"evt_{run_id}_v6a_001",
        "status": "FAIL",
        "error_class": "access_denied",
        "traits": ["access_restricted"],
        "policy_version": "0.1"
    }
    v6a_verdict, v6a_reason = engine.evaluate(evt6a, proposed_action="abort", proposed_policy_version="0.1")
    print(f" >> [6a] 구버전(0.1) 판정: {v6a_verdict} (사유: {v6a_reason})")
    assert v6a_verdict == "BLOCK", f"Vector 6a 실패: 구버전 Drift가 허용됨: {v6a_verdict}"
    assert "Policy drift" in v6a_reason, f"Vector 6a 실패: Drift 사유 누락: {v6a_reason}"
    emit_event(events_file, {"vector": 6, "sub": "a", "event": evt6a, "proposed_version": "0.1", "verdict": v6a_verdict, "reason": v6a_reason})

    # 6b: 변조된/미등록 버전 (9.9) 드리프트 검증
    evt6b = {
        "event_id": f"evt_{run_id}_v6b_001",
        "status": "FAIL",
        "error_class": "access_denied",
        "traits": ["access_restricted"],
        "policy_version": "9.9"
    }
    v6b_verdict, v6b_reason = engine.evaluate(evt6b, proposed_action="abort", proposed_policy_version="9.9")
    print(f" >> [6b] 변조 버전(9.9) 판정: {v6b_verdict} (사유: {v6b_reason})")
    assert v6b_verdict == "BLOCK", f"Vector 6b 실패: 변조 버전 Drift가 허용됨: {v6b_verdict}"
    assert "Policy drift" in v6b_reason, f"Vector 6b 실패: Drift 사유 누락: {v6b_reason}"
    emit_event(events_file, {"vector": 6, "sub": "b", "event": evt6b, "proposed_version": "9.9", "verdict": v6b_verdict, "reason": v6b_reason})

    test_results.append({"vector": 6, "name": "policy_drift_prevention", "verdict": "BLOCK", "pass": True})

    # --------------------------------------------------------------------------
    # Step 3: 사후 불변성 전수 검증
    # --------------------------------------------------------------------------
    print("\n[Step 3] 사후 불변성 전수 검증 (Zero Mutation Check)...")
    post_core_hashes = capture_core_hashes(CORE_DIR)
    core_diffs = []
    for k, v in pre_core_hashes.items():
        if post_core_hashes.get(k) != v:
            core_diffs.append(k)
    for k in post_core_hashes:
        if k not in pre_core_hashes:
            core_diffs.append(f"+{k}")

    assert len(core_diffs) == 0, f"core/ 무변경 원칙 위반: {core_diffs}"
    print(" >> ✅ [Gate 1 통과] core/ 코드 변경 0줄 확인!")

    post_repo_inventory = capture_repo_inventory(PRODUCTION_REPO)
    pre_files = pre_repo_inventory["files"]
    post_files = post_repo_inventory["files"]

    added_files = set(post_files.keys()) - set(pre_files.keys())
    removed_files = set(pre_files.keys()) - set(post_files.keys())
    modified_files = [
        f for f in (set(pre_files.keys()) & set(post_files.keys()))
        if pre_files[f] != post_files[f]
    ]

    print(f" >> {PRODUCTION_REPO} 변경 내역:")
    print(f"    - 추가된 파일: {len(added_files)}개 | 삭제: {len(removed_files)}개 | 수정: {len(modified_files)}개")

    assert len(added_files) == 0, f"운영 저장소 파일 추가됨: {added_files}"
    assert len(removed_files) == 0, f"운영 저장소 파일 삭제됨: {removed_files}"
    assert len(modified_files) == 0, f"운영 저장소 파일 수정됨: {modified_files}"
    print(" >> ✅ [Gate 2 통과] 운영 저장소 변경 0건 전수 검증 완료!")

    # Rollup 생성
    rollup = {
        "run_id": run_id,
        "contract_version": "0.2",
        "policy_version": "0.3",
        "test_type": "ADVERSARIAL_POLICY_VALIDATION",
        "run_status": "COMPLETED_SAFE",
        "total_vectors": len(test_results),
        "passed_vectors": sum(1 for r in test_results if r["pass"]),
        "failed_vectors": 0,
        "bypass_prevented": True,
        "drift_prevented": True,
        "inventory_mutations": 0,
        "core_mutations": 0,
        "results": test_results
    }

    with open(rollup_file, "w", encoding="utf-8") as f:
        json.dump(rollup, f, indent=2)

    print("\n" + "=" * 80)
    print(" 🎉 [SUCCESS] 6대 Adversarial Policy Test Vectors 100% 통과 (ALL BLOCKED / SECURED)!")
    print(f" >> Events Log: {events_file}")
    print(f" >> Rollup Summary: {rollup_file}")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    run_experiment()
