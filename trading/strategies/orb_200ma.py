"""ORB + Multi-TF 200MA Trend Filter Strategy.

Python implementation of the TradingView PineScript strategy:
"ORB + Multi-TF 200MA Trend Filter Strategy"

Rules:
1. Opening Range (OR):
   - Computes the 15-minute Opening Range (09:15 - 09:30 IST) high and low.
   - Validates range size: 0.3% <= orRangePct <= 1.5%.
2. Multi-Timeframe 200 SMA Trend Filter:
   - 1-Hour 200 SMA (60m)
   - 30-Minute 200 SMA (30m)
   - Bullish Trend: Close > 1H 200 SMA AND Close > 30m 200 SMA
   - Bearish Trend: Close < 1H 200 SMA AND Close < 30m 200 SMA
3. Volume Filter:
   - Breakout candle volume must exceed the 20-bar volume SMA.
4. Triggers:
   - BUY : Closed candle > orHigh + Bullish Trend + Volume OK
   - SELL: Closed candle < orLow  + Bearish Trend + Volume OK
   - Max 1 Long and 1 Short trade per symbol per session.
5. Risk Management:
   - Stop Loss: 1.0x ATR(14)
   - Target   : 1.8x the ATR Risk (1 : 1.8 R-multiple)
   - No new entries after 14:30 IST. Force square-off at 15:15 IST.
"""
from __future__ import annotations

import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

from trading.config import (
    ORB_MINUTES,
    ORB_MIN_RANGE_PCT,
    ORB_MAX_RANGE_PCT,
    ORB_USE_1H_FILTER,
    ORB_USE_30M_FILTER,
    ORB_MA_LEN,
    ORB_ATR_LEN,
    ORB_SL_ATR_MULT,
    ORB_TP_R_MULT,
    ORB_USE_VOL_FILTER,
    ORB_ONE_TRADE_PER_SIDE,
    ORB_NO_NEW_ENTRIES_AFTER,
    SQUAREOFF_TIME,
    YF_SUFFIX,
)

IST = ZoneInfo("Asia/Kolkata")

# In-memory cache for Multi-Timeframe 200 MAs: (symbol, tf) -> (timestamp, ma_val)
_MTF_CACHE: dict[tuple[str, str], tuple[float, float]] = {}
CACHE_TTL_SECONDS = 900  # 15 minutes


def calculate_atr(df: pd.DataFrame, length: int = ORB_ATR_LEN) -> pd.Series:
    """Calculate Average True Range (ATR) with standard RMA smoothing."""
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    prev_close = close.shift(1)

    tr0 = high - low
    tr1 = (high - prev_close).abs()
    tr2 = (low - prev_close).abs()
    tr = pd.concat([tr0, tr1, tr2], axis=1).max(axis=1)

    # Wilder's Exponential Smoothing (standard ATR)
    return tr.ewm(alpha=1.0 / length, adjust=False).mean()


def fetch_mtf_200ma(symbol: str, ma_len: int = ORB_MA_LEN) -> dict[str, float]:
    """Fetch 1H and 30m 200 SMA trend filters with local memory caching."""
    now_ts = time.time()
    clean_sym = symbol.replace("NSE:", "").replace("-EQ", "").strip()
    yf_sym = clean_sym if clean_sym.endswith(YF_SUFFIX) else clean_sym + YF_SUFFIX

    result: dict[str, float] = {}

    for tf_key, yf_interval in [("1h", "60m"), ("30m", "30m"), ("15m", "15m"), ("5m", "5m"), ("1d", "1d")]:
        cache_key = (clean_sym, tf_key)
        cached = _MTF_CACHE.get(cache_key)
        if cached and (now_ts - cached[0] < CACHE_TTL_SECONDS):
            result[tf_key] = cached[1]
            continue

        try:
            period = "60d" if yf_interval != "1d" else "2y"
            t = yf.Ticker(yf_sym)
            hist = t.history(period=period, interval=yf_interval)
            if not hist.empty and len(hist) >= 20:
                # If less than ma_len bars available, compute over available window
                eff_len = min(len(hist), ma_len)
                ma_val = float(hist["Close"].rolling(eff_len).mean().dropna().iloc[-1])
                _MTF_CACHE[cache_key] = (now_ts, ma_val)
                result[tf_key] = ma_val
            else:
                result[tf_key] = 0.0
        except Exception:
            result[tf_key] = 0.0

    return result


def get_opening_range(df: pd.DataFrame, or_minutes: int = ORB_MINUTES) -> dict[str, Any]:
    """Calculate 15-minute Opening Range (09:15 - 09:30 IST) for the current session."""
    if df.empty:
        return {"or_high": None, "or_low": None, "valid": False, "range_pct": 0.0, "locked": False}

    # Identify today's session bars
    today_date = df.index[-1].date()
    today_bars = df[df.index.date == today_date]
    if today_bars.empty:
        return {"or_high": None, "or_low": None, "valid": False, "range_pct": 0.0, "locked": False}

    # Opening range covers 09:15 up to 09:15 + or_minutes (e.g. 09:30)
    or_cutoff = dtime(9, 15 + or_minutes) if or_minutes < 45 else dtime(10, 0)
    or_bars = today_bars[today_bars.index.time < or_cutoff]

    if or_bars.empty:
        # First bar of the day
        first_bar = today_bars.iloc[0]
        or_high = float(first_bar["High"])
        or_low = float(first_bar["Low"])
        locked = False
    else:
        or_high = float(or_bars["High"].max())
        or_low = float(or_bars["Low"].min())
        # Locked once we have bars at or after the cutoff
        locked = any(t >= or_cutoff for t in today_bars.index.time)

    range_pct = 0.0
    if or_low and or_low > 0:
        range_pct = ((or_high - or_low) / or_low) * 100.0

    valid = (ORB_MIN_RANGE_PCT <= range_pct <= ORB_MAX_RANGE_PCT)

    return {
        "or_high": or_high,
        "or_low": or_low,
        "range_pct": round(range_pct, 2),
        "valid": valid,
        "locked": locked,
        "bars_counted": len(or_bars),
    }


