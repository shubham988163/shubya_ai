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
    def __init__(self, mode: str = "paper", ledger: Ledger | None = None,
                 get_ltp=None):
        """
        mode:    "paper" or "live". Live requires kiteconnect + static IP +
                 broker Algo-ID tagging — see README before ever flipping this.
        get_ltp: callable(symbol) -> float. In paper mode this is your live
                 tick source; the demo passes a stub.
        """
        assert mode in ("paper", "live")
        self.mode = mode
        self.ledger = ledger or Ledger()
        self.get_ltp = get_ltp
        self.rate_limiter = RateLimiter()
        self.day_config = load_day_config()
        self.kite = None  # set externally in live mode

    # --- risk kernel: ALWAYS runs first, both modes ---

    def risk_check(self, signal: dict) -> str | None:
        """Return a rejection reason, or None if the signal passes."""
        if signal["symbol"] in self.day_config.get("blocked_symbols", []):
            return f"symbol_blocked_by_premarket_agent ({self.day_config.get('rationale', '')})"

        if self.day_config["risk_multiplier"] <= 0:
            return "risk_multiplier_zero (pre-market halt)"

        if self.ledger.day_realized_pnl() <= DAILY_LOSS_LIMIT:
            return "daily_loss_limit_hit"

        open_positions = self.ledger.open_trades()
        if (len(open_positions) >= MAX_OPEN_POSITIONS
                and signal["symbol"] not in {t["symbol"] for t in open_positions}):
            return f"max_open_positions ({len(open_positions)}/{MAX_OPEN_POSITIONS})"

        position_value = signal["qty"] * signal["price"]
        # Hard capital cap, never scaled: with a small account, scaling the
        # cap priced out every symbol on cautious days. The agent's risk
        # multiplier instead scales RISK_PER_TRADE in the engine's sizing
        # (and 0 still halts, above) — it can shrink size, never grow it.
        existing = self.ledger.open_position_value(signal["symbol"])
        if existing + position_value > MAX_POSITION_VALUE:
            return f"max_position_value (cap={MAX_POSITION_VALUE:.0f}, would_be={existing + position_value:.0f})"

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

        # live mode: static IP + Algo-ID tagging handled by broker for
        # sub-10-OPS personal use. Requires self.kite to be configured.
        raise NotImplementedError(
            "Live mode is intentionally not wired up. Complete the go-live "
            "checklist in README.md (static IP, OAuth automation, broker "
            "Algo-ID confirmation) before implementing kite.place_order()."
        )

    def simulate_fill(self, signal: dict) -> float:
        ltp = self.get_ltp(signal["symbol"]) if self.get_ltp else signal["price"]
        slip = SLIPPAGE_PCT * ltp
        return ltp + slip if signal["side"] == "BUY" else ltp - slip
