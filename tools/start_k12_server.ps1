$action = New-ScheduledTaskAction -Execute "C:\Users\kksjmj\Desktop\ai\백업시스템\.venv\Scripts\pythonw.exe" -Argument "run.py" -WorkingDirectory "C:\Users\kksjmj\Desktop\ai\백업시스템"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1)
Register-ScheduledTask -TaskName "K12BackupServer" -Action $action -Trigger $trigger -Force | Out-Null
Start-ScheduledTask -TaskName "K12BackupServer"
Start-Sleep -Seconds 2
