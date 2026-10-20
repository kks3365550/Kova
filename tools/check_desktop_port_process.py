import subprocess

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
        "$p = (Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue).OwningProcess | Select-Object -Unique; "
        "Write-Host 'Port 8765 Owning PID:' $p; "
        "if ($p) { Get-Process -Id $p | Select-Object Id, ProcessName, Path | Format-List }"
    )
    run_ssh(f"powershell -NoProfile -Command \"{ps}\"")
