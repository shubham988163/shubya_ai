"""Correctness tests for the call-option plan.

The dangerous output here is a confident "buy this call" on a contract that
cannot pay: a cheap far-OTM strike expiring today looks attractive precisely
because it is nearly worthless. So the cases below are built around the
breakeven test — the stock hitting the scanner's own target must actually put
the option in profit — and around same-day expiry.

Run with:

    .venv/bin/python -m trading.test_options
"""
from __future__ import annotations

import sys
from datetime import date, datetime

from trading.fno import options as opt
from trading.fno import pricing as pr
from trading.fno.models import IST, Provenance

FAILURES: list[str] = []
TODAY = date(2026, 8, 25)
PROV = Provenance("test", "x", "snapshot",
                  datetime(2026, 8, 25, 10, 0, tzinfo=IST),
                  datetime(2026, 8, 25, 10, 0, tzinfo=IST))


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{': ' + detail if detail else ''}")
        print(f"  FAIL {name} {detail}")


def q(symbol="ACME", strike=100.0, ltp=1.0, kind="Call", expiry="29-Sep-2026",
      oi=5_000.0, vol=50_000.0, spot=100.0):
    return opt.OptionQuote(underlying=symbol, identifier=f"{symbol}{strike}{kind}",
                           option_type=kind, strike=strike, expiry=expiry,
                           last_price=ltp, pct_change=0.0, open_interest=oi,
                           volume=vol, underlying_value=spot, prov=PROV)


def test_quote():
    print("quote maths")
    check("a call breaks even at strike plus premium",
          q(strike=100, ltp=2.5).breakeven == 102.5)
    check("a put breaks even at strike minus premium",
          q(strike=100, ltp=2.5, kind="Put").breakeven == 97.5)
    check("a strike on the money reads ATM", q(strike=100, spot=100).moneyness == "ATM")
    check("a call above spot is out of the money",
          q(strike=110, spot=100).moneyness == "OTM")
    check("a call below spot is in the money",
          q(strike=90, spot=100).moneyness == "ITM")
    check("expiry today is detected",
          q(expiry="25-Aug-2026").expires_today(TODAY))
    check("a later expiry is not today",
          not q(expiry="29-Sep-2026").expires_today(TODAY))
    check("an unparseable expiry is not claimed to be today",
          not q(expiry="whenever").expires_today(TODAY))
    check("thin open interest is flagged", q(oi=10).liquidity_problem())
    check("thin volume is flagged", q(vol=10).liquidity_problem())
    check("a liquid contract is not flagged", q().liquidity_problem() is None)


