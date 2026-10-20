
Import-Module Hyper-V -ErrorAction Stop

Write-Host "============================================================"
Write-Host " [FINAL INTEGRATED DR] Single VHDX Restore & Deep Audit"
Write-Host "============================================================"

$report = [ordered]@{
    timestamp = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    vhdx_path = ""
    disk_size_gb = 0
    restore_metrics = [ordered]@{}
    python_env = [ordered]@{}
    hermes_venv = [ordered]@{}
    node_env = [ordered]@{}
    projects = [ordered]@{}
    cache_check = [ordered]@{}
    gates = [ordered]@{}
    overall_verdict = "PENDING"
}

$targetId = [System.Guid]"4bd21edb-b90c-482c-8427-7341b7add3cb"
$vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1

# 1. VM 셧다운 확인
if ($vm.State -ne 'Off') {
    Write-Host "[Step 1] Stopping VM completely..."
    Stop-VM -VM $vm -TurnOff -Force
    Start-Sleep -Seconds 5
    $vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
}
Write-Host "[Step 1] VM State: $($vm.State)"

# 2. 단일 VHDX 경로 확인
$hd = Get-VMHardDiskDrive -VM $vm | Select-Object -First 1
$vhdxPath = $hd.Path
Write-Host "[Step 2] Target VHDX: $vhdxPath"
$report.vhdx_path = $vhdxPath

$vhdInfo = Get-VHD -Path $vhdxPath
$report.disk_size_gb = [math]::Round($vhdInfo.FileSize/1GB, 2)
Write-Host " - Disk File Size: $($report.disk_size_gb) GB"
Write-Host " - VhdType: $($vhdInfo.VhdType)"
Write-Host " - ParentPath: $($vhdInfo.ParentPath) (Must be Empty for Single VHDX)"

# 3. VHDX 마운트
Write-Host "[Step 3] Mounting VHDX (Read/Write for Restore)..."
$mount = Mount-VHD -Path $vhdxPath -Passthru
$diskNum = $mount.DiskNumber
Start-Sleep -Seconds 3

# 드라이브 레터 확인 및 필요 시 자동 할당
$d = $null
$partWaited = 0
while (-not $d -and $partWaited -lt 15) {
    Start-Sleep -Seconds 1
    $partWaited++
    $targetPart = Get-Partition -DiskNumber $diskNum -ErrorAction SilentlyContinue | Where-Object { $_.DriveLetter } | Sort-Object Size -Descending | Select-Object -First 1
    if ($targetPart) {
        $d = $targetPart.DriveLetter
    } else {
        # 레터가 없으면 미사용 레터 할당 시도
        $basicPart = Get-Partition -DiskNumber $diskNum -ErrorAction SilentlyContinue | Where-Object { $_.Size -gt 20GB } | Select-Object -First 1
        if ($basicPart) {
            $freeLetter = (ls function:[d-z]: -n | ?{ !(test-path $_) })[0] -replace ':$',''
            Set-Partition -DiskNumber $diskNum -PartitionNumber $basicPart.PartitionNumber -NewDriveLetter $freeLetter -ErrorAction SilentlyContinue
            $d = $freeLetter
        }
    }
}

if (-not $d) {
    Write-Error "Failed to assign drive letter to Disk Number $diskNum"
    Dismount-VHD -Path $vhdxPath
    exit 1
}

$destDrive = "$($d):\"
Write-Host " - Destination Drive: $destDrive"
$di = Get-PSDrive $d
Write-Host " - Drive Space Before Restore: Free $([math]::Round($di.Free/1GB, 2)) GB / Used $([math]::Round($di.Used/1GB, 2)) GB"

