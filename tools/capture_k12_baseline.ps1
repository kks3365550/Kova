<#
.SYNOPSIS
    K12 Ground Truth Baseline Capture Script for Emergency Restore Verification
.DESCRIPTION
    Captures K12 environment baseline (Environment Variables, PATH order,
    Installed Software, VC++ Runtimes, Task Scheduler, Non-Standard Services,
    Developer Tools, Volumes, BitLocker) into structured JSON and TXT summary.
#>

[CmdletBinding()]
param(
    [string]$ProjectBaselineDir = "c:\Users\kksjmj\Desktop\ai\백업시스템\data\baseline",
    [string]$RepoBaselineDir    = "D:\MyBackup_Repository\baseline",
    [string]$Tag                = (Get-Date -Format "yyyyMMdd_HHmmss")
)

$ErrorActionPreference = "Continue"

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " [K12 Ground Truth Baseline Capture] Tag: $Tag" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# Output directories preparation
foreach ($dir in @($ProjectBaselineDir, $RepoBaselineDir)) {
    if (-not (Test-Path $dir)) {
        try {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
            Write-Host "[+] Created directory: $dir" -ForegroundColor Green
        } catch {
            Write-Warning "[-] Failed creating directory ($dir): $_"
        }
    }
}

$osCaption = (Get-CimInstance Win32_OperatingSystem).Caption
$osVersion = (Get-CimInstance Win32_OperatingSystem).Version

$baseline = [ordered]@{
    "Meta" = [ordered]@{
        "CaptureTime"   = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
        "Hostname"      = $env:COMPUTERNAME
        "Username"      = $env:USERNAME
        "UserProfile"   = $env:USERPROFILE
        "OSVersion"     = "$osCaption ($osVersion)"
        "Tag"           = $Tag
        "ScriptVersion" = "1.0.0"
    }
    "Environment"       = [ordered]@{}
    "InstalledSoftware" = @()
    "VCRuntimes"        = @()
    "DeveloperTools"    = [ordered]@{}
    "TaskScheduler"     = @()
    "NonStandardServices" = @()
    "StorageAndVolumes" = @()
    "BitLocker"         = @()
}

# 1. Environment Variables & PATH Order
Write-Host "[1/7] Capturing Environment Variables & PATH Order..." -ForegroundColor Yellow

