"""Unit and Integration tests for Forex & Crypto 4-Agent AI Council."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
import pytest

from trading.delta_exchange import DeltaClient
from trading.forex_council import (
    ForexCouncilVerdict,
    debate_forex_setup,
    fetch_crypto_forex_news,
)
from trading.forex_engine import ForexEngine
from trading.forex_trader import ForexAutoTrader
from trading import forex_web


def test_fetch_crypto_forex_news():
    """Verify news fetcher handles queries and parses Google News RSS."""
    news = fetch_crypto_forex_news("BTCUSD", limit=3)
    assert isinstance(news, list)
    # Even if offline/network blocked, it should safely return a list without error
    for item in news:
        assert "title" in item
        assert "source" in item
        assert "link" in item


def test_forex_council_verdict_model():
    """Verify ForexCouncilVerdict schema validation."""
    verdict = ForexCouncilVerdict(
        symbol="BTCUSD",
        verdict="APPROVE",
        action="BUY",
        conviction=9,
        technical_analysis="9/21 EMA bullish cross confirmed above 200 SMA and VWAP.",
        bull_case="Momentum surge and volume expansion indicate trend continuation.",
        bear_case="Watch for overhead liquidity sweep at $72,000.",
        decision_rationale="Asymmetric 1:2 R:R setup with clear structural invalidation.",
        recommended_entry=70000.0,
        recommended_stop_loss=69500.0,
        recommended_target=71000.0,
    )
    assert verdict.verdict == "APPROVE"
    assert verdict.action == "BUY"
    assert verdict.conviction == 9
    assert verdict.recommended_entry == 70000.0


def test_debate_forex_setup_with_mocked_llm():
    """Verify debate_forex_setup parses LLM output and passes news context."""
    setup = {
        "symbol": "ETHUSD",
        "side": "BUY",
        "price": 2500.0,
        "stop_loss": 2470.0,
        "target1": 2560.0,
        "score": 85,
        "reasons": ["ORB high breakout", "RVOL > 1.4"],
    }
    news = [{"title": "Ethereum L2 Surge", "source": "CoinDesk", "link": "https://example.com"}]

    mock_verdict = ForexCouncilVerdict(
        symbol="ETHUSD",
        verdict="APPROVE",
        action="BUY",
        conviction=9,
        technical_analysis="Clean break above 15m ORB with volume confirmation.",
        bull_case="Strong accumulation and L2 scaling momentum.",
        bear_case="No immediate supply block until $2,600.",
        decision_rationale="Risk $50 with 1:2 R:R target.",
        recommended_entry=2500.0,
        recommended_stop_loss=2470.0,
        recommended_target=2560.0,
    )

    with patch("trading.forex_council.call_structured", return_value=mock_verdict) as mock_call:
        res = debate_forex_setup(setup, news=news)
        assert res.verdict == "APPROVE"
        assert res.conviction == 9
        assert mock_call.called


def test_debate_forex_setup_fallback_on_exception():
    """When LLM encounters an exception, debate_forex_setup returns a safe fallback."""
    setup = {
        "symbol": "SOLUSD",
        "side": "BUY",
        "price": 180.0,
        "stop_loss": 176.0,
        "target1": 188.0,
        "score": 80,
        "reasons": ["9 EMA > 21 EMA"],
    }

    with patch("trading.forex_council.call_structured", side_effect=RuntimeError("API error")):
        res = debate_forex_setup(setup, news=[])
        assert res.symbol == "SOLUSD"
        assert res.verdict == "APPROVE"  # score 80 >= 75
        assert res.conviction == 8


def test_auto_trader_council_veto_blocks_execution(tmp_path):
    """When the 4-agent council issues a VETO, the auto trader must block the trade."""
    pos_file = tmp_path / "positions.json"
    client = DeltaClient()
    client._load_paper_positions = lambda: json.loads(pos_file.read_text(encoding="utf-8")) if pos_file.exists() else []
    client._save_paper_positions = lambda positions: pos_file.write_text(json.dumps(positions), encoding="utf-8")
    client.get_tickers = MagicMock(return_value={"BTCUSD": {"mark_price": 70000.0}})

    trader = ForexAutoTrader(client=client)
    trader.engine.scan_all = MagicMock(return_value=[{
        "symbol": "BTCUSD",
        "side": "BUY",
        "price": 70000.0,
        "stop_loss": 69500.0,
        "target1": 71000.0,
        "conviction": 9,
        "score": 90,
        "reasons": ["ORB high breakout"],
    }])

    # Mock Council VETO (e.g. liquidity trap detected by Bearish Researcher)
    veto_verdict = ForexCouncilVerdict(
        symbol="BTCUSD",
        verdict="VETO",
        action="HOLD",
        conviction=3,
        technical_analysis="False breakout trap with long upper wick.",
        bull_case="Momentum is exhausted.",
        bear_case="Massive sell wall and liquidation wick at $70,200.",
        decision_rationale="Trade vetoed to protect capital from liquidity trap.",
        recommended_entry=70000.0,
        recommended_stop_loss=69500.0,
        recommended_target=71000.0,
    )

    with patch("trading.forex_trader.debate_forex_setup", return_value=veto_verdict):
        res = trader.run_pass()
        assert len(res["executed_trades"]) == 0
        assert any("Council VETO" in r for r in res.get("rejections", []))
        assert len(client.get_positions()) == 0


def test_auto_trader_council_bypass_toggle(tmp_path):
    """When council is disabled, auto-trader executes qualifying trades purely on quant score."""
    pos_file = tmp_path / "positions.json"
    client = DeltaClient()
    client._load_paper_positions = lambda: json.loads(pos_file.read_text(encoding="utf-8")) if pos_file.exists() else []
    client._save_paper_positions = lambda positions: pos_file.write_text(json.dumps(positions), encoding="utf-8")
    client.get_tickers = MagicMock(return_value={"BTCUSD": {"mark_price": 70000.0}})

    trader = ForexAutoTrader(client=client)
    trader.set_council_enabled(False)
    assert trader.is_council_enabled is False

    trader.engine.scan_all = MagicMock(return_value=[{
        "symbol": "BTCUSD",
        "side": "BUY",
        "price": 70000.0,
        "stop_loss": 69500.0,
        "target1": 71000.0,
        "conviction": 9,
        "score": 90,
        "reasons": ["ORB high breakout"],
    }])

    res = trader.run_pass()
    assert len(res["executed_trades"]) == 1
    assert len(client.get_positions()) == 1


def test_forex_web_council_endpoints():
    """Verify web API routes for council status, toggle, news, and debate."""
    # 1. Council status
    status_code, ctype, body = forex_web.serve_get("/api/forex/council_status", {})
    assert status_code == 200
    data = json.loads(body.decode("utf-8"))
    assert "council_enabled" in data

    # 2. News endpoint
    status_code, ctype, body = forex_web.serve_get("/api/forex/news", {"symbol": ["BTCUSD"]})
    assert status_code == 200
    data = json.loads(body.decode("utf-8"))
    assert data["ok"] is True
    assert data["symbol"] == "BTCUSD"
    assert "news" in data

    # 3. Council toggle endpoint
    status_code, ctype, body = forex_web.serve_post("/api/forex/council_toggle", {"enabled": True})
    assert status_code == 200
    data = json.loads(body.decode("utf-8"))
    assert data["council_enabled"] is True

    # 4. Council debate endpoint
    setup = {
        "symbol": "BTCUSD",
        "side": "BUY",
        "price": 70000.0,
        "stop_loss": 69500.0,
        "target1": 71000.0,
        "score": 85,
        "conviction": 9,
        "reasons": ["ORB breakout"],
    }
    mock_verdict = ForexCouncilVerdict(
        symbol="BTCUSD",
        verdict="APPROVE",
        action="BUY",
        conviction=9,
        technical_analysis="9/21 EMA bullish cross confirmed above 200 SMA and VWAP.",
        bull_case="Momentum surge and volume expansion indicate trend continuation.",
        bear_case="Watch for overhead liquidity sweep at $72,000.",
        decision_rationale="Asymmetric 1:2 R:R setup with clear structural invalidation.",
        recommended_entry=70000.0,
        recommended_stop_loss=69500.0,
        recommended_target=71000.0,
    )
    with patch("trading.forex_council.fetch_crypto_forex_news", return_value=[]), \
         patch("trading.forex_council.debate_forex_setup", return_value=mock_verdict):
        status_code, ctype, body = forex_web.serve_post("/api/forex/council_debate", {"setup": setup})
        assert status_code == 200
        data = json.loads(body.decode("utf-8"))
        assert data["ok"] is True
        assert "verdict" in data
        assert "news" in data

    # 5. History endpoint
    status_code, ctype, body = forex_web.serve_get("/api/forex/history", {})
    assert status_code == 200
    history = json.loads(body.decode("utf-8"))
    assert isinstance(history, list)

    # 6. Page HTML content verification
    status_code, ctype, body = forex_web.serve_get("/forex", {})
    assert status_code == 200
    html = body.decode("utf-8")
    assert "Total Trades Given" in html
    assert "Completed Trades History" in html
    assert "Net Realized P&amp;L" in html
