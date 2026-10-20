import os

content = (
    "@echo off\r\n"
    "chcp 65001 >nul\r\n"
    'cd /d "C:\\Users\\kksjmj\\Desktop\\ai\\백업시스템"\r\n'
    'start "" "C:\\Users\\kksjmj\\Desktop\\ai\\백업시스템\\.venv\\Scripts\\pythonw.exe" "run.py"\r\n'
    "exit /b 0\r\n"
)
with open("start_detached.bat", "w", encoding="utf-8", newline="\r\n") as f:
    f.write(content)
print("Saved start_detached.bat with CRLF!")
