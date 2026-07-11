"""Push notifications for trade and agent events.

Channels (all fire-and-forget; a notification failure never affects trading):
  - macOS banner via osascript — works out of the box on this machine.
  - Telegram — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to enable
    (create a bot with @BotFather, get your chat id from @userinfobot).

Used by: tv_webhook (entries/exits/rejections), strategy engine (entries/exits),
supervisor (caution/veto verdicts), pre-market agent (day config).
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request


def notify(title: str, message: str) -> None:
    _macos(title, message)
    _telegram(title, message)


def _macos(title: str, message: str) -> None:
    try:
        script = 'display notification "{}" with title "{}"'.format(
            message.replace('"', "'"), title.replace('"', "'"))
        subprocess.run(["osascript", "-e", script], timeout=5,
                       capture_output=True, check=False)
    except Exception:  # noqa: BLE001 — never let notifications break trading
        pass


def _telegram(title: str, message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        payload = json.dumps({"chat_id": chat_id,
                              "text": f"{title}\n{message}"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:  # noqa: BLE001
        pass
