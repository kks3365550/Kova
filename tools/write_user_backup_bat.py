import os

backup_c_script = r'''@echo off
title [BackupSystem] Office Windows 7 User Data Full Backup to D:

echo ======================================================================
echo   [Office PC] User Data One-Click Full Backup to D: Drive
echo ======================================================================
echo  - Desktop, Documents, Downloads, Favorites, Pictures, Videos
echo  - NPKI Joint Certificates (LocalLow, Program Files)
echo  - Target: D:\Office_C_Drive_Backup\
echo ======================================================================
echo.

if not exist D:\ (
    echo [!] ERROR: D: drive not found! Please connect a backup drive.
    pause
    exit /b 1
)

set "DEST_ROOT=D:\Office_C_Drive_Backup"
if not exist "%DEST_ROOT%" mkdir "%DEST_ROOT%" >nul 2>&1

echo [*] [1/5] Backing up Current User Profile (%USERPROFILE%)...
set "USER_DEST=%DEST_ROOT%\Users_%USERNAME%"
if not exist "%USER_DEST%" mkdir "%USER_DEST%" >nul 2>&1

echo  - Desktop...
if exist "%USERPROFILE%\Desktop" robocopy "%USERPROFILE%\Desktop" "%USER_DEST%\Desktop" /E /R:1 /W:1 /XJ /NFL /NDL /NP >nul 2>&1

echo  - Documents...
if exist "%USERPROFILE%\Documents" robocopy "%USERPROFILE%\Documents" "%USER_DEST%\Documents" /E /R:1 /W:1 /XJ /NFL /NDL /NP >nul 2>&1

echo  - Downloads...
if exist "%USERPROFILE%\Downloads" robocopy "%USERPROFILE%\Downloads" "%USER_DEST%\Downloads" /E /R:1 /W:1 /XJ /NFL /NDL /NP >nul 2>&1

echo  - Favorites and Pictures...
if exist "%USERPROFILE%\Favorites" robocopy "%USERPROFILE%\Favorites" "%USER_DEST%\Favorites" /E /R:1 /W:1 /XJ /NFL /NDL /NP >nul 2>&1
if exist "%USERPROFILE%\Pictures" robocopy "%USERPROFILE%\Pictures" "%USER_DEST%\Pictures" /E /R:1 /W:1 /XJ /NFL /NDL /NP >nul 2>&1

echo [*] [2/5] Backing up NPKI Certificates (Bank / Tax / Corporate)...
set "NPKI_DEST=%DEST_ROOT%\NPKI_Certificates"
if not exist "%NPKI_DEST%" mkdir "%NPKI_DEST%" >nul 2>&1

if exist "%USERPROFILE%\AppData\LocalLow\NPKI" robocopy "%USERPROFILE%\AppData\LocalLow\NPKI" "%NPKI_DEST%\LocalLow_NPKI" /E /R:1 /W:1 /XJ /NFL /NDL /NP >nul 2>&1
if exist "C:\Program Files\NPKI" robocopy "C:\Program Files\NPKI" "%NPKI_DEST%\ProgramFiles_NPKI" /E /R:1 /W:1 /XJ /NFL /NDL /NP >nul 2>&1
if exist "C:\Program Files (x86)\NPKI" robocopy "C:\Program Files (x86)\NPKI" "%NPKI_DEST%\ProgramFiles_x86_NPKI" /E /R:1 /W:1 /XJ /NFL /NDL /NP >nul 2>&1
if exist "C:\NPKI" robocopy "C:\NPKI" "%NPKI_DEST%\C_NPKI" /E /R:1 /W:1 /XJ /NFL /NDL /NP >nul 2>&1

echo [*] [3/5] Backing up Browser Bookmarks / User Favorites...
set "BROWSER_DEST=%DEST_ROOT%\Browser_Profiles"
if not exist "%BROWSER_DEST%" mkdir "%BROWSER_DEST%" >nul 2>&1
if exist "%USERPROFILE%\AppData\Local\Google\Chrome\User Data\Default\Bookmarks" copy /y "%USERPROFILE%\AppData\Local\Google\Chrome\User Data\Default\Bookmarks" "%BROWSER_DEST%\Chrome_Bookmarks" >nul 2>&1
if exist "%USERPROFILE%\AppData\Local\Microsoft\Edge\User Data\Default\Bookmarks" copy /y "%USERPROFILE%\AppData\Local\Microsoft\Edge\User Data\Default\Bookmarks" "%BROWSER_DEST%\Edge_Bookmarks" >nul 2>&1

echo [*] [4/5] Backing up Common Root Data Folders on C: Drive...
for %%D in (data work reports backup temp_work) do (
    if exist "C:\%%D" (
        echo  - Found C:\%%D, copying...
        robocopy "C:\%%D" "%DEST_ROOT%\Root_%%D" /E /R:1 /W:1 /XJ /NFL /NDL /NP >nul 2>&1
    )
)

echo [*] [5/5] Also Mirroring NPKI to USB for Extra Safety...
set "USB_DEST=%~dp0Office_C_Drive_Backup"
if not exist "%USB_DEST%" mkdir "%USB_DEST%" >nul 2>&1
if exist "%NPKI_DEST%" robocopy "%NPKI_DEST%" "%USB_DEST%\NPKI_Certificates" /E /R:1 /W:1 /XJ /NFL /NDL /NP >nul 2>&1

echo.
echo ======================================================================
echo   User Data Full Backup COMPLETED!
echo ======================================================================
echo   Location: D:\Office_C_Drive_Backup\
echo   All Desktop, Documents, Downloads, and NPKI Certificates are Safe!
echo ======================================================================
echo.
pause
exit /b 0
'''

path = 'E:/0_Backup_All_User_Files_to_D.bat'
crlf = backup_c_script.strip().replace('\r\n', '\n').replace('\n', '\r\n') + '\r\n'
with open(path, 'wb') as f:
    f.write(crlf.encode('ascii'))
print('Successfully created:', path)
