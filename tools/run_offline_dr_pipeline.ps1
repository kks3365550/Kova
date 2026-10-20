# run_offline_dr_pipeline.ps1 - Final Enterprise VHD DR Restore & Scoring Pipeline
$ErrorActionPreference = 'Stop'

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  [*] Final Enterprise VHD DR Restore & Scoring Starting" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# 1. Extract repo.tar on Host F:\ (if not already extracted)
Write-Host "`n[Step 1/6] Preparing Backup Repository on Host F:\ ..."
if (-not (Test-Path "F:\MyBackup_Repository\blobs")) {
    Write-Host "  Extracting F:\repo.tar into F:\ ... (approx. 20s)"
    tar.exe -xf "F:\repo.tar" -C "F:\"
    Write-Host "  [OK] Host Repository Extracted successfully." -ForegroundColor Green
} else {
    Write-Host "  [OK] Host Repository already ready on F:\MyBackup_Repository." -ForegroundColor Green
}

# 2. Stop VM
Write-Host "`n[Step 2/6] Ensuring VM is stopped..."
$targetVM = Get-VM | Select-Object -First 1
if ($targetVM.State -ne 'Off') {
    Write-Host "  Saving VM State (Stop-VM -Save)..."
    Stop-VM $targetVM -Save
    Start-Sleep -Seconds 2
} else {
    Write-Host "  VM is already stopped." -ForegroundColor Green
}

# 3. Mount VHD (Read-Write)
$vhdPath = (Get-VM).HardDrives.Path
Write-Host "[Step 3/6] Mounting VHD: $vhdPath"
$mount = Mount-VHD -Path $vhdPath -PassThru
Start-Sleep -Seconds 3

$part = Get-Partition -DiskNumber $mount.DiskNumber | Where-Object DriveLetter | Select-Object -First 1
$d = $part.DriveLetter
Write-Host "  [OK] Mounted on ${d}:" -ForegroundColor Green

try {
    # Clean up incomplete repository or tar inside VHD to maximize guest disk space
    if (Test-Path "${d}:\repo.tar") {
        Remove-Item -Path "${d}:\repo.tar" -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path "${d}:\MyBackup_Repository") {
        Write-Host "  Cleaning incomplete repo inside VHD to free guest space..."
        Remove-Item -Path "${d}:\MyBackup_Repository" -Recurse -Force -ErrorAction SilentlyContinue
    }

    $driveInfo = Get-PSDrive $d
    Write-Host "  [Guest Drive ${d}: Available Space] Free: $([math]::Round($driveInfo.Free / 1GB, 2)) GB / Used: $([math]::Round($driveInfo.Used / 1GB, 2)) GB" -ForegroundColor Cyan

    # 4. Execute Disaster Recovery from Host Repo to VHD Target
    Write-Host "`n[Step 4/6] Executing disaster_recovery.py (Host Repo -> VHD ${d}:\)..."
    $pyScript = "F:\disaster_recovery.py"
    $repoDir = "F:\MyBackup_Repository"
    $destDir = "${d}:\"
    $logFile = "F:\dr_restore_log.txt"

    python "$pyScript" --repo "$repoDir" --restore snap_20260926_092057_7461cd --dest "$destDir" 2>&1 | Tee-Object -FilePath $logFile

    # 5. Run Verification Scorecard against VHD Restored Profile
    Write-Host "`n[Step 5/6] Running 3-Tier Verification Scorecard..."
    $scoreScript = "F:\MyBackup_Repository\baseline\verify_k12_dr_restore.ps1"
    $baselineJson = "F:\MyBackup_Repository\baseline\k12_baseline_20260926_004738.json"
    $targetProfile = "${d}:\Users\kksjmj"
    $scoreFile = "F:\dr_scorecard.txt"

    powershell -ExecutionPolicy RemoteSigned -File "$scoreScript" -BaselineJsonPath "$baselineJson" -TargetProfileDir "$targetProfile" 2>&1 | Tee-Object -FilePath $scoreFile

    # Also copy baseline and verification tools into VHD for in-VM self-verification
    New-Item -ItemType Directory -Path "${d}:\MyBackup_Repository\baseline" -Force | Out-Null
    Copy-Item -Path "F:\MyBackup_Repository\baseline\*" -Destination "${d}:\MyBackup_Repository\baseline\" -Force -ErrorAction SilentlyContinue

} finally {
    # 6. Dismount VHD and Start VM
    Write-Host "`n[Step 6/6] Dismounting VHD and Restarting VM..."
    Dismount-VHD -Path $vhdPath
    Start-Sleep -Seconds 2

    Start-VM (Get-VM)
    Write-Host "  [OK] VM Restarted." -ForegroundColor Green
}

Write-Host "`n============================================================" -ForegroundColor Cyan
Write-Host "  🎉 Final Enterprise VHD DR Restore & Verification COMPLETED!" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
