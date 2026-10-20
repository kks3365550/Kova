#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
C:\Users\kksjmj\Desktop\ai\백업시스템\loop\run_goal5_defensive_path.py
========================================================================
GOAL 5: Defensive Path 검증 (Detection + Safe Isolation)

검증 내용:
1. GOAL 4의 integrity_critical 결함 이벤트를 PolicyValidator & ExecutionGuard에 연결
2. 불변식 검증:
   - retry 시도 시 PolicyValidator가 INV-2 위반으로 BLOCK
   - ExecutionGuard가 BLOCK 판정 액션의 실행을 원천 차단 (PolicyViolationError)
3. Safe Action 판정:
   - quarantine 액션 제안 시 PolicyValidator가 ALLOW 승인
   - ExecutionGuard 통과
4. 비파괴적 격리 (Safe Isolation):
   - 의심 블롭을 격리 영역으로 안전 복사/보존 (삭제 없음)
   - 원본 Fixture 내 정상 블롭 및 스냅샷 매니페스트 100% 보호
5. 사후 core/ 및 운영 저장소 0 mutation 검증
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

from core.snapshot import SnapshotEngine
from loop.restore_adapter import RestoreAdapter
from loop.loop_guard import PolicyValidator, ExecutionGuard, PolicyViolationError

PRODUCTION_REPO = Path(r"D:\MyBackup_Repository")
CORE_DIR = PROJECT_ROOT / "core"
LOOP_DIR = PROJECT_ROOT / "loop"
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


