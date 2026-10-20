import os
import sys
import subprocess
from typing import Dict, Any, Optional

TASK_NAME = "BackupSystem_AutoBackup"

def get_python_executable() -> str:
    """Finds the best Python executable, prioritizing the project's own virtualenv."""
    proj_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    candidates = [
        os.path.join(proj_dir, ".venv", "Scripts", "pythonw.exe"),
        os.path.join(proj_dir, ".venv", "Scripts", "python.exe"),
        os.path.join(proj_dir, "emergency_restore", "python", "python.exe"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c

    # Fallback to current runtime
    py_dir = os.path.dirname(sys.executable)
    pythonw = os.path.join(py_dir, "pythonw.exe")
    if os.path.exists(pythonw):
        return pythonw
    return sys.executable

def _run_schtasks_cmd(cmd_list: list, timeout: int = 15) -> tuple[int, str, str]:
    """Safely executes schtasks with robust byte-level cp949/utf-8 decoding."""
    try:
        kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "timeout": timeout}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.run(cmd_list, **kwargs)
        out_bytes = proc.stdout or b""
        err_bytes = proc.stderr or b""

        # Try utf-8 first, fallback to cp949 with error replacement
        try:
            out_str = out_bytes.decode("utf-8")
        except UnicodeDecodeError:
            out_str = out_bytes.decode("cp949", errors="replace")

        try:
            err_str = err_bytes.decode("utf-8")
        except UnicodeDecodeError:
            err_str = err_bytes.decode("cp949", errors="replace")

        return proc.returncode, out_str.strip(), err_str.strip()
    except Exception as e:
        return -1, "", str(e)

def register_windows_scheduled_task(profile_id: Optional[str] = None, schedule_type: str = "daily", schedule_value: str = "09:00") -> Dict[str, Any]:
    """
    Registers or updates a scheduled task in Windows Task Scheduler (schtasks.exe).
    This allows Windows OS to wake up and run the backup without needing Python to run in the background 24/7!
    """
    if not sys.platform.startswith("win"):
        return {"success": False, "error": "Windows OS is required for Task Scheduler."}

    py_exe = get_python_executable()
    cli_py = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "cli_backup.py"))
    
    task_run_cmd = f'"{py_exe}" "{cli_py}"'
    if profile_id:
        task_run_cmd += f' --profile "{profile_id}"'

    cmd = ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", task_run_cmd, "/F"]

    if schedule_type == "daily":
        time_str = schedule_value.strip() if ":" in schedule_value else f"{int(schedule_value):02d}:00"
        cmd.extend(["/SC", "DAILY", "/ST", time_str])
    elif schedule_type == "interval_hours":
        try:
            hours = int(float(schedule_value))
            if hours >= 24:
                cmd.extend(["/SC", "DAILY", "/ST", "09:00"])
            else:
                cmd.extend(["/SC", "HOURLY", "/MO", str(hours)])
        except ValueError:
            cmd.extend(["/SC", "DAILY", "/ST", "09:00"])
    else:
        cmd.extend(["/SC", "DAILY", "/ST", "09:00"])

    code, out, err = _run_schtasks_cmd(cmd, timeout=15)
    if code == 0:
        return {
            "success": True,
            "task_name": TASK_NAME,
            "command": task_run_cmd,
            "schedule_type": schedule_type,
            "schedule_value": schedule_value,
            "message": f"Windows 작업 스케줄러에 등록 완료 ({schedule_type} {schedule_value})"
        }
    else:
        return {
            "success": False,
            "error": err or out or "작업 스케줄러 등록 실패"
        }

def unregister_windows_scheduled_task() -> Dict[str, Any]:
    """Deletes the task from Windows Task Scheduler."""
    if not sys.platform.startswith("win"):
        return {"success": False, "error": "Windows OS required."}

    cmd = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    code, out, err = _run_schtasks_cmd(cmd, timeout=10)
    return {
        "success": code == 0,
        "message": "Windows 작업 스케줄러에서 등록 해제되었습니다." if code == 0 else (err or out)
    }

def get_windows_scheduled_task_status() -> Dict[str, Any]:
    """Queries current task status from Windows Task Scheduler."""
    if not sys.platform.startswith("win"):
        return {"registered": False, "status": "Not Windows"}

    cmd = ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"]
    code, out, err = _run_schtasks_cmd(cmd, timeout=10)
    if code == 0:
        lines = out.splitlines()
        info = {}
        for line in lines:
            if ":" in line:
                parts = line.split(":", 1)
                info[parts[0].strip()] = parts[1].strip()
        
        return {
            "registered": True,
            "task_name": TASK_NAME,
            "status": info.get("상태", info.get("작업 상태", info.get("Status", "등록됨"))),
            "next_run": info.get("다음 실행 시간", info.get("Next Run Time", "-")),
            "last_run": info.get("마지막 실행 시간", info.get("Last Run Time", "-")),
            "last_result": info.get("마지막 결과", info.get("Last Result", "-")),
            "raw": info
        }
    else:
        return {"registered": False, "status": "미등록"}
