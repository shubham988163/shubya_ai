"""Option-chain analytics — what the whole chain says, not one strike.

Standard, publicly documented measures used by Indian index traders. Nothing
proprietary and nothing invented: each is a defined calculation over the chain
we already fetch, and each is reported with the caveat that limits it.

  PCR (put/call ratio)   total put OI / total call OI. Above ~1.3 is read as
                         put-heavy (writers expecting support); below ~0.7 as
                         call-heavy. It is a *positioning* measure and a poor
                         timing tool — extremes mean-revert but give no date.

  Max pain              the strike where option BUYERS lose the most in
                         aggregate at expiry, i.e. where writers pay least.
                         Useful near expiry as a magnet; near-meaningless
                         weeks out, so the distance in days is always shown.

  OI walls              the strikes carrying the most open interest. Heavy
                         call OI above spot acts as resistance, heavy put OI
                         below as support — because writers defend them. They
                         move: a wall that breaks becomes support/resistance
                         in reverse.

The honest limit on all three: **open interest is not directional.** A large
call OI can be a writer expecting the level to hold or a buyer expecting a
break; the chain cannot tell you which. They are context, not signals, and
this module never emits a trade from them alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from trading.fno.options import OptionQuote, parse_expiry

# Bands used to describe PCR in words. Widely quoted levels; they are
# conventions, not laws.
PCR_PUT_HEAVY = 1.30
PCR_CALL_HEAVY = 0.70


@dataclass
class ChainRead:
    """What the chain says about positioning."""
    underlying: str
    expiry: str | None
    spot: float | None
    days_to_expiry: int | None
    call_oi: float
    put_oi: float
    pcr: float | None
    max_pain: float | None
    resistance_strike: float | None      # heaviest call OI above spot
    resistance_oi: float | None
    support_strike: float | None         # heaviest put OI below spot
    support_oi: float | None
    strikes: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def pcr_label(self) -> str:
        if self.pcr is None:
            return "unavailable"
        if self.pcr >= PCR_PUT_HEAVY:
            return "put-heavy (writers defending downside)"
        if self.pcr <= PCR_CALL_HEAVY:
            return "call-heavy (writers capping upside)"
        return "balanced"

    @property
    def max_pain_useful(self) -> bool:
        """Max pain only means much as expiry approaches."""
        return self.days_to_expiry is not None and self.days_to_expiry <= 5


def front_expiry(board: list[OptionQuote], underlying: str) -> str | None:
    rows = [q for q in board if q.underlying == underlying and parse_expiry(q.expiry)]
    if not rows:
        return None
    return min(rows, key=lambda q: parse_expiry(q.expiry)).expiry


def max_pain(calls: dict[float, float], puts: dict[float, float]) -> float | None:
    """The strike minimising total intrinsic value paid out at expiry.

    For each candidate settlement S, writers pay call holders (S-K) on every
    in-the-money call and put holders (K-S) on every in-the-money put. The
    strike where that total is smallest is "max pain".
    """
    strikes = sorted(set(calls) | set(puts))
    if len(strikes) < 3:
        return None
    best, best_pain = None, None
    for settle in strikes:
        pain = 0.0
        for k, oi in calls.items():
            if settle > k:
                pain += (settle - k) * oi
        for k, oi in puts.items():
            if settle < k:
                pain += (k - settle) * oi
        if best_pain is None or pain < best_pain:
            best, best_pain = settle, pain
    return best


def analyse(board: list[OptionQuote], underlying: str = "NIFTY",
            today: date | None = None) -> ChainRead | None:
    """Read the front-expiry chain for one underlying."""
    expiry = front_expiry(board, underlying)
    rows = [q for q in board if q.underlying == underlying and q.expiry == expiry]
    if not rows:
        return None

    spot = next((q.underlying_value for q in rows if q.underlying_value), None)
    calls = {q.strike: (q.open_interest or 0.0) for q in rows if q.option_type == "Call"}
    puts = {q.strike: (q.open_interest or 0.0) for q in rows if q.option_type == "Put"}
    call_oi, put_oi = sum(calls.values()), sum(puts.values())

    exp_date = parse_expiry(expiry)
    dte = (exp_date - (today or date.today())).days if exp_date else None

    read = ChainRead(
        underlying=underlying, expiry=expiry, spot=spot, days_to_expiry=dte,
        call_oi=call_oi, put_oi=put_oi,
        pcr=(put_oi / call_oi) if call_oi > 0 else None,
        max_pain=max_pain(calls, puts),
        resistance_strike=None, resistance_oi=None,
        support_strike=None, support_oi=None,
        strikes=len(set(calls) | set(puts)),
    )

    if spot:
        above = {k: v for k, v in calls.items() if k > spot and v > 0}
        below = {k: v for k, v in puts.items() if k < spot and v > 0}
        if above:
            read.resistance_strike = max(above, key=above.get)
            read.resistance_oi = above[read.resistance_strike]
        if below:
            read.support_strike = max(below, key=below.get)
            read.support_oi = below[read.support_strike]
    else:
        read.notes.append("no spot published with the chain — walls not located")

    if read.max_pain is not None and not read.max_pain_useful:
        read.notes.append(
            f"max pain is {read.days_to_expiry} days out — it behaves as an "
            "expiry-week magnet, not an intraday level")
    read.notes.append("open interest is not directional: a heavy strike may be "
                      "writers defending it or buyers expecting a break")
    return read


def render(read: ChainRead | None) -> str:
    if read is None:
        return "OPTION CHAIN\n  no chain available"
    L = [f"OPTION CHAIN — {read.underlying} {read.expiry or ''}"
         + (f" ({read.days_to_expiry}d to expiry)" if read.days_to_expiry is not None else ""),
         f"  PCR:               {read.pcr:.2f} — {read.pcr_label}"
         if read.pcr is not None else "  PCR:               unavailable",
         f"  Call OI / Put OI:  {read.call_oi:,.0f} / {read.put_oi:,.0f} "
         f"across {read.strikes} strikes"]
    if read.max_pain is not None:
        L.append(f"  Max pain:          {read.max_pain:g}"
                 + (f"  (spot {read.spot:,.2f})" if read.spot else "")
                 + ("" if read.max_pain_useful else "  — too far out to matter"))
    if read.resistance_strike:
        L.append(f"  OI resistance:     {read.resistance_strike:g} CE "
                 f"({read.resistance_oi:,.0f} OI)")
    if read.support_strike:
        L.append(f"  OI support:        {read.support_strike:g} PE "
                 f"({read.support_oi:,.0f} OI)")
    L += [f"  ! {n}" for n in read.notes]
    return "\n".join(L)
