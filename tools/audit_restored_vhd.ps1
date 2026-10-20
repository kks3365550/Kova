# audit_restored_vhd.ps1 - Deep Forensic Audit of Restored VHD
$ErrorActionPreference = 'Stop'

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  [*] Deep Forensic Audit of Restored VHD Starting" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# 1. Stop VM & Mount VHD ReadOnly
Write-Host "`n[Step 1] Mounting VHD in Read-Only Mode..."
Stop-VM (Get-VM) -Save
Start-Sleep -Seconds 2

$vhdPath = (Get-VM).HardDrives.Path
$mount = Mount-VHD -Path $vhdPath -ReadOnly -PassThru
Start-Sleep -Seconds 3

$part = Get-Partition -DiskNumber $mount.DiskNumber | Where-Object DriveLetter | Select-Object -First 1
$d = $part.DriveLetter
Write-Host "  [OK] VHD Mounted on ${d}: (Read-Only)" -ForegroundColor Green

$auditResult = [ordered]@{}

try {
    # 2. Audit Baseline User PATH (13 items)
    Write-Host "`n[Step 2] Auditing 13 User PATH Directories..."
    $baselineJson = "F:\MyBackup_Repository\baseline\k12_baseline_20260926_004738.json"
    $baseData = Get-Content -LiteralPath $baselineJson -Raw -Encoding UTF8 | ConvertFrom-Json

    $userPaths = $baseData.Environment.UserPathOrder
    $pathAudit = @()
    foreach ($p in $userPaths) {
        $mapped = $p -replace '^[A-Za-z]:', "${d}:"
        $exists = Test-Path $mapped
        $pathAudit += [pscustomobject]@{
            OriginalPath = $p
            MappedPath   = $mapped
            Exists       = $exists
        }
        $status = if ($exists) { "[PASS]" } else { "[FAIL]" }
        Write-Host "  $status $p"
    }
    $auditResult["UserPathAudit"] = $pathAudit

    # 3. Audit Baseline 15 Python Executables
    Write-Host "`n[Step 3] Auditing 15 Python Executables..."
    $pyExecs = $baseData.DeveloperTools.PythonExecutables
    $pyAudit = @()
    foreach ($py in $pyExecs) {
        $p = $py.Path
        $mapped = $p -replace '^[A-Za-z]:', "${d}:"
        $exists = Test-Path $mapped
        $pyAudit += [pscustomobject]@{
            OriginalPath = $p
            MappedPath   = $mapped
            Version      = $py.Version
            Exists       = $exists
        }
        $status = if ($exists) { "[PASS]" } else { "[FAIL]" }
        Write-Host "  $status $p ($($py.Version))"
    }
    $auditResult["PythonAudit"] = $pyAudit

    # 3.1 Audit Pip Packages (site-packages)
    Write-Host "`n[Step 3.1] Counting Restored pip Packages in site-packages..."
    $sitePackagesDirs = @(
        "${d}:\Users\kksjmj\AppData\Local\Python\bin\Lib\site-packages",
        "${d}:\Users\kksjmj\AppData\Local\hermes\hermes-agent\venv\Lib\site-packages"
    )
    $pipAudit = @()
    foreach ($sp in $sitePackagesDirs) {
        if (Test-Path $sp) {
            $pkgs = Get-ChildItem -Path $sp -Directory | Where-Object { $_.Name -notlike "*.dist-info" -and $_.Name -notlike "*.egg-info" -and $_.Name -ne "__pycache__" }
            $dists = Get-ChildItem -Path $sp -Directory -Filter "*.dist-info"
            Write-Host "  [FOUND] $sp : $($pkgs.Count) packages ($($dists.Count) dist-info)" -ForegroundColor Green
            $pipAudit += [pscustomobject]@{ Path = $sp; PackageCount = $pkgs.Count; DistInfoCount = $dists.Count }
        } else {
            Write-Host "  [NOT FOUND] $sp" -ForegroundColor Red
            $pipAudit += [pscustomobject]@{ Path = $sp; PackageCount = 0; DistInfoCount = 0 }
        }
    }
    $auditResult["PipAudit"] = $pipAudit

    # 4. Search for Node.js
    Write-Host "`n[Step 4] Searching for Node.js Binary in VHD..."
    $nodePaths = @(
        "${d}:\Users\kksjmj\AppData\Local\hermes\node\node.exe",
        "${d}:\Program Files\nodejs\node.exe"
    )
    $nodeFound = @()
    foreach ($np in $nodePaths) {
        if (Test-Path $np) {
            Write-Host "  [FOUND] $np" -ForegroundColor Green
            $nodeFound += $np
        } else {
            Write-Host "  [FAIL] Missing: $np" -ForegroundColor Red
        }
    }
    $auditResult["NodeAudit"] = $nodeFound

    # 5. Audit 19 AI Projects
    Write-Host "`n[Step 5] Auditing 19 AI Projects (Entrypoints & File Counts)..."
    $aiBase = "${d}:\Users\kksjmj\Desktop\ai"
    $projAudit = @()
    if (Test-Path $aiBase) {
        $projects = Get-ChildItem -Path $aiBase -Directory
        foreach ($proj in $projects) {
            $allFiles = Get-ChildItem -Path $proj.FullName -Recurse -File -ErrorAction SilentlyContinue
            $entrypoints = @()
            $candidates = @("run.py", "main.py", "app.py", "server.py", "package.json", "index.html", "requirements.txt")
            foreach ($c in $candidates) {
                if (Test-Path (Join-Path $proj.FullName $c)) {
                    $entrypoints += $c
                }
            }
            $projAudit += [pscustomobject]@{
                ProjectName = $proj.Name
                FileCount   = $allFiles.Count
                Entrypoints = ($entrypoints -join ", ")
            }
            Write-Host "  * $($proj.Name): $($allFiles.Count) files | Entrypoints: [$($entrypoints -join ', ')]"
        }
    } else {
        Write-Host "  [FAIL] ${aiBase} does not exist!" -ForegroundColor Red
    }
    $auditResult["ProjectAudit"] = $projAudit

} finally {
    # 6. Dismount & Restart VM
    Write-Host "`n[Step 6] Dismounting VHD and Restarting VM..."
    Dismount-VHD -Path $vhdPath
    Start-Sleep -Seconds 2
    Start-VM (Get-VM)
    Write-Host "  [OK] VM Restarted." -ForegroundColor Green
}

# 7. Output Results
$jsonOut = "F:\vhd_deep_audit_report.json"
$auditResult | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $jsonOut -Encoding UTF8
Write-Host "`n============================================================" -ForegroundColor Cyan
Write-Host "  🎉 Deep Forensic Audit Completed! Report: $jsonOut" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