def test_plan():
    print("call selection")
    # Stock at 100 with targets at 102 (1:2) and 103 (1:3).
    args = dict(spot=100.0, target1=102.0, target2=103.0, today=TODAY)

    p = opt.plan_call("ACME", [], **args)
    check("no listed option is stated, not guessed around",
          p.quote is None and "no live option data" in p.rejections[0])

    # A 100 CE at 1.00 breaks even at 101 — under target 1, so T1 pays.
    good = q(strike=100, ltp=1.0)
    p = opt.plan_call("ACME", [good], **args)
    check("a strike whose breakeven clears target 1 is selected",
          p.tradeable and p.quote is good and p.clears_t1, str(p.rejections))
    check("the breakeven is reported", p.breakeven == 101.0)

    # A 105 CE at 1.00 breaks even at 106 — beyond even the 1:3 target.
    far = q(strike=105, ltp=1.0)
    p = opt.plan_call("ACME", [far], **args)
    check("with no stop given, a strike beyond the 1:3 breakeven is rejected",
          not p.tradeable and any("breaks even" in r for r in p.rejections),
          str(p.rejections))
    check("and the rejection says why it could not be risk-checked",
          any("reward-to-risk cannot be checked" in r for r in p.rejections),
          str(p.rejections))

    # Between targets: pays only at 1:3.
    mid = q(strike=102, ltp=0.6)
    p = opt.plan_call("ACME", [mid], **args)
    check("a strike paying only at the 1:3 target is allowed but warned",
          p.tradeable and not p.clears_t1 and p.clears_t2
          and any("above target 1" in w for w in p.warnings), str(p.warnings))

    p = opt.plan_call("ACME", [q(strike=100, ltp=1.0, expiry="25-Aug-2026")], **args)
    check("same-day expiry is warned about, loudly",
          any("EXPIRES TODAY" in w for w in p.warnings), str(p.warnings))

    p = opt.plan_call("ACME", [q(strike=100, ltp=1.0, oi=5)], **args)
    check("an illiquid strike is not offered",
          not p.tradeable and any("open interest" in r for r in p.rejections),
          str(p.rejections))

    p = opt.plan_call("ACME", [q(strike=100, ltp=9.0)], **args)
    check("a premium too large a share of spot is rejected", not p.tradeable)

    # Two viable strikes: prefer the one with more room over its breakeven.
    cheap_near = q(strike=100, ltp=0.5)          # breakeven 100.5
    pricier = q(strike=101, ltp=1.0)             # breakeven 102.0
    p = opt.plan_call("ACME", [pricier, cheap_near], **args)
    check("the strike with the most headroom over breakeven wins",
          p.quote is cheap_near, str(p.quote and p.quote.strike))

    check("puts are never offered for a long", not opt.calls_for(
        "ACME", [q(kind="Put")]))


def test_real_shape():
    """The live board's actual contracts must be handled, not just tidy ones."""
    print("live-shaped contracts")
    # AUBANK 1140 CE @ 1.50 expiring today, spot 1134.50, OI 195 — real values
    # pulled from the board on 25 Aug 2026.
    aubank = q("AUBANK", strike=1140, ltp=1.50, expiry="25-Aug-2026",
               oi=195, vol=6_920_000, spot=1134.5)
    p = opt.plan_call("AUBANK", [aubank], spot=1134.5, target1=1136.0,
                      target2=1137.0, today=TODAY)
    check("a same-day strike beyond the targets is refused when unriskable",
          not p.tradeable, str(p.rejections))
    check("and the refusal names the breakeven or the thinness",
          any("breaks even" in r or "open interest" in r for r in p.rejections),
          str(p.rejections))
    check("describe() leads with the refusal, not the strike",
          opt.describe(p).startswith("options: NO CALL"), opt.describe(p))


def test_pricing():
    """The model behind the projected exit prices."""
    print("option pricing")
    yr = 30 / 365
    check("at expiry an option is worth its intrinsic value",
          pr.call_price(105, 100, 0.0, 0.3) == 5.0
          and pr.call_price(95, 100, 0.0, 0.3) == 0.0)
    check("a call is worth more the higher the stock",
          pr.call_price(101, 100, yr, 0.3) > pr.call_price(100, 100, yr, 0.3))
    check("a call is worth more at higher volatility",
          pr.call_price(100, 100, yr, 0.4) > pr.call_price(100, 100, yr, 0.2))
    check("a call is worth more with longer to run",
          pr.call_price(100, 100, yr, 0.3) > pr.call_price(100, 100, yr / 10, 0.3))

    px = pr.call_price(100, 100, yr, 0.35)
    solved = pr.implied_vol(px, 100, 100, yr)
    check("implied vol round-trips the price it came from",
          solved is not None and abs(solved - 0.35) < 1e-4, str(solved))

    check("a price below intrinsic has no implied vol (stale print)",
          pr.implied_vol(0.35, 101.5, 101.0, yr) is None)
    check("a zero price has no implied vol", pr.implied_vol(0, 100, 100, yr) is None)
    check("an expired option has no implied vol",
          pr.implied_vol(1.0, 100, 100, 0.0) is None)

    now = datetime(2026, 8, 25, 10, 0, tzinfo=IST)
    iv, proj = pr.project(spot_now=100.0, strike=100.0, premium_now=2.0,
                          expiry=date(2026, 9, 24), now=now, targets=[102.0, 104.0])
    check("projected premium rises with the stock", proj[1].premium > proj[0].premium)
    check("and both are above the premium paid", proj[0].premium > 2.0)

    _, slow = pr.project(spot_now=100.0, strike=100.0, premium_now=2.0,
                         expiry=date(2026, 9, 24), now=now, targets=[102.0],
                         hold_hours=48.0)
    check("holding longer costs decay — the same target pays less",
          slow[0].premium < proj[0].premium,
          f"{slow[0].premium:.4f} vs {proj[0].premium:.4f}")

    check("time to expiry counts the remaining session, not whole days",
          pr.years_to_expiry(date(2026, 8, 25), now) > 0
          and pr.years_to_expiry(date(2026, 8, 25),
                                 datetime(2026, 8, 25, 15, 30, tzinfo=IST)) == 0.0)


