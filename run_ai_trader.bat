@echo off
REM Automatic AI trading-agent pass for one evaluation.
REM
REM Why this file exists: the only scheduled task in this repo ran the
REM pre-market agent -- nothing ever launched trading.agents.agent_trader, so
REM the F&O scanner and the multi-agent council never produced a candidate and
REM the desk sat trade-less for days while the dashboard looked "live".
REM
REM This launches one agent pass. Pair it with Task Scheduler (see
REM scripts\install_trader_schedule.ps1) which fires it every 15 minutes across
REM the session, each run bounded by TRADER_TIMEOUT_SECONDS so a hung feed
REM cannot strand a task.
REM
REM Usage (manual):
REM   run_ai_trader.bat --with-scanner --max-trades 3

setlocal
cd /d "%~dp0"

if "%TRADER_TIMEOUT_SECONDS%"=="" set TRADER_TIMEOUT_SECONDS=600

REM Prefer Python 3.12 with installed dependencies; fall back to virtualenv or system python.
set PY=C:\Users\shubham.panchal\AppData\Local\Programs\Python\Python312\python.exe
if not exist "%PY%" (
    if exist ".venv\Scripts\python.exe" (
        set PY=.venv\Scripts\python.exe
    ) else (
        set PY=python
    )
)

echo [%date% %time%] agent pass start >> logs\trader_schedule.log
"%PY%" -m trading.agents.agent_trader --with-scanner --max-trades 3 >> logs\trader_schedule.log 2>&1
echo [%date% %time%] agent pass end (exit %ERRORLEVEL%) >> logs\trader_schedule.log

endlocal
