@echo off
REM Autonomous 24/7 Forex & Crypto Execution Daemon (Delta Exchange)
REM Runs continuously in the background, evaluating setups every 30 seconds
REM and managing trailing stops & take profits.

setlocal
cd /d "%~dp0"

set PY=C:\Users\shubham.panchal\AppData\Local\Programs\Python\Python312\python.exe
if not exist "%PY%" (
    if exist ".venv\Scripts\python.exe" (
        set PY=.venv\Scripts\python.exe
    ) else (
        set PY=python
    )
)

echo [%date% %time%] Starting 24/7 Forex Auto-Trader Daemon...
"%PY%" -m trading.forex_trader --daemon
endlocal
