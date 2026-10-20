#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/test_g3_2_selected_restore.py
====================================
G3-2: Granular Selected Paths Restore (정상 입력 부분 복구 기능 독립 검증 스위트)

검증 원칙:
1. "통과를 위한 테스트"가 아닌 "실제 요구사항을 엄격하게 검증하는 독립 스위트" 구현
2. 8대 핵심 시나리오 전수 검증:
   - Case 1: 단일 파일 선택 복원
   - Case 2: 복수 파일 선택 복원
   - Case 3: 디렉터리 subtree 접두사 복원 (경계값 접두사 docs vs docs_backup 검증)
   - Case 4: 5단계 이상 깊은 디렉터리 경로 복원
   - Case 5: Windows 대소문자 무시 (Case-Insensitive) 매칭 복원
   - Case 6: 존재하지 않는 경로 선택 시 graceful skip (0건 복원)
   - Case 7: selected_rel_paths = None 시맨틱 (스냅샷 전체 복원)
   - Case 8: selected_rel_paths = [] 시맨틱 (요구사항: 0건 복원 / 현재 코드 동작 실측 및 Class B 판정)
   - Case 9: 멀티소스 동일 rel_path 선택 복원 시 거동 실측
3. 실환경 격리 Sandbox (D:\\G3_2_Sandbox_Repo) 사용으로 프로덕션 저장소 100% 안전 보존
4. 코어 코드(core/) 수정 없이 v2.9.12 상태에서 결함 여부를 객관적 증거로 적출
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.storage import BlobStorage
from core.snapshot import SnapshotEngine
from core.restore import RestoreEngine

PRODUCTION_REPO = Path(r"D:\MyBackup_Repository")
SANDBOX_REPO = Path(r"D:\G3_2_Sandbox_Repo")
LOGS_DIR = PROJECT_ROOT / "logs"
REPORT_MD = LOGS_DIR / "g3_2_selected_restore_report.md"


