@echo off
setlocal EnableDelayedExpansion
title Kova bare-metal restore (KOVA Engine)
set "HERE=%~dp0"
set "KENG=%HERE%kova_engine.exe"
if not exist "%KENG%" (
  echo [ERROR] kova_engine.exe not found next to restore.cmd: %KENG%
  goto :fail
)

echo ==============================================================
echo  Kova bare-metal restore
echo  This will ERASE the selected disk completely.
echo ==============================================================
echo.

rem ---- 1. find images on every volume ----
set N=0
for %%D in (C D E F G H I J K L M N O P Q R T U V X Y Z) do (
  for %%E in (kova kimg) do (
    if exist "%%D:\KovaImages\*.%%E" (
      for %%F in ("%%D:\KovaImages\*.%%E") do (
        set /a N+=1
        set "IMG!N!=%%~fF"
        echo   [!N!] %%~fF
      )
    )
  )
)
if %N%==0 (
  echo [ERROR] No KovaImages\*.kova found on any volume.
  goto :fail
)
echo.
set "PICK="
set /p PICK=Select image number: 
if not defined PICK goto :fail
set "IMG="
for /l %%I in (1,1,%N%) do if "!PICK!"=="%%I" set "IMG=!IMG%%I!"
if not defined IMG (
  echo [ERROR] Invalid number.
  goto :fail
)
echo Image: !IMG!
set "IMGDRIVE=!IMG:~0,1!"
if /i "!IMGDRIVE!"=="S" goto :letterclash
if /i "!IMGDRIVE!"=="W" goto :letterclash

rem ---- 2. verify the image BEFORE touching any disk ----
echo.
echo Verifying image integrity. This can take a long time...
"%KENG%" verify --image "!IMG!"
if errorlevel 1 (
  echo [ERROR] Image verification failed. Nothing was changed.
  goto :fail
)

rem ---- 3. firmware type ----
set "FW=BIOS"
reg query HKLM\System\CurrentControlSet\Control /v PEFirmwareType | find "0x2" >nul && set "FW=UEFI"
echo Firmware: %FW%
if "%FW%"=="UEFI" (set "LAYOUT=%HERE%layout_gpt_uefi.txt") else (set "LAYOUT=%HERE%layout_mbr_bios.txt")

rem ---- 4. choose target disk ----
echo list disk> "%TEMP%\dp_list.txt"
diskpart /s "%TEMP%\dp_list.txt"
echo.
set "DISK="
set /p DISK=Target disk number (ALL DATA ON IT WILL BE ERASED): 
if not defined DISK goto :fail
set "NN="
for /f "delims=0123456789" %%a in ("!DISK!") do set "NN=%%a"
if defined NN (
  echo [ERROR] Not a number.
  goto :fail
)
echo.
echo About to ERASE disk !DISK! and restore !IMG!
set "CONFIRM="
set /p CONFIRM=Type ERASE to continue: 
if not "!CONFIRM!"=="ERASE" (
  echo Cancelled. Nothing was changed.
  goto :fail
)

rem ---- 5. partition the disk ----
echo select disk !DISK!> "%TEMP%\dp.txt"
type "%LAYOUT%" >> "%TEMP%\dp.txt"
diskpart /s "%TEMP%\dp.txt"
if errorlevel 1 (
  echo [ERROR] diskpart failed.
  goto :fail
)
if not exist W:\ (
  echo [ERROR] Volume W: was not created.
  goto :fail
)

rem ---- 6. restore the volume ----
"%KENG%" restore --image "!IMG!" --target \\.\W:
if errorlevel 1 (
  echo [ERROR] Restore failed. The disk is NOT bootable.
  goto :fail
)

rem ---- 7. grow the filesystem to the partition ----
diskpart /s "%HERE%extend.txt"

rem ---- 8. boot files ----
if "%FW%"=="UEFI" (
  bcdboot W:\Windows /s S: /f UEFI
) else (
  bcdboot W:\Windows /s S: /f BIOS
)
if errorlevel 1 (
  echo [ERROR] bcdboot failed.
  goto :fail
)

echo.
echo Restore completed. Remove the media and restart.
endlocal
exit /b 0

:letterclash
echo [ERROR] The image is on drive S: or W:, which the restore needs. Assign another letter and retry.
:fail
echo.
echo Restore stopped.
endlocal
exit /b 1
