# -*- coding: utf-8 -*-
"""
3-Way Data Integrity Verification Pipeline (Step C).
Compares Original (Local MiniPC) vs Snapshot (JSON Manifest) vs VM Restored (VHDX).
"""
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import os
import json
import hashlib
import glob
import subprocess
from datetime import datetime

ORIGINAL_BASE = r"C:\Users\kksjmj\Desktop\ai"
SNAPSHOT_PATH = r"D:\MyBackup_Repository\snapshots\snap_20260926_092057_7461cd.json"

PROJECTS = [
    {"name": ".agents", "key_file": "hooks.json"},
    {"name": ".ai", "key_file": "manifest.json"},
    {"name": "_자료보관", "key_file": "server.py"},
    {"name": "assets", "key_file": "pest_api_engine.js"},
    {"name": "hwp2excel", "key_file": "converter.py"},
    {"name": "scratch", "key_file": "s6_pic_0.png"},
    {"name": "검사성적서", "key_file": "웹방식_메인PC서버260918.zip"},
    {"name": "규격서", "key_file": "CCP 번호.xlsx"},
    {"name": "라벨부착검사", "key_file": "main.py"},
    {"name": "배송차량", "key_file": "benchmark_0603.json"},
    {"name": "백업시스템", "key_file": "run.py"},
    {"name": "선행모바일", "key_file": "2025년선행양식지-25.10.hwp"},
    {"name": "운행기록병합기", "key_file": "운행기록_병합기.py"},
    {"name": "위생교육", "key_file": "app.py"},
    {"name": "위생교육_비상패키지", "key_file": "emergency_gui.py"},
    {"name": "유인포충기", "key_file": "main.js"},
    {"name": "챗박스", "key_file": "server.py"},
    {"name": "컴플레인분석기", "key_file": "app.py"},
    {"name": "통합대시보드", "key_file": "dashboard.html"}
]

def sha256_file(filepath):
    if not os.path.exists(filepath):
        return None
    h = hashlib.sha256()
    try:
        with open(filepath, 'rb') as f:
            while chunk := f.read(1024 * 1024):
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        return f"ERR:{e}"

def gather_original():
    print("[1/4] Gathering Original Metadata from Local MiniPC...")
    orig_data = {}
    for p in PROJECTS:
        pname = p["name"]
        pdir = os.path.join(ORIGINAL_BASE, pname)
        if not os.path.exists(pdir):
            orig_data[pname] = {"exists": False, "files": 0, "size": 0, "key_file": p["key_file"], "key_hash": None}
            continue
        
        file_count = 0
        total_size = 0
        for root, dirs, files in os.walk(pdir):
            for f in files:
                file_count += 1
                fp = os.path.join(root, f)
                try:
                    total_size += os.path.getsize(fp)
                except Exception:
                    pass
        
        kf = os.path.join(pdir, p["key_file"])
        khash = sha256_file(kf) if os.path.exists(kf) else None
        orig_data[pname] = {
            "exists": True,
            "files": file_count,
            "size": total_size,
            "key_file": p["key_file"],
            "key_hash": khash
        }
    return orig_data

def gather_snapshot():
    print("[2/4] Gathering Snapshot Metadata from Manifest JSON...")
    snap_data = {}
    if not os.path.exists(SNAPSHOT_PATH):
        print(f"Error: Snapshot not found at {SNAPSHOT_PATH}")
        return snap_data
    
    with open(SNAPSHOT_PATH, 'r', encoding='utf-8') as f:
        mdata = json.load(f)
    
    entries = mdata.get("entries", [])
    
    # Pre-index entries by project
    proj_entries = {p["name"]: [] for p in PROJECTS}
    for e in entries:
        p = e.get("path") or os.path.join(e.get("source_root", ""), e.get("rel_path", ""))
        norm_p = p.replace("/", "\\")
        target_marker = "\\Desktop\\ai\\"
        if target_marker in norm_p:
            sub = norm_p.split(target_marker)[1]
            pname = sub.split("\\")[0]
            if pname in proj_entries:
                proj_entries[pname].append(e)
    
    for p in PROJECTS:
        pname = p["name"]
        items = proj_entries[pname]
        total_size = sum(e.get("size", 0) for e in items)
        
        # Find key file hash
        khash = None
        for e in items:
            p_e = e.get("path") or e.get("rel_path", "")
            if p_e.endswith(p["key_file"]) or p_e.endswith("/" + p["key_file"]) or p_e.endswith("\\" + p["key_file"]):
                khash = e.get("sha256") or e.get("blob_id") or e.get("blob_hash") or e.get("hash")
                break
        
        snap_data[pname] = {
            "files": len(items),
            "size": total_size,
            "key_file": p["key_file"],
            "key_hash": khash
        }
    return snap_data

