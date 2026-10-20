@echo off
chcp 65001 >nul
title Google Drive OAuth Authentication
cd /d "%~dp0"
where python >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo Python not found in PATH!
    pause
    exit /b 1
)
python tools\authenticate_gdrive.py
pause
exit /b 0
