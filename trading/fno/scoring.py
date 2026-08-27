"""Setup scoring (§11) — nine weighted components summing to 100.

Each component returns 0–1 and is multiplied by its weight, so a stock cannot
reach a tradeable score on one loud signal: a perfect breakout with no OI
confirmation, against a bearish market, tops out in the 60s.

Relative strength (§10) is not a separate bucket in the spec's weighting, so it
is folded into the price-trend component and used as the ranking tiebreak.
"""
from __future__ import annotations

from trading.fno import config as C
from trading.fno import oi as oi_mod
from trading.fno.models import Candidate, MarketContext


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _market(mkt: MarketContext) -> float:
    base = C.MARKET_SCORE.get(mkt.classification, 0.45)
    if mkt.nifty_above_vwap:
        base = min(1.0, base + 0.10)
    elif mkt.nifty_above_vwap is False:
        base = max(0.0, base - 0.10)
    if mkt.breadth_pct is not None:
        base = _clamp(base + (mkt.breadth_pct - 50) / 250)   # ±0.2 at the extremes
    return _clamp(base)


def _sector(cand: Candidate) -> tuple[float, str | None]:
    if cand.sector_strength is None:
        return 0.45, "sector strength unavailable — scored neutral"
    s = cand.sector_strength
    # −0.5% → 0, flat → 0.5, +0.5% → 1.0
    return _clamp(0.5 + s), None


def _trend(cand: Candidate) -> float:
    st, lv = cand.structure, cand.levels
    score = 0.0
    if st.higher_highs:
        score += 0.25
    if st.higher_lows:
        score += 0.25
    if lv.price > (lv.or_high + lv.or_low) / 2:
        score += 0.15
    if lv.prev_close and lv.price > lv.prev_close:
        score += 0.10
    # Relative strength vs NIFTY: +1% outperformance earns the last quarter.
    score += _clamp(cand.rel_strength / 1.0) * 0.25
    if st.failed_breakout:
        score *= 0.4
    return _clamp(score)


def _vwap(cand: Candidate) -> float:
    lv = cand.levels
    if lv.price <= lv.vwap:
        return 0.0                       # §8: a long below VWAP is not the setup
    score = 0.65
    if lv.vwap_slope > 0.01:
        score += 0.35
    elif lv.vwap_slope >= -0.01:
        score += 0.15                    # flat VWAP is acceptable, rising is better
    return _clamp(score)


def _ema(cand: Candidate) -> tuple[float, str | None]:
    lv = cand.levels
    if lv.ema_fast is None or lv.ema_slow is None:
        return 0.30, "EMA(20/50) not fully seeded — insufficient candle history"
    score = 0.0
    if lv.price > lv.ema_fast:
        score += 0.35
    if lv.ema_fast > lv.ema_slow:
        score += 0.35
    if (lv.ema_fast_slope or 0) > 0:
        score += 0.20
    if (lv.ema_slow_slope or 0) > 0:
        score += 0.10
    if lv.ema_fast < lv.ema_slow:
        score *= 0.5                     # §9: bearish stack halves whatever is left
    return _clamp(score), None


def _breakout(cand: Candidate) -> float:
    st = cand.structure
    if not st.breakout:
        # Coiling right under resistance is a watch, not a breakout.
        return 0.25 if st.consolidating and not st.rejection else 0.0
    if st.failed_breakout:
        return 0.0
    if not st.holding:
        return 0.20
    score = 0.80
    if cand.levels.price > cand.levels.resistance + 0.2 * cand.levels.atr:
        score += 0.20                    # clear of the level, not sitting on it
    return _clamp(score)


def _volume(cand: Candidate) -> float:
    st, lv = cand.structure, cand.levels
    score = 0.0
    if st.breakout_vol_mult is not None:
        # 1.0x → 0.2, 1.5x → 0.6, 2.0x+ → 1.0
        score += _clamp((st.breakout_vol_mult - 0.8) / 1.2) * 0.7
    if lv.rvol is not None:
        score += _clamp((lv.rvol - 0.8) / 1.2) * 0.3
    else:
        score += 0.10                    # unknown RVOL is not credited as strong
    return _clamp(score)


def _retest(cand: Candidate) -> float:
    st = cand.structure
    if not st.breakout:
        return 0.0
    if st.retested and st.holding:
        return 0.70 if st.notes and any("dipped below" in n for n in st.notes) else 1.0
    return 0.30                          # holding without a retest: unproven


def score_candidate(cand: Candidate, mkt: MarketContext) -> Candidate:
    sector_score, sector_warn = _sector(cand)
    ema_score, ema_warn = _ema(cand)
    for warn in (sector_warn, ema_warn):
        if warn:
            cand.warnings.append(warn)

    parts = {
        "market": _market(mkt),
        "sector": sector_score,
        "trend": _trend(cand),
        "vwap": _vwap(cand),
        "ema": ema_score,
        "breakout": _breakout(cand),
        "volume": _volume(cand),
        "oi": oi_mod.score(cand.oi),
        "retest": _retest(cand),
    }
    total = sum(parts[k] * C.WEIGHTS[k] for k in C.WEIGHTS)

    if mkt.classification == "STRONGLY BEARISH":
        total *= C.STRONGLY_BEARISH_PENALTY
        cand.warnings.append("score cut for a strongly bearish market (§3)")

    cand.scores = {k: round(parts[k] * C.WEIGHTS[k], 1) for k in C.WEIGHTS}
    cand.score = round(total, 1)
    cand.grade = C.grade_for(cand.score)
    return cand
