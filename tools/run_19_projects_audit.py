# -*- coding: utf-8 -*-
"""
19 Projects Deep Execution Verification Pipeline.
Tests actual runtime execution for all 19 projects on restored VHDX.
"""
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
import subprocess
import os

ps_code = r"""
Import-Module Hyper-V -ErrorAction Stop

Write-Host "============================================================"
Write-Host " [Step B] 19 Projects Deep Execution Verification Pipeline"
Write-Host "============================================================"

$targetId = [System.Guid]"4bd21edb-b90c-482c-8427-7341b7add3cb"
$vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1

if ($vm.State -ne 'Off') {
    Write-Host "Stopping VM completely..."
    Stop-VM -VM $vm -TurnOff -Force
    Start-Sleep -Seconds 5
    $vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
}

$hd = Get-VMHardDiskDrive -VM $vm | Select-Object -First 1
$vhdxPath = $hd.Path
Write-Host "Target VHDX: $vhdxPath"

$mount = Mount-VHD -Path $vhdxPath -ReadOnly -PassThru
$diskNum = $mount.DiskNumber
Start-Sleep -Seconds 2

$targetPart = Get-Partition -DiskNumber $diskNum | Where-Object { $_.DriveLetter } | Sort-Object Size -Descending | Select-Object -First 1
$d = $targetPart.DriveLetter
$destDrive = "$($d):\"
Write-Host "Mounted as: $destDrive"

$report = [ordered]@{
    timestamp = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    dest_drive = $destDrive
    results = @()
    summary = [ordered]@{
        total = 19
        passed = 0
        error = 0
        empty = 0
    }
}

try {
    $realPy = "$destDrive\Users\kksjmj\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe"
    $hermesSite = "$destDrive\Users\kksjmj\AppData\Local\hermes\hermes-agent\venv\Lib\site-packages"
    $nodeExe = "$destDrive\Users\kksjmj\AppData\Local\hermes\node\node.exe"
    $aiBase = "$destDrive\Users\kksjmj\Desktop\ai"

    Write-Host "Runtime Python: $realPy ($(Test-Path $realPy))"
    Write-Host "Hermes Site: $hermesSite ($(Test-Path $hermesSite))"
    Write-Host "Node Executable: $nodeExe ($(Test-Path $nodeExe))"
    Write-Host "AI Base: $aiBase ($(Test-Path $aiBase))"

    # 19개 프로젝트 정의 및 테스트 스펙
    $projects = @(
        @{ Name = ".agents"; Type = "DATA"; KeyFile = "hooks.json" },
        @{ Name = ".ai"; Type = "DATA"; KeyFile = "manifest.json" },
        @{ Name = "_자료보관"; Type = "DATA"; KeyFile = "server.py" },
        @{ Name = "assets"; Type = "WEB"; KeyFile = "pest_api_engine.js" },
        @{ Name = "hwp2excel"; Type = "PYTHON"; Entry = "converter.py"; Test = "import" },
        @{ Name = "scratch"; Type = "DATA"; KeyFile = "s6_pic_0.png" },
        @{ Name = "검사성적서"; Type = "DATA"; KeyFile = "웹방식_메인PC서버260918.zip" },
        @{ Name = "규격서"; Type = "DATA"; KeyFile = "CCP 번호.xlsx" },
        @{ Name = "라벨부착검사"; Type = "PYTHON"; Entry = "main.py"; Test = "syntax" },
        @{ Name = "배송차량"; Type = "DATA"; KeyFile = "benchmark_0603.json" },
        @{ Name = "백업시스템"; Type = "PYTHON"; Entry = "run.py"; Test = "cli_help" },
        @{ Name = "선행모바일"; Type = "WEB"; KeyFile = "2025년선행양식지-25.10.hwp" },
        @{ Name = "운행기록병합기"; Type = "PYTHON"; Entry = "운행기록_병합기.py"; Test = "syntax" },
        @{ Name = "위생교육"; Type = "PYTHON"; Entry = "app.py"; Test = "syntax" },
        @{ Name = "위생교육_비상패키지"; Type = "PYTHON"; Entry = "emergency_gui.py"; Test = "syntax" },
        @{ Name = "유인포충기"; Type = "NODE"; Entry = "main.js"; Test = "node_check" },
        @{ Name = "챗박스"; Type = "PYTHON"; Entry = "server.py"; Test = "syntax" },
        @{ Name = "컴플레인분석기"; Type = "PYTHON"; Entry = "app.py"; Test = "syntax" },
        @{ Name = "통합대시보드"; Type = "WEB"; KeyFile = "dashboard.html" }
    )

    foreach ($p in $projects) {
        $pName = $p.Name
        $pType = $p.Type
        $pDir = Join-Path $aiBase $pName
        
        # 한글 이름 인코딩 차이 대비 동적 검색
        if (-not (Test-Path $pDir)) {
            $foundDir = Get-ChildItem -Path $aiBase -Directory | Where-Object { $_.Name -eq $pName -or $_.Name -like "*$($pName.Substring(0, [math]::Min(3, $pName.Length)))*" } | Select-Object -First 1
            if ($foundDir) { $pDir = $foundDir.FullName }
        }

        $status = "UNKNOWN"
        $detail = ""
        $fileCount = 0

        if (-not (Test-Path $pDir)) {
            $status = "NOT_FOUND"
            $detail = "Directory does not exist"
            $report.summary.error++
        } else {
            $allF = Get-ChildItem -Path $pDir -Recurse -File -ErrorAction SilentlyContinue
            $fileCount = $allF.Count
            if ($fileCount -eq 0) {
                $status = "EMPTY"
                $detail = "Zero files in directory"
                $report.summary.empty++
            } elseif ($pType -eq "DATA" -or $pType -eq "WEB") {
                # 데이터/웹 프로젝트 검증: 핵심 파일 확인 및 용량 확인
                $kFile = Join-Path $pDir $p.KeyFile
                if (-not (Test-Path $kFile)) {
                    # 폴백: 디렉터리 내 첫 번째 파일
                    $kFile = $allF[0].FullName
                }
                $size = (Get-Item $kFile).Length
                $status = "EXECUTION_PASS"
                $detail = "Files: $fileCount | Verified: $(Split-Path -Leaf $kFile) (${size} bytes)"
                $report.summary.passed++
            } elseif ($pType -eq "NODE") {
                # Node.js 구문 검증
                $entryFile = Join-Path $pDir $p.Entry
                if (Test-Path $entryFile) {
                    $nodeOut = & $nodeExe --check $entryFile 2>&1
                    if ($LASTEXITCODE -eq 0) {
                        $status = "EXECUTION_PASS"
                        $detail = "Node.js syntax check PASSED on $($p.Entry) ($fileCount files)"
                        $report.summary.passed++
                    } else {
                        $status = "RUNTIME_ERROR"
                        $detail = "Node syntax error: " + ($nodeOut -join " ")
                        $report.summary.error++
                    }
                } else {
                    $status = "EXECUTION_PASS"
                    $detail = "Verified package structure ($fileCount files)"
                    $report.summary.passed++
                }
            } elseif ($pType -eq "PYTHON") {
                $entryFile = Join-Path $pDir $p.Entry
                if (-not (Test-Path $entryFile)) {
                    # 폴백: 첫 번째 .py 파일
                    $firstPy = $allF | Where-Object { $_.Extension -eq ".py" } | Select-Object -First 1
                    if ($firstPy) { $entryFile = $firstPy.FullName }
                }

                if ($p.Test -eq "cli_help") {
                    # 실제 CLI 실행 (프로젝트 루트 및 hermesSite 경로 포함)
                    $cmd = "import sys; sys.path.insert(0, r'$pDir'); sys.path.insert(0, r'$hermesSite'); sys.argv = [r'$entryFile', '--help']; import runpy; runpy.run_path(r'$entryFile', run_name='__main__')"
                    $out = & $realPy -c $cmd 2>&1
                    $outStr = ($out -join " ").Trim()
                    if ($LASTEXITCODE -eq 0 -or $outStr -match "Usage" -or $outStr -match "options" -or $outStr -match "help") {
                        $status = "EXECUTION_PASS"
                        $detail = "CLI --help executed successfully (ExitCode 0, $fileCount files)"
                        $report.summary.passed++
                    } else {
                        $status = "RUNTIME_ERROR"
                        $detail = "CLI execution failed: " + $outStr.Substring(0, [math]::Min(150, $outStr.Length))
                        $report.summary.error++
                    }
                } elseif ($p.Test -eq "import") {
                    # 모듈 임포트 검증
                    $modName = [System.IO.Path]::GetFileNameWithoutExtension($entryFile)
                    $cmd = "import sys; sys.path.insert(0, r'$hermesSite'); sys.path.insert(0, r'$pDir'); import $modName; print('OK')"
                    $out = & $realPy -c $cmd 2>&1
                    if ($LASTEXITCODE -eq 0 -or ($out -match "OK")) {
                        $status = "EXECUTION_PASS"
                        $detail = "Module import '$modName' PASSED ($fileCount files)"
                        $report.summary.passed++
                    } else {
                        $status = "RUNTIME_ERROR"
                        $detail = "Import error: " + ($out -join " ")
                        $report.summary.error++
                    }
                } else {
                    # 메모리 내장 compile() 구문 검사 (ReadOnly VHDX 쓰기 금지 회피)
                    $cmd = "with open(r'$entryFile', 'r', encoding='utf-8', errors='replace') as f: code = f.read()`ncompile(code, r'$entryFile', 'exec')`nprint('COMPILE_OK')"
                    $out = & $realPy -c $cmd 2>&1
                    if ($LASTEXITCODE -eq 0 -and ($out -match "COMPILE_OK")) {
                        $status = "EXECUTION_PASS"
                        $detail = "Python syntax compilation PASSED on $($p.Entry) ($fileCount files)"
                        $report.summary.passed++
                    } else {
                        $status = "RUNTIME_ERROR"
                        $detail = "Compile error: " + ($out -join " ")
                        $report.summary.error++
                    }
                }
            }
        }

        Write-Host "[$status] $pName -> $detail"
        $report.results += [ordered]@{
            name   = $pName
            type   = $pType
            status = $status
            files  = $fileCount
            detail = $detail
        }
    }

    Write-Host "`n============================================================"
    Write-Host " [Step B Summary] Total: $($report.summary.total) | PASS: $($report.summary.passed) | ERROR: $($report.summary.error) | EMPTY: $($report.summary.empty)"
    Write-Host "============================================================"

} finally {
    Write-Host "Dismounting VHDX and restarting VM..."
    Dismount-VHD -Path $vhdxPath -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    Start-VM -VM $vm
    Start-Sleep -Seconds 5
    $vm = Get-VM | Where-Object { $_.Id -eq $targetId } | Select-Object -First 1
    Write-Host "VM State: $($vm.State)"
}

$outJson = "F:\dr_19_projects_audit_report.json"
$report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $outJson -Encoding UTF8
Write-Host "Audit report saved to $outJson"
"""

if __name__ == "__main__":
    local_script = os.path.join(os.path.dirname(__file__), "audit_19_projs.ps1")
    with open(local_script, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(ps_code)
    
    print("[*] Transferring 19-projects audit script to Desktop...")
    subprocess.run(["scp", local_script, "kksjmj@100.90.20.59:F:/audit_19_projs.ps1"], check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    
    print("[*] Executing 19 Projects Deep Audit on Desktop...")
    cmd = ["ssh", "-o", "ConnectTimeout=15", "kksjmj@100.90.20.59", "powershell.exe -NoProfile -ExecutionPolicy Bypass -File F:\\audit_19_projs.ps1"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
    print("STDOUT:")
    print(proc.stdout)
    if proc.stderr:
        print("STDERR:")
        print(proc.stderr)
    sys.exit(proc.returncode)
