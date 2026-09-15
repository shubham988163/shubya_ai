"""Master trading runner and lifecycle manager for shubya_ai.

Runs continuously in the background (e.g. at Windows startup):
  - Web dashboard (http://localhost:8787) — kept alive 24/7.
  - Pre-market agent (08:45 AM weekdays) — sets daily risk params.
  - Live strategy engine & Supervisor agent (09:15 - 15:30 IST weekdays).
  - EOD journal generator (15:35 IST weekdays) — summarizes session trades.
  - Sends status updates to Telegram bot.

Usage:
  python scripts/run_all.py          # run manager loop
  python scripts/run_all.py --status # check status of running services
  python scripts/run_all.py --stop   # stop all running services
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PYTHON = str(PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe")
if not os.path.exists(PYTHON):
    PYTHON = str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe")
if not os.path.exists(PYTHON):
    PYTHON = sys.executable

PID_FILE = PROJECT_ROOT / "data" / "running_services.json"
LOGS_DIR = PROJECT_ROOT / "logs"
DATA_DIR = PROJECT_ROOT / "data"
IST = ZoneInfo("Asia/Kolkata")

# Redirect stdout/stderr to manager.log if running under pythonw (where stdout is None)
if sys.stdout is None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    _mgr_log = open(LOGS_DIR / "manager.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = _mgr_log
    sys.stderr = _mgr_log



if os.name == "nt":
    import ctypes
    import ctypes.wintypes
    _kernel32 = ctypes.windll.kernel32
    _SYNCHRONIZE = 0x00100000
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _CREATE_NO_WINDOW = 0x08000000
else:
    _kernel32 = None
    _CREATE_NO_WINDOW = 0


def ensure_dirs() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def send_telegram(title: str, message: str) -> None:
    try:
        from trading.notify import notify
        notify(title, message)
    except Exception as e:
        print(f"[notify error] {e}")


def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt" and _kernel32 is not None:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.wintypes.DWORD()
            if _kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == 259  # STILL_ACTIVE
            return False
        finally:
            _kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False


def load_pids() -> dict[str, int]:
    if PID_FILE.exists():
        try:
            with open(PID_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_pids(pids: dict[str, int]) -> None:
    ensure_dirs()
    with open(PID_FILE, "w") as f:
        json.dump(pids, f, indent=2)


def kill_proc(pid: int) -> None:
    if pid <= 0:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
                creationflags=_CREATE_NO_WINDOW,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


def stop_all() -> None:
    pids = load_pids()
    if not pids:
        print("No recorded running services found.")
        return

    print("Stopping services...")
    for name, pid in list(pids.items()):
        if is_pid_alive(pid):
            print(f"Killing {name} (PID: {pid})...")
            kill_proc(pid)
        else:
            print(f"{name} (PID: {pid}) was not running.")

    if PID_FILE.exists():
        PID_FILE.unlink(missing_ok=True)

    send_telegram("🛑 RanchoTrade Stopped", "All automated trading services were stopped.")
    print("All services stopped.")


def status() -> None:
    pids = load_pids()
    dash_port = is_port_in_use(8787)
    print("=== RanchoTrade Status ===")
    print(f"Web Dashboard Port (8787): {'LISTENING' if dash_port else 'STOPPED'}")

    if not pids:
        print("No services recorded in PID file.")
        return

    for name, pid in pids.items():
        alive = is_pid_alive(pid)
        print(f"{name:15} | PID {pid:6} | {'RUNNING' if alive else 'STOPPED'}")


class ServiceManager:
    def __init__(self):
        ensure_dirs()
        self.pids = load_pids()
        self.premarket_done_today: str | None = None
        self.journal_done_today: str | None = None

    def start_process(self, name: str, cmd: list[str], log_file: Path) -> subprocess.Popen:
        log_f = open(log_file, "a", encoding="utf-8")
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONPATH": str(PROJECT_ROOT)}
        flags = (_CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=flags
        )
        self.pids[name] = proc.pid
        save_pids(self.pids)
        print(f"[{datetime.now(IST).strftime('%H:%M:%S')}] Started {name} (PID: {proc.pid}) -> {log_file.name}", flush=True)
        return proc

    def ensure_dashboard(self) -> None:
        dash_pid = self.pids.get("dashboard")
        if not dash_pid or not is_pid_alive(dash_pid):
            if not is_port_in_use(8787):
                self.start_process(
                    "dashboard",
                    [PYTHON, "-u", "-m", "trading.dashboard", "8787"],
                    LOGS_DIR / "dashboard.log"
                )
            else:
                print("Dashboard port 8787 already active via existing process.", flush=True)

    def ensure_supervisor(self) -> None:
        sup_pid = self.pids.get("supervisor")
        if not sup_pid or not is_pid_alive(sup_pid):
            self.start_process(
                "supervisor",
                [PYTHON, "-u", "-m", "trading.agents.supervisor", "--loop"],
                LOGS_DIR / "supervisor.log"
            )

    def ensure_strategy(self) -> None:
        strat_pid = self.pids.get("strategy")
        if not strat_pid or not is_pid_alive(strat_pid):
            self.start_process(
                "strategy",
                [PYTHON, "-u", "-m", "trading.strategy"],
                LOGS_DIR / "strategy.log"
            )

    def run_premarket(self) -> None:
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        if self.premarket_done_today == today_str:
            return
        print(f"[{datetime.now(IST).strftime('%H:%M:%S')}] Running pre-market agent...")
        log_f = open(LOGS_DIR / "premarket.log", "a", encoding="utf-8")
        flags = _CREATE_NO_WINDOW if os.name == "nt" else 0
        subprocess.run([PYTHON, "-m", "trading.agents.premarket"],
                       cwd=str(PROJECT_ROOT), stdout=log_f, stderr=subprocess.STDOUT,
                       creationflags=flags)
        self.premarket_done_today = today_str

    def run_eod_journal(self) -> None:
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        if self.journal_done_today == today_str:
            return
        print(f"[{datetime.now(IST).strftime('%H:%M:%S')}] Running EOD journal agent...")
        log_f = open(LOGS_DIR / "eod_journal.log", "a", encoding="utf-8")
        flags = _CREATE_NO_WINDOW if os.name == "nt" else 0
        subprocess.run([PYTHON, "-m", "trading.agents.eod_journal"],
                       cwd=str(PROJECT_ROOT), stdout=log_f, stderr=subprocess.STDOUT,
                       creationflags=flags)
        self.journal_done_today = today_str

    def run_forever(self) -> None:
        # Save manager PID
        self.pids["manager"] = os.getpid()
        save_pids(self.pids)

        now = datetime.now(IST)
        hhmm = now.strftime("%H:%M")
        is_weekday = now.weekday() < 5
        market_open_now = is_weekday and ("09:15" <= hhmm < "15:30")

        # Initial launch
        self.ensure_dashboard()
        if market_open_now:
            self.ensure_supervisor()
            self.ensure_strategy()

        time.sleep(3)

        # Send Telegram Online Notification
        status_text = "Market Open (Trading Active)" if market_open_now else "Off-Market Hours (Dashboard Active)"
        send_telegram(
            "🚀 RanchoTrade Auto-Started",
            f"Laptop is ON · Session: {status_text}\n"
            f"• Dashboard: http://localhost:8787\n"
            f"• Status: All background services operational"
        )

        print(f"RanchoTrade manager started at {now.strftime('%Y-%m-%d %H:%M:%S')} IST.", flush=True)
        print(f"Dashboard available at: http://localhost:8787", flush=True)

        while True:
            try:
                now = datetime.now(IST)
                hhmm = now.strftime("%H:%M")
                is_weekday = now.weekday() < 5

                # 1. Always keep dashboard alive
                self.ensure_dashboard()

                # 2. Weekday trading schedule
                if is_weekday:
                    # 08:45 AM - 09:15 AM: run premarket agent
                    if "08:45" <= hhmm < "09:15":
                        self.run_premarket()

                    # 09:15 AM - 15:30 PM: Market hours — ensure strategy + supervisor running
                    if "09:15" <= hhmm < "15:30":
                        self.ensure_supervisor()
                        self.ensure_strategy()

                    # 15:35 PM: EOD Journal
                    if "15:35" <= hhmm < "16:15":
                        self.run_eod_journal()

                time.sleep(30)
            except KeyboardInterrupt:
                print("Exiting manager loop...")
                break
            except Exception as e:
                print(f"[Manager Exception] {e}")
                time.sleep(10)


def main():
    if "--stop" in sys.argv:
        stop_all()
    elif "--status" in sys.argv:
        status()
    else:
        mgr = ServiceManager()
        mgr.run_forever()


if __name__ == "__main__":
    main()
