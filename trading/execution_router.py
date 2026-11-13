"""Execution router — the paper/live switch and the safety kernel.

Hard risk limits live HERE, not in the strategy and not in the AI agent.
A bug or a bad LLM output can never blow past them. The AI agent only ever
adjusts parameters (risk multiplier, blocked symbols) within bounds enforced
in this file.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from trading.config import (
    ALLOW_DUPLICATE_SYMBOLS,
    DAILY_LOSS_LIMIT,
    DAILY_PROFIT_TARGET,
    MAX_DAILY_TRADES,
    MAX_POSITION_VALUE,
    MAX_OPEN_POSITIONS,
    MAX_ORDERS_PER_SEC,
    REENTRY_COOLDOWN_MINUTES,
    LOSS_COOLDOWN_MINUTES,
    MAX_LOSSES_PER_SYMBOL_DAY,
    SLIPPAGE_PCT,
    CHARGES_PCT_ROUND_TRIP,
    MAX_COST_RISK_RATIO,
    TODAY_CONFIG_PATH,
    FALLBACK_DAY_CONFIG,
)
from trading.ledger import Ledger

IST = ZoneInfo("Asia/Kolkata")


def extract_underlying(sym: str) -> str:
    """Extract base stock or index ticker from equity or option contract symbol.

    Examples:
        'NSE:MUTHOOTFIN26NOV2450CE' -> 'MUTHOOTFIN'
        'NSE:CDSL26NOV1260CE'       -> 'CDSL'
        'NSE:BANKNIFTY26OCT55700CE' -> 'BANKNIFTY'
        'NSE:NIFTY26O0623150CE'     -> 'NIFTY'
        'NSE:RELIANCE-EQ'           -> 'RELIANCE'
        'CDSL'                      -> 'CDSL'
    """
    if not sym:
        return ""
    s = str(sym).upper().strip()
    for prefix in ("NSE:", "BSE:", "MCX:", "OPTIDX", "OPTSTK"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    for suffix in ("-EQ", "-INDEX", ".NS"):
        if s.endswith(suffix):
            s = s[:-len(suffix)]
    if "BANKNIFTY" in s:
        return "BANKNIFTY"
    if "SENSEX" in s:
        return "SENSEX"
    if "FINNIFTY" in s:
        return "FINNIFTY"
    if "MIDCPNIFTY" in s:
        return "MIDCPNIFTY"
    if s == "NIFTY50" or s.startswith("NIFTY50"):
        return "NIFTY50"
    if "NIFTY" in s and "BANK" not in s:
        return "NIFTY"
    # Standard Indian option format: <TICKER><2-digit YY><Month/Week><Strike><CE|PE>
    m = re.match(r"^([A-Z&_-]+?)(\d{2}[A-Z0-9]+?\d+(?:CE|PE))$", s)
    if m:
        return m.group(1)
    # Generic option format without YY: <TICKER><Strike><CE|PE>
    m2 = re.match(r"^([A-Z&_-]+?)\d+(?:CE|PE)$", s)
    if m2:
        return m2.group(1)
    return s


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
            stale["source"] = "fallback"
            stale["rationale"] = (f"fallback: today_config.json is for {cfg_date or 'an unknown date'}, "
                                  f"not {today} — pre-market agent did not run today")
            return stale
        cfg["risk_multiplier"] = max(0.0, min(1.0, float(cfg.get("risk_multiplier", 0.5))))
        cfg.setdefault("blocked_symbols", [])
        cfg.setdefault("regime", "choppy")
        return cfg
    except (OSError, ValueError, TypeError):
        missing = dict(FALLBACK_DAY_CONFIG)
        missing["source"] = "fallback"
        return missing


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

        # 1. Structural stop check (no fallback)
        stop_loss = signal.get("stop_loss")
        if stop_loss is None or stop_loss <= 0:
            return "missing_stop_loss (trade rejected: structural stop required)"

        price = float(signal.get("price", 0.0))
        side = signal.get("side")
        sym = str(signal.get("symbol", ""))
        is_option = bool(signal.get("is_option")) or bool(re.search(r"\d+(?:CE|PE)$", sym))

        if is_option or side == "BUY":
            if stop_loss >= price:
                return f"invalid_stop_loss (BUY stop_loss {stop_loss:.2f} must be below entry price {price:.2f})"
        elif side == "SELL" and stop_loss <= price:
            return f"invalid_stop_loss (SELL stop_loss {stop_loss:.2f} must be above entry price {price:.2f})"

        per_share_risk = abs(price - stop_loss)
        if per_share_risk <= 0.05:
            return "invalid_stop_loss (stop loss too close to entry price)"

        if is_option:
            from trading.config import MIN_OPTION_STOP_LOSS_PCT
            min_dist_pct = MIN_OPTION_STOP_LOSS_PCT if MIN_OPTION_STOP_LOSS_PCT is not None else 0.03
            if price > 0 and (per_share_risk / price) < min_dist_pct:
                return (f"stop_loss_too_tight_for_spread (SL distance {per_share_risk:.2f} "
                        f"is < {min_dist_pct*100:.1f}% of premium; noise stopped)")

        # 2. Cost-floor check: round-trip friction cannot exceed MAX_COST_RISK_RATIO of risk
        if is_option:
            from trading.costs import check_option_cost_floor
            passed, cost_ratio = check_option_cost_floor(price, stop_loss, qty=int(signal.get("qty", 1)))
            if not passed:
                return (f"option_cost_floor_breached (round-trip friction {cost_ratio*100:.1f}% of risk "
                        f"exceeds limit {MAX_COST_RISK_RATIO*100:.1f}%)")
        else:
            from trading.costs import check_cost_floor
            passed, cost_ratio = check_cost_floor(price, stop_loss, qty=int(signal.get("qty", 1)))
            if not passed:
                return (f"cost_floor_breached (round-trip friction {cost_ratio*100:.1f}% of risk "
                        f"exceeds limit {MAX_COST_RISK_RATIO*100:.1f}%)")

        # 3. Trend gate check (router hard risk kernel — restricted to orb strategies)
        strat_id = str(signal.get("strategy_id", ""))
        if strat_id.startswith("orb_ma200") or strat_id.startswith("orb_multi_ma"):
            trend_state = signal.get("trend_state")
            if trend_state not in ("up", "down"):
                return f"trend_gate_failed (trend state '{trend_state}' is not permitted)"
            if is_option:
                opt_type = signal.get("option_type") or ("CE" if "CE" in sym else "PE" if "PE" in sym else "CE")
                if opt_type == "CE" and trend_state != "up":
                    return f"trend_gate_failed (Call Option CE requires trend_state 'up', got '{trend_state}')"
                if opt_type == "PE" and trend_state != "down":
                    return f"trend_gate_failed (Put Option PE requires trend_state 'down', got '{trend_state}')"
            else:
                if side == "BUY" and trend_state != "up":
                    return f"trend_gate_failed (BUY requires trend_state 'up', got '{trend_state}')"
                if side == "SELL" and trend_state != "down":
                    return f"trend_gate_failed (SELL requires trend_state 'down', got '{trend_state}')"

        day_pnl = self.ledger.day_realized_pnl()

        if day_pnl <= DAILY_LOSS_LIMIT:
            return "daily_loss_limit_hit"

        if day_pnl >= DAILY_PROFIT_TARGET:
            return "daily_profit_target_hit (locking in gains for the day)"

        if MAX_DAILY_TRADES is not None and MAX_DAILY_TRADES > 0:
            today_trades = self.ledger.trades_for_date()
            if len(today_trades) >= MAX_DAILY_TRADES:
                return f"max_daily_trades_reached ({len(today_trades)}/{MAX_DAILY_TRADES} trades completed today)"

        # Deduplication check: strictly prevent double / duplicate trades on the same underlying or option
        candidate_und = signal.get("underlying") or extract_underlying(signal.get("symbol", ""))
        open_positions = self.ledger.open_trades()

        if not ALLOW_DUPLICATE_SYMBOLS:
            for op in open_positions:
                op_und = op.get("underlying") or extract_underlying(op.get("symbol", ""))
                if candidate_und == op_und or signal["symbol"] == op.get("symbol"):
                    return (f"symbol_already_open (active trade #{op.get('id')} already open on "
                            f"{candidate_und} [{op.get('symbol')}])")

        # Re-entry cooldown & loss lockout: protect against whipsaw & revenge trading
        closed_today = getattr(self.ledger, "closed_trades_today", lambda: [])()
        symbol_closed = [
            ct for ct in closed_today
            if candidate_und == (ct.get("underlying") or extract_underlying(ct.get("symbol", "")))
            or signal["symbol"] == ct.get("symbol")
        ]
        if symbol_closed:
            # 1. Loss count check: prevent repeated churn on same stock
            loss_count = sum(1 for ct in symbol_closed if (ct.get("pnl") or 0) < 0)
            if loss_count >= MAX_LOSSES_PER_SYMBOL_DAY and not signal.get("override_cooldown"):
                return (f"symbol_loss_lockout ({candidate_und} has {loss_count} stop-outs today; "
                        f"locked out to prevent chop/revenge trading)")

            # 2. Re-entry cooldown check
            last_ct = max(symbol_closed, key=lambda c: c.get("exit_ts") or 0)
            exit_ts = last_ct.get("exit_ts")
            if exit_ts:
                elapsed = time.time() - exit_ts
                last_pnl = float(last_ct.get("pnl") or 0.0)
                
                # If last trade on this symbol was a loss, enforce strict loss cooldown
                if last_pnl < 0:
                    required_secs = LOSS_COOLDOWN_MINUTES * 60.0
                    cooldown_type = f"loss cooldown ({LOSS_COOLDOWN_MINUTES}m)"
                else:
                    required_secs = REENTRY_COOLDOWN_MINUTES * 60.0
                    cooldown_type = f"re-entry cooldown ({REENTRY_COOLDOWN_MINUTES}m)"

                if not signal.get("override_cooldown") and elapsed < required_secs:
                    rem_sec = int(required_secs - elapsed)
                    rem_min = int(rem_sec / 60) + 1
                    return (f"reentry_cooldown_active ({candidate_und} trade #{last_ct.get('id')} "
                            f"closed {int(elapsed/60)}m ago with P&L ₹{last_pnl:.2f}; "
                            f"{cooldown_type} active, wait {rem_min}m)")

        if len(open_positions) >= MAX_OPEN_POSITIONS:
            return f"max_open_positions ({len(open_positions)}/{MAX_OPEN_POSITIONS})"

        position_value = signal["qty"] * signal["price"]
        existing = self.ledger.open_position_value(signal["symbol"])
        if is_option:
            from trading.config import (
                MAX_OPTION_POSITION_VALUE,
                MAX_OPTION_RISK_PER_TRADE,
                MIN_OPTION_STOP_LOSS_PCT,
                ONLY_INDEX_OPTIONS,
            )
            if ONLY_INDEX_OPTIONS:
                is_index = any(idx in sym for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"))
                if not is_index:
                    return f"stock_options_disabled (Option A: only index options permitted: NIFTY, BANKNIFTY, SENSEX)"

            cap = MAX_OPTION_POSITION_VALUE
            if existing + position_value > cap:
                return f"max_option_position_value (cap={cap:.0f}, would_be={existing + position_value:.0f})"
            if MAX_OPTION_RISK_PER_TRADE is not None and MAX_OPTION_RISK_PER_TRADE > 0:
                trade_risk = abs(price - stop_loss) * int(signal.get("qty", 1))
                if trade_risk > MAX_OPTION_RISK_PER_TRADE + 1.0:
                    return (f"max_option_risk_exceeded (trade risk ₹{trade_risk:.2f} exceeds cap "
                            f"₹{MAX_OPTION_RISK_PER_TRADE:.2f})")

            # Contract lot-size validation (specifically for index options e.g. NIFTY 65/75, BANKNIFTY 30, SENSEX 20)
            if any(idx in sym for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")):
                from trading.options_engine import get_lot_size
                lot_size = get_lot_size(sym)
                sig_qty = int(signal.get("qty", 1))
                if lot_size > 1 and (sig_qty < lot_size or sig_qty % lot_size != 0):
                    return (f"invalid_lot_size (symbol {sym} requires quantity to be a multiple of "
                            f"exchange lot size {lot_size}, got {sig_qty})")
        else:
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
        ltp = None
        if self.get_ltp:
            try:
                ltp = self.get_ltp(signal["symbol"])
            except Exception:
                pass
        if not ltp or ltp <= 0:
            try:
                from trading.fno.fyers import FyersClient
                fyers = FyersClient()
                sym = signal["symbol"]
                if "CE" in sym or "PE" in sym or "INDEX" in sym:
                    fyers_sym = sym if sym.startswith("NSE:") else f"NSE:{sym}"
                else:
                    fyers_sym = f"NSE:{sym}-EQ" if not sym.startswith("NSE:") else sym
                q = fyers.quotes([fyers_sym])
                if q and len(q) > 0 and "v" in q[0] and "lp" in q[0]["v"]:
                    ltp = float(q[0]["v"]["lp"])
            except Exception:
                pass
        if not ltp or ltp <= 0:
            ltp = signal["price"]

        slip = SLIPPAGE_PCT * ltp
        return ltp + slip if signal["side"] == "BUY" else ltp - slip

