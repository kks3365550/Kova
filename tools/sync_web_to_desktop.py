import subprocess
import os

DESKTOP_IP = "100.90.20.59"
DESKTOP_USER = "kksjmj"
BASE_DIR = r"C:\Users\kksjmj\Desktop\ai\백업시스템"

files = [
    (os.path.join(BASE_DIR, "web", "templates", "index.html"), "C:/Users/kksjmj/AppData/Local/Temp/index.html", r"web\templates\index.html"),
    (os.path.join(BASE_DIR, "web", "app.py"), "C:/Users/kksjmj/AppData/Local/Temp/app.py", r"web\app.py"),
    (os.path.join(BASE_DIR, "web", "static", "js", "backup_utils.js"), "C:/Users/kksjmj/AppData/Local/Temp/backup_utils.js", r"web\static\js\backup_utils.js"),
]

for src, tmp_dst, rel_dst in files:
    cmd = ["scp", "-o", "BatchMode=yes", src, f"{DESKTOP_USER}@{DESKTOP_IP}:{tmp_dst}"]
    print(f"SCP: {os.path.basename(src)} -> Temp")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"  SCP FAIL: {res.stderr}")
        continue
    
    ps_cmd = (
        "$dir = (Get-ChildItem -Path 'C:\\Program Files' -Filter 'start_silent.vbs' -Recurse | Select-Object -First 1).DirectoryName; "
        f"$target = Join-Path $dir '{rel_dst}'; "
        f"Copy-Item '{tmp_dst}' $target -Force; "
        f"Remove-Item '{tmp_dst}' -Force; "
        "Write-Host 'Copied to:' $target"
    )
    ssh_cmd = ["ssh", "-o", "BatchMode=yes", f"{DESKTOP_USER}@{DESKTOP_IP}", f"powershell -NoProfile -Command \"{ps_cmd}\""]
    res_ssh = subprocess.run(ssh_cmd, capture_output=True, text=True)
    if res_ssh.returncode == 0:
        print(f"  {res_ssh.stdout.strip()}")
    else:
        print(f"  FAIL: {res_ssh.stderr.strip()}")

# Restart task
restart_ps = (
    "Stop-ScheduledTask -TaskName 'BackupSystem_WebServer' -ErrorAction SilentlyContinue; "
    "Start-Sleep -Seconds 2; "
    "Start-ScheduledTask -TaskName 'BackupSystem_WebServer'; "
    "Start-Sleep -Seconds 3; "
    "Write-Host 'Restarted BackupSystem_WebServer'"
)
ssh_restart = ["ssh", "-o", "BatchMode=yes", f"{DESKTOP_USER}@{DESKTOP_IP}", f"powershell -NoProfile -Command \"{restart_ps}\""]
res_restart = subprocess.run(ssh_restart, capture_output=True, text=True)
print(f"Restart: {res_restart.stdout.strip()}")
print("Sync & Restart completed!")
