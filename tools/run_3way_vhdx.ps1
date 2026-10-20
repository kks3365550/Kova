
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
