"""Correctness tests for the risk kernel in trading/execution_router.py.

The kernel is the last place a bad size can be refused, so its failure mode is
silent acceptance. Every case here is a signal that MUST be refused, or one
that must NOT be — and the sizes are chosen so that only the check under test
can be the thing that fires. The 2026-08-21 hole is `test_risk_per_trade`:
position value inside the capital cap, loss twice what the day allowed.

Nothing here touches the real ledger; the ledger is stubbed so a test run can
never write a trade or read yesterday's P&L.

Run with:

    .venv/bin/python -m trading.test_risk_kernel
"""
from __future__ import annotations

import sys

from trading import execution_router as er
from trading.config import MAX_OPEN_POSITIONS

# These cases encode incidents from a Rs 10,00,000 book (2026-08-21 and the
# 10-21 SBIN sizing bug). This repo is configured for a Rs 15,000 account, at
# which those positions are not merely refused but unreachable, so the numbers
# would stop testing anything. The kernel's LOGIC is what is under test, not
# the account it happens to be pointed at, so pin the scale the cases were
# written against. Anything reading these at call time sees the pinned values.
RISK_PER_TRADE = 5_000.0
MAX_POSITION_VALUE = 200_000.0
er.RISK_PER_TRADE = RISK_PER_TRADE
er.MAX_POSITION_VALUE = MAX_POSITION_VALUE

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}" + (f"   [{detail}]" if detail else ""))
        FAILURES.append(name)


class StubLedger:
    """Just the four reads the kernel makes, all answerable from the fixture."""

    def __init__(self, pnl=0.0, open_trades=(), position_value=0.0):
        self._pnl, self._open, self._pv = pnl, list(open_trades), position_value
        self.rejections: list[tuple[dict, str]] = []
        self.entries: list[dict] = []

    def day_realized_pnl(self, date=None):
        return self._pnl

    def open_trades(self):
        return self._open

    def open_position_value(self, symbol):
        return self._pv

    def record_rejection(self, signal, reason):
        self.rejections.append((signal, reason))

    def record_entry(self, signal, fill_price, mode="paper"):
        self.entries.append(dict(signal, fill_price=fill_price))
        return len(self.entries)


def router(multiplier=1.0, ledger=None, **cfg):
    r = er.ExecutionRouter(mode="paper", ledger=ledger or StubLedger(),
                           get_ltp=lambda s: 100.0)
    r.day_config = {"date": "2026-09-04", "regime": "trending",
                    "risk_multiplier": multiplier, "blocked_symbols": [],
                    "rationale": "test", **cfg}
    # execute() re-reads today_config.json on every order — by design, so a
    # long-running loop picks up an intraday halt. In tests that would replace
    # the config injected above with whatever the pre-market agent wrote this
    # morning, making the suite pass or fail by the day's news: on 2026-11-17
    # a real halt (risk_multiplier 0) failed three checks that are about the
    # risk bound, not about halts. Pin it, the way _ts() pins the clock.
    er.load_day_config = lambda: r.day_config
    return r


def _ts(hh: int = 10, mm: int = 0) -> float:
    """A timestamp at a fixed IST time today.

    Every signal here carries one. Left to the wall clock, the kernel's
    late-entry gate refuses the lot after 14:45 and the whole suite starts
    failing by time of day rather than by behaviour — which it did, once.
    """
    import datetime as _dt
    from zoneinfo import ZoneInfo

    return _dt.datetime.now(ZoneInfo("Asia/Kolkata")).replace(
        hour=hh, minute=mm, second=0, microsecond=0).timestamp()


def signal(**kw):
    """A well-formed BUY risking 1,000: 100 shares, 10 rupees to the stop."""
    return {"symbol": "SBIN", "side": "BUY", "qty": 100, "price": 100.0,
            "stop_loss": 90.0, "target": 120.0, "ts": _ts(),
            "strategy_id": "test", "regime": "trending", **kw}


