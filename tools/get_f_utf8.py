import subprocess
import base64
import json

DESKTOP_IP = "100.90.20.59"
DESKTOP_USER = "kksjmj"

def main():
    # Retrieve base64 encoded names so there is zero character encoding corruption
    ps_cmd = "Get-ChildItem -Path 'F:\\' | ForEach-Object { [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($_.FullName)) }"
    full_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", f"{DESKTOP_USER}@{DESKTOP_IP}", f"powershell -NoProfile -Command \"{ps_cmd}\""]
    res = subprocess.run(full_cmd, capture_output=True, text=True)
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            name = base64.b64decode(line).decode('utf-16le')
            print(name)
        except Exception as e:
            print("ERR:", line, e)

if __name__ == "__main__":
    main()
