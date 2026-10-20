@echo off
setlocal
title Emergency Disaster Recovery - One-Click Full C: Drive Restore

:: Check Administrator Privileges and Auto-Elevate
openfiles >nul 2>&1
if '%errorlevel%' NEQ '0' (
    echo [Notice] Elevating to Administrator for C: drive system and user folder recovery...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"
set "PY_EXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PY_EXE=%~dp0.venv\Scripts\python.exe"
if exist "%~dp0emergency_restore\python\python.exe" set "PY_EXE=%~dp0emergency_restore\python\python.exe"
if "%PY_EXE%"=="" (
    where python >nul 2>&1
    if not errorlevel 1 (
        set "PY_EXE=python"
    ) else (
        echo [Error] Python environment not found. Please verify Python is installed.
        pause
        exit /b 1
    )
)

set "PY_SCRIPT=%~dp0emergency_restore.py"
if not exist "%PY_SCRIPT%" (
    if exist "%~dp0emergency_restore\emergency_restore.py" (
        set "PY_SCRIPT=%~dp0emergency_restore\emergency_restore.py"
    )
)

echo ======================================================================
echo   [*] One-Click Full C: Drive Disaster Recovery
echo ======================================================================
echo  - Restores the latest snapshot directly to original C: drive paths.
echo  - Restores programs, source codes, drivers, registry, and desktop files.
echo ======================================================================
echo.

"%PY_EXE%" "%PY_SCRIPT%" --auto

echo.
echo Recovery process finished.
pause