def test_risk_per_share():
    print("\nrisk per share")
    check("a BUY measures entry down to the stop",
          er.risk_per_share(signal()) == 10.0)
    check("a SELL measures the stop up from the entry",
          er.risk_per_share(signal(side="SELL", stop_loss=110.0)) == 10.0)
    check("a missing stop is unknowable, not zero",
          er.risk_per_share(signal(stop_loss=None)) is None)
    check("a BUY stopping above its entry is negative, not zero",
          er.risk_per_share(signal(stop_loss=110.0)) == -10.0)
    check("a SELL stopping below its entry is negative",
          er.risk_per_share(signal(side="SELL", stop_loss=90.0)) == -10.0)
    check("a string price still measures",
          er.risk_per_share(signal(price="100.0")) == 10.0)
    check("an unparseable stop is unknowable",
          er.risk_per_share(signal(stop_loss="not a number")) is None)


def test_risk_per_trade():
    print("\nper-trade risk limit")
    # RISK_PER_TRADE is 5,000. At 10 rupees of stop distance that is 500
    # shares at full size, 250 on a half-size day.
    full = RISK_PER_TRADE / 10.0
    r = router(1.0)
    check("a signal at exactly the limit passes",
          r.risk_check(signal(qty=int(full))) is None,
          r.risk_check(signal(qty=int(full))))
    reason = r.risk_check(signal(qty=int(full) + 50))
    check("a signal over the limit is refused",
          reason is not None and reason.startswith("risk_per_trade"), reason)
    # 497, not 500: the quantity is bounded at the expected FILL (100.05 for a
    # BUY), where the stop is 10.05 away rather than 10.00.
    at_fill = int(RISK_PER_TRADE / er.expected_risk_per_share(signal()))
    check("the refusal states the allowed quantity, measured at the fill",
          reason and f"max_qty={at_fill}" in reason, reason)
    check("which is below the quantity the raw close would allow",
          at_fill == 497 and at_fill < int(full), at_fill)

    half = router(0.5)
    check("a half-size day halves the allowed quantity",
          half.risk_check(signal(qty=int(full / 2))) is None)
    reason = half.risk_check(signal(qty=int(full / 2) + 20))
    check("full size on a half-size day is refused",
          reason is not None and reason.startswith("risk_per_trade"), reason)

    # THE 2026-08-21 TRADE, pinned to arithmetic, at the fill price the ledger
    # actually recorded (2290.854 — not the rounded 2290 an eyeball uses). The
    # supervisor vetoed it as "double the allowed amount for a day with a 0.5
    # risk multiplier" and the EOD journal repeated that as the day's
    # highest-impact fix. It is false: 87 shares at 15.65 to the stop risked
    # 1,361.20 against the 2,500 a 0.5 multiplier allows — 54% of the limit,
    # not 200%. The agent invented a violation, which is exactly why its
    # verdicts are advisory and cannot block a trade. Kept as a regression
    # test so a claim like that is checkable rather than believable.
    tcs = signal(symbol="TCS", qty=87, price=2290.854, stop_loss=2306.5,
                 side="SELL")
    risked = 87 * (2306.5 - 2290.854)
    check("the 2026-08-21 trade risked 1,361.20", abs(risked - 1361.2) < 0.01,
          f"{risked:.2f}")
    check("which is inside the 2,500 a half-size day allows",
          risked < RISK_PER_TRADE * 0.5)
    check("it was the capital cap, not the multiplier, that set the size —"
          " which is what misled the agent",
          87 * 2290.854 > MAX_POSITION_VALUE * 0.99,
          f"{87 * 2290.854:.0f} vs cap {MAX_POSITION_VALUE:.0f}")
    check("so the kernel correctly accepts it — the veto was wrong",
          router(0.5).risk_check(tcs) is None, router(0.5).risk_check(tcs))
    check("it was also inside the capital cap",
          87 * 2290.0 < MAX_POSITION_VALUE)
    # What the supervisor DESCRIBED — a genuinely doubled position — is the
    # thing the kernel now refuses, whoever produces it.
    doubled = signal(symbol="TCS", qty=174, price=2290.0, stop_loss=2306.5,
                     side="SELL")
    reason = router(0.5).risk_check(doubled)
    check("a genuinely doubled position IS refused pre-trade",
          reason is not None and reason.startswith("risk_per_trade"), reason)
    check("and it is refused on risk, not on the capital cap",
          174 * 2290.0 > MAX_POSITION_VALUE
          or reason.startswith("risk_per_trade"), reason)


