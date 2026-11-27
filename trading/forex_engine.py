"""Forex & Crypto Strategy Engine for Delta Exchange.

Applies the Pine Script ORB + Multi-Timeframe MA Strategy to 24/7 Forex and Crypto markets:
1. 15m Session Opening Range Breakout (ORB) with Opening Candle Bias (Green=Long only, Red=Short only)
2. Higher-Timeframe Trend Filter: 30m 200 SMA (res="30", 200 bars)
3. Daily Big Trend Filter: Daily 50 SMA and Daily 200 SMA agreement
4. Daily 365 SMA "Wall" Filter: Skips if price is within 0.5% below 365 SMA (Long) or above 365 SMA (Short)
5. Intraday Trend: Pine 20/50 Exponential Moving Average (EMA)
6. Anchored Session VWAP alignment (price > VWAP for Long, price < VWAP for Short)
7. Volume Confirmation (RVOL >= 1.2x/1.5x of 20-period baseline) + ADX Chop & Order Flow gates
8. 1:2.0 Risk-to-Reward Targets with Opposite OR / 1.5 ATR Structural Stop
9. AI Multi-Agent Conviction Scoring (Threshold >= 8/10)
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
_FOREX_30M_CACHE: dict[str, tuple[float, float | None]] = {}
_CACHE_TTL = 300.0  # 5 minutes
_SESSION_HOURS_UTC = (0, 8, 13)  # 00:00 UTC Daily, 08:00 UTC London, 13:00 UTC New York

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


def fetch_forex_30m_sma200(client: DeltaClient, symbol: str) -> float | None:
    """Fetch 30m historical candles from Delta Exchange and compute 30m 200 SMA (Pine HTF filter)."""
    now_ts = time.time()
    if symbol in _FOREX_30M_CACHE:
        cached_ts, cached_val = _FOREX_30M_CACHE[symbol]
        if now_ts - cached_ts < _CACHE_TTL:
            return cached_val

    sma200_30m: float | None = None
    try:
        df30 = client.get_candles(symbol, resolution="30", bars=220)
        if df30 is not None and len(df30) >= 200:
            val = float(df30["Close"].astype(float).rolling(200).mean().iloc[-1])
            if val == val and val > 0:
                sma200_30m = val
    except Exception as exc:
        logger.debug(f"Could not fetch 30m 200 SMA for {symbol}: {exc}")

    _FOREX_30M_CACHE[symbol] = (now_ts, sma200_30m)
    return sma200_30m


def extract_session_orb(
    df: pd.DataFrame,
    session_hours_utc: tuple[int, ...] = _SESSION_HOURS_UTC,
) -> tuple[float, float, int]:
    """Extract the most recent major session 15m Opening Range (High, Low, OR Bias).

    Looks for the latest completed 15m bar at 00:00 (Daily), 08:00 (London), or 13:00 (NY) UTC.
    Falls back cleanly to the local 14-bar window if timestamps lack session markers.
    """
    hist = df.iloc[:-1] if len(df) > 2 else df
    if isinstance(hist.index, pd.DatetimeIndex):
        mask = hist.index.hour.isin(session_hours_utc) & (hist.index.minute == 0)
        session_bars = hist[mask]
        if not session_bars.empty:
            or_bar = session_bars.iloc[-1]
            or_h = float(or_bar["High"])
            or_l = float(or_bar["Low"])
            or_c = float(or_bar["Close"])
            or_o = float(or_bar["Open"])
            bias = 1 if or_c > or_o else (-1 if or_c < or_o else 0)
            return or_h, or_l, bias

    recent_bars = df.iloc[-15:-1] if len(df) >= 16 else hist
    range_high = float(recent_bars["High"].max())
    range_low = float(recent_bars["Low"].min())
    first_bar = recent_bars.iloc[0]
    or_bias = 1 if float(first_bar["Close"]) > float(first_bar["Open"]) else (
        -1 if float(first_bar["Close"]) < float(first_bar["Open"]) else 0
    )
    return range_high, range_low, or_bias


def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate Pine 20/50 EMAs (plus 9/21), 200 SMA, VWAP, ATR, RSI, and Volume RVOL."""
    if df is None or len(df) < 25:
        return df

    c = df["Close"]
    h = df["High"]
    l = df["Low"]
    v = df["Volume"].replace(0, 1.0)

    # 1. EMAs (Pine 20/50 + legacy 9/21) and 200 SMA
    df["EMA_9"] = c.ewm(span=9, adjust=False).mean()
    df["EMA_21"] = c.ewm(span=21, adjust=False).mean()
    df["EMA_20"] = c.ewm(span=20, adjust=False).mean()
    df["EMA_50"] = c.ewm(span=50, adjust=False).mean()
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

    # 6. ADX (Average Directional Index, 14-period) for Sideways/Chop Detection
    up_move = h - h.shift(1)
    down_move = l.shift(1) - l
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr_series = df["ATR"].replace(0, 1e-5)
    plus_di = 100.0 * (pd.Series(plus_dm, index=df.index).rolling(14, min_periods=5).mean() / tr_series)
    minus_di = 100.0 * (pd.Series(minus_dm, index=df.index).rolling(14, min_periods=5).mean() / tr_series)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-5)
    df["ADX"] = dx.rolling(14, min_periods=5).mean().fillna(20.0)

    # 7. Bollinger Bands & Bandwidth (20-period, 2-std)
    sma_20 = c.rolling(20, min_periods=5).mean()
    std_20 = c.rolling(20, min_periods=5).std().fillna(0)
    upper_bb = sma_20 + (2.0 * std_20)
    lower_bb = sma_20 - (2.0 * std_20)
    df["BB_Width"] = ((upper_bb - lower_bb) / sma_20.replace(0, 1.0)) * 100.0

    # 8. Order Flow Imbalance & Buyer/Seller Volume Pressure
    # Candle volume split: Buyers push from Low to Close; Sellers push from High to Close
    candle_range = (h - l).replace(0, 1e-5)
    buyer_pressure_ratio = ((c - l) / candle_range).clip(0.0, 1.0)
    df["Buyer_Vol"] = v * buyer_pressure_ratio
    df["Seller_Vol"] = v * (1.0 - buyer_pressure_ratio)
    b_vol_sum = df["Buyer_Vol"].rolling(3, min_periods=1).sum()
    s_vol_sum = df["Seller_Vol"].rolling(3, min_periods=1).sum()
    df["Buyer_Ratio"] = (b_vol_sum / (b_vol_sum + s_vol_sum).replace(0, 1.0)) * 100.0

    return df


