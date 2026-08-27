"""Import a TradingView Strategy Tester 'List of trades' CSV into the ledger.

TradingView backtest/Replay fills never fire alerts, so they can't reach the
webhook. This importer brings them into the ledger (and therefore the
dashboard/journal) as closed paper trades.

Export from TradingView: Strategy Tester -> "List of trades" tab -> export icon
(top-right of the panel) -> CSV.

Usage:
  python -m trading.import_tv <file.csv> [--symbol RELIANCE] [--tag tv_backtest]

Notes:
  - Trades are tagged with their own strategy_id (default 'tv_backtest') so
    they never mix with your live paper stats.
  - TradingView reports profit before charges; we apply the same charges model
    as the rest of the system so numbers stay comparable.
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime

from trading.costs import round_trip as round_trip_charges
from trading.ledger import Ledger


def _col(row: dict, *needles: str) -> str | None:
    """Find a column whose header contains all needles (case-insensitive)."""
    for key in row:
        k = key.lower()
        if all(n in k for n in needles):
            return row[key]
    return None


def _num(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "").replace("−", "-"))
    except ValueError:
        return None


def _ts(v: str) -> float:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(v.strip(), fmt).timestamp()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date format: {v!r}")


def run(path: str, symbol: str = "UNKNOWN", tag: str = "tv_backtest") -> int:
    ledger = Ledger()
    trades: dict[str, dict] = {}

    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            n = _col(row, "trade #") or _col(row, "trade")
            typ = (_col(row, "type") or "").lower()
            if not n or not typ:
                continue
            t = trades.setdefault(n, {})
            part = {
                "price": _num(_col(row, "price")),
                "ts": _ts(_col(row, "date")) if _col(row, "date") else None,
                "qty": _num(_col(row, "contracts")) or _num(_col(row, "quantity")),
                "runup": _num(_col(row, "run-up", "inr") or _col(row, "run-up")),
                "drawdown": _num(_col(row, "drawdown", "inr") or _col(row, "drawdown")),
                "long": "long" in typ,
            }
            t["entry" if typ.startswith("entry") else "exit"] = part

    imported = 0
    for n, t in sorted(trades.items(), key=lambda kv: kv[0]):
        e, x = t.get("entry"), t.get("exit")
        if not e or not x or e["price"] is None or x["price"] is None:
            continue  # open or malformed trade
        qty = int(e["qty"] or x["qty"] or 1)
        side = "BUY" if e["long"] else "SELL"
        signal = {"symbol": symbol, "side": side, "qty": qty,
                  "price": e["price"], "ts": e["ts"],
                  "strategy_id": tag, "regime": None}
        trade_id = ledger.record_entry(signal, fill_price=e["price"], mode="paper")
        # backdate the ledger's date column to the entry timestamp
        if e["ts"]:
            with ledger._conn() as conn:  # noqa: SLF001 — importer is part of the package
                conn.execute("UPDATE trades SET date = ? WHERE id = ?",
                             (datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d"),
                              trade_id))
        charges = round_trip_charges(e["price"], x["price"], qty)
        mfe = (t["exit"]["runup"] / qty) if x.get("runup") else None
        mae = (-t["exit"]["drawdown"] / qty) if x.get("drawdown") else None
        ledger.record_exit(trade_id, x["price"], charges=round(charges, 2),
                           mae=mae, mfe=mfe)
        if x["ts"]:
            with ledger._conn() as conn:  # noqa: SLF001
                conn.execute("UPDATE trades SET exit_ts = ? WHERE id = ?",
                             (x["ts"], trade_id))
        imported += 1

    print(f"imported {imported} closed trades from {path} "
          f"(symbol={symbol}, strategy_id={tag})")
    return imported


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    args = sys.argv[1:]
    csv_path = args[0]
    sym = args[args.index("--symbol") + 1] if "--symbol" in args else "UNKNOWN"
    tag = args[args.index("--tag") + 1] if "--tag" in args else "tv_backtest"
    run(csv_path, sym, tag)
