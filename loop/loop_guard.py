#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
C:\Users\kksjmj\Desktop\ai\백업시스템\loop\loop_guard.py
========================================================================
BackupSystem — Loop Policy Validator & Execution Guard (Loop Engineering v0.2/v0.3)

역할:
1. backup_policy.yaml (v0.3)을 로드하여 불변식 및 정책 규칙 평가
   - INV-1: Bounded Retry
   - INV-2: Integrity / Security Non-Retryable (integrity_critical + retry ➔ BLOCK)
   - INV-5: No Quarantine on Access Denial (access_restricted + quarantine ➔ BLOCK)
   - INV-6: No Quarantine on Existence Failure (missing_resource + quarantine ➔ BLOCK)
   - Healthy State Invalidation: PASS 상태에서 방어 액션(quarantine, abort) ➔ BLOCK
   - Policy Drift Prevention: 활성 정책 버전과 불일치 시 ➔ BLOCK
2. ExecutionGuard:
   - Validator 판정이 ALLOW가 아닐 경우 액션 실행을 차단하고 PolicyViolationError 발생.
   - Validator 우회(Bypass) 원천 차단.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOOP_DIR = PROJECT_ROOT / "loop"
DEFAULT_POLICY_PATH = LOOP_DIR / "backup_policy.yaml"


class PolicyViolationError(Exception):
    """Raised when an action is executed without an ALLOW verdict from PolicyValidator."""
    pass


class PolicyValidator:
    def __init__(self, policy_path: Optional[Path] = None):
        self.policy_path = policy_path or DEFAULT_POLICY_PATH
        self.policy_data = self._load_policy()
        self.version = str(self.policy_data.get("policy_version", "0.3"))
        self.rules = {r["error_class"]: r for r in self.policy_data.get("rules", [])}
        self.invariants = {inv["id"]: inv for inv in self.policy_data.get("invariants", [])}

    def _load_policy(self) -> Dict[str, Any]:
        if not self.policy_path.exists():
            return {"policy_version": "0.3", "invariants": [], "rules": []}
        import yaml
        with open(self.policy_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def evaluate(
        self,
        event: Dict[str, Any],
        proposed_action: str,
        proposed_policy_version: Optional[str] = None
    ) -> Tuple[str, str]:
        """
        Evaluates proposed action against policy invariants and rules.
        Returns: (verdict: ALLOW | BLOCK, reason: str)
        """
        # 1. Version Drift Check
        effective_version = str(proposed_policy_version or event.get("policy_version", self.version))
        if effective_version != self.version:
            return "BLOCK", f"Policy drift detected: event version '{effective_version}' != active policy '{self.version}'"

        status = event.get("status")
        error_class = event.get("error_class")
        traits = event.get("traits", [])

        # 2. Healthy State Protection
        if status == "PASS":
            if proposed_action in ["quarantine", "abort", "escalate"]:
                return "BLOCK", f"Inappropriate defensive action '{proposed_action}' proposed for healthy state (PASS)"

        # 3. Minimal Invariants Check
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


class ExecutionGuard:
    """Execution Boundary that prevents unapproved actions from running."""
    def __init__(self, validator: PolicyValidator):
        self.validator = validator

    def guard_action(self, action: str, validator_verdict: str) -> None:
        """
        Enforces that only ALLOWED actions may cross the Execution Gate.
        Raises PolicyViolationError if blocked.
        """
        if validator_verdict != "ALLOW":
            raise PolicyViolationError(
                f"[EXECUTION_GATE_BLOCKED] Attempted to execute action '{action}' "
                f"while PolicyValidator verdict is '{validator_verdict}'"
            )
