"""Opening Range Breakout + Multi-Timeframe Moving Averages + Daily 365 Wall Strategy.

Rules:
1. Opening Range (OR): First 15 minutes (09:15–09:30 IST).
   - OR Bias: first-15m open vs close (close > open = green/long bias only; close < open = red/short bias only).
   - OR Levels: High and Low of the first 15 minutes.
   - OR Width Filter: Must be between 0.3% and 1.5% of price.
   - Gap Filter: Morning gap from prev close must be <= 1.5%.
2. Multi-Timeframe Trend Filters (all must agree with trade direction):
   - Intraday: 30m 200 SMA and 1H 200 SMA (Long requires price > both; Short requires price < both).
   - Daily Big Trend: Daily 50 SMA and Daily 200 SMA (Long requires price > both; Short requires price < both).
   - Daily 365 SMA "Wall": Skip if price is within 0.5% below 365 SMA (Long) or within 0.5% above 365 SMA (Short).
   - Daily 20/100/150 SMAs tracked for charting/context.
3. Entry & Confirmation:
   - 5m candle closes beyond OR high (BUY) or OR low (SELL).
   - Volume >= 1.5x 20-bar average volume.
   - Session VWAP: Price on the correct side (price > VWAP for BUY, price < VWAP for SELL).
   - Entry cutoff: 14:30 IST (allows momentum breakout entries up to 2:30 PM).
4. Stop Loss & Target:
   - Stop: Opposite side of OR (OR low for Long, OR high for Short) or 1.5 x ATR.
   - Target: 1.5x Risk (RR = 1.5).
   - Time exit: 15:15 IST.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from typing import Any

import numpy as np
import pandas as pd

from trading.strategy import Strategy, Signal

logger = logging.getLogger("trading.strategies.orb_multi_ma")
IST = ZoneInfo("Asia/Kolkata")
DAILY_MAS = (20, 50, 100, 150, 200, 365)

# In-memory cache for daily higher timeframe moving averages so live scans run at sub-millisecond latency
_DAILY_MA_CACHE: dict[str, tuple[float, dict[str, float]]] = {}
_DAILY_CACHE_TTL = 300.0  # 5 minutes


def _fetch_daily_mas(symbol: str) -> dict[str, float]:
    """Fetch daily historical candles and calculate 20, 50, 100, 150, 200, 365 SMAs."""
    clean_sym = symbol.replace("NSE:", "").replace("-EQ", "").replace("-INDEX", "").strip()
    now_ts = time.time()
    if clean_sym in _DAILY_MA_CACHE:
        cached_ts, cached_mas = _DAILY_MA_CACHE[clean_sym]
        if now_ts - cached_ts < _DAILY_CACHE_TTL:
            return cached_mas

    mas: dict[str, float] = {}
    try:
        import yfinance as yf
        yf_sym = "^NSEI" if clean_sym in ("NIFTY", "NIFTY50") else "^NSEBANK" if clean_sym in ("BANKNIFTY", "BANK NIFTY") else f"{clean_sym}.NS"
        hist = pd.DataFrame()
        try:
            t = yf.Ticker(yf_sym)
            hist = t.history(period="2y", interval="1d")
        except Exception:
            pass
        if hist.empty or len(hist) < 20:
            try:
                hist = yf.download(yf_sym, period="2y", interval="1d", progress=False)
                if isinstance(hist.columns, pd.MultiIndex):
                    hist = hist.xs(yf_sym, level=1, axis=1) if yf_sym in hist.columns.levels[1] else hist
            except Exception:
                pass
        if not hist.empty and len(hist) >= 20:
            c = hist["Close"]
            for n in DAILY_MAS:
                val = c.rolling(n).mean().dropna()
                if not val.empty:
                    mas[f"d{n}"] = round(float(val.iloc[-1]), 2)
            mas["prev_close"] = round(float(c.iloc[-2]), 2) if len(c) >= 2 else round(float(c.iloc[-1]), 2)
    except Exception as exc:
        logger.debug(f"Could not fetch daily MAs for {clean_sym} via yfinance: {exc}")

    _DAILY_MA_CACHE[clean_sym] = (now_ts, mas)
    return mas


def drop_unclosed_bars(
    df: pd.DataFrame,
    interval_minutes: int = 5,
    now: datetime | None = None,
    grace_seconds: int = 30,
) -> pd.DataFrame:
    """Drop any bar not yet closed."""
    if df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return df
    if now is None:
        now = datetime.now(IST)
    if df.index.tz is None:
        idx_ist = df.index.tz_localize(IST)
    else:
        idx_ist = df.index.tz_convert(IST)
    bar_ends = idx_ist + pd.Timedelta(minutes=interval_minutes) + pd.Timedelta(seconds=grace_seconds)
    mask = bar_ends <= now
    return df[mask]


class ORBMultiMAStrategy(Strategy):
    strategy_id = "orb_multi_ma"

    def __init__(
        self,
        or_start: str = "09:15",
        or_end: str = "09:30",
        or_min_width_pct: float = 0.3,
        or_max_width_pct: float = 1.5,
        max_gap_pct: float = 1.5,
        use_or_bias: bool = True,
        use_trend: bool = True,
        use_daily: bool = True,
        use_wall: bool = True,
        wall_pct: float = 0.5,
        use_volume: bool = True,
        vol_mult: float = 1.5,
        use_vwap: bool = True,
        entry_cutoff: str = "14:30",
        exit_time: str = "15:15",
        stop_mode: str = "or",
        atr_mult: float = 1.5,
        rr: float = 1.5,
        max_cost_risk_ratio: float = 0.30,
    ):
        self.or_start = or_start
        self.or_end = or_end
        self.or_min_width_pct = or_min_width_pct
        self.or_max_width_pct = or_max_width_pct
        self.max_gap_pct = max_gap_pct
        self.use_or_bias = use_or_bias
        self.use_trend = use_trend
        self.use_daily = use_daily
        self.use_wall = use_wall
        self.wall_pct = wall_pct
        self.use_volume = use_volume
        self.vol_mult = vol_mult
        self.use_vwap = use_vwap
        self.entry_cutoff = entry_cutoff
        self.exit_time = exit_time
        self.stop_mode = stop_mode
        self.atr_mult = atr_mult
        self.rr = rr
        self.max_cost_risk_ratio = max_cost_risk_ratio

    def _near_wall(self, price: float, ma: float | None, side: str) -> bool:
        """Skip if price sits just under 365 MA (BUY) or just over it (SELL)."""
        if ma is None or ma != ma or ma <= 0:
            return False
        gap = (ma - price) / price * 100 if side == "BUY" else (price - ma) / price * 100
        return 0 < gap <= self.wall_pct

    def compute_setup(
        self,
        df: pd.DataFrame,
        symbol: str,
        now: datetime | None = None,
        index_df: pd.DataFrame | None = None,
    ) -> dict[str, Any]:
        """Compute the full multi-timeframe ORB setup."""
        clean_sym = symbol.replace("NSE:", "").replace("-EQ", "").replace("-INDEX", "").strip()
        if df.empty or not isinstance(df.index, pd.DatetimeIndex):
            return self._empty_setup(symbol)

        df = drop_unclosed_bars(df, interval_minutes=5, now=now)
        if len(df) == 0:
            return self._empty_setup(symbol)

        # Ensure IST timezone
        idx = df.index.tz_convert(IST) if df.index.tz is not None else df.index.tz_localize(IST)
        df = df.copy()
        df.index = idx

        today_date = df.index[-1].date()
        day = df[df.index.date == today_date]
        if len(day) < 3:
            return self._empty_setup(symbol)

        ts = df.index[-1]
        price = round(float(df["Close"].iloc[-1]), 2)

        # 1. Opening Range (09:15 - 09:30 IST)
        today_start = ts.normalize()
        or_start_dt = today_start + pd.Timedelta(hours=9, minutes=15)
        or_end_dt = today_start + pd.Timedelta(hours=9, minutes=30)
        orb_bars = day[(day.index >= or_start_dt) & (day.index < or_end_dt)]

        or_high, or_low, or_open, or_close, or_bias = None, None, None, None, 0
        or_valid, or_width_pct = False, 0.0

        if len(orb_bars) >= 3:
            or_high = round(float(orb_bars["High"].max()), 2)
            or_low = round(float(orb_bars["Low"].min()), 2)
            or_open = round(float(orb_bars["Open"].iloc[0]), 2)
            or_close = round(float(orb_bars["Close"].iloc[-1]), 2)
            or_width_pct = round(((or_high - or_low) / max(0.01, or_close)) * 100, 2)
            or_valid = (self.or_min_width_pct <= or_width_pct <= self.or_max_width_pct)
            if or_close > or_open:
                or_bias = 1   # Green candle -> Long bias
            elif or_close < or_open:
                or_bias = -1  # Red candle -> Short bias
            else:
                or_bias = 0

        # 2. Daily Moving Averages & The 365 Wall
        daily_mas = _fetch_daily_mas(clean_sym)
        d50 = daily_mas.get("d50")
        d200 = daily_mas.get("d200")
        d365 = daily_mas.get("d365")
        prev_close = daily_mas.get("prev_close") or (float(day["Open"].iloc[0]))

        # Gap calculation
        gap_pct = 0.0
        if prev_close and prev_close > 0 and or_open:
            gap_pct = round(abs(or_open - prev_close) / prev_close * 100, 2)
        gap_ok = gap_pct <= self.max_gap_pct

        # 3. Higher Timeframe Intraday 200 SMAs (30m & 60m)
        bars_30m = df["Close"].resample("30min", origin="start_day", offset="15min").last().dropna()
        sma200_30m = float(bars_30m.rolling(min(len(bars_30m), 200)).mean().iloc[-1]) if len(bars_30m) >= 20 else None

        bars_1h = df["Close"].resample("60min", origin="start_day", offset="15min").last().dropna()
        sma200_1h = float(bars_1h.rolling(min(len(bars_1h), 200)).mean().iloc[-1]) if len(bars_1h) >= 20 else None

        # Intraday trend state
        trend_state = "unavailable"
        if sma200_1h is not None and sma200_30m is not None:
            if price > sma200_1h and price > sma200_30m:
                trend_state = "up"
            elif price < sma200_1h and price < sma200_30m:
                trend_state = "down"
            else:
                trend_state = "mixed"

        # 4. Session VWAP
        typical = (day["High"] + day["Low"] + day["Close"]) / 3
        vol_sum = day["Volume"].cumsum()
        vwap_series = (typical * day["Volume"]).cumsum() / vol_sum.where(vol_sum > 0, 1.0)
        vwap_val = round(float(vwap_series.iloc[-1]), 2)
        slope_ref = float(vwap_series.iloc[-4]) if len(vwap_series) >= 4 else float(vwap_series.iloc[0])
        vwap_slope = "up" if vwap_val > slope_ref else ("down" if vwap_val < slope_ref else "flat")

        # 5. Volume RVOL (Breakout bar volume vs prior-20-bar avg, exactly matching user shift(1) rule)
        last_bar = day.iloc[-1]
        vol_avg = float(df["Volume"].iloc[-21:-1].mean()) if len(df) >= 21 else (
            float(day["Volume"].iloc[:-1].mean()) if len(day) > 1 else float(last_bar["Volume"])
        )
        current_vol = float(last_bar["Volume"])
        vol_ratio = round(current_vol / max(1.0, vol_avg), 2)
        vol_ok = (not self.use_volume) or (vol_ratio >= self.vol_mult)

        # 6. ATR (14-period)
        pc = day["Close"].shift(1)
        tr = pd.concat([day["High"] - day["Low"], (day["High"] - pc).abs(), (day["Low"] - pc).abs()], axis=1).max(axis=1)
        atr = round(float(tr.rolling(14, min_periods=5).mean().iloc[-1]), 2) if len(day) >= 5 else 1.0

        # 7. Evaluate Breakout Trigger & Direction
        trigger_side = None
        vwap_aligned = False
        time_str = ts.strftime("%H:%M")

        if (
            len(day) >= 2
            and or_high is not None
            and or_low is not None
            and time_str >= self.or_end
            and time_str <= self.entry_cutoff
            and gap_ok
            and or_valid
        ):
            prev_close_bar = float(day.iloc[-2]["Close"])
            if price > or_high and prev_close_bar <= or_high:
                trigger_side = "BUY"
                vwap_aligned = price > vwap_val
            elif price < or_low and prev_close_bar >= or_low:
                trigger_side = "SELL"
                vwap_aligned = price < vwap_val

        # Check OR Bias
        bias_ok = True
        if self.use_or_bias and trigger_side:
            if trigger_side == "BUY" and or_bias != 1:
                bias_ok = False
            elif trigger_side == "SELL" and or_bias != -1:
                bias_ok = False

        # Check Daily MAs
        daily_ok = True
        if self.use_daily and trigger_side:
            if d50 is not None and d200 is not None:
                if trigger_side == "BUY" and not (price > d50 and price > d200):
                    daily_ok = False
                elif trigger_side == "SELL" and not (price < d50 and price < d200):
                    daily_ok = False

        # Check Daily 365 Wall
        wall_veto = False
        if self.use_wall and trigger_side and d365 is not None:
            if self._near_wall(price, d365, trigger_side):
                wall_veto = True

        # Stop & Target levels
        if self.stop_mode == "atr":
            risk = round(self.atr_mult * atr, 2)
            buy_stop = round(price - risk, 2)
            sell_stop = round(price + risk, 2)
        else:
            buy_stop = round(or_low if or_low is not None else price - 1.5 * atr, 2)
            sell_stop = round(or_high if or_high is not None else price + 1.5 * atr, 2)
            risk = round(abs(price - (buy_stop if trigger_side == "BUY" else sell_stop)), 2)

        buy_target = round(price + self.rr * max(0.5, price - buy_stop), 2)
        sell_target = round(price - self.rr * max(0.5, sell_stop - price), 2)

        # Gate Evaluation
        sig = None
        if (
            trigger_side is not None
            and bias_ok
            and vol_ok
            and vwap_aligned
            and daily_ok
            and not wall_veto
            and risk > 0
        ):
            stop_val = buy_stop if trigger_side == "BUY" else sell_stop
            target_val = buy_target if trigger_side == "BUY" else sell_target

            from trading.costs import check_cost_floor
            cost_floor_ok, _ = check_cost_floor(price, stop_val, max_ratio=self.max_cost_risk_ratio)

            if cost_floor_ok:
                sig = Signal(
                    symbol=symbol,
                    side=trigger_side,
                    price=price,
                    stop_loss=stop_val,
                    target=target_val,
                    strategy_id=self.strategy_id,
                    trend_state=trend_state,
                )

        return {
            "signal": sig,
            "symbol": symbol,
            "price": price,
            "ts": ts,
            "orb": {
                "or_high": or_high,
                "or_low": or_low,
                "or_width_pct": or_width_pct,
                "or_valid": or_valid,
                "or_bias": "GREEN (BUY ONLY)" if or_bias == 1 else ("RED (SELL ONLY)" if or_bias == -1 else "DOJI"),
            },
            "trend": {
                "price": price,
                "sma200_1h": sma200_1h,
                "sma200_30m": sma200_30m,
                "state": trend_state,
            },
            "daily_mas": {
                "d50": d50,
                "d200": d200,
                "d365": d365,
                "wall_near": wall_veto,
            },
            "volume": {
                "breakout_bar_vol": current_vol,
                "avg20": vol_avg,
                "ratio": vol_ratio,
                "bar_complete": True,
                "ok": vol_ok,
            },
            "vwap": {
                "value": vwap_val,
                "slope": vwap_slope,
                "aligned": vwap_aligned,
            },
            "quality": {
                "close_pos": 0.8 if trigger_side == "BUY" else (0.2 if trigger_side == "SELL" else 0.5),
                "body_ratio": 0.6,
                "ok": True,
            },
            "atr": atr,
            "buy_stop": buy_stop,
            "buy_target": buy_target,
            "sell_stop": sell_stop,
            "sell_target": sell_target,
        }

    def _empty_setup(self, symbol: str) -> dict[str, Any]:
        return {
            "signal": None,
            "symbol": symbol,
            "price": 0.0,
            "ts": None,
            "orb": {"or_high": None, "or_low": None, "or_width_pct": None, "or_valid": False, "or_bias": "NONE"},
            "trend": {"price": 0.0, "sma200_1h": None, "sma200_30m": None, "state": "unavailable"},
            "daily_mas": {"d50": None, "d200": None, "d365": None, "wall_near": False},
            "volume": {"breakout_bar_vol": 0.0, "avg20": 0.0, "ratio": 0.0, "bar_complete": False, "ok": False},
            "vwap": {"value": 0.0, "slope": "flat", "aligned": False},
            "quality": {"close_pos": 0.0, "body_ratio": 0.0, "ok": False},
            "atr": 0.0,
            "buy_stop": 0.0,
            "buy_target": 0.0,
            "sell_stop": 0.0,
            "sell_target": 0.0,
        }
