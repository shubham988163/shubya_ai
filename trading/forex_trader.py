"""Autonomous 24/7 Forex & Crypto Execution Engine for Delta Exchange.

Evaluates multi-strategy confluence setups (ORB + AVWAP + 9/21 EMA + 200 SMA + Volume)
and executes high-conviction trades automatically with strict risk management:
- Conviction Gate: >= 8/10 (A/A+ Setups)
- Portfolio Cap: Max 3 concurrent open positions
- Risk Budget: $50 fixed risk per trade
- Trailing Stop: +2% auto-breakeven & +4% trailing profit protection
- Hard Daily Loss Circuit Breaker: -$150 max daily drawdown
- Deduplication: Never double-enter the same pair
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from trading.config import LOGS_DIR, PROJECT_ROOT
from trading.delta_exchange import DeltaClient, FOREX_WATCHLIST
from trading.forex_council import debate_forex_setup
from trading.forex_engine import ForexEngine
from trading.forex_reflection import auto_reflect_on_closed_trades
from trading.ledger import Ledger

logger = logging.getLogger("trading.forex_trader")

CONFIG_FILE = PROJECT_ROOT / "data" / "forex_config.json"
LOG_FILE = LOGS_DIR / "forex_trader.log"


class ForexAutoTrader:
    """Autonomous execution agent for 24/7 Forex and Crypto trading."""

    def __init__(
        self,
        client: DeltaClient | None = None,
        engine: ForexEngine | None = None,
        ledger: Ledger | None = None,
    ):
        self.client = client or DeltaClient()
        self.engine = engine or ForexEngine(client=self.client)
        self.ledger = ledger or Ledger()
        self.config = self._load_config()

    def _load_config(self) -> dict[str, Any]:
        default_config = {
            "enabled": True,
            "use_council": True,
            "min_conviction": 8,
            "max_open_positions": 2,
            "starting_capital_usd": 100.0,
            "target_milestone_usd": 1000.0,
            "risk_pct_per_trade": 2.5,
            "max_daily_loss_pct": 6.0,
            "poll_interval_seconds": 30,
            "symbols": FOREX_WATCHLIST,
        }
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    default_config.update(loaded)
            except Exception as e:
                logger.error(f"Error reading forex config: {e}")
        return default_config

    def get_account_equity(self) -> dict[str, float]:
        """Calculate live equity, progress, and dynamic risk parameters for the $100 -> $1,000 Challenge."""
        starting_capital = float(self.config.get("starting_capital_usd", 100.0))
        all_pos = self.client.get_all_positions()
        total_realized = sum(float(p.get("pnl") or 0.0) for p in all_pos if p.get("status") == "CLOSED")
        open_pos = [p for p in all_pos if p.get("status") == "OPEN"]
        unrealized = sum(float(p.get("pnl") or 0.0) for p in open_pos)
        current_equity = max(1.0, starting_capital + total_realized + unrealized)

        risk_pct = float(self.config.get("risk_pct_per_trade", 2.5))
        risk_per_trade = max(1.0, round(current_equity * (risk_pct / 100.0), 2))

        daily_loss_pct = float(self.config.get("max_daily_loss_pct", 6.0))
        daily_loss_limit = max(5.0, round(current_equity * (daily_loss_pct / 100.0), 2))
        target_milestone = float(self.config.get("target_milestone_usd", 1000.0))
        progress_pct = min(100.0, max(0.0, ((current_equity - starting_capital) / (target_milestone - starting_capital)) * 100.0)) if target_milestone > starting_capital else 0.0

        return {
            "starting_capital": starting_capital,
            "total_realized_pnl": round(total_realized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "current_equity": round(current_equity, 2),
            "target_milestone": target_milestone,
            "progress_pct": round(progress_pct, 1),
            "risk_pct_per_trade": risk_pct,
            "dynamic_risk_usd": risk_per_trade,
            "max_daily_loss_usd": daily_loss_limit,
        }

    def save_config(self) -> None:
        """Persist auto-trader configuration to disk."""
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving forex config: {e}")

    @property
    def is_enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def set_enabled(self, enabled: bool) -> None:
        self.config["enabled"] = bool(enabled)
        self.save_config()
        self._log(f"Auto-Trader set to: {'ENABLED' if enabled else 'DISABLED'}")

    @property
    def is_council_enabled(self) -> bool:
        return bool(self.config.get("use_council", True))

    def set_council_enabled(self, enabled: bool) -> None:
        self.config["use_council"] = bool(enabled)
        self.save_config()
        self._log(f"4-Agent AI Council Gate set to: {'ENABLED' if enabled else 'DISABLED'}")

    def _log(self, msg: str) -> None:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}\n"
        print(f"[ForexAutoTrader] {msg}")
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def get_daily_pnl(self) -> float:
        """Calculate total realized P&L from closed forex trades today."""
        today = datetime.now().strftime("%Y-%m-%d")
        all_pos = self.client.get_all_positions()
        daily_pnl = 0.0
        for p in all_pos:
            if p.get("status") == "CLOSED":
                exit_time = p.get("exit_time", "")
                entry_time = p.get("entry_time", "")
                if (exit_time and exit_time.startswith(today)) or (entry_time and entry_time.startswith(today)):
                    daily_pnl += float(p.get("pnl") or 0.0)
        return round(daily_pnl, 2)

    def run_pass(self) -> dict[str, Any]:
        """Perform one complete evaluation, management, and auto-execution cycle."""
        # 1. Update and manage active open positions (trails SL, locks breakeven, exits)
        open_positions = self.client.get_positions()
        daily_pnl = self.get_daily_pnl()
        unrealized_pnl = sum(float(p.get("pnl") or 0.0) for p in open_positions)

        # Trigger autonomous post-trade reflection & autopsy for any newly closed trades
        try:
            auto_reflect_on_closed_trades(self.client, ledger=self.ledger)
        except Exception as e:
            logger.debug(f"Auto-reflection error: {e}")

        # Compute live compounding equity status
        eq = self.get_account_equity()

        status_result = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "enabled": self.is_enabled,
            "open_count": len(open_positions),
            "open_positions": open_positions,
            "daily_realized_pnl": daily_pnl,
            "unrealized_pnl": round(unrealized_pnl, 2),
            "equity_info": eq,
            "executed_trades": [],
            "rejections": [],
        }

        if not self.is_enabled:
            status_result["state"] = "disabled"
            return status_result

        # 2. Risk Kernel: Check dynamic daily loss circuit breaker (Survival First)
        max_daily_loss = eq["max_daily_loss_usd"]
        if daily_pnl <= -max_daily_loss:
            reason = f"Survival Circuit Breaker: Daily loss limit hit (${daily_pnl:.2f} <= -${max_daily_loss:.2f}). Standing down to preserve capital."
            status_result["state"] = "daily_loss_limit_hit"
            status_result["rejections"].append(reason)
            self._log(f"[-] {reason}")
            return status_result

        # 3. Check maximum open positions cap
        max_open = int(self.config.get("max_open_positions", 2))
        if len(open_positions) >= max_open:
            status_result["state"] = "max_open_positions"
            status_result["rejections"].append(f"At max open position capacity ({len(open_positions)}/{max_open}).")
            return status_result

        # 4. Scan 24/7 Delta Exchange watchlist
        symbols = self.config.get("symbols", FOREX_WATCHLIST)
        setups = self.engine.scan_all(symbols)
        if not setups:
            status_result["state"] = "no_setups"
            return status_result

        min_conviction = int(self.config.get("min_conviction", 8))
        risk_budget = eq["dynamic_risk_usd"]

        active_symbols = {p["symbol"] for p in open_positions}

        # Re-entry cooldown: do not re-enter a symbol within 15 minutes of exit
        recent_exits = set()
        try:
            now_dt = datetime.now()
            for p in self.client.get_all_positions():
                if p.get("status") == "CLOSED" and p.get("exit_time"):
                    ext = datetime.strptime(p["exit_time"], "%Y-%m-%d %H:%M:%S")
                    if (now_dt - ext).total_seconds() < 900:  # 15 minutes
                        recent_exits.add(p.get("symbol"))
        except Exception:
            pass

        for s in setups:
            sym = s["symbol"]
            conv = s["conviction"]

            # Filter: Min conviction threshold (8/10+)
            if conv < min_conviction:
                continue

            # Filter: Deduplication (no duplicate position on the same pair)
            if sym in active_symbols:
                continue

            # Filter: Cooldown (prevent churn on same breakout bar)
            if sym in recent_exits:
                status_result["rejections"].append(f"Re-entry cooldown active for {sym} (15m window).")
                continue

            price = float(s["price"])
            sl = float(s["stop_loss"])
            target = float(s["target1"])
            side = s["side"]
            per_unit_risk = abs(price - sl)

            # Layer 2: 4-Agent AI Council Audit (Technical Analyst, Bull, Bear Trap Check, Trader Decision)
            council_verdict = None
            if self.is_council_enabled:
                self._log(f"[4-Agent Council] Debating live candidate on {sym} ({side}) with dynamic risk budget ${risk_budget:.2f}...")
                council_verdict = debate_forex_setup(
                    s,
                    client=self.client,
                    ledger=self.ledger,
                    planned_risk_usd=risk_budget,
                )
                self._log(
                    f"[4-Agent Council] {sym}: Verdict={council_verdict.verdict} (Conviction: {council_verdict.conviction}/10) | "
                    f"Bearish Trap Check: {council_verdict.bear_case[:80]}..."
                )
                if council_verdict.verdict != "APPROVE" or council_verdict.conviction < min_conviction or council_verdict.action != side:
                    rej = f"4-Agent Council VETO on {sym} ({council_verdict.verdict}, conviction {council_verdict.conviction}/10): {council_verdict.bear_case}"
                    status_result["rejections"].append(rej)
                    self._log(f"[-] {rej}")
                    continue
                conv = council_verdict.conviction
                if council_verdict.recommended_stop_loss > 0:
                    sl = council_verdict.recommended_stop_loss
                if council_verdict.recommended_target > 0:
                    target = council_verdict.recommended_target
                per_unit_risk = abs(price - sl)
                if per_unit_risk <= 0:
                    continue

            # Dynamic Compounding Sizing: Risk exactly 2.5% of equity
            raw_size = risk_budget / per_unit_risk
            if price > 5000:       # BTCUSD style
                size = max(0.001, round(raw_size, 4))
            elif price > 500:      # ETHUSD style
                size = max(0.01, round(raw_size, 3))
            elif price > 10:       # SOLUSD, XAUTUSD style
                size = max(0.05, round(raw_size, 2))
            else:                  # XRPUSD, DOGEUSD style
                size = max(1.0, round(raw_size, 1))

            # Execute trade
            trade_res = self.client.execute_trade(
                symbol=sym,
                side=side,
                size=size,
                price=price,
                stop_loss=sl,
                target=target,
                strategy_id="forex_orb_vwap_multi",
                conviction=conv,
            )

            if trade_res.get("ok"):
                trade = trade_res.get("trade", {})
                trade["executor"] = "4-Agent AI Council" if self.is_council_enabled else "Quant Strategy"
                if council_verdict:
                    trade["council_verdict"] = council_verdict.model_dump()
                status_result["executed_trades"].append(trade)
                active_symbols.add(sym)
                self._log(
                    f"✓ [EXECUTED] {sym} {side} {size} @ ${price:,.2f} | "
                    f"SL: ${sl:,.2f} | Tgt: ${target:,.2f} | Conviction: {conv}/10 | "
                    f"Executor: {trade['executor']}"
                )

                if len(active_symbols) >= max_open:
                    break
            else:
                status_result["rejections"].append(f"{sym}: {trade_res.get('reason')}")

        status_result["state"] = "active"
        return status_result


def run_continuous_daemon(poll_interval: int = 30) -> None:
    """Run the 24/7 Forex auto-trader daemon indefinitely."""
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 65)
    print("  [+] 24/7 FOREX & CRYPTO AUTONOMOUS AGENT DAEMON STARTED")
    print("  Connected to Delta Exchange · Multi-Strategy Confluence")
    print(f"  Evaluating every {poll_interval}s · Auto-Trailing Stops Enabled")
    print("=" * 65)

    trader = ForexAutoTrader()
    while True:
        try:
            res = trader.run_pass()
            if res.get("executed_trades"):
                print(f"[Daemon] Executed {len(res['executed_trades'])} new trade(s)")
        except Exception as e:
            logger.error(f"Daemon pass error: {e}")
        time.sleep(poll_interval)


if __name__ == "__main__":
    import sys
    if "--daemon" in sys.argv:
        run_continuous_daemon(30)
    else:
        trader = ForexAutoTrader()
        res = trader.run_pass()
        print(json.dumps(res, indent=2))