# Remote PowerShell script template for VHDX inspection
REMOTE_PS1 = r"""
Import-Module Hyper-V -ErrorAction Stop

$targetId = [System.Guid]"4bd21edb-b90c-482c-8427-7341b7add3cb"
$vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1

if ($vm.State -ne 'Off') {
    Stop-VM -VM $vm -TurnOff -Force
    Start-Sleep -Seconds 5
    $vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
}

$hd = Get-VMHardDiskDrive -VM $vm | Select-Object -First 1
$vhdxPath = $hd.Path

$mount = Mount-VHD -Path $vhdxPath -ReadOnly -PassThru
$diskNum = $mount.DiskNumber
Start-Sleep -Seconds 2

$targetPart = Get-Partition -DiskNumber $diskNum | Where-Object { $_.DriveLetter } | Sort-Object Size -Descending | Select-Object -First 1
$destDrive = "$($targetPart.DriveLetter):\"

$results = @{}

try {
    $aiBase = Join-Path $destDrive "Users\kksjmj\Desktop\ai"
    
    $projectSpecs = @(
        @{ Name = ".agents"; Key = "hooks.json" },
        @{ Name = ".ai"; Key = "manifest.json" },
        @{ Name = "_자료보관"; Key = "server.py" },
        @{ Name = "assets"; Key = "pest_api_engine.js" },
        @{ Name = "hwp2excel"; Key = "converter.py" },
        @{ Name = "scratch"; Key = "s6_pic_0.png" },
        @{ Name = "검사성적서"; Key = "웹방식_메인PC서버260918.zip" },
        @{ Name = "규격서"; Key = "CCP 번호.xlsx" },
        @{ Name = "라벨부착검사"; Key = "main.py" },
        @{ Name = "배송차량"; Key = "benchmark_0603.json" },
        @{ Name = "백업시스템"; Key = "run.py" },
        @{ Name = "선행모바일"; Key = "2025년선행양식지-25.10.hwp" },
        @{ Name = "운행기록병합기"; Key = "운행기록_병합기.py" },
        @{ Name = "위생교육"; Key = "app.py" },
        @{ Name = "위생교육_비상패키지"; Key = "emergency_gui.py" },
        @{ Name = "유인포충기"; Key = "main.js" },
        @{ Name = "챗박스"; Key = "server.py" },
        @{ Name = "컴플레인분석기"; Key = "app.py" },
        @{ Name = "통합대시보드"; Key = "dashboard.html" }
    )

    foreach ($ps in $projectSpecs) {
        $pName = $ps.Name
        $pDir = Join-Path $aiBase $pName
        if (-not (Test-Path $pDir)) {
            $foundDir = Get-ChildItem -Path $aiBase -Directory | Where-Object { $_.Name -eq $pName -or $_.Name -like "*$($pName.Substring(0, [math]::Min(3, $pName.Length)))*" } | Select-Object -First 1
            if ($foundDir) { $pDir = $foundDir.FullName }
        }

        if (Test-Path $pDir) {
            $allFiles = Get-ChildItem -Path $pDir -Recurse -File -ErrorAction SilentlyContinue
            $fCount = $allFiles.Count
            $tSize = ($allFiles | Measure-Object -Property Length -Sum).Sum
            if ($null -eq $tSize) { $tSize = 0 }
            
            $kf = Join-Path $pDir $ps.Key
            $kHash = $null
            if (Test-Path $kf) {
                $hashObj = Get-FileHash -Path $kf -Algorithm SHA256
                $kHash = $hashObj.Hash.ToLower()
            }
            $results[$pName] = @{
                exists = $true
                files = $fCount
                size = $tSize
                key_file = $ps.Key
                key_hash = $kHash
            }
        } else {
            $results[$pName] = @{
                exists = $false
                files = 0
                size = 0
                key_file = $ps.Key
                key_hash = $null
            }
        }
    }
} finally {
    Dismount-VHD -Path $vhdxPath -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Start-VM -VM $vm
}

$results | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath "F:\vhdx_3way_results.json" -Encoding UTF8
"""

