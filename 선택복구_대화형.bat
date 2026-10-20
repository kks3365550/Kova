@echo off
chcp 65001 >nul
title Kova Disaster Recovery - Interactive Selective Restore

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [*] Elevating to Administrator...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b 0
)

cd /d "%~dp0"
python emergency_restore.py
pause
exit /b 0
