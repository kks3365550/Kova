# revert_and_check.ps1
$ErrorActionPreference = 'Stop'

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  [*] Reverting VM to Baseline_Clean and Checking Free Space" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

$vm = Get-VM | Select-Object -First 1
Write-Host "Target VM: $($vm.Name) ($($vm.State))"

if ($vm.State -ne 'Off') {
    Write-Host "Turning off VM..."
    Stop-VM $vm -TurnOff
    Start-Sleep -Seconds 3
}

Write-Host "Reverting to Baseline_Clean..."
Restore-VMSnapshot -VMName $vm.Name -Name 'Baseline_Clean' -Confirm:$false
Start-Sleep -Seconds 3

$vhdPath = (Get-VM).HardDrives[0].Path
Write-Host "VHD Path after revert: $vhdPath"

$mount = Mount-VHD -Path $vhdPath -PassThru
Start-Sleep -Seconds 3

$part = Get-Partition -DiskNumber $mount.DiskNumber | Where-Object DriveLetter | Select-Object -First 1
$d = $part.DriveLetter
Write-Host "Mounted on ${d}:" -ForegroundColor Green

try {
    $driveInfo = Get-PSDrive $d
    $freeGb = [math]::Round($driveInfo.Free / 1GB, 2)
    $usedGb = [math]::Round($driveInfo.Used / 1GB, 2)
    Write-Host "`n[Baseline Clean Drive ${d}: Capacity]" -ForegroundColor Yellow
    Write-Host "  Free Space: ${freeGb} GB" -ForegroundColor Green
    Write-Host "  Used Space: ${usedGb} GB" -ForegroundColor Cyan

} finally {
    Write-Host "`nDismounting VHD..."
    Dismount-VHD -Path $vhdPath
    Write-Host "[OK] Dismounted." -ForegroundColor Green
}