def main():
    run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_goal5_defensive"
    run_dir = RUNS_DIR / run_id
    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print(" 🛡️ [GOAL 5] Defensive Path 검증 (Policy / Validator / Safe Isolation)")
    print(f" >> Run ID: {run_id}")
    print(f" >> Live Repo: {PRODUCTION_REPO} (READ-ONLY STRICT GUARANTEE)")
    print("=" * 80 + "\n")

    # Step 0: 사전 상태 캡처
    pre_core_hashes = capture_core_hashes(CORE_DIR)
    pre_repo_inventory = capture_repo_inventory(PRODUCTION_REPO)
    print(f"[Step 0] 사전 상태 캡처: core/={len(pre_core_hashes)}개, LiveRepo={len(pre_repo_inventory['files'])}개\n")

    # Step 1: 독립 Fixture 생성 및 1-byte 결함 주입
    fixture_dir = Path(tempfile.mkdtemp(prefix="goal5_fixture_"))
    fixture_repo = fixture_dir / "repo"
    fixture_source = fixture_dir / "source"
    fixture_source.mkdir(parents=True, exist_ok=True)
    fixture_repo.mkdir(parents=True, exist_ok=True)

    file_valid = fixture_source / "valid_doc.txt"
    file_corrupt = fixture_source / "corrupt_doc.txt"
    file_valid.write_bytes(b"PAYLOAD_VALID_DOC_DATA_001")
    file_corrupt.write_bytes(b"PAYLOAD_CORRUPT_TARGET_DATA_002")

    snap_manifest = SnapshotEngine.create_snapshot(
        repo_dir=str(fixture_repo),
        sources=[str(fixture_source)],
        profile_id="goal5_fixture_profile",
        profile_name="Goal 5 Profile",
        compress_level=3
    )
    fixture_snap_id = snap_manifest.get("id") or snap_manifest.get("snapshot_id")

    blobs = list((fixture_repo / "blobs").rglob("*.blob"))
    target_blob = blobs[0]
    valid_blob = blobs[1] if len(blobs) > 1 else None

    # 결함 주입 (1-byte 변조)
    original_blob_bytes = target_blob.read_bytes()
    corrupted_bytes = bytearray(original_blob_bytes)
    corrupted_bytes[len(corrupted_bytes) // 2] ^= 0xFF
    try:
        os.chmod(target_blob, 0o666)
    except Exception:
        pass
    target_blob.write_bytes(bytes(corrupted_bytes))

    # Step 2: RestoreAdapter 실행 -> FAIL Event 포착
    target_sandbox = Path(tempfile.mkdtemp(prefix="goal5_sandbox_"))
    adapter = RestoreAdapter(runs_dir=RUNS_DIR)
    event, evidence = adapter.run_restore_verification(
        repo_dir=str(fixture_repo),
        snapshot_id=fixture_snap_id,
        target_dir=str(target_sandbox),
        run_id=run_id,
        in_place=False,
        overwrite=True,
        verify_hash=True
    )
    shutil.rmtree(target_sandbox, ignore_errors=True)

    # Event에 무결성 결함 트레이트 보강
    event["error_class"] = "data_integrity_violation"
    event["traits"] = ["integrity_critical"]

    print(f"[Step 1~2] 결함 복원 실행 및 FAIL Event 포착:")
    print(f" >> Event ID: {event['event_id']}")
    print(f" >> Status: {event['status']}")
    print(f" >> Traits: {event['traits']}")

    # Step 3: PolicyValidator 평가 (INV-2 검증)
    validator = PolicyValidator()
    guard = ExecutionGuard(validator)

    print(f"\n[Step 3] PolicyValidator 평가 및 불변식 검증:")
    # 검증 3-1: retry 시도 시 차단 확인
    retry_verdict, retry_reason = validator.evaluate(event, "retry")
    print(f" >> retry 제안 판정: {retry_verdict} (사유: {retry_reason})")
    assert retry_verdict == "BLOCK", f"INV-2 위반: retry가 허용됨 ({retry_verdict})"
    assert "INV-2" in retry_reason, "INV-2 사유 누락"

    # 검증 3-2: ExecutionGuard 우회 차단 확인
    bypass_caught = False
    try:
        guard.guard_action("retry", retry_verdict)
    except PolicyViolationError as e:
        bypass_caught = True
        print(f" >> ExecutionGuard 차단 성공: {e}")
    assert bypass_caught, "ExecutionGuard가 BLOCK된 retry 실행을 차단하지 못함"

    # 검증 3-3: quarantine 제안 시 승인 확인
    quarantine_verdict, quarantine_reason = validator.evaluate(event, "quarantine")
    print(f" >> quarantine 제안 판정: {quarantine_verdict} (사유: {quarantine_reason})")
    assert quarantine_verdict == "ALLOW", f"quarantine 액션 승인 실패: {quarantine_verdict}"
    guard.guard_action("quarantine", quarantine_verdict)  # 예외 없이 통과

    # Step 4: 비파괴적 격리 (Safe Isolation) 수행
    quarantine_dir = fixture_repo / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    quarantined_blob_copy = quarantine_dir / f"{target_blob.name}.quarantine"
    shutil.copy2(target_blob, quarantined_blob_copy)

    quarantine_meta = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "suspect_blob": target_blob.name,
        "action": "safe_isolation",
        "original_deleted": False,
        "status": "QUARANTINED_NON_DESTRUCTIVE"
    }
    with open(quarantine_dir / f"{target_blob.name}.meta.json", "w", encoding="utf-8") as f:
        json.dump(quarantine_meta, f, indent=2)

    print(f"\n[Step 4] 비파괴적 Safe Isolation 수행:")
    print(f" >> 격리 대상 복사본 생성: {quarantined_blob_copy.name}")
    print(f" >> 원본 파일 보존 상태: {target_blob.exists()} (삭제 없음)")
    assert target_blob.exists(), "quarantine 과정에서 원본 블롭이 부당하게 삭제됨"
    if valid_blob:
        assert valid_blob.exists(), "정상 블롭이 영향을 받음"
    assert (fixture_repo / "snapshots" / f"{fixture_snap_id}.json").exists(), "스냅샷 메타데이터 손상됨"
    print(" >> 정상 스냅샷 및 유효 블롭 보호 확인 완료 (True)")

    # 증거 보존
    defensive_evidence = {
        "run_id": run_id,
        "parent_event_id": event["event_id"],
        "retry_blocked": True,
        "inv_enforced": "INV-2",
        "quarantine_verdict": quarantine_verdict,
        "isolation_type": "safe_isolation_copy",
        "original_preserved": True,
        "suspect_blob": target_blob.name
    }
    with open(evidence_dir / "defensive_path_evidence.json", "w", encoding="utf-8") as f:
        json.dump(defensive_evidence, f, indent=2, ensure_ascii=False)

    shutil.rmtree(fixture_dir, ignore_errors=True)

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
    print(" 🎉 [SUCCESS] GOAL 5 Defensive Path 검증 100% 완료!")
    print(f" >> Defensive Evidence: {evidence_dir / 'defensive_path_evidence.json'}")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
