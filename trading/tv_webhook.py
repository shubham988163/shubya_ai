"""TradingView webhook receiver — Path A bridge into the paper pipeline.

TradingView fires the strategy's order-fill alerts (JSON built in
tradingview/ema_9_21.pine) at this endpoint. Entries go through the SAME
ExecutionRouter (risk kernel, rate limiter, agent day-config) and land in the
same ledger — so the supervisor reviews them, the journal analyzes them, and
the dashboard shows them, tagged with their own strategy_id.

Security:
  - Binds to 127.0.0.1 only; expose via a tunnel (ngrok/cloudflared).
  - Every payload must carry the shared secret (TV_WEBHOOK_SECRET env var),
    or it is rejected and logged.
  - The risk kernel still caps anything that gets through.

Usage:
  export TV_WEBHOOK_SECRET=<random string, same as the Pine input>
  python -m trading.tv_webhook [port]        # default 8788
  ngrok http 8788                            # -> webhook URL for TradingView
"""
from __future__ import annotations

import hmac
import json
import os
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

from trading.config import CHARGES_PCT_ROUND_TRIP
from trading.execution_router import ExecutionRouter
from trading.ledger import Ledger
from trading.notify import notify

SECRET = os.environ.get("TV_WEBHOOK_SECRET", "change-me")

ledger = Ledger()
router = ExecutionRouter(mode="paper", ledger=ledger)
_last_price: dict[str, float] = {}
router.get_ltp = lambda sym: _last_price[sym]


def handle_entry(p: dict) -> dict:
    signal = {
        "symbol": str(p["symbol"]),
        "side": str(p["side"]).upper(),
        "qty": int(p["qty"]),
        "price": float(p["price"]),
        "ts": time.time(),
        "stop_loss": float(p["sl"]) if p.get("sl") is not None else None,
        "target": float(p["target"]) if p.get("target") is not None else None,
        "strategy_id": str(p.get("strategy_id", "tv")),
        "regime": router.day_config.get("regime"),
    }
    if signal["side"] not in ("BUY", "SELL"):
        return {"ok": False, "error": "bad side"}
    _last_price[signal["symbol"]] = signal["price"]
    trade_id = router.execute(signal)
    if trade_id is None:
        reason = getattr(router, "last_rejection", None) or "risk kernel"
        # TV rejections stay notified (your live TV strategy diverged from the
        # paper book — worth knowing), now with the specific reason.
        notify(f"🚫 REJECTED {signal['side']} {signal['symbol']} (TV)",
               f"×{signal['qty']} @ {signal['price']:.2f} — {reason}")
        return {"ok": False, "rejected": True, "reason": reason}
    print(f"ENTRY {signal['side']} {signal['symbol']} x{signal['qty']} "
          f"@ ~{signal['price']:.2f} -> trade #{trade_id}")
    notify(f"📈 ENTRY {signal['side']} {signal['symbol']}",
           f"×{signal['qty']} @ ~{signal['price']:.2f} · SL {signal['stop_loss']} · "
           f"TGT {signal['target']} ({signal['strategy_id']})")
    return {"ok": True, "trade_id": trade_id}


def handle_exit(p: dict) -> dict:
    symbol = str(p["symbol"])
    exit_price = float(p["price"])
    strategy_id = str(p.get("strategy_id", "tv"))
    open_trades = [t for t in ledger.open_trades()
                   if t["symbol"] == symbol and t["strategy_id"] == strategy_id]
    if not open_trades:
        return {"ok": False, "error": f"no open {strategy_id} trade for {symbol}"}
    closed = []
    for t in open_trades:  # TV closes the whole position; mirror that
        charges = t["qty"] * (t["entry_price"] + exit_price) * CHARGES_PCT_ROUND_TRIP
        pnl = ledger.record_exit(t["id"], round(exit_price, 2),
                                 charges=round(charges, 2))
        print(f"EXIT  {t['side']} {symbol} @ {exit_price:.2f} -> pnl {pnl:.2f}")
        notify(f"{'✅' if pnl >= 0 else '🔻'} EXIT {t['side']} {symbol}",
               f"@ {exit_price:.2f} → P&L {pnl:+.2f} INR · day {ledger.day_realized_pnl():+.2f}")
        closed.append({"trade_id": t["id"], "pnl": round(pnl, 2)})
    return {"ok": True, "closed": closed}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        if self.path != "/webhook":
            return self._send(404, {"ok": False, "error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length))
        except (ValueError, TypeError):
            return self._send(400, {"ok": False, "error": "bad json"})

        if not hmac.compare_digest(str(payload.get("secret", "")), SECRET):
            ledger.record_rejection(payload, "tv_webhook_bad_secret")
            return self._send(403, {"ok": False, "error": "bad secret"})
        payload.pop("secret", None)

        try:
            event = payload.get("event")
            if event == "entry":
                result = handle_entry(payload)
            elif event == "exit":
                result = handle_exit(payload)
            else:
                result = {"ok": False, "error": f"unknown event {event!r}"}
        except (KeyError, ValueError, TypeError) as e:
            ledger.record_rejection(payload, f"tv_webhook_bad_payload: {e!r}")
            result = {"ok": False, "error": f"bad payload: {e!r}"}
        self._send(200 if result.get("ok") else 422, result)

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # quiet
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8788
    if SECRET == "change-me":
        print("WARNING: TV_WEBHOOK_SECRET not set — using the default. "
              "Set a random secret before exposing this via a tunnel.")
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"TradingView webhook receiver: http://127.0.0.1:{port}/webhook")
    print("expose with:  ngrok http " + str(port))
    server.serve_forever()


if __name__ == "__main__":
    main()
