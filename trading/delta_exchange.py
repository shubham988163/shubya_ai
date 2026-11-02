"""Delta Exchange Client for 24/7 Crypto & Forex Derivatives Trading.

Supports both Delta Exchange India (api.india.delta.exchange) and Global (api.delta.exchange).
Provides live ticker quotes, OHLCV candle feeds for strategy evaluation, wallet inquiry,
and execution in both Paper and Live modes.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from trading.config import PROJECT_ROOT

logger = logging.getLogger("trading.delta")

FOREX_WATCHLIST = [
    "BTCUSD",     # Bitcoin / USD Perpetual
    "ETHUSD",     # Ethereum / USD Perpetual
    "SOLUSD",     # Solana / USD Perpetual
    "XAUTUSD",    # Gold (Tether Gold) / USD Perpetual
    "XRPUSD",     # Ripple / USD Perpetual
    "DOGEUSD",    # Dogecoin / USD Perpetual
    "BNBUSD",     # Binance Coin / USD Perpetual
    "ADAUSD",     # Cardano / USD Perpetual
]

POSITIONS_FILE = PROJECT_ROOT / "data" / "forex_positions.json"


class DeltaClient:
    """Client for Delta Exchange REST API v2."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
        mode: str | None = None,
    ):
        self.base_url = (
            base_url
            or os.getenv("DELTA_BASE_URL", "https://api.india.delta.exchange")
        ).rstrip("/")
        self.api_key = api_key or os.getenv("DELTA_API_KEY", "")
        self.api_secret = api_secret or os.getenv("DELTA_API_SECRET", "")
        self.mode = mode or os.getenv("TRADING_MODE", "paper").lower()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "RanchoTrade-Forex/1.0"})

    # --- Authentication & Request Signing ---

    def _auth_headers(self, method: str, path: str, payload_str: str = "") -> dict[str, str]:
        """Generate HMAC-SHA256 signature headers for authenticated endpoints."""
        timestamp = str(int(time.time()))
        message = method.upper() + timestamp + path + payload_str
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "api-key": self.api_key,
            "timestamp": timestamp,
            "signature": signature,
            "Content-Type": "application/json",
            "User-Agent": "RanchoTrade-Forex/1.0",
        }

    # --- Connection & Wallet Status ---

    def status(self) -> dict[str, Any]:
        """Check API connection and wallet details."""
        res: dict[str, Any] = {
            "connected": False,
            "mode": self.mode,
            "base_url": self.base_url,
            "api_key_configured": bool(self.api_key and self.api_secret),
            "balance": 0.0,
            "message": "Initializing...",
        }
        try:
            # First test public market data access
            t_resp = self.session.get(f"{self.base_url}/v2/tickers/BTCUSD", timeout=5)
            if t_resp.status_code == 200:
                res["connected"] = True
                res["message"] = "Market Data Online (Live Ticks Active)"
            else:
                res["message"] = f"Public endpoint returned status {t_resp.status_code}"
                return res

            # Next test authenticated wallet endpoint if credentials exist
            if self.api_key and self.api_secret:
                path = "/v2/wallet/balances"
                headers = self._auth_headers("GET", path)
                w_resp = self.session.get(f"{self.base_url}{path}", headers=headers, timeout=5)
                if w_resp.status_code == 200:
                    data = w_resp.json()
                    balances = data.get("result", [])
                    total_bal = 0.0
                    for b in balances:
                        total_bal += float(b.get("balance") or 0.0)
                    res["balance"] = round(total_bal, 2)
                    res["message"] = f"Authenticated & Connected ({self.mode.upper()} mode, Balance: ₹{total_bal:,.2f})"
                elif w_resp.status_code == 401:
                    err_json = w_resp.json() if w_resp.text else {}
                    err_code = err_json.get("error", {}).get("code", "")
                    if "ip_not_whitelisted" in err_code:
                        client_ip = err_json.get("error", {}).get("context", {}).get("client_ip", "Unknown")
                        res["client_ip"] = client_ip
                        res["message"] = f"Public Data OK. Live Orders need IP whitelisting in Delta: {client_ip}"
                    else:
                        res["message"] = f"Auth failed ({err_code or w_resp.status_code})"
        except Exception as e:
            res["message"] = f"Connection error: {e}"

        return res

    # --- Market Data ---

    def get_tickers(self, symbols: list[str] | None = None) -> dict[str, dict]:
        """Fetch real-time ticker data for specified symbols or default forex watchlist."""
        target = set(symbols or FOREX_WATCHLIST)
        out: dict[str, dict] = {}
        try:
            resp = self.session.get(f"{self.base_url}/v2/tickers", timeout=6)
            if resp.status_code == 200:
                raw_list = resp.json().get("result", [])
                for t in raw_list:
                    sym = t.get("symbol", "")
                    if not target or sym in target:
                        out[sym] = {
                            "symbol": sym,
                            "mark_price": float(t.get("mark_price") or t.get("close") or 0.0),
                            "close": float(t.get("close") or 0.0),
                            "open": float(t.get("open") or 0.0),
                            "high": float(t.get("high") or 0.0),
                            "low": float(t.get("low") or 0.0),
                            "volume": float(t.get("volume") or 0.0),
                            "turnover": float(t.get("turnover") or 0.0),
                            "change_24h": round(
                                ((float(t.get("close") or 0) - float(t.get("open") or 0)) / max(1e-5, float(t.get("open") or 1))) * 100,
                                2
                            ) if t.get("open") else 0.0,
                        }
        except Exception as e:
            logger.error(f"Error fetching Delta tickers: {e}")
        return out

    def get_candles(self, symbol: str, resolution: str = "5", bars: int = 60) -> pd.DataFrame | None:
        """Fetch historical OHLCV candles from Delta Exchange TradingView UDF endpoint.
        
        resolution: '1', '5', '15', '60', etc.
        """
        now = int(time.time())
        res_secs = int(resolution) * 60 if resolution.isdigit() else 300
        start = now - (res_secs * max(bars, 30))
        url = f"{self.base_url}/v2/chart/history?symbol={symbol}&resolution={resolution}&from={start}&to={now}"

        try:
            resp = self.session.get(url, timeout=8)
            if resp.status_code == 200:
                data = resp.json().get("result", {})
                closes = data.get("c", [])
                if not closes:
                    return None
                timestamps = data.get("t", [])
                opens = data.get("o", [])
                highs = data.get("h", [])
                lows = data.get("l", [])
                volumes = data.get("v", [])

                idx = pd.to_datetime(timestamps, unit="s")
                df = pd.DataFrame(
                    {
                        "Open": [float(x) for x in opens],
                        "High": [float(x) for x in highs],
                        "Low": [float(x) for x in lows],
                        "Close": [float(x) for x in closes],
                        "Volume": [float(x) for x in volumes],
                    },
                    index=idx,
                ).sort_index()
                return df
        except Exception as e:
            logger.error(f"Error fetching candles for {symbol}: {e}")
        return None

    # --- Order Execution & Position Tracking ---

    def _load_paper_positions(self) -> list[dict]:
        if not POSITIONS_FILE.exists():
            return []
        try:
            with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save_paper_positions(self, positions: list[dict]) -> None:
        POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(positions, f, indent=2)

    def execute_trade(
        self,
        symbol: str,
        side: str,
        size: float,
        price: float,
        stop_loss: float,
        target: float,
        strategy_id: str = "forex_orb_vwap",
        conviction: int = 8,
    ) -> dict[str, Any]:
        """Execute a trade in either paper or live mode."""
        side = side.upper()
        trade_id = int(time.time() * 1000)

        trade_record = {
            "id": trade_id,
            "symbol": symbol,
            "side": side,
            "size": size,
            "entry_price": price,
            "stop_loss": stop_loss,
            "initial_stop": stop_loss,
            "target": target,
            "current_price": price,
            "pnl": 0.0,
            "pnl_pct": 0.0,
            "strategy_id": strategy_id,
            "conviction": conviction,
            "status": "OPEN",
            "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": self.mode,
        }

        if self.mode == "paper":
            positions = self._load_paper_positions()
            # Deduplication: block opening duplicate trade on same symbol
            if any(p["symbol"] == symbol and p.get("status") == "OPEN" for p in positions):
                return {"ok": False, "reason": f"Active trade already open on {symbol}"}
            positions.append(trade_record)
            self._save_paper_positions(positions)
            return {"ok": True, "trade_id": trade_id, "mode": "paper", "trade": trade_record}

        # Live Execution via Delta API
        path = "/v2/orders"
        body = {
            "product_symbol": symbol,
            "size": int(size),
            "side": "buy" if side in ("BUY", "LONG") else "sell",
            "order_type": "market_order",
        }
        body_str = json.dumps(body)
        headers = self._auth_headers("POST", path, body_str)
        try:
            resp = self.session.post(f"{self.base_url}{path}", data=body_str, headers=headers, timeout=10)
            if resp.status_code in (200, 201):
                res_data = resp.json().get("result", {})
                live_id = res_data.get("id") or trade_id
                trade_record["id"] = live_id
                trade_record["live_order"] = res_data
                return {"ok": True, "trade_id": live_id, "mode": "live", "trade": trade_record}
            else:
                return {"ok": False, "reason": f"Delta order error ({resp.status_code}): {resp.text}"}
        except Exception as e:
            return {"ok": False, "reason": f"Live order failed: {e}"}

    def get_positions(self) -> list[dict]:
        """Fetch open positions and update live P&L and trailing stops."""
        positions = self._load_paper_positions()
        open_pos = [p for p in positions if p.get("status") == "OPEN"]
        if not open_pos:
            return []

        tickers = self.get_tickers([p["symbol"] for p in open_pos])
        updated = False

        for p in open_pos:
            sym = p["symbol"]
            t = tickers.get(sym)
            if not t:
                continue
            curr = t["mark_price"]
            p["current_price"] = curr
            entry = p["entry_price"]
            side = p["side"]
            sl = p["stop_loss"]
            p.setdefault("initial_stop", sl)
            tg = p["target"]
            initial_sl = float(p.get("initial_stop") or sl)
            r_dist = abs(entry - initial_sl)
            if r_dist <= 0:
                r_dist = max(0.5, entry * 0.002)
            decimals = 2 if entry > 500 else (3 if entry > 10 else 4)

            # Calculate P&L
            if side in ("BUY", "LONG"):
                pnl = (curr - entry) * p["size"]
                pnl_pct = ((curr - entry) / entry) * 100
                # Breakeven arming at +1.0R gain
                if curr >= entry + (1.0 * r_dist) and sl < entry:
                    p["stop_loss"] = entry
                    p["trailing_note"] = "Breakeven locked (+1.0R reached)"
                    updated = True
                # Trailing stop update past +1.5R gain (trails 1.0R behind mark price)
                elif curr >= entry + (1.5 * r_dist):
                    new_sl = round(curr - (1.0 * r_dist), decimals)
                    if new_sl > p["stop_loss"]:
                        p["stop_loss"] = new_sl
                        p["trailing_note"] = "Trailing stop advanced"
                        updated = True
                # Check exit
                if curr <= sl:
                    p["status"] = "CLOSED"
                    p["exit_reason"] = "Stop loss hit"
                    p["exit_price"] = sl
                    p["exit_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    pnl = (sl - entry) * p["size"]
                    pnl_pct = ((sl - entry) / entry) * 100
                    updated = True
                elif curr >= tg:
                    p["status"] = "CLOSED"
                    p["exit_reason"] = "Target hit"
                    p["exit_price"] = tg
                    p["exit_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    pnl = (tg - entry) * p["size"]
                    pnl_pct = ((tg - entry) / entry) * 100
                    updated = True
            else:
                pnl = (entry - curr) * p["size"]
                pnl_pct = ((entry - curr) / entry) * 100
                # Breakeven arming at +1.0R gain
                if curr <= entry - (1.0 * r_dist) and sl > entry:
                    p["stop_loss"] = entry
                    p["trailing_note"] = "Breakeven locked (+1.0R reached)"
                    updated = True
                # Trailing stop update past +1.5R gain (trails 1.0R behind mark price)
                elif curr <= entry - (1.5 * r_dist):
                    new_sl = round(curr + (1.0 * r_dist), decimals)
                    if new_sl < p["stop_loss"]:
                        p["stop_loss"] = new_sl
                        p["trailing_note"] = "Trailing stop advanced"
                        updated = True
                if curr >= sl:
                    p["status"] = "CLOSED"
                    p["exit_reason"] = "Stop loss hit"
                    p["exit_price"] = sl
                    p["exit_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    pnl = (entry - sl) * p["size"]
                    pnl_pct = ((entry - sl) / entry) * 100
                    updated = True
                elif curr <= tg:
                    p["status"] = "CLOSED"
                    p["exit_reason"] = "Target hit"
                    p["exit_price"] = tg
                    p["exit_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    pnl = (entry - tg) * p["size"]
                    pnl_pct = ((entry - tg) / entry) * 100
                    updated = True

            p["pnl"] = round(pnl, 2)
            p["pnl_pct"] = round(pnl_pct, 2)

        if updated or open_pos:
            self._save_paper_positions(positions)

        return [p for p in open_pos if p.get("status") == "OPEN"]

    def close_position(self, trade_id: int | str) -> dict[str, Any]:
        """Close an open position manually."""
        positions = self._load_paper_positions()
        matched = False
        for p in positions:
            if str(p.get("id")) == str(trade_id) and p.get("status") == "OPEN":
                p["status"] = "CLOSED"
                p["exit_reason"] = "Manual square-off"
                p["exit_price"] = p.get("current_price", p["entry_price"])
                p["exit_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                matched = True
                break
        if matched:
            self._save_paper_positions(positions)
            return {"ok": True, "message": f"Position #{trade_id} closed"}
        return {"ok": False, "reason": "Position not found or already closed"}

    def get_all_positions(self) -> list[dict]:
        """Fetch all positions (both OPEN and CLOSED)."""
        self.get_positions()  # Refresh marks and exits first
        return self._load_paper_positions()
