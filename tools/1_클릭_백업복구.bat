@echo off
setlocal
chcp 65001 >nul
title K12 DR Full Unattended Restore
color 0b
cls
echo ========================================================
echo        K12 Clean Windows 11 DR Full Unattended Restore
echo ========================================================
echo.
if not exist "C:\MyBackup_Repository\disaster_recovery.py" (
    if not exist "C:\repo.tar" (
        echo [ERROR] C:\repo.tar not found!
        exit /b 1
    )
    echo [1/3] Extracting C:\repo.tar ... Please wait.
    tar.exe -xf "C:\repo.tar" -C "C:\"
    if not exist "C:\MyBackup_Repository\disaster_recovery.py" (
        echo [ERROR] Extraction failed!
        exit /b 1
    )
    echo [OK] Extraction completed.
    echo.
)
echo [2/3] Executing Unattended Emergency Restore to C:\ ...
set "PY_EXE=C:\MyBackup_Repository\emergency_restore\python\python.exe"
if not exist "%PY_EXE%" set "PY_EXE=python"
"%PY_EXE%" "C:\MyBackup_Repository\disaster_recovery.py" --repo "C:\MyBackup_Repository" --restore snap_20260926_005054_80c1c9 > "C:\dr_restore_log.txt" 2>&1
type "C:\dr_restore_log.txt"
echo.
echo ========================================================
echo [3/3] Running DR Scorecard (verify_k12_dr_restore.ps1)...
echo ========================================================
echo.
if exist "C:\MyBackup_Repository\baseline\verify_k12_dr_restore.ps1" (
    powershell.exe -ExecutionPolicy Bypass -NoProfile -File "C:\MyBackup_Repository\baseline\verify_k12_dr_restore.ps1" > "C:\dr_scorecard.txt" 2>&1
    type "C:\dr_scorecard.txt"
)
echo.
echo ========================================================
echo [4/4] Sending results to K12 (100.72.224.71:8999)...
echo ========================================================
curl.exe -s -X POST --data-binary @"C:\dr_restore_log.txt" http://100.72.224.71:8999/log >nul 2>&1
curl.exe -s -X POST --data-binary @"C:\dr_scorecard.txt" http://100.72.224.71:8999/scorecard >nul 2>&1
for /f "delims=" %%F in ('dir /b /s "C:\MyBackup_Repository\baseline\k12_dr_scorecard_*.json" 2^>nul') do (
    curl.exe -s -X POST --data-binary @"%%F" http://100.72.224.71:8999/json >nul 2>&1
)
curl.exe -s http://100.72.224.71:8999/done >nul 2>&1
echo ALL_DONE > "C:\dr_status.txt"
echo DR Process and Reporting Completed.
exit /b 0
