@echo off
title RanchoTrade Launcher
cd /d "%~dp0"

echo ===================================================
echo   Starting RanchoTrade Background Services...
echo ===================================================

wscript.exe "%~dp0scripts\autostart.vbs"

echo.
echo Services launched in background!
echo Dashboard will be available at: http://localhost:8787
echo Check your Telegram (@ranchoTrade_bot) for online notification.
echo.
timeout /t 3 /nobreak >nul 2>&1
