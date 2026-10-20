#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/record_g2_baseline.py
===========================
G2 Phase (Failure Durability / Self-Healing) — G2-0 BASELINE 측정 스크립트

현재 운영 중인 백업 시스템과 저장소(D:\\MyBackup_Repository)의 정상 기준선을 정밀 측정하여
logs/g2_0_baseline.json 및 logs/g2_0_baseline_report.md 에 영구 기록합니다.

측정 항목:
  1. 저장소(D:\\MyBackup_Repository) 전체 물리적 상태 (총 용량, 파일 수, 디렉터리 트리)
  2. Snapshot 목록, 개수, 최신 매니페스트 JSON 구조
  3. CAS blob 개수 및 총 크기
  4. metadata.db 무결성 (PRAGMA integrity_check, quick_check, 테이블/레코드 수)
  5. 최신 Snapshot 기준 임시 복원(Restore) 테스트 및 SHA-256 원본 대조 일치율
  6. logs/g2_0_baseline.json & logs/g2_0_baseline_report.md 저장

사용법:
  python tools/record_g2_baseline.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 콘솔 UTF-8 설정
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 백업 코어 모듈 import
from core.restore import RestoreEngine
from core.snapshot import SnapshotEngine
from core.storage import BlobStorage

DEFAULT_REPO_DIR = Path(r"D:\MyBackup_Repository")
LOGS_DIR = PROJECT_ROOT / "logs"
BASELINE_JSON = LOGS_DIR / "g2_0_baseline.json"
BASELINE_MD = LOGS_DIR / "g2_0_baseline_report.md"