def test_option_risk_reward():
    """The option's own R:R, which is not the stock's."""
    print("option risk/reward")
    now = datetime(2026, 8, 25, 10, 0, tzinfo=IST)
    # A move worth the premium: spot 100 → 102, stop 99, ATM call at 2.00.
    good = q("ACME", strike=100.0, ltp=2.0, expiry="24-Sep-2026",
             oi=9600, vol=310000, spot=100.0)
    p = opt.plan_call("ACME", [good], spot=100.0, target1=102.0, target2=103.0,
                      stop=99.0, now=now, today=TODAY)
    check("a worthwhile move is offered, with its modelled exits",
          p.tradeable and p.premium_at_stop and p.premium_at_t1 and p.premium_at_t2,
          f"{p.rejections} {p.premium_at_stop} {p.premium_at_t1}")
    check("premium falls at the stop and rises at the targets",
          p.premium_at_stop < good.last_price < p.premium_at_t1 < p.premium_at_t2)
    check("the option's reward-to-risk is computed and beats one-to-one",
          p.option_rr is not None and p.option_rr >= 1.0, str(p.option_rr))

    # The real trap: a 1:2 stock trade whose option version loses.
    itm = q("RELIANCE", strike=100.0, ltp=1.62, expiry="24-Sep-2026",
            oi=9600, vol=310000, spot=101.5)
    p2 = opt.plan_call("RELIANCE", [itm], spot=101.5, target1=101.56,
                       target2=101.74, stop=101.02, now=now, today=TODAY)
    check("a 1:2 stock trade whose option risks more than it makes is refused",
          not p2.tradeable
          and any("worse" in r and ":1" in r for r in p2.rejections),
          str(p2.rejections))

    # A premium no volatility explains must not silently produce projections.
    bad = q("ACME", strike=101.0, ltp=0.35, expiry="24-Sep-2026", spot=101.5)
    p2 = opt.plan_call("ACME", [bad], spot=101.5, target1=102.0, target2=103.0,
                       stop=101.0, now=now, today=TODAY)
    check("an impossible premium yields no projection, with a warning",
          p2.premium_at_t1 is None
          and any("stale print" in w for w in p2.warnings), str(p2.warnings))