def add_orb_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Prepare indicators required for ORB strategy: ATR(14) and Volume SMA(20)."""
    df = df.copy()
    if df.empty:
        return df

    df["atr"] = calculate_atr(df, length=ORB_ATR_LEN)
    df["vol_sma20"] = df["Volume"].rolling(20).mean()
    return df


def detect_orb_signal(
    df: pd.DataFrame,
    symbol: str,
    trades_today: list[dict] | None = None,
    use_1h_filter: bool = ORB_USE_1H_FILTER,
    use_30m_filter: bool = ORB_USE_30M_FILTER,
) -> str | None:
    """Detect Long (BUY) or Short (SELL) signal based on the PineScript ORB strategy.

    Conditions for BUY:
      1. Opening range locked and valid (0.3% <= range <= 1.5%).
      2. Current closed bar Close > orHigh.
      3. Multi-TF 200MA Trend is Bullish (Close > 1H 200 SMA and Close > 30m 200 SMA).
      4. Breakout volume > 20-bar average volume.
      5. Within entry window (09:30 to 14:30 IST).
      6. Max 1 Long trade today.

    Conditions for SELL:
      1. Opening range locked and valid (0.3% <= range <= 1.5%).
      2. Current closed bar Close < orLow.
      3. Multi-TF 200MA Trend is Bearish (Close < 1H 200 SMA and Close < 30m 200 SMA).
      4. Breakdown volume > 20-bar average volume.
      5. Within entry window (09:30 to 14:30 IST).
      6. Max 1 Short trade today.
    """
    if df.empty or len(df) < 5:
        return None

    now = datetime.now(IST)
    hhmm = now.strftime("%H:%M")

    # Entry window check (no new entries after 14:30 IST)
    if hhmm < "09:30" or hhmm > ORB_NO_NEW_ENTRIES_AFTER:
        return None

    # Calculate Opening Range
    orb = get_opening_range(df, or_minutes=ORB_MINUTES)
    if not orb["locked"] or not orb["valid"]:
        return None

    or_high = orb["or_high"]
    or_low = orb["or_low"]
    if or_high is None or or_low is None:
        return None

    last_bar = df.iloc[-1]
    close_price = float(last_bar["Close"])
    volume = float(last_bar.get("Volume", 0))

    # Volume filter
    vol_sma20 = float(last_bar.get("vol_sma20") or df["Volume"].rolling(20).mean().iloc[-1])
    vol_ok = (not ORB_USE_VOL_FILTER) or (volume > vol_sma20)
    if not vol_ok:
        return None

    # Multi-Timeframe 200 MA Trend Alignment
    mtf = fetch_mtf_200ma(symbol, ma_len=ORB_MA_LEN)
    ma1h = mtf.get("1h", 0.0)
    ma30m = mtf.get("30m", 0.0)

    bullish_trend = True
    bearish_trend = True

    if use_1h_filter and ma1h > 0:
        bullish_trend = bullish_trend and (close_price > ma1h)
        bearish_trend = bearish_trend and (close_price < ma1h)

    if use_30m_filter and ma30m > 0:
        bullish_trend = bullish_trend and (close_price > ma30m)
        bearish_trend = bearish_trend and (close_price < ma30m)

    # Check daily trade frequency (max 1 long, 1 short per day)
    long_taken = False
    short_taken = False
    if trades_today and ORB_ONE_TRADE_PER_SIDE:
        for t in trades_today:
            if t.get("symbol") == symbol and t.get("strategy_id") == "orb_200ma":
                side = str(t.get("side", "")).upper()
                if side == "BUY":
                    long_taken = True
                elif side == "SELL":
                    short_taken = True

    # 1. Long Entry Condition
    if close_price > or_high and bullish_trend and (not long_taken):
        return "BUY"

    # 2. Short Entry Condition
    if close_price < or_low and bearish_trend and (not short_taken):
        return "SELL"

    return None


def orb_stop(df: pd.DataFrame, side: str, sl_atr_mult: float = ORB_SL_ATR_MULT) -> float:
    """Calculate ATR-based Stop Loss: Close - (ATR * mult) for BUY, Close + (ATR * mult) for SELL."""
    last_bar = df.iloc[-1]
    close_price = float(last_bar["Close"])
    atr_val = float(last_bar.get("atr") or calculate_atr(df).iloc[-1])
    risk_pts = max(atr_val * sl_atr_mult, close_price * 0.005)  # min 0.5% buffer

    if side == "BUY":
        return round(close_price - risk_pts, 2)
    else:
        return round(close_price + risk_pts, 2)


def orb_target(
    df: pd.DataFrame,
    side: str,
    entry_price: float,
    stop_loss: float,
    tp_r_mult: float = ORB_TP_R_MULT,
) -> float:
    """Calculate Take-Profit Target using R-multiple (1.8x measured risk)."""
    risk = abs(entry_price - stop_loss)
    if risk <= 0:
        risk = entry_price * 0.01  # 1% fallback

    if side == "BUY":
        return round(entry_price + (risk * tp_r_mult), 2)
    else:
        return round(entry_price - (risk * tp_r_mult), 2)
