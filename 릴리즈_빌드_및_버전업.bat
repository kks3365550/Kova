@echo off
setlocal
title Backup System Release and Version Manager
cd /d "%~dp0"

echo ========================================================
echo   Backup System Git Versioning and Release Manager
echo ========================================================
echo.
echo Select version bump type:
echo  [1] Patch (Bugfix, Optimization) - e.g. 2.1.0 -^> 2.1.1 [DEFAULT]
echo  [2] Minor (New Features)         - e.g. 2.1.0 -^> 2.2.0
echo  [3] Major (Breaking Changes)     - e.g. 2.1.0 -^> 3.0.0
echo  [4] Current Version (Build only) - Keep current version
echo.
set /p CHOICE="Enter choice [1-4] (default: 1): "
if "%CHOICE%"=="" set CHOICE=1

set "BUMP=patch"
if "%CHOICE%"=="1" set "BUMP=patch"
if "%CHOICE%"=="2" set "BUMP=minor"
if "%CHOICE%"=="3" set "BUMP=major"
if "%CHOICE%"=="4" set "BUMP=none"

echo.
set /p MSG="Enter release commit message (optional): "
if "%MSG%"=="" set "MSG=Automated engine update and bugfix release"

echo.
echo Starting release pipeline (Bump: %BUMP%)...
python tools\release.py --bump %BUMP% -m "%MSG%"

echo.
pause
exit /b 0
