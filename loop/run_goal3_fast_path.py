#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
C:\Users\kksjmj\Desktop\ai\백업시스템\loop\run_goal3_fast_path.py
========================================================================
GOAL 3: 정상 Fast Path 실제 검증

검증 내용:
1. loop/restore_adapter.py의 RestoreAdapter를 실제 정상 스냅샷에 연결
   - 대상 Snapshot: snap_20261002_090011_a5d463
   - 운영 저장소: D:\MyBackup_Repository (읽기 전용)
   - 임시 복원 대상: tempfile.mkdtemp()
2. RestoreEngine.restore_snapshot() 실제 반환값 수집
3. Contract v0.3 평가:
   - AC-RESTORE-01: restored_files > 0
   - AC-RESTORE-02: failed_files == []
   - AC-RESTORE-03: restored_files + skipped_files == total_files
4. Event (evt_{run_id}_001) 및 Evidence (restore_evidence.json) 생성 및 run_id 전파 확인
5. 임시 복원 Sandbox 안전 정리
6. 사전/사후 core/ 및 운영 저장소 0 mutation 검증
"""

from __future__ import annotations

import os
import sys
import json
import time
import shutil
import hashlib
import tempfile
from pathlib import Path
from datetime import datetime, timezone

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
RUNS_DIR = LOOP_DIR / "runs"
TARGET_SNAPSHOT_ID = "snap_20261002_090011_a5d463"


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
            h = hashlib.sha256(sf.read_bytes()).hexdigest()
            inventory["snapshot_hashes"][sf.name] = h
    return inventory


def capture_core_hashes(core_path: Path) -> dict:
    hashes = {}
    for pyfile in core_path.rglob("*.py"):
        if "__pycache__" not in pyfile.parts:
            hashes[pyfile.as_posix()] = hashlib.sha256(pyfile.read_bytes()).hexdigest()
    return hashes


def main():
    run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_goal3_fast_path"
    print("=" * 80)
    print(" 🚀 [GOAL 3] 정상 Fast Path 실제 검증")
    print(f" >> Run ID: {run_id}")
    print(f" >> Target Repo: {PRODUCTION_REPO}")
    print(f" >> Target Snapshot: {TARGET_SNAPSHOT_ID}")
    print("=" * 80 + "\n")

    # Step 0: 사전 불변성 인벤토리
    print("[Step 0] 사전 상태 캡처...")
    pre_core_hashes = capture_core_hashes(CORE_DIR)
    pre_repo_inventory = capture_repo_inventory(PRODUCTION_REPO)
    print(f" >> core/ 소스 파일 수: {len(pre_core_hashes)}개")
    print(f" >> {PRODUCTION_REPO} 파일 수: {len(pre_repo_inventory['files'])}개\n")

    # Step 1: RestoreAdapter 실행
    temp_sandbox = Path(tempfile.mkdtemp(prefix="goal3_fast_sandbox_"))
    adapter = RestoreAdapter(runs_dir=RUNS_DIR)
    print(f"[Step 1] RestoreAdapter.run_restore_verification() 실행 -> 임시 Sandbox: {temp_sandbox}")

    t0 = time.perf_counter()
    try:
        event, evidence = adapter.run_restore_verification(
            repo_dir=str(PRODUCTION_REPO),
            snapshot_id=TARGET_SNAPSHOT_ID,
            target_dir=str(temp_sandbox),
            run_id=run_id,
            in_place=False,
            overwrite=True,
            verify_hash=True
        )
    finally:
        t1 = time.perf_counter()
        shutil.rmtree(temp_sandbox, ignore_errors=True)
        print(f" >> Sandbox 정리 완료: {temp_sandbox}")

    wall_clock_sec = t1 - t0
    raw_res = evidence["raw_restore_result"]
    print(f"\n[Step 2] 복원 실행 결과:")
    print(f" >> wall_clock_sec: {wall_clock_sec:.2f}s")
    print(f" >> total_files: {raw_res.get('total_files')}")
    print(f" >> restored_files: {raw_res.get('restored_files')}")
    print(f" >> skipped_files: {raw_res.get('skipped_files')}")
    print(f" >> failed_files: {raw_res.get('failed_files')}")
    print(f" >> duration_seconds: {raw_res.get('duration_seconds')}s")

    # Contract 평가 확인
    status, err, code, crit = adapter.evaluate_contract(raw_res)
    print(f"\n[Step 3] Contract v0.3 평가 결과:")
    print(f" >> Status: {status}")
    print(f" >> Criteria: {crit}")
    assert status == "PASS", f"Contract 평가 실패: {err}, {code}"
    assert crit["AC-RESTORE-01"], "AC-RESTORE-01 실패 (restored_files <= 0)"
    assert crit["AC-RESTORE-02"], "AC-RESTORE-02 실패 (failed_files != [])"
    assert crit["AC-RESTORE-03"], "AC-RESTORE-03 실패 (restored + skipped != total)"

    # Event 확인
    print(f"\n[Step 4] Event 확인:")
    print(f" >> Event ID: {event['event_id']}")
    print(f" >> Run ID: {event['run_id']}")
    print(f" >> Status: {event['status']}")
    print(f" >> Evidence Ref: {event['evidence_ref']}")
    assert event["run_id"] == run_id, "run_id 불일치"
    assert event["status"] == "PASS", "event status 불일치"

    # Step 5: 사후 불변성 검증
    print("\n[Step 5] 사후 불변성 검증...")
    post_core_hashes = capture_core_hashes(CORE_DIR)
    core_diffs = [k for k, v in pre_core_hashes.items() if post_core_hashes.get(k) != v]
    assert len(core_diffs) == 0, f"core/ 변경 감지: {core_diffs}"
    print(" >> [Gate 1 통과] core/ 무변경 확인 (0 diff)")

    post_repo_inventory = capture_repo_inventory(PRODUCTION_REPO)
    pre_files = pre_repo_inventory["files"]
    post_files = post_repo_inventory["files"]
    added = set(post_files.keys()) - set(pre_files.keys())
    removed = set(pre_files.keys()) - set(post_files.keys())
    modified = [f for f in (set(pre_files.keys()) & set(post_files.keys())) if pre_files[f] != post_files[f]]
    print(f" >> {PRODUCTION_REPO} 변경 내역: 추가 {len(added)}개 | 삭제 {len(removed)}개 | 수정 {len(modified)}개")
    assert len(added) == 0 and len(removed) == 0 and len(modified) == 0, "운영 저장소 변경 감지"
    print(" >> [Gate 2 통과] 운영 저장소 무변경 확인 (0 diff)")

    print("\n" + "=" * 80)
    print(" 🎉 [SUCCESS] GOAL 3 정상 Fast Path 실제 검증 100% 완료!")
    print(f" >> Run Dir: {RUNS_DIR / run_id}")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
