import subprocess
import json
import sys

DESKTOP_IP = "100.90.20.59"
DESKTOP_USER = "kksjmj"

def run_ssh(ps_script):
    # Wrap powershell command safely
    full_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", f"{DESKTOP_USER}@{DESKTOP_IP}", f"powershell -NoProfile -Command \"{ps_script}\""]
    res = subprocess.run(full_cmd, capture_output=True, text=True)
    return res.stdout, res.stderr

def main():
    print("=" * 60)
    print("  [Desktop 100.90.20.59] Inspecting Drive F:\\")
    print("=" * 60)

    # 1. List all top-level items on F:\
    print("\n--- [1] Top-level Folders & Files on F:\\ ---")
    ps1 = "Get-ChildItem -Path 'F:\\' | Select-Object Mode, Name, Length, LastWriteTime | Format-Table -AutoSize | Out-String -Width 120"
    stdout, stderr = run_ssh(ps1)
    if stdout:
        print(stdout)
    if stderr:
        print(f"[STDERR]: {stderr}")

    # 2. Search for backup / 백업 related items across F:\ (depth up to 3)
    print("\n--- [2] Searching for *backup* or *백업* on F:\\ (Depth 1-3) ---")
    ps2 = "Get-ChildItem -Path 'F:\\' -Depth 2 -ErrorAction SilentlyContinue | Where-Object { $_.Name -like '*backup*' -or $_.Name -like '*백업*' -or $_.Name -like '*MyBackup*' } | Select-Object FullName, LastWriteTime | Format-Table -AutoSize | Out-String -Width 140"
    stdout, stderr = run_ssh(ps2)
    if stdout:
        print(stdout)
    if stderr:
        print(f"[STDERR]: {stderr}")

    # 3. Check for specific python/repo artifacts on F:\
    print("\n--- [3] Checking for specific backup system artifacts (cli_backup.py, run.py, profiles.json, etc.) ---")
    ps3 = "Get-ChildItem -Path 'F:\\' -Recurse -Depth 3 -Filter '*backup*.py' -ErrorAction SilentlyContinue | Select-Object FullName, Length, LastWriteTime | Format-Table -AutoSize | Out-String -Width 140"
    stdout, stderr = run_ssh(ps3)
    if stdout:
        print(stdout)
    if stderr:
        print(f"[STDERR]: {stderr}")

    # 4. Check for setup exe files on F:\
    print("\n--- [4] Checking for setup installers or zip archives on F:\\ ---")
    ps4 = "Get-ChildItem -Path 'F:\\' -Recurse -Depth 2 -ErrorAction SilentlyContinue | Where-Object { $_.Extension -in '.exe', '.zip', '.7z' -and ($_.Name -like '*Backup*' -or $_.Name -like '*백업*') } | Select-Object FullName, Length, LastWriteTime | Format-Table -AutoSize | Out-String -Width 140"
    stdout, stderr = run_ssh(ps4)
    if stdout:
        print(stdout)
    if stderr:
        print(f"[STDERR]: {stderr}")

if __name__ == "__main__":
    main()
