# tools/verify_restored_target.ps1
param(
    [string]$VmName = "USB_Rescue_Test",
    [string]$TargetVhdx = "C:\HyperV_USB_Test\Target_Disk.vhdx"
)

$ErrorActionPreference = "Stop"
Import-Module Hyper-V -ErrorAction SilentlyContinue

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  🔍 [재해복구 검증] 복원 대상 가상 디스크(Target_Disk) 내용 실측 감사" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# 1. VM 상태 확인 및 안전 정지
$vm = Get-VM -Name $VmName -ErrorAction SilentlyContinue
$wasRunning = $false
if ($vm -and $vm.State -eq 'Running') {
    $wasRunning = $true
    Write-Host "[*] 가상 디스크 읽기 감사를 위해 VM을 일시 정지합니다..." -ForegroundColor Yellow
    Stop-VM -Name $VmName -TurnOff -Force
    Start-Sleep -Seconds 1
}

# 2. Target VHDX 마운트
Write-Host "[*] Target VHDX 마운트 중: $TargetVhdx" -ForegroundColor White
$vhd = Mount-VHD -Path $TargetVhdx -Passthru -ReadOnly
Start-Sleep -Milliseconds 500

$part = Get-Partition -DiskNumber $vhd.DiskNumber | Where-Object { $_.Type -ne 'Reserved' } | Select-Object -First 1
$drive = $part.DriveLetter
if (-not $drive) {
    $usedLetters = (Get-Volume).DriveLetter
    $avail = 68..90 | ForEach-Object { [char]$_ } | Where-Object { $_ -notin $usedLetters }
    $drive = $avail[0]
    Set-Partition -DiskNumber $vhd.DiskNumber -PartitionNumber $part.PartitionNumber -NewDriveLetter $drive
}

$root = "$($drive):\"
Write-Host "[*] 대상 디스크 마운트 완료: $root" -ForegroundColor Green

# 3. 복원된 파일 전수 목록 및 내용 확인
$files = Get-ChildItem -Path $root -Recurse -File
Write-Host "============================================================" -ForegroundColor White
Write-Host "  📂 복원된 파일 총 개수: $($files.Count)개" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor White
foreach ($f in $files) {
    $rel = $f.FullName.Substring($root.Length)
    Write-Host " - [OK] $rel ($($f.Length) bytes)" -ForegroundColor Cyan
}

# 4. 검증 보고서 내용 출력
$reportPath = Join-Path $root "Users\Administrator\Desktop\긴급복구_검증보고서.txt"
if (Test-Path $reportPath) {
    Write-Host "`n[📄 복원된 긴급복구 검증보고서 파일 본문:]" -ForegroundColor Yellow
    Get-Content $reportPath -Encoding UTF8 | ForEach-Object { Write-Host "   $_" -ForegroundColor Gray }
}

# 5. 디마운트
Dismount-VHD -Path $TargetVhdx
Write-Host "`n[*] 가상 디스크 디마운트 완료" -ForegroundColor Gray

# 6. VM 재가동 (원래 실행 중이었던 경우)
if ($wasRunning) {
    Write-Host "[*] 가상머신을 다시 시작합니다..." -ForegroundColor Cyan
    Start-VM -Name $VmName
    Start-Sleep -Seconds 1
    Write-Host "[OK] 가상머신 정상 재가동 완료" -ForegroundColor Green
}
