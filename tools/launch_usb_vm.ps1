# launch_usb_vm.ps1
# Hyper-V Gen2 (UEFI) VHDX 1:1 Mirroring Rescue USB Tester & End-to-End DR Verification
[CmdletBinding()]
param(
    [string]$SourceDrive = "G:",
    [string]$VmName = "USB_Rescue_Test",
    [string]$VhdxDir = "C:\HyperV_USB_Test"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# 1. 관리자 권한 확인 및 자동 승격 (-NoExit 로 창 유지)
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "[*] Hyper-V 제어를 위해 관리자 권한으로 승격합니다..." -ForegroundColor Yellow
    $absScript = "C:\Users\kksjmj\Desktop\ai\백업시스템\tools\launch_usb_vm.ps1"
    $parentDir = "C:\Users\kksjmj\Desktop\ai\백업시스템"
    Start-Process powershell.exe -Verb RunAs -WorkingDirectory $parentDir -ArgumentList "-NoProfile -ExecutionPolicy Bypass -NoExit -File `"$absScript`" -SourceDrive `"$SourceDrive`" -VmName `"$VmName`""
    exit
}

$transcriptPath = "C:\Users\kksjmj\Desktop\ai\백업시스템\logs\vm_launch_latest.log"
try { Start-Transcript -Path $transcriptPath -Force } catch {}

Import-Module Hyper-V -ErrorAction Stop

function Write-Step {
    param([string]$Message)
    $time = Get-Date -Format "HH:mm:ss"
    Write-Host "[$time] $Message" -ForegroundColor Cyan
}

