# -*- coding: utf-8 -*-
Import-Module Hyper-V -ErrorAction Stop

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  [*] Phase E: Deep Runtime & Execution Verification" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

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

# 1. VM 콜드 셧다운 및 VHD ReadOnly 마운트
$targetId = [System.Guid]"4bd21edb-b90c-482c-8427-7341b7add3cb"
$vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
if (-not $vm) {
    $vm = Get-VM | Select-Object -First 1
}
Write-Host "`n[Step 1] Managing VM & Mounting VHD..."
Write-Host "  VM Found: $($vm.Name) (Id: $($vm.Id), State: $($vm.State))"
if ($vm.State -ne 'Off') {
    Write-Host "  Stopping VM completely (Stop-VM -TurnOff)..."
    Stop-VM -VM $vm -TurnOff -Force
    Start-Sleep -Seconds 5
    $vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
}

$hd = Get-VMHardDiskDrive -VM $vm | Select-Object -First 1
$vhdPath = $hd.Path
Write-Host "  VHD Path: $vhdPath"
$mount = Mount-VHD -Path $vhdPath -ReadOnly -PassThru
Start-Sleep -Seconds 3

$diskNumber = $mount.DiskNumber
$targetPart = Get-Partition -DiskNumber $diskNumber | Where-Object { $_.DriveLetter } | Sort-Object Size -Descending | Select-Object -First 1
$d = $targetPart.DriveLetter
Write-Host "  [OK] Mounted on ${d}: (ReadOnly, DiskNumber: $diskNumber)" -ForegroundColor Green

