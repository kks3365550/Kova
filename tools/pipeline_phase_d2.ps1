# -*- coding: utf-8 -*-
Import-Module Hyper-V -ErrorAction Stop

Write-Host "=========================================================="
Write-Host " [Phase D-2] Cold Shutdown DR Restoration Pipeline"
Write-Host "=========================================================="

$vmId = "4bd21edb-b90c-482c-8427-7341b7add3cb"
$baselineSnapshotName = "Baseline_Clean"
$drScriptPath = "F:\disaster_recovery.py"
$drRepoPath = "F:\MyBackup_Repository"
$drSnapshotId = "snap_20260926_092057_7461cd"

# 1. VM 콜드 셧다운
$targetId = [System.Guid]"4bd21edb-b90c-482c-8427-7341b7add3cb"
$vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
if (-not $vm) {
    # 폴백: 단일 VM 시스템이므로 첫 번째 VM 선택
    $vm = (Get-VM)[0]
}
if (-not $vm) {
    Write-Error "No Hyper-V VM found on host!"
    exit 1
}

Write-Host "[Step 1] Target VM: $($vm.Name) (Id: $($vm.Id), State: $($vm.State))"
if ($vm.State -ne 'Off') {
    Write-Host "Shutting down VM completely (Stop-VM -TurnOff)..."
    Stop-VM -VM $vm -TurnOff -Force
    Start-Sleep -Seconds 5
    $vm = Get-VM -Id $vmId
}

# 2. 자동 검사점 비활성화 & 임시 자동 검사점 정리
Write-Host "[Step 2] Disabling Automatic Checkpoints..."
Set-VM -VM $vm -AutomaticCheckpointsEnabled $false

$autoSnaps = Get-VMSnapshot -VM $vm | Where-Object { $_.Name -ne $baselineSnapshotName }
if ($autoSnaps) {
    Write-Host "Removing non-baseline checkpoints ($($autoSnaps.Count) found)..."
    $autoSnaps | Remove-VMSnapshot -Confirm:$false
    Start-Sleep -Seconds 3
}

# 3. Baseline_Clean 복원
Write-Host "[Step 3] Restoring to snapshot '$baselineSnapshotName'..."
$baseSnap = Get-VMSnapshot -VM $vm | Where-Object { $_.Name -eq $baselineSnapshotName }
if (-not $baseSnap) {
    Write-Error "Baseline snapshot $baselineSnapshotName not found!"
    exit 1
}
Restore-VMSnapshot -VMSnapshot $baseSnap -Confirm:$false
Start-Sleep -Seconds 5

# 4. 대상 VHD 경로 조회 (복원 후 최신 경로)
$vm = (Get-VM)[0]
$targetVhd = $vm.HardDrives[0].Path
Write-Host "[Step 4] Target VHD Path: $targetVhd"

if (-not (Test-Path $targetVhd)) {
    Write-Error "Target VHD file does not exist: $targetVhd"
    exit 1
}

# 5. VHD 오프라인 마운트
Write-Host "[Step 5] Mounting VHD offline..."
$mounted = Mount-VHD -Path $targetVhd -Passthru
Start-Sleep -Seconds 3

# 6. 볼륨 드라이브 레터 확인
$diskNumber = $mounted.DiskNumber
Write-Host "VHD attached as Disk Number: $diskNumber"

# OS 파티션 (가장 큰 파티션) 찾기
$targetPartition = Get-Partition -DiskNumber $diskNumber | Where-Object { $_.DriveLetter } | Sort-Object Size -Descending | Select-Object -First 1
if (-not $targetPartition) {
    # 드라이브 레터가 할당되지 않은 경우 할당 시도
    $targetPartition = Get-Partition -DiskNumber $diskNumber | Where-Object { $_.Type -eq 'Basic' -or $_.Size -gt 10GB } | Sort-Object Size -Descending | Select-Object -First 1
    if ($targetPartition) {
        $freeLetter = (ls function:[d-z]: -n | ?{ !(test-path $_) })[0] -replace ':$',''
        Set-Partition -DiskNumber $diskNumber -PartitionNumber $targetPartition.PartitionNumber -NewDriveLetter $freeLetter
        $destDrive = "$($freeLetter):\"
    } else {
        Write-Error "Could not find a valid partition on disk $diskNumber"
        Dismount-VHD -Path $targetVhd
        exit 1
    }
} else {
    $destDrive = "$($targetPartition.DriveLetter):\"
}

Write-Host "[Step 6] Destination OS Drive: $destDrive (Partition Size: $([math]::Round($targetPartition.Size/1GB, 2)) GB)"

# 가용 공간 확인
$driveInfo = Get-PSDrive ($destDrive.Substring(0,1))
Write-Host "Free Space: $([math]::Round($driveInfo.Free/1GB, 2)) GB"

# 7. DR 복원 실행
Write-Host "[Step 7] Running disaster_recovery.py for snapshot $drSnapshotId..."
$restoreSw = [System.Diagnostics.Stopwatch]::StartNew()
$proc = Start-Process -FilePath "python" -ArgumentList "$drScriptPath --repo $drRepoPath --restore $drSnapshotId --dest $destDrive" -NoNewWindow -PassThru -Wait
$restoreSw.Stop()
Write-Host "Restoration process exited with code $($proc.ExitCode) in $([math]::Round($restoreSw.Elapsed.TotalSeconds, 2))s"

# 복원 직후 파일 존재 샘플 검증 (오프라인 상태)
Write-Host "[Step 7.1] Verifying restored files on offline VHD..."
$sampleHermesVenv = "$destDrive\Users\kksjmj\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"
$sampleUvPython   = "$destDrive\Users\kksjmj\AppData\Roaming\uv\python\cpython-3.11.11-windows-x86_64-none\install\python.exe"
$sampleProjects   = "$destDrive\Users\kksjmj\Desktop\ai"

Write-Host " - Hermes python.exe: $(Test-Path $sampleHermesVenv)"
Write-Host " - UV CPython python.exe: $(Test-Path $sampleUvPython)"
Write-Host " - Desktop AI Projects Dir: $(Test-Path $sampleProjects)"
if (Test-Path $sampleProjects) {
    $projCount = (Get-ChildItem $sampleProjects -Directory).Count
    Write-Host " - Desktop AI Projects Count: $projCount"
}

# 8. VHD 안전 언마운트
Write-Host "[Step 8] Dismounting VHD..."
Dismount-VHD -Path $targetVhd
Start-Sleep -Seconds 3
Write-Host "VHD safely dismounted."

# 9. VM 클린 기동 (콜드 부팅)
Write-Host "[Step 9] Starting VM for Cold Boot..."
Start-VM -VM $vm
Start-Sleep -Seconds 10

$vm = Get-VM -Name $vm.Name
Write-Host "VM State after start: $($vm.State)"
Write-Host "=========================================================="
Write-Host " [Phase D-2] FINISHED SUCCESSFULLY"
Write-Host "=========================================================="
