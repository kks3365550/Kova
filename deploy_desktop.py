import subprocess
import sys
import time
import os
import json
import urllib.request

DESKTOP_IP = "100.90.20.59"
DESKTOP_USER = "kksjmj"
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTALLER_LOCAL = os.path.join(BASE_DIR, "dist", "BackupSystem_Setup_v2.9.11.exe")
INSTALLER_REMOTE_TMP = r"C:\Users\kksjmj\AppData\Local\Temp\BackupSystem_Setup_v2.9.11.exe"
EXPECTED_SHA256 = "f7576904696242d56b20ae7ac1d7e69416045402831c3238c51102d5213c3126"

def run_ssh(cmd, check=True):
    full_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", f"{DESKTOP_USER}@{DESKTOP_IP}", cmd]
    print(f"\n>>> [SSH {DESKTOP_IP}]: {cmd}")
    res = subprocess.run(full_cmd, capture_output=True, text=True)
    if res.stdout:
        print(f"[STDOUT]\n{res.stdout.strip()}")
    if res.stderr:
        print(f"[STDERR]\n{res.stderr.strip()}")
    if check and res.returncode != 0:
        raise RuntimeError(f"SSH command failed with exit code {res.returncode}")
    return res

def main():
    print("=" * 60)
    print("  K12 -> RTX 5080 Desktop Remote Clean Reinstall Pipeline")
    print("=" * 60)

    # 1. Test SSH Connection
    print("\n[Stage 1] Verifying SSH connectivity to desktop...")
    r = run_ssh("powershell -NoProfile -Command \"whoami; hostname\"")
    print(f"Connected: {r.stdout.strip().replace(chr(10), ' | ')}")

    # 2. Terminate running backup processes and tasks
    print("\n[Stage 2] Terminating any existing backup processes and tasks...")
    kill_ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -in 'python.exe','pythonw.exe' -and ($_.CommandLine -like '*run.py*' -or $_.CommandLine -like '*백업*') } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
        "Unregister-ScheduledTask -TaskName 'BackupSystem_AutoBackup' -Confirm:$false -ErrorAction SilentlyContinue; "
        "Unregister-ScheduledTask -TaskName 'BackupSystem_WebServer' -Confirm:$false -ErrorAction SilentlyContinue; "
        "Write-Host 'Processes and Tasks stopped.'"
    )
    run_ssh(f"powershell -NoProfile -Command \"{kill_ps}\"")

    # 3. Uninstall previous Inno Setup installation
    print("\n[Stage 3] Checking for previous installation and uninstalling...")
    uninst_ps = (
        "$uninstPaths = @('HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall', 'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall'); "
        "foreach ($path in $uninstPaths) { "
        "  if (Test-Path $path) { "
        "    Get-ChildItem $path | ForEach-Object { "
        "      $props = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue; "
        "      if ($props.DisplayName -like '*백업*' -or $props.DisplayName -like '*BackupSystem*') { "
        "        $un = $props.UninstallString; "
        "        if ($un) { "
        "          Write-Host 'Found registered uninstaller:' $un; "
        "          Start-Process -FilePath $un -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART' -Wait; "
        "          Write-Host 'Uninstalled cleanly.'; "
        "        } "
        "      } "
        "    } "
        "  } "
        "}; "
        "Get-ChildItem -Path \"$env:USERPROFILE\\Desktop\" -Filter '*백업*.lnk' -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue; "
        "Write-Host 'Cleaned old desktop shortcuts.'"
    )
    run_ssh(f"powershell -NoProfile -Command \"{uninst_ps}\"")

    # 4. Transfer v2.9.11 installer via SCP
    print(f"\n[Stage 4] Transferring {os.path.basename(INSTALLER_LOCAL)} via SCP...")
    scp_cmd = [
        "scp", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
        INSTALLER_LOCAL,
        f"{DESKTOP_USER}@{DESKTOP_IP}:{INSTALLER_REMOTE_TMP.replace(chr(92), '/')}"
    ]
    t0 = time.time()
    res_scp = subprocess.run(scp_cmd, capture_output=True, text=True)
    if res_scp.returncode != 0:
        raise RuntimeError(f"SCP failed: {res_scp.stderr}")
    print(f"Transfer complete in {time.time() - t0:.2f}s!")

    # 5. Verify remote SHA-256
    print("\n[Stage 5] Verifying SHA-256 on remote Desktop...")
    hash_ps = f"(Get-FileHash '{INSTALLER_REMOTE_TMP}' -Algorithm SHA256).Hash"
    r_hash = run_ssh(f"powershell -NoProfile -Command \"{hash_ps}\"")
    remote_sha = r_hash.stdout.strip().lower()
    print(f"Remote SHA256: {remote_sha}")
    if remote_sha != EXPECTED_SHA256:
        raise ValueError(f"Hash mismatch! {remote_sha} != {EXPECTED_SHA256}")
    print("SHA-256 matches 100%!")

    # 6. Silent install
    print("\n[Stage 6] Executing silent installation on Desktop...")
    install_ps = (
        f"Start-Process -FilePath '{INSTALLER_REMOTE_TMP}' "
        "-ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /TASKS=\"desktopicon,startupicon\"' -Wait; "
        "Write-Host 'Installer execution completed.'"
    )
    run_ssh(f"powershell -NoProfile -Command \"{install_ps}\"")

    # 7. Start service on Desktop
    print("\n[Stage 7] Starting service on Desktop...")
    start_ps = (
        "$vbsFile = Get-ChildItem -Path 'C:\\Program Files' -Filter 'start_silent.vbs' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1; "
        "if ($vbsFile) { "
        "  $appDir = $vbsFile.DirectoryName; "
        "  Write-Host 'Found VBS at:' $vbsFile.FullName; "
        "  Start-Process -FilePath 'wscript.exe' -ArgumentList ('`\"{0}`\"' -f $vbsFile.FullName) -WorkingDirectory $appDir; "
        "  Write-Host 'Service started.'; "
        "} else { "
        "  Write-Host 'Warning: start_silent.vbs not found in C:\\Program Files'; "
        "}"
    )
    run_ssh(f"powershell -NoProfile -Command \"{start_ps}\"")

    # 8. Poll remote HTTP
    print("\n[Stage 8] Polling Desktop HTTP (http://100.90.20.59:8765)...")
    for i in range(12):
        time.sleep(2)
        try:
            req = urllib.request.Request(f"http://{DESKTOP_IP}:8765/api/system/release-info")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode())
                    print(f"    SUCCESS! API response: {data}")
                    break
        except Exception as e:
            print(f"    Waiting... ({i+1}/12): {e}")

    # 9. Clean up remote temp installer
    print("\n[Stage 9] Cleaning up remote temp installer...")
    run_ssh(f"powershell -NoProfile -Command \"Remove-Item '{INSTALLER_REMOTE_TMP}' -Force -ErrorAction SilentlyContinue\"")

    print("\n" + "=" * 60)
    print("  DESKTOP CLEAN REINSTALL COMPLETED!")
    print("=" * 60)

if __name__ == "__main__":
    main()
