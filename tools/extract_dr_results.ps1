$ErrorActionPreference = 'Stop'
Write-Host "[1/6] VM 상태 저장(Stop-VM -Save)..."
Stop-VM (Get-VM) -Save

$vhdPath = (Get-VM).HardDrives.Path
Write-Host "[2/6] VHD 마운트 대상: $vhdPath"

Write-Host "[3/6] VHD 읽기 전용 마운트..."
$mount = Mount-VHD -Path $vhdPath -ReadOnly -PassThru
Start-Sleep -Seconds 3

$part = Get-Partition -DiskNumber $mount.DiskNumber | Where-Object DriveLetter | Select-Object -First 1
$driveLetter = $part.DriveLetter
Write-Host "[4/6] 마운트된 드라이브 문자: ${driveLetter}:"

$outDir = "F:\dr_extracted"
New-Item -ItemType Directory -Path $outDir -Force | Out-Null

$targets = @(
    "${driveLetter}:\dr_scorecard.txt",
    "${driveLetter}:\dr_restore_log.txt",
    "${driveLetter}:\dr_status.txt"
)

foreach ($t in $targets) {
    if (Test-Path $t) {
        Copy-Item -Path $t -Destination $outDir -Force
        Write-Host "  [OK] 복사됨: $t"
    } else {
        Write-Host "  [!] 없음: $t"
    }
}

$jsonFiles = Get-ChildItem -Path "${driveLetter}:\MyBackup_Repository\baseline" -Filter "k12_dr_scorecard_*.json" -ErrorAction SilentlyContinue
foreach ($j in $jsonFiles) {
    Copy-Item -Path $j.FullName -Destination $outDir -Force
    Write-Host "  [OK] JSON 복사됨: $($j.Name)"
}

Write-Host "[5/6] VHD 마운트 해제..."
Dismount-VHD -Path $vhdPath

Write-Host "[6/6] VM 다시 기동(Start-VM)..."
Start-VM (Get-VM)

Write-Host "=== 추출 완료! ==="
Get-ChildItem $outDir
