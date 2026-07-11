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


def review_trade(trade: dict, ledger: Ledger) -> None:
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
        # LLM unavailable (rate limit / network). Leave the trade unreviewed so
        # a later poll retries it once quota recovers, instead of permanently
        # stamping a meaningless verdict.
        print(f"trade #{trade['id']} {trade['symbol']} -> deferred (agent unavailable)")
        return False
    confidence = max(0.0, min(1.0, v.confidence))
    ledger.save_agent_verdict(trade["id"], v.verdict, confidence, v.reasons)
    print(f"trade #{trade['id']} {trade['symbol']} -> {v.verdict} "
          f"({confidence:.2f}): {'; '.join(v.reasons)}")
    if v.verdict in ("caution", "veto"):
        from trading.notify import notify
        icon = "⛔" if v.verdict == "veto" else "⚠️"
        notify(f"{icon} Supervisor: {v.verdict.upper()} trade #{trade['id']} {trade['symbol']}",
               "; ".join(v.reasons)[:180])
    return True


def run(loop: bool = False) -> None:
    ledger = Ledger()
    backoff = SUPERVISOR_POLL_SECONDS
    while True:
        pending = ledger.unreviewed_trades()
        deferred = False
        for trade in pending:
            if review_trade(trade, ledger) is False:
                deferred = True
                break  # quota exhausted — no point hammering the API
        if not loop:
            break
        # Back off up to 5 min while the LLM is rate-limited; reset on success.
        backoff = min(backoff * 2, 300) if deferred else SUPERVISOR_POLL_SECONDS
        time.sleep(backoff)


if __name__ == "__main__":
    run(loop="--loop" in sys.argv)
