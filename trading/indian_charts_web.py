"""NSE Indian Markets — Real-Time Candlestick Chart Station with TradingView & ORB Multi-MA Strategy Indicators.

Provides:
  - Live TradingView candlestick charting for all NSE stocks and indices (NIFTY, BANKNIFTY, RELIANCE, SBIN, DIXON, etc.)
  - Real-time Strategy Indicator Overlay on the chart (Target 1:1.5, OR High/Low, VWAP, 200 SMA, Daily 50/200/365 Wall)
  - 6-Metric Strategy Confluence HUD (Trend filters, 15m Candle Color Bias, RVOL >= 1.5x, 365 Wall Safety Gate)
  - Preloaded TradingView studies: VWAP, Volume, Moving Average, RSI
  - 1-Click Copy TradingView Pine Script v5 Indicator
  - 4-Agent AI Council Debate for any Indian stock
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs

import numpy as np
import pandas as pd

from trading import ui_theme
from trading.agents.agent_trader import (
    AGENT_SYSTEM,
    AgentTradeRecommendation,
    call_structured,
    fetch_symbol_context,
)
from trading.ledger import Ledger
from trading.strategies.orb_multi_ma import _fetch_daily_mas


def get_indian_parameters(symbol: str) -> dict:
    """Fetch live technical parameters and strategy metrics for any NSE stock or index."""
    raw = (symbol or "SBIN").strip().upper()
    clean = (
        raw.replace("NSE:", "")
        .replace("BSE:", "")
        .replace("-EQ", "")
        .replace("-INDEX", "")
        .replace("BINANCE:", "")
        .replace("TVC:", "")
        .strip()
    )
    if clean in ("NIFTY50", "NIFTY 50", "^NSEI"):
        clean = "NIFTY"
    elif clean in ("BANKNIFTY", "BANK NIFTY", "^NSEBANK"):
        clean = "BANKNIFTY"

    ctx = fetch_symbol_context(clean) or {}
    daily_mas = _fetch_daily_mas(clean) or {}

    ltp = float(ctx.get("ltp") or 0.0)
    or_high = float(ctx.get("or_high") or 0.0) if ctx.get("or_high") is not None else None
    or_low = float(ctx.get("or_low") or 0.0) if ctx.get("or_low") is not None else None
    or_pct = float(ctx.get("or_pct") or 0.0)
    or_valid = bool(ctx.get("or_valid", False))
    or_bias_val = ctx.get("or_bias", "NONE")

    vwap = float(ctx.get("session_vwap") or 0.0)
    vwap_slope = str(ctx.get("vwap_slope") or "flat").upper()
    vwap_status = f"ABOVE VWAP ({vwap_slope})" if ltp >= vwap else f"BELOW VWAP ({vwap_slope})"

    sma200_1h = float(ctx.get("ma_200_1h") or 0.0) if ctx.get("ma_200_1h") is not None else None
    sma200_30m = float(ctx.get("ma_200_30m") or 0.0) if ctx.get("ma_200_30m") is not None else None
    trend_gate = str(ctx.get("trend_gate") or "unavailable").lower()

    d50 = daily_mas.get("d50")
    d200 = daily_mas.get("d200")
    d365 = daily_mas.get("d365")

    # 365 MA Wall check
    wall_safe = True
    wall_note = "Safe Distance"
    if d365 and d365 > 0 and ltp > 0:
        gap_buy = (d365 - ltp) / ltp * 100
        gap_sell = (ltp - d365) / ltp * 100
        if 0 < gap_buy <= 0.5:
            wall_safe = False
            wall_note = f"Wall Veto: Resistance {d365:,.2f} is within {gap_buy:.2f}%"
        elif 0 < gap_sell <= 0.5:
            wall_safe = False
            wall_note = f"Wall Veto: Support {d365:,.2f} is within {gap_sell:.2f}%"
        else:
            dist = min(abs(gap_buy), abs(gap_sell))
            wall_note = f"Safe Distance ({dist:.1f}% to 365 MA)"

    # Bias string
    bias_str = "NEUTRAL"
    if or_bias_val == 1 or or_bias_val == "GREEN" or str(or_bias_val).lower() == "bull":
        bias_str = "GREEN"
    elif or_bias_val == -1 or or_bias_val == "RED" or str(or_bias_val).lower() == "bear":
        bias_str = "RED"

    # Volume RVOL
    vol_ratio = float(ctx.get("vol_ratio") or 1.0)
    vol_status = f"SURGE ({vol_ratio:.1f}x >= 1.5x)" if vol_ratio >= 1.5 else f"NORMAL ({vol_ratio:.2f}x)"

    # ATR
    atr = float(ctx.get("atr") or (ltp * 0.008 if ltp > 0 else 5.0))
    ema9 = float(ctx.get("ema9") or (ltp * 0.998 if ltp > 0 else 0.0))
    ema21 = float(ctx.get("ema21") or (ltp * 0.995 if ltp > 0 else 0.0))
    ema_trend = "BULLISH (9 > 21)" if ema9 >= ema21 else "BEARISH (9 < 21)"

    # Triggers and Exits (ORB 1:1.5 RR rule)
    long_trig = or_high if or_high else round(ltp * 1.005, 2)
    short_trig = or_low if or_low else round(ltp * 0.995, 2)

    risk_long = max(1.0, abs(ltp - (or_low if or_low else (ltp - 1.5 * atr))))
    risk_short = max(1.0, abs((or_high if or_high else (ltp + 1.5 * atr)) - ltp))

    target_long = round(ltp + 1.5 * risk_long, 2)
    target_short = round(ltp - 1.5 * risk_short, 2)
    stop_long = round(or_low if or_low else (ltp - risk_long), 2)
    stop_short = round(or_high if or_high else (ltp + risk_short), 2)

    # Score calculation
    score = 50
    reasons = []

    is_breakout_long = or_high and ltp > or_high and bias_str == "GREEN"
    is_breakdown_short = or_low and ltp < or_low and bias_str == "RED"

    if ema9 > ema21:
        score += 10
        reasons.append("Bullish 9/21 EMA momentum")
    else:
        reasons.append("Bearish 9/21 EMA momentum")

    if trend_gate == "up":
        score += 15
        reasons.append("Price above 1H & 30m 200 SMA (Uptrend)")
    elif trend_gate == "down":
        score += 15
        reasons.append("Price below 1H & 30m 200 SMA (Downtrend)")

    if d50 and d200:
        if ltp > d50 and ltp > d200:
            score += 10
            reasons.append("Trading above Daily 50 & 200 SMA")
        elif ltp < d50 and ltp < d200:
            score += 10
            reasons.append("Trading below Daily 50 & 200 SMA")

    if is_breakout_long:
        score += 20
        action = "BUY / LONG BREAKOUT"
        action_badge = "bull"
        reasons.append(f"Breakout above OR-High (₹{or_high:,.2f}) with Green 15m Bias")
    elif is_breakdown_short:
        score += 20
        action = "SELL / SHORT BREAKDOWN"
        action_badge = "bear"
        reasons.append(f"Breakdown below OR-Low (₹{or_low:,.2f}) with Red 15m Bias")
    else:
        action = "HOLD / RANGEBOUND CONSOLIDATION"
        action_badge = "ok"
        if or_high and or_low:
            reasons.append(f"Inside Opening Range (₹{or_low:,.2f} – ₹{or_high:,.2f})")

    if vol_ratio >= 1.5:
        score += 15
        reasons.append(f"Institutional volume surge ({vol_ratio:.1f}x >= 1.5x)")
    elif vol_ratio >= 1.2:
        score += 5

    if not wall_safe:
        score = max(20, score - 30)
        reasons.append(wall_note)

    conviction = min(10, max(2, int(score / 10)))

    return {
        "ok": True,
        "symbol": clean,
        "raw_symbol": raw,
        "tradingview_symbol": f"NSE:{clean}" if not clean.startswith("NSE:") else clean,
        "strategy": "orb_multi_ma",
        "mark_price": ltp,
        "ltp": ltp,
        "ema_9": round(ema9, 2),
        "ema_21": round(ema21, 2),
        "ema_trend": ema_trend,
        "vwap": round(vwap, 2),
        "vwap_status": vwap_status,
        "vwap_slope": vwap_slope,
        "sma200_1h": round(sma200_1h, 2) if sma200_1h else None,
        "sma200_30m": round(sma200_30m, 2) if sma200_30m else None,
        "trend_gate": trend_gate.upper(),
        "daily_50": round(d50, 2) if d50 else None,
        "daily_200": round(d200, 2) if d200 else None,
        "daily_365_wall": round(d365, 2) if d365 else None,
        "wall_note": wall_note,
        "wall_safe": wall_safe,
        "range_high": or_high,
        "range_low": or_low,
        "range_width_pct": or_pct,
        "or_valid": or_valid,
        "or_bias": bias_str,
        "rvol": round(vol_ratio, 2),
        "rvol_status": vol_status,
        "atr": round(atr, 2),
        "score": score,
        "conviction": conviction,
        "action": action,
        "action_badge": action_badge,
        "long_trigger": long_trig,
        "short_trigger": short_trig,
        "planned_sl": stop_long if ltp >= vwap else stop_short,
        "planned_target1": target_long if ltp >= vwap else target_short,
        "planned_target2": round(ltp + 2.0 * risk_long if ltp >= vwap else ltp - 2.0 * risk_short, 2),
        "reasons": reasons,
        "updated_at": pd.Timestamp.now().strftime("%H:%M:%S"),
    }


_CANDLE_CACHE: dict[str, tuple[float, dict]] = {}

def get_indian_candles(symbol: str, tf: str = "5m") -> dict:
    """Fetch historical and live intraday candlestick bars for Indian equities and indices."""
    import time
    import yfinance as yf
    raw = (symbol or "SBIN").strip().upper()
    clean = (
        raw.replace("NSE:", "")
        .replace("BSE:", "")
        .replace("-EQ", "")
        .replace("-INDEX", "")
        .strip()
    )
    if clean in ("NIFTY", "NIFTY50", "NIFTY 50", "^NSEI"):
        ticker = "^NSEI"
    elif clean in ("BANKNIFTY", "BANK NIFTY", "^NSEBANK"):
        ticker = "^NSEBANK"
    elif clean in ("FINNIFTY", "FIN NIFTY"):
        ticker = "NIFTY_FIN_SERVICE.NS"
    else:
        ticker = f"{clean}.NS"

    tf_norm = tf.lower().strip()
    interval = "5m"
    period = "3d"
    if tf_norm in ("1", "1m"):
        interval = "1m"
        period = "1d"
    elif tf_norm in ("5", "5m"):
        interval = "5m"
        period = "3d"
    elif tf_norm in ("15", "15m"):
        interval = "15m"
        period = "5d"
    elif tf_norm in ("60", "1h"):
        interval = "1h"
        period = "14d"
    elif tf_norm in ("d", "1d"):
        interval = "1d"
        period = "1y"

    cache_key = f"{ticker}:{interval}"
    now = time.time()
    if cache_key in _CANDLE_CACHE:
        cached_time, cached_val = _CANDLE_CACHE[cache_key]
        if now - cached_time < 10.0:  # 10s memory cache
            return cached_val

    try:
        import datetime
        now_ist = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
        weekday = now_ist.weekday()
        m_minute = now_ist.hour * 60 + now_ist.minute
        is_market_open = (weekday < 5) and (555 <= m_minute <= 930)
        if is_market_open:
            market_status_text = "🟢 Live Market (Session Active: 09:15 – 15:30 IST)"
        elif weekday >= 5:
            market_status_text = "🔴 Market Closed (Weekend · Reopens Monday 09:15 IST)"
        elif m_minute > 930:
            market_status_text = "🔴 Market Closed (Session ended at 15:30 IST · Showing closing candles)"
        else:
            market_status_text = "🟡 Pre-Market (Opens at 09:15 IST)"

        df = yf.download(ticker, period=period, interval=interval, progress=False)
        if df is None or df.empty:
            return {
                "ok": False,
                "symbol": clean,
                "ticker": ticker,
                "is_market_open": is_market_open,
                "market_status_text": market_status_text,
                "candles": [],
                "volume": []
            }
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [col[0] for col in df.columns]

        candles = []
        volume = []
        for ts, row in df.iterrows():
            if interval == "1d":
                t = ts.strftime("%Y-%m-%d")
            else:
                # Add 19800 seconds (5h 30m) so Lightweight Charts renders exact IST market hours (09:15 - 15:30)
                t = int(ts.timestamp()) + 19800
            o = float(row.get("Open", 0.0))
            h = float(row.get("High", 0.0))
            l = float(row.get("Low", 0.0))
            c = float(row.get("Close", 0.0))
            v = float(row.get("Volume", 0.0))
            if o > 0 and h > 0 and l > 0 and c > 0:
                candles.append({
                    "time": t,
                    "open": round(o, 2),
                    "high": round(h, 2),
                    "low": round(l, 2),
                    "close": round(c, 2)
                })
                volume.append({
                    "time": t,
                    "value": v,
                    "color": "rgba(34, 197, 94, 0.45)" if c >= o else "rgba(239, 68, 68, 0.45)"
                })

        res = {
            "ok": True,
            "symbol": clean,
            "ticker": ticker,
            "interval": interval,
            "count": len(candles),
            "is_market_open": is_market_open,
            "market_status_text": market_status_text,
            "candles": candles,
            "volume": volume,
        }
        _CANDLE_CACHE[cache_key] = (now, res)
        return res
    except Exception as exc:
        return {"ok": False, "error": str(exc), "symbol": clean, "ticker": ticker, "candles": [], "volume": []}


_TOPBAR_RIGHT_CHARTS = """<a href="/" class="btn" style="text-decoration:none">← <span class="hide-mobile">Main </span>Dashboard</a>
    <div class="live"><span class="led"></span><span><span class="hide-mobile">NSE </span>Live<span class="hide-mobile"> (TradingView)</span></span></div>"""

INDIAN_CHARTS_PAGE_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='20' fill='%232F9FDB'/%3E%3Cpath d='M25 65 L45 45 L60 55 L80 30' stroke='%23fff' stroke-width='8' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<link rel="alternate icon" href="/favicon.ico">
<title>📈 Indian Markets TradingView Station · ORB Multi-MA Strategy</title>
<style>
__THEME__
/* TradingView Candlestick Station Styling */
.chart-station {
  background: var(--card);
  border: 1px solid var(--line-2);
  border-radius: 12px;
  overflow: hidden;
  margin-top: 16px;
  box-shadow: 0 16px 40px rgba(0,0,0,0.65);
}
.chart-station-header {
  background: var(--cell);
  border-bottom: 1px solid var(--line);
  padding: 12px 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
}
.tf-btn-group {
  display: flex;
  background: var(--bg-2);
  border: 1px solid var(--line);
  border-radius: 7px;
  padding: 2px;
  gap: 2px;
}
.tf-btn-group button {
  background: transparent;
  border: none;
  color: var(--ink-2);
  padding: 4px 10px;
  border-radius: 5px;
  font-size: 11px;
  font-weight: 700;
  cursor: pointer;
  transition: all 0.15s ease;
}
.tf-btn-group button:hover {
  color: var(--ink);
  background: var(--cell);
}
.tf-btn-group button[aria-pressed="true"] {
  background: var(--accent);
  color: #fff;
}
.quick-pill {
  padding: 4px 10px;
  font-size: 11px;
  font-weight: 700;
  border-radius: 6px;
  border: 1px solid var(--line);
  background: var(--cell);
  color: var(--ink);
  cursor: pointer;
  transition: all 0.15s ease;
  white-space: nowrap;
}
.quick-pill:hover {
  border-color: var(--accent);
  color: var(--accent);
}
.quick-pill.active {
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--accent);
}
.council-overlay {
  position: fixed;
  inset: 0;
  background: rgba(4, 7, 15, 0.88);
  backdrop-filter: blur(8px);
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}
.council-modal {
  background: var(--card);
  border: 1px solid var(--line-2);
  border-radius: 14px;
  width: 100%;
  max-width: 900px;
  max-height: 90vh;
  overflow-y: auto;
  box-shadow: 0 24px 70px rgba(0,0,0,0.85);
}
@media (max-width: 768px){
  main{padding:10px 10px 40px !important}
  .chart-station-header{flex-direction:column;align-items:stretch;gap:8px;padding:10px 12px}
  .chart-station-header > div{width:100%;justify-content:space-between}
  .tf-btn-group{width:100%;justify-content:space-between}
  .tf-btn-group button{flex:1;text-align:center;padding:6px 4px}
  .council-modal{max-width:98%;max-height:95vh;padding:12px}
}
</style>
<script src="/static/lightweight-charts.js"></script>
</head>
<body>
__TOPBAR__

<main style="max-width:1600px;margin:0 auto;padding:16px 20px 40px">

  <!-- Live Market Ticker Row -->
  <div style="background:var(--cell);border:1px solid var(--line);border-radius:10px;padding:9px 14px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:14px">
    <div style="display:flex;align-items:center;gap:8px;font-size:11px;font-weight:700;color:var(--ink-3);text-transform:uppercase;letter-spacing:0.06em">
      <span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--good);box-shadow:0 0 8px var(--good)"></span>
      <span>NSE Market Ticker Feed</span>
    </div>
    <div id="liveTickerScroll" style="display:flex;gap:12px;overflow-x:auto;padding-bottom:2px;flex:1;margin:0 12px">
      <!-- Real-time quote cards injected here -->
      <span class="muted" style="font-size:11.5px">Loading live NSE streaming quotes…</span>
    </div>
    <div id="tickerClock" style="font-size:11px;font-family:ui-monospace,monospace;color:var(--ink-3)">Live Stream</div>
  </div>

  <!-- The TradingView Candlestick Chart Station Panel -->
  <div class="chart-station" id="chartStationPanel">
    
    <!-- Station Header -->
    <div class="chart-station-header">
      <div style="display:flex;align-items:center;gap:12px">
        <span style="font-size:24px">📈</span>
        <div>
          <h1 style="margin:0;font-size:16px;font-weight:800;color:var(--ink);letter-spacing:0.02em" id="chartTitle">
            NIFTY 50 Index (NSE) · Live Technical Candlestick Chart
          </h1>
          <div style="font-size:11px;color:var(--ink-3);margin-top:3px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
            <span>Trading Hours: <b>09:15 – 15:30 IST</b></span>
            <span id="marketStatusBadge" class="badge sm bear">🔴 Market Closed (Session ended at 15:30 IST)</span>
          </div>
        </div>
      </div>

      <!-- Toolbar: Timeframes, Search, Strategy Overlay, Pine Script, Fullscreen -->
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        
        <!-- Timeframe selector -->
        <div class="tf-btn-group" id="tfSelector">
          <button onclick="setChartTf('1')" id="tf_1">1m</button>
          <button onclick="setChartTf('5')" id="tf_5" aria-pressed="true">5m</button>
          <button onclick="setChartTf('15')" id="tf_15">15m</button>
          <button onclick="setChartTf('60')" id="tf_60">1h</button>
          <button onclick="setChartTf('D')" id="tf_D">1D</button>
        </div>

        <!-- Mode selector -->
        <div class="tf-btn-group" id="modeSelector">
          <button onclick="setChartMode('native')" id="mode_native" aria-pressed="true" title="High-Speed Native NSE Candlestick Chart (Zero Licensing Restriction)">📊 Live NSE Candles</button>
          <button onclick="setChartMode('tv')" id="mode_tv" title="TradingView Official Free Widget Embed">🌐 TradingView Embed</button>
        </div>

        <!-- Symbol Search input -->
        <div style="display:flex;align-items:center;gap:4px">
          <input type="text" id="symbolInput" placeholder="Symbol (e.g. SBIN, RELIANCE, DIXON)" style="background:var(--card);border:1px solid var(--line);border-radius:6px;padding:5px 9px;color:var(--ink);font-size:11px;width:190px" onkeydown="if(event.key==='Enter') searchSymbolChart()">
          <button class="btn sm" onclick="searchSymbolChart()" style="padding:5px 9px">Go</button>
        </div>

        <!-- Strategy Buttons -->
        <button class="btn sm" onclick="toggleStrategyOverlay()" id="btnToggleOverlay" title="Toggle On-Chart Strategy Indicator Overlay" style="border-color:var(--accent);color:var(--accent);font-weight:700">🎯 Strategy Overlay</button>
        <button class="btn sm" id="btnPineScript" onclick="openPineModal()" title="View &amp; Copy TradingView Pine Script Indicator" style="border-color:var(--good);color:var(--good)">📜 Pine Script</button>
        <button class="btn sm" id="btnCouncilAudit" onclick="openCouncilModalForSymbol()" style="background:linear-gradient(135deg,var(--accent),#4f46e5);color:#fff;border:none;font-weight:700">🤖 4-Agent Debate</button>
        <a class="btn sm" id="tvExternalLink" target="_blank" href="https://in.tradingview.com/chart/?symbol=NSE:NIFTY" title="Open official TradingView.com Chart" style="text-decoration:none;border-color:var(--accent);color:var(--accent);display:inline-flex;align-items:center;gap:4px">↗ TradingView.com</a>
        <a class="btn sm" target="_blank" href="https://web.dhan.co/" title="Open Dhan Web Options &amp; Stock Charts" style="text-decoration:none;border-color:#38bdf8;color:#38bdf8;display:inline-flex;align-items:center;gap:4px">↗ Dhan Options</a>
        <button class="btn sm" onclick="openChartModal()" title="View in full-screen modal">⤢ Fullscreen</button>
      </div>
    </div>

    <!-- Quick Switcher Bar -->
    <div style="background:var(--bg-2);padding:9px 14px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px;overflow-x:auto;font-size:11px">
      <span style="color:var(--ink-3);font-size:10px;text-transform:uppercase;font-weight:700">👑 Indices:</span>
      <button class="quick-pill active" onclick="selectChartSymbol('NSE:NIFTY')" id="pill_NIFTY">NIFTY 50</button>
      <button class="quick-pill" onclick="selectChartSymbol('NSE:BANKNIFTY')" id="pill_BANKNIFTY">BANK NIFTY</button>
      <button class="quick-pill" onclick="selectChartSymbol('NSE:FINNIFTY')" id="pill_FINNIFTY">FIN NIFTY</button>

      <span style="border-left:1px solid var(--line);margin:0 4px;height:16px"></span>
      <span style="color:var(--ink-3);font-size:10px;text-transform:uppercase;font-weight:700">🏦 Banking:</span>
      <button class="quick-pill" onclick="selectChartSymbol('NSE:SBIN')" id="pill_SBIN">SBIN</button>
      <button class="quick-pill" onclick="selectChartSymbol('NSE:HDFCBANK')" id="pill_HDFCBANK">HDFCBANK</button>
      <button class="quick-pill" onclick="selectChartSymbol('NSE:ICICIBANK')" id="pill_ICICIBANK">ICICIBANK</button>
      <button class="quick-pill" onclick="selectChartSymbol('NSE:KOTAKBANK')" id="pill_KOTAKBANK">KOTAKBANK</button>
      <button class="quick-pill" onclick="selectChartSymbol('NSE:AXISBANK')" id="pill_AXISBANK">AXISBANK</button>

      <span style="border-left:1px solid var(--line);margin:0 4px;height:16px"></span>
      <span style="color:var(--ink-3);font-size:10px;text-transform:uppercase;font-weight:700">⚡ Heavyweights:</span>
      <button class="quick-pill" onclick="selectChartSymbol('NSE:RELIANCE')" id="pill_RELIANCE">RELIANCE</button>
      <button class="quick-pill" onclick="selectChartSymbol('NSE:TCS')" id="pill_TCS">TCS</button>
      <button class="quick-pill" onclick="selectChartSymbol('NSE:INFY')" id="pill_INFY">INFY</button>
      <button class="quick-pill" onclick="selectChartSymbol('NSE:LT')" id="pill_LT">L&amp;T</button>
      <button class="quick-pill" onclick="selectChartSymbol('NSE:BHARTIARTL')" id="pill_BHARTIARTL">BHARTI AIRTEL</button>
      <button class="quick-pill" onclick="selectChartSymbol('NSE:TATAMOTORS')" id="pill_TATAMOTORS">TATA MOTORS</button>
      <button class="quick-pill" onclick="selectChartSymbol('NSE:DIXON')" id="pill_DIXON">DIXON</button>
      <button class="quick-pill" onclick="selectChartSymbol('NSE:TITAN')" id="pill_TITAN">TITAN</button>
      <button class="quick-pill" onclick="selectChartSymbol('NSE:TRENT')" id="pill_TRENT">TRENT</button>
    </div>

    <!-- Live Strategy Parameters & Confluence HUD -->
    <div id="chartParametersHud" style="background:var(--card);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;flex-direction:column;gap:10px">
      
      <!-- Top Badges -->
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
        <div style="display:flex;align-items:center;gap:10px">
          <span style="font-size:11.5px;font-weight:700;color:var(--ink-3);text-transform:uppercase;letter-spacing:.06em">🎯 Strategy Confluence HUD:</span>
          <span id="hudActionBadge" class="badge sm ok" style="font-weight:700;font-size:11px">Analyzing Strategy Parameters…</span>
          <span id="hudScoreBadge" class="badge sm" style="font-weight:700;font-size:11px;background:var(--cell-2);color:var(--ink-2)">Score: --/100</span>
        </div>
        <div style="display:flex;align-items:center;gap:12px;font-size:11px;color:var(--ink-3)">
          <span id="hudPriceBadge" style="font-family:ui-monospace,monospace;font-weight:700;font-size:13.5px;color:var(--ink)">LTP: ₹--</span>
          <span id="hudUpdateTime">Live 3s Refresh</span>
        </div>
      </div>

      <!-- 6 Metrics Cards Grid -->
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px" id="hudCardsGrid">
        
        <!-- 9/21 EMA -->
        <div style="background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:9px 12px">
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:10.5px;color:var(--ink-3);font-weight:700;text-transform:uppercase">
            <span>📈 9/21 EMA Trend</span>
            <span id="hudEmaPill" class="badge sm" style="font-size:9px">--</span>
          </div>
          <div style="font-size:12.5px;font-weight:700;font-family:ui-monospace,monospace;color:var(--ink);margin-top:4px" id="hudEmaValues">₹-- / ₹--</div>
          <div style="font-size:10px;color:var(--ink-3);margin-top:2px" id="hudEmaNote">Fast 9 vs Slow 21 EMA</div>
        </div>

        <!-- VWAP -->
        <div style="background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:9px 12px">
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:10.5px;color:var(--ink-3);font-weight:700;text-transform:uppercase">
            <span>🎯 Session VWAP</span>
            <span id="hudVwapPill" class="badge sm" style="font-size:9px">--</span>
          </div>
          <div style="font-size:12.5px;font-weight:700;font-family:ui-monospace,monospace;color:var(--ink);margin-top:4px" id="hudVwapVal">₹--</div>
          <div style="font-size:10px;color:var(--ink-3);margin-top:2px" id="hudVwapNote">Institutional Fair Value</div>
        </div>

        <!-- Trend Filters -->
        <div style="background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:9px 12px">
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:10.5px;color:var(--ink-3);font-weight:700;text-transform:uppercase">
            <span>⚓ Trend Filters (Daily + HTF)</span>
            <span id="hudSmaPill" class="badge sm" style="font-size:9px">--</span>
          </div>
          <div style="font-size:11.5px;font-weight:700;font-family:ui-monospace,monospace;color:var(--ink);margin-top:4px" id="hudSmaVal">₹-- / ₹--</div>
          <div style="font-size:10px;color:var(--ink-3);margin-top:2px" id="hudSmaNote">Daily 50/200 &amp; 30m/1H 200 SMA</div>
        </div>

        <!-- 09:15-09:30 Opening Range & Bias -->
        <div style="background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:9px 12px">
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:10.5px;color:var(--ink-3);font-weight:700;text-transform:uppercase">
            <span>⏱️ 15m Range &amp; Bias</span>
            <span id="hudRangePill" class="badge sm" style="font-size:9px">--</span>
          </div>
          <div style="font-size:11.5px;font-weight:700;font-family:ui-monospace,monospace;color:var(--ink);margin-top:4px" id="hudRangeVals">H: ₹-- | L: ₹--</div>
          <div style="font-size:10px;color:var(--ink-3);margin-top:2px" id="hudRangeWidth">Width: --% | Bias: --</div>
        </div>

        <!-- Volume (RVOL >= 1.5x) -->
        <div style="background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:9px 12px">
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:10.5px;color:var(--ink-3);font-weight:700;text-transform:uppercase">
            <span>⚡ Volume (RVOL &ge; 1.5x)</span>
            <span id="hudRvolPill" class="badge sm" style="font-size:9px">--</span>
          </div>
          <div style="font-size:12.5px;font-weight:700;font-family:ui-monospace,monospace;color:var(--ink);margin-top:4px" id="hudRvolVal">--x Avg</div>
          <div style="font-size:10px;color:var(--ink-3);margin-top:2px" id="hudRvolNote">Institutional 1.5x Volume Filter</div>
        </div>

        <!-- Daily 365 MA Wall -->
        <div style="background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:9px 12px">
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:10.5px;color:var(--ink-3);font-weight:700;text-transform:uppercase">
            <span>🧱 Daily 365 MA Wall</span>
            <span id="hudWallPill" class="badge sm ok" style="font-size:9px">--</span>
          </div>
          <div style="font-size:11.5px;font-weight:700;font-family:ui-monospace,monospace;color:var(--ink);margin-top:4px" id="hudWallVal">₹--</div>
          <div style="font-size:10px;color:var(--ink-3);margin-top:2px" id="hudWallNote">0.5% Wall Safety Gate</div>
        </div>
      </div>

      <!-- Strategy Triggers & Levels Banner -->
      <div style="background:var(--bg-2);border:1px solid var(--line);border-radius:8px;padding:8px 12px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;font-size:11.5px">
        <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
          <span style="color:var(--ink-3);font-weight:700;font-size:10px;text-transform:uppercase">ORB + Multi-MA Execution Levels:</span>
          <div>🟢 <b>Long Breakout:</b> <span style="font-family:ui-monospace,monospace;color:var(--good)" id="hudLongTrigger">>₹--</span></div>
          <div>🔴 <b>Short Breakdown:</b> <span style="font-family:ui-monospace,monospace;color:var(--bad)" id="hudShortTrigger"><₹--</span></div>
          <div>🛡️ <b>Opposite Stop:</b> <span style="font-family:ui-monospace,monospace;color:var(--bad)" id="hudPlannedSl">₹--</span></div>
          <div>🎯 <b>1:1.5 Target:</b> <span style="font-family:ui-monospace,monospace;color:var(--good)" id="hudPlannedT1">₹--</span></div>
          <div>🎯 <b>1:2.0 Extended:</b> <span style="font-family:ui-monospace,monospace;color:var(--good)" id="hudPlannedT2">₹--</span></div>
        </div>
        <div id="hudReasonsList" style="font-size:10.5px;color:var(--ink-2);display:flex;gap:4px;flex-wrap:wrap">
          <!-- Live Reasons pills injected -->
        </div>
      </div>
    </div>

    <!-- Chart Container -->
    <div style="height:620px;width:100%;background:var(--card);position:relative;overflow:hidden" id="chartContainer">
      <div id="tradingview_embed_box" style="width:100%;height:100%"></div>

      <!-- Strategy Indicator Floating Chart Overlay -->
      <div id="chartStrategyOverlay" style="position:absolute;top:12px;right:65px;z-index:20;background:rgba(11,18,32,0.92);backdrop-filter:blur(10px);border:1px solid var(--line);border-radius:9px;padding:10px 14px;box-shadow:0 8px 30px rgba(0,0,0,0.65);font-size:11px;min-width:280px;max-width:330px;pointer-events:auto;transition:all 0.2s ease">
        <div style="display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid rgba(255,255,255,0.08);padding-bottom:6px;margin-bottom:8px">
          <div style="display:flex;align-items:center;gap:6px">
            <span style="font-size:12px">🎯</span>
            <span style="font-weight:700;font-size:11px;color:var(--ink);letter-spacing:0.04em;text-transform:uppercase">Strategy Indicator Overlay</span>
          </div>
          <button onclick="toggleStrategyOverlayCollapse()" id="btnOverlayCollapse" style="background:transparent;border:none;color:var(--ink-3);cursor:pointer;font-size:11px;padding:0 4px" title="Collapse / Expand">▼</button>
        </div>
        <div id="chartOverlayBody" style="display:flex;flex-direction:column;gap:5px">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span style="color:var(--good);font-weight:600">🎯 1:1.5 Target:</span>
            <span style="font-family:ui-monospace,monospace;font-weight:700;color:var(--good)" id="coTarget">₹--</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span style="color:#22c55e;font-weight:600">🟢 OR Breakout High:</span>
            <span style="font-family:ui-monospace,monospace;font-weight:700;color:#22c55e" id="coOrHigh">₹--</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span style="color:#eab308;font-weight:600">🟡 Session VWAP:</span>
            <span style="font-family:ui-monospace,monospace;font-weight:700;color:#eab308" id="coVwap">₹--</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span style="color:#a855f7;font-weight:600">🟣 200 SMA (30m/1H):</span>
            <span style="font-family:ui-monospace,monospace;font-weight:700;color:#a855f7" id="coSma">₹--</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span style="color:#ef4444;font-weight:600">🔴 OR Breakdown Low:</span>
            <span style="font-family:ui-monospace,monospace;font-weight:700;color:#ef4444" id="coOrLow">₹--</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span style="color:#ec4899;font-weight:600">🧱 Daily 365 MA Wall:</span>
            <span style="font-family:ui-monospace,monospace;font-weight:700;color:#ec4899" id="coWall">₹--</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center;border-top:1px dashed rgba(255,255,255,0.08);padding-top:5px;margin-top:2px">
            <span style="color:var(--bad);font-weight:600">🛡️ Planned Stop:</span>
            <span style="font-family:ui-monospace,monospace;font-weight:700;color:var(--bad)" id="coSl">₹--</span>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Fullscreen Chart Modal -->
  <div id="chartFullscreenModal" class="council-overlay" style="display:none">
    <div style="max-width:96vw;height:90vh;margin:20px auto;background:var(--card);border:1px solid var(--line-2);border-radius:12px;overflow:hidden;display:flex;flex-direction:column;box-shadow:0 24px 60px rgba(0,0,0,0.85)">
      <div style="background:var(--cell);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;align-items:center;justify-content:space-between">
        <div style="display:flex;align-items:center;gap:10px">
          <span style="font-size:20px">📊</span>
          <span style="font-weight:700;font-size:15px;color:var(--ink)" id="fsChartTitle">NIFTY 50 — Real-Time Candlestick Chart</span>
        </div>
        <button class="btn sm" onclick="closeChartModal()" style="padding:5px 12px">✕ Close Fullscreen</button>
      </div>
      <div id="fsChartContainer" style="flex:1;width:100%;background:#04070f;position:relative"></div>
    </div>
  </div>

  <!-- TradingView Pine Script Modal -->
  <div id="pineScriptModal" class="council-overlay" style="display:none">
    <div style="max-width:820px;width:92vw;margin:30px auto;background:var(--card);border:1px solid var(--line-2);border-radius:12px;overflow:hidden;box-shadow:0 24px 60px rgba(0,0,0,0.85);display:flex;flex-direction:column">
      <div style="background:var(--cell);border-bottom:1px solid var(--line);padding:14px 20px;display:flex;align-items:center;justify-content:space-between">
        <div style="display:flex;align-items:center;gap:10px">
          <span style="font-size:22px">📜</span>
          <div>
            <div style="font-weight:700;font-size:15px;color:var(--ink)">TradingView Pine Script (ORB + Multi-MA + 365 Wall)</div>
            <div style="font-size:11px;color:var(--ink-3)">Copy this Pine Script v5 code into TradingView's Pine Editor to plot the exact strategy indicator directly on your chart</div>
          </div>
        </div>
        <button class="btn sm" onclick="closePineModal()" style="padding:5px 12px">✕ Close</button>
      </div>
      <div style="padding:16px 20px;display:flex;flex-direction:column;gap:12px">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
          <span style="font-size:12px;color:var(--ink-2)"><b>Step 1:</b> Click Copy Code below · <b>Step 2:</b> On TradingView, click <i>Pine Editor</i> at the bottom · <b>Step 3:</b> Paste &amp; click <i>Add to chart</i></span>
          <button class="btn sm go" onclick="copyPineCode()" style="padding:6px 16px;font-weight:700">📋 Copy Pine Script Code</button>
        </div>
        <textarea id="pineCodeArea" readonly style="width:100%;height:320px;background:#030712;color:#38bdf8;font-family:ui-monospace,monospace;font-size:11.5px;padding:12px;border:1px solid var(--line);border-radius:8px;line-height:1.5;resize:vertical"></textarea>
      </div>
    </div>
  </div>

  <!-- 4-Agent AI Council Debate Modal -->
  <div id="councilModal" class="council-overlay" style="display:none">
    <div class="council-modal">
      <div style="background:var(--cell);border-bottom:1px solid var(--line);padding:14px 20px;display:flex;align-items:center;justify-content:space-between">
        <div style="display:flex;align-items:center;gap:12px">
          <span style="font-size:24px">🤖</span>
          <div>
            <div style="font-weight:700;font-size:15px;color:var(--ink)" id="cmTitle">4-Agent AI Council Audit</div>
            <div style="font-size:11px;color:var(--ink-3)">Technical Analyst, Bullish Researcher, Bearish Researcher, and Trader Decision Engine</div>
          </div>
        </div>
        <button class="btn sm" onclick="closeCouncilModal()" style="padding:5px 12px">✕ Close</button>
      </div>
      <div id="cmContent" style="padding:20px;display:flex;flex-direction:column;gap:16px">
        <div class="muted">Running Council debate…</div>
      </div>
    </div>
  </div>

</main>

<script>
let ACTIVE_CHART_SYMBOL = "NSE:NIFTY";
let ACTIVE_CHART_TF = "5";
let ACTIVE_CHART_MODE = "native";
let STRATEGY_OVERLAY_VISIBLE = true;
let STRATEGY_OVERLAY_COLLAPSED = false;
let LAST_STRATEGY_PARAMS = null;
let NATIVE_CHART = null;
let CANDLE_SERIES = null;
let VOLUME_SERIES = null;
let ACTIVE_PRICE_LINES = [];

const ASSET_NAMES = {
  "NSE:NIFTY": "NIFTY 50 Index (NSE)",
  "NSE:BANKNIFTY": "BANK NIFTY Index (NSE)",
  "NSE:FINNIFTY": "NIFTY Financial Services (NSE)",
  "NSE:SBIN": "State Bank of India (SBIN)",
  "NSE:RELIANCE": "Reliance Industries Ltd (RELIANCE)",
  "NSE:HDFCBANK": "HDFC Bank Ltd (HDFCBANK)",
  "NSE:ICICIBANK": "ICICI Bank Ltd (ICICIBANK)",
  "NSE:KOTAKBANK": "Kotak Mahindra Bank (KOTAKBANK)",
  "NSE:AXISBANK": "Axis Bank Ltd (AXISBANK)",
  "NSE:TCS": "Tata Consultancy Services (TCS)",
  "NSE:INFY": "Infosys Ltd (INFY)",
  "NSE:LT": "Larsen & Toubro Ltd (LT)",
  "NSE:BHARTIARTL": "Bharti Airtel Ltd (BHARTIARTL)",
  "NSE:TATAMOTORS": "Tata Motors Ltd (TATAMOTORS)",
  "NSE:DIXON": "Dixon Technologies Ltd (DIXON)",
  "NSE:TITAN": "Titan Company Ltd (TITAN)",
  "NSE:TRENT": "Trent Ltd (TRENT)",
};

function fmt(n, d = 2) {
  if (n === null || n === undefined || isNaN(n)) return "0.00";
  return Number(n).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function getTradingViewSymbol(sym) {
  const upper = (sym || "NSE:NIFTY").toUpperCase().trim();
  if (upper === "NIFTY" || upper === "NSE:NIFTY") return "NSE:NIFTY";
  if (upper === "BANKNIFTY" || upper === "NSE:BANKNIFTY") return "NSE:BANKNIFTY";
  if (upper === "FINNIFTY" || upper === "NSE:FINNIFTY") return "NSE:FINNIFTY";
  if (upper.startsWith("NSE:") || upper.startsWith("BSE:")) return upper;
  return "NSE:" + upper;
}

function selectChartSymbol(sym) {
  if (!sym) return;
  ACTIVE_CHART_SYMBOL = sym.trim();
  const clean = ACTIVE_CHART_SYMBOL.replace("NSE:", "").replace("BSE:", "");

  const titleEl = document.getElementById("chartTitle");
  if (titleEl) {
    titleEl.textContent = (ASSET_NAMES[ACTIVE_CHART_SYMBOL] || (clean + " (NSE)")) + " · Live Technical Candlestick Chart";
  }
  const fsTitle = document.getElementById("fsChartTitle");
  if (fsTitle) {
    fsTitle.textContent = (ASSET_NAMES[ACTIVE_CHART_SYMBOL] || clean) + " — Real-Time Candlestick Chart";
  }

  // Update external link
  const tvLink = document.getElementById("tvExternalLink");
  if (tvLink) {
    tvLink.href = "https://in.tradingview.com/chart/?symbol=" + encodeURIComponent(ACTIVE_CHART_SYMBOL);
  }

  // Update pill active states
  document.querySelectorAll(".quick-pill").forEach(p => p.classList.remove("active"));
  const activePill = document.getElementById("pill_" + clean);
  if (activePill) activePill.classList.add("active");

  renderActiveChart("tradingview_embed_box", ACTIVE_CHART_SYMBOL, ACTIVE_CHART_TF);

  // Update URL without reload
  const newUrl = new URL(window.location);
  newUrl.searchParams.set("symbol", clean);
  window.history.replaceState({}, "", newUrl);

  fetchActiveSymbolParameters(clean);
}

function searchSymbolChart() {
  const inp = document.getElementById("symbolInput");
  if (!inp || !inp.value.trim()) return;
  selectChartSymbol(inp.value.trim());
  inp.value = "";
}

function setChartTf(tf) {
  ACTIVE_CHART_TF = tf;
  document.querySelectorAll("#tfSelector button").forEach(b => b.removeAttribute("aria-pressed"));
  const btn = document.getElementById("tf_" + tf);
  if (btn) btn.setAttribute("aria-pressed", "true");
  renderActiveChart("tradingview_embed_box", ACTIVE_CHART_SYMBOL, ACTIVE_CHART_TF);
  const fsModal = document.getElementById("chartFullscreenModal");
  if (fsModal && fsModal.style.display === "flex") {
    renderActiveChart("fsChartContainer", ACTIVE_CHART_SYMBOL, ACTIVE_CHART_TF);
  }
}

function setChartMode(mode) {
  ACTIVE_CHART_MODE = mode;
  document.querySelectorAll("#modeSelector button").forEach(b => b.removeAttribute("aria-pressed"));
  const btn = document.getElementById("mode_" + mode);
  if (btn) btn.setAttribute("aria-pressed", "true");
  renderActiveChart("tradingview_embed_box", ACTIVE_CHART_SYMBOL, ACTIVE_CHART_TF);
  const fsModal = document.getElementById("chartFullscreenModal");
  if (fsModal && fsModal.style.display === "flex") {
    renderActiveChart("fsChartContainer", ACTIVE_CHART_SYMBOL, ACTIVE_CHART_TF);
  }
}

function clearPriceLines() {
  if (!CANDLE_SERIES) return;
  ACTIVE_PRICE_LINES.forEach(l => {
    try { CANDLE_SERIES.removePriceLine(l); } catch(e) {}
  });
  ACTIVE_PRICE_LINES = [];
}

function applyStrategyLinesToNativeChart() {
  if (!CANDLE_SERIES || !LAST_STRATEGY_PARAMS) return;
  clearPriceLines();
  const d = LAST_STRATEGY_PARAMS;

  if (d.planned_target1 && typeof LightweightCharts !== "undefined") {
    ACTIVE_PRICE_LINES.push(CANDLE_SERIES.createPriceLine({
      price: d.planned_target1,
      color: "#10b981",
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      axisLabelVisible: true,
      title: "🎯 1:1.5 Target",
    }));
  }

  if (d.range_high && typeof LightweightCharts !== "undefined") {
    ACTIVE_PRICE_LINES.push(CANDLE_SERIES.createPriceLine({
      price: d.range_high,
      color: "#22c55e",
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Solid,
      axisLabelVisible: true,
      title: "🟢 OR Breakout",
    }));
  }

  if (d.vwap && typeof LightweightCharts !== "undefined") {
    ACTIVE_PRICE_LINES.push(CANDLE_SERIES.createPriceLine({
      price: d.vwap,
      color: "#eab308",
      lineWidth: 1.5,
      lineStyle: LightweightCharts.LineStyle.Solid,
      axisLabelVisible: true,
      title: "🟡 VWAP",
    }));
  }

  const sma = d.sma200_1h || d.sma200_30m;
  if (sma && typeof LightweightCharts !== "undefined") {
    ACTIVE_PRICE_LINES.push(CANDLE_SERIES.createPriceLine({
      price: sma,
      color: "#a855f7",
      lineWidth: 1.5,
      lineStyle: LightweightCharts.LineStyle.Dotted,
      axisLabelVisible: true,
      title: "🟣 200 SMA",
    }));
  }

  if (d.range_low && typeof LightweightCharts !== "undefined") {
    ACTIVE_PRICE_LINES.push(CANDLE_SERIES.createPriceLine({
      price: d.range_low,
      color: "#ef4444",
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Solid,
      axisLabelVisible: true,
      title: "🔴 OR Breakdown",
    }));
  }

  if (d.daily_365_wall && typeof LightweightCharts !== "undefined") {
    ACTIVE_PRICE_LINES.push(CANDLE_SERIES.createPriceLine({
      price: d.daily_365_wall,
      color: "#ec4899",
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Dotted,
      axisLabelVisible: true,
      title: "🧱 365 Wall",
    }));
  }

  if (d.planned_sl && typeof LightweightCharts !== "undefined") {
    ACTIVE_PRICE_LINES.push(CANDLE_SERIES.createPriceLine({
      price: d.planned_sl,
      color: "#f43f5e",
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      axisLabelVisible: true,
      title: "🛡️ Stop Loss",
    }));
  }
}

async function renderNativeLightweightChart(containerId, sym, tf) {
  const container = document.getElementById(containerId);
  if (!container) return;
  container.innerHTML = "";

  if (typeof LightweightCharts === "undefined") {
    container.innerHTML = "<div style='display:flex;align-items:center;justify-content:center;height:100%;color:var(--ink-2)'><span class='spin' style='margin-right:8px'></span> Loading Lightweight Candlestick Engine…</div>";
    return;
  }

  const clean = (sym || "SBIN").replace("NSE:", "").replace("BSE:", "");

  const chart = LightweightCharts.createChart(container, {
    width: container.clientWidth || 1000,
    height: container.clientHeight || 620,
    layout: {
      background: { color: "#070b14" },
      textColor: "#94a3b8",
      fontSize: 11,
      fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
    },
    grid: {
      vertLines: { color: "rgba(255, 255, 255, 0.04)" },
      horzLines: { color: "rgba(255, 255, 255, 0.04)" },
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
    },
    rightPriceScale: {
      borderColor: "rgba(255, 255, 255, 0.1)",
    },
    timeScale: {
      borderColor: "rgba(255, 255, 255, 0.1)",
      timeVisible: true,
      secondsVisible: false,
    },
    localization: {
      dateFormat: "yyyy-MM-dd",
      locale: "en-IN",
      timeFormatter: (timestamp) => {
        if (typeof timestamp === "string") return timestamp;
        const d = new Date(timestamp * 1000);
        const hh = String(d.getUTCHours()).padStart(2, "0");
        const mm = String(d.getUTCMinutes()).padStart(2, "0");
        return `${hh}:${mm} IST`;
      },
    },
  });

  const candleSeries = chart.addCandlestickSeries({
    upColor: "#22c55e",
    downColor: "#ef4444",
    borderVisible: false,
    wickUpColor: "#22c55e",
    wickDownColor: "#ef4444",
  });

  const volumeSeries = chart.addHistogramSeries({
    color: "#38bdf8",
    priceFormat: {
      type: "volume",
    },
    priceScaleId: "",
    scaleMargins: {
      top: 0.8,
      bottom: 0,
    },
  });

  NATIVE_CHART = chart;
  CANDLE_SERIES = candleSeries;
  VOLUME_SERIES = volumeSeries;

  const ro = new ResizeObserver(entries => {
    for (const entry of entries) {
      if (entry.contentRect.width > 0 && entry.contentRect.height > 0) {
        chart.applyOptions({ width: entry.contentRect.width, height: entry.contentRect.height });
      }
    }
  });
  ro.observe(container);

  try {
    const res = await fetch(`/api/indian/candles?symbol=${encodeURIComponent(clean)}&tf=${encodeURIComponent(tf)}`);
    const data = await res.json();
    if (data.ok && data.candles && data.candles.length > 0) {
      candleSeries.setData(data.candles);
      if (data.volume) volumeSeries.setData(data.volume);
      chart.timeScale().fitContent();

      if (data.market_status_text) {
        const ms = document.getElementById("marketStatusBadge");
        if (ms) {
          ms.textContent = data.market_status_text;
          ms.className = `badge sm ${data.is_market_open ? 'bull' : 'bear'}`;
        }
      }
    }
  } catch (err) {
    console.warn("Failed to load candles:", err);
  }

  applyStrategyLinesToNativeChart();
}

function renderTradingViewWidget(containerId, sym, tf) {
  const tvSym = getTradingViewSymbol(sym);
  const container = document.getElementById(containerId);
  if (!container) return;
  container.innerHTML = "";

  const widgetUrl = `https://s.tradingview.com/widgetembed/?frameElementId=tradingview_${Date.now()}&symbol=${encodeURIComponent(tvSym)}&interval=${tf}&hidesidetoolbar=0&symboledit=1&saveimage=1&toolbarbg=0b1220&studies=VWAP%40tv-basicstudies%1FVolume%40tv-basicstudies%1FMASimple%40tv-basicstudies%1FRSI%40tv-basicstudies&theme=dark&style=1&timezone=Asia%2FKolkata`;

  const iframe = document.createElement("iframe");
  iframe.style.width = "100%";
  iframe.style.height = "100%";
  iframe.style.border = "none";
  iframe.src = widgetUrl;
  container.appendChild(iframe);
}

function renderActiveChart(containerId, sym, tf) {
  if (ACTIVE_CHART_MODE === "tv") {
    renderTradingViewWidget(containerId, sym, tf);
  } else {
    renderNativeLightweightChart(containerId, sym, tf);
  }
}

async function fetchActiveSymbolParameters(sym) {
  try {
    const s = sym || ACTIVE_CHART_SYMBOL.replace("NSE:", "");
    const res = await fetch(`/api/indian/parameters?symbol=${encodeURIComponent(s)}`);
    if (!res.ok) return;
    const d = await res.json();
    if (!d.ok) return;

    // Badges
    const actBadge = document.getElementById("hudActionBadge");
    if (actBadge) {
      actBadge.className = `badge sm ${d.action_badge}`;
      actBadge.textContent = d.action;
    }
    const scoreBadge = document.getElementById("hudScoreBadge");
    if (scoreBadge) {
      scoreBadge.textContent = `Score: ${d.score}/100 (Conviction ${d.conviction}/10)`;
    }
    const priceBadge = document.getElementById("hudPriceBadge");
    if (priceBadge) {
      priceBadge.textContent = `LTP: ₹${fmt(d.mark_price)}`;
    }
    const updateEl = document.getElementById("hudUpdateTime");
    if (updateEl) {
      updateEl.textContent = `Live: ${d.updated_at || new Date().toLocaleTimeString('en-IN')}`;
    }

    // 1. EMA
    const isEmaBull = d.ema_9 >= d.ema_21;
    const emaPill = document.getElementById("hudEmaPill");
    if (emaPill) {
      emaPill.className = `badge sm ${isEmaBull ? 'bull' : 'bear'}`;
      emaPill.textContent = isEmaBull ? "🟢 Bullish" : "🔴 Bearish";
    }
    const emaVal = document.getElementById("hudEmaValues");
    if (emaVal) emaVal.textContent = `₹${fmt(d.ema_9)} / ₹${fmt(d.ema_21)}`;
    const emaNote = document.getElementById("hudEmaNote");
    if (emaNote) emaNote.textContent = d.ema_trend;

    // 2. VWAP
    const isAboveVwap = d.mark_price >= d.vwap;
    const vwapPill = document.getElementById("hudVwapPill");
    if (vwapPill) {
      vwapPill.className = `badge sm ${isAboveVwap ? 'bull' : 'bear'}`;
      vwapPill.textContent = isAboveVwap ? "Above" : "Below";
    }
    const vwapVal = document.getElementById("hudVwapVal");
    if (vwapVal) vwapVal.textContent = `₹${fmt(d.vwap)}`;
    const vwapNote = document.getElementById("hudVwapNote");
    if (vwapNote) vwapNote.textContent = d.vwap_status;

    // 3. Trend Filters
    const isTrendUp = d.trend_gate === "UP";
    const smaPill = document.getElementById("hudSmaPill");
    if (smaPill) {
      smaPill.className = `badge sm ${isTrendUp ? 'bull' : (d.trend_gate === 'DOWN' ? 'bear' : 'caution')}`;
      smaPill.textContent = isTrendUp ? "Uptrend Gate" : (d.trend_gate === 'DOWN' ? "Downtrend Gate" : "Mixed Trend");
    }
    const smaVal = document.getElementById("hudSmaVal");
    if (smaVal) {
      if (d.daily_50 && d.daily_200) {
        smaVal.textContent = `D50:₹${fmt(d.daily_50)} | D200:₹${fmt(d.daily_200)}`;
      } else {
        smaVal.textContent = `1H:₹${fmt(d.sma200_1h || d.mark_price)} | 30m:₹${fmt(d.sma200_30m || d.mark_price)}`;
      }
    }
    const smaNote = document.getElementById("hudSmaNote");
    if (smaNote) smaNote.textContent = `1H 200: ₹${fmt(d.sma200_1h)} · 30m 200: ₹${fmt(d.sma200_30m)}`;

    // 4. Session Range & Bias
    const rangePill = document.getElementById("hudRangePill");
    if (rangePill) {
      const isGreen = d.or_bias === "GREEN";
      const isRed = d.or_bias === "RED";
      rangePill.className = `badge sm ${isGreen ? 'bull' : (isRed ? 'bear' : 'ok')}`;
      rangePill.textContent = isGreen ? "🟢 Green Bias" : (isRed ? "🔴 Red Bias" : "⚪ Neutral");
    }
    const rangeVals = document.getElementById("hudRangeVals");
    if (rangeVals) rangeVals.textContent = `H: ₹${fmt(d.range_high)} | L: ₹${fmt(d.range_low)}`;
    const rangeWidth = document.getElementById("hudRangeWidth");
    if (rangeWidth) rangeWidth.textContent = `Width: ${d.range_width_pct}% ${d.or_valid ? '(Valid Range)' : '(Out of Bounds)'}`;

    // 5. Volume RVOL
    const isVolSurge = d.rvol >= 1.5;
    const rvolPill = document.getElementById("hudRvolPill");
    if (rvolPill) {
      rvolPill.className = `badge sm ${isVolSurge ? 'bull' : 'caution'}`;
      rvolPill.textContent = isVolSurge ? "Surge (>=1.5x)" : "Subdued";
    }
    const rvolVal = document.getElementById("hudRvolVal");
    if (rvolVal) rvolVal.textContent = `${d.rvol}x Volume`;
    const rvolNote = document.getElementById("hudRvolNote");
    if (rvolNote) rvolNote.textContent = d.rvol_status;

    // 6. Daily 365 MA Wall
    const wallPill = document.getElementById("hudWallPill");
    if (wallPill) {
      wallPill.className = `badge sm ${d.wall_safe ? 'ok' : 'bear'}`;
      wallPill.textContent = d.wall_safe ? "🟢 Safe" : "🔴 Wall Block";
    }
    const wallVal = document.getElementById("hudWallVal");
    if (wallVal) {
      wallVal.textContent = d.daily_365_wall ? `Wall: ₹${fmt(d.daily_365_wall)}` : "No Wall Nearby";
    }
    const wallNote = document.getElementById("hudWallNote");
    if (wallNote) wallNote.textContent = d.wall_note;

    // Triggers & Levels
    const longTrig = document.getElementById("hudLongTrigger");
    if (longTrig) longTrig.textContent = `> ₹${fmt(d.long_trigger)}`;
    const shortTrig = document.getElementById("hudShortTrigger");
    if (shortTrig) shortTrig.textContent = `< ₹${fmt(d.short_trigger)}`;
    const slEl = document.getElementById("hudPlannedSl");
    if (slEl) slEl.textContent = `₹${fmt(d.planned_sl)}`;
    const t1El = document.getElementById("hudPlannedT1");
    if (t1El) t1El.textContent = `₹${fmt(d.planned_target1)} (1:1.5 RR)`;
    const t2El = document.getElementById("hudPlannedT2");
    if (t2El) t2El.textContent = `₹${fmt(d.planned_target2)} (1:2.0 RR)`;

    // Floating On-Chart Overlay values
    const coTarget = document.getElementById("coTarget");
    if (coTarget) coTarget.textContent = `₹${fmt(d.planned_target1)}`;
    const coOrHigh = document.getElementById("coOrHigh");
    if (coOrHigh) coOrHigh.textContent = `₹${fmt(d.range_high)}`;
    const coVwap = document.getElementById("coVwap");
    if (coVwap) coVwap.textContent = `₹${fmt(d.vwap)}`;
    const coSma = document.getElementById("coSma");
    if (coSma) coSma.textContent = `₹${fmt(d.sma200_1h || d.sma200_30m || d.mark_price)}`;
    const coOrLow = document.getElementById("coOrLow");
    if (coOrLow) coOrLow.textContent = `₹${fmt(d.range_low)}`;
    const coWall = document.getElementById("coWall");
    if (coWall) coWall.textContent = d.daily_365_wall ? `₹${fmt(d.daily_365_wall)}` : "No Wall";
    const coSl = document.getElementById("coSl");
    if (coSl) coSl.textContent = `₹${fmt(d.planned_sl)}`;

    // Reasons pills
    const reasonsCont = document.getElementById("hudReasonsList");
    if (reasonsCont && d.reasons) {
      reasonsCont.innerHTML = d.reasons.map(r => `<span style="background:var(--cell-2);border:1px solid var(--line);border-radius:4px;padding:2px 6px;color:var(--ink-2);font-size:10px">✓ ${r}</span>`).join("");
    }

    LAST_STRATEGY_PARAMS = d;
    applyStrategyLinesToNativeChart();
  } catch (err) {
    console.warn("Failed to fetch Indian parameters:", err);
  }
}

function toggleStrategyOverlay() {
  STRATEGY_OVERLAY_VISIBLE = !STRATEGY_OVERLAY_VISIBLE;
  const el = document.getElementById("chartStrategyOverlay");
  const btn = document.getElementById("btnToggleOverlay");
  if (el) el.style.display = STRATEGY_OVERLAY_VISIBLE ? "block" : "none";
  if (btn) {
    btn.style.opacity = STRATEGY_OVERLAY_VISIBLE ? "1" : "0.6";
    btn.textContent = STRATEGY_OVERLAY_VISIBLE ? "🎯 Strategy Overlay: ON" : "🎯 Strategy Overlay: OFF";
  }
}

function toggleStrategyOverlayCollapse() {
  STRATEGY_OVERLAY_COLLAPSED = !STRATEGY_OVERLAY_COLLAPSED;
  const body = document.getElementById("chartOverlayBody");
  const btn = document.getElementById("btnOverlayCollapse");
  if (body) body.style.display = STRATEGY_OVERLAY_COLLAPSED ? "none" : "flex";
  if (btn) btn.textContent = STRATEGY_OVERLAY_COLLAPSED ? "▲" : "▼";
}

const PINE_SCRIPT_INDIAN_CODE = `//@version=5
indicator("ORB + Multi-MA + 365 Wall (Indian Markets)", overlay=true)

// 1. Opening Range (09:15 - 09:30 IST)
var float or_high = na
var float or_low = na
var float or_open = na
var float or_close = na
var int or_bias = 0 // 1: green/bull, -1: red/bear

is_new_day = ta.change(time("D"))
var int bar_count = 0
if is_new_day
    bar_count := 0
    or_high := na
    or_low := na
    or_open := open
    or_bias := 0

bar_count += 1

// First 3 x 5m bars = 15 minutes (09:15-09:30)
if bar_count <= 3
    or_high := na(or_high) ? high : math.max(or_high, high)
    or_low := na(or_low) ? low : math.min(or_low, low)
    or_close := close
    if bar_count == 3
        or_bias := or_close > or_open ? 1 : (or_close < or_open ? -1 : 0)

plot(or_high, "ORB Breakout High", color=color.green, linewidth=2, style=plot.style_linebr)
plot(or_low, "ORB Breakdown Low", color=color.red, linewidth=2, style=plot.style_linebr)

// 2. Multi-Timeframe Trend Filters
ma30 = request.security(syminfo.tickerid, "30", ta.sma(close, 200))
ma1h = request.security(syminfo.tickerid, "60", ta.sma(close, 200))
d50  = request.security(syminfo.tickerid, "D", ta.sma(close, 50))
d200 = request.security(syminfo.tickerid, "D", ta.sma(close, 200))
d365 = request.security(syminfo.tickerid, "D", ta.sma(close, 365))

plot(ma30, "30m 200 SMA", color=color.purple, linewidth=1)
plot(ma1h, "1H 200 SMA", color=color.blue, linewidth=2)
plot(d50,  "Daily 50 SMA", color=color.orange, linewidth=1)
plot(d200, "Daily 200 SMA", color=color.yellow, linewidth=2)
plot(d365, "Daily 365 MA Wall", color=color.fuchsia, linewidth=3)

// 3. Session VWAP
session_vwap = ta.vwap(hlc3)
plot(session_vwap, "Session VWAP", color=color.yellow, linewidth=2)

// 4. Background Bias Tint (Green for bull, Red for bear)
bgcolor(or_bias == 1 ? color.new(color.green, 92) : (or_bias == -1 ? color.new(color.red, 92) : na), title="15m Bias Tint")
`;

function openPineModal() {
  const modal = document.getElementById("pineScriptModal");
  const area = document.getElementById("pineCodeArea");
  if (area) area.value = PINE_SCRIPT_INDIAN_CODE;
  if (modal) modal.style.display = "flex";
}

function closePineModal() {
  const modal = document.getElementById("pineScriptModal");
  if (modal) modal.style.display = "none";
}

function copyPineCode() {
  const area = document.getElementById("pineCodeArea");
  if (!area) return;
  area.select();
  navigator.clipboard.writeText(PINE_SCRIPT_INDIAN_CODE).then(() => {
    alert("✓ Indian Markets Pine Script copied to clipboard!\n\nOpen TradingView -> click 'Pine Editor' at bottom -> paste -> click 'Add to chart'.");
  }).catch(() => {
    alert("Copied! (Press Ctrl+C to copy selected text)");
  });
}

function openChartModal() {
  const modal = document.getElementById("chartFullscreenModal");
  if (!modal) return;
  modal.style.display = "flex";
  document.body.style.overflow = "hidden";
  renderActiveChart("fsChartContainer", ACTIVE_CHART_SYMBOL, ACTIVE_CHART_TF);
}

function closeChartModal() {
  const modal = document.getElementById("chartFullscreenModal");
  if (!modal) return;
  modal.style.display = "none";
  document.body.style.overflow = "auto";
  const fs = document.getElementById("fsChartContainer");
  if (fs) fs.innerHTML = "";
}

async function openCouncilModalForSymbol() {
  const clean = ACTIVE_CHART_SYMBOL.replace("NSE:", "").replace("BSE:", "");
  const modal = document.getElementById("councilModal");
  const content = document.getElementById("cmContent");
  document.getElementById("cmTitle").textContent = `4-Agent AI Council Audit: ${clean}`;
  if (modal) modal.style.display = "flex";
  if (content) {
    content.innerHTML = `<div style="display:flex;align-items:center;gap:10px;padding:20px;color:var(--ink-2)"><span class="spin"></span> Deliberating with Technical Analyst, Bull, Bear, and Trader Decision Agent...</div>`;
  }

  try {
    const res = await fetch("/api/agents/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol: clean, min_conviction: 7 })
    });
    const j = await res.json();
    if (!j.ok) throw new Error(j.error || "Council scan failed");

    const rec = j.recommendation;
    const ctx = j.context || {};
    const convColor = rec.conviction >= 8 ? "var(--good)" : rec.conviction >= 6 ? "var(--accent)" : "var(--warn)";
    const actBadge = rec.action === "BUY" ? "v-buy" : (rec.action === "SELL" ? "veto" : "v-watch");

    content.innerHTML = `
      <div style="background:var(--cell);border:1px solid var(--line);border-radius:10px;padding:14px;display:flex;justify-content:space-between;align-items:center">
        <div>
          <span style="font-size:12px;color:var(--ink-3);text-transform:uppercase;font-weight:700">Council Decision:</span>
          <div style="font-size:16px;font-weight:800;color:var(--ink);margin-top:2px">${clean} &mdash; <span class="badge sm ${actBadge}">${rec.action}</span></div>
        </div>
        <div style="text-align:right">
          <span class="badge sm" style="font-size:12px;font-weight:700;color:${convColor}">Conviction: ${rec.conviction}/10</span>
        </div>
      </div>

      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:10px">
        <div style="background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:12px">
          <b style="color:var(--accent)">📈 Technical Analyst:</b>
          <div style="font-size:12px;color:var(--ink-2);margin-top:4px;line-height:1.4">${rec.technical_analysis}</div>
        </div>
        <div style="background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:12px">
          <b style="color:var(--good)">🐂 Bullish Researcher:</b>
          <div style="font-size:12px;color:var(--ink-2);margin-top:4px;line-height:1.4">${rec.bull_case}</div>
        </div>
        <div style="background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:12px">
          <b style="color:var(--bad)">🐻 Bearish Researcher:</b>
          <div style="font-size:12px;color:var(--ink-2);margin-top:4px;line-height:1.4">${rec.bear_case}</div>
        </div>
        <div style="background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:12px">
          <b style="color:var(--warn)">⚖️ Trader Decision Engine:</b>
          <div style="font-size:12px;color:var(--ink-2);margin-top:4px;line-height:1.4">${rec.decision_rationale}</div>
        </div>
      </div>
    `;
  } catch (err) {
    if (content) content.innerHTML = `<div class="banner"><b>Council Error:</b> ${err.message}</div>`;
  }
}

function closeCouncilModal() {
  const modal = document.getElementById("councilModal");
  if (modal) modal.style.display = "none";
}

async function loadQuotesFeed() {
  try {
    const res = await fetch("/api/quotes", { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    const box = document.getElementById("liveTickerScroll");
    const clock = document.getElementById("tickerClock");
    if (!box) return;
    if (!data.quotes || !data.quotes.length) return;

    box.innerHTML = "";
    data.quotes.slice(0, 10).forEach(q => {
      const v = q.v || {};
      const raw = (v.short_name || q.n || "").replace("-INDEX","").replace("-EQ","").replace("NSE:","");
      const lp = v.lp || 0;
      const chp = v.chp || 0;
      const isUp = chp >= 0;

      const chip = document.createElement("div");
      chip.style.cssText = "background:var(--bg-2);border:1px solid var(--line);border-radius:6px;padding:5px 9px;display:flex;align-items:center;gap:8px;cursor:pointer;flex:0 0 auto";
      chip.onclick = () => selectChartSymbol(raw);
      chip.innerHTML = `
        <span style="font-weight:700;font-size:11px;color:var(--ink)">${raw}</span>
        <span style="font-family:ui-monospace,monospace;font-size:11px;font-weight:700">₹${fmt(lp)}</span>
        <span class="badge sm ${isUp ? 'bull' : 'bear'}" style="font-size:9.5px;padding:1px 4px">${isUp ? '+' : ''}${chp.toFixed(2)}%</span>
      `;
      box.appendChild(chip);
    });

    if (clock) clock.textContent = "Live: " + new Date().toLocaleTimeString('en-IN', { hour12: false });
  } catch (err) {
    console.warn("Failed to load quotes feed:", err);
  }
}

// Initial load
const urlParams = new URLSearchParams(window.location.search);
const initSym = urlParams.get("symbol") || "NIFTY";
selectChartSymbol(initSym.startsWith("NSE:") ? initSym : "NSE:" + initSym);
loadQuotesFeed();
setInterval(loadQuotesFeed, 5000);
setInterval(() => fetchActiveSymbolParameters(ACTIVE_CHART_SYMBOL.replace("NSE:", "")), 4000);
__THEMEJS__
</script>
</body>
</html>
"""

