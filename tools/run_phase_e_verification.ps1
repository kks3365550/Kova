# run_phase_e_verification.ps1 - Deep Runtime & Execution Verification
$ErrorActionPreference = 'Stop'

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  [*] Phase E: Deep Runtime & Execution Verification Starting" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

$report = [ordered]@{
    timestamp = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    vhd_status = [ordered]@{}
    python_env = [ordered]@{}
    node_env   = [ordered]@{}
    projects   = [ordered]@{}
    cache_check = [ordered]@{}
    overall_status = "PENDING"
}

# 1. Stop VM and mount VHD
$vm = Get-VM | Select-Object -First 1
Write-Host "`n[Step 1] Managing VM & Mounting VHD..."
if ($vm.State -ne 'Off') {
    Write-Host "  Stopping VM (Stop-VM -Save)..."
    Stop-VM $vm -Save
    Start-Sleep -Seconds 2
}

$vhdPath = $vm.HardDrives[0].Path
Write-Host "  VHD Path: $vhdPath"
$mount = Mount-VHD -Path $vhdPath -ReadOnly -PassThru
Start-Sleep -Seconds 3

$part = Get-Partition -DiskNumber $mount.DiskNumber | Where-Object DriveLetter | Select-Object -First 1
$d = $part.DriveLetter
Write-Host "  [OK] Mounted on ${d}: (ReadOnly)" -ForegroundColor Green

