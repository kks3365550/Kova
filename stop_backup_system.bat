@echo off
chcp 65001 >nul
setlocal EnableExtensions

:: 1. Unconditionally terminate any process listening on port 8765
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr :8765 ^| findstr LISTENING') do (
    taskkill.exe /F /PID %%a >nul 2>&1
)

:: 2. Unconditionally terminate all BackupSystem related process trees
taskkill.exe /F /T /IM pythonw.exe >nul 2>&1
taskkill.exe /F /T /IM Kova.exe >nul 2>&1
taskkill.exe /F /T /IM BackupSystem.exe >nul 2>&1

:: 3. Stop Windows Scheduled Tasks if active
schtasks.exe /End /TN "BackupSystem_AutoBackup" >nul 2>&1
schtasks.exe /End /TN "BackupSystem_WebServer" >nul 2>&1
schtasks.exe /End /TN "BackupSystem_Server" >nul 2>&1
schtasks.exe /End /TN "BackupSystem_Server_Daemon" >nul 2>&1

:: 4. Wait for OS file handles to release
ping 127.0.0.1 -n 2 >nul

exit /b 0