def test_fyers_parsing():
    """The Fyers adapter's parsing and failure paths, without a network."""
    print("fyers adapter")
    from trading.fno import fyers

    raw = [{"timestamp": 1787000100, "open": 100.0, "high": 101.0, "low": 99.5,
            "close": 100.8, "volume": 12000},
           {"timestamp": 1787000400, "open": 100.8, "high": 101.4, "low": 100.6,
            "close": 101.2, "volume": 9000}]
    df = fyers.to_frame(raw)
    check("Fyers candles become an IST-indexed OHLCV frame",
          df is not None and list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
          and str(df.index.tz) == "Asia/Kolkata", str(df.index.tz if df is not None else None))
    check("and they are sorted oldest first", df.index[0] < df.index[1])
    check("an empty response is None, not an empty frame",
          fyers.to_frame([]) is None)
    check("rows without a close are dropped",
          fyers.to_frame([{"timestamp": 1787000100, "close": None}]) is None)

    check("index tickers map to Fyers names",
          fyers.INDEX_SYMBOLS["^NSEI"] == "NSE:NIFTY50-INDEX")

    # An unreachable server must be a clear message, never a traceback.
    ok, note = fyers.FyersClient("http://127.0.0.1:9").status()
    check("an unreachable Fyers server reports plainly, and is not connected",
          ok is False and "cannot reach" in note, note)

    # A missing SDK must also be a clear install hint, never a raw traceback.
    try:
        ok, note = fyers.FyersClient().status()
    except Exception as exc:  # pragma: no cover - this is the current failure mode we are fixing
        check("missing fyers SDK is surfaced as a friendly message",
              False, f"raised {type(exc).__name__}: {exc}")
    else:
        check("missing fyers SDK is surfaced as a friendly message",
              ok is False and ("install" in note.lower() or "fyers" in note.lower()), note)


def _nifty_bars(rows):
    """5-minute NIFTY bars — index feeds carry no volume, which is the point."""
    import pandas as pd
    idx = pd.DatetimeIndex([pd.Timestamp(f"2026-08-26T{t}:00+05:30") for t, *_ in rows])
    return pd.DataFrame(
        {"Open": [r[1] for r in rows], "High": [r[2] for r in rows],
         "Low": [r[3] for r in rows], "Close": [r[4] for r in rows],
         "Volume": [0.0] * len(rows)}, index=idx)


# OR 24300-24360; breaks out at 09:35, retests 24358, holds 24370.
NIFTY_BREAKOUT = [
    ("09:15", 24330, 24355, 24300, 24340), ("09:20", 24340, 24360, 24330, 24350),
    ("09:25", 24350, 24360, 24340, 24355), ("09:30", 24355, 24365, 24350, 24362),
    ("09:35", 24362, 24380, 24360, 24376), ("09:40", 24376, 24378, 24358, 24368),
    ("09:45", 24368, 24374, 24364, 24370),
]
NIFTY_FLAT = [(t, o, h, l, c) for t, o, h, l, c in NIFTY_BREAKOUT[:3]] + [
    ("09:30", 24355, 24358, 24345, 24350), ("09:35", 24350, 24356, 24344, 24348),
    ("09:40", 24348, 24352, 24340, 24345), ("09:45", 24345, 24350, 24338, 24342),
]


def _chain(strikes, expiry="01-Sep-2026", spot=24370.0):
    return [q("NIFTY", strike=s, ltp=p, kind="Call", expiry=expiry,
              oi=60000, vol=20_000_000, spot=spot) for s, p in strikes]


