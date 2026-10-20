import os

net_script = r'''@echo off
chcp 65001 >nul
title [백업시스템] 사무실 윈도우7 공유폴더 및 네트워크 설정 백업

echo ======================================================================
echo   [사무실 PC] 공유 폴더 및 네트워크 설정 원클릭 백업
echo ======================================================================
echo  - 공유 폴더 설정(LanmanServer Shares 레지스트리)
echo  - 저장된 네트워크 자격 증명 대상 목록(cmdkey)
echo  - 네트워크 드라이브 연결 목록(net use)
echo  - IP 및 네트워크 어댑터 구성(ipconfig)
echo ======================================================================
echo.

set "CUR_USB=%~dp0"
set "TARGET_USB=%CUR_USB%사무실_네트워크설정_백업"
set "TARGET_D=D:\사무실_네트워크설정_백업"

if not exist "%TARGET_USB%" mkdir "%TARGET_USB%" >nul 2>&1
if exist D:\ if not exist "%TARGET_D%" mkdir "%TARGET_D%" >nul 2>&1

echo [*] [1/4] LanmanServer 공유 폴더 레지스트리 추출 중...
reg export "HKLM\SYSTEM\CurrentControlSet\Services\LanmanServer\Shares" "%TARGET_USB%\1_공유폴더설정_복원용.reg" /y >nul 2>&1
if exist D:\ copy /y "%TARGET_USB%\1_공유폴더설정_복원용.reg" "%TARGET_D%\" >nul 2>&1

echo [*] [2/4] 저장된 네트워크 자격 증명(Vault/cmdkey) 목록 추출 중...
cmdkey /list > "%TARGET_USB%\2_네트워크_자격증명목록.txt" 2>&1
if exist D:\ copy /y "%TARGET_USB%\2_네트워크_자격증명목록.txt" "%TARGET_D%\" >nul 2>&1

echo [*] [3/4] 연결된 네트워크 드라이브(net use) 목록 추출 중...
net use > "%TARGET_USB%\3_네트워크드라이브_매핑목록.txt" 2>&1
if exist D:\ copy /y "%TARGET_USB%\3_네트워크드라이브_매핑목록.txt" "%TARGET_D%\" >nul 2>&1

echo [*] [4/4] IP 및 네트워크 어댑터 상세 설정(ipconfig) 추출 중...
ipconfig /all > "%TARGET_USB%\4_네트워크IP_상세구성.txt" 2>&1
if exist D:\ copy /y "%TARGET_USB%\4_네트워크IP_상세구성.txt" "%TARGET_D%\" >nul 2>&1

echo [사무실 PC 윈도우 11 업그레이드 후 복원 가이드] > "%TARGET_USB%\복원방법_안내.txt"
echo 1. 1_공유폴더설정_복원용.reg: 더블클릭하여 적용하면 기존 공유폴더가 복원됩니다. >> "%TARGET_USB%\복원방법_안내.txt"
echo 2. 2_네트워크_자격증명목록.txt: 기존 연결 NAS/서버 IP 및 계정 목록입니다. >> "%TARGET_USB%\복원방법_안내.txt"
echo 3. 3_네트워크드라이브_매핑목록.txt: 기존 연결 네트워크 드라이브 목록입니다. >> "%TARGET_USB%\복원방법_안내.txt"
if exist D:\ copy /y "%TARGET_USB%\복원방법_안내.txt" "%TARGET_D%\" >nul 2>&1

echo.
echo ======================================================================
echo   사무실 PC 네트워크 및 공유 설정 백업이 완료되었습니다!
echo   저장 위치: %TARGET_USB%
echo ======================================================================
echo.
pause
exit /b 0
'''

