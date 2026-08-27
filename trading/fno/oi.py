"""Futures open-interest interpretation (§6).

    price ↑ + OI ↑  = LONG BUILDUP    — fresh longs, bullish confirmation
    price ↓ + OI ↑  = SHORT BUILDUP   — fresh shorts, bearish
    price ↑ + OI ↓  = SHORT COVERING  — bullish, but it is exits, not conviction
    price ↓ + OI ↓  = LONG UNWINDING  — longs giving up, weak

Moves smaller than OI_FLAT_PCT are treated as no signal rather than being
forced into one of the four boxes.
"""
from __future__ import annotations

from trading.fno import config as C
from trading.fno.models import OIRead


def classify(price_pct: float | None, oi_pct: float | None) -> OIRead:
    if price_pct is None or oi_pct is None:
        return OIRead("UNAVAILABLE", False, False,
                      "futures OI not available — cannot confirm the move",
                      oi_pct, price_pct)

    flat_oi = abs(oi_pct) < C.OI_FLAT_PCT
    flat_px = abs(price_pct) < 0.10

    if flat_oi:
        return OIRead("OI FLAT", False, True,
                      f"OI barely moved ({oi_pct:+.2f}%) — no positional "
                      f"conviction behind the {price_pct:+.2f}% move",
                      oi_pct, price_pct)
    if flat_px:
        return OIRead("OI CHANGE WITHOUT PRICE", False, True,
                      f"OI {oi_pct:+.2f}% with price flat ({price_pct:+.2f}%) "
                      "— direction unresolved",
                      oi_pct, price_pct)

    up, oi_up = price_pct > 0, oi_pct > 0
    if up and oi_up:
        strong = oi_pct >= C.OI_STRONG_PCT
        return OIRead("LONG BUILDUP", True, True,
                      f"price {price_pct:+.2f}% with OI {oi_pct:+.2f}% — "
                      f"{'strong ' if strong else ''}fresh long positions",
                      oi_pct, price_pct)
    if not up and oi_up:
        return OIRead("SHORT BUILDUP", False, True,
                      f"price {price_pct:+.2f}% with OI {oi_pct:+.2f}% — fresh "
                      "shorts; contradicts a long",
                      oi_pct, price_pct)
    if up and not oi_up:
        return OIRead("SHORT COVERING", True, True,
                      f"price {price_pct:+.2f}% with OI {oi_pct:+.2f}% — shorts "
                      "exiting; bullish but less durable than fresh longs",
                      oi_pct, price_pct)
    return OIRead("LONG UNWINDING", False, True,
                  f"price {price_pct:+.2f}% with OI {oi_pct:+.2f}% — longs "
                  "exiting; weak",
                  oi_pct, price_pct)


def score(read: OIRead) -> float:
    """0–1 contribution to the setup score (§11, weight 15)."""
    return {
        "LONG BUILDUP": 1.00 if (read.oi_change_pct or 0) >= C.OI_STRONG_PCT else 0.85,
        "SHORT COVERING": 0.55,
        "OI FLAT": 0.30,
        "OI CHANGE WITHOUT PRICE": 0.25,
        "UNAVAILABLE": 0.0,
        "LONG UNWINDING": 0.0,
        "SHORT BUILDUP": 0.0,
    }.get(read.classification, 0.0)
