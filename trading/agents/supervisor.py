"""Async trade supervisor agent (Role 2) — advisory only, never blocking.

The trade path executes immediately; this process runs alongside it, polling
the ledger for trades that haven't been reviewed, sending each trade's context
to the agent for a sanity check, and writing the verdict back to the same row.

After 4-6 weeks of paper trading you can query the ledger to see whether the
agent's would-be vetoes correlate with losing trades. Only promote it to a
blocking pre-trade check if the data says yes — and even then with a hard
timeout and a default action.

Usage:  python -m trading.agents.supervisor          # run once over backlog
        python -m trading.agents.supervisor --loop   # poll continuously
"""
from __future__ import annotations

import json
import sys
import time
from typing import Literal

from pydantic import BaseModel, Field

from trading.config import SUPERVISOR_POLL_SECONDS
from trading.execution_router import load_day_config
from trading.ledger import Ledger
from trading.agents.llm import call_structured

SYSTEM = (
    "You are a trade supervisor for a personal intraday NSE scalping system. "
    "You review trades that have ALREADY been executed (paper mode). Your verdict "
    "is logged for later analysis and does not affect the trade. Judge whether "
    "the trade was sensible given the day's regime, current P&L, and recent "
    "trade history — look for revenge trading, oversized positions relative to "
    "the day's risk multiplier, trading against the regime, and rule violations."
)


class TradeVerdict(BaseModel):
    verdict: Literal["approve", "caution", "veto"]
    confidence: float = Field(description="0.0 to 1.0")
    reasons: list[str] = Field(description="Short, specific reasons for the verdict")


def heuristic_review(trade: dict, day_config: dict, ledger: Ledger) -> TradeVerdict:
    """Algorithmic heuristic trade review when LLM API is offline or out of credits."""
    reasons = []
    verdict: Literal["approve", "caution", "veto"] = "approve"
    confidence = 0.85

    qty = int(trade.get("qty") or 1)
    px = float(trade.get("entry_price") or 0.0)
    pos_val = qty * px
    regime = str(trade.get("regime") or day_config.get("regime", "choppy")).lower()
    side = str(trade.get("side") or "BUY").upper()
    sl = trade.get("stop_loss")
    tg = trade.get("target")

    # 1. Position size check
    if pos_val > 50_000:
        verdict = "caution"
        reasons.append(f"Position value INR {pos_val:,.0f} exceeds safe small-account allocation cap")
    else:
        reasons.append(f"Position value INR {pos_val:,.0f} sized within capital risk limits")

    # 2. Market Regime Alignment
    if "down" in regime and side == "BUY":
        verdict = "caution"
        reasons.append("Long entry taken counter to prevailing bearish market regime")
    elif "up" in regime and side == "SELL":
        verdict = "caution"
        reasons.append("Short entry taken counter to prevailing bullish market regime")
    elif "choppy" in regime:
        reasons.append("Choppy session: disciplined stop adherence required")
    else:
        reasons.append(f"Trade aligned with {regime} session")

    # 3. Stop loss & Risk/Reward check
    if sl is not None and tg is not None and px > 0:
        risk = abs(px - float(sl))
        reward = abs(float(tg) - px)
        rr = reward / risk if risk > 0 else 0
        if rr < 1.4:
            verdict = "caution"
            reasons.append(f"Sub-optimal risk/reward structure (1:{rr:.1f} < 1:1.5)")
        else:
            reasons.append(f"Favorable structural risk/reward ratio (1:{rr:.1f})")
    elif sl is None:
        verdict = "caution"
        reasons.append("No hard structural stop-loss attached at execution")

    # 4. Revenge trading check
    recent = [t for t in ledger.recent_closed_trades(6) if t["id"] != trade["id"]]
    recent_sym_losses = [
        t for t in recent
        if (t.get("pnl") or 0) < 0 and t.get("symbol") == trade.get("symbol")
    ]
    if len(recent_sym_losses) >= 2:
        verdict = "veto"
        confidence = 0.90
        reasons.insert(0, f"Repeated entry in {trade.get('symbol')} following consecutive losses (revenge trading pattern)")

    if not reasons:
        reasons.append("Trade adheres to intraday discipline and execution rules")

    return TradeVerdict(verdict=verdict, confidence=confidence, reasons=reasons)


def review_trade(trade: dict, ledger: Ledger) -> bool:
    day_config = load_day_config()
    day_pnl = ledger.day_realized_pnl()
    # Exclude the trade under review from its own history — otherwise the
    # agent sees the same trade twice and misreads it as a re-entry pattern.
    recent = [t for t in ledger.recent_closed_trades(6) if t["id"] != trade["id"]][:5]
    recent_summary = [
        {k: t[k] for k in ("symbol", "side", "qty", "entry_price", "exit_price", "pnl")}
        for t in recent
    ]

    prompt = f"""Trade under review:
{json.dumps({k: trade[k] for k in ('symbol', 'side', 'qty', 'entry_price', 'stop_loss', 'target', 'strategy_id', 'regime')}, default=str)}

Today's regime config: {json.dumps(day_config)}
Realized P&L today: {day_pnl:.2f} INR
Previous 5 closed trades (most recent first, NOT including the trade under review): {json.dumps(recent_summary, default=str)}

Give your verdict on this trade."""

    fallback = TradeVerdict(verdict="approve", confidence=0.0,
                            reasons=["fallback: supervisor agent unavailable"])
    v = call_structured("supervisor", SYSTEM, prompt, TradeVerdict, fallback,
                        ledger=ledger)
    if v is fallback:
        # LLM unavailable (credit balance exhausted / offline).
        # Fall back to algorithmic heuristic review so verdicts are always stamped!
        v = heuristic_review(trade, day_config, ledger)

    confidence = max(0.0, min(1.0, v.confidence))
    ledger.save_agent_verdict(trade["id"], v.verdict, confidence, v.reasons)
    print(f"trade #{trade['id']} {trade['symbol']} -> {v.verdict} "
          f"({confidence:.2f}): {'; '.join(v.reasons)}")
    if v.verdict in ("caution", "veto"):
        try:
            from trading.notify import notify
            icon = "⛔" if v.verdict == "veto" else "⚠️"
            notify(f"{icon} Supervisor: {v.verdict.upper()} trade #{trade['id']} {trade['symbol']}",
                   "; ".join(v.reasons)[:180])
        except Exception:
            pass
    return True


def run(loop: bool = False) -> None:
    ledger = Ledger()
    backoff = SUPERVISOR_POLL_SECONDS
    while True:
        pending = ledger.unreviewed_trades(20)
        for trade in pending:
            try:
                review_trade(trade, ledger)
            except Exception as e:
                print(f"[supervisor error] trade #{trade.get('id')}: {e}")
        if not loop:
            break
        time.sleep(SUPERVISOR_POLL_SECONDS)


if __name__ == "__main__":
    run(loop="--loop" in sys.argv)
