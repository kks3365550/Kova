@echo off
setlocal
title [BackupSystem] Cleanup Hyper-V Test VM

net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\cleanup_usb_vm.ps1"
pause
