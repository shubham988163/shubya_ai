' RanchoTrade Background Silent Launcher
' Starts scripts/run_all.py via pythonw without showing any command prompt window.
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Get project directory from script location
scriptPath = WScript.ScriptFullName
scriptDir = fso.GetParentFolderName(scriptPath)
projectDir = fso.GetParentFolderName(scriptDir)

WshShell.CurrentDirectory = projectDir

pythonwPath = projectDir & "\.venv\Scripts\pythonw.exe"
runnerScript = projectDir & "\scripts\run_all.py"

' Run hidden (0 = hide window, false = don't wait for completion)
WshShell.Run """" & pythonwPath & """ """ & runnerScript & """", 0, False
