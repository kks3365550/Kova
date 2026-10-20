import subprocess

DESKTOP_IP = "100.90.20.59"
DESKTOP_USER = "kksjmj"

def run_ssh_ps(ps_script):
    full_cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        f"{DESKTOP_USER}@{DESKTOP_IP}",
        "powershell", "-NoProfile", "-Command", ps_script
    ]
    
    try:
        res = subprocess.run(
            full_cmd, 
            capture_output=True, 
            text=True,
            timeout=30
        )
        print("STDOUT:\n", res.stdout.strip())
        if res.stderr:
            print("STDERR:\n", res.stderr.strip())
        return res
    except subprocess.TimeoutExpired:
        print("ERROR: SSH command timed out.")
        return None
    except Exception as e:
        print(f"ERROR: {e}")
        return None

if __name__ == "__main__":
    ps = """
    $dir = 'C:\\Program Files\\백업시스템'
    $py = Join-Path $dir 'python\\python.exe'
    $run = Join-Path $dir 'run.py'
    Write-Host "AppDir: $dir"
    Write-Host "Python exists: $(Test-Path $py)"
    Write-Host "Run.py exists: $(Test-Path $run)"
    Start-Process -FilePath $py -ArgumentList $run -WorkingDirectory $dir -WindowStyle Hidden
    Start-Sleep -Seconds 4
    Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue | Format-Table -AutoSize
    """
    run_ssh_ps(ps)
