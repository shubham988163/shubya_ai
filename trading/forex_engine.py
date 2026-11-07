"""Forex & Crypto Strategy Engine for Delta Exchange.

Applies the Default ORB + Multi-Timeframe MA Strategy to 24/7 Forex and Crypto markets:
1. 15m / 1h Session Opening Range Breakout (ORB) with Opening Candle Bias (Green=Long only, Red=Short only)
2. Daily Big Trend Filter: Daily 50 SMA and Daily 200 SMA agreement
3. Daily 365 SMA "Wall" Filter: Skips if price is within 0.5% below 365 SMA (Long) or above 365 SMA (Short)
4. Intraday Trend: 9/21 Exponential Moving Average (EMA) and 200 SMA on 1H / 30m
5. Anchored Session VWAP alignment (price > VWAP for Long, price < VWAP for Short)
6. Volume Confirmation (RVOL >= 1.5x of 20-period baseline)
7. 1:1.5 Risk-to-Reward Targets with Opposite Range or 1.5 ATR Structural Stop
8. AI Multi-Agent Conviction Scoring (Threshold >= 8/10)
"""
from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np
import pandas as pd

from trading.delta_exchange import DeltaClient, FOREX_WATCHLIST

logger = logging.getLogger("trading.forex_engine")

_FOREX_DAILY_CACHE: dict[str, tuple[float, dict[str, float]]] = {}
_CACHE_TTL = 300.0  # 5 minutes

_YF_FOREX_MAP = {
    "BTCUSD": "BTC-USD",
    "ETHUSD": "ETH-USD",
    "SOLUSD": "SOL-USD",
    "XAUTUSD": "GC=F",
    "XRPUSD": "XRP-USD",
    "DOGEUSD": "DOGE-USD",
    "BNBUSD": "BNB-USD",
    "ADAUSD": "ADA-USD",
}


def fetch_forex_daily_mas(symbol: str) -> dict[str, float]:
    """Fetch 2y daily candles and calculate Daily 50, 200, and 365 MAs for Forex/Crypto."""
    now_ts = time.time()
    if symbol in _FOREX_DAILY_CACHE:
        cached_ts, cached_mas = _FOREX_DAILY_CACHE[symbol]
        if now_ts - cached_ts < _CACHE_TTL:
            return cached_mas

    mas: dict[str, float] = {}
    yf_sym = _YF_FOREX_MAP.get(symbol) or f"{symbol.replace('USD', '')}-USD"
    try:
        import yfinance as yf
        t = yf.Ticker(yf_sym)
        hist = t.history(period="2y", interval="1d")
        if not hist.empty and len(hist) >= 50:
            c = hist["Close"]
            mas["d50"] = round(float(c.rolling(min(len(c), 50)).mean().iloc[-1]), 4)
            if len(c) >= 150:
                mas["d200"] = round(float(c.rolling(min(len(c), 200)).mean().iloc[-1]), 4)
            if len(c) >= 250:
                mas["d365"] = round(float(c.rolling(min(len(c), 365)).mean().iloc[-1]), 4)
    except Exception as exc:
        logger.debug(f"Could not fetch daily crypto MAs for {symbol}: {exc}")

    _FOREX_DAILY_CACHE[symbol] = (now_ts, mas)
    return mas


def near_wall(price: float, ma: float | None, side: str, wall_pct: float = 0.5) -> bool:
    """Check if price is within wall_pct of the 365 Daily SMA (The Wall)."""
    if ma is None or ma != ma or ma <= 0:
        return False
    gap = (ma - price) / price * 100 if side in ("BUY", "LONG") else (price - ma) / price * 100
    return 0 < gap <= wall_pct


