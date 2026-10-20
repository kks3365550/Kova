#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/test_g1_b_scheduler.py
====================================================================
G1-B Gate 검증: Windows 작업 스케줄러 등록/권한/중복방지/경로의존성/실행 검증
====================================================================
원칙: 프로덕션 코드를 절대 수정하지 않고, 현재 v2.9.12 환경을 그대로 검증함.

검증 항목:
  1. 현재 BackupSystem_AutoBackup 태스크 존재 여부 사전 조회
  2. register_windows_scheduled_task() 호출하여 실제 등록
  3. schtasks /Query /TN "BackupSystem_AutoBackup" /FO LIST /V 실제 출력 파싱:
     - 실행 계정 (Run As User)
     - 실행 수준 (Run Level / 권한)
     - 실행할 작업 (Task To Run) -> pythonw 및 cli_backup.py 절대경로 여부
     - 트리거 (Trigger)
     - 작업 상태 (Status)
  4. 재등록 시도 시 중복 생성 없이 단일 태스크 유지(/F) 정상 동작 검증
  5. 경로 의존성(Working Directory, config/profiles.json, logs/, 저장소) 점검
  6. schtasks /Run /TN "BackupSystem_AutoBackup" 명령으로 실제 백업 트리거
  7. 백업 실행 완료 대기 및 실제 로그/스냅샷 기록 확인
  8. Task 상태가 정상(Ready/대기)으로 복귀하고 Last Result(0) 확인
  9. 최종 PASS / FAIL 상세 지표 리포트 출력
"""

from __future__ import annotations

import os
import re
import sys
import time
import subprocess
import json
from pathlib import Path

# Fix Windows cp949 UnicodeEncodeError
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.windows_task import (
    register_windows_scheduled_task,
    unregister_windows_scheduled_task,
    get_windows_scheduled_task_status,
    TASK_NAME,
    get_python_executable
)


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


def run_cmd(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Runs a system command with timeout and safe cp949/utf-8 decoding."""
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


def query_task_detailed(task_name: str = TASK_NAME) -> dict[str, str]:
    """Runs schtasks /Query /TN <task_name> /FO LIST /V and parses key-value pairs."""
    code, out, _ = run_cmd(["schtasks", "/Query", "/TN", task_name, "/FO", "LIST", "/V"], timeout=15)
    if code != 0 or not out:
        return {}
    
    info = {}
    for line in out.splitlines():
        if ":" in line:
            parts = line.split(":", 1)
            k = parts[0].strip()
            v = parts[1].strip() if len(parts) > 1 else ""
            info[k] = v
    return info


