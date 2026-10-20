# -*- coding: utf-8 -*-
"""
Desktop Remote Command Executor via SSH.
Provides clean argument passing without shell quote mangling.
"""
import sys
import subprocess

def run_remote(command_str: str) -> int:
    ssh_cmd = [
        "ssh",
        "-o", "ConnectTimeout=15",
        "Administrator@100.90.20.59",
        f"powershell -NoProfile -ExecutionPolicy Bypass -Command {command_str}"
    ]
    
    res = subprocess.run(
        ssh_cmd,
        stdout=sys.stdout,
        stderr=sys.stderr,
        creationflags=subprocess.CREATE_NO_WINDOW
    )
    return res.returncode

if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = " ".join(sys.argv[1:])
    else:
        cmd = "Get-Location"
    sys.exit(run_remote(cmd))
