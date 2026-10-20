#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
C:\Users\kksjmj\Desktop\ai\백업시스템\loop\restore_adapter.py
========================================================================
BackupSystem — Restore Verification Thin Adapter (Loop Engineering v0.2/v0.3)

역할:
1. Run Context 생성 (run_id, run_dir, evidence_dir)
2. core.restore.RestoreEngine.restore_snapshot() 호출 (외부 호출, 기존 로직 복사 금지)
3. 실제 반환값 dict 해석:
   - snapshot_id
   - target_dir
   - total_files
   - restored_files
   - skipped_files
   - failed_files
   - restored_bytes
   - duration_seconds
4. Contract 평가 (AC-RESTORE-01, 02, 03)
   - AC-RESTORE-01: restored_files > 0
   - AC-RESTORE-02: failed_files == []
   - AC-RESTORE-03: restored_files + skipped_files == total_files
5. Evidence 저장 (실제 반환 dict를 그대로 evidence/restore_evidence.json에 보존)
6. Event 생성 및 발행 (events.jsonl에 append)
"""

from __future__ import annotations

import os
import sys
import json
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.restore import RestoreEngine

LOOP_DIR = PROJECT_ROOT / "loop"
DEFAULT_CONTRACT_PATH = LOOP_DIR / "backup_contract.yaml"
DEFAULT_POLICY_PATH = LOOP_DIR / "backup_policy.yaml"
DEFAULT_RUNS_DIR = LOOP_DIR / "runs"


class RestoreAdapter:
    def __init__(
        self,
        contract_path: Optional[Path] = None,
        policy_path: Optional[Path] = None,
        runs_dir: Optional[Path] = None
    ):
        self.contract_path = contract_path or DEFAULT_CONTRACT_PATH
        self.policy_path = policy_path or DEFAULT_POLICY_PATH
        self.runs_dir = runs_dir or DEFAULT_RUNS_DIR
        self.contract_version = self._read_version(self.contract_path, "contract_version", "0.3")
        self.policy_version = self._read_version(self.policy_path, "policy_version", "0.3")

    @staticmethod
    def _read_version(filepath: Path, key: str, default: str) -> str:
        if not filepath.exists():
            return default
        try:
            import yaml
            with open(filepath, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                return str(data.get(key, default))
        except Exception:
            return default

    def evaluate_contract(self, restore_result: Dict[str, Any]) -> Tuple[str, Optional[str], Optional[str], Dict[str, bool]]:
        """
        Evaluates actual restore_snapshot() return dict against Contract v0.3.
        Returns: (status: PASS|FAIL, error_class, error_code, criteria_results)
        """
        total_files = restore_result.get("total_files", 0)
        restored_files = restore_result.get("restored_files", 0)
        skipped_files = restore_result.get("skipped_files", 0)
        failed_files = restore_result.get("failed_files", [])

        # AC-RESTORE-01: restored_files > 0
        ac1 = (isinstance(restored_files, int) and restored_files > 0)

        # AC-RESTORE-02: failed_files == []
        ac2 = (isinstance(failed_files, list) and len(failed_files) == 0)

        # AC-RESTORE-03: restored_files + skipped_files == total_files
        ac3 = ((restored_files + skipped_files) == total_files)

        criteria = {
            "AC-RESTORE-01": ac1,
            "AC-RESTORE-02": ac2,
            "AC-RESTORE-03": ac3
        }

        if all(criteria.values()):
            return "PASS", None, None, criteria

        # Determine failure classification
        if not ac2:
            return "FAIL", "restore_file_failures", "E_RESTORE_FAILED_FILES", criteria
        if not ac3:
            return "FAIL", "restore_accounting_mismatch", "E_TOTAL_FILES_MISMATCH", criteria
        if not ac1:
            return "FAIL", "zero_files_restored", "E_ZERO_RESTORED", criteria

        return "FAIL", "restore_contract_violation", "E_CONTRACT_FAIL", criteria

    def run_restore_verification(
        self,
        repo_dir: str,
        snapshot_id: str,
        target_dir: str,
        run_id: Optional[str] = None,
        in_place: bool = False,
        overwrite: bool = True,
        verify_hash: bool = True
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Executes restore verification without modifying existing restore engine.
        Returns: (event_dict, evidence_dict)
        """
        # 1. Run Context 생성
        if not run_id:
            run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_restore"

        run_dir = self.runs_dir / run_id
        evidence_dir = run_dir / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        events_file = run_dir / "events.jsonl"
        evidence_file = evidence_dir / "restore_evidence.json"

        # 2. RestoreEngine.restore_snapshot() 호출
        t0 = time.perf_counter()
        try:
            restore_result = RestoreEngine.restore_snapshot(
                repo_dir=repo_dir,
                snapshot_id=snapshot_id,
                target_dir=target_dir,
                in_place=in_place,
                overwrite=overwrite,
                verify_hash=verify_hash
            )
            t1 = time.perf_counter()
            wall_clock_ms = (t1 - t0) * 1000

            # 3. Contract 평가
            status, error_class, error_code, criteria = self.evaluate_contract(restore_result)

        except Exception as e:
            t1 = time.perf_counter()
            wall_clock_ms = (t1 - t0) * 1000
            restore_result = {
                "snapshot_id": snapshot_id,
                "target_dir": target_dir,
                "exception": str(e),
                "total_files": 0,
                "restored_files": 0,
                "skipped_files": 0,
                "failed_files": [(str(snapshot_id), str(e))],
                "restored_bytes": 0,
                "duration_seconds": round(wall_clock_ms / 1000, 2)
            }
            status = "FAIL"
            error_class = "restore_engine_exception"
            error_code = type(e).__name__

        # 4. Evidence 보존 (실제 반환 dict 그대로 저장)
        evidence_data = {
            "run_id": run_id,
            "contract_version": self.contract_version,
            "policy_version": self.policy_version,
            "raw_restore_result": restore_result,
            "wall_clock_ms": round(wall_clock_ms, 2)
        }
        with open(evidence_file, "w", encoding="utf-8") as f:
            json.dump(evidence_data, f, indent=2, ensure_ascii=False)

        # 5. Event 생성 및 발행
        event = {
            "event_id": f"evt_{run_id}_001",
            "project": "BackupSystem",
            "loop": "restore_verification",
            "run_id": run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stage": "observe",
            "status": status,
            "error_class": error_class,
            "error_code": error_code,
            "duration_ms": round(wall_clock_ms, 2),
            "evidence_ref": f"runs/{run_id}/evidence/restore_evidence.json",
            "contract_version": self.contract_version,
            "policy_version": self.policy_version
        }

        with open(events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

        return event, evidence_data
