"""Unit and Integration tests for Forex & Crypto Autonomous Trader and Backtester."""
from __future__ import annotations

from datetime import datetime
import json
import pytest
from unittest.mock import MagicMock, patch

from trading.delta_exchange import DeltaClient
from trading.forex_engine import ForexEngine
from trading.forex_trader import ForexAutoTrader
from trading.forex_backtest import ForexBacktester


@pytest.fixture
def mock_delta_client(tmp_path):
    pos_file = tmp_path / "test_forex_positions.json"
    client = DeltaClient()
    client._load_paper_positions = lambda: json.loads(pos_file.read_text(encoding="utf-8")) if pos_file.exists() else []
    client._save_paper_positions = lambda positions: pos_file.write_text(json.dumps(positions), encoding="utf-8")
    client.get_tickers = MagicMock(return_value={
        "BTCUSD": {"mark_price": 70000.0},
        "ETHUSD": {"mark_price": 2500.0},
        "SOLUSD": {"mark_price": 180.0},
        "XAUTUSD": {"mark_price": 2700.0},
    })
    return client


def test_forex_auto_trader_config_and_toggle(mock_delta_client, tmp_path):
    trader = ForexAutoTrader(client=mock_delta_client)
    assert trader.is_enabled is True

    trader.set_enabled(False)
    assert trader.is_enabled is False

    trader.set_enabled(True)
    assert trader.is_enabled is True


def test_forex_auto_trader_deduplication(mock_delta_client):
    """Auto-trader must never open a duplicate position on an already active symbol."""
    # Seed 1 open BTCUSD position
    mock_delta_client.execute_trade(
        symbol="BTCUSD",
        side="BUY",
        size=0.01,
        price=70000.0,
        stop_loss=69000.0,
        target=72000.0,
    )

    engine = ForexEngine(client=mock_delta_client)
    # Mock scan_all returning a qualifying setup for BTCUSD
    engine.scan_all = MagicMock(return_value=[{
        "symbol": "BTCUSD",
        "side": "BUY",
        "price": 70100.0,
        "stop_loss": 69100.0,
        "target1": 72100.0,
        "conviction": 9,
    }])

    trader = ForexAutoTrader(client=mock_delta_client, engine=engine)
    res = trader.run_pass()

    # Must be 0 newly executed trades because BTCUSD is already open
    assert len(res["executed_trades"]) == 0
    assert res["open_count"] == 1


def test_forex_auto_trader_max_positions_limit(mock_delta_client):
    """Auto-trader must halt new entries once max_open_positions (3) is reached."""
    # Seed 3 open positions with entry prices matching mock mark prices
    mock_delta_client.execute_trade(symbol="BTCUSD", side="BUY", size=0.01, price=70000.0, stop_loss=69000.0, target=72000.0)
    mock_delta_client.execute_trade(symbol="ETHUSD", side="BUY", size=0.1, price=2500.0, stop_loss=2450.0, target=2600.0)
    mock_delta_client.execute_trade(symbol="SOLUSD", side="BUY", size=1.0, price=180.0, stop_loss=175.0, target=190.0)

    engine = ForexEngine(client=mock_delta_client)
    engine.scan_all = MagicMock(return_value=[{
        "symbol": "XAUTUSD",
        "side": "BUY",
        "price": 2700.0,
        "stop_loss": 2680.0,
        "target1": 2740.0,
        "conviction": 9,
    }])

    trader = ForexAutoTrader(client=mock_delta_client, engine=engine)
    res = trader.run_pass()

    assert res["state"] == "max_open_positions"
    assert len(res["executed_trades"]) == 0
    assert res["open_count"] == 3


def test_forex_auto_trader_daily_loss_circuit_breaker(mock_delta_client):
    """Auto-trader must halt and stand down if daily loss hits -$150."""
    # Seed 1 closed trade with -$160 loss
    positions = [{
        "id": 1,
        "symbol": "BTCUSD",
        "side": "BUY",
        "size": 1.0,
        "entry_price": 70000.0,
        "exit_price": 69840.0,
        "status": "CLOSED",
        "pnl": -160.0,
        "entry_time": datetime.now().strftime("%Y-%m-%d 10:00:00"),
        "exit_time": datetime.now().strftime("%Y-%m-%d 10:30:00"),
    }]
    mock_delta_client._save_paper_positions(positions)

    engine = ForexEngine(client=mock_delta_client)
    trader = ForexAutoTrader(client=mock_delta_client, engine=engine)
    res = trader.run_pass()

    assert res["state"] == "daily_loss_limit_hit"
    assert "Daily loss limit hit" in res["rejections"][0]
    assert len(res["executed_trades"]) == 0


