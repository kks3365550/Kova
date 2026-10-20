import subprocess
import time

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

if __name__ == "__main__":
    ps = (
        "$vbs = Get-ChildItem -Path 'C:\\Program Files' -Filter 'start_silent.vbs' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1; "
        "$dir = $vbs.DirectoryName; "
        "$pyw = Join-Path $dir 'python\\pythonw.exe'; "
        "$run = Join-Path $dir 'run.py'; "
        "Start-Process -FilePath $pyw -ArgumentList $run -WorkingDirectory $dir; "
        "Start-Sleep -Seconds 4; "
        "Get-Process -Name 'pythonw' | Select-Object Id, ProcessName, Path | Format-Table -AutoSize"
    )
    run_ssh(f"powershell -NoProfile -Command \"{ps}\"")
