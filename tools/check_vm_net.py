import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import subprocess
import base64

ps = r"""
Import-Module Hyper-V
$targetId = [System.Guid]"4bd21edb-b90c-482c-8427-7341b7add3cb"
$vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
Write-Host "VM State: $($vm.State)"
Write-Host "IP Addresses:"
$vm.NetworkAdapters | ForEach-Object {
    $_.IPAddresses | ForEach-Object { Write-Host " - $_" }
}
"""

encoded = base64.b64encode(ps.encode('utf-16le')).decode('ascii')
cmd = ["ssh", "kksjmj@100.90.20.59", f"powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"]
res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
print(res.stdout)
if res.stderr:
    print(res.stderr)
