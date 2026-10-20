$ErrorActionPreference = 'Stop'
Stop-VM (Get-VM) -Save
$vhdPath = (Get-VM).HardDrives.Path
$mount = Mount-VHD -Path $vhdPath -ReadOnly -PassThru
Start-Sleep -Seconds 2

$part = Get-Partition -DiskNumber $mount.DiskNumber | Where-Object DriveLetter | Select-Object -First 1
$d = $part.DriveLetter

Write-Host "=== ROOT OF ${d}: ==="
Get-ChildItem -Path "${d}:\" | Select-Object Name, Length, LastWriteTime

Write-Host "=== USER DESKTOP ==="
Get-ChildItem -Path "${d}:\Users\User\Desktop" -ErrorAction SilentlyContinue | Select-Object Name, Length, LastWriteTime

Write-Host "=== STARTUP FOLDER ==="
Get-ChildItem -Path "${d}:\ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp" -ErrorAction SilentlyContinue | Select-Object Name, Length, LastWriteTime

Dismount-VHD -Path $vhdPath
Start-VM (Get-VM)
