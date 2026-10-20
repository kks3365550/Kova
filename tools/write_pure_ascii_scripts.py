import os

net_script = r'''@echo off
title [BackupSystem] Office Windows 7 Network and Shares Backup

echo ======================================================================
echo   [Office PC] Network and Shared Folders One-Click Backup
echo ======================================================================
echo  - Shared folders registry (LanmanServer Shares)
echo  - Stored network credentials (cmdkey)
echo  - Network mapped drives (net use)
echo  - IP and adapter configurations (ipconfig)
echo ======================================================================
echo.

set "CUR_USB=%~dp0"
set "TARGET_USB=%CUR_USB%Office_Network_Backup"
set "TARGET_D=D:\Office_Network_Backup"

if not exist "%TARGET_USB%" mkdir "%TARGET_USB%" >nul 2>&1
if exist D:\ if not exist "%TARGET_D%" mkdir "%TARGET_D%" >nul 2>&1

echo [*] [1/4] Exporting LanmanServer shares registry...
reg export "HKLM\SYSTEM\CurrentControlSet\Services\LanmanServer\Shares" "%TARGET_USB%\1_Shares_Restore.reg" /y >nul 2>&1
if exist D:\ copy /y "%TARGET_USB%\1_Shares_Restore.reg" "%TARGET_D%\" >nul 2>&1

echo [*] [2/4] Exporting stored network credentials...
cmdkey /list > "%TARGET_USB%\2_Credentials_List.txt" 2>&1
if exist D:\ copy /y "%TARGET_USB%\2_Credentials_List.txt" "%TARGET_D%\" >nul 2>&1

echo [*] [3/4] Exporting mapped network drives...
net use > "%TARGET_USB%\3_Mapped_Drives.txt" 2>&1
if exist D:\ copy /y "%TARGET_USB%\3_Mapped_Drives.txt" "%TARGET_D%\" >nul 2>&1

echo [*] [4/4] Exporting IP and network adapters info...
ipconfig /all > "%TARGET_USB%\4_IPConfig_All.txt" 2>&1
if exist D:\ copy /y "%TARGET_USB%\4_IPConfig_All.txt" "%TARGET_D%\" >nul 2>&1

echo [Office PC Restore Guide for Windows 11] > "%TARGET_USB%\Restore_Guide.txt"
echo 1. 1_Shares_Restore.reg: Double click to restore shared folders in Windows 11. >> "%TARGET_USB%\Restore_Guide.txt"
echo 2. 2_Credentials_List.txt: Saved NAS and server credentials list. >> "%TARGET_USB%\Restore_Guide.txt"
echo 3. 3_Mapped_Drives.txt: Mapped network drives list. >> "%TARGET_USB%\Restore_Guide.txt"
if exist D:\ copy /y "%TARGET_USB%\Restore_Guide.txt" "%TARGET_D%\" >nul 2>&1

echo.
echo ======================================================================
echo   Backup Completed Successfully!
echo   Target: %TARGET_USB%
echo ======================================================================
echo.
pause
exit /b 0
'''

