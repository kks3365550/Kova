import subprocess

cmd = [
    "schtasks", "/Create",
    "/TN", "BackupSystem_Server",
    "/TR", r'wscript.exe "C:\Users\kksjmj\Desktop\ai\백업시스템\start_silent.vbs"',
    "/SC", "ONLOGON",
    "/F"
]
subprocess.run(cmd, check=True)
subprocess.run(["schtasks", "/Run", "/TN", "BackupSystem_Server"], check=True)
print("[OK] Task BackupSystem_Server created and launched.")
