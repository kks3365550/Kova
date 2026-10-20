@echo off
chcp 65001 >nul
title BackupSystem GitHub Release Deployer
cls
echo ============================================================
echo   Kova GitHub Release Pipeline
echo ============================================================
echo.

python tools/release.py --bump patch -m "Release via Batch Trigger"
pause
exit /b 0
