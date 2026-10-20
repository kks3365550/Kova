import ctypes
from ctypes import wintypes

CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

class STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_char_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]

class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]

si = STARTUPINFO()
si.cb = ctypes.sizeof(STARTUPINFO)
pi = PROCESS_INFORMATION()

cmd = r'"C:\Users\kksjmj\Desktop\ai\백업시스템\.venv\Scripts\pythonw.exe" "C:\Users\kksjmj\Desktop\ai\백업시스템\run.py"'
cwd = r"C:\Users\kksjmj\Desktop\ai\백업시스템"

flags = CREATE_BREAKAWAY_FROM_JOB | CREATE_NO_WINDOW | DETACHED_PROCESS

success = ctypes.windll.kernel32.CreateProcessW(
    None, cmd, None, None, False, flags, None, cwd, ctypes.byref(si), ctypes.byref(pi)
)

if not success:
    flags = CREATE_NO_WINDOW | DETACHED_PROCESS
    success = ctypes.windll.kernel32.CreateProcessW(
        None, cmd, None, None, False, flags, None, cwd, ctypes.byref(si), ctypes.byref(pi)
    )

if success:
    print(f"[OK] Process spawned PID={pi.dwProcessId}")
    ctypes.windll.kernel32.CloseHandle(pi.hProcess)
    ctypes.windll.kernel32.CloseHandle(pi.hThread)
else:
    print(f"[FAIL] CreateProcess error code: {ctypes.GetLastError()}")
