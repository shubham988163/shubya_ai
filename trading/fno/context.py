"""Market and sector context (§3, §10).

The market read is made *before* any stock is graded, and it gates everything
after it: a long setup in a strongly bearish tape is penalised in scoring and
vetoed in filtering, no matter how clean the chart looks.

Sector strength prefers the live NSE sector index for the stock's sector and
falls back to a peer-median proxy (the median intraday move of that sector's
F&O names) when no index maps cleanly. Which one was used is stated in the
report rather than left for the reader to assume.
"""
from __future__ import annotations

import statistics
from datetime import datetime

from trading.fno import config as C
from trading.fno import indicators as ind
from trading.fno.models import Candles, MarketContext


def index_read(candles: Candles | None, day, now: datetime
               ) -> tuple[float | None, bool | None]:
    """(% change from the previous session's close, is price above VWAP)."""
    if candles is None or len(candles) == 0:
        return None, None
    df = ind.closed_bars(candles.df, now)
    if df.empty:
        return None, None
    today = ind.session_of(df, day)
    if today.empty:
        return None, None
    price = float(today["Close"].iloc[-1])
    # Index feeds carry no volume, so an index VWAP is simply not available.
    above = (bool(price > float(ind.vwap(today).iloc[-1]))
             if ind.has_volume(today) else None)
    _, _, prev_close = ind.prev_session_levels(df, day)
    if not prev_close:
        return None, above
    return (price - prev_close) / prev_close * 100, above


def classify(nifty_pct: float | None, banknifty_pct: float | None,
             breadth_pct: float | None, nifty_above_vwap: bool | None) -> str:
    """Five-way market classification, NIFTY-led with breadth confirmation."""
    if nifty_pct is None:
        return "NEUTRAL"
    label = "STRONGLY BEARISH"
    for cutoff, name in C.MARKET_BANDS:
        if nifty_pct >= cutoff:
            label = name
            break

    order = ["STRONGLY BEARISH", "BEARISH", "NEUTRAL", "BULLISH", "STRONGLY BULLISH"]
    i = order.index(label)
    # Breadth and Bank Nifty can nudge the read one notch, never two.
    if breadth_pct is not None:
        if breadth_pct >= 70 and banknifty_pct is not None and banknifty_pct > 0:
            i = min(i + 1, 4)
        elif breadth_pct <= 30:
            i = max(i - 1, 0)
    if nifty_above_vwap is False and i > 0 and label in ("BULLISH", "STRONGLY BULLISH"):
        i -= 1        # green but under VWAP is a weaker tape than the % suggests
    return order[i]


def sector_medians(universe: list[dict]) -> dict[str, float]:
    buckets: dict[str, list[float]] = {}
    for row in universe:
        pct, sec = row.get("pct"), C.SECTORS.get(row["symbol"])
        if pct is None or sec is None:
            continue
        buckets.setdefault(sec, []).append(pct)
    return {sec: statistics.median(v) for sec, v in buckets.items() if len(v) >= 2}


def breadth(universe: list[dict]) -> tuple[float | None, int | None, int | None]:
    moves = [r["pct"] for r in universe if r.get("pct") is not None]
    if len(moves) < 10:
        return None, None, None
    adv = sum(1 for m in moves if m > 0)
    dec = sum(1 for m in moves if m < 0)
    return adv / len(moves) * 100, adv, dec


def build(feed, universe: list[dict], day, now: datetime) -> MarketContext:
    """Index snapshot first, delayed candles second, peer proxies last.

    Each source is weaker than the one before it, so which one supplied the
    read is recorded in the notes rather than being flattened into one number.
    """
    notes: list[str] = []
    sources = []
    nifty_pct = bank_pct = breadth_pct = None
    adv = dec = None
    sector_pct: dict[str, float] = {}
    sector_source = breadth_source = "unavailable"

    snap = feed.market_snapshot() if hasattr(feed, "market_snapshot") else None
    if snap:
        sources.append(snap["prov"])
        nifty_pct = snap["pct"].get("NIFTY 50")
        bank_pct = snap["pct"].get("NIFTY BANK")
        adv, dec = snap["advances"], snap["declines"]
        if adv and dec and (adv + dec) > 0:
            breadth_pct = adv / (adv + dec) * 100
        sector_pct = {code: snap["pct"][name]
                      for code, name in C.SECTOR_INDEX.items()
                      if snap["pct"].get(name) is not None}
        if sector_pct:
            sector_source = "live NSE sector indices"
        if breadth_pct is not None:
            breadth_source = "NIFTY 50 advances/declines"

    # VWAP for the index needs candles; the snapshot cannot supply it.
    nifty = feed.index_candles(C.NIFTY_TICKER)
    if nifty is not None:
        sources.append(nifty.prov)
    cand_pct, nifty_vwap = index_read(nifty, day, now)
    if nifty_pct is None:
        nifty_pct = cand_pct
        if nifty_pct is not None:
            notes.append("NIFTY move taken from delayed index candles — the "
                         "live index snapshot was unavailable")
    if bank_pct is None:
        bank = feed.index_candles(C.BANKNIFTY_TICKER)
        if bank is not None:
            sources.append(bank.prov)
        bank_pct, _ = index_read(bank, day, now)

    if not sector_pct:
        sector_pct = sector_medians(universe)
        if sector_pct:
            sector_source = "peer-median proxy (live sector indices unavailable)"

    # A gainers/losers universe is ~50% green by construction, so breadth
    # derived from it would be meaningless; only a cross-section can be used.
    if breadth_pct is None and not getattr(feed, "universe_is_biased", False):
        breadth_pct, adv, dec = breadth(universe)
        if breadth_pct is not None:
            breadth_source = "scanned universe"

    if nifty_pct is None:
        notes.append("NIFTY data unavailable — market context is unverified and "
                     "every setup is scored against a NEUTRAL market")
    if breadth_pct is None:
        notes.append("market breadth unavailable")

    label = classify(nifty_pct, bank_pct, breadth_pct, nifty_vwap)
    return MarketContext(
        classification=label, nifty_pct=nifty_pct, banknifty_pct=bank_pct,
        nifty_above_vwap=nifty_vwap, breadth_pct=breadth_pct,
        advances=adv, declines=dec, notes=notes, sources=sources,
        sector_pct=sector_pct, sector_source=sector_source,
        breadth_source=breadth_source,
    )
