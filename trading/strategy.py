"""Strategy engine — EMA 9/21 crossover on 5-minute candles (delayed data).

Rules:
  Entry  : EMA(9) crosses above EMA(21) on a closed candle -> BUY
           EMA(9) crosses below EMA(21)                     -> SELL (intraday short)
  Stop   : swing low (long) / swing high (short) of the last SWING_LOOKBACK bars
  Target : entry +/- RR_TARGET x risk  (1:2 by default)
  Sizing : RISK_PER_TRADE / per-share risk, capped by the day's position cap
  Exits  : stop or target checked against candle low/high (SL assumed to fill
           first if both hit in one bar — conservative), square-off at 15:15 IST

Data comes from yfinance (delayed ~15 min) — fine for paper validation; swap in
the Kite WebSocket later without touching the rules. Exit checks against candle
extremes are optimistic vs. live ticks; treat paper results accordingly.

Usage:
  python -m trading.strategy               # live polling loop during market hours
  python -m trading.strategy --once        # single scan (cron-able)
  python -m trading.strategy --replay      # replay the last session bar-by-bar
  python -m trading.strategy --replay 2026-07-04
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from trading.config import (
    SCAN_UNIVERSE, YF_SUFFIX, FAST_EMA, SLOW_EMA, CANDLE_INTERVAL, SWING_LOOKBACK,
    RR_TARGET, RISK_PER_TRADE, POLL_SECONDS, SQUAREOFF_TIME, MARKET_OPEN,
    MARKET_CLOSE, MAX_POSITION_VALUE, CHARGES_PCT_ROUND_TRIP,
)
from trading.execution_router import ExecutionRouter
from trading.ledger import Ledger
from trading.notify import notify

IST = ZoneInfo("Asia/Kolkata")


# --- data & indicators ---

def fetch_candles(symbol: str, period: str = "5d") -> pd.DataFrame:
    """Closed 5-minute candles for an NSE symbol, IST-indexed."""
    df = yf.Ticker(symbol + YF_SUFFIX).history(period=period, interval=CANDLE_INTERVAL)
    if df.empty:
        return df
    df = df.tz_convert(IST)
    # Drop the still-forming candle so we only ever act on closed bars.
    now = datetime.now(IST)
    if df.index[-1] + timedelta(minutes=5) > now:
        df = df.iloc[:-1]
    return df


def fetch_all_candles(symbols: list[str], period: str = "5d") -> dict[str, pd.DataFrame]:
    """Batched candle download for the whole universe — one HTTP call for all
    symbols instead of one per symbol (matters at Nifty-50 scale)."""
    data = yf.download(
        tickers=[s + YF_SUFFIX for s in symbols],
        period=period, interval=CANDLE_INTERVAL,
        group_by="ticker", threads=True, progress=False, auto_adjust=True,
    )
    now = datetime.now(IST)
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            df = data[sym + YF_SUFFIX] if len(symbols) > 1 else data
        except KeyError:
            continue
        df = df.dropna(subset=["Close"])
        if df.empty:
            continue
        df = df.tz_convert(IST)
        if df.index[-1] + timedelta(minutes=5) > now:
            df = df.iloc[:-1]
        if not df.empty:
            out[sym] = df
    return out


def add_emas(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = df["Close"].ewm(span=FAST_EMA, adjust=False).mean()
    df["ema_slow"] = df["Close"].ewm(span=SLOW_EMA, adjust=False).mean()
    return df


def detect_cross(df: pd.DataFrame) -> str | None:
    """BUY/SELL if the fast EMA crossed the slow EMA on the last closed bar."""
    if len(df) < SLOW_EMA + 2:
        return None
    prev, last = df.iloc[-2], df.iloc[-1]
    if prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]:
        return "BUY"
    if prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]:
        return "SELL"
    return None


def swing_stop(df: pd.DataFrame, side: str) -> float:
    window = df.iloc[-SWING_LOOKBACK:]
    return float(window["Low"].min()) if side == "BUY" else float(window["High"].max())


# --- engine ---

class Engine:
    def __init__(self, router: ExecutionRouter, ledger: Ledger):
        self.router = router
        self.ledger = ledger
        self.prices: dict[str, float] = {}
        router.get_ltp = lambda sym: self.prices[sym]
        # open positions: symbol -> state (re-synced from ledger on start)
        self.open: dict[str, dict] = {}
        for t in ledger.open_trades():
            self.open[t["symbol"]] = {
                "trade_id": t["id"], "side": t["side"], "qty": t["qty"],
                "entry": t["entry_price"], "sl": t["stop_loss"],
                "target": t["target"], "mae": 0.0, "mfe": 0.0,
            }
        self.last_bar: dict[str, object] = {}

    def size(self, price: float, risk_per_share: float) -> int:
        if risk_per_share <= 0:
            return 0
        qty = int(RISK_PER_TRADE / risk_per_share)
        cap = MAX_POSITION_VALUE * self.router.day_config["risk_multiplier"]
        return min(qty, int(cap / price)) if price > 0 else 0

    def try_enter(self, symbol: str, df: pd.DataFrame, ts: float):
        if symbol in self.open:
            return
        side = detect_cross(df)
        if side is None:
            return
        price = float(df.iloc[-1]["Close"])
        sl = swing_stop(df, side)
        risk = (price - sl) if side == "BUY" else (sl - price)
        if risk <= 0:
            return
        target = price + RR_TARGET * risk if side == "BUY" else price - RR_TARGET * risk
        qty = self.size(price, risk)
        if qty < 1:
            return
        self.prices[symbol] = price
        signal = {"symbol": symbol, "side": side, "qty": qty, "price": price,
                  "ts": ts, "stop_loss": round(sl, 2), "target": round(target, 2),
                  "strategy_id": f"ema_{FAST_EMA}_{SLOW_EMA}_5m",
                  "regime": self.router.day_config.get("regime")}
        trade_id = self.router.execute(signal)
        if trade_id:
            self.open[symbol] = {"trade_id": trade_id, "side": side, "qty": qty,
                                 "entry": price, "sl": sl, "target": target,
                                 "mae": 0.0, "mfe": 0.0}
            print(f"ENTER {side} {symbol} x{qty} @ ~{price:.2f} sl={sl:.2f} tgt={target:.2f}")
            notify(f"📈 ENTRY {side} {symbol}",
                   f"×{qty} @ ~{price:.2f} · SL {sl:.2f} · TGT {target:.2f} (python engine)")
        else:
            reason = getattr(self.router, "last_rejection", None) or "risk kernel"
            print(f"rejected {side} {symbol} x{qty} ({reason})")
            # Routine cap/rate-limit rejections are expected many times per day
            # with a 49-symbol scan — log them but don't spam notifications.
            if not reason.startswith(("max_open_positions", "rate_limit")):
                notify(f"🚫 REJECTED {side} {symbol}", f"×{qty} @ {price:.2f} — {reason}")

    def manage_open(self, symbol: str, bar: pd.Series):
        pos = self.open.get(symbol)
        if pos is None:
            return
        lo, hi = float(bar["Low"]), float(bar["High"])
        direction = 1 if pos["side"] == "BUY" else -1
        pos["mae"] = min(pos["mae"], direction * ((lo if direction == 1 else hi) - pos["entry"]))
        pos["mfe"] = max(pos["mfe"], direction * ((hi if direction == 1 else lo) - pos["entry"]))

        exit_price = None
        if pos["side"] == "BUY":
            if lo <= pos["sl"]:
                exit_price = pos["sl"]          # conservative: stop fills first
            elif hi >= pos["target"]:
                exit_price = pos["target"]
        else:
            if hi >= pos["sl"]:
                exit_price = pos["sl"]
            elif lo <= pos["target"]:
                exit_price = pos["target"]
        if exit_price is not None:
            self._close(symbol, exit_price)

    def _close(self, symbol: str, exit_price: float):
        pos = self.open.pop(symbol)
        charges = pos["qty"] * (pos["entry"] + exit_price) * CHARGES_PCT_ROUND_TRIP
        pnl = self.ledger.record_exit(pos["trade_id"], round(exit_price, 2),
                                      charges=round(charges, 2),
                                      mae=round(pos["mae"], 2), mfe=round(pos["mfe"], 2))
        print(f"EXIT  {pos['side']} {symbol} @ {exit_price:.2f} -> pnl {pnl:.2f}")
        notify(f"{'✅' if pnl >= 0 else '🔻'} EXIT {pos['side']} {symbol}",
               f"@ {exit_price:.2f} → P&L {pnl:+.2f} INR · day {self.ledger.day_realized_pnl():+.2f}")

    def squareoff(self, note: str = "squareoff"):
        for symbol in list(self.open):
            price = self.prices.get(symbol, self.open[symbol]["entry"])
            print(f"{note}: closing {symbol}")
            self._close(symbol, price)

    # --- modes ---

    def scan_once(self):
        try:
            frames = fetch_all_candles(SCAN_UNIVERSE)
        except Exception as e:  # noqa: BLE001
            print(f"batch data error {e!r}")
            return
        bull = bear = 0
        for symbol, df in frames.items():
            df = add_emas(df)
            bar = df.iloc[-1]
            self.prices[symbol] = float(bar["Close"])
            if self.last_bar.get(symbol) == df.index[-1]:
                continue  # already acted on this bar
            self.last_bar[symbol] = df.index[-1]
            self.manage_open(symbol, bar)
            self.try_enter(symbol, df, ts=df.index[-1].timestamp())
            if bar["ema_fast"] > bar["ema_slow"]:
                bull += 1
            else:
                bear += 1
        open_desc = ", ".join(f"{s}({p['side']})" for s, p in self.open.items()) or "none"
        print(f"scanned {len(frames)}/{len(SCAN_UNIVERSE)} symbols · "
              f"{bull} bull / {bear} bear · open: {open_desc}")

    def live(self):
        print(f"live loop: {CANDLE_INTERVAL} candles, poll {POLL_SECONDS}s, "
              f"squareoff {SQUAREOFF_TIME} IST")
        while True:
            now = datetime.now(IST)
            hhmm = now.strftime("%H:%M")
            if hhmm >= SQUAREOFF_TIME:
                self.squareoff("EOD")
                if hhmm >= MARKET_CLOSE:
                    print("market closed — exiting")
                    return
            elif hhmm >= MARKET_OPEN:
                self.scan_once()
            else:
                print(f"{hhmm} IST — waiting for open")
            time.sleep(POLL_SECONDS)

    def replay(self, date: str | None = None):
        """Replay a session bar-by-bar through the same rules — a dry run on
        real market data that writes real (paper) trades to the ledger."""
        frames = {}
        for symbol, df in fetch_all_candles(SCAN_UNIVERSE, period="5d").items():
            if date:
                df = df[df.index.strftime("%Y-%m-%d") == date]
            else:
                last_day = df.index[-1].strftime("%Y-%m-%d")
                df = df[df.index.strftime("%Y-%m-%d") == last_day]
            if len(df) > SLOW_EMA + 2:
                frames[symbol] = df
        if not frames:
            print("no candle data to replay")
            return
        day = next(iter(frames.values())).index[0].strftime("%Y-%m-%d")
        print(f"replaying {day} for {', '.join(frames)}")
        n = max(len(df) for df in frames.values())
        for i in range(SLOW_EMA + 2, n):
            for symbol, df in frames.items():
                if i >= len(df):
                    continue
                window = add_emas(df.iloc[: i + 1])
                bar = window.iloc[-1]
                self.prices[symbol] = float(bar["Close"])
                self.manage_open(symbol, bar)
                self.try_enter(symbol, window, ts=window.index[-1].timestamp())
        self.squareoff("replay EOD")
        print(f"\nreplay done — day P&L: {self.ledger.day_realized_pnl():.2f} INR "
              f"({len(self.ledger.trades_for_date())} trades today in ledger)")


def main():
    ledger = Ledger()
    router = ExecutionRouter(mode="paper", ledger=ledger)
    engine = Engine(router, ledger)
    print(f"day config: {router.day_config}")
    args = sys.argv[1:]
    if "--replay" in args:
        date = next((a for a in args if a[0].isdigit()), None)
        engine.replay(date)
    elif "--once" in args:
        engine.scan_once()
    else:
        engine.live()


if __name__ == "__main__":
    main()
