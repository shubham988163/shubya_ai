# Registers RanchoTrade to start automatically when Windows boots / user logs in.
$ProjectRoot = (Get-Item $PSScriptRoot).Parent.FullName
$VbsPath = Join-Path $ProjectRoot "scripts\autostart.vbs"

$WshShell = New-Object -ComObject WScript.Shell
$StartupFolder = [System.Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $StartupFolder "RanchoTrade.lnk"

$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "wscript.exe"
$Shortcut.Arguments = "`"$VbsPath`""
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.Description = "RanchoTrade Automated Background Trading Services"
$Shortcut.Save()

Write-Host "SUCCESS: RanchoTrade Auto-Start registered!" -ForegroundColor Green
Write-Host "Shortcut created in: $ShortcutPath"
