#Requires -RunAsAdministrator
<#
 Kova2 WinPE 복구 미디어(ISO) 만들기.
 필요: Windows ADK + WinPE 애드온(이 PC에는 설치돼 있음). 관리자 PowerShell.
 사용: powershell -ExecutionPolicy Bypass -File make_pe.ps1 [-Out dist\kova_pe.iso] [-Exe target\release\kova-pe.exe]
 ISO 는 Rufus 같은 도구나 MakeWinPEMedia /UFD 로 USB 에 쓸 수 있다.
#>
param(
    [string]$Out  = (Join-Path $PSScriptRoot '..\dist\kova_pe.iso'),
    [string]$Exe  = (Join-Path $PSScriptRoot '..\target\release\kova-pe.exe'),
    [string]$Work = (Join-Path $env:TEMP 'kova_pe_work')
)
$ErrorActionPreference = 'Stop'
$kit = 'C:\Program Files (x86)\Windows Kits\10\Assessment and Deployment Kit'
$env_bat = Join-Path $kit 'Deployment Tools\DandISetEnv.bat'
if (-not (Test-Path $env_bat)) { throw "Windows ADK 가 없습니다: $kit" }
if (-not (Test-Path $Exe)) { throw "kova-pe.exe 가 없습니다. 먼저 cargo build --release -p kova-pe 를 실행하세요: $Exe" }
$Out = [IO.Path]::GetFullPath($Out); $Exe = [IO.Path]::GetFullPath($Exe)
New-Item -ItemType Directory -Force (Split-Path $Out) | Out-Null

function Run-Dandi($cmd) {
    cmd /c "call `"$env_bat`" >nul && $cmd"
    if ($LASTEXITCODE -ne 0) { throw "실패(종료 코드 $LASTEXITCODE): $cmd" }
}

if (Test-Path $Work) { Remove-Item $Work -Recurse -Force }
Run-Dandi "copype amd64 `"$Work`""
$mount = Join-Path $Work 'mount'
$wim = Join-Path $Work 'media\sources\boot.wim'
dism /Mount-Image /ImageFile:$wim /Index:1 /MountDir:$mount | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'boot.wim 마운트 실패' }
$ok = $false
try {
    Copy-Item $Exe (Join-Path $mount 'Windows\System32\kova-pe.exe') -Force
    # startnet.cmd: 네트워크 초기화 후 복원 프로그램 실행 (ASCII, CRLF)
    $startnet = "wpeinit`r`nkova-pe.exe`r`n"
    [IO.File]::WriteAllText((Join-Path $mount 'Windows\System32\startnet.cmd'), $startnet, [Text.Encoding]::ASCII)
    $ok = $true
}
finally {
    if ($ok) { dism /Unmount-Image /MountDir:$mount /Commit | Out-Null } else { dism /Unmount-Image /MountDir:$mount /Discard | Out-Null }
}
if (-not $ok) { throw 'boot.wim 수정 실패' }
# "Press any key to boot from CD/DVD" 대기가 없는 부팅 이미지를 쓴다(무인 복구·가상머신 시험에서 키 입력이 없어도 부팅).
foreach ($pair in @(@('efisys_noprompt.bin', 'efisys.bin'), @('efisys_noprompt_EX.bin', 'efisys_EX.bin'))) {
    $src = Join-Path $Work "bootbins\$($pair[0])"
    if (Test-Path $src) { Copy-Item $src (Join-Path $Work "bootbins\$($pair[1])") -Force }
}
if (Test-Path $Out) { Remove-Item $Out -Force }
Run-Dandi "MakeWinPEMedia /ISO `"$Work`" `"$Out`""
Remove-Item $Work -Recurse -Force -ErrorAction SilentlyContinue
Write-Host ("OK: {0} ({1:N0} MB)" -f $Out, ((Get-Item $Out).Length / 1MB))