INDIAN_CHARTS_PAGE = (
    INDIAN_CHARTS_PAGE_TEMPLATE.replace("__THEME__", ui_theme.CSS)
    .replace("__TOPBAR__", ui_theme.topbar("📈 NSE Indian Markets TradingView Station", "charts", _TOPBAR_RIGHT_CHARTS))
    .replace("__THEMEJS__", ui_theme.THEME_JS)
)


def serve_get(path: str, query: dict) -> tuple[int, str, bytes] | None:
    """Serve Indian Charts GET endpoints."""
    if path in ("/charts", "/chart", "/indian-chart", "/tv"):
        return (200, "text/html; charset=utf-8", INDIAN_CHARTS_PAGE.encode("utf-8"))

    if path == "/api/indian/parameters":
        sym_list = query.get("symbol", ["SBIN"])
        sym = sym_list[0] if sym_list else "SBIN"
        params = get_indian_parameters(sym)
        return (200, "application/json", json.dumps(params).encode("utf-8"))

    if path == "/api/indian/candles":
        sym_list = query.get("symbol", ["SBIN"])
        sym = sym_list[0] if sym_list else "SBIN"
        tf_list = query.get("tf", ["5m"])
        tf = tf_list[0] if tf_list else "5m"
        data = get_indian_candles(sym, tf)
        return (200, "application/json", json.dumps(data).encode("utf-8"))

    if path == "/static/lightweight-charts.js":
        import os
        static_file = os.path.join(os.path.dirname(__file__), "static", "lightweight-charts.js")
        if os.path.exists(static_file):
            with open(static_file, "rb") as f:
                return (200, "application/javascript", f.read())

    return None