def calc_sha256(filepath: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def setup_sandbox() -> Path:
    def _force_remove_readonly(func, path, excinfo):
        import stat
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass

    if SANDBOX_REPO.exists():
        if (SANDBOX_REPO / "blobs").exists():
            subprocess.run(["cmd", "/c", f'rmdir "{SANDBOX_REPO / "blobs"}"'], capture_output=True)
        subprocess.run(["cmd", "/c", f'attrib -r -s -h "{SANDBOX_REPO}\\*.*" /s /d'], capture_output=True)
        shutil.rmtree(SANDBOX_REPO, onerror=_force_remove_readonly)
    SANDBOX_REPO.mkdir(parents=True, exist_ok=True)

    # 1. snapshots 디렉토리 생성
    sandbox_snaps = SANDBOX_REPO / "snapshots"
    sandbox_snaps.mkdir(parents=True, exist_ok=True)

    # 2. metadata.db 복사
    sandbox_db = SANDBOX_REPO / "metadata.db"
    if (PRODUCTION_REPO / "metadata.db").exists():
        shutil.copy(PRODUCTION_REPO / "metadata.db", sandbox_db)

    # 3. blobs 정션 연결 (mklink /J)
    sandbox_blobs = SANDBOX_REPO / "blobs"
    if not sandbox_blobs.exists():
        subprocess.run(f'cmd /c mklink /J "{sandbox_blobs}" "{PRODUCTION_REPO / "blobs"}"', shell=True, capture_output=True)
    assert sandbox_blobs.exists(), "Failed to create directory junction for blobs"

    return SANDBOX_REPO


def cleanup_sandbox():
    if sys.platform == "win32" and (SANDBOX_REPO / "blobs").exists():
        subprocess.run(["cmd", "/c", f'rmdir "{SANDBOX_REPO / "blobs"}"'], capture_output=True)
    def _force_remove(func, path, excinfo):
        import stat
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass
    shutil.rmtree(SANDBOX_REPO, onerror=_force_remove)


def run_g3_2_test():
    print("=" * 80)
    print(" 🚀 [G3-2 Granular Selected Paths Restore: 부분 복구 기능 독립 검증]")
    print(f" >> 기준 저장소: {PRODUCTION_REPO}")
    print(f" >> 샌드박스 격리 저장소: {SANDBOX_REPO}")
    print("=" * 80)

    # 1. 샌드박스 구성
    setup_sandbox()
    print(f"\n[Step 1] 샌드박스 격리 저장소 구성 완료: {SANDBOX_REPO}")

    # =========================================================================
    # Step 2: 종합 테스트 데이터셋 구성 및 스냅샷 생성
    # =========================================================================
    test_root = Path(tempfile.mkdtemp(prefix="g3_2_dataset_"))
    src_main = test_root / "Source_Main"
    src_extra = test_root / "Source_Extra"
    src_main.mkdir(parents=True, exist_ok=True)
    src_extra.mkdir(parents=True, exist_ok=True)

    # 파일 구성
    files_spec = {
        # 단일 및 복수 파일용
        "file_alpha.txt": b"ALPHA_CONTENT_111",
        "file_beta.txt": b"BETA_CONTENT_222",
        "file_gamma.txt": b"GAMMA_CONTENT_333",
        # 디렉터리 subtree 접두사용
        "docs/manual.pdf": b"PDF_MANUAL_DATA",
        "docs/sub/guide.txt": b"SUB_GUIDE_TEXT",
        "docs_backup/archive.zip": b"ARCHIVE_ZIP_SHOULD_NOT_BE_MATCHED_BY_DOCS",
        # 깊은 경로용 (5단계)
        "l1/l2/l3/l4/l5/deep_target.bin": b"DEEP_NESTED_BINARY_PAYLOAD_5_LEVELS",
        # 대소문자 테스트용 (원본: 소문자 파일)
        "case_test/readme.txt": b"CASE_INSENSITIVE_TEST_CONTENT",
        # 멀티소스 충돌용 (Source_Main과 Source_Extra에 동일 rel_path 생성)
        "shared/config.json": b"CONFIG_FROM_MAIN_SOURCE",
    }

    hashes = {}
    for rel_p, data in files_spec.items():
        fp = src_main / rel_p
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(data)
        hashes[f"main:{rel_p}"] = hashlib.sha256(data).hexdigest()

    # Source_Extra 에 shared/config.json 생성 (내용은 다름)
    extra_shared = src_extra / "shared" / "config.json"
    extra_shared.parent.mkdir(parents=True, exist_ok=True)
    extra_data = b"CONFIG_FROM_EXTRA_SOURCE_DIFFERENT_HASH"
    extra_shared.write_bytes(extra_data)
    hashes["extra:shared/config.json"] = hashlib.sha256(extra_data).hexdigest()

    print(f"\n[Step 2] 테스트 데이터셋 생성 완료 (Source_Main: {len(files_spec)}개, Source_Extra: 1개)")

    # 스냅샷 생성
    manifest = SnapshotEngine.create_snapshot(
        repo_dir=str(SANDBOX_REPO),
        sources=[str(src_main), str(src_extra)],
        profile_id="g3_2_profile",
        profile_name="g3_2_selected_restore_test",
        compress_level=3
    )
    snap_id = manifest.get("id") or manifest.get("snapshot_id")
    print(f" >> 스냅샷 생성 성공: {snap_id} (엔트리 수: {len(manifest.get('entries', []))}개)")

    test_results = []

    # =========================================================================
    # Case 1: 단일 파일 선택 복원
    # =========================================================================
    print(f"\n[Case 1] 단일 파일 선택 복원 ('file_alpha.txt')...")
    with tempfile.TemporaryDirectory(prefix="g3_2_c1_") as t_dir:
        res = RestoreEngine.restore_snapshot(
            repo_dir=str(SANDBOX_REPO),
            snapshot_id=snap_id,
            target_dir=t_dir,
            selected_rel_paths=["file_alpha.txt"],
            in_place=False
        )
        restored_files = [str(p.relative_to(t_dir)).replace("\\", "/") for p in Path(t_dir).rglob("*") if p.is_file()]
        c1_pass = (restored_files == ["file_alpha.txt"]) and (calc_sha256(Path(t_dir) / "file_alpha.txt") == hashes["main:file_alpha.txt"])
        print(f" >> 복원된 파일: {restored_files} | 판정: {'✅ PASS' if c1_pass else '❌ FAIL'}")
        test_results.append({
            "case": "Case 1: 단일 파일 선택 복원",
            "input": "['file_alpha.txt']",
            "expected": "file_alpha.txt 정확히 1개 복원",
            "actual": f"{restored_files}",
            "verdict": "PASS" if c1_pass else "FAIL",
            "class": "None" if c1_pass else "Class B"
        })
        assert c1_pass, "Case 1 failed"

    # =========================================================================
    # Case 2: 복수 파일 선택 복원
    # =========================================================================
    print(f"\n[Case 2] 복수 파일 선택 복원 ('file_alpha.txt', 'file_beta.txt')...")
    with tempfile.TemporaryDirectory(prefix="g3_2_c2_") as t_dir:
        res = RestoreEngine.restore_snapshot(
            repo_dir=str(SANDBOX_REPO),
            snapshot_id=snap_id,
            target_dir=t_dir,
            selected_rel_paths=["file_alpha.txt", "file_beta.txt"],
            in_place=False
        )
        restored_files = sorted([str(p.relative_to(t_dir)).replace("\\", "/") for p in Path(t_dir).rglob("*") if p.is_file()])
        expected = ["file_alpha.txt", "file_beta.txt"]
        c2_pass = (restored_files == expected)
        print(f" >> 복원된 파일: {restored_files} | 판정: {'✅ PASS' if c2_pass else '❌ FAIL'}")
        test_results.append({
            "case": "Case 2: 복수 파일 선택 복원",
            "input": "['file_alpha.txt', 'file_beta.txt']",
            "expected": f"{expected}",
            "actual": f"{restored_files}",
            "verdict": "PASS" if c2_pass else "FAIL",
            "class": "None" if c2_pass else "Class B"
        })
        assert c2_pass, "Case 2 failed"

    # =========================================================================
    # Case 3: 디렉터리 subtree 접두사 복원 (경계값: docs vs docs_backup)
    # =========================================================================
    print(f"\n[Case 3] 디렉터리 subtree 접두사 복원 ('docs')...")
    with tempfile.TemporaryDirectory(prefix="g3_2_c3_") as t_dir:
        res = RestoreEngine.restore_snapshot(
            repo_dir=str(SANDBOX_REPO),
            snapshot_id=snap_id,
            target_dir=t_dir,
            selected_rel_paths=["docs"],
            in_place=False
        )
        restored_files = sorted([str(p.relative_to(t_dir)).replace("\\", "/") for p in Path(t_dir).rglob("*") if p.is_file()])
        expected = ["docs/manual.pdf", "docs/sub/guide.txt"]
        # docs_backup 이 절대 포함되지 않아야 함!
        c3_pass = (restored_files == expected) and ("docs_backup/archive.zip" not in restored_files)
        print(f" >> 복원된 파일: {restored_files} | 판정: {'✅ PASS' if c3_pass else '❌ FAIL'}")
        test_results.append({
            "case": "Case 3: 디렉터리 subtree 복원 (docs/)",
            "input": "['docs']",
            "expected": "docs/ 하위 2개 복원 (docs_backup 제외)",
            "actual": f"{restored_files}",
            "verdict": "PASS" if c3_pass else "FAIL",
            "class": "None" if c3_pass else "Class B"
        })
        assert c3_pass, f"Case 3 failed: {restored_files}"

    # =========================================================================
    # Case 4: 5단계 깊은 디렉터리 경로 복원
    # =========================================================================
    print(f"\n[Case 4] 5단계 깊은 디렉터리 복원 ('l1/l2/l3/l4/l5/deep_target.bin')...")
    with tempfile.TemporaryDirectory(prefix="g3_2_c4_") as t_dir:
        deep_target = "l1/l2/l3/l4/l5/deep_target.bin"
        res = RestoreEngine.restore_snapshot(
            repo_dir=str(SANDBOX_REPO),
            snapshot_id=snap_id,
            target_dir=t_dir,
            selected_rel_paths=[deep_target],
            in_place=False
        )
        restored_files = [str(p.relative_to(t_dir)).replace("\\", "/") for p in Path(t_dir).rglob("*") if p.is_file()]
        c4_pass = (restored_files == [deep_target]) and (calc_sha256(Path(t_dir) / deep_target) == hashes[f"main:{deep_target}"])
        print(f" >> 복원된 파일: {restored_files} | 판정: {'✅ PASS' if c4_pass else '❌ FAIL'}")
        test_results.append({
            "case": "Case 4: 깊은 디렉터리 경로 (5단계)",
            "input": f"['{deep_target}']",
            "expected": f"{deep_target} 정확히 복원",
            "actual": f"{restored_files}",
            "verdict": "PASS" if c4_pass else "FAIL",
            "class": "None" if c4_pass else "Class B"
        })
        assert c4_pass, "Case 4 failed"

    # =========================================================================
    # Case 5: Windows 대소문자 무시 (Case-Insensitive) 매칭 복원
    # =========================================================================
    print(f"\n[Case 5] Windows 대소문자 무시 매칭 ('CASE_TEST/README.TXT' -> 'case_test/readme.txt')...")
    with tempfile.TemporaryDirectory(prefix="g3_2_c5_") as t_dir:
        res = RestoreEngine.restore_snapshot(
            repo_dir=str(SANDBOX_REPO),
            snapshot_id=snap_id,
            target_dir=t_dir,
            selected_rel_paths=["CASE_TEST/README.TXT"],
            in_place=False
        )
        restored_files = [str(p.relative_to(t_dir)).replace("\\", "/") for p in Path(t_dir).rglob("*") if p.is_file()]
        c5_pass = len(restored_files) == 1 and restored_files[0].lower() == "case_test/readme.txt"
        print(f" >> 대문자 요청 후 복원된 파일: {restored_files} | 판정: {'✅ PASS' if c5_pass else '❌ FAIL'}")
        test_results.append({
            "case": "Case 5: 대소문자 무시 (Case-Insensitive)",
            "input": "['CASE_TEST/README.TXT']",
            "expected": "case_test/readme.txt 1개 복원",
            "actual": f"{restored_files}",
            "verdict": "PASS" if c5_pass else "FAIL",
            "class": "None" if c5_pass else "Class B"
        })
        assert c5_pass, "Case 5 failed"

    # =========================================================================
    # Case 6: 존재하지 않는 경로 선택 (Graceful Skip)
    # =========================================================================
    print(f"\n[Case 6] 존재하지 않는 경로 선택 ('ghost/non_existent.txt')...")
    with tempfile.TemporaryDirectory(prefix="g3_2_c6_") as t_dir:
        res = RestoreEngine.restore_snapshot(
            repo_dir=str(SANDBOX_REPO),
            snapshot_id=snap_id,
            target_dir=t_dir,
            selected_rel_paths=["ghost/non_existent.txt"],
            in_place=False
        )
        restored_files = [str(p.relative_to(t_dir)).replace("\\", "/") for p in Path(t_dir).rglob("*") if p.is_file()]
        c6_pass = (len(restored_files) == 0) and (res["restored_files"] == 0) and (len(res["failed_files"]) == 0)
        print(f" >> 복원된 파일 수: {len(restored_files)}개, 실패 리포트: {len(res['failed_files'])}개 | 판정: {'✅ PASS' if c6_pass else '❌ FAIL'}")
        test_results.append({
            "case": "Case 6: 부존재 경로 선택 (Graceful Skip)",
            "input": "['ghost/non_existent.txt']",
            "expected": "0개 복원, 에러 없음",
            "actual": f"복원 {len(restored_files)}개, 에러 {len(res['failed_files'])}개",
            "verdict": "PASS" if c6_pass else "FAIL",
            "class": "None" if c6_pass else "Class B"
        })
        assert c6_pass, "Case 6 failed"

    # =========================================================================
    # Case 7: selected_rel_paths = None 시맨틱 (스냅샷 전체 복원)
    # =========================================================================
    print(f"\n[Case 7] selected_rel_paths = None 시맨틱 (필터 미지정 -> 전체 복원)...")
    with tempfile.TemporaryDirectory(prefix="g3_2_c7_") as t_dir:
        res = RestoreEngine.restore_snapshot(
            repo_dir=str(SANDBOX_REPO),
            snapshot_id=snap_id,
            target_dir=t_dir,
            selected_rel_paths=None,
            in_place=False
        )
        restored_files = [str(p.relative_to(t_dir)).replace("\\", "/") for p in Path(t_dir).rglob("*") if p.is_file()]
        # target_dir 플랫 복원이므로 9개 고유 rel_path 파일 복원됨
        c7_pass = (len(restored_files) == len(files_spec))
        print(f" >> 복원된 파일 수: {len(restored_files)} / {len(files_spec)}개 | 판정: {'✅ PASS' if c7_pass else '❌ FAIL'}")
        test_results.append({
            "case": "Case 7: None 시맨틱 (필터 미지정)",
            "input": "None",
            "expected": f"스냅샷 전체 {len(files_spec)}개 복원",
            "actual": f"{len(restored_files)}개 복원",
            "verdict": "PASS" if c7_pass else "FAIL",
            "class": "None" if c7_pass else "Class B"
        })
        assert c7_pass, "Case 7 failed"

    # =========================================================================
    # Case 8: selected_rel_paths = [] 시맨틱 (빈 리스트 전달)
    # ⚠️ 중요: 요구사항은 "0개 선택 -> 0개 복원"이나, 현재 코드는 'if selected_rel_paths:'로 인해
    # Falsy 평가되어 전체 복원되는 시맨틱 결함이 존재함. 이를 객관적으로 검출/기록!
    # =========================================================================
    print(f"\n[Case 8] selected_rel_paths = [] 시맨틱 (빈 리스트 전달)...")
    with tempfile.TemporaryDirectory(prefix="g3_2_c8_") as t_dir:
        res = RestoreEngine.restore_snapshot(
            repo_dir=str(SANDBOX_REPO),
            snapshot_id=snap_id,
            target_dir=t_dir,
            selected_rel_paths=[],
            in_place=False
        )
        restored_files = [str(p.relative_to(t_dir)).replace("\\", "/") for p in Path(t_dir).rglob("*") if p.is_file()]
        # 엄격한 독립 요구사항: 사용자가 빈 리스트를 넘겼다면 0개만 복원되어야 함!
        c8_pass = (len(restored_files) == 0)
        c8_verdict = "PASS" if c8_pass else "FAIL"
        c8_class = "None" if c8_pass else "Class B"
        print(f" >> 빈 리스트([]) 전달 시 실제 복원된 파일 수: {len(restored_files)}개")
        print(f" >> 💥 [판정] 결과: {c8_verdict} (결함 등급: {c8_class} — Restore Correctness)")
        if not c8_pass:
            print(f"    ※ 결함 상세: 'if selected_rel_paths:'가 빈 리스트([])를 Falsy로 평가하여 전체 파일({len(restored_files)}개)을 몽땅 복원함!")
        test_results.append({
            "case": "Case 8: 빈 리스트 [] 시맨틱 (0개 선택)",
            "input": "[]",
            "expected": "0개 복원",
            "actual": f"{len(restored_files)}개 복원" if c8_pass else f"{len(restored_files)}개 복원 (전체 복원 발생)",
            "verdict": c8_verdict,
            "class": c8_class
        })

    # =========================================================================
    # Case 9: 멀티소스 동일 rel_path 선택 복원 시 거동 실측
    # =========================================================================
    print(f"\n[Case 9] 멀티소스 동일 rel_path 선택 복원 ('shared/config.json')...")
    # in_place=True 환경에서 Source_Main과 Source_Extra 둘 다 파일 삭제 후 선택 복원
    (src_main / "shared" / "config.json").unlink()
    (src_extra / "shared" / "config.json").unlink()
    assert not (src_main / "shared" / "config.json").exists()
    assert not (src_extra / "shared" / "config.json").exists()

    res_c9 = RestoreEngine.restore_snapshot(
        repo_dir=str(SANDBOX_REPO),
        snapshot_id=snap_id,
        selected_rel_paths=["shared/config.json"],
        in_place=True,
        overwrite=True
    )
    main_restored = (src_main / "shared" / "config.json").exists()
    extra_restored = (src_extra / "shared" / "config.json").exists()
    sha_main_res = calc_sha256(src_main / "shared" / "config.json") if main_restored else ""
    sha_extra_res = calc_sha256(src_extra / "shared" / "config.json") if extra_restored else ""

    c9_both_restored = (main_restored and extra_restored)
    c9_hashes_match = (sha_main_res == hashes["main:shared/config.json"]) and (sha_extra_res == hashes["extra:shared/config.json"])
    c9_pass = c9_both_restored and c9_hashes_match
    print(f" >> Source_Main 복원 여부: {main_restored} (해시 일치: {sha_main_res == hashes['main:shared/config.json']})")
    print(f" >> Source_Extra 복원 여부: {extra_restored} (해시 일치: {sha_extra_res == hashes['extra:shared/config.json']})")
    print(f" >> 판정: {'✅ PASS' if c9_pass else '❌ FAIL'}")
    test_results.append({
        "case": "Case 9: 멀티소스 동일 rel_path 선택 복원",
        "input": "['shared/config.json'] (in_place=True)",
        "expected": "Main/Extra 두 소스의 shared/config.json이 각각의 루트로 100% 분리 복원",
        "actual": f"Main={main_restored}, Extra={extra_restored}, 해시 일치={c9_hashes_match}",
        "verdict": "PASS" if c9_pass else "FAIL",
        "class": "None" if c9_pass else "Class B"
    })
    assert c9_pass, "Case 9 failed"

    # 정리
    shutil.rmtree(test_root, ignore_errors=True)
    cleanup_sandbox()

    # =========================================================================
    # Step 3: 리포트 생성 및 통계 요약
    # =========================================================================
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    total_cases = len(test_results)
    pass_cases = sum(1 for r in test_results if r["verdict"] == "PASS")
    fail_cases = sum(1 for r in test_results if r["verdict"] == "FAIL")

    report_content = f"""# G3-2 Granular Selected Paths Restore 실전 검증 리포트

- **검증 시각**: `{datetime.datetime.now().isoformat()}`
- **최종 요약**: 총 `{total_cases}`개 시나리오 중 **PASS: `{pass_cases}`개**, **FAIL: `{fail_cases}`개**
- **검증 대상**: `RestoreEngine.restore_snapshot(..., selected_rel_paths=...)`

---

## 9대 시나리오별 실측 검증 결과 매트릭스

| No | 시나리오 | 입력값 | 기대 요구사항 | 실제 실측 결과 | 판정 | 결함 등급 |
|:---:|:---|:---|:---|:---|:---:|:---:|
"""
    for idx, r in enumerate(test_results, 1):
        v_icon = "✅ **PASS**" if r["verdict"] == "PASS" else "❌ **FAIL**"
        report_content += f"| **{idx}** | **{r['case']}** | `{r['input']}` | {r['expected']} | {r['actual']} | {v_icon} | `{r['class']}` |\n"

    report_content += f"""
---

## 🚨 발견된 결함 상세 분석 (Defect Triage)

### 1. [Class B — Restore Correctness] Case 8: 빈 리스트 `[]` 전달 시 전체 복원 버그
- **증상**: 호출자가 `selected_rel_paths=[]` (0개 파일 선택)를 명시적으로 전달했음에도, 스냅샷 내 **모든 파일(100%)이 전체 복원**됨.
- **원인 코드 (`core/restore.py` L49)**:
  ```python
  if selected_rel_paths:  # 빈 리스트([])는 Falsy이므로 else로 진입!
      ...
  else:
      to_restore = entries  # 스냅샷 전체 복원 발생!
  ```
- **영향 범위**:
  - `selected_rel_paths=None` (필터 미지정 ➡️ 전체 복원 의도)
  - `selected_rel_paths=[]` (선택 개수 0개 ➡️ 0개 복원 의도)
  - 두 시맨틱의 구분이 파괴되어, 웹 UI나 API에서 사용자가 체크박스를 전부 해제하고 복원을 시작할 경우 의도치 않게 모든 파일이 복원되는 위험한 동작 유발.
- **권고 수정안**:
  `if selected_rel_paths is not None:`로 변경하여 `None`일 때만 전체 복원으로 진입하고, `[]`일 때는 `to_restore = []`로 유지되어 0개 복원되도록 처리 필요.

---

## 💡 결론 및 조치 권고
- G3-2의 기능적 필터링(단일 파일, 복수 파일, 디렉터리 subtree, 깊은 경로, Windows 대소문자 무시, 부존재 경로, 멀티소스 분리)은 **모두 100% 정상 작동**함을 확인했습니다.
- 단, **Case 8 (`[]` vs `None`) 결함은 사용자가 수립한 기준에 따라 `Class B (Restore Correctness)` 결함으로 공식 적출**되었습니다.
- 사용자 지침에 따라 즉시 버전을 올리지 않고, 본 결함을 **G3 결함 트래커에 기록**한 뒤 G3 전체 단계 완료 후 일괄 패치 여부를 결정하는 것을 권고합니다.
"""
    REPORT_MD.write_text(report_content, encoding="utf-8")
    print(f"\n >> 마크다운 리포트 저장 완료: {REPORT_MD}")

    print("\n" + "=" * 80)
    print(f" 🏁 [G3-2 Granular Selected Paths Restore] 결과: PASS {pass_cases}/{total_cases}, FAIL {fail_cases}/{total_cases}")
    print("=" * 80)


if __name__ == "__main__":
    run_g3_2_test()
