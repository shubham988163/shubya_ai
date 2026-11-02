"""Autonomous Post-Trade Autopsy & Continuous Learning Engine for Forex & Crypto.

Every closed trade (especially losses) undergoes a rigorous post-mortem review:
1. Root-cause analysis (e.g. false breakout, tight stop, counter-trend, liquidity grab).
2. Actionable lesson learned to avoid repeating the mistake.
3. Rule adjustment recommendation.
4. Lessons are dynamically injected into future 4-Agent Council deliberations.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from trading.agents.llm import call_structured
from trading.config import PROJECT_ROOT
from trading.delta_exchange import DeltaClient
from trading.ledger import Ledger

logger = logging.getLogger("trading.forex_reflection")

REFLECTIONS_FILE = PROJECT_ROOT / "data" / "forex_reflections.json"


class TradeReflection(BaseModel):
    trade_id: str
    symbol: str
    side: str
    pnl: float
    pnl_pct: float
    outcome: Literal["WIN", "LOSS", "BREAKEVEN"]
    root_cause: str = Field(description="Primary driver of why the trade won, lost, or broke even")
    lesson_learned: str = Field(description="Concise, actionable lesson to avoid repeating losses or replicate wins")
    rule_recommendation: str = Field(description="Specific filter or parameter adjustment for future setups")
    exit_reason: str = Field(description="Reason for exit recorded by execution engine")
    timestamp: str = Field(description="Reflection analysis timestamp")


AUTOPSY_SYSTEM_PROMPT = """You are the Senior Trading Risk Auditor and Post-Trade Autopsy Specialist.
Your sole job is continuous improvement and risk mitigation:
- You analyze completed trades on Delta Exchange (Forex & Crypto).
- When a trade is a LOSS, you brutally dissect what went wrong: Was it a false breakout wick? Counter-trend against 200 SMA? Entry during low-liquidity chop? Stop loss placed inside normal ATR noise?
- When a trade is a WIN or BREAKEVEN, you identify the exact factor that protected capital or drove profitability.
- You provide a CONCISE, ACTIONABLE LESSON that the 4-Agent Council will remember before taking future trades.
- No sugarcoating. Focus on capital survival and asymmetric compounding."""


def load_reflections() -> list[dict[str, Any]]:
    """Load all historical trade reflections from disk."""
    if not REFLECTIONS_FILE.exists():
        return []
    try:
        with open(REFLECTIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading reflections: {e}")
        return []


def save_reflections(reflections: list[dict[str, Any]]) -> None:
    """Persist reflections to disk."""
    REFLECTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(REFLECTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(reflections, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving reflections: {e}")


def get_recent_lessons(limit: int = 5) -> list[str]:
    """Retrieve the most recent actionable lessons to feed into Council deliberation."""
    refs = load_reflections()
    if not refs:
        return [
            "Maintain strict 1:2 R:R; never chase extended candles.",
            "Respect 200 SMA and VWAP alignment as primary trend filters.",
            "Lock breakeven at +1.0R to eliminate risk on maturing trades.",
        ]
    recent = refs[-limit:]
    lessons = []
    for r in reversed(recent):
        tag = "🔴 LOSS" if r.get("outcome") == "LOSS" else ("🟢 WIN" if r.get("outcome") == "WIN" else "⚪ BE")
        sym = r.get("symbol", "")
        lesson = r.get("lesson_learned", "")
        if lesson:
            lessons.append(f"{tag} on {sym}: {lesson}")
    return lessons


def reflect_on_trade(trade: dict[str, Any], ledger: Ledger | None = None) -> TradeReflection:
    """Perform an AI post-mortem autopsy on a completed trade."""
    tid = str(trade.get("id", ""))
    sym = trade.get("symbol", "")
    side = trade.get("side", "")
    entry = float(trade.get("entry_price", 0.0))
    exit_p = float(trade.get("exit_price", entry))
    sl = float(trade.get("initial_stop", trade.get("stop_loss", entry)))
    target = float(trade.get("target", entry))
    pnl = float(trade.get("pnl", 0.0))
    pnl_pct = float(trade.get("pnl_pct", 0.0))
    exit_reason = trade.get("exit_reason", "Closed")
    trailing_note = trade.get("trailing_note", "None")
    entry_time = trade.get("entry_time", "")
    exit_time = trade.get("exit_time", "")

    if pnl > 0.01:
        outcome = "WIN"
    elif pnl < -0.01:
        outcome = "LOSS"
    else:
        outcome = "BREAKEVEN"

    prompt = f"""PERFORM POST-TRADE AUTOPSY:
