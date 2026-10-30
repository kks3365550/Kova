# launch_usb_vm.ps1
param(
    [int]$DiskNumber = 3,
    [string]$VmName = "USB_Rescue_Test"
)

$logFile = "C:\Users\kksjmj\Desktop\ai\백업시스템\logs\vm_launch.log"
Start-Transcript -Path $logFile -Force

try {
    Write-Host "============================================================"
    Write-Host "  🛡️ [백업시스템] Hyper-V 물리 USB 부팅 실측 검증기"
    Write-Host "============================================================"

    # 1. 기존 동일 이름 VM 정리
    $existing = Get-VM -Name $VmName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "[*] 기존 $VmName 가상머신 강제 종료 및 삭제 중..."
        Stop-VM -Name $VmName -TurnOff -Force -ErrorAction SilentlyContinue
        Remove-VM -Name $VmName -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }

    # 2. 물리 USB 디스크 오프라인 설정 (Hyper-V 단독 패스스루용)
    Write-Host "[*] 물리 USB (디스크 $DiskNumber) 호스트 오프라인 전환 중..."
    Set-Disk -Number $DiskNumber -IsOffline $true
    Start-Sleep -Seconds 1

    # 3. UEFI (Gen2) 가상머신 생성
    Write-Host "[*] Gen2 (UEFI 64-bit) 가상머신 생성 중 (RAM: 4GB 정적)..."
    New-VM -Name $VmName -Generation 2 -MemoryStartupBytes 4GB | Out-Null
    Set-VMMemory -VMName $VmName -DynamicMemoryEnabled $false

    # 4. 물리 USB 패스스루 연결
    Write-Host "[*] SCSI 컨트롤러에 물리 USB(디스크 $DiskNumber) 패스스루 연결 중..."
    Add-VMHardDiskDrive -VMName $VmName -ControllerType SCSI -ControllerNumber 0 -ControllerLocation 0 -DiskNumber $DiskNumber

    # 5. UEFI 부팅 순서 및 Secure Boot 설정
    Write-Host "[*] UEFI 부팅 1순위를 USB로 설정 (Secure Boot: Microsoft Windows)..."
    $disk = Get-VMHardDiskDrive -VMName $VmName
    Set-VMFirmware -VMName $VmName -FirstBootDevice $disk -EnableSecureBoot On -SecureBootTemplate "MicrosoftWindows"

    # 6. 가상머신 시작
    Write-Host "[*] 가상머신($VmName) 부팅 시작..."
    Start-VM -Name $VmName

    # 7. 사용자 화면에 가상머신 연결 창 띄우기
    Write-Host "[*] vmconnect.exe 콘솔 창을 화면에 실행합니다..."
    Start-Process "vmconnect.exe" -ArgumentList "localhost", $VmName

    Write-Host "============================================================"
    Write-Host "  🎉 가상머신이 실행되었습니다! 부팅 화면을 확인하세요."
    Write-Host "============================================================"
} catch {
    Write-Host "[!] 오류 발생: $_"
} finally {
    Stop-Transcript
}
