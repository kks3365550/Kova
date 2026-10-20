# find_restored_files.ps1
$vm = Get-VM | Select-Object -First 1
if ($vm.State -ne 'Off') {
    Stop-VM $vm -Save
    Start-Sleep -Seconds 2
}

$vhdPath = $vm.HardDrives[0].Path
$mount = Mount-VHD -Path $vhdPath -ReadOnly -PassThru
Start-Sleep -Seconds 3

$part = Get-Partition -DiskNumber $mount.DiskNumber | Where-Object DriveLetter | Select-Object -First 1
$d = $part.DriveLetter

try {
    Write-Host "Mounted on ${d}:"
    Write-Host "`nSearching for 'hermes' directories on ${d}:\"
    Get-ChildItem "${d}:\" -Recurse -Directory -Filter "hermes" -ErrorAction SilentlyContinue | Select-Object FullName

    Write-Host "`nSearching for 'cpython-3.11*' directories on ${d}:\"
    Get-ChildItem "${d}:\" -Recurse -Directory -Filter "*cpython-3.11*" -ErrorAction SilentlyContinue | Select-Object FullName

    Write-Host "`nSearching for 'python.exe' under ${d}:\Users:"
    Get-ChildItem "${d}:\Users" -Recurse -File -Filter "python.exe" -ErrorAction SilentlyContinue | Select-Object FullName

    Write-Host "`nChecking ${d}:\Users\kksjmj\AppData\Local:"
    if (Test-Path "${d}:\Users\kksjmj\AppData\Local") {
        Get-ChildItem "${d}:\Users\kksjmj\AppData\Local" | Select-Object Name
    } else {
        Write-Host "${d}:\Users\kksjmj\AppData\Local does not exist!"
    }

    Write-Host "`nChecking ${d}:\Users\kksjmj\AppData\Roaming:"
    if (Test-Path "${d}:\Users\kksjmj\AppData\Roaming") {
        Get-ChildItem "${d}:\Users\kksjmj\AppData\Roaming" | Select-Object Name
    } else {
        Write-Host "${d}:\Users\kksjmj\AppData\Roaming does not exist!"
    }

} finally {
    Dismount-VHD -Path $vhdPath
    Start-VM $vm
}
