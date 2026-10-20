# -*- coding: utf-8 -*-
"""
Desktop Remote PowerShell Runner via Base64 EncodedCommand.
100% immune to CMD / SSH quotation mangling.
"""
import sys
import base64
import subprocess

def run_ps_remote(script_content: str) -> int:
    # PowerShell -EncodedCommand requires UTF-16LE bytes
    encoded_cmd = base64.b64encode(script_content.encode('utf-16le')).decode('ascii')
    
    ssh_cmd = [
        "ssh",
        "-o", "ConnectTimeout=15",
        "kksjmj@100.90.20.59",
        f"powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded_cmd}"
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
        script = " ".join(sys.argv[1:])
    else:
        script = "Get-Location"
    sys.exit(run_ps_remote(script))
