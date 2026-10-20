import os
import subprocess

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

base_dir = r"C:\Users\kksjmj\Desktop\ai\백업시스템"
vbs_path = os.path.join(base_dir, "start_silent.vbs")

p = subprocess.Popen(
    ["wscript.exe", vbs_path],
    cwd=base_dir,
    creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
    close_fds=True
)
print(f"[OK] Launched detached wscript PID={p.pid}")
