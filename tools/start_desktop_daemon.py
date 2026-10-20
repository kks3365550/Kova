import subprocess
import time
import base64
import urllib.request
import json

DESKTOP_IP = "100.90.20.59"
DESKTOP_USER = "kksjmj"

ps_script = r"""
$action = New-ScheduledTaskAction -Execute 'C:\Users\kksjmj\AppData\Local\Programs\백업시스템\python\pythonw.exe' -Argument 'run.py --silent' -WorkingDirectory 'C:\Users\kksjmj\AppData\Local\Programs\백업시스템'
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName 'BackupSystem_Daemon' -Action $action -Trigger $trigger -Force
Start-ScheduledTask -TaskName 'BackupSystem_Daemon'
"""

def main():
    encoded = base64.b64encode(ps_script.encode("utf-16le")).decode("ascii")
    print("Executing EncodedCommand on Desktop via SSH...")
    cmd = ["ssh", f"{DESKTOP_USER}@{DESKTOP_IP}", "powershell", "-NoProfile", "-EncodedCommand", encoded]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="cp949", errors="ignore")
    print("STDOUT:", res.stdout.strip())
    print("STDERR:", res.stderr.strip())

    print("\nWaiting for service to respond...")
    for i in range(10):
        time.sleep(2)
        try:
            url = f"http://{DESKTOP_IP}:8765/api/system/release-info"
            req = urllib.request.Request(url, headers={"User-Agent": "DaemonVerify/1.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                print(f"SUCCESS: Service is UP on Desktop! Version: {data.get('data', {}).get('version')}")
                return
        except Exception as e:
            print(f"Waiting ({i+1}/10)... {e}")

if __name__ == "__main__":
    main()