$sysEnv = [ordered]@{}
$sysProps = (Get-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" -ErrorAction SilentlyContinue).PSObject.Properties
foreach ($p in $sysProps) {
    if ($p.Name -notmatch "^PS.*") {
        $sysEnv[$p.Name] = [string]$p.Value
    }
}

$userEnv = [ordered]@{}
$userProps = (Get-ItemProperty -Path "HKCU:\Environment" -ErrorAction SilentlyContinue).PSObject.Properties
foreach ($p in $userProps) {
    if ($p.Name -notmatch "^PS.*") {
        $userEnv[$p.Name] = [string]$p.Value
    }
}

$sysPathList = @()
if ($sysEnv["Path"]) {
    $sysPathList = $sysEnv["Path"] -split ";" | Where-Object { $_ -match "\S" }
}

$userPathList = @()
if ($userEnv["Path"]) {
    $userPathList = $userEnv["Path"] -split ";" | Where-Object { $_ -match "\S" }
}

$runtimePathList = ($env:PATH -split ";" | Where-Object { $_ -match "\S" })

$baseline["Environment"] = [ordered]@{
    "SystemVariables" = $sysEnv
    "UserVariables"   = $userEnv
    "SystemPathOrder" = $sysPathList
    "UserPathOrder"   = $userPathList
    "RuntimePathOrder"= $runtimePathList
}

# 2. Installed Applications & VC++ Runtimes (Fast Registry Scan)
Write-Host "[2/7] Scanning Installed Software & VC++ Runtimes..." -ForegroundColor Yellow

$regPaths = @(
    "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*"
)

$installedApps = @()
$vcRuntimes = @()
$seenApps = @{}

foreach ($path in $regPaths) {
    $items = Get-ItemProperty -Path $path -ErrorAction SilentlyContinue
    foreach ($item in $items) {
        $name = $item.DisplayName
        if ($name -and -not $seenApps.ContainsKey($name)) {
            $seenApps[$name] = $true
            $appEntry = [ordered]@{
                "DisplayName"     = $name
                "DisplayVersion"  = [string]$item.DisplayVersion
                "Publisher"       = [string]$item.Publisher
                "InstallDate"     = [string]$item.InstallDate
                "InstallLocation" = [string]$item.InstallLocation
            }
            $installedApps += $appEntry

            if ($name -match "Visual C\+\+|Microsoft Visual C\+\+|Redistributable") {
                $vcRuntimes += $appEntry
            }
        }
    }
}

$baseline["InstalledSoftware"] = ($installedApps | Sort-Object { $_.DisplayName })
$baseline["VCRuntimes"] = ($vcRuntimes | Sort-Object { $_.DisplayName })

# 3. Developer Tools (Python, Git, Node)
Write-Host "[3/7] Diagnosing Developer Toolchains (Python, Git, Node)..." -ForegroundColor Yellow

$pythonList = @()
$pythonExes = (Get-Command python*, pythonw* -All -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -Unique)
foreach ($py in $pythonExes) {
    try {
        $ver = & $py --version 2>&1
        $pythonList += [ordered]@{
            "Path"    = $py
            "Version" = ($ver -join " ").Trim()
        }
    } catch {}
}

$pipPackages = @()
try {
    $pipOut = python -m pip list --format=json 2>&1
    if ($LASTEXITCODE -eq 0) {
        $pipObj = $pipOut | ConvertFrom-Json -ErrorAction SilentlyContinue
        foreach ($pkg in $pipObj) {
            $pipPackages += [ordered]@{ "Name" = $pkg.name; "Version" = $pkg.version }
        }
    }
} catch {}

$gitVer = $null
$gitConfigGlobal = @()
try {
    $gitVer = (git --version 2>&1) -join " "
    $configs = git config --global --list 2>&1
    foreach ($cfg in $configs) {
        if ($cfg -match "^(.*?)=(.*)$") {
            $gitConfigGlobal += [ordered]@{ "Key" = $Matches[1]; "Value" = $Matches[2] }
        }
    }
} catch {}

$nodeVer = $null
$npmVer = $null
try { $nodeVer = (node -v 2>&1) -join " " } catch {}
try { $npmVer = (npm -v 2>&1) -join " " } catch {}

$baseline["DeveloperTools"] = [ordered]@{
    "PythonExecutables" = $pythonList
    "PipPackagesCount"  = $pipPackages.Count
    "PipPackages"       = $pipPackages
    "GitVersion"        = $gitVer
    "GitConfigGlobal"   = $gitConfigGlobal
    "NodeVersion"       = $nodeVer
    "NpmVersion"        = $npmVer
}

# 4. Windows Task Scheduler
Write-Host "[4/7] Capturing Active Task Scheduler Entries..." -ForegroundColor Yellow

$tasks = @()
try {
    $schedTasks = Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.State -ne "Disabled" }
    foreach ($t in $schedTasks) {
        $actionStr = ($t.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join "; "
        $tasks += [ordered]@{
            "TaskName" = $t.TaskName
            "TaskPath" = $t.TaskPath
            "State"    = [string]$t.State
            "Action"   = $actionStr
        }
    }
} catch {
    Write-Warning "[-] Error getting scheduled tasks: $_"
}
$baseline["TaskScheduler"] = $tasks

# 5. Non-Standard Services (Auto start)
Write-Host "[5/7] Capturing Auto-Start Services..." -ForegroundColor Yellow

$services = @()
try {
    $autoSvcs = Get-CimInstance Win32_Service -Filter "StartMode='Auto'" -ErrorAction SilentlyContinue
    foreach ($svc in $autoSvcs) {
        $services += [ordered]@{
            "Name"        = $svc.Name
            "DisplayName" = $svc.DisplayName
            "State"       = $svc.State
            "PathName"    = $svc.PathName
        }
    }
} catch {
    Write-Warning "[-] Error getting services: $_"
}
$baseline["NonStandardServices"] = $services

# 6. Storage and Volumes
Write-Host "[6/7] Analyzing Disk Volumes..." -ForegroundColor Yellow

$volumes = @()
try {
    $vols = Get-Volume -ErrorAction SilentlyContinue | Where-Object { $_.DriveLetter }
    foreach ($v in $vols) {
        $volumes += [ordered]@{
            "DriveLetter"     = [string]$v.DriveLetter
            "FileSystemLabel" = [string]$v.FileSystemLabel
            "FileSystem"      = [string]$v.FileSystem
            "SizeGB"          = [math]::Round($v.Size / 1GB, 2)
            "SizeRemainingGB" = [math]::Round($v.SizeRemaining / 1GB, 2)
        }
    }
} catch {
    Write-Warning "[-] Error getting volumes: $_"
}
$baseline["StorageAndVolumes"] = $volumes

# 7. BitLocker Status
Write-Host "[7/7] Checking BitLocker Status..." -ForegroundColor Yellow

$bitLockerStatus = @()
try {
    $blVols = Get-BitLockerVolume -ErrorAction SilentlyContinue
    foreach ($bl in $blVols) {
        $bitLockerStatus += [ordered]@{
            "MountPoint"        = [string]$bl.MountPoint
            "VolumeStatus"      = [string]$bl.VolumeStatus
            "ProtectionStatus"  = [string]$bl.ProtectionStatus
            "EncryptionMethod"  = [string]$bl.EncryptionMethod
            "KeyProtectorCount" = $bl.KeyProtector.Count
            "KeyProtectorTypes" = ($bl.KeyProtector | Select-Object -ExpandProperty KeyProtectorType)
        }
    }
} catch {
    $bitLockerStatus += [ordered]@{ "Notice" = "BitLocker cmdlet unavailable or not active" }
}
$baseline["BitLocker"] = $bitLockerStatus

# Output Generation
$jsonContent = ($baseline | ConvertTo-Json -Depth 10)
$jsonContent = $jsonContent.Replace("`r`n", "`n").Replace("`n", "`r`n")

$summarySb = New-Object System.Text.StringBuilder
[void]$summarySb.AppendLine("============================================================")
[void]$summarySb.AppendLine(" [K12 Ground Truth Baseline Summary Report]")
[void]$summarySb.AppendLine("============================================================")
[void]$summarySb.AppendLine("Capture Time   : $($baseline.Meta.CaptureTime)")
[void]$summarySb.AppendLine("Host Name      : $($baseline.Meta.Hostname)")
[void]$summarySb.AppendLine("User Name      : $($baseline.Meta.Username)")
[void]$summarySb.AppendLine("User Profile   : $($baseline.Meta.UserProfile)")
[void]$summarySb.AppendLine("OS Version     : $($baseline.Meta.OSVersion)")
[void]$summarySb.AppendLine("------------------------------------------------------------")
[void]$summarySb.AppendLine("1. Environment & PATH")
[void]$summarySb.AppendLine(" - System Variables : $($baseline.Environment.SystemVariables.Count)")
[void]$summarySb.AppendLine(" - User Variables   : $($baseline.Environment.UserVariables.Count)")
[void]$summarySb.AppendLine(" - System PATH Items: $($baseline.Environment.SystemPathOrder.Count)")
[void]$summarySb.AppendLine(" - User PATH Items  : $($baseline.Environment.UserPathOrder.Count)")
[void]$summarySb.AppendLine("2. Software & Runtimes")
[void]$summarySb.AppendLine(" - Installed Apps   : $($baseline.InstalledSoftware.Count)")
[void]$summarySb.AppendLine(" - VC++ Runtimes    : $($baseline.VCRuntimes.Count)")
[void]$summarySb.AppendLine("3. Developer Tools")
[void]$summarySb.AppendLine(" - Detected Pythons : $($baseline.DeveloperTools.PythonExecutables.Count)")
[void]$summarySb.AppendLine(" - pip Packages     : $($baseline.DeveloperTools.PipPackagesCount)")
[void]$summarySb.AppendLine(" - Git Version      : $($baseline.DeveloperTools.GitVersion)")
[void]$summarySb.AppendLine(" - Node Version     : $($baseline.DeveloperTools.NodeVersion)")
[void]$summarySb.AppendLine("4. Automation & Services")
[void]$summarySb.AppendLine(" - Active Tasks     : $($baseline.TaskScheduler.Count)")
[void]$summarySb.AppendLine(" - Auto Services    : $($baseline.NonStandardServices.Count)")
[void]$summarySb.AppendLine("5. Storage")
$driveList = ($baseline.StorageAndVolumes | ForEach-Object { "$($_.DriveLetter):" }) -join ', '
[void]$summarySb.AppendLine(" - Mounted Drives   : $driveList")
[void]$summarySb.AppendLine("============================================================")
$summaryContent = $summarySb.ToString().Replace("`r`n", "`n").Replace("`n", "`r`n")

$utf8Encoding = New-Object System.Text.UTF8Encoding($true)

$targetDirs = @($ProjectBaselineDir, $RepoBaselineDir)
foreach ($td in $targetDirs) {
    if (Test-Path $td) {
        $jsonPath = Join-Path $td "k12_baseline_${Tag}.json"
        $txtPath  = Join-Path $td "k12_baseline_${Tag}_summary.txt"
        
        [System.IO.File]::WriteAllText($jsonPath, $jsonContent, $utf8Encoding)
        [System.IO.File]::WriteAllText($txtPath, $summaryContent, $utf8Encoding)
        
        Write-Host "[OK] Saved JSON: $jsonPath" -ForegroundColor Green
        Write-Host "[OK] Saved Summary: $txtPath" -ForegroundColor Green
    }
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " [SUCCESS] K12 Ground Truth Baseline Captured!" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