class ForexEngine:
    """Analyzes Forex/Crypto assets using Pine Script ORB + 20/50 EMA + 30m 200 SMA + 365 Wall."""

    def __init__(self, client: DeltaClient | None = None):
        self.client = client or DeltaClient()

    def _load_symbol_context(self, symbol: str) -> dict[str, Any] | None:
        """Fetch 15m candles, 30m 200 SMA, Daily MAs, and Session 15m ORB context."""
        df = self.client.get_candles(symbol, resolution="15", bars=120)
        if df is None or len(df) < 25:
            return None

        df = calculate_indicators(df)
        last = df.iloc[-1]
        price = float(last["Close"])
        ema9 = float(last["EMA_9"])
        ema21 = float(last["EMA_21"])
        ema20 = float(last["EMA_20"])
        ema50 = float(last["EMA_50"])
        sma200_local = float(last["SMA_200"])
        sma200_30m = fetch_forex_30m_sma200(self.client, symbol)
        htf_sma200 = sma200_30m if sma200_30m is not None else sma200_local
        vwap = float(last["VWAP"])
        atr = float(last["ATR"])
        rsi = float(last["RSI"])
        rvol = float(last["RVOL"])

        daily_mas = fetch_forex_daily_mas(symbol)
        d50 = daily_mas.get("d50")
        d200 = daily_mas.get("d200")
        d365 = daily_mas.get("d365")

        range_high, range_low, or_bias = extract_session_orb(df)
        range_width_pct = ((range_high - range_low) / range_low) * 100.0 if range_low > 0 else 0.0

        return {
            "df": df,
            "last": last,
            "price": price,
            "ema9": ema9,
            "ema21": ema21,
            "ema20": ema20,
            "ema50": ema50,
            "sma200_local": sma200_local,
            "sma200_30m": sma200_30m,
            "htf_sma200": htf_sma200,
            "vwap": vwap,
            "atr": atr,
            "rsi": rsi,
            "rvol": rvol,
            "d50": d50,
            "d200": d200,
            "d365": d365,
            "range_high": range_high,
            "range_low": range_low,
            "range_width_pct": range_width_pct,
            "or_bias": or_bias,
        }

    def evaluate_symbol(self, symbol: str) -> dict[str, Any] | None:
        """Run the Pine ORB + Multi-MA strategy on a Forex/Crypto symbol."""
        ctx = self._load_symbol_context(symbol)
        if ctx is None:
            return None

        last = ctx["last"]
        price = ctx["price"]
        ema20, ema50 = ctx["ema20"], ctx["ema50"]
        htf_sma200 = ctx["htf_sma200"]
        sma200_30m = ctx["sma200_30m"]
        vwap, atr, rsi, rvol = ctx["vwap"], ctx["atr"], ctx["rsi"], ctx["rvol"]
        d50, d200, d365 = ctx["d50"], ctx["d200"], ctx["d365"]
        range_high, range_low, or_bias = ctx["range_high"], ctx["range_low"], ctx["or_bias"]

        # 3. Pine Multi-MA Trend Alignment (20/50 EMA + 30m 200 SMA + Session VWAP)
        trend_up = ema20 > ema50 and price > vwap and price > htf_sma200
        trend_down = ema20 < ema50 and price < vwap and price < htf_sma200

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

        # 4. Strict Pine ORB Breakout + Opening Candle Bias Gate
        is_breakout_long = price > range_high and trend_up and (or_bias == 1)
        is_breakout_short = price < range_low and trend_down and (or_bias == -1)

        adx = float(last.get("ADX", 20.0))
        bb_width = float(last.get("BB_Width", 1.5))
        buyer_ratio = float(last.get("Buyer_Ratio", 50.0))

        # 4b. Sideways & Low-Volatility Filter (Hard Gate: Stand down during sideways chop)
        is_sideways = (adx < 20.0) or (bb_width < 1.0 and rvol < 1.5)
        if is_sideways:
            logger.info(f"[{symbol}] Sideways/Low-Volatility Filter: ADX={adx:.1f} < 20 or BB_Width={bb_width:.2f}% < 1.0%. Vetoing breakout traps.")
            is_breakout_long = False
            is_breakout_short = False

        # 4c. Order Flow Imbalance & Buyer/Seller Dominance Gate
        if is_breakout_long and buyer_ratio < 55.0:
            logger.info(f"[{symbol}] Long Breakout VETOED: Buyer volume ratio {buyer_ratio:.1f}% < 55% threshold (lack of buyer pressure).")
            is_breakout_long = False

        if is_breakout_short and buyer_ratio > 45.0:
            logger.info(f"[{symbol}] Short Breakdown VETOED: Seller volume ratio {100 - buyer_ratio:.1f}% < 55% threshold (lack of seller pressure).")
            is_breakout_short = False

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
            reasons.append("Bullish Pine 20/50 EMA alignment above VWAP & 30m 200 SMA")
        elif trend_down:
            score += 15
            reasons.append("Bearish Pine 20/50 EMA alignment below VWAP & 30m 200 SMA")

        if d50 and d200:
            if price > d50 and price > d200:
                score += 10
                reasons.append("Macro Daily 50 & 200 SMA alignment")
            elif price < d50 and price < d200:
                score += 10
                reasons.append("Macro Daily 50 & 200 SMA breakdown")

        if is_breakout_long:
            score += 20
            reasons.append(f"Pine ORB Breakout above Session High (${range_high:,.{decimals}f}) with Green OR Bias")
        elif is_breakout_short:
            score += 20
            reasons.append(f"Pine ORB Breakdown below Session Low (${range_low:,.{decimals}f}) with Red OR Bias")

        if rvol >= 2.0:
            score += 20
            reasons.append(f"Institutional Volume Surge ({rvol:.1f}x >= 2.0x threshold)")
        elif rvol >= 1.5:
            score += 10
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

        # 7. Action, Stop Loss & Pine 1:2 RR Targets (requires actual ORB breakout)
        atr_mult = 2.5 if symbol == "XAUTUSD" else 1.5
        if is_breakout_long:
            side = "BUY"
            if symbol == "XAUTUSD":
                raw_sl = price - atr_mult * atr
            else:
                raw_sl = max(range_low, price - atr_mult * atr)
                raw_sl = min(raw_sl, price - max(0.5 * atr, price * 0.003))
            stop_loss = round(raw_sl, decimals)
            risk = max(price * 0.003, price - stop_loss)
            target1 = round(price + 2.0 * risk, decimals)
            target2 = round(price + 3.0 * risk, decimals)
        elif is_breakout_short:
            side = "SELL"
            if symbol == "XAUTUSD":
                raw_sl = price + atr_mult * atr
            else:
                raw_sl = min(range_high, price + atr_mult * atr)
                raw_sl = max(raw_sl, price + max(0.5 * atr, price * 0.003))
            stop_loss = round(raw_sl, decimals)
            risk = max(price * 0.003, stop_loss - price)
            target1 = round(price - 2.0 * risk, decimals)
            target2 = round(price - 3.0 * risk, decimals)
        else:
            return None

        conviction = min(10, max(4, int(score / 10)))
        grade = "A+" if score >= 85 else "A" if score >= 75 else "B" if score >= 65 else "WATCH"

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
            "strategy": "pine_orb_multi_ma",
            "or_bias": "GREEN (BUY)" if or_bias == 1 else ("RED (SELL)" if or_bias == -1 else "DOJI"),
            "sma_200_30m": round(sma200_30m, decimals) if sma200_30m is not None else None,
            "daily_wall_365": d365,
            "reasons": reasons,
            "rvol": round(rvol, 2),
            "vwap": round(vwap, decimals),
            "adx": round(adx, 1),
            "bb_width": round(bb_width, 2),
            "buyer_ratio": round(buyer_ratio, 1),
            "order_flow": "BUYERS_DOMINANT" if buyer_ratio >= 55.0 else ("SELLERS_DOMINANT" if buyer_ratio <= 45.0 else "BALANCED_CHOP"),
            "market_state": "SIDEWAYS_CHOP" if is_sideways else "TRENDING_MOMENTUM",
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
        """Compute real-time Pine ORB + Multi-MA parameters for the Chart Station HUD."""
        ctx = self._load_symbol_context(symbol)
        if ctx is None:
            return {"ok": False, "error": f"Insufficient candle data for {symbol}"}

        price = ctx["price"]
        ema9, ema21 = ctx["ema9"], ctx["ema21"]
        ema20, ema50 = ctx["ema20"], ctx["ema50"]
        htf_sma200, sma200_30m = ctx["htf_sma200"], ctx["sma200_30m"]
        vwap, atr, rsi, rvol = ctx["vwap"], ctx["atr"], ctx["rsi"], ctx["rvol"]
        d50, d200, d365 = ctx["d50"], ctx["d200"], ctx["d365"]
        range_high, range_low = ctx["range_high"], ctx["range_low"]
        range_width_pct, or_bias = ctx["range_width_pct"], ctx["or_bias"]

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

        ema_trend = "BULLISH (20 > 50)" if ema20 >= ema50 else "BEARISH (20 < 50)"
        vwap_status = "ABOVE VWAP (+)" if price >= vwap else "BELOW VWAP (-)"
        sma_label = "30m 200 SMA" if sma200_30m is not None else "200 SMA"
        sma_status = f"ABOVE {sma_label}" if price >= htf_sma200 else f"BELOW {sma_label}"
        rvol_status = "VOLUME EXPANSION (>= 1.5x)" if rvol >= 1.5 else (
            "MODERATE VOLUME (>= 1.2x)" if rvol >= 1.2 else f"NORMAL ({rvol:.2f}x)"
        )

        is_breakout_long = price > range_high and ema20 > ema50 and price > htf_sma200 and or_bias == 1 and wall_safe
        is_breakout_short = price < range_low and ema20 < ema50 and price < htf_sma200 and or_bias == -1 and wall_safe
        in_range = range_low <= price <= range_high

        decimals = 2 if price > 500 else (3 if price > 10 else 4)
        atr_mult = 2.5 if symbol == "XAUTUSD" else 1.5

        if is_breakout_long:
            action = "BUY BREAKOUT"
            action_badge = "bull"
            sl = round(price - atr_mult * atr if symbol == "XAUTUSD" else max(range_low, price - atr_mult * atr), decimals)
            risk = max(price * 0.003, price - sl)
            target1 = round(price + 2.0 * risk, decimals)
            target2 = round(price + 3.0 * risk, decimals)
        elif is_breakout_short:
            action = "SELL BREAKDOWN"
            action_badge = "bear"
            sl = round(price + atr_mult * atr if symbol == "XAUTUSD" else min(range_high, price + atr_mult * atr), decimals)
            risk = max(price * 0.003, sl - price)
            target1 = round(price - 2.0 * risk, decimals)
            target2 = round(price - 3.0 * risk, decimals)
        else:
            action = "HOLD / IN-RANGE CONSOLIDATION"
            action_badge = "ok"
            base_sl = price - atr_mult * atr if ema20 > ema50 else price + atr_mult * atr
            sl = round(base_sl if symbol == "XAUTUSD" else (range_low if ema20 > ema50 else range_high), decimals)
            risk = max(price * 0.003, abs(price - sl))
            target1 = round(price + 2.0 * risk if ema20 > ema50 else price - 2.0 * risk, decimals)
            target2 = round(price + 3.0 * risk if ema20 > ema50 else price - 3.0 * risk, decimals)

        score = 50
        reasons = []
        if ema20 > ema50:
            score += 15
            reasons.append("Bullish Pine 20/50 EMA momentum")
        else:
            score += 15
            reasons.append("Bearish Pine 20/50 EMA momentum")

        if d50 and d200:
            if price > d50 and price > d200:
                score += 10
                reasons.append("Trading above Daily 50 & 200 SMA")
            elif price < d50 and price < d200:
                score += 10
                reasons.append("Trading below Daily 50 & 200 SMA")

        if is_breakout_long:
            score += 20
            reasons.append(f"Pine ORB Breakout above High (${range_high:,.{decimals}f}) with Green Bias")
        elif is_breakout_short:
            score += 20
            reasons.append(f"Pine ORB Breakdown below Low (${range_low:,.{decimals}f}) with Red Bias")
        else:
            reasons.append(f"Inside Session Range (${range_low:,.{decimals}f} - ${range_high:,.{decimals}f})")

        if rvol >= 2.0:
            score += 20
            reasons.append(f"Institutional volume surge ({rvol:.1f}x >= 2.0x)")
        elif rvol >= 1.5:
            score += 10
            reasons.append(f"Volume expansion ({rvol:.1f}x >= 1.5x)")
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
            "strategy": "pine_orb_multi_ma",
            "mark_price": round(price, decimals),
            "ema_9": round(ema9, decimals),
            "ema_21": round(ema21, decimals),
            "ema_20": round(ema20, decimals),
            "ema_50": round(ema50, decimals),
            "ema_trend": ema_trend,
            "vwap": round(vwap, decimals),
            "vwap_status": vwap_status,
            "sma_200": round(htf_sma200, decimals),
            "sma_200_30m": round(sma200_30m, decimals) if sma200_30m is not None else None,
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
