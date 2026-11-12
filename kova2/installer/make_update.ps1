# Build the Kova2 update package: dist\release_<tag>.zip (+ .sig when a private key is given).
# Usage: powershell -File make_update.ps1 -Tag kova2-v0.1.2 [-PrivateKey C:\secure\release_priv.pem]
# The private key must stay outside the repository. Without -PrivateKey the package is built unsigned
# (the app refuses unsigned packages, so it cannot be installed by auto-update).
param(
    [Parameter(Mandatory = $true)][string]$Tag,
    [string]$PrivateKey = ""
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

cargo build --release -p kova-app -p kova-service
if ($LASTEXITCODE -ne 0) { throw "release build failed" }

$dist = Join-Path $root "dist"
New-Item -ItemType Directory -Force $dist | Out-Null
$stage = Join-Path $dist ("stage_" + $Tag)
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory $stage | Out-Null
foreach ($f in @("kova-app.exe", "kova-service.exe")) {
    $src = Join-Path $root ("target\release\" + $f)
    if (-not (Test-Path $src)) { throw "missing build output: $src" }
    Copy-Item $src $stage
}
$zip = Join-Path $dist ("release_" + $Tag + ".zip")
Remove-Item -Force $zip, ($zip + ".sig") -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip
Remove-Item -Recurse -Force $stage
Write-Output ("package: " + $zip)

if ($PrivateKey -ne "") {
    if (-not (Test-Path $PrivateKey)) { throw "private key not found: $PrivateKey" }
    $full = (Resolve-Path $PrivateKey).Path
    if ($full.StartsWith((Split-Path -Parent $root), [System.StringComparison]::OrdinalIgnoreCase)) {
        Write-Warning "private key is inside the project tree; keep it outside the repository"
    }
    cargo run --release -q -p kova-core --example sign_release -- sign $zip $PrivateKey
    if ($LASTEXITCODE -ne 0) { throw "signing failed" }
} else {
    Write-Output "unsigned package (no -PrivateKey given)"
}
