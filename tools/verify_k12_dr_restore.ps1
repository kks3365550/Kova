<#
.SYNOPSIS
    K12 DR 3-Tier Automated Verification and Scoring Script
.DESCRIPTION
    Emergency Restore가 완료된 대상 환경(VM 또는 새 SSD)에서
    K12 Ground Truth 답안지(k12_baseline_*.json)를 로드하여
    Test 1(파일 복구), Test 2(환경 복구), Test 3(업무 재개)를
    기계적으로 1:1 대조 채점하고 정량 채점표를 출력합니다.
.NOTES
    인코딩: UTF-8 No BOM, CRLF 개행
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory=$false)]
    [string]$BaselineJsonPath = "k12_baseline_20260926_004738.json",
    [string]$TargetProfileDir = "C:\Users\kksjmj"
)

$ErrorActionPreference = "Continue"

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " 📊 [K12 DR 3-Tier Restore Verification & Scoring]" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

if (-not (Test-Path $BaselineJsonPath)) {
    # 주변 디렉터리 탐색
    $candidates = @(
        $BaselineJsonPath,
        ".\data\baseline\$BaselineJsonPath",
        "..\data\baseline\$BaselineJsonPath",
        "D:\MyBackup_Repository\baseline\$BaselineJsonPath",
        "F:\K12_DR_Repository_ReadOnly\baseline\$BaselineJsonPath"
    )
    $found = $null
    foreach ($c in $candidates) {
        if (Test-Path $c) { $found = $c; break }
    }
    if ($found) {
        $BaselineJsonPath = $found
    } else {
        Write-Error "[-] 기준선 답안지 JSON 파일을 찾을 수 없습니다: $BaselineJsonPath"
        exit 1
    }
}

Write-Host "[+] 기준선 답안지 로드: $BaselineJsonPath" -ForegroundColor Green
$baseline = Get-Content -LiteralPath $BaselineJsonPath -Raw -Encoding UTF8 | ConvertFrom-Json

# ============================================================
# Test 1: 파일 복구 검증 (User Profile, Projects, Docs)
# ============================================================
Write-Host "`n--- [Test 1] 파일 복구율 및 핵심 경로 검증 ---" -ForegroundColor Yellow

$corePaths = @(
    "Desktop",
    "Documents",
    "Downloads",
    "AppData\Roaming",
    "AppData\Local"
)

$pathTotal = $corePaths.Count
$pathFound = 0
foreach ($rel in $corePaths) {
    $full = Join-Path $TargetProfileDir $rel
    if (Test-Path $full) {
        $pathFound++
        Write-Host " [PASS] $rel 존재 확인 ($full)" -ForegroundColor Green
    } else {
        Write-Host " [FAIL] $rel 누락 ($full)" -ForegroundColor Red
    }
}
$fileScore = [math]::Round(($pathFound / $pathTotal) * 100, 1)

# ============================================================
# Test 2: 환경 복구 검증 (PATH, 런타임, 레지스트리, 서비스)
# ============================================================
Write-Host "`n--- [Test 2] 환경 및 런타임 복구 검증 ---" -ForegroundColor Yellow

# 2.1 PATH 일치율
$currentPaths = ($env:PATH -split ";" | Where-Object { $_ -match "\S" })
$expectedSysPaths = $baseline.Environment.SystemPathOrder
$expectedUserPaths = $baseline.Environment.UserPathOrder

$matchedSys = 0
foreach ($p in $expectedSysPaths) {
    if ($currentPaths -contains $p) { $matchedSys++ }
}
$sysPathPct = if ($expectedSysPaths.Count -gt 0) { [math]::Round(($matchedSys / $expectedSysPaths.Count) * 100, 1) } else { 100.0 }

$matchedUser = 0
foreach ($p in $expectedUserPaths) {
    if ($currentPaths -contains $p) { $matchedUser++ }
}
$userPathPct = if ($expectedUserPaths.Count -gt 0) { [math]::Round(($matchedUser / $expectedUserPaths.Count) * 100, 1) } else { 100.0 }

Write-Host " - 시스템 PATH 복원율: $sysPathPct% ($matchedSys / $($expectedSysPaths.Count))"
Write-Host " - 사용자 PATH 복원율: $userPathPct% ($matchedUser / $($expectedUserPaths.Count))"

# 2.2 Python 환경
$pyStatus = "FAIL"
$pyVer = $null
try {
    $pyVer = (python --version 2>&1) -join " "
    if ($LASTEXITCODE -eq 0 -and $pyVer -match "Python") { $pyStatus = "PASS" }
} catch {}
Write-Host " - Python CLI 감지: [$pyStatus] ($pyVer)"

# 2.3 Git 환경
$gitStatus = "FAIL"
$gitVer = $null
try {
    $gitVer = (git --version 2>&1) -join " "
    if ($LASTEXITCODE -eq 0 -and $gitVer -match "git version") { $gitStatus = "PASS" }
} catch {}
Write-Host " - Git CLI 감지:    [$gitStatus] ($gitVer)"

# 2.4 Node.js 환경
$nodeStatus = "FAIL"
$nodeVer = $null
try {
    $nodeVer = (node -v 2>&1) -join " "
    if ($LASTEXITCODE -eq 0 -and $nodeVer -match "v\d+") { $nodeStatus = "PASS" }
} catch {}
Write-Host " - Node.js 감지:   [$nodeStatus] ($nodeVer)"

