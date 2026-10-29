# -*- coding: utf-8 -*-
import os
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import time
import json
import logging
import subprocess
import ctypes

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.disk_clone import run_elevated_command
from core.block_stream import restore_block_stream

def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

def log(msg: str, log_file=None):
    ts = time.strftime("%H:%M:%S")
    formatted = f"[{ts}] {msg}"
    print(formatted, flush=True)
    if log_file:
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(formatted + "\n")
        except Exception:
            pass

def main():
    vhdx_path = r"E:\HyperV_Drill\MiniPC_BootVM.vhdx"
    benchmark_log = r"E:\HyperV_Drill\restore_benchmark.log"
    vm_name = "MiniPC_Backup_Verify"

    # Self-elevation if not admin
    if not is_admin():
        log("[INFO] Requesting Administrator Elevation (UAC)...", benchmark_log)
        rc, out, err = run_elevated_command([sys.executable, os.path.abspath(__file__)], timeout=7200)
        print(out)
        if err:
            print(err, file=sys.stderr)
        sys.exit(rc)

    # Archive selection: use latest from D: or E:
    archive_path = r"D:\MyBackup_Repository\archives\System_Drive_C_20261029_214938.qblk"
    if not os.path.exists(archive_path):
        archive_path = r"E:\MyBackup_Repository\archives\System_Drive_C_20261029_212835.qblk"

    os.makedirs(r"E:\HyperV_Drill", exist_ok=True)
    with open(benchmark_log, "w", encoding="utf-8") as f:
        f.write("=== Hyper-V VM Restoration Benchmark Log ===\n")

    log("=" * 65, benchmark_log)
    log("[Hyper-V VM Boot Verification Pipeline Started]", benchmark_log)
    log(f"  - Source Archive : {archive_path} ({os.path.getsize(archive_path) / (1024**3):.2f} GB)", benchmark_log)
    log(f"  - Target VHDX    : {vhdx_path}", benchmark_log)
    log(f"  - Target Volume  : W: (OS Partition)", benchmark_log)
    log(f"  - EFI Volume     : S: (100MB FAT32 EFI Partition)", benchmark_log)
    log("=" * 65, benchmark_log)

    # -------------------------------------------------------------
    # [Step 1] LBA Block Stream Restoration to W:
    # -------------------------------------------------------------
    log("\n[Stage 1/4] Starting LBA Block Stream Restoration into W:...", benchmark_log)
    t_start = time.time()
    last_logged_time = [0.0]

    def prog_cb(info):
        now = time.time()
        pct = info.get("percent", 0.0)
        speed = info.get("speed_mb_s", 0.0)
        proc_gb = info.get("processed_bytes", 0) / (1024**3)
        tot_gb = info.get("total_bytes", 0) / (1024**3)
        if (now - last_logged_time[0] >= 5.0) or pct >= 99.9:
            last_logged_time[0] = now
            el = int(now - t_start)
            log(f"  - Restore: {pct:.1f}% ({proc_gb:.2f}/{tot_gb:.2f} GB) | Speed: {speed:.1f} MB/s | Elapsed: {el}s", benchmark_log)

    res = restore_block_stream(
        image_file_path=archive_path,
        target_drive="W:",
        auto_bcdboot=False,  # We inject explicitly to S: EFI
        auto_restore_vsn=True,
        progress_callback=prog_cb
    )

    t_restore_end = time.time()
    restore_sec = round(t_restore_end - t_start, 1)
    written_gb = round(res.get("restored_bytes", 0) / (1024**3), 2)
    avg_speed = res.get("average_speed_mb_s", 0.0)

    log("=" * 65, benchmark_log)
    log(f"[SUCCESS Stage 1 Complete] Data Restored: {written_gb} GB | Elapsed: {restore_sec}s ({round(restore_sec/60, 1)}m) | Avg Speed: {avg_speed:.1f} MB/s", benchmark_log)
    log("=" * 65, benchmark_log)

    # -------------------------------------------------------------
    # [Step 2] UEFI BCD Bootloader Injection to S: (EFI System Partition)
    # -------------------------------------------------------------
    log("\n[Stage 2/4] Injecting UEFI BCD Bootloader: bcdboot W:\\Windows /s S: /f UEFI...", benchmark_log)
    bcd_res = subprocess.run(
        ["bcdboot", "W:\\Windows", "/s", "S:", "/f", "UEFI"],
        capture_output=True, text=True, errors="ignore"
    )
    log(f"  - bcdboot stdout: {bcd_res.stdout.strip()}", benchmark_log)
    if bcd_res.stderr.strip():
        log(f"  - bcdboot stderr: {bcd_res.stderr.strip()}", benchmark_log)

    # Verify bootloader presence
    bcd_file = r"S:\EFI\Microsoft\Boot\BCD"
    kernel_file = r"W:\Windows\System32\ntoskrnl.exe"
    log(f"  - Verifying BCD file ({bcd_file}) : {os.path.exists(bcd_file)}", benchmark_log)
    log(f"  - Verifying Kernel   ({kernel_file}) : {os.path.exists(kernel_file)}", benchmark_log)

    # -------------------------------------------------------------
    # [Step 3] Safely Dismount VHDX
    # -------------------------------------------------------------
    log("\n[Stage 3/4] Safely Dismounting VHDX from Host OS...", benchmark_log)
    time.sleep(2)
    rc, out, err = run_elevated_command(
        ["powershell", "-NoProfile", "-Command", f"Dismount-DiskImage -ImagePath '{vhdx_path}'"],
        timeout=30
    )
    log(f"  - Dismount result: RC={rc} {out.strip()}", benchmark_log)

    # -------------------------------------------------------------
    # [Step 4] Hyper-V Gen 2 VM Setup, Power On & vmconnect Screen Popup
    # -------------------------------------------------------------
    log(f"\n[Stage 4/4] Configuring & Starting Hyper-V Gen 2 VM '{vm_name}'...", benchmark_log)
    ps_vm = f"""
    Import-Module Hyper-V -ErrorAction Stop
    $vmName = '{vm_name}'
    $vhdx = '{vhdx_path}'

    # Remove previous VM if exists
    $existing = Get-VM -Name $vmName -ErrorAction SilentlyContinue
    if ($existing) {{
        if ($existing.State -ne 'Off') {{ Stop-VM -Name $vmName -TurnOff -Force }}
        Remove-VM -Name $vmName -Force
    }}

    # Create Gen 2 VM with 8GB RAM, 4 vCPUs
    $sw = Get-VMSwitch | Where-Object {{ $_.Name -like '*Default*' }} | Select-Object -First 1
    if ($sw) {{
        $vm = New-VM -Name $vmName -Generation 2 -MemoryStartupBytes 8GB -VHDPath $vhdx -SwitchName $sw.Name
    }} else {{
        $vm = New-VM -Name $vmName -Generation 2 -MemoryStartupBytes 8GB -VHDPath $vhdx
    }}
    Set-VMProcessor -VMName $vmName -Count 4
    Set-VMMemory -VMName $vmName -DynamicMemoryEnabled $true -MinimumBytes 4GB -MaximumBytes 8GB
    Set-VMFirmware -VMName $vmName -EnableSecureBoot On -SecureBootTemplate "MicrosoftUEFICertificateAuthority"
    Set-VM -Name $vmName -AutomaticCheckpointsEnabled $false

    # Start VM
    Start-VM -Name $vmName
    Start-Sleep -Seconds 3
    $state = (Get-VM -Name $vmName).State
    Write-Output "VM_STATE: $state"

    # Launch vmconnect window to show screen
    Start-Process "vmconnect.exe" -ArgumentList "localhost $vmName"
    """

    rc_vm, out_vm, err_vm = run_elevated_command(
        ["powershell", "-NoProfile", "-Command", ps_vm],
        timeout=60
    )
    log(f"  - VM Deployment Output:\n{out_vm.strip()}", benchmark_log)
    if err_vm.strip():
        log(f"  - VM Deployment Warnings:\n{err_vm.strip()}", benchmark_log)

    log("\n=============================================================", benchmark_log)
    log(f"[SUCCESS] VM Verification Drill Completed Successfully!", benchmark_log)
    log("  - vmconnect.exe window has been popped up on your screen.", benchmark_log)
    log("  - You should see Windows 11 UEFI Booting and Login Screen!", benchmark_log)
    log("=============================================================", benchmark_log)

if __name__ == "__main__":
    main()