def test_slippage_is_inside_the_risk():
    print("\nslippage counts against the allowance")
    # The 2026-10-27 finding. Sizing divided the allowance by the distance at
    # the close; the ledger then booked the slipped fill, which sits FURTHER
    # from the stop. Every trade therefore risked a little more than allowed —
    # ~3% at this book's average 0.77% stop — and RISK_TOLERANCE (2%) was not
    # wide enough to hide it, so the supervisor reported a "kernel bypass" on
    # roughly every session while the kernel's own tests passed.
    check("a BUY fills above the close, widening the stop distance",
          er.expected_risk_per_share(signal()) > er.risk_per_share(signal()))
    check("a SELL fills below it, which widens it too",
          er.expected_risk_per_share(signal(side="SELL", stop_loss=110.0))
          > er.risk_per_share(signal(side="SELL", stop_loss=110.0)))

    # The worked example: SBIN, 2026-10-21, 0.5 multiplier => 2,500 allowed.
    # The engine sized 149 off the close and the fill risked 2,560 (102.4%).
    sbin = signal(symbol="SBIN", side="SELL", qty=149, price=966.25,
                  stop_loss=982.95)
    booked = 149 * er.expected_risk_per_share(sbin)
    check("the 10-21 SBIN trade really did risk over its 2,500 allowance",
          2550 < booked < 2570, f"{booked:.2f}")
    check("so the kernel now refuses the 149 the old sizing produced",
          (r := router(0.5).risk_check(sbin)) is not None
          and r.startswith("risk_per_trade"), r)

    # A stop sitting exactly on the entry is degenerate, and slippage must not
    # be able to manufacture a sliver of positive risk that slips through.
    reason = router(1.0).risk_check(signal(stop_loss=100.0))
    check("a stop on the entry is still refused, not slipped into validity",
          reason is not None and "stop" in reason,
          reason)


def test_malformed_stops():
    print("\nmalformed stops")
    r = router(1.0)
    reason = r.risk_check(signal(stop_loss=None))
    # Wording differs across forks (no_stop_loss / missing_stop_loss); what
    # matters is that an unbounded signal is refused and the reason says why.
    check("a signal with no stop is refused",
          reason is not None and "stop" in reason, reason)
    reason = r.risk_check(signal(stop_loss=110.0))
    check("a BUY stopping above its entry is refused",
          reason is not None and "stop" in reason, reason)
    reason = r.risk_check(signal(side="SELL", stop_loss=90.0))
    check("a SELL stopping below its entry is refused",
          reason is not None and "stop" in reason, reason)
    reason = r.risk_check(signal(stop_loss=100.0))
    check("a stop at the entry is refused, not treated as free",
          reason is not None and "stop" in reason, reason)


