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

def main():
    # 1. Locate installed directory
    ps_find = (
        "$vbs = Get-ChildItem -Path 'C:\\Program Files' -Filter 'start_silent.vbs' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1; "
        "if ($vbs) { "
        "  $dir = $vbs.DirectoryName; "
        "  Write-Host 'Found app at:' $dir; "
        "  Get-ChildItem $dir | Select-Object Name; "
        "  if (Test-Path \"$dir\\logs\") { Get-ChildItem \"$dir\\logs\" | Select-Object Name }; "
        "  if (Test-Path \"$dir\\logs\\startup_error.log\") { Write-Host '--- startup_error.log ---'; Get-Content \"$dir\\logs\\startup_error.log\" }; "
        "  if (Test-Path \"$dir\\logs\\server.log\") { Write-Host '--- server.log ---'; Get-Content \"$dir\\logs\\server.log\" -Tail 20 }; "
        "} else { "
        "  Write-Host 'start_silent.vbs not found in C:\\Program Files'; "
        "}"
    )
    run_ssh(f"powershell -NoProfile -Command \"{ps_find}\"")

    # 2. Test running python on desktop to see if python-multipart is importable
    ps_test_py = (
        "$vbs = Get-ChildItem -Path 'C:\\Program Files' -Filter 'start_silent.vbs' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1; "
        "if ($vbs) { "
        "  $py = Join-Path $vbs.DirectoryName 'python\\python.exe'; "
        "  if (Test-Path $py) { "
        "    & $py -c 'import fastapi, uvicorn, zstandard, multipart; print(\"ALL_MODULES_OK\")'; "
        "  } else { "
        "    Write-Host 'Embedded python not found at' $py; "
        "  } "
        "}"
    )
    run_ssh(f"powershell -NoProfile -Command \"{ps_test_py}\"")

    # 3. Start via start_silent.vbs properly
    ps_start = (
        "$vbs = Get-ChildItem -Path 'C:\\Program Files' -Filter 'start_silent.vbs' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1; "
        "if ($vbs) { "
        "  $dir = $vbs.DirectoryName; "
        "  Start-Process -FilePath 'wscript.exe' -ArgumentList ('`\"{0}`\"' -f $vbs.FullName) -WorkingDirectory $dir; "
        "  Start-Sleep -Seconds 4; "
        "  netstat -ano | findstr 8765; "
        "  try { "
        "    $res = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/system/release-info' -TimeoutSec 5; "
        "    Write-Host 'Local API:' ($res | ConvertTo-Json -Compress); "
        "  } catch { "
        "    Write-Host 'Local API error:' $_; "
        "  } "
        "}"
    )
    run_ssh(f"powershell -NoProfile -Command \"{ps_start}\"")

if __name__ == "__main__":
    main()