def test_forex_trailing_stop_and_breakeven_lock(mock_delta_client):
    """DeltaClient must lock stop at entry on +1.0R gain and trail past +1.5R gain."""
    mock_delta_client.execute_trade(
        symbol="BTCUSD",
        side="BUY",
        size=0.1,
        price=70000.0,
        stop_loss=68000.0,  # Risk R = 2,000 pts
        target=74000.0,
    )

    # 1. Price advances +1.0R to 72,100 -> stop loss must be moved to entry (70,000)
    mock_delta_client.get_tickers = MagicMock(return_value={
        "BTCUSD": {"mark_price": 72100.0}
    })
    open_pos = mock_delta_client.get_positions()
    assert len(open_pos) == 1
    assert open_pos[0]["stop_loss"] == 70000.0
    assert "Breakeven locked" in open_pos[0].get("trailing_note", "")

    # 2. Price advances past +1.5R to 73,500 -> stop loss must trail 1.0R behind mark price (73,500 - 2,000 = 71,500)
    mock_delta_client.get_tickers = MagicMock(return_value={
        "BTCUSD": {"mark_price": 73500.0}
    })
    open_pos = mock_delta_client.get_positions()
    assert open_pos[0]["stop_loss"] == 71500.0

    # 3. Price drops to hit trailing stop at 71,400 -> position must CLOSE with profit
    mock_delta_client.get_tickers = MagicMock(return_value={
        "BTCUSD": {"mark_price": 71400.0}
    })
    open_pos = mock_delta_client.get_positions()
    assert len(open_pos) == 0  # Now closed!

    all_pos = mock_delta_client.get_all_positions()
    assert len(all_pos) == 1
    closed = all_pos[0]
    assert closed["status"] == "CLOSED"
    assert closed["exit_reason"] == "Stop loss hit"
    assert closed["pnl"] > 0.0  # Profitable exit via trailing stop!


def test_forex_backtester_execution(mock_delta_client):
    """ForexBacktester must parse candles and compute metrics properly."""
    import pandas as pd
    import numpy as np

    dates = pd.date_range("2026-10-01", periods=100, freq="15min")
    prices = 70000.0 + np.cumsum(np.random.normal(5, 50, 100))
    df = pd.DataFrame({
        "Open": prices,
        "High": prices + 20,
        "Low": prices - 20,
        "Close": prices + 5,
        "Volume": [100.0] * 100,
    }, index=dates)

    mock_delta_client.get_candles = MagicMock(return_value=df)
    bt = ForexBacktester(client=mock_delta_client)
    res = bt.backtest_symbol("BTCUSD", bars=100)

    assert "total_trades" in res
    assert "win_rate" in res
    assert "profit_factor" in res
    assert "net_pnl" in res


def test_forex_auto_trader_peak_session_hard_gate(mock_delta_client):
    """Auto-trader must strictly stand down outside London/NY overlap (12-17 UTC)."""
    engine = ForexEngine(client=mock_delta_client)
    engine.scan_all = MagicMock(return_value=[{
        "symbol": "BTCUSD",
        "side": "BUY",
        "price": 70000.0,
        "stop_loss": 69000.0,
        "target1": 72000.0,
        "conviction": 9.5,
        "rvol": 3.0,
    }])

    trader = ForexAutoTrader(client=mock_delta_client, engine=engine)
    trader.config["peak_session_only"] = True
    trader.config["peak_session_utc_hours"] = [12, 17]

    # Mock time outside peak window (e.g. 06:00 UTC / Asian session)
    with patch("datetime.datetime") as mock_dt:
        mock_now = MagicMock()
        mock_now.hour = 6
        mock_now.strftime.return_value = "2026-11-21 11:30:00"
        mock_dt.now.return_value = mock_now

        res = trader.run_pass()
        assert res["state"] == "off_peak_liquidity"
        assert len(res["executed_trades"]) == 0
        assert "Outside peak London/NY overlap" in res["rejections"][0]


def test_forex_auto_trader_sideways_and_order_flow_filters(mock_delta_client):
    """Auto-trader must reject sideways chop setups and balanced order flow."""
    engine = ForexEngine(client=mock_delta_client)
    trader = ForexAutoTrader(client=mock_delta_client, engine=engine)
    trader.config["peak_session_only"] = False
    trader.set_council_enabled(False)

    # 1. Sideways chop setup rejection (ADX < 20 / SIDEWAYS_CHOP)
    engine.scan_all = MagicMock(return_value=[{
        "symbol": "BTCUSD",
        "side": "BUY",
        "price": 70000.0,
        "stop_loss": 69000.0,
        "target1": 72000.0,
        "conviction": 9.0,
        "rvol": 2.2,
        "market_state": "SIDEWAYS_CHOP",
        "adx": 14.5,
        "bb_width": 0.6,
        "buyer_ratio": 65.0,
    }])
    res = trader.run_pass()
    assert len(res["executed_trades"]) == 0
    assert any("Sideways Market Gate" in r for r in res["rejections"])

    # 2. Balanced order flow rejection (Buyer Ratio 48% < 55% threshold on BUY)
    engine.scan_all = MagicMock(return_value=[{
        "symbol": "BTCUSD",
        "side": "BUY",
        "price": 70000.0,
        "stop_loss": 69000.0,
        "target1": 72000.0,
        "conviction": 9.0,
        "rvol": 2.2,
        "market_state": "TRENDING_MOMENTUM",
        "adx": 28.0,
        "bb_width": 2.5,
        "buyer_ratio": 48.0,
    }])
    res = trader.run_pass()
    assert len(res["executed_trades"]) == 0
    assert any("Order Flow Gate" in r for r in res["rejections"])

    # 3. Valid trending setup with aggressive buyer order flow (Buyer Ratio 68% >= 55%) -> Approved!
    engine.scan_all = MagicMock(return_value=[{
        "symbol": "BTCUSD",
        "side": "BUY",
        "price": 70000.0,
        "stop_loss": 69000.0,
        "target1": 72000.0,
        "conviction": 9.0,
        "rvol": 2.5,
        "market_state": "TRENDING_MOMENTUM",
        "adx": 32.0,
        "bb_width": 3.0,
        "buyer_ratio": 68.0,
    }])
    res = trader.run_pass()
    assert len(res["executed_trades"]) == 1
    assert res["executed_trades"][0]["symbol"] == "BTCUSD"


