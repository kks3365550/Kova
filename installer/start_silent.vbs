Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
Dim currentDir, pyExe, userProfile
currentDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = currentDir

userProfile = WshShell.ExpandEnvironmentStrings("%USERPROFILE%")

If fso.FileExists(currentDir & "\python\pythonw.exe") Then
    pyExe = """" & currentDir & "\python\pythonw.exe"""
ElseIf fso.FileExists(currentDir & "\python\python.exe") Then
    pyExe = """" & currentDir & "\python\python.exe"""
ElseIf fso.FileExists(currentDir & "\.venv\Scripts\pythonw.exe") Then
    pyExe = """" & currentDir & "\.venv\Scripts\pythonw.exe"""
ElseIf fso.FileExists(currentDir & "\.venv\Scripts\python.exe") Then
    pyExe = """" & currentDir & "\.venv\Scripts\python.exe"""
ElseIf fso.FileExists(userProfile & "\.gemini\antigravity\venv\Scripts\pythonw.exe") Then
    pyExe = """" & userProfile & "\.gemini\antigravity\venv\Scripts\pythonw.exe"""
ElseIf fso.FileExists(userProfile & "\.gemini\antigravity\venv\Scripts\python.exe") Then
    pyExe = """" & userProfile & "\.gemini\antigravity\venv\Scripts\python.exe"""
ElseIf fso.FileExists(userProfile & "\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe") Then
    pyExe = """" & userProfile & "\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"""
Else
    pyExe = "pythonw"
End If

WshShell.Run pyExe & " run.py --silent", 0, False