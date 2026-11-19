<#
 Kova2 릴리즈 산출물 SHA256SUMS.txt 생성 스크립트.
 사용: powershell -ExecutionPolicy Bypass -File make_checksums.ps1 [-DistDir ..\dist] [-Out ..\dist\SHA256SUMS.txt]
#>
param(
    [string]$DistDir = (Join-Path $PSScriptRoot '..\dist'),
    [string]$Out     = (Join-Path $PSScriptRoot '..\dist\SHA256SUMS.txt')
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path $DistDir)) { throw "dist 폴더가 없습니다: $DistDir" }

$targets = Get-ChildItem -Path $DistDir -File | Where-Object {
    $_.Name -ne 'SHA256SUMS.txt' -and (
        $_.Extension -in @('.exe', '.zip', '.sig', '.iso')
    )
} | Sort-Object Name

if ($targets.Count -eq 0) { throw "해시를 생성할 릴리즈 자산이 없습니다: $DistDir" }

$lines = foreach ($f in $targets) {
    $h = (Get-FileHash -Path $f.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$h  $($f.Name)"
}
[IO.File]::WriteAllLines($Out, $lines, [Text.UTF8Encoding]::new($false))
Write-Host ("OK: {0} ({1}개 파일)" -f $Out, $targets.Count)