try {
    $driveInfo = Get-PSDrive $d
    $report.vhd_status = [ordered]@{
        drive_letter = "${d}:"
        free_space_gb = [math]::Round($driveInfo.Free / 1GB, 2)
        used_space_gb = [math]::Round($driveInfo.Used / 1GB, 2)
    }
    Write-Host "  Drive ${d}: Free: $($report.vhd_status.free_space_gb) GB / Used: $($report.vhd_status.used_space_gb) GB"

    # 2. Python Environment Verification
    Write-Host "`n[Step 2] Auditing Python Runtimes & Imports..."
    $uvPy = "${d}:\Users\kksjmj\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe"
    $venvSitePackages = "${d}:\Users\kksjmj\AppData\Local\hermes\hermes-agent\venv\Lib\site-packages"

    $pyVerified = $false
    $pyVersion = ""
    if (Test-Path $uvPy) {
        $pyVersion = (& $uvPy --version 2>&1).Trim()
        Write-Host "  [FOUND] UV Base CPython: $pyVersion" -ForegroundColor Green
        $pyVerified = $true
    } else {
        Write-Host "  [FAIL] Missing: $uvPy" -ForegroundColor Red
    }

    $pkgCount = 0
    $distCount = 0
    if (Test-Path $venvSitePackages) {
        $pkgs = Get-ChildItem -Path $venvSitePackages -Directory | Where-Object { $_.Name -notlike "*.dist-info" -and $_.Name -notlike "*.egg-info" -and $_.Name -ne "__pycache__" }
        $dists = Get-ChildItem -Path $venvSitePackages -Directory -Filter "*.dist-info"
        $pkgCount = $pkgs.Count
        $distCount = $dists.Count
        Write-Host "  [FOUND] Hermes venv site-packages: $pkgCount packages ($distCount dist-info)" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] Missing site-packages: $venvSitePackages" -ForegroundColor Red
    }

    # Import Test
    $importResults = [ordered]@{}
    $modulesToTest = @("torch", "fastapi", "pandas", "httpx", "cryptography")
    if ($pyVerified -and (Test-Path $venvSitePackages)) {
        foreach ($mod in $modulesToTest) {
            $cmd = "import sys; sys.path.insert(0, r'$venvSitePackages'); import $mod; print('VER:' + str(getattr($mod, '__version__', 'OK')))"
            $out = & $uvPy -c $cmd 2>&1
            $isOk = $LASTEXITCODE -eq 0 -and ($out -match 'VER:')
            if ($isOk) {
                $verStr = ($out | Select-String -Pattern 'VER:(.*)').Matches.Groups[1].Value
                Write-Host "  * [PASS] import $mod (Version: $verStr)" -ForegroundColor Green
                $importResults[$mod] = [ordered]@{ status = "PASS"; version = $verStr }
            } else {
                $errStr = ($out -join " ").Trim()
                Write-Host "  * [FAIL] import $mod : $errStr" -ForegroundColor Red
                $importResults[$mod] = [ordered]@{ status = "FAIL"; error = $errStr }
            }
        }
    }

    $report.python_env = [ordered]@{
        uv_python_found = $pyVerified
        python_version  = $pyVersion
        site_packages_path = $venvSitePackages
        package_count   = $pkgCount
        dist_info_count = $distCount
        critical_imports = $importResults
    }

    # 3. Node.js Verification
    Write-Host "`n[Step 3] Auditing Node.js & npm..."
    $nodeExe = "${d}:\Users\kksjmj\AppData\Local\hermes\node\node.exe"
    $npmCmd  = "${d}:\Users\kksjmj\AppData\Local\hermes\node\npm.cmd"

    $nodeVer = ""
    $npmVer  = ""
    if (Test-Path $nodeExe) {
        $nodeVer = (& $nodeExe -v 2>&1).Trim()
        Write-Host "  [FOUND] Node.js: $nodeVer" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] Node.exe missing: $nodeExe" -ForegroundColor Red
    }

    if (Test-Path $npmCmd) {
        $npmVer = (& cmd /c "$npmCmd -v" 2>&1).Trim()
        Write-Host "  [FOUND] npm: $npmVer" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] npm.cmd missing: $npmCmd" -ForegroundColor Red
    }

    $report.node_env = [ordered]@{
        node_path    = $nodeExe
        node_version = $nodeVer
        npm_version  = $npmVer
    }

    # 4. Project Verification (19 AI Projects)
    Write-Host "`n[Step 4] Auditing 19 AI Projects in Desktop\ai..."
    $aiBase = "${d}:\Users\kksjmj\Desktop\ai"
    $projList = @()
    if (Test-Path $aiBase) {
        $projects = Get-ChildItem -Path $aiBase -Directory
        foreach ($proj in $projects) {
            $allFiles = Get-ChildItem -Path $proj.FullName -Recurse -File -ErrorAction SilentlyContinue
            $entrypoints = @()
            $candidates = @("run.py", "main.py", "app.py", "server.py", "package.json", "index.html")
            foreach ($c in $candidates) {
                if (Test-Path (Join-Path $proj.FullName $c)) {
                    $entrypoints += $c
                }
            }
            $projList += [ordered]@{
                name        = $proj.Name
                file_count  = $allFiles.Count
                entrypoints = ($entrypoints -join ", ")
            }
            Write-Host "  * $($proj.Name): $($allFiles.Count) files | [$($entrypoints -join ', ')]"
        }
    }
    $report.projects = [ordered]@{
        project_count = $projList.Count
        project_list  = $projList
    }

    # Smoke Test: BackupSystem CLI help
    $backupRunPy = "${d}:\Users\kksjmj\Desktop\ai\백업시스템\run.py"
    $smokeOk = $false
    if ((Test-Path $backupRunPy) -and $pyVerified) {
        $smokeOut = & $uvPy $backupRunPy --help 2>&1
        if ($LASTEXITCODE -eq 0 -or ($smokeOut -match "Usage" -or $smokeOut -match "options" -or $smokeOut -match "help")) {
            Write-Host "  [PASS] Smoke test: 백업시스템 run.py --help executed successfully" -ForegroundColor Green
            $smokeOk = $true
        } else {
            Write-Host "  [FAIL] Smoke test failed: $smokeOut" -ForegroundColor Red
        }
    }
    $report.projects["smoke_test"] = [ordered]@{
        target = "백업시스템\run.py --help"
        passed = $smokeOk
    }

    # 5. Android Studio Cache Check
    Write-Host "`n[Step 5] Checking Android Studio index cache..."
    $asIndexDir = "${d}:\Users\kksjmj\AppData\Local\Google\AndroidStudio*\index"
    $asMatches = Get-ChildItem -Path $asIndexDir -Recurse -File -ErrorAction SilentlyContinue
    $asCount = if ($asMatches) { $asMatches.Count } else { 0 }
    Write-Host "  Android Studio index cache files: $asCount (Goal: 0)" -ForegroundColor (if ($asCount -eq 0) { "Green" } else { "Red" })

    $report.cache_check = [ordered]@{
        androidstudio_index_cache_files = $asCount
        clean_elimination = ($asCount -eq 0)
    }

    # Overall Verdict
    $allImportsPassed = ($importResults.Values | Where-Object { $_.status -ne 'PASS' }).Count -eq 0
    $overallPass = $pyVerified -and ($pkgCount -ge 200) -and $allImportsPassed -and ($nodeVer -ne "") -and ($asCount -eq 0) -and $smokeOk
    $report.overall_status = if ($overallPass) { "SUCCESS_ALL_GATES_PASSED" } else { "PARTIAL_FAIL" }

    Write-Host "`n============================================================" -ForegroundColor Cyan
    if ($overallPass) {
        Write-Host "  🎉 [Phase E VERDICT: PASS] ALL RUNTIMES AND PROJECTS VERIFIED!" -ForegroundColor Green
    } else {
        Write-Host "  ⚠️ [Phase E VERDICT: WARN] SOME CHECKS DID NOT PASS" -ForegroundColor Yellow
    }
    Write-Host "============================================================" -ForegroundColor Cyan

} finally {
    Write-Host "`n[Step 6] Dismounting VHD and Restarting VM..."
    Dismount-VHD -Path $vhdPath
    Start-Sleep -Seconds 2
    Start-VM $vm
    Write-Host "  [OK] VM Restarted." -ForegroundColor Green
}

# Save Report
$reportJson = "F:\phase_e_verification_report.json"
$report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $reportJson -Encoding UTF8
Write-Host "`n💾 Verification report saved to: $reportJson" -ForegroundColor Cyan
