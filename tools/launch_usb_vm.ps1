# launch_usb_vm.ps1
# Hyper-V Gen2 (UEFI) VHDX 1:1 Mirroring Rescue USB Tester
[CmdletBinding()]
param(
    [string]$SourceDrive = "G:",
    [string]$VmName = "USB_Rescue_Test",
    [string]$VhdxDir = "C:\HyperV_USB_Test"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# 1. 관리자 권한 확인 및 자동 승격
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "[*] Hyper-V 제어를 위해 관리자 권한으로 승격합니다..." -ForegroundColor Yellow
    Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -SourceDrive `"$SourceDrive`" -VmName `"$VmName`""
    exit
}

Import-Module Hyper-V -ErrorAction Stop

function Write-Step {
    param([string]$Message)
    $time = Get-Date -Format "HH:mm:ss"
    Write-Host "[$time] $Message" -ForegroundColor Cyan
}

try {
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "  [백업시스템] Hyper-V VHDX 1:1 미러링 부팅 검증기" -ForegroundColor Green
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
    if (Test-Path $vhdxPath) {
        Dismount-VHD -Path $vhdxPath -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
        Remove-Item -Path $vhdxPath -Force -ErrorAction SilentlyContinue
    }

    # 4. 16GB Dynamic VHDX 생성
    Write-Step "2단계: 16GB Dynamic VHDX 생성 중 ($vhdxPath)..."
    New-VHD -Path $vhdxPath -SizeBytes 16GB -Dynamic | Out-Null
    Write-Host "    [OK] VHDX 생성 완료" -ForegroundColor Gray

    # 5. VHDX 마운트 및 파티션 구성 (GPT + FAT32)
    Write-Step "3단계: VHDX 마운트 및 GPT FAT32 파티션 구성 중..."
    $mountedVhd = Mount-VHD -Path $vhdxPath -Passthru
    Start-Sleep -Seconds 1
    $diskNum = $mountedVhd.DiskNumber

    Initialize-Disk -Number $diskNum -PartitionStyle GPT -ErrorAction Stop
    $part = New-Partition -DiskNumber $diskNum -UseMaximumSize -AssignDriveLetter -ErrorAction Stop
    Start-Sleep -Milliseconds 500

    # 할당된 드라이브 문자 획득 (누락 방지 안전장치)
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
    Write-Host "    [OK] 임시 드라이브($destDrive) FAT32 포맷 완료" -ForegroundColor Gray

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
        "/XD", "System Volume Information", "$RECYCLE.BIN",
        "/LOG:$robocopyLog"
    )
    $proc = Start-Process "robocopy.exe" -ArgumentList $copyArgs -Wait -PassThru -NoNewWindow
    if ($proc.ExitCode -ge 8) {
        throw "Robocopy 복제 실패 (ExitCode: $($proc.ExitCode)). 로그 확인: $robocopyLog"
    }
    Write-Host "    [OK] 1:1 미러링 복제 완료 (Robocopy ExitCode: $($proc.ExitCode))" -ForegroundColor Gray

    # 7. VHDX 마운트 해제
    Write-Step "5단계: VHDX 마운트 해제..."
    Start-Sleep -Seconds 1
    Dismount-VHD -Path $vhdxPath -ErrorAction Stop
    Start-Sleep -Seconds 1
    Write-Host "    [OK] VHDX 마운트 해제 완료" -ForegroundColor Gray

    # 8. Gen2 (UEFI) 가상머신 생성 및 구성
    Write-Step "6단계: Hyper-V Gen2 (UEFI 64-bit) 가상머신 생성 중..."
    New-VM -Name $VmName -Generation 2 -MemoryStartupBytes 4GB -VHDPath $vhdxPath | Out-Null
    Set-VMMemory -VMName $VmName -DynamicMemoryEnabled $false
    Set-VMProcessor -VMName $VmName -Count 4
    Set-VMFirmware -VMName $VmName -EnableSecureBoot On -SecureBootTemplate "MicrosoftWindows"
    $hdd = Get-VMHardDiskDrive -VMName $VmName
    Set-VMFirmware -VMName $VmName -FirstBootDevice $hdd
    Write-Host "    [OK] Gen2 VM 설정 완료 (4GB RAM, 4 vCPU, Secure Boot: Microsoft Windows)" -ForegroundColor Gray

    # 9. 가상머신 시작 및 화면 팝업
    Write-Step "7단계: 가상머신 시작 및 콘솔 화면(vmconnect) 실행..."
    Start-VM -Name $VmName
    Start-Sleep -Seconds 1
    Start-Process "vmconnect.exe" -ArgumentList "localhost", $VmName

    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "  가상머신이 실행되었습니다!" -ForegroundColor Green
    Write-Host "  잠시 후 가상머신 화면에서 Windows RE 로드 및" -ForegroundColor Yellow
    Write-Host "  '0초 긴급 복구 콘솔'이 자동 실행되는지 확인하세요." -ForegroundColor Yellow
    Write-Host "============================================================" -ForegroundColor Green
} catch {
    Write-Host "[!] 오류 발생: $_" -ForegroundColor Red
    if ($vhdxPath -and (Test-Path $vhdxPath)) {
        Dismount-VHD -Path $vhdxPath -ErrorAction SilentlyContinue
    }
    throw $_
}
