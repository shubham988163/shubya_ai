' RanchoTrade Background Silent Launcher
' Starts trading.dashboard on port 8787 via pythonw without showing a console window.
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptPath = WScript.ScriptFullName
scriptDir = fso.GetParentFolderName(scriptPath)
projectDir = fso.GetParentFolderName(scriptDir)

WshShell.CurrentDirectory = projectDir

Dim pythonExe
If fso.FileExists("C:\Users\shubham.panchal\AppData\Local\Programs\Python\Python312\python.exe") Then
    pythonExe = "C:\Users\shubham.panchal\AppData\Local\Programs\Python\Python312\python.exe"
ElseIf fso.FileExists(projectDir & "\.venv\Scripts\python.exe") Then
    pythonExe = projectDir & "\.venv\Scripts\python.exe"
Else
    pythonExe = "python.exe"
End If

' Run dashboard on 8787 hidden (0 = hide window, false = do not wait)
WshShell.Run """" & pythonExe & """ -u -m trading.dashboard 8787", 0, False