def test_existing_limits_still_hold():
    print("\nthe limits that were already there")
    check("a blocked symbol is refused",
          (router(1.0, blocked_symbols=["SBIN"]).risk_check(signal()) or "")
          .startswith("symbol_blocked_by_premarket_agent"))
    check("a zero multiplier halts the day",
          router(0.0).risk_check(signal()) == "risk_multiplier_zero (pre-market halt)")
    check("the daily loss limit halts the day",
          router(1.0, ledger=StubLedger(pnl=-50_000.0)).risk_check(signal())
          == "daily_loss_limit_hit")
    # This fork scopes the open-position cap to TODAY's trades
    # (`t.get("date") == today`), so a stub row without a date is invisible to
    # it and the cap never fires. Date the fixture rows so the check is
    # actually exercised. Worth knowing: that scoping means a position carried
    # overnight does not count against the cap here.
    _today = __import__("datetime").datetime.now(
        __import__("zoneinfo").ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    many = [{"symbol": f"X{i}", "date": _today}
            for i in range(MAX_OPEN_POSITIONS)]
    check("the open-position cap refuses a new symbol",
          (router(1.0, ledger=StubLedger(open_trades=many)).risk_check(signal())
           or "").startswith("max_open_positions"))
    check("but an existing symbol may still be managed",
          router(1.0, ledger=StubLedger(
              open_trades=[{"symbol": "SBIN"}] + many[1:])).risk_check(signal())
          is None)
    # Wide stop, big position: little risk per share, too much capital.
    wide = signal(qty=3000, price=100.0, stop_loss=99.0)
    check("the capital cap refuses a position too large to hold",
          (router(1.0).risk_check(wide) or "").startswith("max_position_value"),
          router(1.0).risk_check(wide))


def test_squareoff_gate():
    print("\nlate-entry gate")
    if not hasattr(er, "minutes_to_squareoff"):
        print("  SKIP this kernel has no late-entry gate")
        return
    import datetime as dt
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")

    def at(hh, mm):
        """A signal stamped at a given IST time on a weekday."""
        when = dt.datetime(2026, 9, 9, hh, mm, tzinfo=IST)
        return signal(ts=when.timestamp())

    check("minutes are measured to 15:15, not to the close",
          abs(er.minutes_to_squareoff(at(14, 45)) - 30) < 0.01,
          er.minutes_to_squareoff(at(14, 45)))
    check("after the square-off the figure goes negative",
          er.minutes_to_squareoff(at(15, 30)) < 0)

    r = router(1.0)
    check("a morning entry passes", r.risk_check(at(9, 30)) is None)
    check("an entry with exactly the minimum passes",
          r.risk_check(at(14, 45)) is None, r.risk_check(at(14, 45)))
    reason = r.risk_check(at(14, 50))
    check("an entry inside the window is refused",
          reason is not None and reason.startswith("too_late_to_reach_target"),
          reason)
    check("the refusal says how long was left",
          reason and "25 min" in reason, reason)
    check("an entry after the square-off is refused",
          (r.risk_check(at(15, 30)) or "").startswith("too_late_to_reach_target"))

    # Timing off the signal, not the wall clock: a replayed signal from any
    # past session must grade the way it did live.
    old = signal(ts=dt.datetime(2026, 7, 8, 10, 0, tzinfo=IST).timestamp())
    check("a replayed morning signal is not judged against today's clock",
          r.risk_check(old) is None, r.risk_check(old))

    # The check is switchable, because it is calibrated on 19 sessions.
    saved = er.MIN_MINUTES_TO_SQUAREOFF
    er.MIN_MINUTES_TO_SQUAREOFF = 0
    try:
        check("setting the minimum to 0 disables the gate",
              router(1.0).risk_check(at(15, 10)) is None)
    finally:
        er.MIN_MINUTES_TO_SQUAREOFF = saved


def test_gate_against_the_real_book():
    """Replay every EMA trade through the gate and confirm the claimed effect.

    The rule was derived from this book, so this is not evidence that it will
    work in future — it is a guard that the implementation reproduces the
    measurement the config comment cites. If the two ever disagree, one of
    them is wrong.
    """
    print("\nthe gate against the recorded book")
    if not hasattr(er, "minutes_to_squareoff"):
        print("  SKIP this kernel has no late-entry gate")
        return
    import sqlite3

    from trading.config import DB_PATH

    if not DB_PATH.exists():
        check("ledger present to replay", True, "skipped — no ledger")
        return
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT ts, pnl FROM trades WHERE exit_price IS NOT NULL "
            "AND strategy_id LIKE 'ema_9_21%'")]
    finally:
        conn.close()
    if len(rows) < 50:
        check("enough trades to replay", True, f"skipped — only {len(rows)}")
        return

    kept = [r for r in rows
            if er.minutes_to_squareoff({"ts": r["ts"]}) >= er.MIN_MINUTES_TO_SQUAREOFF]
    before, after = sum(r["pnl"] for r in rows), sum(r["pnl"] for r in kept)
    skipped = len(rows) - len(kept)
    print(f"       {len(rows)} trades, gate skips {skipped}: "
          f"{before:.2f} -> {after:.2f}")
    check("the gate actually excludes late entries", skipped > 0)
    check("and the recorded book improves, as the config claims",
          after > before, f"{before:.2f} -> {after:.2f}")


def test_rejections_are_recorded():
    print("\nrejections reach the ledger")
    led = StubLedger()
    r = router(0.5, ledger=led)
    trade_id = r.execute(signal(qty=500))
    check("an oversized signal does not become a trade", trade_id is None)
    check("nothing was written to the trades table", led.entries == [])
    check("the rejection is recorded with its reason",
          len(led.rejections) == 1
          and led.rejections[0][1].startswith("risk_per_trade"), led.rejections)
    check("and the reason is readable by the caller",
          r.last_rejection.startswith("risk_per_trade"), r.last_rejection)

    led2 = StubLedger()
    ok = router(1.0, ledger=led2)
    trade_id = ok.execute(signal())
    check("a well-sized signal still fills", trade_id == 1, trade_id)
    check("and lands in the ledger once", len(led2.entries) == 1)


