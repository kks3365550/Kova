Option Explicit

Dim objShell, objFSO, appDir, pyExe, userProfile
Set objShell = CreateObject("WScript.Shell")
Set objFSO   = CreateObject("Scripting.FileSystemObject")

appDir = objFSO.GetParentFolderName(WScript.ScriptFullName)
objShell.CurrentDirectory = appDir
userProfile = objShell.ExpandEnvironmentStrings("%USERPROFILE%")

' 1. Python Interpreter Search Priority
If objFSO.FileExists(appDir & "\python\pythonw.exe") Then
    pyExe = """" & appDir & "\python\pythonw.exe"""
ElseIf objFSO.FileExists(appDir & "\python\python.exe") Then
    pyExe = """" & appDir & "\python\python.exe"""
ElseIf objFSO.FileExists(appDir & "\.venv\Scripts\pythonw.exe") Then
    pyExe = """" & appDir & "\.venv\Scripts\pythonw.exe"""
ElseIf objFSO.FileExists(appDir & "\.venv\Scripts\python.exe") Then
    pyExe = """" & appDir & "\.venv\Scripts\python.exe"""
ElseIf objFSO.FileExists(userProfile & "\.gemini\antigravity\venv\Scripts\pythonw.exe") Then
    pyExe = """" & userProfile & "\.gemini\antigravity\venv\Scripts\pythonw.exe"""
ElseIf objFSO.FileExists(userProfile & "\.gemini\antigravity\venv\Scripts\python.exe") Then
    pyExe = """" & userProfile & "\.gemini\antigravity\venv\Scripts\python.exe"""
ElseIf objFSO.FileExists(userProfile & "\AppData\Local\Python\pythoncore-3.14-64\python.exe") Then
    pyExe = """" & userProfile & "\AppData\Local\Python\pythoncore-3.14-64\python.exe"""
Else
    pyExe = "python.exe"
End If

' 2. Run run.py silently
objShell.Run pyExe & " """ & appDir & "\run.py""", 0, False