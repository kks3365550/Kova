import subprocess
import time
import urllib.request
import json

DESKTOP_IP = "100.90.20.59"
DESKTOP_USER = "kksjmj"

def run_ssh(cmd):
    full_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", f"{DESKTOP_USER}@{DESKTOP_IP}", cmd]
    res = subprocess.run(full_cmd, capture_output=True, text=True)
    print(res.stdout)
    if res.stderr:
        print("ERR:", res.stderr)

if __name__ == "__main__":
    ps = (
        "$pids = (Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue).OwningProcess | Where-Object { $_ -gt 4 } | Select-Object -Unique; "
        "foreach ($p in $pids) { Write-Host 'Stopping PID:' $p; Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }; "
        "Start-Sleep -Seconds 2; "
        "Start-ScheduledTask -TaskName 'BackupSystem_WebServer'; "
        "Start-Sleep -Seconds 4; "
        "$newPid = (Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue).OwningProcess | Where-Object { $_ -gt 4 } | Select-Object -Unique; "
        "Write-Host 'New listening PID:' $newPid"
    )
    run_ssh(f"powershell -NoProfile -Command \"{ps}\"")

    try:
        req = urllib.request.Request(f"http://{DESKTOP_IP}:8765/api/system/release-info")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            print(f"API version response: {data}")
    except Exception as e:
        print(f"API request error: {e}")
