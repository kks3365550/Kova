import subprocess
import json

DESKTOP_IP = "100.90.20.59"
DESKTOP_USER = "kksjmj"

def run_ssh(cmd):
    full_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", f"{DESKTOP_USER}@{DESKTOP_IP}", cmd]
    print(f"\n>>> [SSH {DESKTOP_IP}]: {cmd}")
    res = subprocess.run(full_cmd, capture_output=True, text=True)
    if res.stdout:
        print(f"[STDOUT]\n{res.stdout.strip()}")
    if res.stderr:
        print(f"[STDERR]\n{res.stderr.strip()}")
    return res

def main():
    print("=" * 60)
    print("  Verifying Desktop (100.90.20.59) Installation Status")
    print("=" * 60)

    # 1. Process check
    print("\n[1] Checking pythonw processes on desktop...")
    run_ssh("powershell -NoProfile -Command \"Get-Process -Name 'pythonw','python' -ErrorAction SilentlyContinue | Select-Object Id, ProcessName, Path | Format-Table -AutoSize\"")

    # 2. Port check
    print("\n[2] Checking Port 8765 listening on desktop...")
    run_ssh("powershell -NoProfile -Command \"netstat -ano | findstr 8765\"")

    # 3. Local API check
    print("\n[3] Checking local API endpoint on desktop...")
    run_ssh("powershell -NoProfile -Command \"try { (Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/system/release-info' -TimeoutSec 5) | ConvertTo-Json -Compress } catch { Write-Host 'API Error:' $_ }\"")

    # 4. Check desktop shortcuts
    print("\n[4] Checking Desktop shortcuts on desktop...")
    run_ssh("powershell -NoProfile -Command \"Get-ChildItem -Path $env:USERPROFILE\\Desktop -Filter '*백업*' | Select-Object Name, Length, LastWriteTime | Format-Table -AutoSize\"")

    # 5. Check logs if exists
    print("\n[5] Checking Desktop logs...")
    run_ssh("powershell -NoProfile -Command \"$appDir = 'C:\\Program Files\\백업시스템'; if (Test-Path \"$appDir\\logs\\server.log\") { Get-Content \"$appDir\\logs\\server.log\" -Tail 15 } else { Write-Host 'No server.log yet' }\"")

if __name__ == "__main__":
    main()
