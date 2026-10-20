import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
import base64
import subprocess

def run_remote_ps(script_content: str):
    encoded = base64.b64encode(script_content.encode('utf-16le')).decode('ascii')
    cmd = [
        "ssh", "-o", "ConnectTimeout=15",
        "kksjmj@100.90.20.59",
        f"powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
    print("STDOUT:")
    print(res.stdout)
    if res.stderr:
        print("STDERR:")
        print(res.stderr)
    return res.returncode

script = r"""
Import-Module Hyper-V
$vm = Get-VM
Write-Host "VM Name: $($vm.Name)"
Write-Host "VM State: $($vm.State)"
Write-Host "AutoCheckpoints: $($vm.AutomaticCheckpointsEnabled)"
Write-Host "HardDrives:"
$vm.HardDrives | ForEach-Object { Write-Host " - Path: $($_.Path)" }
Write-Host "Snapshots:"
Get-VMSnapshot -VMName $vm.Name | ForEach-Object { Write-Host " - Snapshot: $($_.Name) ($($_.CreationTime))" }
"""

if __name__ == "__main__":
    sys.exit(run_remote_ps(script))
