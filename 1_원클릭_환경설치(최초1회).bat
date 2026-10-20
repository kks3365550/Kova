@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Kova - One-Click Environment Setup

echo ============================================================
echo   [Kova Backup System] One-Click Automated Environment Setup
echo ============================================================
echo.

set "PY_CMD="
py -3 --version >nul 2>&1
if not errorlevel 1 set "PY_CMD=py -3"

if "!PY_CMD!"=="" (
    python --version >nul 2>&1
    if not errorlevel 1 set "PY_CMD=python"
)

if "!PY_CMD!"=="" (
    echo [ERROR] Python 3.9+ was not found on this system.
    echo Opening official Python download page...
    start https://www.python.org/downloads/
    echo.
    echo IMPORTANT: Make sure to check "Add python.exe to PATH" during install!
    pause
    exit /b 1
)

echo [1/3] Python detected: !PY_CMD!
echo.

set "TARGET_PY="
if exist ".venv\Scripts\python.exe" (
    echo [2/3] Virtual environment (.venv) already exists.
    set "TARGET_PY=%~dp0.venv\Scripts\python.exe"
) else (
    echo [2/3] Creating virtual environment (.venv)...
    !PY_CMD! -m venv .venv >nul 2>&1
    if exist ".venv\Scripts\python.exe" (
        echo     Virtual environment created successfully!
        set "TARGET_PY=%~dp0.venv\Scripts\python.exe"
    ) else (
        echo     Using system Python directly.
        set "TARGET_PY=!PY_CMD!"
    )
)
echo.

echo [3/3] Installing dependencies...
!TARGET_PY! -m pip install -r requirements.txt --quiet --no-warn-script-location
echo.

echo [*] Creating Desktop Shortcut...
!TARGET_PY! create_desktop_shortcut.py
echo.

echo ============================================================
echo   Setup completed successfully!
echo   Double click "Kova Dashboard" on your desktop to start.
echo ============================================================
echo.
pause
exit /b 0
