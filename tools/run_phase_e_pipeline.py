# -*- coding: utf-8 -*-
"""
Phase E Deep Runtime & Execution Verification Runner.
Executes robust PowerShell script on Desktop (100.90.20.59) via Base64 EncodedCommand.
"""
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
import base64
import subprocess

ps_script = r"""
Import-Module Hyper-V -ErrorAction Stop

Write-Host "============================================================"
Write-Host "  [*] Phase E: Deep Runtime & Execution Verification"
Write-Host "============================================================"

$report = [ordered]@{
    timestamp = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    vhd_status = [ordered]@{}
    python_env = [ordered]@{}
    hermes_venv = [ordered]@{}
    node_env   = [ordered]@{}
    projects   = [ordered]@{}
    cache_check = [ordered]@{}
    metrics    = [ordered]@{}
    errors     = @()
    overall_status = "PENDING"
}

$targetId = [System.Guid]"4bd21edb-b90c-482c-8427-7341b7add3cb"
$vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1

# 1. VM 셧다운 루프
Write-Host "`n[Step 1] Stopping VM completely..."
if ($vm.State -ne 'Off') {
    Stop-VM -VM $vm -TurnOff -Force
    $timeout = 30
    $waited = 0
    while ($vm.State -ne 'Off' -and $waited -lt $timeout) {
        Start-Sleep -Seconds 1
        $waited++
        $vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
    }
}
Write-Host "  VM State is now: $($vm.State)"

# 2. VHD 경로 획득
$hd = Get-VMHardDiskDrive -VM $vm | Select-Object -First 1
$vhdPath = $hd.Path
Write-Host "  VHD Path: $vhdPath"

if (-not (Test-Path $vhdPath)) {
    Write-Error "VHD path does not exist: $vhdPath"
    exit 1
}

# 3. VHD 마운트 및 파티션 대기 루프
Write-Host "`n[Step 2] Mounting VHD (ReadOnly)..."
$mount = Mount-VHD -Path $vhdPath -ReadOnly -PassThru
$diskNum = $mount.DiskNumber
Write-Host "  Mounted as Disk Number: $diskNum"

$d = $null
$destDrive = $null
$partWaited = 0
while (-not $d -and $partWaited -lt 15) {
    Start-Sleep -Seconds 1
    $partWaited++
    $targetPart = Get-Partition -DiskNumber $diskNum -ErrorAction SilentlyContinue | Where-Object { $_.DriveLetter } | Sort-Object Size -Descending | Select-Object -First 1
    if ($targetPart) {
        $d = $targetPart.DriveLetter
        $destDrive = "$($d):\"
    }
}

if (-not $d) {
    Write-Error "Failed to obtain drive letter for Disk $diskNum within 15s"
    Dismount-VHD -Path $vhdPath
    exit 1
}

Write-Host "  Drive Letter Assigned: $destDrive"

try {
    $driveInfo = Get-PSDrive $d
    $freeGb = [math]::Round($driveInfo.Free / 1GB, 2)
    $usedGb = [math]::Round($driveInfo.Used / 1GB, 2)
    Write-Host "  Disk Status: Free ${freeGb} GB / Used ${usedGb} GB"
    
    $report.vhd_status = [ordered]@{
        drive_letter  = $destDrive
        disk_number   = $diskNum
        free_space_gb = $freeGb
        used_space_gb = $usedGb
    }

    # 디렉토리 트리 진단
    Write-Host "`n[Diagnostic] Top-level directories in $destDrive :"
    Get-ChildItem -Path $destDrive -Directory | ForEach-Object { Write-Host " - $($_.Name)" }
    
    $usersPath = "$destDrive\Users"
    if (Test-Path $usersPath) {
        Write-Host "Users directory contents:"
        Get-ChildItem -Path $usersPath -Directory | ForEach-Object { Write-Host " - $($_.Name)" }
    }

    # 4. UV Base CPython 3.11 검증
    Write-Host "`n[Step 3] Auditing UV Base CPython 3.11..."
    $uvPy = "$destDrive\Users\kksjmj\AppData\Roaming\uv\python\cpython-3.11.11-windows-x86_64-none\install\python.exe"
    $uvPyFound = Test-Path $uvPy
    $uvPyVer = ""

    if ($uvPyFound) {
        $uvPyVer = (& $uvPy --version 2>&1).Trim()
        Write-Host "  [FOUND] UV CPython: $uvPyVer"
    } else {
        # 대안 경로 탐색
        $foundPy = Get-ChildItem -Path "$destDrive\Users" -Recurse -Filter "python.exe" -ErrorAction SilentlyContinue | Select-Object -First 5
        Write-Host "  [FAIL] $uvPy not found directly. Other pythons found:"
        $foundPy | ForEach-Object { Write-Host "   * $($_.FullName)" }
        $report.errors += "UV CPython not at expected path: $uvPy"
    }

    $report.python_env = [ordered]@{
        binary_path = $uvPy
        found       = $uvPyFound
        version     = $uvPyVer
        status      = if ($uvPyFound) { "PASS" } else { "FAIL" }
    }

    # 5. Hermes venv 런타임 & 패키지 검증
    Write-Host "`n[Step 4] Auditing Hermes venv & 206 pip Packages..."
    $hermesPy = "$destDrive\Users\kksjmj\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"
    $hermesSitePkgs = "$destDrive\Users\kksjmj\AppData\Local\hermes\hermes-agent\venv\Lib\site-packages"
    $hermesPyFound = Test-Path $hermesPy
    $distCount = 0
    $pkgCount = 0

    if (Test-Path $hermesSitePkgs) {
        $distInfos = Get-ChildItem -Path $hermesSitePkgs -Directory -Filter "*.dist-info"
        $distCount = $distInfos.Count
        $pkgs = Get-ChildItem -Path $hermesSitePkgs -Directory | Where-Object { $_.Name -notlike "*.dist-info" -and $_.Name -notlike "*.egg-info" -and $_.Name -ne "__pycache__" }
        $pkgCount = $pkgs.Count
        Write-Host "  [FOUND] Hermes site-packages: $distCount dist-info, $pkgCount packages"
    } else {
        Write-Host "  [FAIL] Hermes site-packages not found: $hermesSitePkgs"
        $report.errors += "Hermes site-packages not found"
    }

    $importResults = [ordered]@{}
    $modulesToTest = @("torch", "fastapi", "pandas", "httpx", "cryptography", "uvicorn", "pydantic")
    $allImportsPass = $false

    if ($hermesPyFound) {
        $hVer = (& $hermesPy --version 2>&1).Trim()
        Write-Host "  [EXEC] Hermes Python Version: $hVer"
        $failedMods = @()
        foreach ($mod in $modulesToTest) {
            $cmd = "import $mod; print('VER:' + str(getattr($mod, '__version__', 'OK')))"
            $out = & $hermesPy -c $cmd 2>&1
            if ($LASTEXITCODE -eq 0 -and ($out -match 'VER:')) {
                $verStr = ($out | Select-String -Pattern 'VER:(.*)').Matches.Groups[1].Value
                Write-Host "    * [PASS] import $mod -> $verStr"
                $importResults[$mod] = [ordered]@{ status = "PASS"; version = $verStr }
            } else {
                $errStr = ($out -join " ").Trim()
                Write-Host "    * [FAIL] import $mod -> $errStr"
                $importResults[$mod] = [ordered]@{ status = "FAIL"; error = $errStr }
                $failedMods += $mod
            }
        }
        $allImportsPass = ($failedMods.Count -eq 0)
    }

    $report.hermes_venv = [ordered]@{
        python_path      = $hermesPy
        python_found     = $hermesPyFound
        site_packages    = $hermesSitePkgs
        dist_info_count  = $distCount
        package_count    = $pkgCount
        imports_tested   = $modulesToTest.Count
        all_imports_pass = $allImportsPass
        import_details   = $importResults
    }

    # 6. Node.js & npm 검증
    Write-Host "`n[Step 5] Auditing Node.js & npm..."
    $nodeExe = "$destDrive\Users\kksjmj\AppData\Local\hermes\node\node.exe"
    $npmCmd  = "$destDrive\Users\kksjmj\AppData\Local\hermes\node\npm.cmd"
    $nodeVer = ""
    $npmVer  = ""

    if (Test-Path $nodeExe) {
        $nodeVer = (& $nodeExe -v 2>&1).Trim()
        Write-Host "  [FOUND] Node.js: $nodeVer"
    } else {
        Write-Host "  [FAIL] Node missing: $nodeExe"
        $report.errors += "Node missing"
    }

    if (Test-Path $npmCmd) {
        $npmVer = (& cmd /c "$npmCmd -v" 2>&1).Trim()
        Write-Host "  [FOUND] npm: $npmVer"
    } else {
        Write-Host "  [FAIL] npm missing: $npmCmd"
        $report.errors += "npm missing"
    }

    $report.node_env = [ordered]@{
        node_path    = $nodeExe
        node_version = $nodeVer
        npm_path     = $npmCmd
        npm_version  = $npmVer
        status       = if ($nodeVer -and $npmVer) { "PASS" } else { "FAIL" }
    }

    # 7. 19개 AI 프로젝트 검증
    Write-Host "`n[Step 6] Auditing 19 AI Projects in Desktop\ai..."
    $aiBase = "$destDrive\Users\kksjmj\Desktop\ai"
    $projList = @()
    $smokePass = $false

    if (Test-Path $aiBase) {
        $projects = Get-ChildItem -Path $aiBase -Directory | Sort-Object Name
        Write-Host "  Found $($projects.Count) project directories in $aiBase"
        foreach ($proj in $projects) {
            $files = Get-ChildItem -Path $proj.FullName -Recurse -File -ErrorAction SilentlyContinue
            $entrypoints = @()
            $candidates = @("run.py", "main.py", "app.py", "server.py", "package.json", "index.html")
            foreach ($c in $candidates) {
                if (Test-Path (Join-Path $proj.FullName $c)) {
                    $entrypoints += $c
                }
            }
            $projList += [ordered]@{
                name        = $proj.Name
                file_count  = $files.Count
                entrypoints = ($entrypoints -join ", ")
            }
            Write-Host "  * $($proj.Name): $($files.Count) files | [$($entrypoints -join ', ')]"
        }

        # BackupSystem CLI Smoke Test
        $backupRunPy = "$aiBase\백업시스템\run.py"
        if (-not (Test-Path $backupRunPy)) {
            $foundRun = Get-ChildItem -Path $aiBase -Recurse -Filter "run.py" -ErrorAction SilentlyContinue | Where-Object { $_.FullName -like "*백업*" } | Select-Object -First 1
            if ($foundRun) { $backupRunPy = $foundRun.FullName }
        }

        if ((Test-Path $backupRunPy) -and $hermesPyFound) {
            $smokeRaw = & $hermesPy $backupRunPy --help 2>&1
            $smokeOut = ($smokeRaw -join " ").Trim()
            if ($LASTEXITCODE -eq 0 -or $smokeOut -match "Usage" -or $smokeOut -match "options" -or $smokeOut -match "help") {
                Write-Host "  [PASS] BackupSystem CLI Smoke Test executed successfully"
                $smokePass = $true
            } else {
                Write-Host "  [FAIL] Smoke test output: $smokeOut"
            }
        }
    } else {
        Write-Host "  [FAIL] $aiBase missing"
        $report.errors += "Desktop\ai missing: $aiBase"
    }

    $report.projects = [ordered]@{
        total_projects = $projList.Count
        expected_count = 19
        count_match    = ($projList.Count -ge 19)
        smoke_test     = [ordered]@{
            target = "run.py --help"
            passed = $smokePass
        }
        project_details = $projList
    }

    # 8. Android Studio 캐시 무결성
    Write-Host "`n[Step 7] Auditing Android Studio Cache Elimination..."
    $asBase = "$destDrive\Users\kksjmj\AppData\Local\Google"
    $asCount = 0
    if (Test-Path $asBase) {
        $asFiles = Get-ChildItem -Path $asBase -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.FullName -like "*AndroidStudio*\index\*" }
        $asCount = $asFiles.Count
    }
    $asEliminated = ($asCount -eq 0)
    Write-Host "  Android Studio Index Cache Files: $asCount (Target: 0)"

    $report.cache_check = [ordered]@{
        androidstudio_index_cache_files = $asCount
        clean_elimination = $asEliminated
    }

    # 9. 종합 판정
    $gate1_vhd      = ($freeGb -gt 10)
    $gate2_python   = $uvPyFound
    $gate3_hermes   = ($distCount -ge 190) -and $allImportsPass
    $gate4_node     = ($nodeVer -ne "") -and ($npmVer -ne "")
    $gate5_projects = ($projList.Count -ge 19) -and $smokePass
    $gate6_cache    = $asEliminated

    $overallPass = $gate1_vhd -and $gate2_python -and $gate3_hermes -and $gate4_node -and $gate5_projects -and $gate6_cache
    $report.overall_status = if ($overallPass) { "SUCCESS_ALL_GATES_PASSED" } else { "PARTIAL_FAIL" }

    $passedCount = @($gate1_vhd, $gate2_python, $gate3_hermes, $gate4_node, $gate5_projects, $gate6_cache).Where({$_}).Count
    $report.metrics = [ordered]@{
        gate1_vhd_space_pass       = $gate1_vhd
        gate2_uv_python_pass       = $gate2_python
        gate3_hermes_runtime_pass  = $gate3_hermes
        gate4_node_runtime_pass    = $gate4_node
        gate5_projects_smoke_pass  = $gate5_projects
        gate6_cache_clean_pass     = $gate6_cache
        total_gates_passed         = $passedCount
        total_gates                = 6
    }

    Write-Host "`n============================================================"
    if ($overallPass) {
        Write-Host "  >>> [Phase E VERDICT: ALL PASS] 6/6 GATES PERFECTLY PASSED! <<<"
    } else {
        Write-Host "  >>> [Phase E VERDICT: WARN] Passed $passedCount / 6 Gates <<<"
    }
    Write-Host "============================================================"

} finally {
    Write-Host "`n[Step 8] Safely Dismounting VHD & Restarting VM..."
    Dismount-VHD -Path $vhdPath -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    Write-Host "  VHD safely dismounted."
    
    Write-Host "  Restarting VM..."
    $vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
    if ($vm.State -ne 'Running') {
        Start-VM -VM $vm
        Start-Sleep -Seconds 10
    }
    $vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
    Write-Host "  VM Final State: $($vm.State)"
}

$reportJson = "F:\phase_e_deep_audit_report.json"
$report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $reportJson -Encoding UTF8
Write-Host "`n[Report Saved] Verification report written to: $reportJson"
"""

if __name__ == "__main__":
    import os
    local_ps1 = os.path.join(os.path.dirname(__file__), "phase_e_desktop.ps1")
    with open(local_ps1, "w", encoding="utf-8") as f:
        f.write(ps_script)
    
    print("[*] Transferring script to Desktop via SCP...")
    scp_cmd = ["scp", local_ps1, "kksjmj@100.90.20.59:F:/run_phase_e_pipeline.ps1"]
    subprocess.run(scp_cmd, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    
    print("[*] Launching Phase E Deep Audit on Desktop via -File...")
    ssh_cmd = [
        "ssh", "-o", "ConnectTimeout=15",
        "kksjmj@100.90.20.59",
        "powershell.exe -NoProfile -ExecutionPolicy Bypass -File F:\\run_phase_e_pipeline.ps1"
    ]
    proc = subprocess.run(ssh_cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
    print("STDOUT:")
    print(proc.stdout)
    if proc.stderr:
        print("STDERR:")
        print(proc.stderr)
    sys.exit(proc.returncode)
