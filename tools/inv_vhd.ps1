
Import-Module Hyper-V -ErrorAction Stop

$targetId = [System.Guid]"4bd21edb-b90c-482c-8427-7341b7add3cb"
$vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
if ($vm.State -ne 'Off') {
    Stop-VM -VM $vm -TurnOff -Force
    Start-Sleep -Seconds 5
    $vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
}

$hd = Get-VMHardDiskDrive -VM $vm | Select-Object -First 1
$vhdPath = $hd.Path
Write-Host "VHD Path: $vhdPath"

$mount = Mount-VHD -Path $vhdPath -ReadOnly -PassThru
$diskNum = $mount.DiskNumber
Start-Sleep -Seconds 2

$targetPart = Get-Partition -DiskNumber $diskNum | Where-Object { $_.DriveLetter } | Sort-Object Size -Descending | Select-Object -First 1
$d = $targetPart.DriveLetter
$destDrive = "$($d):\"
Write-Host "Mounted as: $destDrive"

try {
    Write-Host "`n=== Free / Used Space ==="
    $di = Get-PSDrive $d
    Write-Host "Free: $([math]::Round($di.Free/1GB, 2)) GB / Used: $([math]::Round($di.Used/1GB, 2)) GB"

    Write-Host "`n=== Searching for 'kksjmj' on $destDrive ==="
    $foundKks = Get-ChildItem -Path $destDrive -Recurse -Filter "*kksjmj*" -ErrorAction SilentlyContinue | Select-Object -First 10
    if ($foundKks) {
        $foundKks | ForEach-Object { Write-Host "FOUND: $($_.FullName)" }
    } else {
        Write-Host "NOT FOUND: No item matching '*kksjmj*' on $destDrive"
    }

    Write-Host "`n=== Searching for 'hermes' on $destDrive ==="
    $foundHermes = Get-ChildItem -Path $destDrive -Recurse -Filter "*hermes*" -ErrorAction SilentlyContinue | Select-Object -First 10
    if ($foundHermes) {
        $foundHermes | ForEach-Object { Write-Host "FOUND: $($_.FullName)" }
    } else {
        Write-Host "NOT FOUND: No item matching '*hermes*' on $destDrive"
    }

    Write-Host "`n=== Searching for '백업시스템' on $destDrive ==="
    $foundBackup = Get-ChildItem -Path $destDrive -Recurse -Filter "*백업시스템*" -ErrorAction SilentlyContinue | Select-Object -First 10
    if ($foundBackup) {
        $foundBackup | ForEach-Object { Write-Host "FOUND: $($_.FullName)" }
    } else {
        Write-Host "NOT FOUND: No item matching '*백업시스템*' on $destDrive"
    }

    Write-Host "`n=== Recently modified files on $destDrive (Last 2 hours) ==="
    $twoHoursAgo = (Get-Date).AddHours(-2)
    $recentFiles = Get-ChildItem -Path $destDrive -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -gt $twoHoursAgo } | Select-Object -First 20
    Write-Host "Recent files count (sampled 20):"
    $recentFiles | ForEach-Object { Write-Host " - $($_.FullName) ($($_.LastWriteTime))" }

} finally {
    Dismount-VHD -Path $vhdPath -ErrorAction SilentlyContinue
    Write-Host "`nVHD Dismounted."
}
