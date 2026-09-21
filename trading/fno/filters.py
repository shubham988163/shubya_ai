"""Trade-quality rejection rules (§15).

These are hard gates applied *after* scoring: a candidate that trips any of
them is not emitted as a trade regardless of how well it scored. Scoring
ranks; filters veto. Keeping the two separate means a 91-point setup with
contradictory OI is visibly rejected rather than quietly ranked below others.

Each veto is tagged with a category so the UI can separate "hasn't set up yet"
(structure, timing, risk) from "the evidence argues against it" (OI, market,
sector, VWAP, volume, liquidity, data). Only the former can leave a stock on
the watch list.
"""
from __future__ import annotations

from trading.fno import config as C
from trading.fno.models import Candidate, MarketContext

# Sector median move (%) below which a long into that sector is fighting it.
WEAK_SECTOR_PCT = -0.75


def apply(cand: Candidate, mkt: MarketContext,
          trade_problems: list[tuple[str, str]]) -> Candidate:
    st, lv, fut = cand.structure, cand.levels, cand.fut
    found: list[tuple[str, str]] = list(trade_problems)

    def veto(kind: str, text: str):
        found.append((kind, text))

    if st.breakout and st.breakout_vol_mult is not None \
            and st.breakout_vol_mult < C.WEAK_VOL_MULT:
        veto("volume", f"breakout came on {st.breakout_vol_mult:.1f}x volume — no "
                       "expansion behind it")

    if st.target2_hit:
        veto("timing", "Target 2 already achieved today — target move completed; do not enter now")
    elif st.target1_hit:
        veto("timing", "Target 1 already achieved today — target move completed; do not enter now")

    if st.extended:
        veto("timing", "price is already extended from the level/VWAP — this is a "
                       "chase; wait for a pullback into the entry zone")

    if st.failed_breakout:
        veto("structure", "failed breakout — price closed back below the level")

    if not cand.oi.bullish and cand.direction == "BUY":
        veto("oi", f"futures OI says {cand.oi.classification} — {cand.oi.detail}")
    if cand.oi.bullish and cand.direction == "SELL":
        veto("oi", f"futures OI says {cand.oi.classification} — {cand.oi.detail}")

    if mkt.classification == "STRONGLY BEARISH" and cand.direction == "BUY":
        veto("market", "broader market is strongly bearish — no long")
    if mkt.classification == "STRONGLY BULLISH" and cand.direction == "SELL":
        veto("market", "broader market is strongly bullish — no short")

    if cand.sector_strength is not None:
        if cand.direction == "BUY" and cand.sector_strength < WEAK_SECTOR_PCT:
            veto("sector", f"sector is weak ({cand.sector_strength:+.2f}%) — the long fights its sector")
        elif cand.direction == "SELL" and cand.sector_strength > -WEAK_SECTOR_PCT:
            veto("sector", f"sector is strong ({cand.sector_strength:+.2f}%) — the short fights its sector")

    if cand.direction == "BUY" and lv.price < lv.vwap:
        veto("vwap", "price is below VWAP")
    if cand.direction == "SELL" and lv.price > lv.vwap:
        veto("vwap", "price is above VWAP")

    if lv.rvol is not None and lv.rvol < C.MIN_RVOL:
        veto("volume", f"relative volume {lv.rvol:.2f}x — participation is below "
                       "normal for this time of day")

    spread = fut.spread_bps
    if spread is not None and spread > C.MAX_SPREAD_BPS:
        veto("liquidity", f"futures spread {spread:.1f} bps — too wide to trade "
                          "cleanly")

    if fut.turnover_cr is not None and fut.turnover_cr < C.MIN_FUT_VALUE_CR:
        veto("liquidity", f"futures turnover ₹{fut.turnover_cr:.0f} cr — illiquid "
                          "contract")

    if fut.pct_change is not None and abs(fut.pct_change) > C.MAX_ABS_MOVE_PCT:
        veto("data", f"{fut.pct_change:+.1f}% move is abnormal — verify for news "
                     "before trading it")

    if cand.oi.classification == "UNAVAILABLE":
        veto("data", "OI data unavailable — the setup cannot be confirmed (§18)")

    if cand.score < C.MIN_TRADEABLE_SCORE:
        veto("score", f"score {cand.score:.0f} is below the "
                      f"{C.MIN_TRADEABLE_SCORE} minimum")

    # Deduplicate on the text while keeping the original order.
    seen = set()
    for kind, text in found:
        if text in seen:
            continue
        seen.add(text)
        cand.rejections.append(text)
        cand.blockers.append(kind)
    return cand
