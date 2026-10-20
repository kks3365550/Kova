#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/record_pre_reboot_state.py
====================================================================
G1-B 재부팅 라이프사이클 검증 Phase 1-1: 재부팅 전 상태 정밀 덤프/기록 도구
====================================================================
원칙: 프로덕션 코드를 절대 수정하지 않고, 재부팅 전 현재 시스템 상태를
      객관적인 JSON과 Markdown 리포트로 logs/pre_reboot_state.json 에 저장함.

기록 대상:
  1. BackupSystem_AutoBackup Task 상태 및 속성
  2. 시스템 내 등록된 전체 Task 중 BackupSystem 관련 작업 목록
  3. 현재 실행 중인 python / pythonw 프로세스 목록 (PID, ProcessName, CommandLine)
  4. 127.0.0.1:8765 HTTP / TCP 연결 상태
  5. D:\MyBackup_Repository\snapshots 내 최신 스냅샷 목록 및 파일명, 크기, 수정 시각
  6. 현재 시스템 시간 및 마지막 백업 기록 시각
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
JSON_OUTPUT_PATH = LOGS_DIR / "pre_reboot_state.json"
REPORT_OUTPUT_PATH = LOGS_DIR / "pre_reboot_state_report.md"
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


def collect_pre_reboot_state() -> dict:
    state = {
        "timestamp_iso": datetime.now().isoformat(),
        "timestamp_epoch": time.time(),
        "task_scheduler": {},
        "matching_tasks": [],
        "python_processes": [],
        "server_port_8765": {},
        "repository_snapshots": {},
        "data_profiles": {},
    }

    # 1. BackupSystem_AutoBackup Task Scheduler Status
    log(">> [1/6] BackupSystem_AutoBackup Task 상태 조회 중...")
    code, out, err = run_cmd(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"])
    task_info = {}
    if code == 0 and out:
        for line in out.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                task_info[k.strip()] = v.strip()
    state["task_scheduler"] = {
        "query_returncode": code,
        "is_registered": (code == 0),
        "raw_attributes": task_info,
        "status": task_info.get("Status", task_info.get("작업 상태", "UNKNOWN")),
        "run_as_user": task_info.get("Run As User", task_info.get("실행할 사용자", "UNKNOWN")),
        "task_to_run": task_info.get("Task To Run", task_info.get("실행할 작업", "UNKNOWN")),
        "last_run_time": task_info.get("Last Run Time", task_info.get("마지막 실행 시간", "UNKNOWN")),
        "last_result": task_info.get("Last Result", task_info.get("마지막 결과", "UNKNOWN")),
        "next_run_time": task_info.get("Next Run Time", task_info.get("다음 실행 시간", "UNKNOWN")),
    }

    # 2. All BackupSystem matching tasks
    log(">> [2/6] BackupSystem 관련 전체 작업 목록 조회 중...")
    code_all, out_all, _ = run_cmd(["schtasks", "/Query", "/FO", "LIST"])
    matching = []
    if code_all == 0 and out_all:
        for line in out_all.splitlines():
            if "BackupSystem" in line:
                matching.append(line.strip())
    state["matching_tasks"] = matching

    # 3. Python / Pythonw Processes
    log(">> [3/6] 현재 실행 중인 python/pythonw 프로세스 수집 중...")
    ps_proc_cmd = "Get-CimInstance Win32_Process | Where-Object { $_.Name -like '*python*' } | Select-Object ProcessId, Name, CommandLine"
    code_ps, out_ps, _ = run_cmd(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_proc_cmd])
    procs = []
    if code_ps == 0 and out_ps:
        for block in out_ps.split("\r\n\r\n"):
            p_dict = {}
            for line in block.splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    p_dict[k.strip()] = v.strip()
            if p_dict.get("ProcessId"):
                procs.append(p_dict)
    state["python_processes"] = procs

    # 4. Port 8765 Status
    log(">> [4/6] 127.0.0.1:8765 포트 및 HTTP 응답 점검 중...")
    port_info = {"tcp_open": False, "http_status": None, "detail": ""}
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=2.0):
            port_info["tcp_open"] = True
    except Exception as e:
        port_info["detail"] = str(e)

    if port_info["tcp_open"]:
        try:
            req = urllib.request.Request("http://127.0.0.1:8765/api/status", headers={"User-Agent": "PreRebootRecorder"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                port_info["http_status"] = resp.status
                port_info["detail"] = "HTTP 200 OK"
        except Exception as e:
            port_info["detail"] = f"HTTP Error: {e}"
    state["server_port_8765"] = port_info

    # 5. D:\MyBackup_Repository\snapshots inspection
    log(">> [5/6] D:\\MyBackup_Repository\\snapshots 스캔 중...")
    repo_info = {"exists": REPO_SNAPSHOTS_DIR.exists(), "total_count": 0, "latest_snapshots": []}
    if REPO_SNAPSHOTS_DIR.exists():
        snap_files = []
        for f in REPO_SNAPSHOTS_DIR.glob("*.json"):
            st = f.stat()
            snap_files.append({
                "name": f.name,
                "size_bytes": st.st_size,
                "mtime_iso": datetime.fromtimestamp(st.st_mtime).isoformat(),
                "mtime_epoch": st.st_mtime
            })
        snap_files.sort(key=lambda x: x["mtime_epoch"], reverse=True)
        repo_info["total_count"] = len(snap_files)
        repo_info["latest_snapshots"] = snap_files[:10]
    state["repository_snapshots"] = repo_info

    # 6. Data Profiles & Last Run
    log(">> [6/6] data/profiles.json 프로필 상태 점검 중...")
    prof_path = PROJECT_ROOT / "data" / "profiles.json"
    if prof_path.exists():
        try:
            with open(prof_path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                state["data_profiles"] = {
                    "exists": True,
                    "profile_count": len(data),
                    "profiles_summary": [
                        {
                            "id": p.get("id"),
                            "name": p.get("name"),
                            "auto_backup_enabled": p.get("auto_backup_enabled"),
                            "last_run": p.get("last_run"),
                            "last_status": p.get("last_status"),
                            "last_snapshot_id": p.get("last_snapshot_id")
                        }
                        for p in data
                    ]
                }
        except Exception as e:
            state["data_profiles"] = {"exists": True, "error": str(e)}
    else:
        state["data_profiles"] = {"exists": False}

    return state


def save_reports(state: dict):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(JSON_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    log(f">> JSON 상태 덤프 저장 완료: {JSON_OUTPUT_PATH}")

    # Generate Markdown Report
    ts_task = state["task_scheduler"]
    latest_snaps = state["repository_snapshots"].get("latest_snapshots", [])
    top_snap = latest_snaps[0] if latest_snaps else None

    md = f"""# G1-B 재부팅 전 시스템 정밀 상태 덤프 (Pre-Reboot State)

- **기록 시각 (ISO)**: `{state['timestamp_iso']}`
- **기록 타임스탬프**: `{state['timestamp_epoch']}`

---

## 1. Windows Task Scheduler (`BackupSystem_AutoBackup`)
- **등록 여부**: `{'✅ 등록됨' if ts_task.get('is_registered') else '❌ 미등록'}`
- **작업 상태 (Status)**: `{ts_task.get('status')}`
- **실행 계정 (Run As User)**: `{ts_task.get('run_as_user')}`
- **실행할 작업 (Task To Run)**: `{ts_task.get('task_to_run')}`
- **마지막 실행 시각**: `{ts_task.get('last_run_time')}`
- **마지막 결과 (Last Result)**: `{ts_task.get('last_result')}`
- **다음 실행 시각 (Next Run Time)**: `{ts_task.get('next_run_time')}`
- **관련 작업 등록 수**: `{len(state['matching_tasks'])}개` ({state['matching_tasks']})

---

## 2. 백그라운드 프로세스 & 포트 (8765)
- **TCP 포트 8765**: `{'✅ OPEN' if state['server_port_8765'].get('tcp_open') else '❌ CLOSED'}`
- **HTTP 응답 상태**: `{state['server_port_8765'].get('detail')}`
- **실행 중인 Python 프로세스**: `{len(state['python_processes'])}개`
"""
    for p in state["python_processes"]:
        md += f"  - PID `{p.get('ProcessId')}`: `{p.get('Name')}` ({p.get('CommandLine', '')[:100]})\n"

    md += f"""
---

## 3. 백업 저장소 (`D:\\MyBackup_Repository\\snapshots`)
- **총 스냅샷 수**: `{state['repository_snapshots'].get('total_count')}개`
- **최신 스냅샷**: `{top_snap.get('name') if top_snap else '없음'}`
  - 크기: `{top_snap.get('size_bytes', 0):,} 바이트`
  - 수정 시각: `{top_snap.get('mtime_iso') if top_snap else '-'}`
"""
    with open(REPORT_OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(md)
    log(f">> Markdown 리포트 저장 완료: {REPORT_OUTPUT_PATH}")


if __name__ == "__main__":
    state = collect_pre_reboot_state()
    save_reports(state)
    log(">> [Phase 1-1] 재부팅 전 사전 상태 수집이 완벽히 완료되었습니다.")