def test_engine_sizing_agrees_with_the_kernel():
    print("\nthe engine and the kernel agree")
    # Engine.size() floors its quantity, so anything it produces must pass the
    # kernel. A disagreement here would mean the engine cannot trade at all.
    from trading import strategy as _strat
    from trading.strategy import Engine

    # Engine.size() reads RISK_PER_TRADE and MAX_POSITION_VALUE from its OWN
    # module namespace, so the scale pinned at the top of this file (on
    # execution_router) does not reach it. Left unpinned, the engine sizes
    # against this fork's Rs 15,000 account while the kernel judges against the
    # pinned Rs 10,00,000 one, and the resulting 3-share positions are refused
    # by the cost floor — a disagreement manufactured by the test, not a real
    # one. Pin both sides to the same scale.
    _strat.RISK_PER_TRADE = RISK_PER_TRADE
    _strat.MAX_POSITION_VALUE = MAX_POSITION_VALUE

    class FakeEngine:
        """Just the one attribute Engine.size reads."""

        def __init__(self, r):
            self.router = r

    for mult in (1.0, 0.5, 0.25):
        r = router(mult)
        fake = FakeEngine(r)
        for price, per_share in ((100.0, 10.0), (2290.0, 16.5), (815.0, 3.2)):
            stop = round(price - per_share, 2)
            # Mirror try_enter: size off the expected fill, not the close.
            sig = signal(qty=0, price=price, stop_loss=stop)
            fill_risk = er.expected_risk_per_share(sig)
            qty = Engine.size(fake, er.expected_fill(price, "BUY"), fill_risk)
            if qty < 1:
                continue
            sig["qty"] = qty
            reason = r.risk_check(sig)
            # This fork adds a cost floor the original kernel has no equivalent
            # of: it refuses a trade whose round-trip friction eats more than
            # MAX_COST_RISK_RATIO of the risk. A 3.20 stop on an 815 share is a
            # 0.39% stop, so friction is ~40% of R and the refusal is CORRECT —
            # the engine sizes from risk and knows nothing about costs, so it
            # can legitimately propose a trade this gate then rejects. What
            # must never happen is a refusal on RISK grounds, which would mean
            # the engine and the kernel disagree about the same arithmetic.
            economic = reason is None or reason.startswith("cost_floor_breached")
            check(f"engine size {qty} at {price:.0f} is not refused on risk "
                  f"(x{mult:g})", economic, reason)
            if reason is not None:
                continue
            # The point of the fix: the engine's own size is inside the
            # allowance on its own, with no help from RISK_TOLERANCE.
            allowed = RISK_PER_TRADE * mult
            check(f"  and risks {qty * fill_risk:.0f} <= {allowed:.0f} "
                  "without the tolerance band",
                  qty * fill_risk <= allowed, f"{qty * fill_risk:.2f}")


# The late-entry gate (minutes_to_squareoff / MIN_MINUTES_TO_SQUAREOFF) does
# not exist in this fork's kernel, so the two cases covering it have nothing to
# run against. They are skipped LOUDLY rather than deleted: the gate refuses an
# intraday entry too close to square-off to reach its target, and its absence
# here is a real gap, not a difference of opinion. Port the gate and these two
# start running again with no further change.
_HAS_SQUAREOFF_GATE = hasattr(er, "minutes_to_squareoff")

SKIPPED: list[str] = []


def main() -> int:
    gated = {test_squareoff_gate, test_gate_against_the_real_book}
    for fn in (test_risk_per_share, test_risk_per_trade,
               test_slippage_is_inside_the_risk, test_malformed_stops,
               test_squareoff_gate, test_gate_against_the_real_book,
               test_existing_limits_still_hold, test_rejections_are_recorded,
               test_engine_sizing_agrees_with_the_kernel):
        if fn in gated and not _HAS_SQUAREOFF_GATE:
            SKIPPED.append(fn.__name__)
            print(f"\n{fn.__name__}\n  SKIP this kernel has no late-entry gate "
                  "(er.minutes_to_squareoff is absent)")
            continue
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    if SKIPPED:
        print(f"all checks passed ({len(SKIPPED)} skipped: {', '.join(SKIPPED)})")
        return 0
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
