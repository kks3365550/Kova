$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "start_silent.vbs" -WorkingDirectory "C:\Users\kksjmj\Desktop\ai\백업시스템"
Register-ScheduledTask -TaskName "BackupSystem_Server" -Action $action -Force | Out-Null
Start-ScheduledTask -TaskName "BackupSystem_Server"
Start-Sleep -Seconds 3
$conn = Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue
if ($conn) {
    Write-Host "[OK] BackupSystem_Server is running on port 8765 via Task Scheduler."
} else {
    Write-Host "[FAIL] Port 8765 is not open."
}
