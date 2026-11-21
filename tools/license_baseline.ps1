# KOV-04: Windows/Office license baseline (READ-ONLY). Run before and after a restore, then compare the JSON files.
# Usage: powershell -ExecutionPolicy Bypass -File license_baseline.ps1 [-Out C:\path\baseline.json]
param([string]$Out = "")
$ErrorActionPreference = "Continue"

function Get-Lic {
    # Only the product name, status and last 5 chars of the key; never the full key.
    Get-CimInstance SoftwareLicensingProduct -ErrorAction SilentlyContinue |
        Where-Object { $_.PartialProductKey } |
        ForEach-Object { [ordered]@{ Name = $_.Name; AppId = $_.ApplicationID; Status = $_.LicenseStatus; Last5 = $_.PartialProductKey; Grace = $_.GracePeriodRemaining } }
}
function Get-OsppLic {
    Get-CimInstance OfficeSoftwareProtectionProduct -ErrorAction SilentlyContinue |
        Where-Object { $_.PartialProductKey } |
        ForEach-Object { [ordered]@{ Name = $_.Name; Status = $_.LicenseStatus; Last5 = $_.PartialProductKey; Grace = $_.GracePeriodRemaining } }
}
function Get-DirHash($p) {
    if (-not (Test-Path $p)) { return [ordered]@{ Path = $p; Exists = $false } }
    $files = Get-ChildItem $p -Recurse -File -Force -ErrorAction SilentlyContinue
    $rows = foreach ($f in $files) {
        $h = $null
        try { $h = (Get-FileHash $f.FullName -Algorithm SHA256 -ErrorAction Stop).Hash } catch { $h = "LOCKED" }
        "{0}|{1}|{2}" -f $f.FullName.Substring($p.Length), $f.Length, $h
    }
    $sorted = $rows | Sort-Object
    $bytes = [Text.Encoding]::UTF8.GetBytes(($sorted -join "`n"))
    $agg = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bytes)).Replace("-", "")
    [ordered]@{ Path = $p; Exists = $true; Files = @($files).Count; AggregateSha256 = $agg }
}
function Get-Suspect {
    # Files that suggest an external activator: sppc.dll in Office vfs\System, size small.
    $roots = @("C:\Program Files\Microsoft Office\root\vfs\System", "C:\Program Files\Microsoft Office\root\vfs\SystemX86", "C:\Program Files (x86)\Microsoft Office\root\vfs\System")
    foreach ($r in $roots) {
        if (Test-Path $r) {
            Get-ChildItem $r -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^sppc' } |
                ForEach-Object { [ordered]@{ Path = $_.FullName; Length = $_.Length; Modified = $_.LastWriteTime.ToString("s") } }
        }
    }
}
$xpr = (cscript //nologo "$env:windir\system32\slmgr.vbs" /xpr 2>&1 | Out-String).Trim()
$dli = (cscript //nologo "$env:windir\system32\slmgr.vbs" /dli 2>&1 | Out-String).Trim()
$ospp = $null
foreach ($p in @("C:\Program Files\Microsoft Office\Office16\ospp.vbs", "C:\Program Files (x86)\Microsoft Office\Office16\ospp.vbs")) {
    if (Test-Path $p) { $ospp = (cscript //nologo $p /dstatus 2>&1 | Out-String).Trim(); break }
}
$os = Get-CimInstance Win32_OperatingSystem
$r = [ordered]@{
    Computer     = $env:COMPUTERNAME
    CapturedAt   = (Get-Date).ToString("s")
    OS           = "$($os.Caption) $($os.Version)"
    SlmgrXpr     = $xpr
    SlmgrDli     = $dli
    OsppDstatus  = $ospp
    WindowsLic   = @(Get-Lic)
    OfficeLic    = @(Get-OsppLic)
    SuspectFiles = @(Get-Suspect)
    Stores       = @(
        (Get-DirHash "$env:windir\System32\spp\store"),
        (Get-DirHash "$env:windir\System32\spp\store\2.0"),
        (Get-DirHash "$env:ProgramData\Microsoft\OfficeSoftwareProtectionPlatform"),
        (Get-DirHash "$env:ProgramData\Microsoft\Windows\ClipSVC")
    )
}
$json = $r | ConvertTo-Json -Depth 6
if ($Out) { $json | Set-Content -Path $Out -Encoding UTF8; Write-Output "saved: $Out" } else { $json }