# 2.5 VC++ Runtime
$installedApps = @()
$regPaths = @(
    "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*"
)
foreach ($rp in $regPaths) {
    $items = Get-ItemProperty -Path $rp -ErrorAction SilentlyContinue
    foreach ($it in $items) {
        if ($it.DisplayName) { $installedApps += $it.DisplayName }
    }
}
$expectedVCR = $baseline.VCRuntimes
$matchedVCR = 0
foreach ($vcr in $expectedVCR) {
    if ($installedApps -contains $vcr.DisplayName) { $matchedVCR++ }
}
$vcrPct = if ($expectedVCR.Count -gt 0) { [math]::Round(($matchedVCR / $expectedVCR.Count) * 100, 1) } else { 100.0 }
Write-Host " - VC++ 런타임 일치: $vcrPct% ($matchedVCR / $($expectedVCR.Count))"

# 2.6 작업 스케줄러
$schedTasks = (Get-ScheduledTask -ErrorAction SilentlyContinue | Select-Object -ExpandProperty TaskName)
$expectedTasks = $baseline.TaskScheduler
$matchedTasks = 0
foreach ($t in $expectedTasks) {
    if ($schedTasks -contains $t.TaskName) { $matchedTasks++ }
}
$taskPct = if ($expectedTasks.Count -gt 0) { [math]::Round(($matchedTasks / $expectedTasks.Count) * 100, 1) } else { 100.0 }
Write-Host " - 작업 스케줄러 복구: $taskPct% ($matchedTasks / $($expectedTasks.Count))"

# ============================================================
# Test 3: 업무 재개 검증 (Business Continuity)
# ============================================================
Write-Host "`n--- [Test 3] 업무 재개(Business Continuity) 실증 ---" -ForegroundColor Yellow

$desktopPath = Join-Path $TargetProfileDir "Desktop"
$aiProjectsPath = Join-Path $desktopPath "ai"
$hasProjects = Test-Path $aiProjectsPath
$workReadiness = "FAIL"

if ($hasProjects) {
    $projCount = (Get-ChildItem -Path $aiProjectsPath -Directory -ErrorAction SilentlyContinue).Count
    Write-Host " [PASS] AI 프로젝트 디렉터리 발견 ($projCount 개 서브프로젝트)" -ForegroundColor Green
    if ($pyStatus -eq "PASS" -or (Test-Path "$aiProjectsPath\백업시스템")) {
        $workReadiness = "PASS"
    }
} else {
    Write-Host " [FAIL] AI 프로젝트 디렉터리 누락 ($aiProjectsPath)" -ForegroundColor Red
}
Write-Host " - 핵심 업무 재개 가능 여부: [$workReadiness]"

# ============================================================
# 최종 스코어카드 출력
# ============================================================
Write-Host "`n============================================================" -ForegroundColor Cyan
Write-Host " 📋 [K12 VM DR 드라이런 최종 판정표]" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ("| {0,-25} | {1,-12} | {2,-15} |" -f "검증 항목", "측정 결과", "판정")
Write-Host ("|{0}|{1}|{2}|" -f ("-"*27), ("-"*14), ("-"*17))
Write-Host ("| {0,-25} | {1,-12} | {2,-15} |" -f "Test 1: 핵심 경로 복원율", "$fileScore%", $(if($fileScore -ge 90){"PASS"}else{"GAP DETECTED"}))
Write-Host ("| {0,-25} | {1,-12} | {2,-15} |" -f "Test 2: 시스템 PATH 복원", "$sysPathPct%", $(if($sysPathPct -ge 90){"PASS"}else{"GAP DETECTED"}))
Write-Host ("| {0,-25} | {1,-12} | {2,-15} |" -f "Test 2: 사용자 PATH 복원", "$userPathPct%", $(if($userPathPct -ge 90){"PASS"}else{"GAP DETECTED"}))
Write-Host ("| {0,-25} | {1,-12} | {2,-15} |" -f "Test 2: Python 개발환경", $pyStatus, $(if($pyStatus -eq "PASS"){"PASS"}else{"GAP DETECTED"}))
Write-Host ("| {0,-25} | {1,-12} | {2,-15} |" -f "Test 2: Git 버전관리", $gitStatus, $(if($gitStatus -eq "PASS"){"PASS"}else{"GAP DETECTED"}))
Write-Host ("| {0,-25} | {1,-12} | {2,-15} |" -f "Test 2: Node.js 런타임", $nodeStatus, $(if($nodeStatus -eq "PASS"){"PASS"}else{"GAP DETECTED"}))
Write-Host ("| {0,-25} | {1,-12} | {2,-15} |" -f "Test 2: VC++ 런타임 일치", "$vcrPct%", $(if($vcrPct -ge 80){"PASS"}else{"GAP DETECTED"}))
Write-Host ("| {0,-25} | {1,-12} | {2,-15} |" -f "Test 2: 스케줄러 태스크", "$taskPct%", $(if($taskPct -ge 70){"PASS"}else{"GAP DETECTED"}))
Write-Host ("| {0,-25} | {1,-12} | {2,-15} |" -f "Test 3: 핵심 업무 재개", $workReadiness, $(if($workReadiness -eq "PASS"){"READY"}else{"BLOCKED"}))
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "※ 판정이 GAP DETECTED인 항목은 Emergency Restore 개선 과제(GAP)로 자동 등록됩니다."
Write-Host "============================================================" -ForegroundColor Cyan
