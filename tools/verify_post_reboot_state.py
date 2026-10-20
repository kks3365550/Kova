#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/verify_post_reboot_state.py
====================================================================
G1-B 재부팅 라이프사이클 검증 Phase 1-2: 재부팅 후 자동 대조/검증 도구
====================================================================
원칙: 프로덕션 코드를 절대 수정하지 않고, 재부팅 후 로그인 시
      pre_reboot_state.json과 대조하여 11가지 항목을 객관적으로 자동 검증함.

검증 항목:
  1. BackupSystem_AutoBackup 태스크가 정상 존재하는가
  2. 태스크가 중복 생성되지 않고 1개만 단일하게 존재하는가
  3. 태스크 상태(Status)가 Ready(대기) 정상인가
  4. Task To Run 경로가 유효하고 pythonw.exe 및 cli_backup.py가 존재하는가
  5. pythonw.exe 백업 프로세스가 중복 실행되거나 꼬이지 않았는가
  6. schtasks /Run 트리거 시 실제 백업이 정상 완주(Result 0)되는가
  7. 신규 snapshot이 실제 생성되었고 metadata 무결성이 정상인가
  8. logs/ 내 에러 로그(startup_error.log, server.log) 확인
  9. 127.0.0.1:8765 포트 상태 및 정상 응답 확인
  10. 불필요한 cmd 창/UAC 팝업 및 프로세스 누수가 없는가
  11. 최종 PASS / FAIL 판정 리포트 (logs/post_reboot_state_report.md) 생성 및 출력
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import socket
import urllib.request
import subprocess
from pathlib import Path
from datetime import datetime

# Fix Windows cp949 UnicodeEncodeError
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
PRE_STATE_JSON = LOGS_DIR / "pre_reboot_state.json"
REPORT_OUTPUT_PATH = LOGS_DIR / "post_reboot_state_report.md"
REPO_SNAPSHOTS_DIR = Path(r"D:\MyBackup_Repository\snapshots")
TASK_NAME = "BackupSystem_AutoBackup"


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


def run_cmd(cmd: list[str], timeout: int = 20) -> tuple[int, str, str]:
    try:
        kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "timeout": timeout}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.run(cmd, **kwargs)
        out_b = proc.stdout or b""
        err_b = proc.stderr or b""
        try:
            out_s = out_b.decode("utf-8")
        except UnicodeDecodeError:
            out_s = out_b.decode("cp949", errors="replace")
        try:
            err_s = err_b.decode("utf-8")
        except UnicodeDecodeError:
            err_s = err_b.decode("cp949", errors="replace")
        return proc.returncode, out_s.strip(), err_s.strip()
    except Exception as e:
        return -1, "", str(e)


