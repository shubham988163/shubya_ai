"""CLI runner for Cloudflare Tunnel remote access.

Usage:
  python scripts/tunnel.py          # Start tunnel, print URL and mobile QR code
  python scripts/tunnel.py --status # Check status of tunnel and show public URL
  python scripts/tunnel.py --stop   # Stop tunnel
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

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
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trading.tunnel import (
    start_tunnel,
    stop_tunnel,
    get_tunnel_info,
    print_ascii_qr,
)


def main():
    parser = argparse.ArgumentParser(description="Cloudflare Tunnel CLI for RanchoTrade Dashboard")
    parser.add_argument("--port", type=int, default=8787, help="Local port to forward (default: 8787)")
    parser.add_argument("--stop", action="store_true", help="Stop running tunnel")
    parser.add_argument("--status", action="store_true", help="Show tunnel status and public URL")
    parser.add_argument("--qr", action="store_true", help="Display QR code for active tunnel")
    parser.add_argument("--daemon", "--bg", action="store_true", help="Start tunnel in background and exit")
    args = parser.parse_args()

    if args.stop:
        if stop_tunnel():
            print("🛑 Cloudflare tunnel stopped successfully.")
        else:
            print("No active Cloudflare tunnel found.")
        return

    if args.status or args.qr:
        info = get_tunnel_info()
        if info.get("active") and info.get("url"):
            print("\n=======================================================")
            print("  🌐 CLOUDFLARE TUNNEL IS ACTIVE")
            print("=======================================================")
            print(f"• Public URL:   {info['url']}")
            print(f"• Local Target: http://127.0.0.1:{info.get('port', 8787)}")
            print(f"• Process PID:  {info.get('pid')}")
            print("=======================================================\n")
            print("📱 Scan this QR code with your phone camera to open:")
            print_ascii_qr(info["url"])
            print(f"\nDirect Link: {info['url']}\n")
        else:
            print("Cloudflare tunnel is currently NOT running.")
            print("Run 'python scripts/tunnel.py' to start it.")
        return

    # Start or attach to tunnel
    print("🚀 Connecting to Cloudflare Edge Network...")
    try:
        import time
        info = start_tunnel(port=args.port)
        url = info.get("url")
        print("\n=======================================================")
        print("  🎉 CLOUDFLARE TUNNEL CONNECTED SUCCESSFULLY!")
        print("=======================================================")
        print(f"• Public HTTPS URL : {url}")
        print(f"• Local Dashboard  : http://127.0.0.1:{args.port}")
        print(f"• Process PID      : {info.get('pid')}")
        print("=======================================================\n")
        print("📱 Scan this QR code with your mobile camera to open:")
        print_ascii_qr(url)
        print(f"\nDirect Link: {url}")
        print("• You can access this link from any mobile, tablet, or PC worldwide!\n")

        if args.daemon:
            print("• Running in background daemon mode. Managed via scripts/run_all.py or 'python scripts/tunnel.py --stop'.\n")
            return

        print("Press Ctrl+C to disconnect tunnel (or run with --daemon to keep in background).")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop_tunnel()
            print("\nTunnel disconnected.")
    except Exception as exc:
        print(f"❌ Error starting Cloudflare tunnel: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