Trade ID: {tid}
Asset: {sym}
Direction: {side}
Entry Price: ${entry:,.4f} ({entry_time})
Exit Price: ${exit_p:,.4f} ({exit_time})
Initial Stop Loss: ${sl:,.4f}
Target: ${target:,.4f}
Exit Reason: {exit_reason}
Trailing Action: {trailing_note}
Realized P&L: ${pnl:+,.2f} ({pnl_pct:+.2f}%)
Outcome: {outcome}

Explain the root cause, identify the actionable lesson learned, and propose a rule recommendation to keep future trading sharp."""

    # Deterministic fallback in case of API failure
    if outcome == "LOSS":
        fallback = TradeReflection(
            trade_id=tid,
            symbol=sym,
            side=side,
            pnl=pnl,
            pnl_pct=pnl_pct,
            outcome="LOSS",
            root_cause=f"Adverse market excursion triggered stop-loss at ${sl:,.4f}. Exit: {exit_reason}.",
            lesson_learned=f"Ensure entry on {sym} has confirmed volume expansion and wait for retest before committing capital.",
            rule_recommendation="Require RVOL >= 1.2 and avoid entering near major swing resistance.",
            exit_reason=exit_reason,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
    elif outcome == "WIN":
        fallback = TradeReflection(
            trade_id=tid,
            symbol=sym,
            side=side,
            pnl=pnl,
            pnl_pct=pnl_pct,
            outcome="WIN",
            root_cause=f"Directional expansion reached favorable territory ({exit_reason}). Trailing/target executed properly.",
            lesson_learned="Letting trailing stops advance behind structural levels captured trend upside while protecting gains.",
            rule_recommendation="Continue using dynamic 1.0R trailing once +1.5R target threshold is achieved.",
            exit_reason=exit_reason,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
    else:
        fallback = TradeReflection(
            trade_id=tid,
            symbol=sym,
            side=side,
            pnl=pnl,
            pnl_pct=pnl_pct,
            outcome="BREAKEVEN",
            root_cause="Breakeven defense was armed at +1.0R, shielding capital when momentum reversed.",
            lesson_learned="Automated breakeven shift saved capital from turning a winning run into a loss.",
            rule_recommendation="Maintain +1.0R breakeven lock as a fundamental survival rule.",
            exit_reason=exit_reason,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    try:
        reflection = call_structured(
            agent_name="forex_reflection",
            system=AUTOPSY_SYSTEM_PROMPT,
            prompt=prompt,
            output_model=TradeReflection,
            fallback=fallback,
            ledger=ledger,
        )
        return reflection
    except Exception as e:
        logger.warning(f"Trade reflection LLM failed for {sym} #{tid}: {e}")
        return fallback


def auto_reflect_on_closed_trades(client: DeltaClient, ledger: Ledger | None = None) -> list[dict[str, Any]]:
    """Scan all closed positions and generate autopsies for any that have not yet been analyzed."""
    all_pos = client.get_all_positions()
    closed = [p for p in all_pos if p.get("status") == "CLOSED"]
    existing_refs = load_reflections()
    reflected_ids = {str(r.get("trade_id")) for r in existing_refs}

    new_reflections = []
    for p in closed:
        tid = str(p.get("id"))
        if tid not in reflected_ids:
            logger.info(f"Running post-trade autopsy for closed trade #{tid} ({p.get('symbol')})...")
            ref = reflect_on_trade(p, ledger=ledger)
            ref_dict = ref.model_dump()
            existing_refs.append(ref_dict)
            reflected_ids.add(tid)
            new_reflections.append(ref_dict)

    if new_reflections:
        save_reflections(existing_refs)

    return existing_refs
