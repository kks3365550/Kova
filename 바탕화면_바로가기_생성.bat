@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Create Desktop Shortcut

set "PY_EXE="
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY_EXE=%~dp0.venv\Scripts\python.exe"
) else (
    set "PY_EXE=python"
)

"%PY_EXE%" create_desktop_shortcut.py
echo [*] Desktop shortcut created.
pause
exit /b 0
