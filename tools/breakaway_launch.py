import sys
import subprocess

CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000

pyw = r"C:\Users\kksjmj\Desktop\ai\백업시스템\.venv\Scripts\pythonw.exe"
script = r"C:\Users\kksjmj\Desktop\ai\백업시스템\run.py"

flags = CREATE_NO_WINDOW
try:
    p = subprocess.Popen([pyw, script], creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, close_fds=True)
    print(f"[OK] Launched with BREAKAWAY_FROM_JOB PID={p.pid}")
except Exception as e:
    p = subprocess.Popen([pyw, script], creationflags=flags, close_fds=True)
    print(f"[OK] Fallback launch PID={p.pid}")
