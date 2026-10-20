
Import-Module Hyper-V -ErrorAction Stop

$targetId = [System.Guid]"4bd21edb-b90c-482c-8427-7341b7add3cb"
$vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1

if ($vm.State -ne 'Off') {
    Write-Host "[Step 1] Stopping VM completely..."
    Stop-VM -VM $vm -TurnOff -Force
    Start-Sleep -Seconds 5
    $vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
}

$hd = Get-VMHardDiskDrive -VM $vm | Select-Object -First 1
$vhdxPath = $hd.Path
Write-Host "VHDX Path: $vhdxPath"

$mount = Mount-VHD -Path $vhdxPath -Passthru
$diskNum = $mount.DiskNumber
Start-Sleep -Seconds 2

$targetPart = Get-Partition -DiskNumber $diskNum | Where-Object { $_.DriveLetter } | Sort-Object Size -Descending | Select-Object -First 1
$d = $targetPart.DriveLetter
$destDrive = "$($d):\"
Write-Host "Mounted as: $destDrive"

try {
    Write-Host "`n=== 1. Searching for UV CPython Executable in $destDrive ==="
    $uvPyCandidates = @(
        "$destDrive\Users\kksjmj\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe",
        "$destDrive\Users\kksjmj\AppData\Roaming\uv\python\cpython-3.11.11-windows-x86_64-none\python.exe",
        "$destDrive\Users\kksjmj\AppData\Roaming\uv\python\cpython-3.11.11-windows-x86_64-none\install\python.exe"
    )

    $realPy = $null
    foreach ($cand in $uvPyCandidates) {
        Write-Host "Checking: $cand -> $(Test-Path $cand)"
        if (Test-Path $cand) {
            $realPy = $cand
            break
        }
    }

    if ($realPy) {
        $targetLinecache = "$destDrive\Users\kksjmj\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\Lib\linecache.py"
        if (-not (Test-Path $targetLinecache) -and (Test-Path "F:\linecache.py")) {
            Copy-Item "F:\linecache.py" $targetLinecache -Force
            Write-Host "[INJECT] Restored linecache.py to $targetLinecache"
        }

        $pyVer = (& $realPy --version 2>&1).Trim()
        Write-Host "`n[PASS] Python Version: $pyVer (from $realPy)"

        Write-Host "`n=== 2. Auditing 206 Packages in Hermes venv site-packages ==="
        $hermesSite = "$destDrive\Users\kksjmj\AppData\Local\hermes\hermes-agent\venv\Lib\site-packages"
        if (Test-Path $hermesSite) {
            $distInfos = Get-ChildItem -Path $hermesSite -Directory -Filter "*.dist-info"
            $pkgs = Get-ChildItem -Path $hermesSite -Directory | Where-Object { $_.Name -notlike "*.dist-info" -and $_.Name -notlike "*.egg-info" -and $_.Name -ne "__pycache__" }
            Write-Host "[FOUND] Site-packages: $($distInfos.Count) dist-info packages, $($pkgs.Count) package dirs"
        }

        Write-Host "`n=== 3. Executing Real Imports via CPython ==="
        $testStd = & $realPy -c "import sys; print('SYS.PATH:', sys.path); import linecache; print('LINECACHE: OK')" 2>&1
        Write-Host "Stdlib Test: $testStd"

        $modules = @("fastapi", "pandas", "numpy", "httpx", "cryptography", "uvicorn", "pydantic", "openai", "google_genai", "onnxruntime")
        $importParts = $modules | ForEach-Object { "import $_; print('[$_] VER:' + str(getattr($_, '__version__', 'OK')))" }
        $importCmd = "import sys; sys.path.append(r'$hermesSite'); " + ($importParts -join "; ")
        $out = & $realPy -c $importCmd 2>&1
        Write-Host "Import Output:"
        $out | ForEach-Object { Write-Host "  $_" }

        Write-Host "`n=== 4. Auditing 19 AI Projects in Desktop\ai ==="
        $aiBase = "$destDrive\Users\kksjmj\Desktop\ai"
        if (Test-Path $aiBase) {
            $projs = Get-ChildItem -Path $aiBase -Directory | Sort-Object Name
            Write-Host "Found $($projs.Count) AI projects:"
            foreach ($p in $projs) {
                $fCount = (Get-ChildItem -Path $p.FullName -Recurse -File -ErrorAction SilentlyContinue).Count
                Write-Host " - $($p.Name): $fCount files"
            }

            Write-Host "`n=== 5. BackupSystem CLI Smoke Test ==="
            $backupProj = $projs | Where-Object { $_.Name -like "*백업*" } | Select-Object -First 1
            if ($backupProj) {
                $backupRun = Join-Path $backupProj.FullName "run.py"
                Write-Host "Resolved run.py: $backupRun"
                if (Test-Path $backupRun) {
                    $smokeCmd = "import sys; sys.path.insert(0, r'$hermesSite'); sys.argv = [r'$backupRun', '--help']; import runpy; runpy.run_path(r'$backupRun', run_name='__main__')"
                    $smokeOut = & $realPy -c $smokeCmd 2>&1
                    Write-Host "Smoke Test Output (First 6 lines):"
                    $smokeOut | Select-Object -First 6 | ForEach-Object { Write-Host "  $_" }
                }
            }
        } else {
            Write-Host "[FAIL] AI Base not found: $aiBase"
        }
    }

} finally {
    Write-Host "`n[Finally] Dismounting VHDX and Restarting VM..."
    Dismount-VHD -Path $vhdxPath -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    Start-VM -VM $vm
    Start-Sleep -Seconds 5
    $vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
    Write-Host "VM State: $($vm.State)"
}