def test_index_options():
    """NIFTY 50 — a different instrument, with three fewer confirmations."""
    print("nifty index options")
    from trading.fno import index_options as ix
    from trading.fno.models import MarketContext

    now = datetime(2026, 8, 26, 9, 50, tzinfo=IST)
    bull = MarketContext(classification="NEUTRAL", nifty_pct=0.2, banknifty_pct=0.5,
                         nifty_above_vwap=None, breadth_pct=55.0, advances=27,
                         declines=23, sector_pct={"BANK": 0.7})
    chain = _chain([(24350, 163.0), (24400, 135.0), (24500, 82.0)])

    p = ix.analyse(_nifty_bars(NIFTY_BREAKOUT), now, bull, chain)
    check("the opening range is read off the index",
          abs(p.or_high - 24360) < 1e-6 and abs(p.or_low - 24300) < 1e-6,
          f"{p.or_low}-{p.or_high}")
    check("a close above the range is a breakout that is holding",
          p.breakout and p.holding, p.status)
    check("the retest into the level is seen", p.retested, p.status)
    check("index levels are produced", p.stop < p.entry_low <= p.entry_high
          < p.target1 < p.target2, f"{p.stop} {p.entry_low} {p.target1}")
    check("a front-expiry call is chosen", p.option.quote is not None,
          str(p.option.rejections))
    check("the missing-volume caveat is always stated",
          any("no volume" in c for c in p.caveats))

    flat = ix.analyse(_nifty_bars(NIFTY_FLAT), now, bull, chain)
    check("no breakout means no index trade",
          not flat.breakout and not flat.tradeable
          and any("has not closed above" in r for r in flat.rejections),
          str(flat.rejections))

    bear = MarketContext(classification="STRONGLY BEARISH", nifty_pct=-0.9,
                         banknifty_pct=-1.0, nifty_above_vwap=None,
                         breadth_pct=18.0, advances=9, declines=41)
    p2 = ix.analyse(_nifty_bars(NIFTY_BREAKOUT), now, bear, chain)
    check("a bearish tape blocks a long on the index it is made of",
          not p2.tradeable and any("fights the tape" in r for r in p2.rejections),
          str(p2.rejections))

    far = _chain([(24350, 163.0)], expiry="27-Oct-2026")
    near = _chain([(24350, 163.0)])
    p3 = ix.analyse(_nifty_bars(NIFTY_BREAKOUT), now, bull, far + near)
    check("the front expiry is preferred over a later one",
          p3.option.quote is not None and p3.option.quote.expiry == "01-Sep-2026",
          str(p3.option.quote and p3.option.quote.expiry))

    check("render() never crashes on a plan with no trade",
          "NIFTY 50 OPTIONS" in ix.render(flat) and "NIFTY" in ix.render(None))


def test_decay():
    """The cost of being early — the number that decides a cheap strike."""
    print("time decay")
    now = datetime(2026, 8, 27, 10, 0, tzinfo=IST)
    exp = date(2026, 9, 1)

    near = pr.decay_curve(spot=24200.0, strike=24200.0, premium_now=133.5,
                          expiry=exp, now=now)
    far = pr.decay_curve(spot=24200.0, strike=24850.0, premium_now=4.35,
                         expiry=exp, now=now)
    check("a flat underlying still loses the option value",
          near[0]["premium"] > near[-1]["premium"])
    check("value drains monotonically with time",
          all(a["premium"] >= b["premium"] for a, b in zip(near, near[1:])))
    check("the far strike keeps far less of its value than the near one",
          far[-1]["pct_left"] < near[-1]["pct_left"] / 3,
          f"far {far[-1]['pct_left']:.0f}% vs near {near[-1]['pct_left']:.0f}%")
    check("a cheap far strike is nearly worthless after a few flat days",
          far[-1]["pct_left"] < 25, f"{far[-1]['pct_left']:.0f}%")
    check("an unmodellable premium yields no curve",
          pr.decay_curve(spot=100.0, strike=101.0, premium_now=0.0,
                         expiry=exp, now=now) == [])

    # And the plan warns about it rather than leaving it to be worked out.
    cheap = q("NIFTY", strike=24850.0, ltp=4.35, expiry="01-Sep-2026",
              oi=32_000, vol=5_000_000, spot=24200.0)
    p = opt.plan_call("NIFTY", [cheap], spot=24200.0, target1=24260.0,
                      target2=24290.0, stop=24170.0, now=now, today=now.date())
    check("the plan carries the decay curve", len(p.decay) == 4, str(len(p.decay)))
    check("and warns when the strike cannot afford to wait",
          any("bet on the move happening now" in w for w in p.warnings),
          str(p.warnings))


def main() -> int:
    for fn in (test_quote, test_plan, test_real_shape, test_pricing,
               test_option_risk_reward, test_fyers_parsing, test_index_options,
               test_decay):
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
