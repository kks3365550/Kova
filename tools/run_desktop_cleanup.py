import subprocess
import sys
import time

DESKTOP_IP = "100.90.20.59"
DESKTOP_USER = "kksjmj"
REMOTE_SCRIPT_PATH = r"C:\Users\kksjmj\cleanup_F_drive.py"
LOCAL_SCRIPT_PATH = r"C:\Users\kksjmj\Desktop\ai\백업시스템\cleanup_F_drive.py"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]

def sync_script():
    scp_cmd = ["scp"] + SSH_OPTS + [LOCAL_SCRIPT_PATH, f"{DESKTOP_USER}@{DESKTOP_IP}:{REMOTE_SCRIPT_PATH.replace('\\', '/')}"]
    print("Syncing cleanup_F_drive.py to desktop...")
    try:
        res = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            print("SCP Failed:", res.stderr)
            return False
        print("Sync successful.")
        return True
    except subprocess.TimeoutExpired:
        print("SCP Timeout.")
        return False
    except Exception as e:
        print(f"SCP Error: {e}")
        return False

def run_remote(args_str):
    cli_arg = "" if args_str == "--execute" else args_str
    ps_cmd = f"$py = (where.exe python | Select-Object -First 1); if (-not $py) {{ $py = (Get-Item 'C:\\Program Files\\*\\python\\python.exe').FullName }}; if ($py) {{ & $py C:\\Users\\kksjmj\\cleanup_F_drive.py {cli_arg} }} else {{ Write-Error 'Python not found'; exit 1 }}"
    full_cmd = ["ssh"] + SSH_OPTS + [f"{DESKTOP_USER}@{DESKTOP_IP}", f"powershell -NoProfile -Command \"{ps_cmd}\""]
    print(f"Executing remote cleanup (mode: {args_str})...")
    try:
        res = subprocess.run(full_cmd, capture_output=True, text=True, encoding="cp949", errors="replace", timeout=300)
        print("STDOUT:")
        print(res.stdout)
        if res.stderr:
            print("STDERR:")
            print(res.stderr)
        return res.returncode
    except subprocess.TimeoutExpired:
        print("Remote execution timed out.")
        return 1
    except Exception as e:
        print(f"Remote execution error: {e}")
        return 1

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "--dry-run"
    if not sync_script():
        sys.exit(1)
    code = run_remote(mode)
    sys.exit(code)
