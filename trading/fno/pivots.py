"""Pivot levels and the pre-market gap read.

Standard floor-trader arithmetic, computed from the previous session's high,
low and close. Nothing here is proprietary — these formulas are in every
intraday text — but they are the levels a large share of Indian intraday
traders watch, which is most of why they work: they are self-fulfilling
reference points, not predictions.

  Classic pivots   P = (H+L+C)/3, with R1..R3 and S1..S3 stepping out from it.

  CPR              the "central pivot range": P with a top (TC) and bottom
                   (BC) central line. Its WIDTH is the signal — a narrow CPR
                   says yesterday closed in balance and often precedes a
                   trending day; a wide CPR says the opposite. The width is
                   reported as a share of price so it compares across stocks.

  Camarilla        H3/L3 (reversal band) and H4/L4 (breakout band), the levels
                   intraday range traders use.

  Gap              today's open against yesterday's close, and whether the gap
                   has been filled — a gap that fills early often kills the
                   day's directional premise.

These are levels, not signals. Nothing here emits a trade; the scanner uses
them as context and as candidate stop/target references.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from trading.fno import indicators as ind

# A CPR narrower than this share of price is "narrow" — the conventional read
# is that a trending day is more likely.
NARROW_CPR_PCT = 0.25
WIDE_CPR_PCT = 0.75
# A move of this size from the previous close counts as a gap worth naming.
GAP_PCT = 0.30


@dataclass
class Pivots:
    """Yesterday's levels, projected onto today."""
    prev_high: float
    prev_low: float
    prev_close: float
    pivot: float
    bc: float
    tc: float
    r1: float
    r2: float
    r3: float
    s1: float
    s2: float
    s3: float
    h3: float
    h4: float
    l3: float
    l4: float

    @property
    def cpr_width(self) -> float:
        return abs(self.tc - self.bc)

    def cpr_width_pct(self) -> float:
        return self.cpr_width / self.pivot * 100 if self.pivot else 0.0

    @property
    def cpr_label(self) -> str:
        pct = self.cpr_width_pct()
        if pct <= NARROW_CPR_PCT:
            return "narrow — conventionally read as a trending day"
        if pct >= WIDE_CPR_PCT:
            return "wide — conventionally read as range-bound"
        return "average"

    def nearest_above(self, price: float) -> tuple[str, float] | None:
        levels = {"R1": self.r1, "R2": self.r2, "R3": self.r3, "TC": self.tc,
                  "H3": self.h3, "H4": self.h4, "PDH": self.prev_high,
                  "Pivot": self.pivot}
        above = sorted(((v, k) for k, v in levels.items() if v > price))
        return (above[0][1], above[0][0]) if above else None

    def nearest_below(self, price: float) -> tuple[str, float] | None:
        levels = {"S1": self.s1, "S2": self.s2, "S3": self.s3, "BC": self.bc,
                  "L3": self.l3, "L4": self.l4, "PDL": self.prev_low,
                  "Pivot": self.pivot}
        below = sorted(((v, k) for k, v in levels.items() if v < price), reverse=True)
        return (below[0][1], below[0][0]) if below else None


@dataclass
class GapRead:
    """Today's open against yesterday's close."""
    prev_close: float
    open_price: float
    gap_pct: float
    filled: bool
    day_low: float
    day_high: float

    @property
    def label(self) -> str:
        if abs(self.gap_pct) < GAP_PCT:
            return "flat open"
        side = "gap up" if self.gap_pct > 0 else "gap down"
        return f"{side} {abs(self.gap_pct):.2f}%" + (" — filled" if self.filled else "")


def compute(prev_high: float, prev_low: float, prev_close: float) -> Pivots:
    """Floor, CPR and Camarilla levels from one session's H/L/C."""
    rng = prev_high - prev_low
    p = (prev_high + prev_low + prev_close) / 3.0
    bc = (prev_high + prev_low) / 2.0
    tc = 2 * p - bc
    # TC is not always above BC; the pair is a range, so order them.
    if tc < bc:
        tc, bc = bc, tc
    return Pivots(
        prev_high=prev_high, prev_low=prev_low, prev_close=prev_close,
        pivot=p, bc=bc, tc=tc,
        r1=2 * p - prev_low, s1=2 * p - prev_high,
        r2=p + rng, s2=p - rng,
        r3=prev_high + 2 * (p - prev_low), s3=prev_low - 2 * (prev_high - p),
        h3=prev_close + rng * 1.1 / 4, l3=prev_close - rng * 1.1 / 4,
        h4=prev_close + rng * 1.1 / 2, l4=prev_close - rng * 1.1 / 2,
    )


def from_candles(df: pd.DataFrame, day) -> Pivots | None:
    """Pivots for `day`, computed from the session before it."""
    high, low, close = ind.prev_session_levels(df, day)
    if high is None or low is None or close is None:
        return None
    return compute(high, low, close)


def gap(df: pd.DataFrame, day) -> GapRead | None:
    """Today's gap against the previous close, and whether it has filled."""
    _, _, prev_close = ind.prev_session_levels(df, day)
    today = ind.session_of(df, day)
    if prev_close is None or today.empty:
        return None
    open_price = float(today["Open"].iloc[0])
    low, high = float(today["Low"].min()), float(today["High"].max())
    gap_pct = (open_price - prev_close) / prev_close * 100
    # A gap fills when price trades back through the previous close.
    filled = low <= prev_close <= high
    return GapRead(prev_close=prev_close, open_price=open_price, gap_pct=gap_pct,
                   filled=filled, day_low=low, day_high=high)


def render(piv: Pivots | None, g: GapRead | None, price: float | None = None) -> str:
    if piv is None:
        return "PIVOTS\n  no previous session in the data"
    L = ["PIVOTS & PRE-MARKET",
         f"  Prev H/L/C:        {piv.prev_high:,.2f} / {piv.prev_low:,.2f} / "
         f"{piv.prev_close:,.2f}"]
    if g is not None:
        L.append(f"  Open:              {g.open_price:,.2f} — {g.label}")
    L += [f"  Pivot:             {piv.pivot:,.2f}",
          f"  CPR:               {piv.bc:,.2f} – {piv.tc:,.2f}  "
          f"({piv.cpr_width_pct():.2f}% wide, {piv.cpr_label})",
          f"  Resistance:        R1 {piv.r1:,.2f} · R2 {piv.r2:,.2f} · R3 {piv.r3:,.2f}",
          f"  Support:           S1 {piv.s1:,.2f} · S2 {piv.s2:,.2f} · S3 {piv.s3:,.2f}",
          f"  Camarilla:         H4 {piv.h4:,.2f} · H3 {piv.h3:,.2f} | "
          f"L3 {piv.l3:,.2f} · L4 {piv.l4:,.2f}"]
    if price is not None:
        up, down = piv.nearest_above(price), piv.nearest_below(price)
        bits = []
        if down:
            bits.append(f"{down[0]} {down[1]:,.2f} below")
        if up:
            bits.append(f"{up[0]} {up[1]:,.2f} above")
        if bits:
            L.append(f"  Around {price:,.2f}:   " + "  ·  ".join(bits))
    L.append("  ! levels, not signals — they work largely because many traders "
             "watch the same ones")
    return "\n".join(L)
