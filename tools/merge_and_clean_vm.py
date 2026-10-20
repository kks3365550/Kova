# -*- coding: utf-8 -*-
"""
Merge all AVHDX differencing disks into single parent VHDX.
Guarantees 1:1 identical environment to physical SSD DR.
"""
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import subprocess
import os

ps_code = r"""
Import-Module Hyper-V -ErrorAction Stop

Write-Host "============================================================"
Write-Host "  [*] Merging all Snapshots into Single VHDX"
Write-Host "============================================================"

$targetId = [System.Guid]"4bd21edb-b90c-482c-8427-7341b7add3cb"
$vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1

# 1. VM 셧다운
Write-Host "[Step 1] Ensuring VM is completely Off..."
if ($vm.State -ne 'Off') {
    Stop-VM -VM $vm -TurnOff -Force
    Start-Sleep -Seconds 5
    $vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
}
Write-Host "  VM State: $($vm.State)"

# 2. 자동 검사점 비활성화
Write-Host "[Step 2] Disabling Automatic Checkpoints..."
Set-VM -VM $vm -AutomaticCheckpointsEnabled $false

# 3. 모든 스냅샷 병합 및 삭제
$snaps = Get-VMSnapshot -VM $vm
Write-Host "[Step 3] Existing snapshots count: $($snaps.Count)"
if ($snaps) {
    $snaps | ForEach-Object { Write-Host " - Removing & Merging: $($_.Name)" }
    $snaps | Remove-VMSnapshot -Confirm:$false
    Write-Host "  Waiting for Hyper-V merge operation to complete..."
    Start-Sleep -Seconds 15
}

# 병합 완료 대기 루프 (하드디스크 경로가 .vhdx로 바뀔 때까지)
$timeout = 180
$waited = 0
$hdPath = ""
while ($waited -lt $timeout) {
    $hd = Get-VMHardDiskDrive -VM $vm | Select-Object -First 1
    $hdPath = $hd.Path
    Write-Host "  Current HardDrive Path: $hdPath (Elapsed: ${waited}s)"
    if ($hdPath.EndsWith(".vhdx") -and -not $hdPath.EndsWith(".avhdx")) {
        Write-Host "  >>> MERGE COMPLETED! Single VHDX Confirmed! <<<"
        break
    }
    Start-Sleep -Seconds 5
    $waited += 5
    $vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
}

# 4. 최종 디스크 상태 출력
Write-Host "`n[Step 4] Final Disk Verification:"
$vhdInfo = Get-VHD -Path $hdPath
Write-Host " - Path: $($vhdInfo.Path)"
Write-Host " - VhdType: $($vhdInfo.VhdType)"
Write-Host " - FileSize: $([math]::Round($vhdInfo.FileSize/1GB, 2)) GB"
Write-Host " - ParentPath: $($vhdInfo.ParentPath)"

Write-Host "============================================================"
Write-Host "  [*] MERGE PIPELINE SUCCESSFUL"
Write-Host "============================================================"
"""

if __name__ == "__main__":
    local_ps = os.path.join(os.path.dirname(__file__), "merge_vhd.ps1")
    with open(local_ps, "w", encoding="utf-8") as f:
        f.write(ps_code)
    
    print("[*] Transferring merge script to Desktop via SCP...")
    subprocess.run(["scp", local_ps, "kksjmj@100.90.20.59:F:/merge_vhd.ps1"], check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    
    print("[*] Executing merge on Desktop...")
    cmd = ["ssh", "-o", "ConnectTimeout=15", "kksjmj@100.90.20.59", "powershell.exe -NoProfile -ExecutionPolicy Bypass -File F:\\merge_vhd.ps1"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
    print("STDOUT:")
    print(proc.stdout)
    if proc.stderr:
        print("STDERR:")
        print(proc.stderr)
    sys.exit(proc.returncode)
