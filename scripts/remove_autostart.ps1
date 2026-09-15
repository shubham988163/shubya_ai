# Removes RanchoTrade from Windows startup.
$StartupFolder = [System.Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $StartupFolder "RanchoTrade.lnk"

if (Test-Path $ShortcutPath) {
    Remove-Item $ShortcutPath -Force
    Write-Host "SUCCESS: RanchoTrade Auto-Start removed." -ForegroundColor Yellow
} else {
    Write-Host "INFO: RanchoTrade Auto-Start was not installed."
}
