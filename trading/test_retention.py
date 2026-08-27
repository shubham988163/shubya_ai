"""Correctness tests for the 5-session retention policy.

Pruning is irreversible, so the risk is not that it fails loudly — it is that
it deletes one session too many, or cuts rejections loose from the trades they
explain. Every case here is built on a throwaway database whose contents are
known by construction, and the real ledger is never touched.

Run with:

    .venv/bin/python -m trading.test_retention
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from trading import retention

IST = ZoneInfo("Asia/Kolkata")
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{': ' + detail if detail else ''}")
        print(f"  FAIL {name} {detail}")


SCHEMA = """
CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, date TEXT,
  symbol TEXT, status TEXT);
CREATE TABLE rejections (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, reason TEXT);
CREATE TABLE agent_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, agent TEXT);
"""


def build_db(path: Path, sessions: list[str]) -> dict[str, float]:
    """One trade, rejection and log row per session. Returns date -> timestamp."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    stamps = {}
    for i, day in enumerate(sessions):
        ts = datetime.fromisoformat(day + "T09:20:00+05:30").timestamp()
        stamps[day] = ts
        conn.execute("INSERT INTO trades (ts, date, symbol, status) VALUES (?,?,?,?)",
                     (ts, day, f"SYM{i}", "closed"))
        conn.execute("INSERT INTO rejections (ts, reason) VALUES (?,?)", (ts, "cap"))
        conn.execute("INSERT INTO agent_log (ts, agent) VALUES (?,?)", (ts, "supervisor"))
    conn.commit()
    conn.close()
    return stamps


def rows(path: Path, table: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def dates(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM trades ORDER BY date")]
    finally:
        conn.close()


SESSIONS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07",
            "2026-08-10", "2026-08-11", "2026-08-12"]


def with_temp_env(fn):
    """Run fn against a throwaway DB/reports/candles, restoring the module after."""
    def wrapper():
        tmp = Path(tempfile.mkdtemp())
        db, reports, candles = tmp / "l.db", tmp / "reports", tmp / "candles"
        reports.mkdir(); candles.mkdir()
        keep = (retention.DB_PATH, retention.REPORTS_DIR, retention.CANDLE_DIR)
        retention.DB_PATH, retention.REPORTS_DIR, retention.CANDLE_DIR = db, reports, candles
        try:
            fn(db, reports, candles)
        finally:
            (retention.DB_PATH, retention.REPORTS_DIR,
             retention.CANDLE_DIR) = keep
    return wrapper


@with_temp_env
def test_prune(db, reports, candles):
    print("ledger pruning")
    build_db(db, SESSIONS)
    for day in SESSIONS:
        (reports / f"{day}.md").write_text("x")

    p = retention.plan(5)
    check("the plan keeps exactly five sessions", len(p["kept_sessions"]) == 5,
          str(p["kept_sessions"]))
    check("the newest session is kept", p["kept_sessions"][-1] == "2026-08-12")
    check("the cutoff is the oldest session kept", p["cutoff_date"] == "2026-08-06",
          str(p["cutoff_date"]))
    check("the plan counts what would go", p["trades"] == 3, str(p["trades"]))

    check("a dry run deletes nothing",
          retention.prune(5, dry_run=True) and rows(db, "trades") == 8)

    retention.prune(5)
    check("only the five newest sessions survive",
          dates(db) == SESSIONS[3:], str(dates(db)))
    check("rejections older than the window go too", rows(db, "rejections") == 5,
          str(rows(db, "rejections")))
    check("so does the agent log", rows(db, "agent_log") == 5)
    check("reports follow the same window",
          sorted(p.name for p in reports.glob("*.md")) ==
          [f"{d}.md" for d in SESSIONS[3:]])

    check("pruning again is a no-op", retention.prune(5)["trades"] == 0
          and rows(db, "trades") == 5)


@with_temp_env
def test_edges(db, reports, candles):
    print("edges")
    build_db(db, SESSIONS[:3])
    p = retention.prune(5)
    check("fewer sessions than the window are left alone",
          rows(db, "trades") == 3 and p["cutoff_date"] is None)
    check("the summary says there was nothing to do",
          "nothing to prune" in retention.summary(p), retention.summary(p))

    # A session is a SESSION, not a calendar day — a long weekend must not
    # silently cost one.
    build_db(db2 := db.parent / "gap.db",
             ["2026-08-07", "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"])
    retention.DB_PATH = db2
    retention.prune(5)
    check("a weekend gap does not evict a session", rows(db2, "trades") == 5,
          str(dates(db2)))

    try:
        retention.plan(0)
        check("keep_days must be at least 1", False, "no error raised")
    except ValueError:
        check("keep_days must be at least 1", True)


@with_temp_env
def test_files(db, reports, candles):
    print("cached files")
    build_db(db, SESSIONS)
    old = candles / "OLD_5m.pkl"; old.write_text("x")
    fresh = candles / "NEW_5m.pkl"; fresh.write_text("x")
    stale = (datetime.now(IST) - timedelta(days=30)).timestamp()
    import os
    os.utime(old, (stale, stale))

    retention.prune(5)
    check("stale candle caches are removed", not old.exists())
    check("recent candle caches are kept", fresh.exists())


@with_temp_env
def test_no_db(db, reports, candles):
    print("missing ledger")
    p = retention.prune(5)
    check("a missing database is not an error", p["trades"] == 0)
    check("and the summary still reads sensibly", "retention:" in retention.summary(p))


def main() -> int:
    for fn in (test_prune, test_edges, test_files, test_no_db):
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