def test_forex_pine_orb_20_50_ema_and_30m_sma200_gates(monkeypatch):
    """ForexEngine must enforce Pine OR Bias, 20/50 EMA, and 30m 200 SMA alignment."""
    import pandas as pd
    import numpy as np
    from trading import forex_engine as fe

    fe._FOREX_30M_CACHE.clear()
    fe._FOREX_DAILY_CACHE.clear()
    monkeypatch.setattr(fe, "fetch_forex_daily_mas", lambda sym: {})

    # Construct 90 15m bars starting at 08:00 UTC with a clean bullish trend + Green 13:00 UTC OR candle
    idx = pd.date_range("2026-11-27 00:00", periods=90, freq="15min", tz="UTC")
    base = np.linspace(69000.0, 70000.0, 90)
    opens = base - 10.0
    closes = base + 15.0
    highs = closes + 20.0
    lows = opens - 20.0
    vols = np.full(90, 100.0)

    # Identify the 13:00 UTC bar as the session OR bar; keep steady uptrend right below OR high so ADX stays > 20
    or_idx = [i for i, t in enumerate(idx) if t.hour == 13 and t.minute == 0][-1]
    opens[or_idx] = 69800.0
    closes[or_idx] = 69900.0  # Green OR candle
    highs[or_idx] = 69920.0
    lows[or_idx] = 69780.0
    post_count = 89 - (or_idx + 1)
    if post_count > 0:
        ramp = np.linspace(69500.0, 69900.0, post_count)
        for k, j in enumerate(range(or_idx + 1, 89)):
            opens[j] = ramp[k] - 30.0
            closes[j] = ramp[k] + 15.0
            highs[j] = ramp[k] + 20.0
            lows[j] = ramp[k] - 40.0
    # Final breakout bar with strong buyer close and RVOL
    opens[-1] = 69930.0
    closes[-1] = 70250.0
    highs[-1] = 70260.0
    lows[-1] = 69920.0
    vols[-1] = 350.0

    df15 = pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols}, index=idx)

    # 30m DataFrame where 200 SMA is BELOW price (allows Long)
    df30_ok = pd.DataFrame({
        "Open": np.full(220, 68000.0),
        "High": np.full(220, 68100.0),
        "Low": np.full(220, 67900.0),
        "Close": np.full(220, 68000.0),
        "Volume": np.full(220, 100.0),
    })

    client = MagicMock()
    client.get_candles.side_effect = lambda sym, resolution="15", bars=120: df30_ok if resolution == "30" else df15
    engine = ForexEngine(client=client)
    setup = engine.evaluate_symbol("BTCUSD")
    assert setup is not None
    assert setup["strategy"] == "pine_orb_multi_ma"
    assert setup["side"] == "BUY"
    assert setup["sma_200_30m"] == 68000.0
    assert setup["target1"] > setup["price"]

    # Now make 30m 200 SMA ABOVE spot price (72000 > 70250) -> Pine HTF filter must veto Long
    fe._FOREX_30M_CACHE.clear()
    df30_high = df30_ok.copy()
    df30_high["Close"] = 72000.0
    client.get_candles.side_effect = lambda sym, resolution="15", bars=120: df30_high if resolution == "30" else df15
    assert engine.evaluate_symbol("BTCUSD") is None

    # Now restore 30m 200 SMA below price, but flip the 13:00 UTC OR candle RED -> Pine OR bias must veto Long
    fe._FOREX_30M_CACHE.clear()
    df15_red_or = df15.copy()
    df15_red_or.iloc[or_idx, df15_red_or.columns.get_loc("Open")] = 69910.0
    df15_red_or.iloc[or_idx, df15_red_or.columns.get_loc("Close")] = 69790.0
    client.get_candles.side_effect = lambda sym, resolution="15", bars=120: df30_ok if resolution == "30" else df15_red_or
    assert engine.evaluate_symbol("BTCUSD") is None



