# inspect_vhd_space.ps1
$ErrorActionPreference = 'Stop'

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  [*] Inspecting VHD Disk Space on Host" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

$vm = Get-VM | Select-Object -First 1
Write-Host "Target VM: $($vm.Name) ($($vm.State))"

if ($vm.State -eq 'Running') {
    Write-Host "Stopping VM (Stop-VM -Save)..."
    Stop-VM $vm -Save
    Start-Sleep -Seconds 2
}

$vhdPath = $vm.HardDrives.Path
Write-Host "VHD Path: $vhdPath"

$mount = Mount-VHD -Path $vhdPath -PassThru
Start-Sleep -Seconds 3

$part = Get-Partition -DiskNumber $mount.DiskNumber | Where-Object DriveLetter | Select-Object -First 1
$d = $part.DriveLetter
Write-Host "Mounted on ${d}:" -ForegroundColor Green

try {
    $driveInfo = Get-PSDrive $d
    $freeGb = [math]::Round($driveInfo.Free / 1GB, 2)
    $usedGb = [math]::Round($driveInfo.Used / 1GB, 2)
    Write-Host "Current Capacity: Free ${freeGb} GB / Used ${usedGb} GB" -ForegroundColor Yellow

    Write-Host "`nTop root directories on ${d}:\"
    Get-ChildItem "${d}:\" -Force -ErrorAction SilentlyContinue | ForEach-Object {
        $name = $_.Name
        if ($_.PSIsContainer) {
            $sz = (Get-ChildItem $_.FullName -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
            [PSCustomObject]@{
                Type = "DIR"
                Name = $name
                SizeGB = [math]::Round($sz / 1GB, 2)
            }
        } else {
            [PSCustomObject]@{
                Type = "FILE"
                Name = $name
                SizeGB = [math]::Round($_.Length / 1GB, 2)
            }
        }
    } | Sort-Object SizeGB -Descending | Format-Table -AutoSize

    Write-Host "`nInspecting ${d}:\Users\kksjmj breakdown:"
    if (Test-Path "${d}:\Users\kksjmj") {
        Get-ChildItem "${d}:\Users\kksjmj" -Force -ErrorAction SilentlyContinue | ForEach-Object {
            $name = $_.Name
            if ($_.PSIsContainer) {
                $sz = (Get-ChildItem $_.FullName -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
                [PSCustomObject]@{
                    Name = $name
                    SizeGB = [math]::Round($sz / 1GB, 2)
                }
            }
        } | Sort-Object SizeGB -Descending | Format-Table -AutoSize
    }

} finally {
    Write-Host "`nDismounting VHD..."
    Dismount-VHD -Path $vhdPath
    Start-Sleep -Seconds 2
    Start-VM $vm
    Write-Host "VM restarted." -ForegroundColor Green
}
