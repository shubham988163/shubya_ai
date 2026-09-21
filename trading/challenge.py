"""15k to 1 Lakh (₹15,000 → ₹1,00,000) Survival & Compounding Engine.

Core mission:
  Starting capital: ₹15,000 INR
  Target capital:   ₹1,00,000 INR (~6.67x return)

Survival Philosophy:
  1. Anti-Ruin First: Protect principal with asymmetric risk/reward and dynamic position sizing.
  2. Phased Compounding:
       - Phase 1 (Survival & Shield): ₹15k - ₹25k -> 1.5% risk/trade, max 2 positions, tight daily stops.
       - Phase 2 (Compounding Engine): ₹25k - ₹50k -> 2.0% risk/trade, max 3 positions, accelerated scaling.
       - Phase 3 (Wealth Sprint): ₹50k - ₹100k -> 2.0% risk/trade, lock-in defense near ₹90k+.
       - Phase 4 (Rich Guy 🏆): ₹100,000+ reached!
  3. Circuit Breakers:
       - 2 consecutive losses in a single day -> instant day halt to prevent tilt/revenge trading.
       - Drawdown > 10% from peak -> enter DEFENSE_MODE (cut risk by 50%).
       - Capital < ₹12,000 (-20%) -> EMERGENCY_SURVIVAL_MODE (0.75% risk, 1 trade/day).
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trading.config import DB_PATH, PROJECT_ROOT

IST = ZoneInfo("Asia/Kolkata")
CHALLENGE_STATE_PATH = PROJECT_ROOT / "data" / "challenge_state.json"
DEFAULT_STARTING_CAPITAL = 15_000.0
DEFAULT_TARGET_CAPITAL = 100_000.0
DEFAULT_START_DATE = "2026-09-21"


def _now_str() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _today_str() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def load_raw_state() -> dict:
    if CHALLENGE_STATE_PATH.exists():
        try:
            with open(CHALLENGE_STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    # Default initial state
    state = {
        "challenge_name": "15k to 1 Lakh Survival & Compounding Challenge",
        "start_date": DEFAULT_START_DATE,
        "initial_capital": DEFAULT_STARTING_CAPITAL,
        "target_capital": DEFAULT_TARGET_CAPITAL,
        "current_equity": DEFAULT_STARTING_CAPITAL,
        "peak_equity": DEFAULT_STARTING_CAPITAL,
        "drawdown_pct": 0.0,
        "total_pnl": 0.0,
        "roi_pct": 0.0,
        "multiple": 1.0,
        "distance_to_target": DEFAULT_TARGET_CAPITAL - DEFAULT_STARTING_CAPITAL,
        "progress_pct": 0.0,
        "phase": "Phase 1: Survival & Shield",
        "phase_num": 1,
        "risk_pct": 1.5,
        "risk_per_trade": 225.0,
        "daily_loss_limit": -525.0,
        "max_open_positions": 2,
        "max_position_value": 45_000.0,
        "survival_health": 100.0,
        "survival_status": "OPTIMAL",
        "defense_mode": False,
        "emergency_mode": False,
        "circuit_breaker_active": False,
        "circuit_breaker_reason": None,
        "today_losses_count": 0,
        "today_trades_count": 0,
        "today_pnl": 0.0,
        "total_trades": 0,
        "winning_trades": 0,
        "win_rate": 0.0,
        "last_updated": _now_str(),
        "ai_directive": "Challenge initialized with INR 15,000. Objective: Survival & Steady Compounding. Waiting for prime intraday setups.",
        "ai_directive_time": _now_str(),
        "milestones": [
            {"target": 15000, "name": "Start Line", "reached": True, "date": DEFAULT_START_DATE},
            {"target": 25000, "name": "Phase 1: Capital Cushion", "reached": False, "date": None},
            {"target": 50000, "name": "Phase 2: Half-Centurion", "reached": False, "date": None},
            {"target": 75000, "name": "Phase 3: Wealth Sprint", "reached": False, "date": None},
            {"target": 100000, "name": "Rich Guy Trophy (1 Lakh)", "reached": False, "date": None},
        ],
    }
    save_state(state)
    return state


def save_state(state: dict) -> None:
    CHALLENGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["last_updated"] = _now_str()
    with open(CHALLENGE_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def recalculate_state() -> dict:
    """Recalculates equity, drawdowns, streaks, and parameters from the ledger."""
    state = load_raw_state()
    start_date = state.get("start_date", DEFAULT_START_DATE)
    initial_cap = float(state.get("initial_capital", DEFAULT_STARTING_CAPITAL))
    target_cap = float(state.get("target_capital", DEFAULT_TARGET_CAPITAL))

    # Read trades from database since start_date
    trades = []
    if os.path.exists(DB_PATH):
        try:
            conn = sqlite3.connect(DB_PATH, timeout=5)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trades WHERE date >= ? ORDER BY id ASC", (start_date,)
            ).fetchall()
            trades = [dict(r) for r in rows]
            conn.close()
        except Exception as e:
            print(f"[challenge db error] {e}")

    today = _today_str()
    closed_trades = [t for t in trades if t.get("status") == "closed"]
    today_trades = [t for t in trades if t.get("date") == today]
    today_closed = [t for t in today_trades if t.get("status") == "closed"]

    # Calculate P&L progression
    total_pnl = 0.0
    running_equity = initial_cap
    peak_equity = max(initial_cap, float(state.get("peak_equity", initial_cap)))
    consecutive_losses = 0
    today_consecutive_losses = 0

    wins = 0
    for t in closed_trades:
        pnl = float(t.get("pnl") or 0.0)
        total_pnl += pnl
        running_equity += pnl
        if running_equity > peak_equity:
            peak_equity = running_equity

        if pnl > 0:
            wins += 1
            consecutive_losses = 0
        else:
            consecutive_losses += 1

    # Today's stats
    today_pnl = sum(float(t.get("pnl") or 0.0) for t in today_closed)
    today_losses = sum(1 for t in today_closed if (t.get("pnl") or 0.0) < 0)

    # Calculate today's consecutive loss streak at the end of the day
    today_loss_streak = 0
    for t in reversed(today_closed):
        if (t.get("pnl") or 0.0) < 0:
            today_loss_streak += 1
        else:
            break

    current_equity = round(running_equity, 2)
    peak_equity = round(peak_equity, 2)
    total_pnl = round(total_pnl, 2)
    today_pnl = round(today_pnl, 2)

    # Drawdown from peak
    drawdown_pct = 0.0
    if peak_equity > 0:
        dd = (peak_equity - current_equity) / peak_equity * 100.0
        drawdown_pct = max(0.0, round(dd, 2))

    # Determine Phase
    if current_equity < 25_000.0:
        phase = "Phase 1: Survival & Shield"
        phase_num = 1
        base_risk_pct = 1.5
        max_positions = 2
        max_pos_val = min(60_000.0, current_equity * 4.0)
        daily_loss_limit = -round(current_equity * 0.035, 2)
    elif current_equity < 50_000.0:
        phase = "Phase 2: Compounding Engine"
        phase_num = 2
        base_risk_pct = 2.0
        max_positions = 3
        max_pos_val = min(150_000.0, current_equity * 5.0)
        daily_loss_limit = -round(current_equity * 0.04, 2)
    elif current_equity < 100_000.0:
        phase = "Phase 3: Wealth Sprint"
        phase_num = 3
        # If very close to 1 Lakh (> 90k), tighten risk to 1.5% to protect gains
        base_risk_pct = 1.5 if current_equity >= 90_000.0 else 2.0
        max_positions = 3
        max_pos_val = min(300_000.0, current_equity * 5.0)
        daily_loss_limit = -round(current_equity * 0.035, 2)
    else:
        phase = "Phase 4: Rich Guy Trophy (Goal Achieved)"
        phase_num = 4
        base_risk_pct = 1.0
        max_positions = 2
        max_pos_val = 300_000.0
        daily_loss_limit = -round(current_equity * 0.02, 2)

    # Anti-Ruin Defense and Emergency Modes
    defense_mode = False
    emergency_mode = False
    active_risk_pct = base_risk_pct

    if current_equity <= 12_000.0:
        # Emergency Mode (< -20% from initial capital)
        emergency_mode = True
        defense_mode = True
        active_risk_pct = 0.75
        max_positions = 1
        survival_status = "CRITICAL (Emergency Defense)"
    elif drawdown_pct >= 10.0 or current_equity <= 13_500.0:
        # Defense Mode (> 10% drawdown)
        defense_mode = True
        active_risk_pct = round(base_risk_pct * 0.6, 2)
        max_positions = max(1, max_positions - 1)
        survival_status = "DEFENSIVE (Shield Active)"
    elif drawdown_pct >= 6.0:
        survival_status = "CAUTION"
    else:
        survival_status = "OPTIMAL"

    # Dynamic Risk per trade (INR)
    risk_per_trade = round(current_equity * (active_risk_pct / 100.0), 2)
    risk_per_trade = max(100.0, risk_per_trade)

    # Circuit Breakers
    circuit_breaker_active = bool(state.get("circuit_breaker_active", False))
    circuit_breaker_reason = state.get("circuit_breaker_reason")

    # Circuit breaker 1: 2 consecutive losses in one session
    if today_loss_streak >= 2:
        circuit_breaker_active = True
        circuit_breaker_reason = f"2 consecutive losses today ({today_loss_streak} losses in a row) - Day halted to preserve capital."

    # Circuit breaker 2: Daily loss limit hit
    if today_pnl <= daily_loss_limit:
        circuit_breaker_active = True
        circuit_breaker_reason = f"Daily loss limit reached (P&L INR {today_pnl:.2f} <= INR {daily_loss_limit:.2f}) - Trading paused for today."

    # Survival Health Score (0 - 100)
    # Starts at 100. Penalized by drawdown, loss streaks; rewarded by gain multiple & win rate
    health = 100.0
    health -= (drawdown_pct * 3.5)
    health -= (today_loss_streak * 12.0)
    if current_equity < initial_cap:
        health -= ((initial_cap - current_equity) / initial_cap * 60.0)
    else:
        health += min(15.0, (current_equity - initial_cap) / initial_cap * 20.0)

    survival_health = round(max(5.0, min(100.0, health)), 1)

    # Progress towards ₹100,000
    target_gain = target_cap - initial_cap
    current_gain = current_equity - initial_cap
    progress_pct = round(max(0.0, min(100.0, (current_gain / target_gain) * 100.0)), 2)
    multiple = round(current_equity / initial_cap, 2)
    roi_pct = round(((current_equity - initial_cap) / initial_cap) * 100.0, 2)
    distance = max(0.0, round(target_cap - current_equity, 2))

    # Update Milestones
    milestones = state.get("milestones", [])
    for m in milestones:
        if not m.get("reached") and current_equity >= m.get("target", 0):
            m["reached"] = True
            m["date"] = _today_str()

    # Consolidate state
    state.update({
        "current_equity": current_equity,
        "peak_equity": peak_equity,
        "drawdown_pct": drawdown_pct,
        "total_pnl": total_pnl,
        "today_pnl": today_pnl,
        "roi_pct": roi_pct,
        "multiple": multiple,
        "distance_to_target": distance,
        "progress_pct": progress_pct,
        "phase": phase,
        "phase_num": phase_num,
        "risk_pct": active_risk_pct,
        "base_risk_pct": base_risk_pct,
        "risk_per_trade": risk_per_trade,
        "daily_loss_limit": daily_loss_limit,
        "max_open_positions": max_positions,
        "max_position_value": max_pos_val,
        "survival_health": survival_health,
        "survival_status": survival_status,
        "defense_mode": defense_mode,
        "emergency_mode": emergency_mode,
        "circuit_breaker_active": circuit_breaker_active,
        "circuit_breaker_reason": circuit_breaker_reason,
        "today_losses_count": today_losses,
        "today_trades_count": len(today_trades),
        "total_trades": len(closed_trades),
        "winning_trades": wins,
        "win_rate": round(wins / len(closed_trades) * 100, 1) if closed_trades else 0.0,
        "milestones": milestones,
    })

    save_state(state)
    return state


def get_challenge_state() -> dict:
    """Returns the cached or freshly recalculated challenge state."""
    state = load_raw_state()
    # If state hasn't been updated recently, recalculate
    return recalculate_state()


def get_challenge_risk() -> float:
    """Returns the current dynamically allowed risk INR per trade."""
    state = get_challenge_state()
    return float(state.get("risk_per_trade", 225.0))


def can_enter_trade(symbol: str | None = None) -> tuple[bool, str | None]:
    """Safety check for execution router: checks circuit breaker and defense status."""
    state = get_challenge_state()

    if state.get("circuit_breaker_active"):
        return False, state.get("circuit_breaker_reason", "Challenge circuit breaker active")

    if state.get("emergency_mode") and state.get("today_trades_count", 0) >= 1:
        return False, "Emergency mode: strictly 1 trade per day allowed to survive drawdown."

    return True, None


def reset_challenge(initial_capital: float = DEFAULT_STARTING_CAPITAL, start_date: str | None = None) -> dict:
    """Resets the challenge to baseline capital starting today."""
    date = start_date or _today_str()
    state = {
        "challenge_name": "15k to 1 Lakh Survival & Compounding Challenge",
        "start_date": date,
        "initial_capital": initial_capital,
        "target_capital": DEFAULT_TARGET_CAPITAL,
        "current_equity": initial_capital,
        "peak_equity": initial_capital,
        "drawdown_pct": 0.0,
        "total_pnl": 0.0,
        "roi_pct": 0.0,
        "multiple": 1.0,
        "distance_to_target": DEFAULT_TARGET_CAPITAL - initial_capital,
        "progress_pct": 0.0,
        "phase": "Phase 1: Survival & Shield",
        "phase_num": 1,
        "risk_pct": 1.5,
        "risk_per_trade": round(initial_capital * 0.015, 2),
        "daily_loss_limit": -round(initial_capital * 0.035, 2),
        "max_open_positions": 2,
        "max_position_value": 45_000.0,
        "survival_health": 100.0,
        "survival_status": "OPTIMAL",
        "defense_mode": False,
        "emergency_mode": False,
        "circuit_breaker_active": False,
        "circuit_breaker_reason": None,
        "today_losses_count": 0,
        "today_trades_count": 0,
        "today_pnl": 0.0,
        "total_trades": 0,
        "winning_trades": 0,
        "win_rate": 0.0,
        "last_updated": _now_str(),
        "ai_directive": f"Challenge started fresh with ₹{initial_capital:,.2f}. Objective: 1 Lakh. Capital preservation active.",
        "ai_directive_time": _now_str(),
        "milestones": [
            {"target": initial_capital, "name": "Start Line", "reached": True, "date": date},
            {"target": 25000, "name": "Phase 1: Capital Cushion", "reached": False, "date": None},
            {"target": 50000, "name": "Phase 2: Half-Centurion", "reached": False, "date": None},
            {"target": 75000, "name": "Phase 3: Wealth Sprint", "reached": False, "date": None},
            {"target": 100000, "name": "Rich Guy Trophy 🏆", "reached": False, "date": None},
        ],
    }
    save_state(state)
    return recalculate_state()
