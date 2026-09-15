@echo off
title RanchoTrade Status
cd /d "%~dp0"

.venv\Scripts\python.exe scripts\run_all.py --status

echo.
pause
