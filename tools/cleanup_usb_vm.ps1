# cleanup_usb_vm.ps1
param(
    [int]$DiskNumber = 3,
    [string]$VmName = "USB_Rescue_Test"
)

$ErrorActionPreference = "SilentlyContinue"

Write-Host "============================================================"
Write-Host "  🧹 Hyper-V 테스트 VM 및 디스크 복구"
Write-Host "============================================================"

# 1. VM 종료 및 삭제
Write-Host "[*] $VmName 가상머신 종료 및 삭제..."
Stop-VM -Name $VmName -TurnOff -Force
Remove-VM -Name $VmName -Force

# 2. 디스크 온라인 복구
Write-Host "[*] 물리 USB (디스크 $DiskNumber) 온라인 복구..."
Set-Disk -Number $DiskNumber -IsOffline $false
Start-Sleep -Seconds 1

Write-Host "[OK] 가상머신 삭제 및 USB 디스크 온라인 복귀 완료!"
