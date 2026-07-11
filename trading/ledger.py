"""SQLite ledger — the single source of truth for trades, signals, and agent audit logs.

Tables:
  trades     — every paper/live trade with entry/exit, P&L, MAE/MFE, and the
               supervisor agent's verdict (written asynchronously, never blocking).
  rejections — signals the risk kernel or rate limiter refused.
  agent_log  — full prompt + response for every LLM call (the audit trail).
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from trading.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    date            TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty             INTEGER NOT NULL,
    entry_price     REAL NOT NULL,
    stop_loss       REAL,
    target          REAL,
    exit_price      REAL,
    exit_ts         REAL,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    pnl             REAL,
    charges         REAL,
    mae             REAL,
    mfe             REAL,
    strategy_id     TEXT,
    regime          TEXT,
    mode            TEXT NOT NULL DEFAULT 'paper',
    agent_verdict   TEXT,
    agent_confidence REAL,
    agent_reasons   TEXT,
    agent_reviewed_at REAL
);

CREATE TABLE IF NOT EXISTS rejections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    reason      TEXT NOT NULL,
    signal_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    agent       TEXT NOT NULL,
    model       TEXT,
    prompt      TEXT,
    response    TEXT,
    ok          INTEGER NOT NULL,
    error       TEXT
);
"""


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


class Ledger:
    def __init__(self, db_path=DB_PATH):
        self.db_path = str(db_path)
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- trades ---

    def record_entry(self, signal: dict, fill_price: float, mode: str = "paper") -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO trades
                   (ts, date, symbol, side, qty, entry_price, stop_loss, target,
                    strategy_id, regime, mode)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    signal.get("ts", time.time()),
                    _today(),
                    signal["symbol"],
                    signal["side"],
                    signal["qty"],
                    fill_price,
                    signal.get("stop_loss"),
                    signal.get("target"),
                    signal.get("strategy_id"),
                    signal.get("regime"),
                    mode,
                ),
            )
            return cur.lastrowid

    def record_exit(self, trade_id: int, exit_price: float, charges: float,
                    mae: float | None = None, mfe: float | None = None):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT side, qty, entry_price FROM trades WHERE id = ?", (trade_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"trade {trade_id} not found")
            direction = 1 if row["side"] == "BUY" else -1
            gross = direction * (exit_price - row["entry_price"]) * row["qty"]
            pnl = gross - charges
            conn.execute(
                """UPDATE trades SET exit_price = ?, exit_ts = ?, status = 'closed',
                   pnl = ?, charges = ?, mae = ?, mfe = ? WHERE id = ?""",
                (exit_price, time.time(), pnl, charges, mae, mfe, trade_id),
            )
            return pnl

    def record_rejection(self, signal: dict, reason: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO rejections (ts, reason, signal_json) VALUES (?, ?, ?)",
                (time.time(), reason, json.dumps(signal, default=str)),
            )

    def day_realized_pnl(self, date: str | None = None) -> float:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) AS pnl FROM trades "
                "WHERE date = ? AND status = 'closed'",
                (date or _today(),),
            ).fetchone()
            return row["pnl"]

    def open_position_value(self, symbol: str) -> float:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(qty * entry_price), 0) AS v FROM trades "
                "WHERE symbol = ? AND status = 'open'",
                (symbol,),
            ).fetchone()
            return row["v"]

    def trades_for_date(self, date: str | None = None) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE date = ? ORDER BY ts", (date or _today(),)
            ).fetchall()
            return [dict(r) for r in rows]

    def unreviewed_trades(self, limit: int = 10) -> list[dict]:
        """Trades the supervisor agent has not yet reviewed."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE agent_verdict IS NULL ORDER BY ts LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def save_agent_verdict(self, trade_id: int, verdict: str,
                           confidence: float, reasons: list[str]):
        with self._conn() as conn:
            conn.execute(
                """UPDATE trades SET agent_verdict = ?, agent_confidence = ?,
                   agent_reasons = ?, agent_reviewed_at = ? WHERE id = ?""",
                (verdict, confidence, json.dumps(reasons), time.time(), trade_id),
            )

    def open_trades(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status = 'open' ORDER BY ts").fetchall()
            return [dict(r) for r in rows]

    def recent_closed_trades(self, n: int = 5) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status = 'closed' ORDER BY exit_ts DESC LIMIT ?",
                (n,),
            ).fetchall()
            return [dict(r) for r in rows]

    # --- agent audit log ---

    def log_agent_call(self, agent: str, model: str, prompt: str,
                       response: str | None, ok: bool, error: str | None = None):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO agent_log (ts, agent, model, prompt, response, ok, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), agent, model, prompt, response, int(ok), error),
            )
