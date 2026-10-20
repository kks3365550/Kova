import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import subprocess

ps = r"""
Import-Module Hyper-V
Get-ChildItem 'C:\ProgramData\Microsoft\Windows\Virtual Hard Disks' -Include *.vhdx,*.avhdx -Recurse | ForEach-Object {
    $vhdInfo = $null
    try { $vhdInfo = Get-VHD $_.FullName -ErrorAction SilentlyContinue } catch {}
    [PSCustomObject]@{
        Name = $_.Name
        SizeGB = [math]::Round($_.Length / 1GB, 2)
        LastWrite = $_.LastWriteTime
        VhdType = if ($vhdInfo) { $vhdInfo.VhdType } else { "N/A" }
        ParentPath = if ($vhdInfo) { $vhdInfo.ParentPath } else { "N/A" }
    }
} | Format-Table -AutoSize
"""

import base64
encoded = base64.b64encode(ps.encode('utf-16le')).decode('ascii')
cmd = ["ssh", "kksjmj@100.90.20.59", f"powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"]
res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
print(res.stdout)
if res.stderr:
    print(res.stderr)
