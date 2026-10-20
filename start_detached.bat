@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "" "%~dp0.venv\Scripts\pythonw.exe" "run.py"
exit /b 0