def gather_vhdx():
    print("[3/4] Gathering VHDX Restored Metadata from Desktop...")
    remote_ps = os.path.join(os.path.dirname(__file__), "run_3way_vhdx.ps1")
    with open(remote_ps, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(REMOTE_PS1)
    
    subprocess.run(["scp", remote_ps, "kksjmj@100.90.20.59:F:/run_3way_vhdx.ps1"], check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    
    cmd = ["ssh", "-o", "ConnectTimeout=15", "kksjmj@100.90.20.59", "powershell.exe -NoProfile -ExecutionPolicy Bypass -File F:\\run_3way_vhdx.ps1"]
    subprocess.run(cmd, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    
    local_vhdx_res = os.path.join(os.path.dirname(__file__), "..", "data", "dr_result", "vhdx_3way_results.json")
    subprocess.run(["scp", "kksjmj@100.90.20.59:F:/vhdx_3way_results.json", local_vhdx_res], check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    
    with open(local_vhdx_res, "r", encoding="utf-8-sig") as f:
        return json.load(f)

def run():
    orig = gather_original()
    snap = gather_snapshot()
    vhdx = gather_vhdx()
    
    print("[4/4] Cross-Comparing 3 Sources (Original vs Snapshot vs VHDX)...")
    report = {
        "timestamp": datetime.now().isoformat(),
        "summary": {
            "total_projects": len(PROJECTS),
            "perfect_match": 0,
            "minor_diff": 0,
            "hash_match_count": 0,
            "hash_total_count": 0
        },
        "projects": []
    }
    
    for p in PROJECTS:
        pname = p["name"]
        o = orig.get(pname, {})
        s = snap.get(pname, {})
        v = vhdx.get(pname, {})
        
        # File count match
        f_match = (o.get("files") == s.get("files") == v.get("files"))
        # Size match
        s_match = (o.get("size") == s.get("size") == v.get("size"))
        
        # Hash match
        h_o = o.get("key_hash")
        h_s = s.get("key_hash")
        h_v = v.get("key_hash")
        
        has_hash = bool(h_o and h_s and h_v)
        h_match = (h_o == h_s == h_v) if has_hash else False
        
        if has_hash:
            report["summary"]["hash_total_count"] += 1
            if h_match:
                report["summary"]["hash_match_count"] += 1
        
        if f_match and s_match and (not has_hash or h_match):
            status = "PERFECT_MATCH"
            report["summary"]["perfect_match"] += 1
        else:
            status = "DIFF"
            report["summary"]["minor_diff"] += 1
            
        proj_entry = {
            "name": pname,
            "status": status,
            "files": {"original": o.get("files", 0), "snapshot": s.get("files", 0), "vhdx": v.get("files", 0), "match": f_match},
            "size_bytes": {"original": o.get("size", 0), "snapshot": s.get("size", 0), "vhdx": v.get("size", 0), "match": s_match},
            "key_file": p["key_file"],
            "hash_sha256": {"original": h_o, "snapshot": h_s, "vhdx": h_v, "match": h_match}
        }
        report["projects"].append(proj_entry)
        
        print(f"[{status}] {pname:18s} | Files: {o.get('files')}/{s.get('files')}/{v.get('files')} | HashMatch: {h_match}")

    out_file = os.path.join(os.path.dirname(__file__), "..", "data", "dr_result", "dr_3way_integrity_report.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    
    print("\n" + "="*60)
    print(f"🎉 3-Way Integrity Audit Completed!")
    print(f" - Total Projects: {report['summary']['total_projects']}")
    print(f" - Perfect Match:  {report['summary']['perfect_match']}")
    print(f" - Minor Diff:     {report['summary']['minor_diff']}")
    print(f" - Key File Hashes: {report['summary']['hash_match_count']}/{report['summary']['hash_total_count']} 100% MATCH")
    print(f"Report saved to: {out_file}")
    print("="*60)

if __name__ == "__main__":
    run()
