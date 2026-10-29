import os
import sys
import tempfile
import subprocess
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
from core.disk_clone import run_elevated_command

def setup_vhdx(vhdx_path: str = r"E:\HyperV_Drill\MiniPC_BootVM.vhdx"):
    print(f"Creating and formatting VHDX at: {vhdx_path}")
    if os.path.exists(vhdx_path):
        print("Existing VHDX found, detaching and removing...")
        detach_script = f'select vdisk file="{vhdx_path}"\ndetach vdisk\n'
        run_elevated_command(["diskpart", "/s", "-"], timeout=30)
        try:
            os.remove(vhdx_path)
        except Exception as e:
            print("Notice:", e)

    os.makedirs(os.path.dirname(vhdx_path), exist_ok=True)

    diskpart_script = f"""create vdisk file="{vhdx_path}" maximum=409600 type=expandable
select vdisk file="{vhdx_path}"
attach vdisk
convert gpt
create partition efi size=100
format quick fs=fat32 label="EFI"
assign letter=S
create partition msr size=16
create partition primary
format quick fs=ntfs label="RestoreOS"
assign letter=W
"""
    tmp_dp = os.path.join(tempfile.gettempdir(), "setup_vhdx.dp")
    with open(tmp_dp, "w", encoding="ascii") as f:
        f.write(diskpart_script)

    rc, out, err = run_elevated_command(["diskpart", "/s", tmp_dp], timeout=60)
    print("Diskpart RC:", rc)
    print("Diskpart Output:\n", out)
    if err:
        print("Diskpart Err:\n", err)
    try:
        os.remove(tmp_dp)
    except Exception:
        pass
    return rc == 0

if __name__ == "__main__":
    success = setup_vhdx()
    sys.exit(0 if success else 1)
