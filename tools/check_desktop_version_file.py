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
        "$dir = 'C:\\Program Files\\백업시스템'; "
        "Write-Host '--- VERSION FILE ---'; "
        "Get-Content (Join-Path $dir 'VERSION'); "
        "Write-Host '--- INDEX.HTML LINE 44 ---'; "
        "Get-Content (Join-Path $dir 'web\\templates\\index.html') | Select-String 'header-version-badge'; "
        "Write-Host '--- APP.PY LINE 280 ---'; "
        "Get-Content (Join-Path $dir 'web\\app.py') | Select-String 'TemplateResponse'; "
    )
    run_ssh(f"powershell -NoProfile -Command \"{ps}\"")
