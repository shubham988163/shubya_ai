' RanchoTrade Background Silent Trader Pass
' Runs run_ai_trader.bat without popping up any command window.
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptPath = WScript.ScriptFullName
scriptDir = fso.GetParentFolderName(scriptPath)
projectDir = fso.GetParentFolderName(scriptDir)

WshShell.CurrentDirectory = projectDir

batPath = projectDir & "\run_ai_trader.bat"

' 0 = Hide window, True = wait for pass to finish
WshShell.Run """" & batPath & """", 0, True
