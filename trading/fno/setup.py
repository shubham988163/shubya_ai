"""Breakout structure detection and structural entry/stop/target (§5, §12–14).

The setup this looks for is deliberately narrow:

    BREAKOUT → RETEST → HOLD → CONTINUATION

A single candle poking above the opening range is not a breakout here; it has
to close above the level, on expanded volume, and still be above it now. The
retest is scored, not required — but without one the setup cannot reach A+.

Stops come from structure (retest low, breakout-bar low, swing low, range low),
never from a round percentage, and the trade is rejected outright if no such
level sits at a sane distance below entry.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from trading.fno import config as C
from trading.fno import indicators as ind
from trading.fno.models import Levels, Structure, Trade


def build_levels(df: pd.DataFrame, day, now: datetime,
                 pct_change: float | None) -> Levels | None:
    """Read every level the spec's §4 checklist asks for off the 5m frame.

    `df` must contain prior sessions too — a 20/50 EMA seeded only from
    today's six bars is not a 20/50 EMA.
    """
    day_df = ind.session_of(df, day)
    if day_df.empty or not ind.has_volume(day_df):
        return None          # no volume means no VWAP and no RVOL to stand on

    or_high, or_low, or_bars = ind.opening_range(day_df)
    if or_bars == 0 or or_high != or_high:      # NaN check
        return None

    vw = ind.vwap(day_df)
    atr_s = ind.atr(df)
    atr_val = float(atr_s.iloc[-1])
    if not atr_val or atr_val != atr_val:
        atr_val = max(float(day_df["High"].max() - day_df["Low"].min()) / 4, 0.01)

    # EMAs need history; below the span they are still warming up and must not
    # be presented as if they were real.
    ema_f = ema_s = ema_f_sl = ema_s_sl = None
    closes = df["Close"].astype(float)
    if len(closes) >= C.EMA_FAST:
        f = ind.ema(closes, C.EMA_FAST)
        ema_f, ema_f_sl = float(f.iloc[-1]), ind.slope(f)
    if len(closes) >= C.EMA_SLOW:
        s = ind.ema(closes, C.EMA_SLOW)
        ema_s, ema_s_sl = float(s.iloc[-1]), ind.slope(s)

    prev_h, prev_l, prev_c = ind.prev_session_levels(df, day)
    price = float(day_df["Close"].iloc[-1])
    day_high = float(day_df["High"].max())
    day_low = float(day_df["Low"].min())

    # Resistance the setup is built on: the opening range high, unless the
    # previous day's high sits just above it — then that is the real barrier.
    resistance = or_high
    if prev_h and or_high < prev_h <= or_high + 0.5 * atr_val:
        resistance = prev_h

    # The day high is only "resistance" once price has backed away from it; in a
    # fresh breakout it sits on the current bar and would veto every setup.
    overhead_levels = [prev_h, prev_c]
    if day_high > price + C.OVERHEAD_MIN_ATR * atr_val:
        overhead_levels.append(day_high)
    overhead = _next_resistance_above(price, overhead_levels)

    pivots = ind.pivot_lows(day_df)
    support = max([p for p in pivots if p < price] or [or_low], default=or_low)

    bar_vols = day_df["Volume"].astype(float)
    cutoff = now.time()
    return Levels(
        price=price, or_high=or_high, or_low=or_low,
        vwap=float(vw.iloc[-1]), vwap_slope=ind.slope(vw),
        ema_fast=ema_f, ema_slow=ema_s,
        ema_fast_slope=ema_f_sl, ema_slow_slope=ema_s_sl,
        atr=atr_val, prev_high=prev_h, prev_low=prev_l, prev_close=prev_c,
        day_high=day_high, day_low=day_low,
        support=support, resistance=resistance, overhead=overhead,
        rvol=ind.rvol(df, day, cutoff),
        last_bar_vol=float(bar_vols.iloc[-1]),
        avg_bar_vol=float(bar_vols.mean()),
        pct_change=pct_change,
    )


def _next_resistance_above(price: float, levels) -> float | None:
    above = sorted(l for l in levels if l and l > price * 1.001)
    return above[0] if above else None


def read_structure(df: pd.DataFrame, day, lv: Levels) -> Structure:
    """Classify today's price action against the resistance level."""
    day_df = ind.session_of(df, day)
    post = ind.after_opening_range(day_df)
    R, atr_val = lv.resistance, lv.atr
    notes: list[str] = []

    hh, hl = ind.higher_highs_lows(day_df)
    consolidating = ind.is_consolidating(day_df, atr_val)

    breakout = False
    breakout_time = None
    breakout_vol_mult = None
    breakout_idx = None
    trigger = R + C.BREAKOUT_BUFFER_ATR * atr_val

    for i, (ts, bar) in enumerate(post.iterrows()):
        if float(bar["Close"]) > trigger:
            breakout = True
            breakout_time = ts.to_pydatetime()
            breakout_idx = i
            # Baseline is the bars *before* the breakout — including the
            # breakout bar (or later ones) in its own baseline would flatten
            # exactly the expansion this is trying to measure.
            prior = day_df["Volume"][day_df.index < ts]
            base = float(prior.mean()) if len(prior) else 0.0
            breakout_vol_mult = float(bar["Volume"]) / base if base > 0 else None
            break

    holding = False
    retested = False
    retest_low = None
    failed = False

    if breakout:
        after = post.iloc[breakout_idx:]
        holding = (lv.price > R
                   and float(after["Close"].min()) > R - C.HOLD_TOLERANCE_ATR * atr_val)
        failed = lv.price < R - C.HOLD_TOLERANCE_ATR * atr_val
        # A retest is a bar that dips back to the level and closes above it.
        pullbacks = after.iloc[1:]
        band = R + C.RETEST_TOLERANCE_ATR * atr_val
        touched = pullbacks[(pullbacks["Low"] <= band) & (pullbacks["Close"] > R)]
        if not touched.empty:
            retested = True
            retest_low = float(touched["Low"].min())
            if retest_low < R - C.HOLD_TOLERANCE_ATR * atr_val:
                notes.append("retest dipped below the breakout level before recovering")
        if breakout_vol_mult is not None and breakout_vol_mult < C.WEAK_VOL_MULT:
            notes.append(f"breakout bar volume only {breakout_vol_mult:.1f}x the "
                         "prior-bar average")

    # Rejection: tagged the level repeatedly but never closed through it.
    rejection = (not breakout
                 and float(day_df["High"].max()) >= R
                 and lv.price < R)

    extended = ((lv.price - R) > C.EXTENDED_ATR * atr_val
                or (lv.vwap > 0
                    and (lv.price - lv.vwap) / lv.vwap * 100 > C.EXTENDED_VWAP_PCT))
    if extended:
        notes.append("price is extended from the breakout level / VWAP — "
                     "entering here is chasing")

    return Structure(
        higher_highs=hh, higher_lows=hl, consolidating=consolidating,
        breakout=breakout, breakout_time=breakout_time,
        breakout_vol_mult=breakout_vol_mult, holding=holding,
        retested=retested, retest_low=retest_low, failed_breakout=failed,
        rejection=rejection, extended=extended, notes=notes,
    )


