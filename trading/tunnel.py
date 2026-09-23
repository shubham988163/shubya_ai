"""Cloudflare Tunnel Manager for remote mobile/desktop access.

Exposes the local web dashboard (http://127.0.0.1:8787) to a secure public HTTPS URL
(e.g., https://xxxx.trycloudflare.com) with zero router configuration or port forwarding.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
TOOLS_DIR = PROJECT_ROOT / "tools"
TUNNEL_INFO_FILE = DATA_DIR / "tunnel_info.json"
TUNNEL_LOG = LOGS_DIR / "tunnel.log"
QR_SVG_FILE = DATA_DIR / "tunnel_qr.svg"
IST = ZoneInfo("Asia/Kolkata")

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_CREATE_NEW_PROCESS_GROUP = 0x00000200 if os.name == "nt" else 0


def find_cloudflared_binary() -> Path | None:
    """Find cloudflared executable."""
    # 1. Project tools directory
    local_bin = TOOLS_DIR / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
    if local_bin.exists():
        return local_bin

    # 2. PATH
    in_path = shutil.which("cloudflared")
    if in_path:
        return Path(in_path)

    # 3. WinGet package directory
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            winget_dir = Path(local_app) / "Microsoft" / "WinGet" / "Packages"
            if winget_dir.exists():
                for found in winget_dir.glob("**/cloudflared.exe"):
                    return found

    return None


def is_pid_alive(pid: int) -> bool:
    """Check if process ID is still alive."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        import ctypes.wintypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000 | 0x00100000, False, pid)
        if not h:
            return False
        try:
            code = ctypes.wintypes.DWORD()
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return False
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False


def kill_proc(pid: int) -> None:
    """Kill process by PID."""
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


def get_tunnel_info() -> dict:
    """Read saved tunnel status and verify liveness."""
    if not TUNNEL_INFO_FILE.exists():
        return {"active": False, "url": None, "pid": None, "port": 8787}
    try:
        with open(TUNNEL_INFO_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        pid = data.get("pid")
        if pid and is_pid_alive(pid):
            data["active"] = True
            return data
        # Stale PID
        data["active"] = False
        return data
    except Exception:
        return {"active": False, "url": None, "pid": None, "port": 8787}


def save_tunnel_info(info: dict) -> None:
    """Persist tunnel status to data/tunnel_info.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(TUNNEL_INFO_FILE, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)


def generate_qr_svg(url: str) -> Path | None:
    """Generate SVG QR code for web UI rendering."""
    try:
        import qrcode
        import qrcode.image.svg
        factory = qrcode.image.svg.SvgPathImage
        img = qrcode.make(url, image_factory=factory, box_size=10)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        img.save(str(QR_SVG_FILE))
        return QR_SVG_FILE
    except Exception as exc:
        print(f"[tunnel] QR generation failed: {exc}")
        return None


def get_qr_svg_content() -> str | None:
    """Return raw SVG content of the active QR code."""
    if QR_SVG_FILE.exists():
        try:
            return QR_SVG_FILE.read_text(encoding="utf-8")
        except Exception:
            pass
    return None


def print_ascii_qr(url: str) -> None:
    """Print clean QR code to terminal for mobile camera scanning."""
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        import qrcode
        qr = qrcode.QRCode()
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except Exception:
        pass


def start_tunnel(port: int = 8787, timeout: float = 30.0) -> dict:
    """Start cloudflared quick tunnel and capture the public URL."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Check if existing tunnel is still alive
    existing = get_tunnel_info()
    if existing.get("active") and existing.get("url"):
        return existing

    binary = find_cloudflared_binary()
    if not binary:
        raise FileNotFoundError(
            "cloudflared binary not found! Please install it or ensure tools/cloudflared.exe exists."
        )

    cmd = [
        str(binary),
        "tunnel",
        "--url",
        f"http://127.0.0.1:{port}",
        "--protocol",
        "http2",
        "--no-autoupdate",
    ]

    flags = (_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0

    log_file = open(TUNNEL_LOG, "a", encoding="utf-8")
    start_pos = log_file.tell()

    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )

    public_url = None
    t0 = time.time()

    # Read from the log file until the trycloudflare URL appears
    with open(TUNNEL_LOG, "r", encoding="utf-8", errors="replace") as reader:
        reader.seek(start_pos)
        while time.time() - t0 < timeout:
            line = reader.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.2)
                continue

            match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
            if match:
                public_url = match.group(0)
                break

    if not public_url:
        kill_proc(proc.pid)
        raise TimeoutError(
            f"Failed to obtain Cloudflare tunnel URL within {timeout} seconds. Check logs/tunnel.log."
        )

    tunnel_data = {
        "active": True,
        "url": public_url,
        "pid": proc.pid,
        "port": port,
        "started_at": time.time(),
        "started_iso": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
    }
    save_tunnel_info(tunnel_data)
    generate_qr_svg(public_url)

    # Send Telegram Notification
    try:
        from trading.notify import notify
        notify(
            "🌐 RanchoTrade Mobile & Remote Access Online",
            f"Public URL: {public_url}\n"
            f"• Access from your Phone or another PC\n"
            f"• Live Dashboard & F&O Scanner active"
        )
    except Exception:
        pass

    return tunnel_data


def stop_tunnel() -> bool:
    """Stop active tunnel process."""
    info = get_tunnel_info()
    pid = info.get("pid")
    stopped = False
    if pid and is_pid_alive(pid):
        kill_proc(pid)
        stopped = True
    info["active"] = False
    info["url"] = None
    info["pid"] = None
    save_tunnel_info(info)
    return stopped


def ensure_tunnel_running(port: int = 8787) -> tuple[int | None, str | None]:
    """Ensure tunnel is running; used by run_all.py."""
    info = get_tunnel_info()
    if info.get("active") and info.get("url"):
        return info.get("pid"), info.get("url")
    try:
        new_info = start_tunnel(port=port)
        return new_info.get("pid"), new_info.get("url")
    except Exception as exc:
        print(f"[tunnel error] {exc}")
        return None, None