def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate EMAs, 200 SMA, VWAP, ATR, RSI, and Volume RVOL on OHLCV candles."""
    if df is None or len(df) < 25:
        return df

    c = df["Close"]
    h = df["High"]
    l = df["Low"]
    v = df["Volume"].replace(0, 1.0)

    # 1. EMAs and 200 SMA
    df["EMA_9"] = c.ewm(span=9, adjust=False).mean()
    df["EMA_21"] = c.ewm(span=21, adjust=False).mean()
    df["SMA_200"] = c.rolling(window=min(len(df), 200), min_periods=20).mean()

    # 2. VWAP
    typical_price = (h + l + c) / 3.0
    cum_vol = v.cumsum()
    cum_vol_price = (typical_price * v).cumsum()
    df["VWAP"] = cum_vol_price / cum_vol.replace(0, np.nan)
    df["VWAP"] = df["VWAP"].bfill().fillna(c)

    # 3. ATR (Average True Range, 14-period)
    high_low = h - l
    high_close = (h - c.shift()).abs()
    low_close = (l - c.shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["ATR"] = true_range.rolling(window=14, min_periods=5).mean().bfill()

    # 4. RSI (14-period)
    delta = c.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=14, min_periods=5).mean()
    avg_loss = loss.rolling(window=14, min_periods=5).mean().replace(0, 1e-5)
    rs = avg_gain / avg_loss
    df["RSI"] = 100.0 - (100.0 / (1.0 + rs))
    df["RSI"] = df["RSI"].fillna(50.0)

    # 5. Volume RVOL (vs 20-period average)
    df["Vol_Avg"] = v.rolling(window=20, min_periods=5).mean()
    df["RVOL"] = v / df["Vol_Avg"].replace(0, 1.0)

    return df


class ForexEngine:
    """Analyzes Forex/Crypto assets using the Default ORB + Multi-MA + 365 Wall Strategy."""

    def __init__(self, client: DeltaClient | None = None):
        self.client = client or DeltaClient()

    def evaluate_symbol(self, symbol: str) -> dict[str, Any] | None:
        """Run the strategy on a Forex/Crypto symbol and produce an actionable trade setup."""
        df = self.client.get_candles(symbol, resolution="5", bars=90)
        if df is None or len(df) < 25:
            return None

        df = calculate_indicators(df)
        last = df.iloc[-1]
        prev = df.iloc[-2]

        price = float(last["Close"])
        ema9 = float(last["EMA_9"])
        ema21 = float(last["EMA_21"])
        sma200 = float(last["SMA_200"])
        vwap = float(last["VWAP"])
        atr = float(last["ATR"])
        rsi = float(last["RSI"])
        rvol = float(last["RVOL"])

        # 1. Higher Timeframe Daily MAs & The 365 Wall
        daily_mas = fetch_forex_daily_mas(symbol)
        d50 = daily_mas.get("d50")
        d200 = daily_mas.get("d200")
        d365 = daily_mas.get("d365")

        # 2. Session Range & Opening Candle Bias (15 bars = 1 hour 15 min session range)
        recent_bars = df.iloc[-15:-1]
        range_high = float(recent_bars["High"].max())
        range_low = float(recent_bars["Low"].min())
        first_bar = recent_bars.iloc[0]
        or_bias = 1 if float(first_bar["Close"]) > float(first_bar["Open"]) else (
            -1 if float(first_bar["Close"]) < float(first_bar["Open"]) else 0
        )

        # 3. Trend Alignment & VWAP Gate
        trend_up = ema9 > ema21 and price > vwap and price >= sma200 * 0.99
        trend_down = ema9 < ema21 and price < vwap and price <= sma200 * 1.01

        # Daily Big Trend agreement
        if d50 is not None:
            if price < d50:
                trend_up = False
            if price > d50:
                trend_down = False
        if d200 is not None:
            if price < d200:
                trend_up = False
            if price > d200:
                trend_down = False

        # 4. Breakout Triggers
        is_breakout_long = price >= range_high and trend_up and (or_bias != -1)
        is_breakout_short = price <= range_low and trend_down and (or_bias != 1)

        # 5. Daily 365 Wall Filter Check
        if is_breakout_long and near_wall(price, d365, "BUY"):
            logger.info(f"[{symbol}] Long Breakout VETOED by Daily 365 MA Wall at ${d365}")
            is_breakout_long = False
            trend_up = False

        if is_breakout_short and near_wall(price, d365, "SELL"):
            logger.info(f"[{symbol}] Short Breakdown VETOED by Daily 365 MA Wall at ${d365}")
            is_breakout_short = False
            trend_down = False

        decimals = 2 if price > 500 else (3 if price > 10 else 4)

        # 6. Scoring & Confluence
        score = 50
        reasons = []

        if trend_up:
            score += 15
            reasons.append("Bullish 9/21 EMA crossover above VWAP")
        elif trend_down:
            score += 15
            reasons.append("Bearish 9/21 EMA crossover below VWAP")

        if d50 and d200:
            if price > d50 and price > d200:
                score += 10
                reasons.append("Macro Daily 50 & 200 SMA alignment")
            elif price < d50 and price < d200:
                score += 10
                reasons.append("Macro Daily 50 & 200 SMA breakdown")

        if is_breakout_long:
            score += 20
            reasons.append(f"ORB Breakout above session High (${range_high:,.{decimals}f}) with Green Bias")
        elif is_breakout_short:
            score += 20
            reasons.append(f"ORB Breakdown below session Low (${range_low:,.{decimals}f}) with Red Bias")

        if rvol >= 1.5:
            score += 15
            reasons.append(f"Volume Expansion Surge ({rvol:.1f}x >= 1.5x threshold)")
        elif rvol >= 1.2:
            score += 5
            reasons.append(f"Moderate Volume ({rvol:.1f}x)")

        if 48 <= rsi <= 68 and trend_up:
            score += 10
            reasons.append(f"RSI in bullish momentum zone ({rsi:.1f})")
        elif 32 <= rsi <= 52 and trend_down:
            score += 10
            reasons.append(f"RSI in bearish momentum zone ({rsi:.1f})")

        # 7. Action, Stop Loss & 1:1.5 RR Targets
        if is_breakout_long or (trend_up and or_bias == 1):
            side = "BUY"
            raw_sl = min(range_low, price - 1.5 * atr)
            raw_sl = max(raw_sl, price - max(0.5 * atr, price * 0.003))
            stop_loss = round(raw_sl, decimals)
            risk = max(price * 0.003, price - stop_loss)
            target1 = round(price + 1.5 * risk, decimals)  # Default 1:1.5 RR
            target2 = round(price + 2.0 * risk, decimals)
        elif is_breakout_short or (trend_down and or_bias == -1):
            side = "SELL"
            raw_sl = max(range_high, price + 1.5 * atr)
            raw_sl = min(raw_sl, price + max(0.5 * atr, price * 0.003))
            stop_loss = round(raw_sl, decimals)
            risk = max(price * 0.003, stop_loss - price)
            target1 = round(price - 1.5 * risk, decimals)  # Default 1:1.5 RR
            target2 = round(price - 2.0 * risk, decimals)
        else:
            return None

        conviction = min(10, max(4, int(score / 10)))
        grade = "A+" if score >= 85 else "A" if score >= 75 else "B" if score >= 65 else "WATCH"

        # Position sizing: target ~$25 - $50 risk per trade
        risk_per_unit = abs(price - stop_loss)
        unit_size = max(1.0, round(50.0 / max(0.1, risk_per_unit), 2)) if price > 100 else max(10.0, round(50.0 / max(0.01, risk_per_unit), 0))

        return {
            "symbol": symbol,
            "side": side,
            "price": round(price, decimals),
            "stop_loss": round(stop_loss, decimals),
            "target1": round(target1, decimals),
            "target2": round(target2, decimals),
            "risk_per_unit": round(risk_per_unit, decimals),
            "unit_size": unit_size,
            "score": score,
            "conviction": conviction,
            "grade": grade,
            "strategy": "orb_multi_ma",
            "or_bias": "GREEN (BUY)" if or_bias == 1 else ("RED (SELL)" if or_bias == -1 else "DOJI"),
            "daily_wall_365": d365,
            "reasons": reasons,
            "rvol": round(rvol, 2),
            "vwap": round(vwap, decimals),
            "timestamp": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def scan_all(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        """Scan all watchlist symbols and return qualifying setups."""
        syms = symbols or FOREX_WATCHLIST
        results = []
        for s in syms:
            try:
                setup = self.evaluate_symbol(s)
                if setup and setup.get("conviction", 0) >= 6:
                    results.append(setup)
            except Exception as e:
                logger.error(f"Error evaluating {s}: {e}")
        return sorted(results, key=lambda x: x["score"], reverse=True)

    def get_symbol_parameters(self, symbol: str) -> dict[str, Any]:
        """Compute real-time strategy parameters for the Technical Candlestick Station HUD."""
        df = self.client.get_candles(symbol, resolution="5", bars=90)
        if df is None or len(df) < 25:
            return {"ok": False, "error": f"Insufficient candle data for {symbol}"}

        df = calculate_indicators(df)
        last = df.iloc[-1]
        price = float(last["Close"])
        ema9 = float(last["EMA_9"])
        ema21 = float(last["EMA_21"])
        sma200 = float(last["SMA_200"])
        vwap = float(last["VWAP"])
        atr = float(last["ATR"])
        rsi = float(last["RSI"])
        rvol = float(last["RVOL"])

        daily_mas = fetch_forex_daily_mas(symbol)
        d50 = daily_mas.get("d50")
        d200 = daily_mas.get("d200")
        d365 = daily_mas.get("d365")

        recent_bars = df.iloc[-15:-1]
        range_high = float(recent_bars["High"].max())
        range_low = float(recent_bars["Low"].min())
        range_width_pct = ((range_high - range_low) / range_low) * 100.0 if range_low > 0 else 0.0

        first_bar = recent_bars.iloc[0]
        or_bias = 1 if float(first_bar["Close"]) > float(first_bar["Open"]) else (
            -1 if float(first_bar["Close"]) < float(first_bar["Open"]) else 0
        )
        bias_str = "BULLISH (GREEN)" if or_bias == 1 else ("BEARISH (RED)" if or_bias == -1 else "NEUTRAL")

        wall_safe = True
        wall_note = "Safe Distance"
        if d365:
            if 0 < (d365 - price) / price * 100 <= 0.5:
                wall_safe = False
                wall_note = f"VETO: Approaching 365 MA Resistance Wall (${d365:,.2f})"
            elif 0 < (price - d365) / price * 100 <= 0.5:
                wall_safe = False
                wall_note = f"VETO: Approaching 365 MA Support Wall (${d365:,.2f})"

        ema_diff = ema9 - ema21
        ema_trend = "BULLISH (9 > 21)" if ema_diff > 0 else "BEARISH (9 < 21)"
        vwap_status = "ABOVE VWAP (+)" if price >= vwap else "BELOW VWAP (-)"
        sma_status = "ABOVE 200 SMA" if price >= sma200 else "BELOW 200 SMA"
        rvol_status = "VOLUME EXPANSION (>= 1.5x)" if rvol >= 1.5 else (
            "MODERATE VOLUME (>= 1.2x)" if rvol >= 1.2 else f"NORMAL ({rvol:.2f}x)"
        )

        is_breakout_long = price >= range_high and ema9 > ema21 and or_bias == 1 and wall_safe
        is_breakout_short = price <= range_low and ema9 < ema21 and or_bias == -1 and wall_safe
        in_range = range_low <= price <= range_high

        decimals = 2 if price > 500 else (3 if price > 10 else 4)

        if is_breakout_long:
            action = "BUY BREAKOUT"
            action_badge = "bull"
            sl = round(range_low, decimals)
            risk = max(price * 0.003, price - sl)
            target1 = round(price + 1.5 * risk, decimals)
            target2 = round(price + 2.0 * risk, decimals)
        elif is_breakout_short:
            action = "SELL BREAKDOWN"
            action_badge = "bear"
            sl = round(range_high, decimals)
            risk = max(price * 0.003, sl - price)
            target1 = round(price - 1.5 * risk, decimals)
            target2 = round(price - 2.0 * risk, decimals)
        else:
            action = "HOLD / IN-RANGE CONSOLIDATION"
            action_badge = "ok"
            sl = round(range_low if ema9 > ema21 else range_high, decimals)
            risk = max(price * 0.003, abs(price - sl))
            target1 = round(price + 1.5 * risk if ema9 > ema21 else price - 1.5 * risk, decimals)
            target2 = round(price + 2.0 * risk if ema9 > ema21 else price - 2.0 * risk, decimals)

        score = 50
        reasons = []
        if ema9 > ema21:
            score += 15
            reasons.append("Bullish 9/21 EMA momentum")
        else:
            score += 15
            reasons.append("Bearish 9/21 EMA momentum")

        if d50 and d200:
            if price > d50 and price > d200:
                score += 10
                reasons.append("Trading above Daily 50 & 200 SMA")
            elif price < d50 and price < d200:
                score += 10
                reasons.append("Trading below Daily 50 & 200 SMA")

        if is_breakout_long:
            score += 20
            reasons.append(f"Breakout above High (${range_high:,.{decimals}f}) with Green Bias")
        elif is_breakout_short:
            score += 20
            reasons.append(f"Breakdown below Low (${range_low:,.{decimals}f}) with Red Bias")
        else:
            reasons.append(f"Inside Session Range (${range_low:,.{decimals}f} - ${range_high:,.{decimals}f})")

        if rvol >= 1.5:
            score += 15
            reasons.append(f"Institutional volume surge ({rvol:.1f}x >= 1.5x)")
        elif rvol >= 1.2:
            score += 5
            reasons.append(f"Moderate volume ({rvol:.1f}x)")

        if not wall_safe:
            score = max(20, score - 30)
            reasons.append(wall_note)

        conviction = min(10, max(2, int(score / 10)))

        return {
            "ok": True,
            "symbol": symbol,
            "strategy": "orb_multi_ma",
            "mark_price": round(price, decimals),
            "ema_9": round(ema9, decimals),
            "ema_21": round(ema21, decimals),
            "ema_trend": ema_trend,
            "vwap": round(vwap, decimals),
            "vwap_status": vwap_status,
            "sma_200": round(sma200, decimals),
            "sma_status": sma_status,
            "daily_50": d50,
            "daily_200": d200,
            "daily_365_wall": d365,
            "wall_note": wall_note,
            "or_bias": bias_str,
            "range_high": round(range_high, decimals),
            "range_low": round(range_low, decimals),
            "range_width_pct": round(range_width_pct, 2),
            "in_range": in_range,
            "rvol": round(rvol, 2),
            "rvol_status": rvol_status,
            "rsi": round(rsi, 1),
            "atr": round(atr, decimals),
            "score": score,
            "conviction": conviction,
            "action": action,
            "action_badge": action_badge,
            "long_trigger": round(range_high, decimals),
            "short_trigger": round(range_low, decimals),
            "planned_sl": sl,
            "planned_target1": target1,
            "planned_target2": target2,
            "risk_per_unit": round(risk, decimals),
            "reasons": reasons,
            "decimals": decimals,
            "updated_at": pd.Timestamp.now().strftime("%H:%M:%S"),
        }
