@echo off
setlocal EnableExtensions
title Windows Bare-Metal System Image Backup [VHDX]

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [*] Administrator privileges required. Requesting elevation...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath cmd.exe -ArgumentList '/c `"`"%~f0`"`"' -Verb RunAs"
    exit /b 0
)

cd /d "%~dp0"
echo ======================================================================
echo   Windows Bare-Metal System Image Backup (VHDX Block Copy)
echo ======================================================================
echo   Source: C: Drive (OS, EFI, Recovery)
echo   Target: D:\ (VHDX Image Backup)
echo   Engine: Microsoft Windows wbadmin native VHDX block stream
echo ======================================================================
echo.
echo [*] Starting wbadmin backup now...
wbadmin start backup -backupTarget:D: -include:C: -allCritical -quiet

if %errorlevel% equ 0 (
    echo.
    echo ======================================================================
    echo   [SUCCESS] Bare-Metal System Image Backup Completed!
    echo   Location: D:\WindowsImageBackup
    echo ======================================================================
) else (
    echo.
    echo [ERROR] Backup failed or was cancelled. Exit Code: %errorlevel%
)

echo.
pause
exit /b %errorlevel%
