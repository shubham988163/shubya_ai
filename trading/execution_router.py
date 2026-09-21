"""Execution router — the paper/live switch and the safety kernel.

Hard risk limits live HERE, not in the strategy and not in the AI agent.
A bug or a bad LLM output can never blow past them. The AI agent only ever
adjusts parameters (risk multiplier, blocked symbols) within bounds enforced
in this file.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from trading.config import (
    DAILY_LOSS_LIMIT,
    MAX_POSITION_VALUE,
    MAX_OPEN_POSITIONS,
    MAX_ORDERS_PER_SEC,
    SLIPPAGE_PCT,
    TODAY_CONFIG_PATH,
    FALLBACK_DAY_CONFIG,
    TRADING_MODE,
    FYERS_APP_ID,
    FYERS_TOKEN_PATH,
)
from trading.ledger import Ledger

IST = ZoneInfo("Asia/Kolkata")


class RateLimiter:
    """Sliding one-second window. Keeps us under SEBI's 10 orders-per-second
    registration threshold with a buffer."""

    def __init__(self, max_per_sec: int = MAX_ORDERS_PER_SEC):
        self.max_per_sec = max_per_sec
        self._timestamps: list[float] = []

    def allow(self) -> bool:
        now = time.monotonic()
        self._timestamps = [t for t in self._timestamps if now - t < 1.0]
        if len(self._timestamps) >= self.max_per_sec:
            return False
        self._timestamps.append(now)
        return True


def load_day_config() -> dict:
    """Read the config written by the pre-market agent; safe defaults if absent.

    A config whose `date` is not today is REJECTED. The router re-reads this
    file on every order, so a stale file would silently apply an old session's
    regime, risk multiplier and blocked_symbols to today's trades — e.g. after
    a missed pre-market run (2026-08-13 was skipped entirely because the Mac
    was asleep, leaving 08-12's config in place). Falling back to
    FALLBACK_DAY_CONFIG trades at half size instead of trusting stale advice.
    """
    try:
        with open(TODAY_CONFIG_PATH) as f:
            cfg = json.load(f)
        today = datetime.now(IST).strftime("%Y-%m-%d")
        cfg_date = str(cfg.get("date", ""))
        if cfg_date != today:
            stale = dict(FALLBACK_DAY_CONFIG)
            stale["rationale"] = (f"fallback: today_config.json is for {cfg_date or 'an unknown date'}, "
                                  f"not {today} — pre-market agent did not run today")
            return stale
        cfg["risk_multiplier"] = max(0.0, min(1.0, float(cfg.get("risk_multiplier", 0.5))))
        cfg.setdefault("blocked_symbols", [])
        cfg.setdefault("regime", "choppy")
        return cfg
    except (OSError, ValueError, TypeError):
        return dict(FALLBACK_DAY_CONFIG)


class ExecutionRouter:
    def __init__(self, mode: str | None = None, ledger: Ledger | None = None,
                 get_ltp=None):
        """
        mode:    "paper" or "live". Defaults to TRADING_MODE from config.py.
                 Paper mode is safe simulation. Live mode places real orders on Fyers.
        get_ltp: callable(symbol) -> float. Live tick price source.
        """
        mode = mode or TRADING_MODE
        assert mode in ("paper", "live")
        self.mode = mode
        self.ledger = ledger or Ledger()
        self.get_ltp = get_ltp
        self.rate_limiter = RateLimiter()
        self.day_config = load_day_config()
        self.kite = None

    # --- risk kernel: ALWAYS runs first, both modes ---

    def risk_check(self, signal: dict) -> str | None:
        """Return a rejection reason, or None if the signal passes."""
        # 1. 15k to 1 Lakh Challenge Survival Checks
        try:
            from trading.challenge import can_enter_trade, get_challenge_state
            can_enter, reason = can_enter_trade(signal.get("symbol"))
            if not can_enter:
                return f"challenge_survival_halt ({reason})"
            challenge_state = get_challenge_state()
            active_daily_limit = challenge_state.get("daily_loss_limit", DAILY_LOSS_LIMIT)
            active_max_open = challenge_state.get("max_open_positions", MAX_OPEN_POSITIONS)
            active_max_pos_val = challenge_state.get("max_position_value", MAX_POSITION_VALUE)
        except Exception:
            active_daily_limit = DAILY_LOSS_LIMIT
            active_max_open = MAX_OPEN_POSITIONS
            active_max_pos_val = MAX_POSITION_VALUE

        if signal["symbol"] in self.day_config.get("blocked_symbols", []):
            return f"symbol_blocked_by_premarket_agent ({self.day_config.get('rationale', '')})"

        if self.day_config["risk_multiplier"] <= 0:
            return "risk_multiplier_zero (pre-market halt)"

        if self.ledger.day_realized_pnl() <= active_daily_limit:
            return f"daily_loss_limit_hit ({self.ledger.day_realized_pnl():.2f} <= {active_daily_limit:.2f})"

        today = datetime.now(IST).strftime("%Y-%m-%d")
        open_positions = [t for t in self.ledger.open_trades() if t.get("date") == today]
        if (len(open_positions) >= active_max_open
                and signal["symbol"] not in {t["symbol"] for t in open_positions}):
            return f"max_open_positions ({len(open_positions)}/{active_max_open})"

        position_value = signal["qty"] * signal["price"]
        existing = self.ledger.open_position_value(signal["symbol"])
        if existing + position_value > active_max_pos_val:
            return f"max_position_value (cap={active_max_pos_val:.0f}, would_be={existing + position_value:.0f})"

        return None

    # --- execution ---

    def execute(self, signal: dict) -> int | None:
        """Execute a signal. Returns the ledger trade id, or None if rejected."""
        # Re-read the agent's day config on every order — long-running processes
        # (webhook receiver, live loop) must pick up a config written after they
        # started, e.g. the 8:45 pre-market run or an intraday halt.
        self.day_config = load_day_config()
        self.last_rejection = None
        reason = self.risk_check(signal)
        if reason:
            self.last_rejection = reason
            self.ledger.record_rejection(signal, reason)
            return None

        if not self.rate_limiter.allow():
            self.last_rejection = "rate_limit"
            self.ledger.record_rejection(signal, "rate_limit")
            return None

        if self.mode == "paper":
            fill = self.simulate_fill(signal)
            return self.ledger.record_entry(signal, fill_price=fill, mode="paper")

        # Live mode: execute directly via Fyers API v3
        return self._execute_fyers_live(signal)

    def _execute_fyers_live(self, signal: dict) -> int | None:
        """Execute a real-money order via Fyers API v3."""
        try:
            from fyers_apiv3 import fyersModel
            from trading.fno.fyers import load_token
            token, _ = load_token()
            if not token:
                self.last_rejection = "fyers_token_missing (please authenticate via /api/fyers/login)"
                self.ledger.record_rejection(signal, self.last_rejection)
                print(f"[LIVE FYERS ERROR] Cannot trade live: {self.last_rejection}")
                return None

            fyers = fyersModel.FyersModel(client_id=FYERS_APP_ID, token=token, is_async=False, log_path="")

            sym = str(signal["symbol"]).strip().upper()
            if not (sym.startswith("NSE:") or sym.startswith("BSE:") or sym.startswith("MCX:")):
                fyers_sym = f"NSE:{sym}-EQ"
            else:
                fyers_sym = sym

            side = 1 if str(signal["side"]).upper() == "BUY" else -1
            order_data = {
                "symbol": fyers_sym,
                "qty": int(signal["qty"]),
                "type": 2,  # Market order for immediate fill
                "side": side,
                "productType": "INTRADAY",
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": False,
                "stopLoss": 0,
                "takeProfit": 0
            }

            resp = fyers.place_order(data=order_data)
            if isinstance(resp, dict) and resp.get("s") == "ok":
                order_id = resp.get("id")
                ltp = self.get_ltp(signal["symbol"]) if self.get_ltp else signal["price"]
                trade_id = self.ledger.record_entry(signal, fill_price=ltp, mode="live")
                print(f"[LIVE FYERS SUCCESS] Real order placed: {fyers_sym} x{signal['qty']} (Fyers ID: {order_id}) -> Ledger #{trade_id}")
                return trade_id
            else:
                err_msg = resp.get("message") if isinstance(resp, dict) else str(resp)
                self.last_rejection = f"fyers_broker_rejection: {err_msg}"
                self.ledger.record_rejection(signal, self.last_rejection)
                print(f"[LIVE FYERS REJECTED] Broker rejected: {err_msg}")
                return None
        except Exception as exc:
            self.last_rejection = f"fyers_exception: {exc}"
            self.ledger.record_rejection(signal, self.last_rejection)
            print(f"[LIVE FYERS ERROR] Exception: {exc}")
            return None

    def simulate_fill(self, signal: dict) -> float:
        ltp = self.get_ltp(signal["symbol"]) if self.get_ltp else signal["price"]
        slip = SLIPPAGE_PCT * ltp
        return ltp + slip if signal["side"] == "BUY" else ltp - slip