try {
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "  🛡️ [백업시스템] Hyper-V VHDX 1:1 부팅 & 재해복구(DR) 검증기" -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green

    # 원본 드라이브 확인 (G: 또는 RESCUE_DR 라벨 탐색)
    $cleanSource = $SourceDrive.TrimEnd('\')
    if (-not (Test-Path "$cleanSource\sources\boot.wim")) {
        Write-Step "지정된 드라이브($cleanSource)에 boot.wim이 없습니다. RESCUE_DR 탐색 중..."
        $vol = Get-Volume | Where-Object { $_.FileSystemLabel -eq "RESCUE_DR" -and $_.DriveLetter } | Select-Object -First 1
        if ($vol) {
            $cleanSource = "$($vol.DriveLetter):"
            Write-Step "RESCUE_DR 드라이브 발견: $cleanSource"
        } else {
            throw "복구 USB 드라이브($cleanSource)를 찾을 수 없거나 제작되지 않았습니다."
        }
    }

    # 2. 기존 VM 정리
    Write-Step "1단계: 기존 가상머신($VmName) 확인 및 정리..."
    $existingVm = Get-VM -Name $VmName -ErrorAction SilentlyContinue
    if ($existingVm) {
        if ($existingVm.State -ne 'Off') {
            Stop-VM -Name $VmName -TurnOff -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 1
        }
        Remove-VM -Name $VmName -Force -ErrorAction SilentlyContinue
        Write-Host "    [OK] 기존 VM 정리 완료" -ForegroundColor Gray
    }

    # 3. 임시 폴더 및 VHDX 경로 준비
    if (-not (Test-Path $VhdxDir)) {
        New-Item -ItemType Directory -Path $VhdxDir -Force | Out-Null
    }
    $vhdxPath = Join-Path $VhdxDir "$VmName.vhdx"
    $repoVhdxPath = Join-Path $VhdxDir "Test_Repository.vhdx"
    $targetVhdxPath = Join-Path $VhdxDir "Target_Disk.vhdx"

    foreach ($vpath in @($vhdxPath, $repoVhdxPath, $targetVhdxPath)) {
        if (Test-Path $vpath) {
            Dismount-VHD -Path $vpath -ErrorAction SilentlyContinue
            Start-Sleep -Milliseconds 300
            Remove-Item -Path $vpath -Force -ErrorAction SilentlyContinue
        }
    }

    # 4. 16GB Dynamic Rescue Boot VHDX 생성
    Write-Step "2단계: 16GB Rescue Boot VHDX 생성 중 ($vhdxPath)..."
    New-VHD -Path $vhdxPath -SizeBytes 16GB -Dynamic | Out-Null
    Write-Host "    [OK] Boot VHDX 생성 완료" -ForegroundColor Gray

    # 5. VHDX 마운트 및 파티션 구성 (GPT + FAT32)
    Write-Step "3단계: Boot VHDX 마운트 및 GPT FAT32 파티션 구성 중..."
    $mountedVhd = Mount-VHD -Path $vhdxPath -Passthru
    Start-Sleep -Seconds 1
    $diskNum = $mountedVhd.DiskNumber

    Initialize-Disk -Number $diskNum -PartitionStyle GPT -ErrorAction Stop
    $part = New-Partition -DiskNumber $diskNum -UseMaximumSize -AssignDriveLetter -ErrorAction Stop
    Start-Sleep -Milliseconds 500

    $refreshedPart = Get-Partition -DiskNumber $diskNum | Where-Object { $_.PartitionNumber -eq $part.PartitionNumber }
    $driveLetter = $refreshedPart.DriveLetter
    if (-not $driveLetter) {
        $usedLetters = (Get-Volume).DriveLetter
        $availableLetters = 68..90 | ForEach-Object { [char]$_ } | Where-Object { $_ -notin $usedLetters }
        $driveLetter = $availableLetters[0]
        Set-Partition -DiskNumber $diskNum -PartitionNumber $part.PartitionNumber -NewDriveLetter $driveLetter
    }

    Format-Volume -DriveLetter $driveLetter -FileSystem FAT32 -NewFileSystemLabel "RESCUE_VM" -Force -Confirm:$false | Out-Null
    $destDrive = "$($driveLetter):\"
    Write-Host "    [OK] 부팅 드라이브($destDrive) FAT32 포맷 완료" -ForegroundColor Gray

    # 6. USB 내용 1:1 미러링 복사 (robocopy)
    Write-Step "4단계: USB($cleanSource) 부팅 파일 1:1 미러링 복사 중..."
    $robocopyLog = Join-Path $VhdxDir "robocopy.log"
    $copyArgs = @(
        "$cleanSource\",
        "$destDrive",
        "/MIR",
        "/R:1",
        "/W:1",
        "/MT:8",
        "/NP",
        "/XD", "System Volume Information", '$RECYCLE.BIN',
        "/LOG:$robocopyLog"
    )
    $proc = Start-Process "robocopy.exe" -ArgumentList $copyArgs -Wait -PassThru -NoNewWindow
    if ($proc.ExitCode -ge 8) {
        throw "Robocopy 복제 실패 (ExitCode: $($proc.ExitCode)). 로그 확인: $robocopyLog"
    }
    Write-Host "    [OK] 1:1 미러링 복제 완료 (Robocopy ExitCode: $($proc.ExitCode))" -ForegroundColor Gray

    # VHDX 마운트 해제
    Dismount-VHD -Path $vhdxPath -ErrorAction Stop
    Start-Sleep -Seconds 1
    Write-Host "    [OK] Boot VHDX 마운트 해제 완료" -ForegroundColor Gray

    # 7. 테스트 백업 저장소 VHDX 생성 및 초기화
    Write-Step "5단계: 백업 저장소 가상 디스크(Test_Repository.vhdx) 생성 중..."
    New-VHD -Path $repoVhdxPath -SizeBytes 2GB -Dynamic | Out-Null
    $mountedRepo = Mount-VHD -Path $repoVhdxPath -Passthru
    Start-Sleep -Seconds 1
    Initialize-Disk -Number $mountedRepo.DiskNumber -PartitionStyle GPT -ErrorAction Stop
    $repoPart = New-Partition -DiskNumber $mountedRepo.DiskNumber -UseMaximumSize -AssignDriveLetter -ErrorAction Stop
    Start-Sleep -Milliseconds 500

    $refreshedRepoPart = Get-Partition -DiskNumber $mountedRepo.DiskNumber | Where-Object { $_.PartitionNumber -eq $repoPart.PartitionNumber }
    $repoDriveLetter = $refreshedRepoPart.DriveLetter
    Format-Volume -DriveLetter $repoDriveLetter -FileSystem NTFS -NewFileSystemLabel "BACKUP_REPO" -Force -Confirm:$false | Out-Null

    $repoDir = "$($repoDriveLetter):\MyBackup_Repository"
    Write-Step "    - 테스트 백업 스냅샷 및 무결성 데이터 생성 중 ($repoDir)..."
    $pyExe = "C:\Users\kksjmj\Desktop\ai\백업시스템\.venv\Scripts\python.exe"
    $prepareScript = "C:\Users\kksjmj\Desktop\ai\백업시스템\tools\prepare_test_repo.py"
    $pyProc = Start-Process $pyExe -ArgumentList "`"$prepareScript`" `"$repoDir`"" -Wait -PassThru -NoNewWindow
    if ($pyProc.ExitCode -ne 0) {
        throw "테스트 백업 저장소 생성 실패 (ExitCode: $($pyProc.ExitCode))"
    }
    Dismount-VHD -Path $repoVhdxPath -ErrorAction Stop
    Start-Sleep -Seconds 1
    Write-Host "    [OK] 백업 저장소 VHDX 준비 완료" -ForegroundColor Gray

    # 8. 복구 대상(Target) 가상 디스크 생성
    Write-Step "6단계: 복원 대상 가상 디스크(Target_Disk.vhdx) 생성 중..."
    New-VHD -Path $targetVhdxPath -SizeBytes 5GB -Dynamic | Out-Null
    $mountedTarget = Mount-VHD -Path $targetVhdxPath -Passthru
    Start-Sleep -Seconds 1
    Initialize-Disk -Number $mountedTarget.DiskNumber -PartitionStyle GPT -ErrorAction Stop
    $targetPart = New-Partition -DiskNumber $mountedTarget.DiskNumber -UseMaximumSize -AssignDriveLetter -ErrorAction Stop
    Start-Sleep -Milliseconds 500

    $refreshedTargetPart = Get-Partition -DiskNumber $mountedTarget.DiskNumber | Where-Object { $_.PartitionNumber -eq $targetPart.PartitionNumber }
    $targetDriveLetter = $refreshedTargetPart.DriveLetter
    Format-Volume -DriveLetter $targetDriveLetter -FileSystem NTFS -NewFileSystemLabel "RESCUE_TARGET" -Force -Confirm:$false | Out-Null
    Dismount-VHD -Path $targetVhdxPath -ErrorAction Stop
    Start-Sleep -Seconds 1
    Write-Host "    [OK] 복원 대상 가상 디스크 준비 완료" -ForegroundColor Gray

    # 9. Gen2 (UEFI) 가상머신 생성 및 3중 디스크 연결
    Write-Step "7단계: Hyper-V Gen2 가상머신 구성 및 3중 가상 디스크 마운트..."
    New-VM -Name $VmName -Generation 2 -MemoryStartupBytes 4GB -VHDPath $vhdxPath | Out-Null
    Set-VMMemory -VMName $VmName -DynamicMemoryEnabled $false
    Set-VMProcessor -VMName $VmName -Count 4
    Set-VMFirmware -VMName $VmName -EnableSecureBoot On -SecureBootTemplate "MicrosoftWindows"

    # 컨트롤러 0의 1번, 2번 슬롯에 저장소 및 대상 디스크 연결
    Add-VMHardDiskDrive -VMName $VmName -ControllerType SCSI -ControllerNumber 0 -ControllerLocation 1 -Path $repoVhdxPath
    Add-VMHardDiskDrive -VMName $VmName -ControllerType SCSI -ControllerNumber 0 -ControllerLocation 2 -Path $targetVhdxPath

    $bootHdd = Get-VMHardDiskDrive -VMName $VmName | Where-Object { $_.ControllerLocation -eq 0 }
    Set-VMFirmware -VMName $VmName -FirstBootDevice $bootHdd
    Write-Host "    [OK] Gen2 VM 3중 디스크 구성 완료 (Boot USB, Backup Repo, Target Disk)" -ForegroundColor Gray

    # 10. 가상머신 시작 및 화면 팝업
    Write-Step "8단계: 가상머신 시작 및 콘솔 화면(vmconnect) 연결..."
    Start-VM -Name $VmName
    Start-Sleep -Seconds 1
    Start-Process "vmconnect.exe" -ArgumentList "localhost", $VmName

    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "  🎉 가상머신 및 DR 복구 테스트 환경이 완벽하게 가동되었습니다!" -ForegroundColor Green
    Write-Host "  💡 가상머신 콘솔에서 0초 자동 복구 콘솔이 구동되며," -ForegroundColor Yellow
    Write-Host "     백업 저장소 자동 탐색 -> 대상 디스크 복원이 진행됩니다." -ForegroundColor Yellow
    Write-Host "============================================================" -ForegroundColor Green
} catch {
    Write-Host "[!] 오류 발생: $_" -ForegroundColor Red
    foreach ($vpath in @($vhdxPath, $repoVhdxPath, $targetVhdxPath)) {
        if ($vpath -and (Test-Path $vpath)) {
            Dismount-VHD -Path $vpath -ErrorAction SilentlyContinue
        }
    }
    throw $_
} finally {
    try { Stop-Transcript } catch {}
}