try {
    $driveInfo = Get-PSDrive $d
    $report.vhd_status = [ordered]@{
        drive_letter  = "${d}:"
        disk_number   = $diskNumber
        free_space_gb = [math]::Round($driveInfo.Free / 1GB, 2)
        used_space_gb = [math]::Round($driveInfo.Used / 1GB, 2)
    }
    Write-Host "  Drive ${d}: Free: $($report.vhd_status.free_space_gb) GB / Used: $($report.vhd_status.used_space_gb) GB"

    # 2. UV Base CPython 3.11 실행 실증
    Write-Host "`n[Step 2] Auditing UV Base CPython 3.11 Runtime..."
    $uvPy = "${d}:\Users\kksjmj\AppData\Roaming\uv\python\cpython-3.11.11-windows-x86_64-none\install\python.exe"
    $uvPyFound = $false
    $uvPyVer = ""

    if (Test-Path $uvPy) {
        $uvPyVerRaw = (& $uvPy --version 2>&1).Trim()
        Write-Host "  [FOUND] UV CPython Binary: $uvPy" -ForegroundColor Green
        Write-Host "  [EXEC] Output: $uvPyVerRaw (ExitCode: $LASTEXITCODE)" -ForegroundColor Green
        if ($LASTEXITCODE -eq 0 -and $uvPyVerRaw -match "3\.11") {
            $uvPyFound = $true
            $uvPyVer = $uvPyVerRaw
        }
    } else {
        Write-Host "  [FAIL] Missing: $uvPy" -ForegroundColor Red
        $report.errors += "UV Base CPython 3.11 not found at $uvPy"
    }

    $report.python_env = [ordered]@{
        binary_path = $uvPy
        found       = $uvPyFound
        version     = $uvPyVer
        status      = if ($uvPyFound) { "PASS" } else { "FAIL" }
    }

    # 3. Hermes venv 런타임 & 206개 pip 패키지 전수 검증
    Write-Host "`n[Step 3] Auditing Hermes venv & 206 pip Packages..."
    $hermesPy = "${d}:\Users\kksjmj\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"
    $hermesSitePkgs = "${d}:\Users\kksjmj\AppData\Local\hermes\hermes-agent\venv\Lib\site-packages"

    $hermesPyFound = Test-Path $hermesPy
    $distCount = 0
    $pkgCount = 0

    if (Test-Path $hermesSitePkgs) {
        $distInfos = Get-ChildItem -Path $hermesSitePkgs -Directory -Filter "*.dist-info"
        $distCount = $distInfos.Count
        $pkgs = Get-ChildItem -Path $hermesSitePkgs -Directory | Where-Object { $_.Name -notlike "*.dist-info" -and $_.Name -notlike "*.egg-info" -and $_.Name -ne "__pycache__" }
        $pkgCount = $pkgs.Count
        Write-Host "  [FOUND] Site-packages at: $hermesSitePkgs" -ForegroundColor Green
        Write-Host "  [COUNT] Installed dist-info: $distCount (Reference: ~206 packages)" -ForegroundColor Green
        Write-Host "  [COUNT] Distinct package dirs: $pkgCount" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] Hermes site-packages missing: $hermesSitePkgs" -ForegroundColor Red
        $report.errors += "Hermes site-packages not found at $hermesSitePkgs"
    }

    # Hermes Python 직접 실행 및 핵심 패키지 임포트 실증
    $importResults = [ordered]@{}
    $modulesToTest = @("torch", "fastapi", "pandas", "httpx", "cryptography", "uvicorn", "pydantic")
    $allImportsPass = $false

    if ($hermesPyFound) {
        Write-Host "  [EXEC] Hermes Python: (& $hermesPy --version)"
        $hVer = (& $hermesPy --version 2>&1).Trim()
        Write-Host "  [FOUND] Hermes Python Version: $hVer" -ForegroundColor Green

        $failedModules = @()
        foreach ($mod in $modulesToTest) {
            $testCmd = "import $mod; print('VER:' + str(getattr($mod, '__version__', 'OK')))"
            $out = & $hermesPy -c $testCmd 2>&1
            $exitCode = $LASTEXITCODE
            if ($exitCode -eq 0 -and ($out -match 'VER:')) {
                $verStr = ($out | Select-String -Pattern 'VER:(.*)').Matches.Groups[1].Value
                Write-Host "    * [PASS] import $mod -> Version: $verStr" -ForegroundColor Green
                $importResults[$mod] = [ordered]@{ status = "PASS"; version = $verStr }
            } else {
                $errStr = ($out -join " ").Trim()
                Write-Host "    * [FAIL] import $mod (ExitCode: $exitCode) -> $errStr" -ForegroundColor Red
                $importResults[$mod] = [ordered]@{ status = "FAIL"; error = $errStr }
                $failedModules += $mod
            }
        }
        $allImportsPass = ($failedModules.Count -eq 0)
    } else {
        Write-Host "  [FAIL] Hermes python.exe missing: $hermesPy" -ForegroundColor Red
        $report.errors += "Hermes python.exe missing"
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

    # 4. Node.js & npm 런타임 검증
    Write-Host "`n[Step 4] Auditing Node.js & npm Runtime..."
    $nodeExe = "${d}:\Users\kksjmj\AppData\Local\hermes\node\node.exe"
    $npmCmd  = "${d}:\Users\kksjmj\AppData\Local\hermes\node\npm.cmd"

    $nodeVer = ""
    $npmVer  = ""
    if (Test-Path $nodeExe) {
        $nodeVer = (& $nodeExe -v 2>&1).Trim()
        Write-Host "  [FOUND] Node.js: $nodeVer (Path: $nodeExe)" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] node.exe missing: $nodeExe" -ForegroundColor Red
        $report.errors += "node.exe missing"
    }

    if (Test-Path $npmCmd) {
        $npmVer = (& cmd /c "$npmCmd -v" 2>&1).Trim()
        Write-Host "  [FOUND] npm: $npmVer (Path: $npmCmd)" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] npm.cmd missing: $npmCmd" -ForegroundColor Red
        $report.errors += "npm.cmd missing"
    }

    $report.node_env = [ordered]@{
        node_path    = $nodeExe
        node_version = $nodeVer
        npm_path     = $npmCmd
        npm_version  = $npmVer
        status       = if ($nodeVer -and $npmVer) { "PASS" } else { "FAIL" }
    }

    # 5. 19개 AI 프로젝트 디렉토리 및 핵심 파일 검증
    Write-Host "`n[Step 5] Auditing 19 AI Projects in Desktop\ai..."
    $aiBase = "${d}:\Users\kksjmj\Desktop\ai"
    $projList = @()
    if (Test-Path $aiBase) {
        $projects = Get-ChildItem -Path $aiBase -Directory | Sort-Object Name
        Write-Host "  Found $($projects.Count) project directories in $aiBase"
        foreach ($proj in $projects) {
            $files = Get-ChildItem -Path $proj.FullName -Recurse -File -ErrorAction SilentlyContinue
            $entrypoints = @()
            $candidates = @("run.py", "main.py", "app.py", "server.py", "package.json", "index.html", "requirements.txt")
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
    } else {
        Write-Host "  [FAIL] Desktop\ai directory missing: $aiBase" -ForegroundColor Red
        $report.errors += "Desktop\ai directory missing"
    }

    # Smoke Test: 백업시스템 run.py --help
    $backupRunPy = "${d}:\Users\kksjmj\Desktop\ai\백업시스템\run.py"
    $smokePass = $false
    $smokeOutput = ""
    if ((Test-Path $backupRunPy) -and $hermesPyFound) {
        $smokeRaw = & $hermesPy $backupRunPy --help 2>&1
        $smokeExit = $LASTEXITCODE
        $smokeOutput = ($smokeRaw -join " ").Trim()
        if ($smokeExit -eq 0 -or $smokeOutput -match "Usage" -or $smokeOutput -match "options" -or $smokeOutput -match "help") {
            Write-Host "  [PASS] BackupSystem CLI Smoke Test (ExitCode: $smokeExit)" -ForegroundColor Green
            $smokePass = $true
        } else {
            Write-Host "  [FAIL] BackupSystem CLI Smoke Test: $smokeOutput" -ForegroundColor Red
            $report.errors += "BackupSystem CLI smoke test failed"
        }
    } else {
        # 한글 경로 인코딩 차이 탐색
        $foundRunPy = Get-ChildItem -Path $aiBase -Recurse -Filter "run.py" -ErrorAction SilentlyContinue | Where-Object { $_.FullName -like "*백업*" } | Select-Object -First 1
        if ($foundRunPy -and $hermesPyFound) {
            $smokeRaw = & $hermesPy $foundRunPy.FullName --help 2>&1
            $smokeExit = $LASTEXITCODE
            $smokeOutput = ($smokeRaw -join " ").Trim()
            if ($smokeExit -eq 0 -or $smokeOutput -match "Usage" -or $smokeOutput -match "options" -or $smokeOutput -match "help") {
                Write-Host "  [PASS] BackupSystem CLI Smoke Test via resolved path: $($foundRunPy.FullName)" -ForegroundColor Green
                $smokePass = $true
            }
        }
    }

    $report.projects = [ordered]@{
        total_projects = $projList.Count
        expected_count = 19
        count_match    = ($projList.Count -ge 19)
        smoke_test     = [ordered]@{
            target     = "run.py --help"
            passed     = $smokePass
            output_len = $smokeOutput.Length
        }
        project_details = $projList
    }

    # 6. Android Studio 인덱스 캐시 무결성 검증 (0건 확인)
    Write-Host "`n[Step 6] Auditing Android Studio Index Cache Elimination..."
    $asBase = "${d}:\Users\kksjmj\AppData\Local\Google"
    $asIndexFiles = @()
    if (Test-Path $asBase) {
        $asIndexFiles = Get-ChildItem -Path $asBase -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.FullName -like "*AndroidStudio*\index\*" }
    }
    $asCount = $asIndexFiles.Count
    $asEliminated = ($asCount -eq 0)
    Write-Host "  Android Studio index cache files: $asCount (Target: 0)" -ForegroundColor (if ($asEliminated) { "Green" } else { "Red" })

    $report.cache_check = [ordered]@{
        androidstudio_index_cache_files = $asCount
        clean_elimination = $asEliminated
    }

    # 7. 종합 판정 (Overall Verdict)
    $gate1_vhd      = ($report.vhd_status.free_space_gb -gt 10)
    $gate2_python   = $uvPyFound
    $gate3_hermes   = ($distCount -ge 190) -and $allImportsPass
    $gate4_node     = ($nodeVer -ne "") -and ($npmVer -ne "")
    $gate5_projects = ($projList.Count -ge 19) -and $smokePass
    $gate6_cache    = $asEliminated

    $overallPass = $gate1_vhd -and $gate2_python -and $gate3_hermes -and $gate4_node -and $gate5_projects -and $gate6_cache
    $report.overall_status = if ($overallPass) { "SUCCESS_ALL_GATES_PASSED" } else { "PARTIAL_FAIL" }

    $report.metrics = [ordered]@{
        gate1_vhd_space_pass       = $gate1_vhd
        gate2_uv_python_pass       = $gate2_python
        gate3_hermes_runtime_pass  = $gate3_hermes
        gate4_node_runtime_pass    = $gate4_node
        gate5_projects_smoke_pass  = $gate5_projects
        gate6_cache_clean_pass     = $gate6_cache
        total_gates_passed         = @($gate1_vhd, $gate2_python, $gate3_hermes, $gate4_node, $gate5_projects, $gate6_cache).Where({$_}).Count
        total_gates                = 6
    }

    Write-Host "`n============================================================" -ForegroundColor Cyan
    if ($overallPass) {
        Write-Host "  >>> [Phase E VERDICT: ALL PASS] 6/6 GATES PERFECTLY PASSED! <<<" -ForegroundColor Green
    } else {
        Write-Host "  >>> [Phase E VERDICT: WARN] SOME GATES FAILED ($($report.metrics.total_gates_passed)/6) <<<" -ForegroundColor Yellow
    }
    Write-Host "============================================================" -ForegroundColor Cyan

} finally {
    Write-Host "`n[Step 7] Safely Dismounting VHD & Restarting VM..."
    if ($vhdPath -and (Test-Path $vhdPath)) {
        Dismount-VHD -Path $vhdPath -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 3
    Write-Host "  VHD dismounted." -ForegroundColor Green
    
    Write-Host "  Starting VM for post-verification run..."
    $vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
    if ($vm -and $vm.State -ne 'Running') {
        Start-VM -VM $vm
        Start-Sleep -Seconds 10
    }
    $vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
    Write-Host "  VM Final State: $($vm.State)" -ForegroundColor Green
}

# 8. Save Report JSON
$reportJson = "F:\phase_e_deep_audit_report.json"
$report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $reportJson -Encoding UTF8
Write-Host "`n[Report Saved] Verification report written to: $reportJson" -ForegroundColor Cyan