def build_trade(df: pd.DataFrame, day, lv: Levels, st: Structure
                ) -> tuple[Trade | None, list[tuple[str, str]]]:
    """Entry zone, structural stop and 1:2 / 1:3 targets.

    Returns (trade, [(category, reason), …]) where the reasons are why a trade
    could not be built. A trade is only produced when the level structure
    supports one; §12 forbids handing out an entry just because price is near
    resistance.
    """
    problems: list[tuple[str, str]] = []
    if not st.breakout:
        return None, [("structure",
                       "no confirmed breakout — price has not closed above resistance")]
    if not st.holding:
        return None, [("structure",
                       "breakout not holding — price is back at/below the level")]

    R, atr_val = lv.resistance, lv.atr
    entry_low = R
    entry_high = R + 0.40 * atr_val
    entry = min(max(lv.price, entry_low), entry_high)

    # Structural stop: the highest real level that still sits below entry.
    candidates: list[tuple[float, str]] = []
    if st.retest_low:
        candidates.append((st.retest_low, "below the retest low"))
    day_df = ind.session_of(df, day)
    post = ind.after_opening_range(day_df)
    if st.breakout_time is not None and not post.empty:
        bo = post[post.index >= pd.Timestamp(st.breakout_time)]
        if not bo.empty:
            candidates.append((float(bo["Low"].iloc[0]), "below the breakout bar low"))
    for p in ind.pivot_lows(day_df):
        candidates.append((p, "below the last intraday swing low"))
    candidates.append((lv.or_low, "below the opening-range low"))

    usable = [(lvl, why) for lvl, why in candidates if lvl < entry]
    if not usable:
        return None, [("risk",
                       "no structural level below entry to place a stop against")]
    base, basis = max(usable, key=lambda x: x[0])
    stop = base - C.SL_BUFFER_ATR * atr_val

    # A shallow retest can leave the stop *above* the breakout level, and an
    # entry zone that still started at the level would invite a fill below the
    # stop — a plan that is under water the moment it fills. Lift the floor of
    # the zone so every price inside it has real risk beneath it.
    entry_low = max(entry_low, stop + C.ENTRY_FLOOR_ATR * atr_val)
    if entry_low > entry_high:
        return None, [("risk",
                       f"the structural stop ({stop:.2f}) sits inside the entry "
                       f"zone — there is no price here that is worth the risk")]
    entry = min(max(lv.price, entry_low), entry_high)

    risk = entry - stop
    if risk <= 0:
        return None, [("risk", "stop-loss could not be placed below the entry")]

    risk_pct = risk / entry * 100
    if risk_pct > C.MAX_RISK_PCT:
        problems.append(("risk", f"structural stop is {risk_pct:.2f}% away — wider "
                                 f"than the {C.MAX_RISK_PCT:.2f}% cap"))
    if risk_pct < C.MIN_RISK_PCT:
        problems.append(("risk", f"structural stop is only {risk_pct:.2f}% away — "
                                 "inside noise, it would be taken out at random"))

    t1 = entry + C.TARGET_RR_1 * risk
    t2 = entry + C.TARGET_RR_2 * risk

    if lv.overhead:
        room = (lv.overhead - entry) / risk
        if room < C.MIN_RR_TO_RESISTANCE:
            problems.append((
                "risk",
                f"resistance at {lv.overhead:.2f} is only {room:.1f}x risk above "
                f"entry — cannot make the 1:{C.MIN_RR:g} target at {t1:.2f}"))

    trade = Trade(entry_low=entry_low, entry_high=entry_high, entry=entry,
                  stop=stop, stop_basis=basis, target1=t1, target2=t2,
                  risk=risk, rr1=C.TARGET_RR_1, rr2=C.TARGET_RR_2)
    return trade, problems
