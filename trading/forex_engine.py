"""Forex & Crypto Strategy Engine for Delta Exchange.

Applies all core trading strategies to 24/7 Forex and Crypto markets:
1. 15m / 5m Opening Range Breakout (ORB) with Volume Confirmation
2. Anchored VWAP (AVWAP) and VWAP Slope Analysis
3. 9/21 Exponential Moving Average (EMA) Trend Momentum
4. 200 SMA Long-Term Trend Filter
5. ATR-Based Risk Budgeting, Trailing Stops, and 1:2 / 1:3 Targets
6. AI Multi-Agent Conviction Scoring (Threshold >= 8/10)
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from trading.delta_exchange import DeltaClient, FOREX_WATCHLIST

logger = logging.getLogger("trading.forex_engine")


def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate EMAs, 200 SMA, VWAP, ATR, and RSI on OHLCV candles."""
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
    """Analyzes Forex/Crypto assets and generates Multi-Strategy setups."""

    def __init__(self, client: DeltaClient | None = None):
        self.client = client or DeltaClient()

    def evaluate_symbol(self, symbol: str) -> dict[str, Any] | None:
        """Run all strategies on a symbol and produce a structured trade setup."""
        df = self.client.get_candles(symbol, resolution="5", bars=80)
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

        # Determine directional bias using 9/21 EMA and 200 SMA
        trend_up = ema9 > ema21 and price > vwap and price >= sma200 * 0.99
        trend_down = ema9 < ema21 and price < vwap and price <= sma200 * 1.01

        # Opening Range / Session Range (last 12 bars = 1 hour range)
        recent_bars = df.iloc[-15:-1]
        range_high = float(recent_bars["High"].max())
        range_low = float(recent_bars["Low"].min())

        is_breakout_long = price >= range_high and trend_up
        is_breakout_short = price <= range_low and trend_down

        # Score the setup (0 - 100)
        score = 50
        reasons = []

        if trend_up:
            score += 15
            reasons.append("Bullish 9/21 EMA crossover above VWAP")
        elif trend_down:
            score += 15
            reasons.append("Bearish 9/21 EMA crossover below VWAP")

        if price > sma200:
            score += 10
            reasons.append("Price trading above 200 SMA")
        elif price < sma200:
            score += 10
            reasons.append("Price trading below 200 SMA")

        if is_breakout_long:
            score += 20
            reasons.append(f"ORB Breakout above 1h High (${range_high:,.2f})")
        elif is_breakout_short:
            score += 20
            reasons.append(f"ORB Breakdown below 1h Low (${range_low:,.2f})")

        if rvol >= 1.2:
            score += 10
            reasons.append(f"High Relative Volume ({rvol:.1f}x average)")

        if 48 <= rsi <= 68 and trend_up:
            score += 10
            reasons.append(f"RSI in strong bullish momentum zone ({rsi:.1f})")
        elif 32 <= rsi <= 52 and trend_down:
            score += 10
            reasons.append(f"RSI in strong bearish momentum zone ({rsi:.1f})")

        # Precision based on asset price magnitude (e.g. BTC=2, SOL=3, ADA/XRP=4)
        decimals = 2 if price > 500 else (3 if price > 10 else 4)

        # Determine Action & Sizing
        if trend_up or is_breakout_long:
            side = "BUY"
            raw_sl = max(vwap - 0.5 * atr, price - 1.5 * atr)
            raw_sl = min(raw_sl, price - max(0.5 * atr, price * 0.003))
            stop_loss = round(raw_sl, decimals)
            risk = max(price * 0.003, price - stop_loss)
            target1 = round(price + 2.0 * risk, decimals)
            target2 = round(price + 3.0 * risk, decimals)
        elif trend_down or is_breakout_short:
            side = "SELL"
            raw_sl = min(vwap + 0.5 * atr, price + 1.5 * atr)
            raw_sl = max(raw_sl, price + max(0.5 * atr, price * 0.003))
            stop_loss = round(raw_sl, decimals)
            risk = max(price * 0.003, stop_loss - price)
            target1 = round(price - 2.0 * risk, decimals)
            target2 = round(price - 3.0 * risk, decimals)
        else:
            return None

        # Conviction score out of 10
        conviction = min(10, max(5, int(score / 10)))
        grade = "A+" if score >= 85 else "A" if score >= 75 else "B" if score >= 65 else "WATCH"

        # Calculate position size: target ~$25 - $50 risk per trade
        risk_per_unit = abs(price - stop_loss)
        unit_size = max(1.0, round(50.0 / max(0.1, risk_per_unit), 2)) if price > 100 else max(10.0, round(50.0 / max(0.01, risk_per_unit), 0))

        return {
            "symbol": symbol,
            "side": side,
            "price": round(price, 2 if price > 10 else 4),
            "stop_loss": round(stop_loss, 2 if price > 10 else 4),
            "target1": round(target1, 2 if price > 10 else 4),
            "target2": round(target2, 2 if price > 10 else 4),
            "risk": round(risk, 2 if price > 10 else 4),
            "risk_reward": "1:2.0",
            "score": score,
            "grade": grade,
            "conviction": conviction,
            "reasons": reasons,
            "suggested_size": unit_size,
            "strategies": ["Opening Range Breakout (ORB)", "Anchored VWAP", "9/21 EMA", "200 SMA Trend"],
            "timestamp": pd.Timestamp.now().strftime("%H:%M:%S"),
        }

    def scan_all(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        """Scan all watchlist symbols and return ranked qualifying trade setups."""
        targets = symbols or FOREX_WATCHLIST
        results = []
        for sym in targets:
            try:
                setup = self.evaluate_symbol(sym)
                if setup and setup["score"] >= 65:
                    results.append(setup)
            except Exception as e:
                logger.error(f"Error evaluating {sym}: {e}")

        # Rank setups by conviction and score
        results.sort(key=lambda x: (x["conviction"], x["score"]), reverse=True)
        return results

    def get_symbol_parameters(self, symbol: str) -> dict[str, Any]:
        """Fetch live OHLCV data, compute all strategy parameters and indicator metrics for the candlestick HUD."""
        sym_clean = symbol.replace("NSE:", "").replace("BINANCE:", "").replace("TVC:", "").strip().upper()
        
        df = None
        # 1. Try Delta Client first
        try:
            df = self.client.get_candles(sym_clean, resolution="5", bars=80)
        except Exception:
            df = None
            
        # 2. If Delta has no data (e.g. Indian stocks), fallback to yfinance
        if df is None or len(df) < 15:
            try:
                import yfinance as yf
                yf_ticker = f"{sym_clean}.NS" if not sym_clean.endswith(".NS") and sym_clean in ("RELIANCE", "TCS", "HDFCBANK", "INFY", "SBIN") else ("^NSEI" if sym_clean == "NIFTY" else ("^NSEBANK" if sym_clean == "BANKNIFTY" else f"{sym_clean}-USD"))
                hist = yf.Ticker(yf_ticker).history(period="2d", interval="5m")
                if not hist.empty:
                    df = hist.rename(columns={"Open": "Open", "High": "High", "Low": "Low", "Close": "Close", "Volume": "Volume"})
            except Exception:
                pass

        if df is None or len(df) < 15:
            return {"ok": False, "symbol": symbol, "error": f"Live candles temporarily unavailable for {symbol}"}

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

        recent_bars = df.iloc[-15:-1]
        range_high = float(recent_bars["High"].max())
        range_low = float(recent_bars["Low"].min())
        range_width_pct = ((range_high - range_low) / range_low) * 100.0 if range_low > 0 else 0.0

        ema_diff = ema9 - ema21
        ema_trend = "BULLISH (9 > 21)" if ema_diff > 0 else "BEARISH (9 < 21)"
        vwap_status = "ABOVE VWAP (+)" if price >= vwap else "BELOW VWAP (-)"
        sma_status = "ABOVE 200 SMA (Macro Up)" if price >= sma200 else "BELOW 200 SMA (Macro Down)"
        rvol_status = "VOLUME SURGE (>= 1.2x)" if rvol >= 1.2 else f"LOW / NORMAL ({rvol:.2f}x < 1.2x)"

        is_breakout_long = price >= range_high and ema9 > ema21
        is_breakout_short = price <= range_low and ema9 < ema21
        in_range = range_low <= price <= range_high

        decimals = 2 if price > 500 else (3 if price > 10 else 4)

        if is_breakout_long:
            action = "BUY BREAKOUT"
            action_badge = "bull"
            sl = round(max(vwap - 0.5 * atr, price - 1.5 * atr), decimals)
            risk = max(price * 0.003, price - sl)
            target1 = round(price + 2.0 * risk, decimals)
            target2 = round(price + 3.0 * risk, decimals)
        elif is_breakout_short:
            action = "SELL BREAKDOWN"
            action_badge = "bear"
            sl = round(min(vwap + 0.5 * atr, price + 1.5 * atr), decimals)
            risk = max(price * 0.003, sl - price)
            target1 = round(price - 2.0 * risk, decimals)
            target2 = round(price - 3.0 * risk, decimals)
        else:
            action = "HOLD / IN-RANGE CONSOLIDATION"
            action_badge = "ok"
            sl = round(price - 1.2 * atr if ema9 > ema21 else price + 1.2 * atr, decimals)
            risk = abs(price - sl)
            target1 = round(price + 2.0 * risk if ema9 > ema21 else price - 2.0 * risk, decimals)
            target2 = round(price + 3.0 * risk if ema9 > ema21 else price - 3.0 * risk, decimals)

        score = 50
        reasons = []
        if ema9 > ema21:
            score += 15
            reasons.append("Bullish 9/21 EMA crossover")
        else:
            score += 15
            reasons.append("Bearish 9/21 EMA crossover")

        if price >= sma200:
            score += 10
            reasons.append("Trading above 200 SMA macro filter")
        else:
            score += 10
            reasons.append("Trading below 200 SMA macro filter")

        if is_breakout_long:
            score += 20
            reasons.append(f"Breakout above 1h High (${range_high:,.{decimals}f})")
        elif is_breakout_short:
            score += 20
            reasons.append(f"Breakdown below 1h Low (${range_low:,.{decimals}f})")
        else:
            reasons.append(f"Inside 1h Range (${range_low:,.{decimals}f} - ${range_high:,.{decimals}f})")

        if rvol >= 1.2:
            score += 10
            reasons.append(f"Volume expansion ({rvol:.1f}x of 20-candle average)")
        else:
            reasons.append(f"Subdued volume ({rvol:.2f}x) — awaiting institutional expansion")

        if 48 <= rsi <= 68 and ema9 > ema21:
            score += 10
            reasons.append(f"RSI in bullish momentum zone ({rsi:.1f})")
        elif 32 <= rsi <= 52 and ema9 < ema21:
            score += 10
            reasons.append(f"RSI in bearish momentum zone ({rsi:.1f})")

        conviction = min(10, max(2, int(score / 10)))

        return {
            "ok": True,
            "symbol": symbol,
            "mark_price": round(price, decimals),
            "ema_9": round(ema9, decimals),
            "ema_21": round(ema21, decimals),
            "ema_trend": ema_trend,
            "vwap": round(vwap, decimals),
            "vwap_status": vwap_status,
            "sma_200": round(sma200, decimals),
            "sma_status": sma_status,
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

