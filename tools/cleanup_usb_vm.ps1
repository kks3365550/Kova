# cleanup_usb_vm.ps1
# Hyper-V Rescue USB Tester VM Teardown and Cleanup
[CmdletBinding()]
param(
    [string]$VmName = "USB_Rescue_Test",
    [string]$VhdxDir = "C:\HyperV_USB_Test"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "SilentlyContinue"

# 1. 관리자 권한 확인 및 자동 승격
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "[*] 정리 작업을 위해 관리자 권한으로 승격합니다..." -ForegroundColor Yellow
    Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -VmName `"$VmName`""
    exit
}

Import-Module Hyper-V -ErrorAction SilentlyContinue

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  🧹 Hyper-V 테스트 가상머신 및 임시 디스크 정리" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# 2. VM 종료 및 삭제
$existingVm = Get-VM -Name $VmName -ErrorAction SilentlyContinue
if ($existingVm) {
    Write-Host "[*] 가상머신 '$VmName' 종료 및 삭제 중..." -ForegroundColor White
    if ($existingVm.State -ne 'Off') {
        Stop-VM -Name $VmName -TurnOff -Force
        Start-Sleep -Seconds 1
    }
    Remove-VM -Name $VmName -Force
    Write-Host "    [OK] 가상머신 삭제 완료" -ForegroundColor Gray
} else {
    Write-Host "[*] 실행 중인 테스트 가상머신 없음" -ForegroundColor Gray
}

# 3. VHDX 디마운트 및 파일 삭제
$vhdxPath = Join-Path $VhdxDir "$VmName.vhdx"
if (Test-Path $vhdxPath) {
    Write-Host "[*] VHDX 디마운트 및 파일 삭제 중..." -ForegroundColor White
    Dismount-VHD -Path $vhdxPath -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    Remove-Item -Path $vhdxPath -Force -ErrorAction SilentlyContinue
}

# 4. 임시 디렉터리 정리
if (Test-Path $VhdxDir) {
    Remove-Item -Path $VhdxDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "    [OK] 임시 폴더($VhdxDir) 정리 완료" -ForegroundColor Gray
}

Write-Host "============================================================" -ForegroundColor Green
Write-Host "  🎉 가상머신 및 임시 파일이 깔끔하게 정리되었습니다!" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
