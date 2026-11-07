"""Unit tests for position deduplication and re-entry cooldown safety rules."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
import pytest

from trading.config import ALLOW_DUPLICATE_SYMBOLS, REENTRY_COOLDOWN_MINUTES
from trading.execution_router import ExecutionRouter, extract_underlying
from trading.ledger import Ledger


@pytest.fixture
def temp_ledger():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_ledger.db"
        ledger = Ledger(db_path=db_path)
        yield ledger


def test_extract_underlying_formats():
    """Verify underlying symbol normalization for equity and option formats."""
    cases = {
        "NSE:MUTHOOTFIN26NOV2450CE": "MUTHOOTFIN",
        "NSE:CDSL26NOV1260CE": "CDSL",
        "NSE:BANKNIFTY26OCT55700CE": "BANKNIFTY",
        "NSE:NIFTY26O0623150CE": "NIFTY",
        "NSE:RELIANCE26OCT1190CE": "RELIANCE",
        "NSE:CDSL-EQ": "CDSL",
        "CDSL": "CDSL",
        "MUTHOOTFIN": "MUTHOOTFIN",
        "NSE:NIFTY50-INDEX": "NIFTY50",
        "M&M26NOV2900CE": "M&M",
        "NSE:TCS-EQ": "TCS",
        "INFY.NS": "INFY",
    }
    for sym, expected in cases.items():
        assert extract_underlying(sym) == expected


def test_reject_duplicate_active_positions(temp_ledger):
    """Router must reject opening a second trade on the same underlying or option contract."""
    router = ExecutionRouter(mode="paper", ledger=temp_ledger)

    first_sig = {
        "symbol": "NSE:CDSL26NOV1260CE",
        "underlying": "CDSL",
        "side": "BUY",
        "qty": 200,
        "price": 25.0,
        "stop_loss": 23.0,
        "target": 30.0,
        "strategy_id": "fno_scanner_option",
        "is_option": True,
        "ts": time.time(),
    }

    # First trade executes successfully
    trade_id_1 = router.execute(first_sig)
    assert trade_id_1 is not None
    assert trade_id_1 > 0

    # 1. Exact same option symbol -> rejected
    duplicate_sig = dict(first_sig, ts=time.time() + 10)
    err = router.risk_check(duplicate_sig)
    assert err is not None
    assert "symbol_already_open" in err
    assert router.execute(duplicate_sig) is None

    # 2. Different strike on the same underlying -> rejected
    diff_strike_sig = dict(
        first_sig,
        symbol="NSE:CDSL26NOV1280CE",
        ts=time.time() + 20,
    )
    err2 = router.risk_check(diff_strike_sig)
    assert err2 is not None
    assert "symbol_already_open" in err2
    assert router.execute(diff_strike_sig) is None

    # 3. Cash equity on the same underlying -> rejected
    equity_sig = {
        "symbol": "CDSL",
        "side": "BUY",
        "qty": 10,
        "price": 1270.0,
        "stop_loss": 1250.0,
        "target": 1310.0,
        "strategy_id": "fno_scanner",
        "ts": time.time() + 30,
    }
    err3 = router.risk_check(equity_sig)
    assert err3 is not None
    assert "symbol_already_open" in err3
    assert router.execute(equity_sig) is None

    # 4. A completely different underlying (e.g. RELIANCE) -> passes
    diff_stock_sig = {
        "symbol": "NSE:RELIANCE26NOV1300CE",
        "underlying": "RELIANCE",
        "side": "BUY",
        "qty": 250,
        "price": 30.0,
        "stop_loss": 28.50,
        "target": 35.0,
        "strategy_id": "fno_scanner_option",
        "is_option": True,
        "ts": time.time() + 40,
    }
    assert router.risk_check(diff_stock_sig) is None
    trade_id_2 = router.execute(diff_stock_sig)
    assert trade_id_2 is not None
    assert trade_id_2 > trade_id_1


def test_reentry_cooldown_rejection(temp_ledger):
    """Router must enforce cooldown window after a trade closes on a symbol."""
    router = ExecutionRouter(mode="paper", ledger=temp_ledger)

    trade_sig = {
        "symbol": "TCS",
        "underlying": "TCS",
        "side": "BUY",
        "qty": 1,
        "price": 2000.0,
        "stop_loss": 1800.0,
        "target": 2200.0,
        "strategy_id": "fno_scanner",
        "ts": time.time(),
    }

    trade_id = router.execute(trade_sig)
    assert trade_id is not None

    # Close the trade right now at modest profit (+45 INR) so neither profit target nor loss limit triggers
    exit_pnl = temp_ledger.record_exit(trade_id, exit_price=2050.0, charges=5.0)
    assert exit_pnl is not None

    # Attempt re-entry immediately (1 second after exit)
    reentry_sig = dict(trade_sig, ts=time.time())
    err = router.risk_check(reentry_sig)
    assert err is not None
    assert "reentry_cooldown_active" in err
    assert "TCS" in err
    assert router.execute(reentry_sig) is None

    # Check that another symbol is NOT affected by MUTHOOTFIN's cooldown
    unrelated_sig = {
        "symbol": "NSE:INFY26NOV1800CE",
        "underlying": "INFY",
        "side": "BUY",
        "qty": 300,
        "price": 30.0,
        "stop_loss": 28.50,
        "target": 35.0,
        "strategy_id": "fno_scanner_option",
        "is_option": True,
        "ts": time.time(),
    }
    assert router.risk_check(unrelated_sig) is None
    assert router.execute(unrelated_sig) is not None


def test_max_daily_trades_limit(temp_ledger, monkeypatch):
    """Router must halt and reject any new trade once MAX_DAILY_TRADES is reached today."""
    monkeypatch.setattr("trading.execution_router.MAX_DAILY_TRADES", 3)
    router = ExecutionRouter(mode="paper", ledger=temp_ledger)

    stocks = ["SBIN", "TCS", "INFY"]
    for i, sym in enumerate(stocks):
        sig = {
            "symbol": sym,
            "side": "BUY",
            "qty": 2,
            "price": 500.0 + i * 50,
            "stop_loss": 480.0 + i * 50,
            "target": 540.0 + i * 50,
            "strategy_id": "fno_scanner",
            "ts": time.time() + i,
        }
        assert router.risk_check(sig) is None
        trade_id = router.execute(sig)
        assert trade_id is not None

    # 4th trade must be rejected because MAX_DAILY_TRADES == 3
    excess_sig = {
        "symbol": "RELIANCE",
        "side": "BUY",
        "qty": 1,
        "price": 1300.0,
        "stop_loss": 1270.0,
        "target": 1360.0,
        "strategy_id": "fno_scanner",
        "ts": time.time() + 10,
    }
    err = router.risk_check(excess_sig)
    assert err is not None
    assert "max_daily_trades_reached" in err
    assert router.execute(excess_sig) is None


def test_index_options_execution_and_guards(temp_ledger, monkeypatch):
    """Index option execution helper must evaluate, size, route, and enforce safety guards."""
    from trading.agents.agent_trader import _evaluate_and_execute_index_plan
    from trading.fno.index_options import IndexPlan
    from trading.fno.options import OptionPlan, OptionQuote
    from trading.fno.models import Provenance

    router = ExecutionRouter(mode="paper", ledger=temp_ledger)
    from datetime import datetime
    now_dt = datetime.now()
    prov = Provenance("nse-public", "NIFTY", "snapshot", now_dt, now_dt)

    quote = OptionQuote(
        underlying="NIFTY",
        identifier="NSE:NIFTY26NOV24500CE",
        option_type="Call",
        strike=24500.0,
        expiry="26-Nov-2026",
        last_price=120.0,
        pct_change=5.0,
        open_interest=50000.0,
        volume=20000.0,
        underlying_value=24520.0,
        prov=prov,
    )
    opt_plan = OptionPlan(
        quote=quote,
        breakeven=24620.0,
        breakeven_vs_t1=-0.5,
        clears_t1=True,
        clears_t2=True,
        premium_at_stop=84.0,
        premium_at_t1=180.0,
    )

    valid_nifty_plan = IndexPlan(
        spot=24520.0,
        or_high=24500.0,
        or_low=24400.0,
        atr=80.0,
        ema_fast=24480.0,
        ema_slow=24450.0,
        day_high=24530.0,
        day_low=24400.0,
        breakout=True,
        retested=True,
        holding=True,
        extended=False,
        name="NIFTY 50",
        index_symbol="NIFTY",
        direction="CALL",
        entry_low=24500.0,
        entry_high=24532.0,
        stop=24420.0,
        target1=24660.0,
        target2=24740.0,
        option=opt_plan,
    )

    # 1. Valid plan executes with correct symbol, lot size 65, and option parameters
    res = _evaluate_and_execute_index_plan(valid_nifty_plan, router, temp_ledger, dry_run=False)
    assert res is not None
    assert res["executed"] is True
    assert res["symbol"] == "NSE:NIFTY26NOV24500CE"
    assert res["qty"] == 65

    # 2. Duplicate NIFTY trade is strictly skipped by deduplication
    res_dup = _evaluate_and_execute_index_plan(valid_nifty_plan, router, temp_ledger, dry_run=False)
    assert res_dup is None  # Skipped before routing

    # 3. Non-tradeable plan (e.g. no breakout) is skipped
    invalid_plan = IndexPlan(
        spot=24450.0,
        or_high=24500.0,
        or_low=24400.0,
        atr=80.0,
        ema_fast=None,
        ema_slow=None,
        day_high=24470.0,
        day_low=24400.0,
        breakout=False,
        retested=False,
        holding=False,
        extended=False,
        name="BANK NIFTY",
        index_symbol="BANKNIFTY",
        direction="CALL",
        option=None,
        rejections=["no breakout above OR high"],
    )
    res_inv = _evaluate_and_execute_index_plan(invalid_plan, router, temp_ledger, dry_run=False)
    assert res_inv is None


def test_max_option_risk_and_position_caps(temp_ledger):
    """Router must strictly enforce MAX_OPTION_POSITION_VALUE (10k) and MAX_OPTION_RISK_PER_TRADE (500 INR)."""
    router = ExecutionRouter(mode="paper", ledger=temp_ledger)

    # 1. Oversized position (e.g. MuthootFin style 110 premium x 250 lot = 27,500 INR) must be REJECTED
    oversized_sig = {
        "symbol": "NSE:MUTHOOTFIN26NOV2450CE",
        "underlying": "MUTHOOTFIN",
        "side": "BUY",
        "qty": 250,
        "price": 110.0,
        "stop_loss": 99.0,  # 10% SL (11 pts x 250 = 2,750 INR risk, value = 27,500 INR)
        "target": 130.0,
        "is_option": True,
        "strategy_id": "fno_scanner_option",
        "ts": time.time(),
    }
    err = router.risk_check(oversized_sig)
    assert err is not None
    assert "max_option_position_value" in err
    assert router.execute(oversized_sig) is None

    # 2. Wide stop loss (e.g. risking 30% on 30 INR premium with 300 lot = 2,700 INR risk) must be REJECTED
    wide_stop_sig = {
        "symbol": "NSE:INFY26NOV1800CE",
        "underlying": "INFY",
        "side": "BUY",
        "qty": 300,
        "price": 30.0,
        "stop_loss": 21.0,  # 9 pts x 300 = 2,700 INR risk (> 500 cap)
        "target": 45.0,
        "is_option": True,
        "strategy_id": "fno_scanner_option",
        "ts": time.time(),
    }
    err2 = router.risk_check(wide_stop_sig)
    assert err2 is not None
    assert "max_option_risk_exceeded" in err2
    assert router.execute(wide_stop_sig) is None

    # 3. Properly sized trade with tight risk (<= 500 INR risk and <= 10,000 INR value) must PASS
    valid_sig = {
        "symbol": "NSE:INFY26NOV1800CE",
        "underlying": "INFY",
        "side": "BUY",
        "qty": 300,
        "price": 30.0,
        "stop_loss": 28.50,  # 1.5 pts x 300 = 450 INR risk (<= 500 cap)
        "target": 35.0,
        "is_option": True,
        "strategy_id": "fno_scanner_option",
        "ts": time.time(),
    }
    assert router.risk_check(valid_sig) is None
    trade_id = router.execute(valid_sig)
    assert trade_id is not None


def test_stock_lot_wise_risk_and_max_viable_premiums():
    """Verify standard lot sizes and lot-wise maximum viable premiums for stocks and indices."""
    from trading.options_engine import get_lot_size
    from trading.config import get_max_viable_option_premium

    # 1. Standard lot sizes
    assert get_lot_size("MUTHOOTFIN") == 250
    assert get_lot_size("NSE:MUTHOOTFIN26NOV2450CE") == 250
    assert get_lot_size("CDSL") == 375
    assert get_lot_size("NSE:CDSL26NOV1260CE") == 375
    assert get_lot_size("TATASTEEL") == 2750
    assert get_lot_size("NIFTY") == 65
    assert get_lot_size("BANKNIFTY") == 30
    assert get_lot_size("MRF") == 5

    # 2. Max viable premium ensures 1-lot risk <= 1,250 and 1-lot capital <= 10,000
    # For MUTHOOTFIN: lot 250 -> min(1250/(250*0.10)=50, 10000/250=40) = 40.0 INR max premium
    prem_muthoot = get_max_viable_option_premium("MUTHOOTFIN")
    assert prem_muthoot == 40.0
    assert 250 * prem_muthoot <= 10000.0
    assert 250 * (prem_muthoot * 0.10) <= 1250.0

    # For NIFTY: lot 65 -> min(1250/(65*0.10)=192.31, 10000/65=153.85) = 153.85 INR max premium
    prem_nifty = get_max_viable_option_premium("NIFTY")
    assert prem_nifty == 153.85
    assert 65 * (prem_nifty * 0.10) <= 1250.0

    # For TATASTEEL: lot 2750 -> min(1250/(2750*0.10)=4.55, 10000/2750=3.64) = 3.64 INR max premium
    prem_tatasteel = get_max_viable_option_premium("TATASTEEL")
    assert prem_tatasteel == 3.64
    assert abs(2750 * prem_tatasteel - 10000.0) <= 20.0


def test_options_engine_rejects_oversized_contracts_and_noise_stops(temp_ledger):
    """Options engine and router must reject oversized stock options and stops tighter than 3%."""
    from trading.options_engine import select_option_for_underlying
    from trading.fno.fyers import FyersClient

    # Mock client returning an option chain where all MuthootFin strikes are expensive (e.g. 110 INR)
    class ExpensiveMockFyers:
        def option_chain(self, symbol, strikecount=10):
            return {
                "optionsChain": [
                    {"strike_price": -1, "ltp": 2450.0},
                    {"strike_price": 2400.0, "option_type": "CE", "symbol": "NSE:MUTHOOTFIN26NOV2400CE", "ltp": 110.0, "oi": 50000, "volume": 10000},
                    {"strike_price": 2450.0, "option_type": "CE", "symbol": "NSE:MUTHOOTFIN26NOV2450CE", "ltp": 80.0, "oi": 60000, "volume": 15000},
                ],
                "expiryData": [{"date": "26-Nov-2026"}],
            }

    # Should be strictly rejected because even 80.0 > 20.0 max viable premium for lot size 250
    res = select_option_for_underlying("MUTHOOTFIN", side="BUY", spot_price=2450.0, client=ExpensiveMockFyers())
    assert res is None

    # Router must reject stops tighter than 3% (spread noise)
    router = ExecutionRouter(mode="paper", ledger=temp_ledger)
    noise_sig = {
        "symbol": "NSE:RELIANCE26NOV1300CE",
        "underlying": "RELIANCE",
        "side": "BUY",
        "qty": 250,
        "price": 100.0,
        "stop_loss": 99.0,  # 1% stop loss (too tight for option bid-ask spread)
        "target": 110.0,
        "is_option": True,
        "strategy_id": "fno_scanner_option",
        "ts": time.time(),
    }
    err = router.risk_check(noise_sig)
    assert err is not None
    assert "stop_loss_too_tight_for_spread" in err
    assert router.execute(noise_sig) is None