tec_script = r'''@echo off
title [BackupSystem] Tecnosoft SPD Program and Database Backup

echo ======================================================================
echo   [Office PC] Tecnosoft SPD Data Logger and DB Full Backup
echo ======================================================================
echo  - F0 Sterilization and Temperature measurement databases (.spd, .sdb)
echo  - Calibration profiles and software settings
echo  - Tecnosoft registry license entries
echo ======================================================================
echo.

set "CUR_USB=%~dp0"
set "TARGET_USB=%CUR_USB%Office_Tecnosoft_Backup"
set "TARGET_D=D:\Office_Tecnosoft_Backup"

if not exist "%TARGET_USB%" mkdir "%TARGET_USB%" >nul 2>&1
if exist D:\ if not exist "%TARGET_D%" mkdir "%TARGET_D%" >nul 2>&1

echo [*] [1/3] Exporting Tecnosoft registry entries...
reg export "HKLM\SOFTWARE\Tecnosoft" "%TARGET_USB%\Tecnosoft_HKLM.reg" /y >nul 2>&1
reg export "HKLM\SOFTWARE\Wow6432Node\Tecnosoft" "%TARGET_USB%\Tecnosoft_HKLM_x64.reg" /y >nul 2>&1
reg export "HKCU\SOFTWARE\Tecnosoft" "%TARGET_USB%\Tecnosoft_HKCU.reg" /y >nul 2>&1

echo [*] [2/3] Copying Program Files installation folders...
if exist "C:\Program Files (x86)\Tecnosoft" xcopy "C:\Program Files (x86)\Tecnosoft" "%TARGET_USB%\ProgramFiles_x86\" /E /I /H /Y /Q >nul 2>&1
if exist "C:\Program Files\Tecnosoft" xcopy "C:\Program Files\Tecnosoft" "%TARGET_USB%\ProgramFiles\" /E /I /H /Y /Q >nul 2>&1
if exist "C:\Tecnosoft" xcopy "C:\Tecnosoft" "%TARGET_USB%\Tecnosoft_Root\" /E /I /H /Y /Q >nul 2>&1

echo [*] [3/3] Searching and copying ProgramData and user databases...
if exist "C:\ProgramData\Tecnosoft" xcopy "C:\ProgramData\Tecnosoft" "%TARGET_USB%\ProgramData\" /E /I /H /Y /Q >nul 2>&1
if exist "%USERPROFILE%\AppData\Local\Tecnosoft" xcopy "%USERPROFILE%\AppData\Local\Tecnosoft" "%TARGET_USB%\AppData_Local\" /E /I /H /Y /Q >nul 2>&1
if exist "%USERPROFILE%\Documents\Tecnosoft" xcopy "%USERPROFILE%\Documents\Tecnosoft" "%TARGET_USB%\Documents\" /E /I /H /Y /Q >nul 2>&1

if exist D:\ xcopy "%TARGET_USB%" "%TARGET_D%\" /E /I /H /Y /Q >nul 2>&1

echo.
echo ======================================================================
echo   Tecnosoft SPD Backup Completed Successfully!
echo   Target: %TARGET_USB%
echo ======================================================================
echo.
pause
exit /b 0
'''

vhd_script = r'''@echo off
title [BackupSystem] Windows 7 C Drive P2V Virtualization Backup (Disk2vhd)

echo ======================================================================
echo   [Office PC] Full C-Drive Virtualization Backup (Disk2vhd)
echo ======================================================================
echo  - Clones entire C: drive (Win 7, Office, HWP, Tecnosoft, all apps)
echo    into a single virtual hard disk (.vhdx) file without data loss.
echo ======================================================================
echo.

set "TOOL_EXE=%~dp0Rescue_Tools\Disk2vhd\disk2vhd64.exe"
if not exist "%TOOL_EXE%" set "TOOL_EXE=%~dp0Rescue_Tools\Disk2vhd\disk2vhd.exe"

if not exist "%TOOL_EXE%" (
    echo [!] Error: Disk2vhd executable not found: %TOOL_EXE%
    pause
    exit /b 1
)

echo [*] Launching Microsoft Disk2vhd tool...
echo.
echo [Instructions]
echo  1. Check 'Use Vhdx' at the top of the window.
echo  2. In the Volumes list, CHECK ONLY [C:\]. (Uncheck D: drive).
echo  3. Set 'VHD File name' destination to D: drive:
echo     e.g., D:\Office_Win7_Backup.vhdx
echo  4. Click [Create] button to start virtualization.
echo.
start "" "%TOOL_EXE%"
exit /b 0
'''

def write_pure_ascii(path, content):
    crlf = content.strip().replace('\r\n', '\n').replace('\n', '\r\n') + '\r\n'
    with open(path, 'wb') as f:
        f.write(crlf.encode('ascii'))
    print('Successfully wrote pure ASCII:', path)

write_pure_ascii('E:/0_Backup_Network_and_Shares.bat', net_script)
write_pure_ascii('E:/0_Backup_Tecnosoft_SPD.bat', tec_script)
write_pure_ascii('E:/0_Backup_Full_C_Disk2vhd.bat', vhd_script)