def calc_sha256(filepath: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def human_size(size_bytes: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


def measure_baseline(repo_dir: Path = DEFAULT_REPO_DIR) -> Dict[str, Any]:
    print("=" * 80)
    print(" 🚀 [G2-0 BASELINE 측정 시작: 정상 상태 기준선 확보]")
    print(f" >> 타겟 저장소: {repo_dir}")
    print("=" * 80)

    assert repo_dir.exists(), f"Repository directory does not exist: {repo_dir}"

    baseline = {
        "timestamp_iso": datetime.now().isoformat(),
        "repo_dir": str(repo_dir),
        "snapshots": {},
        "cas_blobs": {},
        "metadata_db": {},
        "restore_verification": {},
        "summary": {},
    }

    # 1. Snapshot 매니페스트 전수 점검
    print("\n[Step 1] Snapshot 매니페스트 점검...")
    snapshots_dir = repo_dir / "snapshots"
    snap_files = sorted(snapshots_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    baseline["snapshots"]["total_count"] = len(snap_files)
    baseline["snapshots"]["list"] = []

    latest_snap_path: Optional[Path] = snap_files[0] if snap_files else None
    latest_snap_data: Optional[Dict[str, Any]] = None

    for sf in snap_files:
        st = sf.stat()
        sha = calc_sha256(sf)
        info = {
            "name": sf.name,
            "path": str(sf),
            "size_bytes": st.st_size,
            "mtime_iso": datetime.fromtimestamp(st.st_mtime).isoformat(),
            "sha256": sha,
        }
        baseline["snapshots"]["list"].append(info)

    if latest_snap_path:
        with open(latest_snap_path, "r", encoding="utf-8") as f:
            latest_snap_data = json.load(f)
        entries_list = latest_snap_data.get("entries") or latest_snap_data.get("files") or []
        tot_bytes = latest_snap_data.get("total_bytes") or latest_snap_data.get("total_size") or sum(e.get("size", 0) for e in entries_list)
        baseline["snapshots"]["latest_snapshot"] = {
            "name": latest_snap_path.name,
            "sha256": calc_sha256(latest_snap_path),
            "profile_name": latest_snap_data.get("profile_name", ""),
            "timestamp": latest_snap_data.get("timestamp", ""),
            "file_count": len(entries_list),
            "total_bytes": tot_bytes,
        }
        print(f" >> 스냅샷 총 개수: {len(snap_files)}개 | 최신: {latest_snap_path.name} ({len(entries_list)}개 파일, {human_size(tot_bytes)})")
    else:
        print(" ⚠️ 등록된 스냅샷이 없습니다!")

    # 2. CAS Blob 점검
    print("\n[Step 2] CAS Blob 저장소 점검...")
    blobs_dir = repo_dir / "blobs"
    total_blobs = 0
    total_blob_bytes = 0
    if blobs_dir.exists():
        for bf in blobs_dir.rglob("*"):
            if bf.is_file() and not bf.name.endswith(".tmp"):
                total_blobs += 1
                total_blob_bytes += bf.stat().st_size

    baseline["cas_blobs"]["total_count"] = total_blobs
    baseline["cas_blobs"]["total_bytes"] = total_blob_bytes
    baseline["cas_blobs"]["total_size_human"] = human_size(total_blob_bytes)
    print(f" >> CAS Blob 총 개수: {total_blobs}개 | 총 용량: {human_size(total_blob_bytes)}")

    # 3. metadata.db 점검 (PRAGMA integrity_check, quick_check)
    print("\n[Step 3] metadata.db 무결성 및 테이블 레코드 점검...")
    db_path = repo_dir / "metadata.db"
    assert db_path.exists(), f"metadata.db not found at {db_path}"

    db_size = db_path.stat().st_size
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # integrity_check
    cursor.execute("PRAGMA integrity_check;")
    integrity_rows = [r[0] for r in cursor.fetchall()]
    integrity_status = "ok" if integrity_rows == ["ok"] else "; ".join(integrity_rows)

    # quick_check
    cursor.execute("PRAGMA quick_check;")
    quick_rows = [r[0] for r in cursor.fetchall()]
    quick_status = "ok" if quick_rows == ["ok"] else "; ".join(quick_rows)

    # 테이블 통계
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
    tables = [r[0] for r in cursor.fetchall()]
    table_stats = {}
    total_db_records = 0
    for tbl in tables:
        cursor.execute(f"SELECT COUNT(*) FROM [{tbl}];")
        cnt = cursor.fetchone()[0]
        table_stats[tbl] = cnt
        total_db_records += cnt

    conn.close()

    baseline["metadata_db"] = {
        "path": str(db_path),
        "size_bytes": db_size,
        "size_human": human_size(db_size),
        "integrity_check": integrity_status,
        "quick_check": quick_status,
        "tables": table_stats,
        "total_records": total_db_records,
    }
    print(f" >> metadata.db 크기: {human_size(db_size)}")
    print(f" >> PRAGMA integrity_check: {integrity_status} | quick_check: {quick_status}")
    print(f" >> 테이블 현황: {table_stats} (총 {total_db_records}개 레코드)")

    # 4. 최신 Snapshot 기준 복원(Restore) 테스트 및 SHA-256 원본 전수 대조 (고유 파일 100개 샘플 정밀 검증)
    print("\n[Step 4] 최신 Snapshot 실제 복원 및 SHA-256 전수 대조 테스트 (충돌 없는 고유 샘플 100개)...")
    if latest_snap_path:
        snap_id = latest_snap_path.stem
        snap_obj = SnapshotEngine.get_snapshot(str(repo_dir), snap_id)
        if snap_obj:
            entries = snap_obj.get("entries", [])
            # 루트 파일명 충돌(build_exe.bat, README.md 등)을 피하고 고유한 서브디렉터리 파일 100개 선정
            unique_entries = []
            seen_rel_paths = set()
            for e in entries:
                rp = e.get("rel_path")
                if not rp or rp in seen_rel_paths:
                    continue
                # 서브디렉터리가 포함된 파일(예: core/..., web/...)을 우선 선정하여 소스간 충돌 원천 방지
                if "/" in rp.replace("\\", "/"):
                    seen_rel_paths.add(rp)
                    unique_entries.append(e)
                    if len(unique_entries) >= 100:
                        break

            sample_size = len(unique_entries)
            sample_entries = unique_entries
            selected_paths = [e.get("rel_path") for e in sample_entries]

            with tempfile.TemporaryDirectory(prefix="g2_baseline_restore_") as tmpdir:
                restore_target_dir = Path(tmpdir)
                t0 = time.perf_counter()
                restore_res = RestoreEngine.restore_snapshot(
                    repo_dir=str(repo_dir),
                    snapshot_id=snap_id,
                    target_dir=str(restore_target_dir),
                    selected_rel_paths=selected_paths
                )
                dur_sec = time.perf_counter() - t0

                matched_count = 0
                mismatched_count = 0
                missing_count = 0
                mismatched_items = []

                for ent in sample_entries:
                    rel_path = ent.get("rel_path")
                    expected_sha = ent.get("hash") or ent.get("sha256")
                    expected_size = ent.get("size", 0)

                    # 경로 정규화 (절대 경로 또는 드라이브 문자 제거)
                    clean_rel = rel_path.replace("\\", "/")
                    if ":" in clean_rel:
                        clean_rel = clean_rel.split(":", 1)[-1]
                    clean_rel = clean_rel.lstrip("/")

                    restored_file = restore_target_dir / clean_rel
                    # 만약 없으면 원본 rel_path로도 검색
                    if not restored_file.exists():
                        restored_file = restore_target_dir / rel_path

                    if not restored_file.exists():
                        missing_count += 1
                        mismatched_items.append({
                            "rel_path": rel_path,
                            "reason": "MISSING",
                            "expected_sha": expected_sha,
                            "expected_size": expected_size,
                            "target_searched": str(restored_file)
                        })
                        continue

                    act_size = restored_file.stat().st_size
                    act_sha = calc_sha256(restored_file)
                    if act_sha == expected_sha and act_size == expected_size:
                        matched_count += 1
                    else:
                        mismatched_count += 1
                        mismatched_items.append({
                            "rel_path": rel_path,
                            "reason": "HASH_OR_SIZE_MISMATCH",
                            "expected_sha": expected_sha,
                            "act_sha": act_sha,
                            "expected_size": expected_size,
                            "act_size": act_size,
                        })

                total_expected = len(sample_entries)
                match_rate = (matched_count / total_expected * 100.0) if total_expected > 0 else 0.0
                restored_count = restore_res.get("restored_count", matched_count)

                baseline["restore_verification"] = {
                    "tested_snapshot_id": snap_id,
                    "sample_size": sample_size,
                    "total_snapshot_entries": len(entries),
                    "restore_duration_sec": round(dur_sec, 2),
                    "total_expected_files": total_expected,
                    "restored_count": restored_count,
                    "sha256_matched_count": matched_count,
                    "mismatched_count": mismatched_count,
                    "missing_count": missing_count,
                    "match_rate_percent": round(match_rate, 2),
                    "is_perfect_match": (matched_count == total_expected and total_expected > 0),
                    "mismatched_items": mismatched_items,
                }
                print(f" >> 복원 소요시간: {dur_sec:.2f}초 | 복원 파일 수: {restored_count}/{total_expected}")
                print(f" >> SHA-256 전수 일치율: {match_rate:.2f}% ({matched_count}/{total_expected} 일치, 불일치 {mismatched_count}, 누락 {missing_count})")
                if mismatched_items:
                    print(" >> [불일치 항목 상세]")
                    for mi in mismatched_items[:10]:
                        print(f"    * {mi['rel_path']}: {mi['reason']} (Exp Size: {mi.get('expected_size')} vs Act: {mi.get('act_size', 'N/A')})")
        else:
            baseline["restore_verification"] = {
                "error": f"Failed to load snapshot {snap_id}",
                "is_perfect_match": False,
            }
    else:
        baseline["restore_verification"] = {
            "error": "No snapshots available for restore test",
            "is_perfect_match": False,
        }

    # 종합 판정
    is_db_ok = (integrity_status == "ok" and quick_status == "ok")
    is_restore_ok = baseline["restore_verification"].get("is_perfect_match", False)
    baseline["summary"] = {
        "status": "PASS" if (is_db_ok and is_restore_ok) else "FAIL",
        "db_healthy": is_db_ok,
        "restore_verified": is_restore_ok,
    }

    # 5. logs/g2_0_baseline.json 저장
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(BASELINE_JSON, "w", encoding="utf-8") as f:
        json.dump(baseline, f, indent=2, ensure_ascii=False)
    print(f"\n >> JSON 기준선 저장 완료: {BASELINE_JSON}")

    # 6. logs/g2_0_baseline_report.md 저장
    md_content = f"""# G2-0 저장소 및 메타데이터 정상 기준선(Baseline) 리포트

- **측정 시각**: `{baseline['timestamp_iso']}`
- **타겟 저장소**: `{repo_dir}`
- **종합 상태**: **{baseline['summary']['status']}** (DB 건전성: {is_db_ok}, 복원 검증: {is_restore_ok})

---

## 1. Snapshot 현황
- **총 Snapshot 개수**: `{baseline['snapshots']['total_count']}개`
- **최신 Snapshot**: `{baseline['snapshots'].get('latest_snapshot', {}).get('name', 'N/A')}`
  - 파일 수: `{baseline['snapshots'].get('latest_snapshot', {}).get('file_count', 0)}개`
  - 총 용량: `{human_size(baseline['snapshots'].get('latest_snapshot', {}).get('total_bytes', 0))}`
  - SHA-256: `{baseline['snapshots'].get('latest_snapshot', {}).get('sha256', 'N/A')}`

## 2. CAS Blob 저장소 현황
- **총 CAS Blob 수**: `{baseline['cas_blobs']['total_count']}개`
- **총 Blob 용량**: `{baseline['cas_blobs']['total_size_human']}` (`{baseline['cas_blobs']['total_bytes']:,} 바이트`)

## 3. metadata.db 무결성
- **DB 크기**: `{baseline['metadata_db']['size_human']}`
- **PRAGMA integrity_check**: `{baseline['metadata_db']['integrity_check']}`
- **PRAGMA quick_check**: `{baseline['metadata_db']['quick_check']}`
- **테이블별 레코드 수**:
"""
    for tbl, cnt in baseline['metadata_db']['tables'].items():
        md_content += f"  - `{tbl}`: {cnt:,}건\n"

    md_content += f"""
## 4. 최신 Snapshot 복원(Restore) & SHA-256 대조 검증
- **테스트 Snapshot**: `{baseline['restore_verification'].get('tested_snapshot_id', 'N/A')}`
- **복원 소요 시간**: `{baseline['restore_verification'].get('restore_duration_sec', 0)}초`
- **검증 파일 수**: `{baseline['restore_verification'].get('total_expected_files', 0)}개`
- **SHA-256 일치 수**: `{baseline['restore_verification'].get('sha256_matched_count', 0)}개`
- **불일치 / 누락**: `{baseline['restore_verification'].get('mismatched_count', 0)}개 / {baseline['restore_verification'].get('missing_count', 0)}개`
- **일치율**: **`{baseline['restore_verification'].get('match_rate_percent', 0)}%`** (완전 일치: {is_restore_ok})

---
이 리포트는 이후 진행될 G2-1(Interrupted Write), G2-2(Process Kill), G2-3(DB Corruption), G2-4(Power Loss Simulation), G2-5(Concurrency/Lock) 테스트의 절대적 비교 기준선으로 사용됩니다.
"""
    with open(BASELINE_MD, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f" >> 마크다운 리포트 저장 완료: {BASELINE_MD}")

    print("\n" + "=" * 80)
    print(f" 🏁 [G2-0 BASELINE 측정 완료] 종합 판정: {'🎉 ALL GREEN (PASS)' if baseline['summary']['status'] == 'PASS' else '❌ FAIL'}")
    print("=" * 80)

    return baseline


if __name__ == "__main__":
    measure_baseline()
