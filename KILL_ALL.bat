@echo off
setlocal EnableExtensions
title HILL ALL BACKUP AND DISM PROCESSES

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [*] Administrator privileges required. Requesting elevation...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath cmd.exe -ArgumentList '/c ``\"@"~ e0`"\`"' -Verb RunAs"
    exit /b 0
)

echo [*] Terminating Dism.exe processes...
taskkill /F /T /IM Dism.exe >nul 2>&1
taskkill /F /T /IM dism.exe >nul 2>&1

echo [*] Terminating pythonw.exe processes...
taskkill /F /T /IM pythonw.exe >nul 2>&1

echo [*] Stopping scheduled tasks...
schtasks.exe /End /TN "BackupSystem_AutoBackup" >nul 2>&1
schtasks.exe /End /TN "BackupSystem_WebServer" >nul 2>&1

echo [OK] All backup processes terminated successfully.
ping 127.0.0.1 -n 3 >nul

