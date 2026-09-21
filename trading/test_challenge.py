"""Tests for the 15k to 1 Lakh Survival & Compounding Challenge."""
import tempfile
from pathlib import Path
from trading import challenge
from trading.execution_router import ExecutionRouter
from trading.ledger import Ledger


def test_challenge_initial_state():
    state = challenge.recalculate_state()
    assert state["initial_capital"] == 15000.0
    assert state["target_capital"] == 100000.0
    assert state["phase"] == "Phase 1: Survival & Shield"
    assert state["risk_pct"] == 1.5
    assert state["risk_per_trade"] == 225.0
    assert state["daily_loss_limit"] == -525.0
    assert state["max_open_positions"] == 2
    assert state["survival_health"] >= 90.0
    print("[PASS] test_challenge_initial_state passed")


def test_can_enter_trade_circuit_breaker():
    # Ensure fresh state
    state = challenge.load_raw_state()
    state["circuit_breaker_active"] = False
    state["circuit_breaker_reason"] = None
    challenge.save_state(state)
    challenge.recalculate_state()

    # When circuit breaker is not active
    can_enter, reason = challenge.can_enter_trade()
    assert can_enter is True

    # Temporarily set circuit breaker active
    state = challenge.load_raw_state()
    state["circuit_breaker_active"] = True
    state["circuit_breaker_reason"] = "2 losses hit"
    challenge.save_state(state)

    can_enter, reason = challenge.can_enter_trade()
    assert can_enter is False
    assert "2 losses hit" in str(reason)

    # Restore state
    state = challenge.load_raw_state()
    state["circuit_breaker_active"] = False
    state["circuit_breaker_reason"] = None
    challenge.save_state(state)
    challenge.recalculate_state()
    print("[PASS] test_can_enter_trade_circuit_breaker passed")


def test_execution_router_integration():
    router = ExecutionRouter(mode="paper")
    signal = {
        "symbol": "TCS",
        "side": "BUY",
        "qty": 1,
        "price": 3500.0,
    }
    # If open positions exceed Phase 1 max (2), router correctly halts new position
    check = router.risk_check(signal)
    assert check is None or "max_open_positions" in check
    print(f"[PASS] test_execution_router_integration passed (Risk check: {check})")


if __name__ == "__main__":
    test_challenge_initial_state()
    test_can_enter_trade_circuit_breaker()
    test_execution_router_integration()
    print("\nAll challenge unit tests passed successfully!")
