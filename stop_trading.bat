@echo off
title RanchoTrade Stopper
cd /d "%~dp0"

echo ===================================================
echo   Stopping RanchoTrade Services...
echo ===================================================

.venv\Scripts\python.exe scripts\run_all.py --stop

echo.
pause
