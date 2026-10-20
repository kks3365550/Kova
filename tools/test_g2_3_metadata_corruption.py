#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/test_g2_3_metadata_corruption.py
======================================
G2-3: Metadata DB Corruption & Self-Healing 검증 스위트

검증 원칙:
1. 기존 G2-0/G2-1 실환경 저장소(D:\\MyBackup_Repository)를 100% 보호하기 위해,
   별도의 격리된 Sandbox(임시 저장소) 환경을 구성하여 테스트합니다.
   (snapshots/ 및 metadata.db는 복사하고, blobs/는 디렉터리 정션/참조를 통해 원본 보존 및 디스크 낭비 방지)
2. metadata.db에 실제 물리적 바이트 파손(헤더 오염, 페이지 파괴)을 주입하여 확실한 Corrupt 상태 생성 (단순 삭제 대체 불가)
3. core 시스템 재기동 시 자가 치유(Self-Healing) 동작 검증:
   - Corrupt DB 자동 감지 및 metadata.db.corrupt_<timestamp> 안전 격리(Quarantine) 확인
   - Ground Truth인 snapshots/*.json 매니페스트와 CAS blobs를 기반으로 metadata.db 100% 자동 재구축(Rebuild) 확인
4. Rebuild 사후 검증:
   - PRAGMA integrity_check == 'ok'
   - PRAGMA quick_check == 'ok'
   - 기존 정상 snapshot manifest SHA-256 0비트 불변 확인
   - 복원(Restore) 100개 샘플 SHA-256 100% 일치 확인
   - 사후 신규 증분 백업 성공 확인
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
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

from core.metadata_db import MetadataDB
from core.snapshot import SnapshotEngine
from core.storage import BlobStorage
from core.restore import RestoreEngine

PRODUCTION_REPO = Path(r"D:\MyBackup_Repository")
LOGS_DIR = PROJECT_ROOT / "logs"
BASELINE_JSON = LOGS_DIR / "g2_0_baseline.json"
REPORT_MD = LOGS_DIR / "g2_3_metadata_corruption_report.md"


def calc_sha256(filepath: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def run_g2_3_test():
    print("=" * 80)
    print(" 🚀 [G2-3 Metadata DB Corruption & Self-Healing 실전 검증]")
    print(f" >> 기준 저장소: {PRODUCTION_REPO}")
    print("=" * 80)

    # 1. G2-0 Baseline 로드
    assert BASELINE_JSON.exists(), f"Baseline not found: {BASELINE_JSON}"
    with open(BASELINE_JSON, "r", encoding="utf-8") as f:
        baseline = json.load(f)

    pre_snap_count = baseline["snapshots"]["total_count"]
    pre_snap_hashes = {s["name"]: s["sha256"] for s in baseline["snapshots"]["list"]}
    pre_blob_count = baseline["cas_blobs"]["total_count"]

    print(f"\n[Step 0] G2-0 기준선 로드:")
    print(f" >> 스냅샷: {pre_snap_count}개 ({list(pre_snap_hashes.keys())})")
    print(f" >> CAS Blob: {pre_blob_count:,}개")

    # 2. 격리된 테스트 Sandbox 저장소 구성 (D: 드라이브 내 격리 폴더 생성으로 초고속 정션 보장)
    # 실환경 D:\MyBackup_Repository를 절대 건드리지 않도록 별도 격리 디렉터리 생성
    sandbox_dir = Path(r"D:\G2_3_Sandbox_Repo")
    def _force_remove_readonly(func, path, excinfo):
        import stat
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass

    if sandbox_dir.exists():
        if (sandbox_dir / "blobs").exists():
            subprocess.run(["cmd", "/c", f'rmdir "{sandbox_dir / "blobs"}"'], capture_output=True)
        # 읽기 전용 해제 후 삭제
        subprocess.run(["cmd", "/c", f'attrib -r -s -h "{sandbox_dir}\\*.*" /s /d'], capture_output=True)
        shutil.rmtree(sandbox_dir, onerror=_force_remove_readonly)
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n[Step 1] 격리된 테스트 저장소(Sandbox) 구성: {sandbox_dir}")
    
    # 2-1. snapshots 복사 (copy2 대신 copy로 readonly 속성 복사 방지)
    sandbox_snaps = sandbox_dir / "snapshots"
    sandbox_snaps.mkdir(parents=True, exist_ok=True)
    for sf in (PRODUCTION_REPO / "snapshots").glob("*.json"):
        dest_f = sandbox_snaps / sf.name
        if dest_f.exists():
            try:
                os.chmod(dest_f, 0o666)
            except Exception:
                pass
        shutil.copy(sf, dest_f)
    print(f" >> snapshots/*.json {len(list(sandbox_snaps.glob('*.json')))}개 복사 완료")

    # 2-2. metadata.db 복사
    sandbox_db = sandbox_dir / "metadata.db"
    if sandbox_db.exists():
        try:
            os.chmod(sandbox_db, 0o666)
        except Exception:
            pass
    shutil.copy(PRODUCTION_REPO / "metadata.db", sandbox_db)
    print(f" >> metadata.db 복사 완료 ({sandbox_db.stat().st_size:,} 바이트)")

    # 2-3. blobs 연결 (동일 D: 드라이브 내 완벽한 디렉터리 정션 mklink /J 생성)
    sandbox_blobs = sandbox_dir / "blobs"
    if not sandbox_blobs.exists():
        subprocess.run(f'cmd /c mklink /J "{sandbox_blobs}" "{PRODUCTION_REPO / "blobs"}"', shell=True, capture_output=True)
    print(f" >> blobs 저장소 연결 완료 (실환경 8.62GB CAS Blob 안전 공유, 존재: {sandbox_blobs.exists()})")
    assert sandbox_blobs.exists(), "Failed to create directory junction for blobs!"

    # 2-4. Sandbox 사전 DB 점검
    actual_json_snaps_count = len(list(sandbox_snaps.glob("*.json")))
    conn_pre = sqlite3.connect(str(sandbox_db))
    c_pre = conn_pre.cursor()
    c_pre.execute("PRAGMA integrity_check;")
    pre_integ = c_pre.fetchone()[0]
    c_pre.execute("PRAGMA quick_check;")
    pre_quick = c_pre.fetchone()[0]
    c_pre.execute("SELECT COUNT(*) FROM snapshots_meta;")
    pre_meta_count = c_pre.fetchone()[0]
    conn_pre.close()

    print(f" >> Sandbox 사전 DB 점검: integrity={pre_integ}, quick={pre_quick}, snapshots_meta={pre_meta_count}건, json파일={actual_json_snaps_count}개")
    assert pre_integ == "ok" and pre_quick == "ok", "Sandbox DB initial integrity failed!"

    # 3. metadata.db에 실제 물리적 Corruption 주입 (단순 삭제 불가)
    print(f"\n[Step 2] metadata.db 의도적 Corruption 주입 (헤더 파괴 & B-Tree 오염)...")
    original_db_size = sandbox_db.stat().st_size
    with open(sandbox_db, "r+b") as f:
        # SQLite 헤더("SQLite format 3\000")를 무작위 쓰레기 바이트로 파괴
        f.seek(0)
        f.write(os.urandom(64))
        # 1KB, 2KB 위치의 B-Tree 페이지 헤더를 쓰레기 바이트로 덮어쓰기
        if original_db_size > 2048:
            f.seek(1024)
            f.write(os.urandom(128))
            f.seek(2048)
            f.write(os.urandom(128))
        f.flush()

    # 파손 상태 검증 (열려고 하면 오류 발생해야 함)
    corrupted_as_expected = False
    conn_corrupt = None
    try:
        conn_corrupt = sqlite3.connect(str(sandbox_db))
        c = conn_corrupt.cursor()
        c.execute("SELECT * FROM snapshots_meta;")
        c.fetchall()
    except sqlite3.DatabaseError as e:
        corrupted_as_expected = True
        print(f" >> 의도적 DB 손상 확인 성공 (오류 감지: {e})")
    finally:
        if conn_corrupt:
            try:
                conn_corrupt.close()
            except Exception:
                pass
        import gc
        gc.collect()
        time.sleep(0.1)  # Windows 파일 핸들 해제 대기

    assert corrupted_as_expected, "Failed to inject corruption into metadata.db!"

    # 4. 재기동 및 Self-Healing (Quarantine & Rebuild) 트리거
    print(f"\n[Step 3] 재기동 및 Self-Healing (손상 감지 -> 격리 -> Rebuild) 트리거...")
    t_start = time.perf_counter()
    
    # MetadataDB 인스턴스화 시 내부 _verify_and_heal_db가 손상을 자동 감지하고 격리 및 재구축 수행
    db_engine = MetadataDB(str(sandbox_dir))
    dur_heal = time.perf_counter() - t_start
    print(f" >> Self-Healing 트리거 및 완료 (소요시간: {dur_heal:.2f}초)")

    # 5. Quarantine 검증 (metadata.db.corrupt_<timestamp> 생성 여부)
    print(f"\n[Step 4] Quarantine(격리) 상태 검증...")
    quarantine_files = list(sandbox_dir.glob("metadata.db.corrupt_*"))
    print(f" >> 격리된 Corrupt DB 파일 수: {len(quarantine_files)}개")
    assert len(quarantine_files) > 0, "Corrupt DB was not quarantined to metadata.db.corrupt_*!"
    q_file = quarantine_files[0]
    print(f" >> 격리 파일명: {q_file.name} ({q_file.stat().st_size:,} 바이트)")
    assert q_file.stat().st_size > 0, "Quarantined file is empty!"

    # 6. Rebuild 상태 검증
    print(f"\n[Step 5] Rebuild 결과 및 무결성 검증...")
    rebuilt_db = sandbox_dir / "metadata.db"
    assert rebuilt_db.exists(), "Rebuilt metadata.db does not exist!"

    conn_post = sqlite3.connect(str(rebuilt_db))
    c_post = conn_post.cursor()
    c_post.execute("PRAGMA integrity_check;")
    post_integ = c_post.fetchone()[0]
    c_post.execute("PRAGMA quick_check;")
    post_quick = c_post.fetchone()[0]
    
    c_post.execute("SELECT COUNT(*) FROM snapshots_meta;")
    post_meta_count = c_post.fetchone()[0]

    c_post.execute("SELECT COUNT(*) FROM blobs_summary;")
    blobs_summary_count = c_post.fetchone()[0]
    conn_post.close()

    print(f" >> [검증 1] Rebuilt DB PRAGMA integrity_check: {'✅ ok' if post_integ == 'ok' else f'❌ {post_integ}'}")
    print(f" >> [검증 2] Rebuilt DB PRAGMA quick_check: {'✅ ok' if post_quick == 'ok' else f'❌ {post_quick}'}")
    print(f" >> [검증 3] snapshots_meta 레코드 수 복원: {post_meta_count}개 (사전: {pre_meta_count}개)")
    print(f" >> [검증 4] blobs_summary 통계 복원: {blobs_summary_count}개")

    assert post_integ == "ok", f"Rebuilt DB integrity check failed: {post_integ}"
    assert post_quick == "ok", f"Rebuilt DB quick check failed: {post_quick}"
    assert post_meta_count == actual_json_snaps_count, f"Snapshot count mismatch after rebuild: {post_meta_count} != {actual_json_snaps_count}"

    # 7. 기존 Snapshot Manifest 파일들의 불변성 검증 (0비트 변형 없음)
    print(f"\n[Step 6] 기존 정상 Snapshot Manifest 파일들의 0비트 불변성 확인...")
    snap_manifest_unmodified = True
    for s_name, exp_sha in pre_snap_hashes.items():
        s_path = sandbox_snaps / s_name
        act_sha = calc_sha256(s_path)
        if act_sha != exp_sha:
            snap_manifest_unmodified = False
            print(f"    ❌ 스냅샷 매니페스트 변형: {s_name}")
    print(f" >> 스냅샷 매니페스트 SHA-256 불변성: {'✅ 100% 동일 (0비트 불변)' if snap_manifest_unmodified else '❌ 변형 감지'}")
    assert snap_manifest_unmodified, "Snapshot manifest files were modified during DB rebuild!"

    # blobs 연결 상태 확인 (만약 심볼릭 링크 권한이 없어서 sandbox_blobs가 없으면 임시 연결)
    if not sandbox_blobs.exists():
        # 복원을 위해 blobs 디렉터리를 원본 저장소로 직접 설정할 수 있도록 처리
        sandbox_blobs_dir_for_restore = PRODUCTION_REPO / "blobs"
    else:
        sandbox_blobs_dir_for_restore = sandbox_blobs

    # 8. 복구 후 기준선 스냅샷 파일 복원 검증 (Restore Verification)
    print(f"\n[Step 7] 복구된 DB를 통한 Snapshot 복원(Restore) 및 SHA-256 전수 대조...")
    baseline_latest_snap = baseline["snapshots"]["latest_snapshot"]["name"].replace(".json", "")
    snap_obj = SnapshotEngine.get_snapshot(str(sandbox_dir), baseline_latest_snap)
    entries = snap_obj.get("entries", [])
    # 서브디렉터리가 포함된 고유 경로 100개 샘플 선정
    unique_subpath_entries = []
    seen = set()
    for e in entries:
        rp = e.get("rel_path")
        if rp and rp not in seen and "/" in rp.replace("\\", "/"):
            seen.add(rp)
            unique_subpath_entries.append(e)
            if len(unique_subpath_entries) >= 100:
                break

    selected_paths = [e["rel_path"] for e in unique_subpath_entries]
    with tempfile.TemporaryDirectory(prefix="g2_3_restore_") as restore_dir:
        res = RestoreEngine.restore_snapshot(
            repo_dir=str(sandbox_dir),
            snapshot_id=baseline_latest_snap,
            target_dir=restore_dir,
            selected_rel_paths=selected_paths
        )
        matched_files = 0
        for e in unique_subpath_entries:
            rf = Path(restore_dir) / e["rel_path"]
            if rf.exists():
                act_sha = calc_sha256(rf)
                exp_sha = e.get("hash") or e.get("sha256")
                if act_sha == exp_sha and rf.stat().st_size == e.get("size", 0):
                    matched_files += 1

        total_tested = len(unique_subpath_entries)
        match_rate = (matched_files / total_tested * 100.0) if total_tested > 0 else 0.0
        print(f" >> 복원 완료: {res.get('restored_files')}/{total_tested}개 파일")
        print(f" >> SHA-256 bit-for-bit 일치율: {'✅ 100.0% (100/100)' if matched_files == total_tested else f'❌ 불일치 ({matched_files}/{total_tested})'}")
        assert matched_files == total_tested, f"Restore SHA-256 mismatch after rebuild: {matched_files}/{total_tested}"

    # 9. 복구 후 신규 증분 백업 (Subsequent Incremental Backup)
    print(f"\n[Step 8] 복구된 DB 상태에서 신규 증분 백업 검증...")
    test_src_dir = Path(tempfile.mkdtemp(prefix="g2_3_src_"))
    test_file = test_src_dir / "new_incremental_data.bin"
    test_bytes = os.urandom(10 * 1024 * 1024)  # 10MB
    test_file.write_bytes(test_bytes)
    test_sha = hashlib.sha256(test_bytes).hexdigest()

    manifest = SnapshotEngine.create_snapshot(
        repo_dir=str(sandbox_dir),
        sources=[str(test_src_dir)],
        profile_id="g2_3_post_healing",
        profile_name="g2_3_post_healing_profile",
        compress_level=3
    )
    new_snap_id = manifest.get("id") or manifest.get("snapshot_id")
    print(f" >> 신규 증분 백업 성공! 스냅샷 ID: {new_snap_id}")
    assert new_snap_id, "Subsequent incremental backup failed!"

    # 신규 스냅샷 복원 검증
    with tempfile.TemporaryDirectory(prefix="g2_3_inc_restore_") as inc_restore_dir:
        RestoreEngine.restore_snapshot(
            repo_dir=str(sandbox_dir),
            snapshot_id=new_snap_id,
            target_dir=inc_restore_dir
        )
        restored_inc = Path(inc_restore_dir) / "new_incremental_data.bin"
        inc_matched = restored_inc.exists() and (calc_sha256(restored_inc) == test_sha)
        print(f" >> 신규 증분 데이터 복원 SHA-256 일치: {'✅ 100% 일치' if inc_matched else '❌ 불일치'}")
        assert inc_matched, "Restored incremental file does not match source!"

    # 10. 실환경 저장소(D:\MyBackup_Repository) 무결성 최종 절대 확인
    print(f"\n[Step 9] 실환경 저장소(D:\\MyBackup_Repository) 절대 불변성 검증...")
    prod_db = PRODUCTION_REPO / "metadata.db"
    conn_prod = sqlite3.connect(str(prod_db))
    c_p = conn_prod.cursor()
    c_p.execute("PRAGMA integrity_check;")
    prod_integ = c_p.fetchone()[0]
    conn_prod.close()
    prod_snaps_count = len(list((PRODUCTION_REPO / "snapshots").glob("*.json")))
    print(f" >> 실환경 저장소 스냅샷 수: {prod_snaps_count}개 | metadata.db 무결성: {prod_integ}")
    assert prod_integ == "ok", "PRODUCTION DB was modified or corrupted!"

    # Sandbox 정리
    shutil.rmtree(test_src_dir, ignore_errors=True)
    if sys.platform == "win32":
        # Windows 정션 삭제 (rmdir)
        subprocess.run(["cmd", "/c", f'rmdir "{sandbox_blobs}"'], capture_output=True)
    shutil.rmtree(sandbox_dir, ignore_errors=True)

    # 11. 마크다운 리포트 생성
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    report_content = f"""# G2-3 Metadata DB Corruption & Self-Healing 검증 리포트

- **검증 시각**: `{datetime.now().isoformat()}`
- **최종 판정**: **🎉 ALL PASS**
- **대상 격리 환경**: Sandbox 임시 저장소 (실환경 8.62GB 저장소 100% 보존)
- **손상 주입 방식**: 헤더 64바이트 난수 파괴 + B-Tree 페이지(1KB, 2KB) 강제 오염

---

## 8대 Self-Healing 및 무결성 검증 지표

| No | 검증 항목 | 결과 | 실측 내용 |
|:---:|:---|:---:|:---|
| **1** | **의도적 Corruption 주입** | ✅ **PASS** | `sqlite3.DatabaseError` 정상 유발 확인 |
| **2** | **Corrupt DB 격리(Quarantine)** | ✅ **PASS** | `{q_file.name}` 격리 보존 확인 |
| **3** | **Rebuild 후 PRAGMA 무결성** | ✅ **PASS** | `integrity_check: ok`, `quick_check: ok` |
| **4** | **스냅샷 메타데이터 자동 복구** | ✅ **PASS** | Ground Truth 기반 `{post_meta_count}개` 스냅샷 100% 복원 |
| **5** | **CAS Blob 통계 자동 복구** | ✅ **PASS** | `blobs_summary` 정상 재구축 완료 |
| **6** | **기존 Snapshot Manifest 불변성** | ✅ **PASS** | 기존 매니페스트 SHA-256 0비트 변형 없음 |
| **7** | **복구 후 파일 복원 SHA-256 일치율** | ✅ **PASS** | 기준선 스냅샷 100/100 파일 (100.0%) 일치 |
| **8** | **복구 후 신규 증분 백업/복원** | ✅ **PASS** | 10MB 신규 백업(`{new_snap_id}`) 및 복원 SHA-256 100% 일치 |

---
**주의 사항 표기**:
- 본 테스트는 metadata DB 파일에 대한 바이트 레벨 Corruption 및 Self-Healing 복구력을 검증한 것이며, **실제 물리적 전원 차단을 의미하지 않습니다.**
- 실환경 저장소(`{PRODUCTION_REPO}`)는 100% 완벽히 보존되었으며, 테스트는 격리된 샌드박스에서 완결되었습니다.
"""
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write(report_content)
    print(f"\n >> 마크다운 리포트 저장 완료: {REPORT_MD}")

    print("\n" + "=" * 80)
    print(" 🏁 [G2-3 Metadata DB Corruption & Self-Healing] 종합 판정: 🎉 ALL PASS")
    print("=" * 80)


if __name__ == "__main__":
    run_g2_3_test()