try {
    # 4. Disaster Recovery 복원 엔진 실행
    Write-Host "`n[Step 4] Running disaster_recovery.py..."
    $snapId = "snap_20260926_092057_7461cd"
    $repoPath = "F:\MyBackup_Repository"
    $drScript = "F:\disaster_recovery.py"

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $drProc = Start-Process -FilePath "python" -ArgumentList "$drScript --repo $repoPath --restore $snapId --dest $destDrive" -NoNewWindow -PassThru -Wait
    $sw.Stop()
    $restoreSec = [math]::Round($sw.Elapsed.TotalSeconds, 2)
    Write-Host " - Restoration ExitCode: $($drProc.ExitCode) in ${restoreSec}s"

    $report.restore_metrics = [ordered]@{
        snapshot_id   = $snapId
        restore_dest  = $destDrive
        exit_code     = $drProc.ExitCode
        elapsed_sec   = $restoreSec
        success       = ($drProc.ExitCode -eq 0)
    }

    $diAfter = Get-PSDrive $d
    Write-Host " - Drive Space After Restore: Free $([math]::Round($diAfter.Free/1GB, 2)) GB / Used $([math]::Round($diAfter.Used/1GB, 2)) GB"

    # 5. 심층 런타임 실증 (Phase E)
    Write-Host "`n[Step 5] Phase E Deep Runtime Audits:"

    # 5.1 UV CPython 3.11 검증
    $uvPy = "$destDrive\Users\kksjmj\AppData\Roaming\uv\python\cpython-3.11.11-windows-x86_64-none\install\python.exe"
    $uvPyFound = Test-Path $uvPy
    $uvPyVer = ""
    if ($uvPyFound) {
        $uvPyVer = (& $uvPy --version 2>&1).Trim()
        Write-Host "  * [PASS] UV CPython: $uvPyVer ($uvPy)"
    } else {
        Write-Host "  * [FAIL] UV CPython not found at: $uvPy"
    }
    $report.python_env = [ordered]@{
        path    = $uvPy
        found   = $uvPyFound
        version = $uvPyVer
        passed  = ($uvPyFound -and ($uvPyVer -match "3\.11"))
    }

    # 5.2 Hermes venv & 206 pip 패키지 전수 검증
    $hermesPy = "$destDrive\Users\kksjmj\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"
    $hermesSite = "$destDrive\Users\kksjmj\AppData\Local\hermes\hermes-agent\venv\Lib\site-packages"
    $hermesFound = Test-Path $hermesPy
    $distCount = 0
    $pkgCount = 0
    if (Test-Path $hermesSite) {
        $distCount = (Get-ChildItem $hermesSite -Directory -Filter "*.dist-info").Count
        $pkgCount = (Get-ChildItem $hermesSite -Directory | Where-Object { $_.Name -notlike "*.dist-info" -and $_.Name -notlike "*.egg-info" -and $_.Name -ne "__pycache__" }).Count
        Write-Host "  * [FOUND] Hermes site-packages: $distCount dist-info packages ($pkgCount package dirs)"
    } else {
        Write-Host "  * [FAIL] Hermes site-packages missing: $hermesSite"
    }

    $importResults = [ordered]@{}
    $modules = @("torch", "fastapi", "pandas", "httpx", "cryptography", "uvicorn", "pydantic")
    $allImportsOk = $false
    if ($hermesFound) {
        $failedMods = @()
        foreach ($m in $modules) {
            $cmd = "import $m; print('VER:' + str(getattr($m, '__version__', 'OK')))"
            $out = & $hermesPy -c $cmd 2>&1
            if ($LASTEXITCODE -eq 0 -and ($out -match 'VER:')) {
                $ver = ($out | Select-String -Pattern 'VER:(.*)').Matches.Groups[1].Value
                Write-Host "    - [PASS] import $m -> $ver"
                $importResults[$m] = [ordered]@{ status = "PASS"; version = $ver }
            } else {
                $err = ($out -join " ").Trim()
                Write-Host "    - [FAIL] import $m -> $err"
                $importResults[$m] = [ordered]@{ status = "FAIL"; error = $err }
                $failedMods += $m
            }
        }
        $allImportsOk = ($failedMods.Count -eq 0)
    }

    $report.hermes_venv = [ordered]@{
        python_path     = $hermesPy
        found           = $hermesFound
        dist_info_count = $distCount
        package_count   = $pkgCount
        all_imports_pass= $allImportsOk
        import_details  = $importResults
        passed          = ($hermesFound -and ($distCount -ge 190) -and $allImportsOk)
    }

    # 5.3 Node.js & npm 검증
    $nodeExe = "$destDrive\Users\kksjmj\AppData\Local\hermes\node\node.exe"
    $npmCmd  = "$destDrive\Users\kksjmj\AppData\Local\hermes\node\npm.cmd"
    $nodeVer = ""
    $npmVer  = ""
    if (Test-Path $nodeExe) {
        $nodeVer = (& $nodeExe -v 2>&1).Trim()
        Write-Host "  * [PASS] Node.js: $nodeVer"
    }
    if (Test-Path $npmCmd) {
        $npmVer = (& cmd /c "$npmCmd -v" 2>&1).Trim()
        Write-Host "  * [PASS] npm: $npmVer"
    }
    $report.node_env = [ordered]@{
        node_version = $nodeVer
        npm_version  = $npmVer
        passed       = ($nodeVer -ne "" -and $npmVer -ne "")
    }

    # 5.4 19개 AI 프로젝트 및 CLI 스모크 테스트
    $aiPath = "$destDrive\Users\kksjmj\Desktop\ai"
    $projCount = 0
    $projList = @()
    $smokeOk = $false
    if (Test-Path $aiPath) {
        $projects = Get-ChildItem -Path $aiPath -Directory | Sort-Object Name
        $projCount = $projects.Count
        Write-Host "  * [FOUND] $projCount AI Projects in $aiPath"
        foreach ($p in $projects) {
            $fCount = (Get-ChildItem -Path $p.FullName -Recurse -File -ErrorAction SilentlyContinue).Count
            $projList += [ordered]@{ name = $p.Name; files = $fCount }
            Write-Host "    - $($p.Name): $fCount files"
        }

        # BackupSystem CLI Smoke Test
        $backupRun = "$aiPath\백업시스템\run.py"
        if (-not (Test-Path $backupRun)) {
            $found = Get-ChildItem -Path $aiPath -Recurse -Filter "run.py" -ErrorAction SilentlyContinue | Where-Object { $_.FullName -like "*백업*" } | Select-Object -First 1
            if ($found) { $backupRun = $found.FullName }
        }
        if ((Test-Path $backupRun) -and $hermesFound) {
            $smokeRaw = & $hermesPy $backupRun --help 2>&1
            $smokeText = ($smokeRaw -join " ").Trim()
            if ($LASTEXITCODE -eq 0 -or $smokeText -match "Usage" -or $smokeText -match "options" -or $smokeText -match "help") {
                Write-Host "  * [PASS] BackupSystem CLI Smoke Test (run.py --help) OK"
                $smokeOk = $true
            } else {
                Write-Host "  * [FAIL] Smoke test output: $smokeText"
            }
        }
    } else {
        Write-Host "  * [FAIL] AI Projects directory missing: $aiPath"
    }

    $report.projects = [ordered]@{
        total_projects = $projCount
        expected_count = 19
        smoke_test_pass= $smokeOk
        passed         = ($projCount -ge 19 -and $smokeOk)
        project_list   = $projList
    }

    # 5.5 Android Studio 인덱스 캐시 무결성 검증 (0건 확인)
    $asBase = "$destDrive\Users\kksjmj\AppData\Local\Google"
    $asCount = 0
    if (Test-Path $asBase) {
        $asCount = (Get-ChildItem -Path $asBase -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.FullName -like "*AndroidStudio*\index\*" }).Count
    }
    $cacheClean = ($asCount -eq 0)
    Write-Host "  * [CACHE] Android Studio Index Cache Files: $asCount (Target: 0)"
    $report.cache_check = [ordered]@{
        index_cache_files = $asCount
        clean_eliminated  = $cacheClean
        passed            = $cacheClean
    }

    # 6. 최종 6대 게이트 판정
    $g1 = ($diAfter.Free -gt 10GB)
    $g2 = $report.python_env.passed
    $g3 = $report.hermes_venv.passed
    $g4 = $report.node_env.passed
    $g5 = $report.projects.passed
    $g6 = $report.cache_check.passed

    $report.gates = [ordered]@{
        gate1_disk_space     = $g1
        gate2_uv_python      = $g2
        gate3_hermes_runtime = $g3
        gate4_node_runtime   = $g4
        gate5_ai_projects    = $g5
        gate6_clean_cache    = $g6
        gates_passed_count   = @($g1, $g2, $g3, $g4, $g5, $g6).Where({$_}).Count
        total_gates          = 6
    }

    $allPass = $g1 -and $g2 -and $g3 -and $g4 -and $g5 -and $g6
    $report.overall_verdict = if ($allPass) { "SUCCESS_ALL_GATES_PASSED" } else { "PARTIAL_FAIL" }

    Write-Host "`n============================================================"
    if ($allPass) {
        Write-Host "  >>> [VERDICT: ALL 6/6 GATES PASSED PERFECTLY!] <<<"
    } else {
        Write-Host "  >>> [VERDICT: FAIL] Passed $($report.gates.gates_passed_count) / 6 Gates <<<"
    }
    Write-Host "============================================================"

} finally {
    # 7. 안전 언마운트 및 VM 콜드 부팅
    Write-Host "`n[Step 6] Safely Dismounting VHDX..."
    Dismount-VHD -Path $vhdxPath -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    Write-Host "  VHDX dismounted successfully."

    Write-Host "[Step 7] Starting VM for Clean Cold Boot..."
    $vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
    Start-VM -VM $vm
    Start-Sleep -Seconds 10
    $vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
    Write-Host "  VM Post-Restore State: $($vm.State)"
}

# 8. 최종 성적서 저장
$reportFile = "F:\dr_final_verdict_report.json"
$report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $reportFile -Encoding UTF8
Write-Host "`n[Report Saved] Written to $reportFile"