tec_script = r'''@echo off
chcp 65001 >nul
title [백업시스템] Tecnosoft SPD 프로그램 및 데이터베이스 전수 추출 백업

echo ======================================================================
echo   [사무실 PC] Tecnosoft SPD 데이터 로거 프로그램 및 DB 전수 백업
echo ======================================================================
echo  - 과거 살균/열분포 F0 측정 데이터베이스(.spd, .sdb 등)
echo  - 장비 캘리브레이션 프로필 및 프로그램 설정
echo  - Tecnosoft 관련 레지스트리 라이선스 키
echo ======================================================================
echo.

set "CUR_USB=%~dp0"
set "TARGET_USB=%CUR_USB%사무실_Tecnosoft_백업"
set "TARGET_D=D:\사무실_Tecnosoft_백업"

if not exist "%TARGET_USB%" mkdir "%TARGET_USB%" >nul 2>&1
if exist D:\ if not exist "%TARGET_D%" mkdir "%TARGET_D%" >nul 2>&1

echo [*] [1/3] Tecnosoft 레지스트리 설정 추출 중...
reg export "HKLM\SOFTWARE\Tecnosoft" "%TARGET_USB%\Tecnosoft_HKLM.reg" /y >nul 2>&1
reg export "HKLM\SOFTWARE\Wow6432Node\Tecnosoft" "%TARGET_USB%\Tecnosoft_HKLM_x64.reg" /y >nul 2>&1
reg export "HKCU\SOFTWARE\Tecnosoft" "%TARGET_USB%\Tecnosoft_HKCU.reg" /y >nul 2>&1

echo [*] [2/3] Program Files 설치 폴더 복사 중...
if exist "C:\Program Files (x86)\Tecnosoft" xcopy "C:\Program Files (x86)\Tecnosoft" "%TARGET_USB%\ProgramFiles_x86\" /E /I /H /Y /Q >nul 2>&1
if exist "C:\Program Files\Tecnosoft" xcopy "C:\Program Files\Tecnosoft" "%TARGET_USB%\ProgramFiles\" /E /I /H /Y /Q >nul 2>&1
if exist "C:\Tecnosoft" xcopy "C:\Tecnosoft" "%TARGET_USB%\Tecnosoft_Root\" /E /I /H /Y /Q >nul 2>&1

echo [*] [3/3] ProgramData 및 사용자 계정 데이터베이스 탐색 및 복사 중...
if exist "C:\ProgramData\Tecnosoft" xcopy "C:\ProgramData\Tecnosoft" "%TARGET_USB%\ProgramData\" /E /I /H /Y /Q >nul 2>&1
if exist "%USERPROFILE%\AppData\Local\Tecnosoft" xcopy "%USERPROFILE%\AppData\Local\Tecnosoft" "%TARGET_USB%\AppData_Local\" /E /I /H /Y /Q >nul 2>&1
if exist "%USERPROFILE%\Documents\Tecnosoft" xcopy "%USERPROFILE%\Documents\Tecnosoft" "%TARGET_USB%\Documents\" /E /I /H /Y /Q >nul 2>&1

if exist D:\ xcopy "%TARGET_USB%" "%TARGET_D%\" /E /I /H /Y /Q >nul 2>&1

echo.
echo ======================================================================
echo   Tecnosoft SPD 데이터 및 라이선스 정보 추출이 완료되었습니다!
echo   저장 위치: %TARGET_USB%
echo ======================================================================
echo.
pause
exit /b 0
'''

vhd_script = r'''@echo off
chcp 65001 >nul
title [백업시스템] 윈도우7 C드라이브 전체 무손실 가상화 백업 (Disk2vhd)

echo ======================================================================
echo   [사무실 PC] C드라이브 전체 무손실 가상화 백업 (Disk2vhd)
echo ======================================================================
echo  - C드라이브(Windows 7, MS Office, 한글, Tecnosoft SPD 등) 전체를
echo    가상 머신용 하드디스크(.vhdx) 단 1개 파일로 완벽 압축 복제합니다.
echo ======================================================================
echo.

set "TOOL_EXE=%~dp0Rescue_Tools\Disk2vhd\disk2vhd64.exe"
if not exist "%TOOL_EXE%" set "TOOL_EXE=%~dp0Rescue_Tools\Disk2vhd\disk2vhd.exe"

if not exist "%TOOL_EXE%" (
    echo [!] 오류: Disk2vhd 실행 파일을 찾을 수 없습니다: %TOOL_EXE%
    pause
    exit /b 1
)

echo [*] Microsoft Disk2vhd 가상화 도구를 실행합니다.
echo.
echo [사용 방법]
echo  1. 프로그램 창이 뜨면 상단의 'Use Vhdx' 에 체크합니다.
echo  2. 하단 볼륨 목록에서 [C:\] 만 체크합니다. (D드라이브 체크 해제)
echo  3. 저장 위치(VHD File name)를 D드라이브로 지정합니다:
echo     예: D:\사무실_Win7_가상백업.vhdx
echo  4. [Create] 버튼을 누르면 작업이 완료됩니다.
echo.
start "" "%TOOL_EXE%"
exit /b 0
'''

