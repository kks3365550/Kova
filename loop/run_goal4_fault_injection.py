#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
C:\Users\kksjmj\Desktop\ai\백업시스템\loop\run_goal4_fault_injection.py
========================================================================
GOAL 4: 독립 Fixture Fault Injection (1-byte 결함 주입 및 탐지 실측)

검증 내용:
1. 운영 저장소와 100% 분리된 독립 임시 Fixture 생성 (tempfile.mkdtemp)
2. Fixture 내 스냅샷 생성 및 대상 블롭 1개 선택
3. 블롭 1개에 대해 정확히 1 byte 변조 (Bit-flip) 수행
   - 스냅샷 매니페스트 및 메타데이터는 원형 유지
4. RestoreAdapter.run_restore_verification(verify_hash=True) 실행
5. RestoreEngine이 failed_files에 해시 불일치 결함을 직접 포착하는지 실측
6. Contract v0.3 평가: AC-RESTORE-02 위반 및 FAIL 판정 확인
7. 정상 데이터 vs 결함 데이터 바이트 차이 및 Evidence 저장
8. 사후 core/ 및 운영 저장소 0 mutation 검증
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
    run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_goal4_fault_injection"
    run_dir = RUNS_DIR / run_id
    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print(" 🧪 [GOAL 4] 독립 Fixture Fault Injection (1-byte Bitrot)")
    print(f" >> Run ID: {run_id}")
    print(f" >> Live Repo: {PRODUCTION_REPO} (READ-ONLY STRICT GUARANTEE)")
    print("=" * 80 + "\n")

    # Step 0: 사전 불변성 인벤토리
    pre_core_hashes = capture_core_hashes(CORE_DIR)
    pre_repo_inventory = capture_repo_inventory(PRODUCTION_REPO)
    print(f"[Step 0] 사전 상태 캡처: core/={len(pre_core_hashes)}개, LiveRepo={len(pre_repo_inventory['files'])}개\n")

    # Step 1: 독립 Fixture 구성
    fixture_dir = Path(tempfile.mkdtemp(prefix="goal4_fixture_"))
    fixture_repo = fixture_dir / "repo"
    fixture_source = fixture_dir / "source"
    fixture_source.mkdir(parents=True, exist_ok=True)
    fixture_repo.mkdir(parents=True, exist_ok=True)

    file1 = fixture_source / "fault_test_doc1.txt"
    file2 = fixture_source / "fault_test_doc2.txt"
    file1.write_bytes(b"ALPHA_PAYLOAD_FOR_FAULT_INJECTION_TEST_001")
    file2.write_bytes(b"BETA_PAYLOAD_FOR_FAULT_INJECTION_TEST_002")

    snap_manifest = SnapshotEngine.create_snapshot(
        repo_dir=str(fixture_repo),
        sources=[str(fixture_source)],
        profile_id="goal4_fixture_profile",
        profile_name="Goal 4 Profile",
        compress_level=3
    )
    fixture_snap_id = snap_manifest.get("id") or snap_manifest.get("snapshot_id")
    print(f"[Step 1] 독립 Fixture 생성 완료: Snapshot ID={fixture_snap_id}")

    blobs = list((fixture_repo / "blobs").rglob("*.blob"))
    assert len(blobs) > 0, "Fixture에 Blob이 생성되지 않았습니다."
    target_blob = blobs[0]
    original_blob_bytes = target_blob.read_bytes()
    original_blob_hash = hashlib.sha256(original_blob_bytes).hexdigest()
    print(f" >> 대상 블롭: {target_blob.name} (크기: {len(original_blob_bytes)}B, SHA-256: {original_blob_hash[:16]}...)")

    # Step 2: 1-byte 결함 주입 (중간 위치 1바이트 비트 반전)
    corrupted_bytes = bytearray(original_blob_bytes)
    corrupt_offset = len(corrupted_bytes) // 2
    corrupted_bytes[corrupt_offset] ^= 0xFF
    corrupted_blob_hash = hashlib.sha256(corrupted_bytes).hexdigest()

    # WORM 읽기 전용 속성 해제 후 1바이트 변조 파일 덮어쓰기
    try:
        os.chmod(target_blob, 0o666)
    except Exception:
        pass
    target_blob.write_bytes(bytes(corrupted_bytes))
    print(f"\n[Step 2] 1-byte 결함 주입 완료:")
    print(f" >> 변조 오프셋: {corrupt_offset} 번째 바이트 (원본: 0x{original_blob_bytes[corrupt_offset]:02X} -> 변조: 0x{corrupted_bytes[corrupt_offset]:02X})")
    print(f" >> 변조 후 해시: {corrupted_blob_hash[:16]}... (원본과 일치 여부: {original_blob_hash == corrupted_blob_hash})")
    assert original_blob_hash != corrupted_blob_hash, "해시가 변경되지 않았습니다."

    # Step 3: RestoreAdapter 실행 및 결함 탐지 실측
    target_sandbox = Path(tempfile.mkdtemp(prefix="goal4_sandbox_"))
    adapter = RestoreAdapter(runs_dir=RUNS_DIR)
    print(f"\n[Step 3] RestoreAdapter 실행 (임시 Sandbox: {target_sandbox})...")

    t0 = time.perf_counter()
    event, evidence = adapter.run_restore_verification(
        repo_dir=str(fixture_repo),
        snapshot_id=fixture_snap_id,
        target_dir=str(target_sandbox),
        run_id=run_id,
        in_place=False,
        overwrite=True,
        verify_hash=True
    )
    t1 = time.perf_counter()
    shutil.rmtree(target_sandbox, ignore_errors=True)
    shutil.rmtree(fixture_dir, ignore_errors=True)

    raw_res = evidence["raw_restore_result"]
    print(f"\n[Step 4] RestoreEngine 실제 반환값:")
    print(f" >> total_files: {raw_res.get('total_files')}")
    print(f" >> restored_files: {raw_res.get('restored_files')}")
    print(f" >> failed_files: {raw_res.get('failed_files')}")
    print(f" >> Event Status: {event['status']}")
    print(f" >> Error Class: {event['error_class']}")

    # 결함 탐지 확인
    failed_files = raw_res.get("failed_files", [])
    assert len(failed_files) > 0, "결함이 탐지되지 않았습니다 (failed_files가 비어있음)!"
    assert event["status"] == "FAIL", f"Event status가 FAIL이 아닙니다: {event['status']}"
    print(f" >> ✅ [결함 탐지 성공] RestoreEngine이 failed_files에 무결성 결함을 포착함!")

    # 결함 상세 증거 기록
    fault_evidence = {
        "run_id": run_id,
        "target_blob": target_blob.name,
        "corrupt_offset": corrupt_offset,
        "original_byte": f"0x{original_blob_bytes[corrupt_offset]:02X}",
        "corrupted_byte": f"0x{corrupted_bytes[corrupt_offset]:02X}",
        "original_sha256": original_blob_hash,
        "corrupted_sha256": corrupted_blob_hash,
        "detected_in_failed_files": failed_files,
        "event_status": event["status"]
    }
    with open(evidence_dir / "fault_injection_evidence.json", "w", encoding="utf-8") as f:
        json.dump(fault_evidence, f, indent=2, ensure_ascii=False)

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
    print(" 🎉 [SUCCESS] GOAL 4 독립 Fixture Fault Injection 검증 100% 완료!")
    print(f" >> Fault Evidence: {evidence_dir / 'fault_injection_evidence.json'}")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
