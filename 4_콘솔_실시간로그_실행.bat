@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Kova - Console Realtime Log Viewer

echo ============================================================
echo   Kova Backup System - Console Realtime Logs
echo ============================================================
echo [*] Displaying real-time logs in this terminal.
echo [*] Press Ctrl+C to stop.
echo ============================================================
echo.

set "PY_EXE="
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY_EXE=%~dp0.venv\Scripts\python.exe"
) else (
    set "PY_EXE=python"
)

"%PY_EXE%" run.py --console
pause
exit /b 0
