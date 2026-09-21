"""Autonomous Survival & Compounding Agent for the 15k to 1 Lakh Challenge.

Role:
  - Continuously evaluates capital survival, drawdowns, and compounding progression.
  - Performs AI risk and regime assessment to issue real-time "Tactical Directives".
  - Enforces anti-ruin circuit breakers (stops trading if risk parameters are breached).
  - Sends milestone and defense alerts to Telegram.

Usage:
  python -m trading.agents.survival_agent          # run once
  python -m trading.agents.survival_agent --loop   # run continuously
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo
from pydantic import BaseModel, Field

from trading.challenge import (
    get_challenge_state,
    recalculate_state,
    save_state,
    IST,
)
from trading.execution_router import load_day_config
from trading.ledger import Ledger
from trading.agents.llm import call_structured

SYSTEM = (
    "You are the Chief Capital Preservation & Survival Agent for a disciplined retail "
    "trader attempting the '15k to 1 Lakh Challenge' in Indian equities & F&O. "
    "Your primary duty is SURVIVAL: prevent ruin, stop revenge trading, and compound "
    "profits methodically. A trader who loses their capital is out of the game. "
    "Advise strictly based on the current equity, drawdown, win streaks, and today's market regime."
)


class SurvivalDirective(BaseModel):
    posture: Literal["DEFENSIVE", "SELECTIVE", "COMPOUNDING", "AGGRESSIVE", "HALTED"]
    conviction_score: float = Field(description="Confidence in current market posture, 0.0 to 1.0")
    directive: str = Field(description="Actionable 1-2 sentence tactical advice for today's market")
    key_focus: str = Field(description="What specific setup or behavior to look for right now")
    risk_action: str = Field(description="Guidance on risk sizing (e.g. '1.5% standard', 'Cut to 1.0%', 'Halt')")


def generate_directive(state: dict, ledger: Ledger | None = None) -> SurvivalDirective:
    ledger = ledger or Ledger()
    day_config = load_day_config()
    recent = ledger.recent_closed_trades(5)
    recent_summary = [
        {"symbol": t["symbol"], "side": t["side"], "pnl": round(t["pnl"] or 0, 2)}
        for t in recent
    ]

    prompt = f"""CHALLENGE STATUS:
- Initial Capital: INR {state['initial_capital']:,.2f}
- Current Equity: INR {state['current_equity']:,.2f} (ROI: {state['roi_pct']}%, Multiple: {state['multiple']}x)
- Target: INR {state['target_capital']:,.2f} (Progress: {state['progress_pct']}%)
- Phase: {state['phase']}
- Drawdown from Peak: {state['drawdown_pct']}%
- Survival Health Index: {state['survival_health']}/100 ({state['survival_status']})
- Circuit Breaker Active: {state['circuit_breaker_active']} ({state.get('circuit_breaker_reason') or 'None'})
- Today's P&L: INR {state['today_pnl']:,.2f}
- Today's Losses: {state['today_losses_count']}
- Current Risk/Trade: INR {state['risk_per_trade']:,.2f} ({state['risk_pct']}%)

MARKET CONTEXT:
- Today's Regime: {day_config.get('regime', 'unknown')}
- Pre-market Rationale: {day_config.get('rationale', 'none')}
- Recent Closed Trades: {json.dumps(recent_summary)}

Issue your authoritative Survival Directive for the current session."""

    fallback_posture = "HALTED" if state["circuit_breaker_active"] else (
        "DEFENSIVE" if state["defense_mode"] else "SELECTIVE"
    )
    fallback = SurvivalDirective(
        posture=fallback_posture,
        conviction_score=0.85,
        directive="Preserve starting capital. Prioritize grade-A setups with minimum 1:2 risk/reward ratio.",
        key_focus="Strict stop loss adherence and no chasing high-volatility momentum spikes.",
        risk_action=f"Risk INR {state['risk_per_trade']:.0f} per trade",
    )

    return call_structured("survival_agent", SYSTEM, prompt, SurvivalDirective, fallback, ledger=ledger)


def run_cycle(ledger: Ledger | None = None, notify_milestones: bool = True) -> dict:
    ledger = ledger or Ledger()
    state = recalculate_state()

    directive = generate_directive(state, ledger)

    # Save to state
    state["ai_directive"] = f"[{directive.posture}] {directive.directive}"
    state["ai_directive_detail"] = {
        "posture": directive.posture,
        "conviction": directive.conviction_score,
        "key_focus": directive.key_focus,
        "risk_action": directive.risk_action,
    }
    state["ai_directive_time"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)

    print(f"[{datetime.now(IST).strftime('%H:%M:%S')}] Survival Agent [{directive.posture}]: {directive.directive}")
    print(f"  Focus: {directive.key_focus} | Risk: {directive.risk_action}")

    # Check for milestone or circuit breaker Telegram notifications
    if notify_milestones:
        try:
            from trading.notify import notify

            if state.get("circuit_breaker_active") and not state.get("_circuit_notified_today"):
                notify(
                    "🛑 Challenge Circuit Breaker Triggered",
                    f"{state.get('circuit_breaker_reason')}\n"
                    f"Current Equity: INR {state['current_equity']:,.2f}\n"
                    f"Capital preservation mode engaged for the rest of today."
                )
                state["_circuit_notified_today"] = True
                save_state(state)

            # Check if milestone newly unlocked
            for m in state.get("milestones", []):
                if m.get("reached") and not m.get("_telegram_sent"):
                    notify(
                        f"🎯 Milestone Achieved: {m['name']}!",
                        f"Current Equity: INR {state['current_equity']:,.2f}\n"
                        f"Progress towards INR 1 Lakh: {state['progress_pct']}%\n"
                        f"Health Score: {state['survival_health']}%"
                    )
                    m["_telegram_sent"] = True
                    save_state(state)
        except Exception as e:
            print(f"[survival notify error] {e}")

    return state


def run_loop(poll_seconds: int = 60) -> None:
    ledger = Ledger()
    print("Survival & Compounding Agent loop started...")
    while True:
        try:
            run_cycle(ledger)
            time.sleep(poll_seconds)
        except KeyboardInterrupt:
            print("Stopping Survival Agent loop.")
            break
        except Exception as e:
            print(f"[survival agent loop error] {e}")
            time.sleep(15)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        run_loop()
    else:
        run_cycle()
