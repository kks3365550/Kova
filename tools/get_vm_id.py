import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import base64
import subprocess

script = r"""
Import-Module Hyper-V
Get-VM | ForEach-Object {
    Write-Host "VM Name: [$($_.Name)]"
    Write-Host "VM Id: [$($_.Id)]"
}
"""

encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
cmd = ["ssh", "kksjmj@100.90.20.59", f"powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"]
res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
print(res.stdout)
if res.stderr:
    print(res.stderr)
