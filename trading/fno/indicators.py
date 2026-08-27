"""Indicator maths on 5-minute IST-indexed OHLCV frames.

Pure functions over pandas — no network, no clock reads except what is passed
in — so every rule in the spec (VWAP, EMA structure, opening range, relative
volume, pivots) can be checked against candles whose answer is known by
construction. See trading/test_fno.py.
"""
from __future__ import annotations

from datetime import datetime, time

import pandas as pd

from trading.fno import config as C
from trading.fno.models import IST


def _hhmm(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


def session_of(df: pd.DataFrame, day) -> pd.DataFrame:
    """Bars belonging to one trading date."""
    return df[df.index.date == day]


def sessions(df: pd.DataFrame) -> list:
    """Trading dates present in the frame, oldest first."""
    return sorted({ts.date() for ts in df.index})


def closed_bars(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Drop any bar whose interval has not finished by `now`.

    Acting on a forming bar is the classic intraday look-ahead bug: its high,
    low and volume are all still moving.
    """
    if df.empty:
        return df
    end = df.index + pd.Timedelta(minutes=C.BAR_MINUTES)
    return df[end <= pd.Timestamp(now)]


def has_volume(df: pd.DataFrame) -> bool:
    """Does this frame carry real traded volume?

    Index feeds report Volume=0 for every bar. A VWAP computed off that is not
    a VWAP — callers must check this rather than silently comparing price to a
    typical-price fallback.
    """
    return bool(len(df)) and float(df["Volume"].astype(float).sum()) > 0


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session-anchored VWAP over typical price.

    Bars before any volume has traded fall back to the typical price, which is
    the correct value for a session that has not traded yet. Use has_volume()
    to decide whether the result means anything at all.
    """
    tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
    vol = df["Volume"].astype(float)
    cum_vol = vol.cumsum()
    cum_pv = (tp * vol).cumsum()
    traded = cum_vol > 0
    return (cum_pv / cum_vol.where(traded, 1.0)).where(traded, tp)


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = C.ATR_PERIOD) -> pd.Series:
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def slope(series: pd.Series, bars: int = C.VWAP_SLOPE_BARS) -> float:
    """Per-bar change over the last `bars`, as a fraction of the level.

    Returned as a percentage of price so it is comparable across stocks.
    """
    if len(series) < bars + 1:
        return 0.0
    now, then = float(series.iloc[-1]), float(series.iloc[-1 - bars])
    if then == 0:
        return 0.0
    return (now - then) / then / bars * 100.0


def opening_range(day_df: pd.DataFrame) -> tuple[float, float, int]:
    """High/low of MARKET_OPEN → OPENING_RANGE_END, and the bar count.

    The window is half-open [09:15, 09:30) so the 09:30 bar — which trades
    *after* the range is set — is not folded into the range it must break.
    """
    start, end = _hhmm(C.MARKET_OPEN), _hhmm(C.OPENING_RANGE_END)
    t = day_df.index.time
    win = day_df[(t >= start) & (t < end)]
    if win.empty:
        return float("nan"), float("nan"), 0
    return float(win["High"].max()), float(win["Low"].min()), len(win)


def after_opening_range(day_df: pd.DataFrame) -> pd.DataFrame:
    end = _hhmm(C.OPENING_RANGE_END)
    return day_df[day_df.index.time >= end]


def cumulative_volume_at(day_df: pd.DataFrame, cutoff: time) -> float:
    return float(day_df[day_df.index.time < cutoff]["Volume"].sum())


def rvol(df: pd.DataFrame, day, cutoff: time, lookback: int = 5) -> float | None:
    """Today's volume-to-now vs the average volume-to-same-time on prior days.

    Compares like with like: 09:45 today against 09:45 on each prior session,
    not against a full-day average, which would make every morning look thin.
    """
    days = [d for d in sessions(df) if d < day][-lookback:]
    if not days:
        return None
    today = cumulative_volume_at(session_of(df, day), cutoff)
    prior = [cumulative_volume_at(session_of(df, d), cutoff) for d in days]
    prior = [v for v in prior if v > 0]
    if not prior or today <= 0:
        return None
    return today / (sum(prior) / len(prior))


def prev_session_levels(df: pd.DataFrame, day) -> tuple[float | None, float | None, float | None]:
    """Previous session's high, low and close."""
    days = [d for d in sessions(df) if d < day]
    if not days:
        return None, None, None
    prev = session_of(df, days[-1])
    if prev.empty:
        return None, None, None
    return (float(prev["High"].max()), float(prev["Low"].min()),
            float(prev["Close"].iloc[-1]))


def pivot_lows(day_df: pd.DataFrame, span: int = 2) -> list[float]:
    """Local lows with `span` higher lows on both sides."""
    lows = day_df["Low"].astype(float).tolist()
    out = []
    for i in range(span, len(lows) - span):
        window = lows[i - span:i + span + 1]
        if lows[i] == min(window) and window.count(lows[i]) == 1:
            out.append(lows[i])
    return out


def higher_highs_lows(day_df: pd.DataFrame, bars: int = 6) -> tuple[bool, bool]:
    """Are the recent swing points stepping up?

    Falls back to comparing the two halves of the recent window when there are
    too few bars for pivots — which is the normal state at 09:45.
    """
    recent = day_df.tail(bars)
    if len(recent) < 4:
        return False, False
    half = len(recent) // 2
    first, second = recent.iloc[:half], recent.iloc[half:]
    hh = float(second["High"].max()) > float(first["High"].max())
    hl = float(second["Low"].min()) > float(first["Low"].min())
    return hh, hl


def is_consolidating(day_df: pd.DataFrame, atr_val: float, bars: int = 4) -> bool:
    """Recent range compressed to under ~1.2 ATR — coiling, not trending."""
    recent = day_df.tail(bars)
    if len(recent) < bars or atr_val <= 0:
        return False
    rng = float(recent["High"].max()) - float(recent["Low"].min())
    return rng <= 1.2 * atr_val