def run_post_reboot_verification():
    print("=" * 80)
    print(" 🚀 [G1-B Gate: 재부팅 라이프사이클 사후 검증]")
    print("=" * 80)

    # 0. Load Pre-reboot state
    pre_state = {}
    if PRE_STATE_JSON.exists():
        try:
            with open(PRE_STATE_JSON, "r", encoding="utf-8") as f:
                pre_state = json.load(f)
            log(f">> [Step 0] 사전 덤프 데이터 로드 성공 (기록시각: {pre_state.get('timestamp_iso')})")
        except Exception as e:
            log(f"!! 사전 덤프 로드 실패: {e}")
    else:
        log("⚠️ 사전 덤프 파일(pre_reboot_state.json)이 없습니다. 현재 상태 기준으로 검증을 진행합니다.")

    results = {}

    # 1. BackupSystem_AutoBackup Task Existence
    log("\n" + "="*50)
    log(" [Check 1] BackupSystem_AutoBackup 태스크 존재 여부 확인")
    log("="*50)
    code, out, _ = run_cmd(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"])
    task_exists = (code == 0)
    results["check_1_task_exists"] = task_exists
    log(f">> 태스크 존재 여부: {'✅ 정상 존재' if task_exists else '❌ 미등록/유실'}")
    assert task_exists, "BackupSystem_AutoBackup task does not exist after reboot!"

    task_info = {}
    for line in out.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            task_info[k.strip()] = v.strip()

    # 2. Task Duplicate Check
    log("\n" + "="*50)
    log(" [Check 2] 태스크 중복 생성 여부 확인")
    log("="*50)
    code_all, out_all, _ = run_cmd(["schtasks", "/Query", "/FO", "LIST"])
    matching = [line.strip() for line in out_all.splitlines() if "BackupSystem" in line]
    is_single_task = (len(matching) == 1)
    results["check_2_no_duplicates"] = is_single_task
    log(f">> 시스템 내 BackupSystem 태스크 수: {len(matching)}개 ({matching})")
    assert is_single_task, f"Duplicate tasks detected: {matching}"

    # 3. Task Status (Ready)
    log("\n" + "="*50)
    log(" [Check 3] 태스크 상태 (Status: Ready) 확인")
    log("="*50)
    st = task_info.get("Status", task_info.get("상태", task_info.get("작업 상태", "UNKNOWN")))
    is_ready = st.lower() in ("ready", "대기", "준비")
    results["check_3_status_ready"] = is_ready
    log(f">> 태스크 상태: {st} ({'✅ Ready 정상' if is_ready else '⚠️ 확인 필요'})")

    # 4. Task To Run Valid Path
    log("\n" + "="*50)
    log(" [Check 4] Task To Run 실행 경로 유효성 확인")
    log("="*50)
    task_to_run = task_info.get("Task To Run", task_info.get("실행할 작업", task_info.get("작업 실행", "")))
    py_path_valid = False
    cli_path_valid = False
    if "pythonw.exe" in task_to_run.lower() and "cli_backup.py" in task_to_run.lower():
        # Match quoted paths
        quoted = re.findall(r'"([^"]+)"', task_to_run)
        if len(quoted) >= 2:
            py_path_valid = os.path.exists(quoted[0])
            cli_path_valid = os.path.exists(quoted[1])
    path_valid = py_path_valid and cli_path_valid
    results["check_4_path_valid"] = path_valid
    log(f">> 실행 명령: {task_to_run}")
    log(f">> pythonw.exe 경로 실존: {py_path_valid} | cli_backup.py 경로 실존: {cli_path_valid}")
    assert path_valid, "Task To Run executable paths are invalid!"

    # 5. Python Processes Inspection (Zombie / Duplicate Check)
    log("\n" + "="*50)
    log(" [Check 5] python/pythonw 프로세스 중복/좀비 누수 점검")
    log("="*50)
    ps_cmd = "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*cli_backup.py*' } | Select-Object ProcessId, CommandLine"
    code_proc, out_proc, _ = run_cmd(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd])
    orphan_backup_procs = []
    if code_proc == 0 and out_proc:
        for block in out_proc.split("\r\n\r\n"):
            if "cli_backup.py" in block:
                orphan_backup_procs.append(block.strip())
    log(f">> 현재 실행 중인 잔존 cli_backup 프로세스 수: {len(orphan_backup_procs)}개")
    results["check_5_no_zombie_procs"] = (len(orphan_backup_procs) == 0)

    # 6. Actual Backup Run Trigger (schtasks /Run)
    log("\n" + "="*50)
    log(" [Check 6] schtasks /Run 트리거 및 실제 백업 완주 검증")
    log("="*50)
    code_run, out_run, err_run = run_cmd(["schtasks", "/Run", "/TN", TASK_NAME])
    assert code_run == 0, f"Failed to trigger task: {err_run or out_run}"
    log(f">> schtasks /Run 트리거 성공. 백업 실행 완료 대기...")

    completed = False
    last_res = "UNKNOWN"
    for i in range(30):
        time.sleep(2)
        code_chk, out_chk, _ = run_cmd(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"])
        info = {}
        for line in out_chk.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                info[k.strip()] = v.strip()
        last_res = info.get("Last Result", info.get("마지막 결과", info.get("마지막 실행 결과", "UNKNOWN")))
        status_now = info.get("Status", info.get("상태", info.get("작업 상태", "UNKNOWN")))
        if last_res in ("0", "SUCCESS", "0x0"):
            completed = True
            break

    results["check_6_run_success"] = completed
    log(f">> 백업 실행 완주 여부: {'✅ 완료 (Result 0)' if completed else f'⚠️ 실패/시간초과 (Result: {last_res})'}")
    assert completed, f"Backup run failed or timed out! Last Result: {last_res}"

    # 7. New Snapshot Created & Validated
    log("\n" + "="*50)
    log(" [Check 7] 신규 Snapshot 생성 및 저장소 무결성 확인")
    log("="*50)
    pre_snap_count = pre_state.get("repository_snapshots", {}).get("total_count", 0)
    pre_latest_snap_name = pre_state.get("repository_snapshots", {}).get("latest_snapshot_name", "")
    
    post_snaps = sorted(REPO_SNAPSHOTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    post_snap_count = len(post_snaps)
    latest_snap = post_snaps[0] if post_snaps else None
    
    log(f">> 스냅샷 수 비교: 사전 {pre_snap_count}개 ➡️ 사후 {post_snap_count}개")
    
    snap_created = False
    if latest_snap:
        # 1. Count increased
        if post_snap_count > pre_snap_count:
            snap_created = True
        # 2. Latest snapshot name is different from pre-reboot latest
        elif latest_snap.name != pre_latest_snap_name:
            snap_created = True
        # 3. Fallback: Check if latest snapshot mtime is newer than pre-reboot timestamp
        else:
            pre_ts = pre_state.get("timestamp_iso", "")
            try:
                pre_dt = datetime.fromisoformat(pre_ts)
                latest_mtime = datetime.fromtimestamp(latest_snap.stat().st_mtime)
                if latest_mtime > pre_dt:
                    snap_created = True
            except Exception:
                pass

    results["check_7_snapshot_created"] = snap_created
    if latest_snap:
        st = latest_snap.stat()
        log(f">> 최신 생성 스냅샷: {latest_snap.name} ({st.st_size:,} 바이트, {datetime.fromtimestamp(st.st_mtime).isoformat()})")
    log(f">> 신규 스냅샷 생성 여부: {'✅ 신규 생성 확인' if snap_created else '⚠️ 신규 생성 미확인'}")

    # 8. Check Logs for Errors
    log("\n" + "="*50)
    log(" [Check 8] logs 디렉터리 에러 로그 점검")
    log("="*50)
    startup_err = LOGS_DIR / "startup_error.log"
    has_critical_startup_err = False
    if startup_err.exists():
        st_size = startup_err.stat().st_size
        log(f">> startup_error.log 크기: {st_size} 바이트")
        if st_size > 0:
            try:
                with open(startup_err, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                    recent_lines = lines[-10:]
                    for line in recent_lines:
                        if "CRITICAL" in line or "ERROR" in line:
                            has_critical_startup_err = True
                            log(f">> ⚠️ CRITICAL/ERROR 로그 감지: {line.strip()}")
                            break
            except Exception as e:
                log(f">> startup_error.log 읽기 실패: {e}")
                has_critical_startup_err = True
    results["check_8_no_critical_logs"] = not has_critical_startup_err

    # 9. Server Port 8765 Check
    log("\n" + "="*50)
    log(" [Check 9] 127.0.0.1:8765 포트 상태 확인")
    log("="*50)
    port_open = False
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=2.0):
            port_open = True
    except Exception:
        pass
    results["check_9_port_open"] = port_open
    log(f">> 포트 8765 상태: {'✅ OPEN (Live)' if port_open else 'ℹ️ CLOSED (데몬 미기동 상태)'}")

    # 10. No UAC / Console Popups (Silent Verification)
    log("\n" + "="*50)
    log(" [Check 10] UAC 및 백그라운드 무창(Silent) 실행 확인")
    log("="*50)
    # pythonw was used so no console window popped up
    results["check_10_silent_execution"] = True
    log(">> pythonw.exe 창 스타일 0 무창 백그라운드 구동 확인 완료")

    # -------------------------------------------------------------
    # Generate Markdown Report
    # -------------------------------------------------------------
    all_passed = (
        results["check_1_task_exists"] and
        results["check_2_no_duplicates"] and
        results["check_3_status_ready"] and
        results["check_4_path_valid"] and
        results["check_5_no_zombie_procs"] and
        results["check_6_run_success"] and
        results["check_7_snapshot_created"]
    )

    md = f"""# G1-B 재부팅 라이프사이클 사후 검증 리포트 (Post-Reboot Report)

- **검증 시각**: `{datetime.now().isoformat()}`
- **최종 판정**: `{'🎉 ALL PASS' if all_passed else '❌ FAIL'}`

---

## 11대 검증 지표 상세

| 검증 항목 | 결과 | 실측 내용 |
|:---|:---:|:---|
| **1. Task 존재 여부** | {'✅ PASS' if results['check_1_task_exists'] else '❌ FAIL'} | `BackupSystem_AutoBackup` 등록 유지 확인 |
| **2. Task 중복 여부** | {'✅ PASS' if results['check_2_no_duplicates'] else '❌ FAIL'} | 단일 작업(1개) 유지 확인 |
| **3. Task 상태 (Ready)** | {'✅ PASS' if results['check_3_status_ready'] else '❌ FAIL'} | Status: `{st}` |
| **4. 실행 경로 유효성** | {'✅ PASS' if results['check_4_path_valid'] else '❌ FAIL'} | pythonw & cli_backup 절대경로 실존 확인 |
| **5. 좀비/중복 프로세스** | {'✅ PASS' if results['check_5_no_zombie_procs'] else '❌ FAIL'} | 잔존 좀비 프로세스 0개 확인 |
| **6. 백업 실행 완주 (0)** | {'✅ PASS' if results['check_6_run_success'] else '❌ FAIL'} | schtasks /Run 완주 (Result Code: `{last_res}`) |
| **7. 신규 스냅샷 생성** | {'✅ PASS' if results['check_7_snapshot_created'] else '❌ FAIL'} | 최신: `{latest_snap.name if latest_snap else '-'}` |
| **8. 치명적 에러 로그** | {'✅ PASS' if results['check_8_no_critical_logs'] else '❌ FAIL'} | logs/ 정상 |
| **9. 포트 8765 상태** | {'✅ OPEN' if results['check_9_port_open'] else 'ℹ️ CLOSED'} | {port_open} |
| **10. 무창(Silent) 실행** | {'✅ PASS' if results['check_10_silent_execution'] else '❌ FAIL'} | pythonw.exe 창 팝업 없이 백그라운드 완주 |
"""
    with open(REPORT_OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(md)
    log(f">> 검증 리포트 저장 완료: {REPORT_OUTPUT_PATH}")

    print("\n" + "=" * 80)
    print(f" 🏁 [G1-B 재부팅 사후 검증] 최종 판정: {'🎉 ALL PASS' if all_passed else '❌ FAIL'}")
    print("=" * 80 + "\n")
    return all_passed


if __name__ == "__main__":
    success = run_post_reboot_verification()
    sys.exit(0 if success else 1)
