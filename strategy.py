"""
strategy.py - Opening Range Breakout + multi-timeframe MA strategy

RULES
  1. Opening range (OR) = first 15 min (09:15-09:30 IST, three 5m bars).
       OR bias   : first-15m open vs close (green = long bias, red = short bias)
       OR levels : high / low of those 15 minutes
  2. Trend filters (all must agree with trade direction):
       30m 200 MA and 1H 200 MA            (intraday trend)
       Daily 50 MA and 200 MA              (big trend)
       Daily 365 MA = "wall": skip if price sits just under it (long) or just over it (short)
       Daily 20/100/150 MAs are computed for charting/context only.
  3. Entry : 5m candle CLOSES beyond OR high (buy) / OR low (sell), then enter at NEXT bar open.
             Needs volume >= 1.5x 20-bar avg and price on correct side of session VWAP.
  4. Stop  : opposite side of OR (or ATR-based). Target = rr x risk. Time exit 15:15.
  5. Skips : OR width outside 0.3-1.5%, gap > 1.5%, cost gate (friction > 30% of stop),
             after daily loss limit / profit target, after max trades per day.

DATA   CSV with columns: datetime (or timestamp, epoch seconds ok), open, high, low, close, volume
       5-minute bars, IST. Needs history for MA warmup:
         30m 200 MA ~ 16 trading days, 1H 200 MA ~ 29 days, daily 200 MA = 200 days (365 MA = 365 days).
       Daily filter without enough history = zero trades. Use --no-daily for short data.
       Index spot data has volume = 0, so use --no-volume --no-vwap, or use index futures data.

USAGE  python strategy.py --csv SBIN.csv RELIANCE.csv --split 0.7 --out trades.csv
       Tune parameters on the TRAIN part only. Look at the TEST part once.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, replace
from datetime import time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

DAILY_MAS = (20, 50, 100, 150, 200, 365)


# --------------------------------------------------------------------------- config
@dataclass
class Config:
    # opening range
    or_start: dtime = dtime(9, 15)
    or_end: dtime = dtime(9, 30)          # exclusive
    or_min_width_pct: float = 0.3
    or_max_width_pct: float = 1.5
    use_or_bias: bool = True              # breakout direction must match first-15m candle colour
    # filters
    use_trend: bool = True                # 30m + 1H 200 MA
    use_daily: bool = True                # daily 50 + 200 MA
    use_wall: bool = True                 # daily 365 MA wall
    wall_pct: float = 0.5
    use_volume: bool = True
    vol_mult: float = 1.5
    use_vwap: bool = True
    max_gap_pct: float = 1.5
    # timing
    entry_cutoff: dtime = dtime(14, 30)
    exit_time: dtime = dtime(15, 15)
    # exits / risk
    stop_mode: str = "or"                 # "or" or "atr"
    atr_mult: float = 1.5
    rr: float = 1.5
    risk_per_trade: float = 75.0          # Rs
    position_cap: float = 3000.0          # Rs notional per trade
    max_trades_per_day: int = 1
    daily_loss_limit: float = 500.0       # Rs, stop trading for the day
    daily_profit_target: float = 1000.0   # Rs, stop trading for the day
    # costs (percent of notional)
    cost_roundtrip_pct: float = 0.10      # brokerage + STT + exchange + GST + stamp, approx
    slippage_pct: float = 0.02            # per side
    max_cost_to_stop: float = 0.30        # friction must be <= 30% of stop distance
    # baseline mode
    direction_mode: str = "signal"        # "signal" or "random" (coin-flip direction baseline)
    seed: int = 42


# --------------------------------------------------------------------------- data
def load_ohlcv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    tcol = next((c for c in ("datetime", "timestamp", "date", "time") if c in df.columns), None)
    if tcol is None:
        raise ValueError(f"{path}: need a datetime/timestamp column")
    t = df[tcol]
    if pd.api.types.is_numeric_dtype(t):
        idx = pd.to_datetime(t, unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    else:
        idx = pd.to_datetime(t)
        if idx.dt.tz is not None:
            idx = idx.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df.index = pd.DatetimeIndex(idx)
    df = df[["open", "high", "low", "close", "volume"]].astype(float).sort_index()
    df = df[~df.index.duplicated()].between_time("09:15", "15:29")
    step = df.index.to_series().diff().dropna().median()
    if step != pd.Timedelta(minutes=5):
        raise ValueError(f"{path}: expected 5-minute bars, got median step {step}")
    return df


def _htf_ma(d: pd.DataFrame, rule: str, n: int) -> pd.Series:
    """n-bar SMA on a higher timeframe, no lookahead.
    Value at a 5m bar uses only higher-timeframe bars that are already complete."""
    bars = d["close"].resample(rule, origin="start_day", offset="15min").last().dropna()
    ma = bars.rolling(n).mean().shift(1)
    return ma.reindex(d.index, method="ffill")


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Add all indicator columns. Every value is known at the close of its 5m bar."""
    d = df.copy()
    d["date"] = d.index.normalize()
    d["ma30"] = _htf_ma(d, "30min", 200)
    d["ma1h"] = _htf_ma(d, "60min", 200)

    daily_close = d.groupby("date")["close"].last()
    for n in DAILY_MAS:  # previous completed days only
        m = daily_close.rolling(n).mean().shift(1)
        d[f"d{n}"] = m.reindex(d["date"]).to_numpy()
    d["prev_close"] = d["date"].map(daily_close.shift(1))

    tp = (d["high"] + d["low"] + d["close"]) / 3
    pv = (tp * d["volume"]).groupby(d["date"]).cumsum()
    vv = d["volume"].groupby(d["date"]).cumsum()
    d["vwap"] = pv / vv.replace(0, np.nan)
    d["vol_avg"] = d["volume"].rolling(20).mean().shift(1)

    pc = d["close"].shift(1)
    tr = pd.concat([d["high"] - d["low"], (d["high"] - pc).abs(), (d["low"] - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.rolling(14).mean()
    return d


# --------------------------------------------------------------------------- rules
def opening_range(day: pd.DataFrame, cfg: Config) -> dict | None:
    orb = day.between_time(cfg.or_start, cfg.or_end, inclusive="left")
    if len(orb) < 3:
        return None
    hi, lo = orb["high"].max(), orb["low"].min()
    o, c = orb["open"].iloc[0], orb["close"].iloc[-1]
    return {
        "high": hi, "low": lo, "open": o, "close": c,
        "width_pct": (hi - lo) / c * 100,
        "bias": 1 if c > o else (-1 if c < o else 0),
    }


def _near_wall(price: float, ma: float, pct: float, side: int) -> bool:
    if ma != ma:  # NaN
        return False
    gap = (ma - price) / price * 100 if side == 1 else (price - ma) / price * 100
    return 0 < gap <= pct


def _bar_side(row, rng: dict, cfg: Config) -> int:
    """Return +1 buy, -1 sell, 0 no trade, evaluated on a COMPLETED 5m bar.
    NaN indicator (warmup) always means no trade."""
    c = row.close
    if c > rng["high"]:
        side = 1
    elif c < rng["low"]:
        side = -1
    else:
        return 0

    if cfg.use_or_bias and rng["bias"] != side:
        return 0
    if cfg.use_trend:
        if side == 1 and not (c > row.ma30 and c > row.ma1h):
            return 0
        if side == -1 and not (c < row.ma30 and c < row.ma1h):
            return 0
    if cfg.use_daily:
        if side == 1 and not (c > row.d50 and c > row.d200):
            return 0
        if side == -1 and not (c < row.d50 and c < row.d200):
            return 0
    if cfg.use_wall and _near_wall(c, row.d365, cfg.wall_pct, side):
        return 0
    if cfg.use_volume and not (row.volume >= cfg.vol_mult * row.vol_avg):
        return 0
    if cfg.use_vwap:
        if side == 1 and not (c > row.vwap):
            return 0
        if side == -1 and not (c < row.vwap):
            return 0
    return side


def _build_order(side: int, entry: float, rng: dict, atr: float, cfg: Config):
    """Return (order dict, None) or (None, reason)."""
    if cfg.stop_mode == "atr":
        risk = cfg.atr_mult * atr
    else:
        risk = abs(entry - (rng["low"] if side == 1 else rng["high"]))
    if not (risk > 0):
        return None, "bad_stop"
    # stop must be on the correct side of entry (gap through OR)
    if cfg.stop_mode == "or" and ((side == 1 and entry <= rng["low"]) or (side == -1 and entry >= rng["high"])):
        return None, "bad_stop"

    stop_pct = risk / entry * 100
    friction = cfg.cost_roundtrip_pct + 2 * cfg.slippage_pct
    if friction > cfg.max_cost_to_stop * stop_pct:
        return None, "cost_gate"

    qty = int(min(cfg.risk_per_trade / risk, cfg.position_cap / entry))
    if qty < 1:
        return None, "qty_zero"
    return {
        "risk": risk,
        "stop": entry - side * risk,
        "target": entry + side * cfg.rr * risk,
        "qty": qty,
    }, None


def _simulate(bars: list, start: int, side: int, stop: float, target: float, cfg: Config):
    """Walk bars from the entry bar. Stop is checked before target (conservative)."""
    for j in range(start, len(bars)):
        b = bars[j]
        if b.Index.time() >= cfg.exit_time:
            return j, b.open, "time"
        if side == 1:
            if b.low <= stop:
                return j, min(stop, b.open), "stop"
            if b.high >= target:
                return j, max(target, b.open), "target"
        else:
            if b.high >= stop:
                return j, max(stop, b.open), "stop"
            if b.low <= target:
                return j, min(target, b.open), "target"
    return len(bars) - 1, bars[-1].close, "eod"


# --------------------------------------------------------------------------- backtest
def backtest(d: pd.DataFrame, cfg: Config, symbol: str = "") -> tuple[pd.DataFrame, Counter]:
    rnd = np.random.default_rng(cfg.seed)
    trades: list[dict] = []
    skipped: Counter = Counter()

    for date, day in d.groupby("date"):
        rng = opening_range(day, cfg)
        if rng is None:
            skipped["no_or"] += 1
            continue
        if not (cfg.or_min_width_pct <= rng["width_pct"] <= cfg.or_max_width_pct):
            skipped["or_width"] += 1
            continue
        pc = day["prev_close"].iloc[0]
        if pc == pc and abs(rng["open"] - pc) / pc * 100 > cfg.max_gap_pct:
            skipped["gap"] += 1
            continue

        bars = list(day.itertuples())
        i = next((k for k, b in enumerate(bars) if b.Index.time() >= cfg.or_end), None)
        if i is None:
            continue

        n_trades, day_pnl, seen = 0, 0.0, set()
        while i < len(bars) - 1 and n_trades < cfg.max_trades_per_day:
            b = bars[i]
            if b.Index.time() > cfg.entry_cutoff:
                break
            if day_pnl <= -cfg.daily_loss_limit or day_pnl >= cfg.daily_profit_target:
                break

            side = _bar_side(b, rng, cfg)
            if side == 0:
                i += 1
                continue
            if cfg.direction_mode == "random":
                side = int(rnd.choice([1, -1]))

            nb = bars[i + 1]
            if nb.Index.time() >= cfg.exit_time:
                break
            entry = nb.open
            order, why = _build_order(side, entry, rng, b.atr, cfg)
            if order is None:
                if why not in seen:
                    seen.add(why)
                    skipped[why] += 1
                i += 1
                continue

            j, exit_px, reason = _simulate(bars, i + 1, side, order["stop"], order["target"], cfg)
            qty, slip = order["qty"], cfg.slippage_pct / 100
            fill_in, fill_out = entry * (1 + side * slip), exit_px * (1 - side * slip)
            gross = side * (exit_px - entry) * qty
            net = side * (fill_out - fill_in) * qty - cfg.cost_roundtrip_pct / 100 * entry * qty

            trades.append({
                "symbol": symbol, "date": date, "side": "BUY" if side == 1 else "SELL",
                "entry_time": nb.Index, "entry": entry, "stop": order["stop"], "target": order["target"],
                "exit_time": bars[j].Index, "exit": exit_px, "reason": reason, "qty": qty,
                "or_width_pct": rng["width_pct"], "gross": gross, "net": net,
                "r_net": net / (order["risk"] * qty),
            })
            n_trades += 1
            day_pnl += net
            i = j + 1

    return pd.DataFrame(trades), skipped


# --------------------------------------------------------------------------- stats
def summarize(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"trades": 0}
    t = t.sort_values(["date", "entry_time"])
    wins = t.loc[t.net > 0, "net"].sum()
    losses = -t.loc[t.net < 0, "net"].sum()
    eq = t["net"].cumsum()
    return {
        "trades": len(t),
        "win_rate_%": round((t.net > 0).mean() * 100, 1),
        "avg_R_net": round(t.r_net.mean(), 3),
        "profit_factor": round(wins / losses, 2) if losses > 0 else float("inf"),
        "net_pnl_Rs": round(t.net.sum(), 1),
        "gross_pnl_Rs": round(t.gross.sum(), 1),
        "max_drawdown_Rs": round((eq - eq.cummax()).min(), 1),
        "stop_%": round((t.reason == "stop").mean() * 100, 1),
        "target_%": round((t.reason == "target").mean() * 100, 1),
        "time_exit_%": round((t.reason.isin(["time", "eod"])).mean() * 100, 1),
    }


def variants(cfg: Config) -> dict[str, Config]:
    """Strategy must beat both baselines or it has no edge."""
    plain = replace(cfg, use_or_bias=False, use_trend=False, use_daily=False,
                    use_wall=False, use_volume=False, use_vwap=False)
    return {
        "full": cfg,
        "plain_orb": plain,
        "random_dir": replace(plain, direction_mode="random"),
    }


# --------------------------------------------------------------------------- live helper
def latest_signal(df: pd.DataFrame, cfg: Config | None = None) -> dict | None:
    """Check the LAST COMPLETED 5m bar in df (history + today). Returns an order plan or None.
    Pass only completed bars. For options: trade the direction of this signal on the underlying."""
    cfg = cfg or Config()
    d = prepare(df)
    day = d[d["date"] == d["date"].iloc[-1]]
    rng = opening_range(day, cfg)
    if rng is None or not (cfg.or_min_width_pct <= rng["width_pct"] <= cfg.or_max_width_pct):
        return None
    pc = day["prev_close"].iloc[0]
    if pc == pc and abs(rng["open"] - pc) / pc * 100 > cfg.max_gap_pct:
        return None
    row = next(day.tail(1).itertuples())
    if not (cfg.or_end <= row.Index.time() <= cfg.entry_cutoff):
        return None
    side = _bar_side(row, rng, cfg)
    if side == 0:
        return None
    order, why = _build_order(side, row.close, rng, row.atr, cfg)
    if order is None:
        return {"side": "NONE", "blocked_by": why}
    return {"side": "BUY" if side == 1 else "SELL", "time": row.Index, "entry_ref": row.close,
            "stop": order["stop"], "target": order["target"], "qty": order["qty"], "or": rng}


# --------------------------------------------------------------------------- cli
def main() -> None:
    ap = argparse.ArgumentParser(description="ORB + MA strategy backtest")
    ap.add_argument("--csv", nargs="+", required=True, help="5m OHLCV csv file(s), one per symbol")
    ap.add_argument("--split", type=float, default=0.7, help="chronological train fraction")
    ap.add_argument("--rr", type=float, default=1.5)
    ap.add_argument("--stop-mode", choices=["or", "atr"], default="or")
    ap.add_argument("--max-trades", type=int, default=1)
    ap.add_argument("--no-daily", action="store_true", help="disable daily 50/200 MA filter (short history)")
    ap.add_argument("--no-volume", action="store_true")
    ap.add_argument("--no-vwap", action="store_true")
    ap.add_argument("--out", help="write full-strategy trades to this csv")
    a = ap.parse_args()

    cfg = Config(rr=a.rr, stop_mode=a.stop_mode, max_trades_per_day=a.max_trades,
                 use_daily=not a.no_daily, use_wall=not a.no_daily,
                 use_volume=not a.no_volume, use_vwap=not a.no_vwap)

    frames = {Path(p).stem: prepare(load_ohlcv(p)) for p in a.csv}
    all_dates = sorted(set().union(*[set(f["date"].unique()) for f in frames.values()]))
    if cfg.use_daily and len(all_dates) < 220:
        print(f"WARNING: only {len(all_dates)} trading days. Daily 200 MA needs 200+ prior days, "
              "so almost no trades will pass. Get more history or use --no-daily.\n")
    cut = all_dates[int(len(all_dates) * a.split)]
    print(f"{len(frames)} symbol(s), {len(all_dates)} days. Train < {cut.date()} <= Test\n")

    rows, full_trades = [], None
    for name, vcfg in variants(cfg).items():
        parts, skipped = [], Counter()
        for sym, f in frames.items():
            tr, sk = backtest(f, vcfg, sym)
            parts.append(tr)
            skipped.update(sk)
        trades = pd.concat([p for p in parts if not p.empty], ignore_index=True) if any(
            not p.empty for p in parts) else pd.DataFrame()
        if name == "full":
            full_trades = trades
            print("Full-strategy skipped days:", dict(skipped), "\n")
        for label, part in (("train", trades[trades.date < cut] if not trades.empty else trades),
                            ("test", trades[trades.date >= cut] if not trades.empty else trades)):
            rows.append({"variant": name, "set": label, **summarize(part)})

    print(pd.DataFrame(rows).to_string(index=False))
    print("\nRead it: 'full' must beat 'plain_orb' AND 'random_dir' on TEST, with avg_R_net > 0.")
    print("Want 100+ test trades before trusting anything. Fewer = noise.")

    if a.out and full_trades is not None and not full_trades.empty:
        full_trades.to_csv(a.out, index=False)
        print(f"\nTrades written to {a.out}")


if __name__ == "__main__":
    main()
