@echo off
chcp 65001 >nul
schtasks /Create /TN "BackupSystem_Server" /TR "wscript.exe \"%~dp0..\start_silent.vbs\"" /SC ONLOGON /RL HIGHEST /F
schtasks /Run /TN "BackupSystem_Server"
ping 127.0.0.1 -n 3 >nul
exit /b 0
