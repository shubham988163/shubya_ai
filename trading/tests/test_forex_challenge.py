"""Unit tests for the $100 to $1,000 Compounding Survival Challenge and AI Post-Mortem Autopsy Engine."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trading.forex_reflection import (
    TradeReflection,
    reflect_on_trade,
    get_recent_lessons,
    save_reflections,
    load_reflections,
    REFLECTIONS_FILE,
)
from trading.forex_trader import ForexAutoTrader
from trading.forex_web import serve_get, serve_post


@pytest.fixture
def temp_positions_file(tmp_path, monkeypatch):
    pos_file = tmp_path / "forex_positions.json"
    import trading.delta_exchange
    monkeypatch.setattr(trading.delta_exchange, "POSITIONS_FILE", pos_file)
    return pos_file


def test_equity_and_dynamic_risk_sizing(temp_positions_file):
    """Verify $100 capital survival math: 2.5% risk ($2.50) and 6% daily limit ($6.00)."""
    trader = ForexAutoTrader()
    # At initial $100 capital with 0 positions
    eq = trader.get_account_equity()
    assert eq["starting_capital"] == 100.0
    assert eq["current_equity"] == 100.0
    assert eq["dynamic_risk_usd"] == 2.50
    assert eq["max_daily_loss_usd"] == 6.00
    assert eq["progress_pct"] == 0.0


def test_compounding_equity_at_scale(temp_positions_file):
    """Verify dynamic compounding scales risk safely as account grows to $500 and $1,000."""
    sample_closed = [
        {"id": 1, "symbol": "BTCUSD", "side": "BUY", "size": 0.1, "entry_price": 70000, "exit_price": 72000, "pnl": 400.0, "status": "CLOSED"},
    ]
    temp_positions_file.write_text(json.dumps(sample_closed), encoding="utf-8")

    trader = ForexAutoTrader()
    eq = trader.get_account_equity()
    assert eq["starting_capital"] == 100.0
    assert eq["total_realized_pnl"] == 400.0
    assert eq["current_equity"] == 500.0
    # At $500 equity: 2.5% risk = $12.50
    assert eq["dynamic_risk_usd"] == 12.50
    # At $500 equity: 6% daily loss limit = $30.00
    assert eq["max_daily_loss_usd"] == 30.00
    # Progress from 100 to 1000 is (400 / 900) * 100 = 44.4%
    assert 44.0 <= eq["progress_pct"] <= 45.0


def test_post_trade_autopsy_reflection():
    """Verify AI post-mortem autopsy extracts lessons from losses and wins."""
    losing_trade = {
        "id": 12345,
        "symbol": "SOLUSD",
        "side": "BUY",
        "entry_price": 95.0,
        "exit_price": 94.0,
        "stop_loss": 94.0,
        "initial_stop": 94.0,
        "target": 97.0,
        "pnl": -2.50,
        "pnl_pct": -1.05,
        "exit_reason": "Stop loss hit",
        "entry_time": "2026-10-31 10:00:00",
        "exit_time": "2026-10-31 10:30:00",
    }
    reflection = reflect_on_trade(losing_trade)
    assert isinstance(reflection, TradeReflection)
    assert reflection.outcome == "LOSS"
    assert reflection.symbol == "SOLUSD"
    assert len(reflection.lesson_learned) > 0
    assert len(reflection.root_cause) > 0
    assert len(reflection.rule_recommendation) > 0


def test_recent_lessons_retrieval(tmp_path, monkeypatch):
    """Verify lessons are retrieved and formatted for injection into the 4-Agent Council."""
    temp_ref_file = tmp_path / "forex_reflections.json"
    monkeypatch.setattr("trading.forex_reflection.REFLECTIONS_FILE", temp_ref_file)

    sample_refs = [
        {
            "trade_id": "1",
            "symbol": "ETHUSD",
            "side": "BUY",
            "pnl": -2.50,
            "outcome": "LOSS",
            "lesson_learned": "Wait for 1-hour candle close instead of wick breakout to avoid trap.",
            "rule_recommendation": "Require volume confirmation.",
        },
        {
            "trade_id": "2",
            "symbol": "BTCUSD",
            "side": "BUY",
            "pnl": 5.00,
            "outcome": "WIN",
            "lesson_learned": "Trailing stop after +1.5R protected full expansion.",
            "rule_recommendation": "Maintain dynamic 1.0R trailing.",
        }
    ]
    save_reflections(sample_refs)
    lessons = get_recent_lessons(limit=5)
    assert len(lessons) == 2
    assert "BTCUSD" in lessons[0]
    assert "ETHUSD" in lessons[1]


def test_web_challenge_endpoints():
    """Verify web API routes /api/forex/equity and /api/forex/reflections."""
    eq_res = serve_get("/api/forex/equity", {})
    assert eq_res is not None
    code, ctype, body = eq_res
    assert code == 200
    data = json.loads(body.decode("utf-8"))
    assert "current_equity" in data
    assert "dynamic_risk_usd" in data

    ref_res = serve_get("/api/forex/reflections", {})
    assert ref_res is not None
    code, ctype, body = ref_res
    assert code == 200
    refs = json.loads(body.decode("utf-8"))
    assert isinstance(refs, list)


def test_chart_and_candles_endpoints():
    """Verify /chart and /api/forex/candles endpoints."""
    chart_res = serve_get("/chart", {"symbol": ["BTCUSD"]})
    assert chart_res is not None
    code, ctype, body = chart_res
    assert code == 200
    assert b"TradingView" in body or b"tradingview" in body
    assert b"chartStationPanel" in body

    candle_res = serve_get("/api/forex/candles", {"symbol": ["BTCUSD"], "resolution": ["5"]})
    assert candle_res is not None
    code, ctype, body = candle_res
    assert code == 200
    data = json.loads(body.decode("utf-8"))
    assert "candles" in data
