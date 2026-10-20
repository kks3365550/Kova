' ==============================================================================
' launch_dashboard.vbs
' 백업시스템 스마트 런처 (Smart Dashboard Launcher - v2.9.22 Self-Gated Launch)
'
' 1. VBScript 내부 임의 대기(3초) 및 임의 브라우저 호출 완전 제거 (DEF-03 해결)
' 2. run.py 를 무창(SW_HIDE=0)으로 즉시 백그라운드 기동
' 3. 실제 포트 바인딩 및 브라우저 오픈은 run.py 가 100% 준비된 순간 직접 수행
' ==============================================================================
Option Explicit

Dim objShell, objFSO, appDir, pyExe, userProfile
Set objShell = CreateObject("WScript.Shell")
Set objFSO   = CreateObject("Scripting.FileSystemObject")

appDir = objFSO.GetParentFolderName(WScript.ScriptFullName)
objShell.CurrentDirectory = appDir
userProfile = objShell.ExpandEnvironmentStrings("%USERPROFILE%")

' 1. Python 인터프리터 탐색
If objFSO.FileExists(appDir & "\python\pythonw.exe") Then
    pyExe = """" & appDir & "\python\pythonw.exe"""
ElseIf objFSO.FileExists(appDir & "\python\python.exe") Then
    pyExe = """" & appDir & "\python\python.exe"""
ElseIf objFSO.FileExists(appDir & "\.venv\Scripts\pythonw.exe") Then
    pyExe = """" & appDir & "\.venv\Scripts\pythonw.exe"""
ElseIf objFSO.FileExists(appDir & "\.venv\Scripts\python.exe") Then
    pyExe = """" & appDir & "\.venv\Scripts\python.exe"""
ElseIf objFSO.FileExists(userProfile & "\AppData\Local\Python\pythoncore-3.14-64\python.exe") Then
    pyExe = """" & userProfile & "\AppData\Local\Python\pythoncore-3.14-64\python.exe"""
Else
    pyExe = "pythonw.exe"
End If

' 2. run.py 를 일반 모드로 백그라운드 실행 후 VBS 즉시 종료 (0.01초 소요)
' (run.py 가 Mutex 확인 후 소켓 준비 시점에 브라우저 직접 오픈)
objShell.Run pyExe & " """ & appDir & "\run.py""", 0, False