vbs_script = '''Option Explicit

Dim fso, shell, outFile, ts, outPath, curDir
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

curDir = fso.GetParentFolderName(WScript.ScriptFullName)
ts = Year(Now) & Right("0" & Month(Now), 2) & Right("0" & Day(Now), 2) & "_" & Right("0" & Hour(Now), 2) & Right("0" & Minute(Now), 2)

If fso.FolderExists("D:\\") Then
    If Not fso.FolderExists("D:\\사무실_라이선스_백업") Then fso.CreateFolder("D:\\사무실_라이선스_백업")
    outPath = "D:\\사무실_라이선스_백업\\라이선스_제품키_추출_" & ts & ".txt"
Else
    outPath = curDir & "\\라이선스_제품키_추출_" & ts & ".txt"
End If

Set outFile = fso.CreateTextFile(outPath, True, True)

outFile.WriteLine "======================================================================"
outFile.WriteLine "   [사무실 PC] 라이선스 키 및 설치 제품키 전수 추출 보고서"
outFile.WriteLine "   추출 일시: " & Now
outFile.WriteLine "======================================================================"
outFile.WriteLine ""

' 1. Windows 제품키 디코딩
outFile.WriteLine "[1. Windows 제품키]"
Dim winKey
winKey = GetWindowsProductKey()
outFile.WriteLine " - Windows 제품 키: " & winKey
outFile.WriteLine ""

' 2. MS Office 제품키 탐색
outFile.WriteLine "[2. Microsoft Office 제품키]"
Dim officeKey
officeKey = GetOfficeProductKey()
outFile.WriteLine " - Office 탐색 결과: " & officeKey
outFile.WriteLine ""

' 3. 한컴오피스 / 한글 시리얼 탐색
outFile.WriteLine "[3. 한컴오피스 (한글) 시리얼 / 등록정보]"
Dim hncSerial
hncSerial = GetHncSerial()
outFile.WriteLine hncSerial
outFile.WriteLine ""

' 4. Tecnosoft 및 기타 계측/업무 S/W 레지스트리 경로 안내
outFile.WriteLine "[4. Tecnosoft SPD 및 기타 업무용 소프트웨어]"
outFile.WriteLine " - Tecnosoft SPD 설치 경로는 USB의 'Disk2vhd' 도구로 C드라이브 전체를 가상화 백업해 두었습니다."
outFile.WriteLine " - 레지스트리 키: HKLM\\SOFTWARE\\Tecnosoft 및 HKCU\\SOFTWARE\\Tecnosoft"
outFile.WriteLine ""
outFile.WriteLine "======================================================================"

outFile.Close

MsgBox "라이선스 키 추출이 완료되었습니다!" & vbCrLf & vbCrLf & "저장 위치: " & outPath, vbInformation, "라이선스 추출 완료"

Function GetWindowsProductKey()
    On Error Resume Next
    Dim regObj, digitalProductId, isWin8
    Const HKEY_LOCAL_MACHINE = &H80000002
    Set regObj = GetObject("winmgmts:{impersonationLevel=impersonate}!\\\\.\\root\\default:StdRegProv")
    regObj.GetBinaryValue HKEY_LOCAL_MACHINE, "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion", "DigitalProductId", digitalProductId
    If IsArray(digitalProductId) Then
        isWin8 = (digitalProductId(66) \\ 6) And 1
        digitalProductId(66) = (digitalProductId(66) And &HF7) Or ((isWin8 And 2) * 4)
        GetWindowsProductKey = DecodeKey(digitalProductId, isWin8)
    Else
        GetWindowsProductKey = "레지스트리에서 제품키를 찾을 수 없습니다."
    End If
End Function

Function GetOfficeProductKey()
    On Error Resume Next
    Dim regObj, digitalProductId, isWin8, res, k, hivePaths
    Const HKEY_LOCAL_MACHINE = &H80000002
    Set regObj = GetObject("winmgmts:{impersonationLevel=impersonate}!\\\\.\\root\\default:StdRegProv")
    
    hivePaths = Array( _
        "SOFTWARE\\Microsoft\\Office\\14.0\\Registration", _
        "SOFTWARE\\Microsoft\\Office\\15.0\\Registration", _
        "SOFTWARE\\Microsoft\\Office\\16.0\\Registration", _
        "SOFTWARE\\Wow6432Node\\Microsoft\\Office\\14.0\\Registration", _
        "SOFTWARE\\Wow6432Node\\Microsoft\\Office\\15.0\\Registration", _
        "SOFTWARE\\Wow6432Node\\Microsoft\\Office\\16.0\\Registration" _
    )
    
    res = ""
    For Each k In hivePaths
        Dim subKeys, subK
        regObj.EnumKey HKEY_LOCAL_MACHINE, k, subKeys
        If IsArray(subKeys) Then
            For Each subK In subKeys
                regObj.GetBinaryValue HKEY_LOCAL_MACHINE, k & "\\" & subK, "DigitalProductId", digitalProductId
                If IsArray(digitalProductId) Then
                    isWin8 = (digitalProductId(66) \\ 6) And 1
                    digitalProductId(66) = (digitalProductId(66) And &HF7) Or ((isWin8 And 2) * 4)
                    res = res & vbCrLf & " - Office (" & k & "): " & DecodeKey(digitalProductId, isWin8)
                End If
            Next
        End If
    Next
    
    If res = "" Then
        GetOfficeProductKey = "오피스 설치 키를 레지스트리에서 찾지 못했거나 볼륨 라이선스(KMS/간편실행) 방식입니다."
    Else
        GetOfficeProductKey = res
    End If
End Function

Function GetHncSerial()
    On Error Resume Next
    Dim regObj, val, res
    Const HKEY_LOCAL_MACHINE = &H80000002
    Const HKEY_CURRENT_USER = &H80000001
    Set regObj = GetObject("winmgmts:{impersonationLevel=impersonate}!\\\\.\\root\\default:StdRegProv")
    res = ""
    
    regObj.GetStringValue HKEY_LOCAL_MACHINE, "SOFTWARE\\Hnc\\Hwp", "ProductKey", val
    If val <> "" Then res = res & " - 한컴 HWP 제품키 (HKLM): " & val & vbCrLf
    
    regObj.GetStringValue HKEY_LOCAL_MACHINE, "SOFTWARE\\Wow6432Node\\Hnc\\Hwp", "ProductKey", val
    If val <> "" Then res = res & " - 한컴 HWP 제품키 (HKLM WOW64): " & val & vbCrLf
    
    regObj.GetStringValue HKEY_CURRENT_USER, "SOFTWARE\\Hnc\\Hwp", "SerialNumber", val
    If val <> "" Then res = res & " - 한컴 HWP 시리얼번호 (HKCU): " & val & vbCrLf

    If res = "" Then
        GetHncSerial = " - 한글 시리얼 키 레지스트리 미검출 (Disk2vhd 가상화 백업본에서 확인 권장)"
    Else
        GetHncSerial = res
    End If
End Function

Function DecodeKey(rKey, isWin8)
    Const keyOffset = 52
    Dim i, j, cur, x, keyOutput, chars
    chars = "BCDFGHJKMPQRTVWXY2346789"
    keyOutput = ""
    i = 28
    Do
        cur = 0
        j = 14
        Do
            cur = cur * 256
            cur = rKey(j + keyOffset) + cur
            rKey(j + keyOffset) = (cur \\ 24)
            cur = cur Mod 24
            j = j - 1
        Loop While j >= 0
        i = i - 1
        keyOutput = Mid(chars, cur + 1, 1) & keyOutput
        If (((29 - i) Mod 6) = 0) And (i <> -1) Then
            i = i - 1
            keyOutput = "-" & keyOutput
        End If
    Loop While i >= 0
    DecodeKey = keyOutput
End Function
'''

def write_crlf(path, content, encoding='utf-8'):
    crlf_content = content.strip().replace('\r\n', '\n').replace('\n', '\r\n') + '\r\n'
    with open(path, 'wb') as f:
        f.write(crlf_content.encode(encoding))
    print(f'Successfully wrote: {path}')

write_crlf(r'E:\0_사무실_공유폴더_및_네트워크_원클릭백업.bat', net_script)
write_crlf(r'E:\0_사무실_Tecnosoft_SPD_원클릭추출.bat', tec_script)
write_crlf(r'E:\0_사무실_C드라이브_가상화백업(Disk2vhd).bat', vhd_script)
write_crlf(r'E:\0_사무실_라이선스키_원클릭_추출.vbs', vbs_script, encoding='utf-16le')
