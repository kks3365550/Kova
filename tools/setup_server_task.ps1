 = New-ScheduledTaskAction -Execute 'c:\Users\kksjmj\Desktop\ai\백업시스템\.venv\Scripts\python.exe' -Argument 'run.py' -WorkingDirectory 'c:\Users\kksjmj\Desktop\ai\백업시스템'
 = New-ScheduledTaskPrincipal -UserId kksjmj -LogonType Interactive
Register-ScheduledTask -TaskName 'BackupSystem_WebServer' -Action  -Principal  -Force
Start-ScheduledTask -TaskName 'BackupSystem_WebServer'
Start-Sleep -Seconds 3
