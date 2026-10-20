"""
MAINTENANCE: Approved DR Test Artifacts & Legacy Remnants Cleanup for F: Drive.
Authorized by System Administrator after dry-run verification.
Strictly excludes: F:\\MyBackup_Repository, F:\\WindowsImageBackup, SteamLibrary, etc.
"""

import subprocess
import sys
import time

DESKTOP_IP = "100.90.20.59"
DESKTOP_USER = "kksjmj"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
LOCAL_SCRIPT = r"C:\Users\kksjmj\Desktop\ai\백업시스템\cleanup_F_drive.py"
REMOTE_SCRIPT = "C:/Users/kksjmj/cleanup_F_drive.py"

def run_ssh(ps_cmd):
    full_cmd = ["ssh"] + SSH_OPTS + [f"{DESKTOP_USER}@{DESKTOP_IP}", f"powershell -NoProfile -Command \"{ps_cmd}\""]
    res = subprocess.run(full_cmd, capture_output=True, text=True, errors="replace", timeout=300)
    return res

def main():
    print("=" * 60)
    print("  Executing Complete Storage Maintenance on Desktop F: Drive")
    print("=" * 60)

    # 1. Sync updated cleanup_F_drive.py
    print("[1/5] Syncing updated cleanup script to desktop...")
    scp_cmd = ["scp"] + SSH_OPTS + [LOCAL_SCRIPT, f"{DESKTOP_USER}@{DESKTOP_IP}:{REMOTE_SCRIPT}"]
    res_scp = subprocess.run(scp_cmd, capture_output=True, text=True)
    if res_scp.returncode != 0:
        print("SCP Failed:", res_scp.stderr)
        return 1
    print("Sync OK.")

    # 2. Stop WebServer task temporarily to release any file locks on F:\replication_queue.db
    print("\n[2/5] Pausing BackupSystem_WebServer on desktop...")
    ps_stop = (
        "Stop-ScheduledTask -TaskName 'BackupSystem_WebServer' -ErrorAction SilentlyContinue; "
        "$p = (Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue).OwningProcess | Select-Object -Unique; "
        "if ($p) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }; "
        "Start-Sleep -Seconds 2"
    )
    run_ssh(ps_stop)

    # 3. Execute cleanup_F_drive.py
    print("\n[3/5] Executing F: drive cleanup on desktop...")
    ps_clean = (
        "$py = (where.exe python | Select-Object -First 1); "
        "if (-not $py) { $py = (Get-Item 'C:\\Program Files\\*\\python\\python.exe').FullName }; "
        "& $py C:\\Users\\kksjmj\\cleanup_F_drive.py"
    )
    res_clean = run_ssh(ps_clean)
    print(res_clean.stdout)
    if res_clean.stderr:
        print("[STDERR]:", res_clean.stderr)

    # 4. Restart WebServer task
    print("\n[4/5] Resuming BackupSystem_WebServer on desktop...")
    ps_start = (
        "Start-ScheduledTask -TaskName 'BackupSystem_WebServer' -ErrorAction SilentlyContinue; "
        "Start-Sleep -Seconds 4; "
        "Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue | Select-Object LocalAddress, LocalPort, State, OwningProcess | Format-Table -AutoSize"
    )
    res_start = run_ssh(ps_start)
    print(res_start.stdout)
    if res_start.stderr:
        print("[STDERR]:", res_start.stderr)

    # 5. Final check
    print("\n[5/5] Checking final F: drive top-level directory listing...")
    ps_verify = "Get-ChildItem -Path 'F:\\' | Select-Object Mode, Name, Length, LastWriteTime | Format-Table -AutoSize | Out-String -Width 120"
    res_verify = run_ssh(ps_verify)
    print(res_verify.stdout)

    print("=" * 60)
    print("  Storage Maintenance Process Finished")
    print("=" * 60)
    return 0

if __name__ == "__main__":
    sys.exit(main())