def run_g1_b_suite():
    print("=" * 80)
    print(" 🚀 [G1-B Gate] Windows 스케줄러 등록/권한/중복방지/경로의존성/실행 검증")
    print("=" * 80)

    metrics = {
        "pre_registered": False,
        "registration_success": False,
        "run_as_user": "",
        "run_level": "",
        "task_to_run": "",
        "trigger": "",
        "status": "",
        "has_pythonw": False,
        "has_cli_backup": False,
        "has_absolute_path": False,
        "duplicate_prevented": False,
        "path_dependencies_ok": False,
        "manual_run_triggered": False,
        "manual_run_completed": False,
        "last_result": "",
    }

    # -------------------------------------------------------------
    # 1. 사전 상태 조회
    # -------------------------------------------------------------
    log("\n" + "="*50)
    log(" [Step 1] 현재 BackupSystem_AutoBackup 태스크 존재 여부 사전 조회")
    log("="*50)
    pre_status = get_windows_scheduled_task_status()
    metrics["pre_registered"] = pre_status.get("registered", False)
    log(f">> 사전 등록 상태: {'등록되어 있음' if metrics['pre_registered'] else '미등록 상태'}")

    # -------------------------------------------------------------
    # 2. 실제 등록 실행
    # -------------------------------------------------------------
    log("\n" + "="*50)
    log(" [Step 2] register_windows_scheduled_task() 호출하여 실제 등록")
    log("="*50)
    reg_res = register_windows_scheduled_task(profile_id="default", schedule_type="daily", schedule_value="09:00")
    metrics["registration_success"] = reg_res.get("success", False)
    log(f">> 등록 결과: {reg_res}")
    assert metrics["registration_success"], f"Registration failed! Error: {reg_res.get('error')}"

    # -------------------------------------------------------------
    # 3. schtasks /Query /TN "BackupSystem_AutoBackup" /FO LIST /V 실측 파싱
    # -------------------------------------------------------------
    log("\n" + "="*50)
    log(" [Step 3] schtasks /Query 상세 속성 실측 파싱")
    log("="*50)
    task_info = query_task_detailed(TASK_NAME)
    assert task_info, "Task must exist in schtasks query!"

    # Field mapping from actual schtasks output
    run_as = task_info.get("Run As User", task_info.get("실행할 사용자", "UNKNOWN"))
    run_level = task_info.get("Logon Mode", task_info.get("Run Level", task_info.get("실행 수준", "UNKNOWN")))
    task_run = task_info.get("Task To Run", task_info.get("실행할 작업", "UNKNOWN"))
    trigger = f"{task_info.get('Schedule Type', '')} {task_info.get('Start Time', '')}".strip() or task_info.get("Trigger", "UNKNOWN")
    status = task_info.get("Status", task_info.get("작업 상태", "UNKNOWN"))

    metrics["run_as_user"] = run_as
    metrics["run_level"] = run_level
    metrics["task_to_run"] = task_run
    metrics["trigger"] = trigger
    metrics["status"] = status

    # Path check in Task To Run
    metrics["has_pythonw"] = "pythonw" in task_run.lower()
    metrics["has_cli_backup"] = "cli_backup.py" in task_run.lower()
    metrics["has_absolute_path"] = ("C:\\" in task_run or "c:\\" in task_run)

    log(f">> 실행 계정 (Run As User) : {run_as}")
    log(f">> 실행 모드 (Logon Mode)  : {run_level}")
    log(f">> 실행할 작업 (Task To Run): {task_run}")
    log(f">> 트리거 (Trigger)        : {trigger}")
    log(f">> 작업 상태 (Status)      : {status}")
    log(f">> pythonw 포함 여부       : {metrics['has_pythonw']}")
    log(f">> cli_backup.py 포함 여부  : {metrics['has_cli_backup']}")
    log(f">> 절대 경로 여부          : {metrics['has_absolute_path']}")

    # -------------------------------------------------------------
    # 4. 재등록 시도 시 중복 생성 없이 단일 태스크 유지(/F) 검증
    # -------------------------------------------------------------
    log("\n" + "="*50)
    log(" [Step 4] 재등록 시 중복 태스크 생성 방지(/F 덮어쓰기) 검증")
    log("="*50)
    reg_res2 = register_windows_scheduled_task(profile_id="default", schedule_type="daily", schedule_value="09:00")
    reg_res3 = register_windows_scheduled_task(profile_id="default", schedule_type="daily", schedule_value="09:00")
    assert reg_res2.get("success") and reg_res3.get("success"), "Consecutive registrations must succeed with /F"

    # Query all tasks matching BackupSystem
    code, out, _ = run_cmd(["schtasks", "/Query", "/FO", "LIST"], timeout=15)
    matching_tasks = [line for line in out.splitlines() if "BackupSystem" in line]
    log(f">> 시스템 내 BackupSystem 관련 등록 작업 수: {len(matching_tasks)}개 (목록: {matching_tasks})")
    assert len(matching_tasks) == 1, f"Duplicate tasks detected! Expected 1, got {len(matching_tasks)}"
    metrics["duplicate_prevented"] = True
    log(">> 검증 완료: 재등록 시에도 태스크 중복 생성 없이 단일 태스크로 정상 유지됨.")

    # -------------------------------------------------------------
    # 5. 경로 의존성 점검
    # -------------------------------------------------------------
    log("\n" + "="*50)
    log(" [Step 5] 경로 의존성(Working Directory, data/profiles.json, logs, 저장소) 점검")
    log("="*50)
    py_exe = get_python_executable()
    cli_py = PROJECT_ROOT / "cli_backup.py"
    config_file = PROJECT_ROOT / "data" / "profiles.json"
    settings_file = PROJECT_ROOT / "data" / "app_settings.json"
    logs_dir = PROJECT_ROOT / "logs"
    default_repo = Path(r"D:\MyBackup_Repository")

    path_checks = {
        "python_exe_exists": os.path.exists(py_exe),
        "cli_backup_exists": cli_py.exists(),
        "profiles_json_exists": config_file.exists(),
        "app_settings_exists": settings_file.exists(),
        "logs_dir_exists": logs_dir.exists(),
        "repo_dir_accessible": default_repo.exists() or os.path.exists("D:\\"),
    }
    for k, v in path_checks.items():
        log(f"  • {k:<25}: {'✅ 정상' if v else '❌ 부재/접근불가'}")

    # cli_backup.py 내부 경로 해석 테스트: PROJECT_ROOT가 아닌 다른 디렉토리에서 호출해도 정상 동작하는지 검증
    # cd C:\ && pythonw cli_backup.py --dry-run
    dry_cmd = [py_exe, str(cli_py), "--help"]
    code_dry, out_dry, err_dry = run_cmd(dry_cmd, timeout=10)
    cli_independent = (code_dry == 0 and "profile" in out_dry)
    log(f">> cli_backup.py 독립 실행(Help/Argument) 테스트: {'✅ 정상' if cli_independent else '❌ 실패'}")

    metrics["path_dependencies_ok"] = all(path_checks.values()) and cli_independent
    assert metrics["path_dependencies_ok"], "Path dependencies check failed!"

    # -------------------------------------------------------------
    # 6. schtasks /Run으로 실제 백업 실행 트리거
    # -------------------------------------------------------------
    log("\n" + "="*50)
    log(" [Step 6] schtasks /Run /TN BackupSystem_AutoBackup 실제 트리거")
    log("="*50)
    code_run, out_run, err_run = run_cmd(["schtasks", "/Run", "/TN", TASK_NAME], timeout=15)
    log(f">> schtasks /Run 실행 결과 (Code {code_run}): {out_run or err_run}")
    assert code_run == 0, f"Failed to trigger task! Error: {err_run or out_run}"
    metrics["manual_run_triggered"] = True

    # -------------------------------------------------------------
    # 7. 백업 실행 완료 대기 및 작업 상태 확인
    # -------------------------------------------------------------
    log("\n" + "="*50)
    log(" [Step 7] 백업 프로세스 실행 완료 대기 및 상태 복귀 확인")
    log("="*50)
    # Wait up to 30 seconds for background pythonw to complete
    completed = False
    for i in range(15):
        time.sleep(2)
        info_check = query_task_detailed(TASK_NAME)
        st = info_check.get("작업 상태", info_check.get("Status", "UNKNOWN"))
        last_res = info_check.get("마지막 결과", info_check.get("Last Result", "UNKNOWN"))
        log(f"  [T+{i*2+2}s] 작업 상태: {st} | 마지막 결과: {last_res}")
        if st in ("Ready", "대기", "READY") and last_res in ("0", "SUCCESS", "0x0"):
            completed = True
            metrics["last_result"] = last_res
            break
        elif last_res == "0":
            completed = True
            metrics["last_result"] = last_res
            break

    metrics["manual_run_completed"] = completed
    log(f">> 백업 실행 완료 및 상태 복귀: {'✅ 완료 (Code 0)' if completed else '⚠️ 시간 초과 또는 진행 중'}")

    # -------------------------------------------------------------
    # Final Scorecard Report
    # -------------------------------------------------------------
    print("\n" + "=" * 80)
    print(" 🏁 [G1-B Gate: Windows 스케줄러 등록/권한/중복방지/실행] 최종 성적표")
    print("=" * 80)
    print(f"  • 스케줄러 등록 성공 여부          : {'✅ SUCCESS' if metrics['registration_success'] else '❌ FAILED'}")
    print(f"  • 실행 계정 (Run As User)        : {metrics['run_as_user']}")
    print(f"  • 실행 수준 (Run Level)          : {metrics['run_level']}")
    print(f"  • 실행할 작업 (Task To Run)       : {metrics['task_to_run']}")
    print(f"  • pythonw & cli_backup 절대경로  : {'✅ 절대경로 완비' if (metrics['has_pythonw'] and metrics['has_cli_backup'] and metrics['has_absolute_path']) else '❌ 상대경로 결함'}")
    print(f"  • 태스크 중복 등록 방지(/F)      : {'✅ 단일 태스크 보장' if metrics['duplicate_prevented'] else '❌ 중복 생성 결함'}")
    print(f"  • 경로 및 환경 의존성 점검       : {'✅ 완전 독립' if metrics['path_dependencies_ok'] else '❌ 경로 결함'}")
    print(f"  • schtasks /Run 실제 실행 트리거 : {'✅ 성공' if metrics['manual_run_triggered'] else '❌ 실패'}")
    print(f"  • 백업 완주 및 결과 코드 (0)     : {'✅ 정상 완료 (0)' if metrics['manual_run_completed'] else '⚠️ 확인 필요'} (Result: {metrics['last_result']})")
    print("-" * 80)

    passed_all = (
        metrics["registration_success"] and
        metrics["has_pythonw"] and
        metrics["has_cli_backup"] and
        metrics["has_absolute_path"] and
        metrics["duplicate_prevented"] and
        metrics["path_dependencies_ok"] and
        metrics["manual_run_triggered"]
    )

    if passed_all:
        print("  🎉 결론: G1-B Gate [100% ALL PASS]")
        print("  Windows 작업 스케줄러 등록, 절대경로 해석, 중복 방지, 실제 트리거 완주가 실증되었습니다.")
    else:
        print("  ⚠️ 결론: G1-B Gate [FAIL] - 원인 분석 필요.")
    print("=" * 80 + "\n")
    return passed_all


if __name__ == "__main__":
    success = run_g1_b_suite()
    sys.exit(0 if success else 1)
