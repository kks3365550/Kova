import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import base64
import subprocess

script = r"""
Import-Module Hyper-V
$targetId = [System.Guid]"4bd21edb-b90c-482c-8427-7341b7add3cb"
$vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
Write-Host "VM: $($vm.Name) ($($vm.State))"
$hds = Get-VMHardDiskDrive -VM $vm
Write-Host "HDS count: $($hds.Count)"
foreach ($h in $hds) {
    Write-Host "Path: $($h.Path)"
}
"""

encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
cmd = ["ssh", "kksjmj@100.90.20.59", f"powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"]
res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
print(res.stdout)
if res.stderr:
    print(res.stderr)
