"""Local dashboard — see everything the system does in one page.

Zero dependencies (stdlib http.server). Reads the SQLite ledger and serves:
  stat tiles (net P&L, win rate, trades, charges, rejections)
  cumulative P&L line chart with crosshair + tooltip
  day config from the pre-market agent
  trades table with the supervisor's verdicts
  rejections table (risk kernel / rate limiter)
  LLM audit log
  latest EOD journal report

Usage:  python -m trading.dashboard [port]     # default 8787
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

import yfinance as yf

from trading import ui_theme
from trading.config import (DB_PATH, REPORTS_DIR, RETENTION_DAYS, TODAY_CONFIG_PATH)
from trading.costs import round_trip as round_trip_charges
from trading.execution_router import ExecutionRouter
from trading.fno import fyers, web as fno_web
from trading.ledger import Ledger

IST = ZoneInfo("Asia/Kolkata")


def _breakdown(trades: list[dict], key: str) -> list[dict]:
    """Per-group performance report (used for side and strategy breakdowns)."""
    groups: dict = {}
    for t in trades:
        k = t[key] or "—"
        g = groups.setdefault(k, {"name": k, "trades": 0, "closed": 0,
                                  "wins": 0, "net_pnl": 0.0, "charges": 0.0})
        g["trades"] += 1
        if t["status"] == "closed":
            g["closed"] += 1
            g["net_pnl"] += t["pnl"] or 0
            g["charges"] += t["charges"] or 0
            if (t["pnl"] or 0) > 0:
                g["wins"] += 1
    out = []
    for g in groups.values():
        g["win_rate"] = round(g["wins"] / g["closed"] * 100, 1) if g["closed"] else None
        g["net_pnl"] = round(g["net_pnl"], 2)
        g["charges"] = round(g["charges"], 2)
        out.append(g)
    return sorted(out, key=lambda g: g["name"])


def _get_ltp(symbol: str) -> float:
    try:
        from trading.fno.fyers import FyersClient
        client = FyersClient()
        fyers_sym = f"NSE:{symbol}-EQ" if not symbol.startswith("NSE:") else symbol
        q = client.quotes([fyers_sym])
        if q and len(q) > 0 and "v" in q[0] and "lp" in q[0]["v"]:
            return float(q[0]["v"]["lp"])
    except Exception:
        pass
    try:
        t = yf.Ticker(symbol + ".NS")
        hist = t.history(period="1d", interval="5m")
        if not hist.empty and "Close" in hist.columns:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return 1000.0


def _handle_trade(data: dict) -> dict:
    ledger = Ledger()
    router = ExecutionRouter(mode="paper", ledger=ledger)
    sym = str(data.get("symbol", "RELIANCE")).upper().strip()
    side = str(data.get("side", "BUY")).upper().strip()
    qty = int(data.get("qty", 10))
    price = float(data["price"]) if data.get("price") else None
    if price is None or price <= 0:
        price = _get_ltp(sym)
    router.get_ltp = lambda s: price

    sl = float(data["stop_loss"]) if data.get("stop_loss") else None
    tg = float(data["target"]) if data.get("target") else None
    if sl is None and price > 0:
        sl = round(price * 0.99, 2) if side == "BUY" else round(price * 1.01, 2)
    if tg is None and price > 0:
        tg = round(price * 1.02, 2) if side == "BUY" else round(price * 0.98, 2)

    signal = {
        "symbol": sym,
        "side": side,
        "qty": qty,
        "price": price,
        "ts": time.time(),
        "stop_loss": sl,
        "target": tg,
        "strategy_id": str(data.get("strategy_id", "manual_paper")),
        "regime": router.day_config.get("regime", "choppy"),
    }
    trade_id = router.execute(signal)
    if trade_id is None:
        reason = getattr(router, "last_rejection", "Risk kernel rejection")
        return {"ok": False, "rejected": True, "reason": reason}
    return {"ok": True, "trade_id": trade_id, "signal": signal}


def _handle_telegram_alert(data: dict) -> dict:
    """Send a Telegram notification for scanner alerts."""
    try:
        from trading.notify import notify
        title = data.get("title", "F&O Alert")
        message = data.get("message", "")
        notify(title, message)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_close(data: dict) -> dict:
    ledger = Ledger()
    trade_id = int(data.get("trade_id", 0))
    with ledger._conn() as conn:
        t = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if not t:
        return {"ok": False, "error": "Trade not found"}
    if t["status"] == "closed":
        return {"ok": False, "error": "Trade already closed"}

    exit_price = float(data["exit_price"]) if data.get("exit_price") else None
    if exit_price is None or exit_price <= 0:
        exit_price = _get_ltp(t["symbol"])

    charges = round_trip_charges(t["entry_price"], exit_price, t["qty"])
    pnl = ledger.record_exit(trade_id, round(exit_price, 2), charges=round(charges, 2))
    return {"ok": True, "trade_id": trade_id, "exit_price": round(exit_price, 2), "pnl": round(pnl, 2)}


def get_data(date: str | None) -> dict:
    ledger = Ledger()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    dates = [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM trades ORDER BY date DESC").fetchall()]
    # Today belongs in the picker (and is the default view) even before the
    # first trade of the day, so a live session never looks "missing".
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if today not in dates:
        dates.insert(0, today)
    if not date:
        date = today

    if date == "ALL":
        trades = [dict(r) for r in conn.execute(
            "SELECT * FROM trades ORDER BY ts DESC").fetchall()]
    else:
        trades = [dict(r) for r in conn.execute(
            "SELECT * FROM trades WHERE date = ? ORDER BY ts", (date,)).fetchall()] if date else []

    picker_dates = [today, "ALL"] + [d for d in dates if d != today and d != "ALL"]

    rejections = [dict(r) for r in conn.execute(
        "SELECT * FROM rejections ORDER BY ts DESC LIMIT 50").fetchall()]

    agent_log = [dict(r) for r in conn.execute(
        "SELECT id, ts, agent, model, ok, error, "
        "substr(COALESCE(response,''),1,400) AS response "
        "FROM agent_log ORDER BY ts DESC LIMIT 50").fetchall()]
    conn.close()

    open_trades = [t for t in trades if t["status"] == "open"]
    total_unrealized = 0.0
    total_est_charges = 0.0
    for t in open_trades:
        try:
            ltp = _get_ltp(t["symbol"])
            t["current_price"] = round(ltp, 2)
            side_mult = 1.0 if str(t["side"]).upper() == "BUY" else -1.0
            gross_pnl = (ltp - t["entry_price"]) * t["qty"] * side_mult
            est_chg = round_trip_charges(t["entry_price"], ltp, t["qty"])
            t["unrealized_pnl"] = round(gross_pnl - est_chg, 2)
            t["unrealized_gross_pnl"] = round(gross_pnl, 2)
            t["est_charges"] = round(est_chg, 2)
            total_unrealized += t["unrealized_pnl"]
            total_est_charges += est_chg
        except Exception:
            t["current_price"] = None
            t["unrealized_pnl"] = None
            t["est_charges"] = None

    closed = [t for t in trades if t["status"] == "closed"]
    wins = [t for t in closed if (t["pnl"] or 0) > 0]
    closed_pnl = sum(t["pnl"] or 0 for t in closed)
    closed_charges = sum(t["charges"] or 0 for t in closed)
    stats = {
        "net_pnl": round(closed_pnl + total_unrealized, 2),
        "realized_pnl": round(closed_pnl, 2),
        "unrealized_pnl": round(total_unrealized, 2),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "trades": len(trades),
        "open": len(open_trades),
        "charges": round(closed_charges + total_est_charges, 2),
        "rejections_today": sum(1 for r in rejections
                                if date and __import__("datetime").datetime
                                .fromtimestamp(r["ts"]).strftime("%Y-%m-%d") == date),
    }

    try:
        day_config = json.load(open(TODAY_CONFIG_PATH))
    except (OSError, ValueError):
        day_config = None

    report = None
    if date:
        p = REPORTS_DIR / f"{date}.md"
        if p.exists():
            report = p.read_text(encoding="utf-8")

    try:
        from trading.agents.llm import provider
        from trading.config import GEMINI_MODEL, ANTHROPIC_MODEL
        prov = provider()
        model = GEMINI_MODEL if prov == "gemini" else ANTHROPIC_MODEL
    except Exception:  # noqa: BLE001
        prov, model = "?", "?"

    challenge_data = None
    try:
        from trading.challenge import get_challenge_state
        challenge_data = get_challenge_state()
    except Exception:
        pass

    return {"date": date, "dates": picker_dates, "stats": stats, "trades": trades,
            "rejections": rejections, "agent_log": agent_log,
            "day_config": day_config, "report": report,
            "provider": prov, "model": model,
            "retention_days": RETENTION_DAYS,
            "side_report": _breakdown(trades, "side"),
            "strategy_report": _breakdown(trades, "strategy_id"),
            "day_pnl_all_time": ledger.day_realized_pnl(date) if date else 0,
            "challenge": challenge_data}


_PAGE_TEMPLATE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trading Agent Dashboard</title>
<style>
__THEME__
/* Aliases: the chart and a few older blocks were written against the previous
   token names. Mapping them here keeps that code untouched and still theme-aware. */
:root{
  --surface-1:var(--card); --page:var(--bg); --grid:var(--line); --axis:var(--line-2);
  --series-1:var(--accent); --border:var(--line); --warning:var(--warn);
  --critical:var(--bad); --delta-good:var(--up); --delta-bad:var(--down);
}
h1{font-size:13px}
.filters{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.card{background:var(--card);border:1px solid var(--line);border-radius:11px;
  padding:14px;box-shadow:var(--shadow)}

/* --- Buy now: the scanner's current call, first thing on the page --- */
#buynow .body{padding:14px}
.bn-hero{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.bn-sym{font-size:26px;font-weight:700;letter-spacing:-.01em}
.bn-px{font-size:18px;font-weight:600;font-variant-numeric:tabular-nums}
.bn-why{color:var(--ink-2);font-size:12.5px;margin-top:10px}
.bn-none{font-size:17px;font-weight:700;letter-spacing:.02em}
.bn-levels{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden;
  margin-top:12px}
.bn-levels div{background:var(--cell);padding:9px 12px}
.bn-levels .k{font-size:9.5px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.10em}
.bn-levels .v{font-size:16px;font-weight:650;margin-top:2px;font-variant-numeric:tabular-nums}
.bn-levels .v.sl{color:var(--bad)} .bn-levels .v.tg{color:var(--good)}
.bn-opt{margin-top:11px;border:1px solid var(--line);background:var(--cell);
  border-radius:9px;padding:9px 12px;font-size:12.5px;color:var(--ink-2);
  display:flex;gap:9px;flex-wrap:wrap;align-items:baseline}
.bn-opt .k{font-size:9.5px;letter-spacing:.10em;text-transform:uppercase;
  color:var(--ink-3)}
.bn-opt .strike{font-size:15px;font-weight:650;color:var(--ink);
  font-variant-numeric:tabular-nums}
.bn-opt b{color:var(--ink);font-weight:600}
.bn-opt .nocall{color:var(--bad);font-weight:700;letter-spacing:.06em}
.bn-opt .warn{color:var(--warn);width:100%;font-size:11.5px}
.bn-more{margin-top:11px;font-size:12px;color:var(--ink-2);display:flex;
  gap:8px 10px;flex-wrap:wrap;align-items:center}
.bn-chip{border:1px solid var(--line);background:var(--cell);border-radius:999px;
  padding:2px 9px;font-size:11.5px}
.bn-warn{color:var(--bad);font-size:13px}

/* --- chart --- */
#chartwrap{position:relative}
#tooltip{position:absolute;pointer-events:none;background:var(--card);
  border:1px solid var(--line-2);border-radius:8px;padding:8px 10px;font-size:12px;
  box-shadow:var(--shadow);display:none;min-width:140px;z-index:2}
#tooltip .v{font-weight:600;font-size:14px}
#tooltip .k{display:inline-block;width:12px;height:0;border-top:2px solid var(--accent);
  vertical-align:middle;margin-right:6px}
.cfg{display:flex;flex-wrap:wrap;gap:10px 16px;font-size:12.5px;color:var(--ink-2)}
.cfg b{color:var(--ink);font-weight:600}
pre.report{white-space:pre-wrap;font:12.5px/1.6 ui-sans-serif,system-ui,sans-serif;
  margin:0;color:var(--ink-2)}
.pnl-pos{color:var(--up)} .pnl-neg{color:var(--down)}
.small{font-size:11.5px;color:var(--ink-2)}
.overflow{overflow-x:auto}

/* --- trading desk composition --- */
.desk-kicker{display:flex;align-items:center;gap:8px;margin:18px 0 0;color:var(--ink-3);
  font-size:10px;font-weight:700;letter-spacing:.14em;text-transform:uppercase}
.desk-kicker::before{content:"";width:22px;height:1px;background:var(--accent)}
.market-grid{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(320px,1fr);gap:14px;margin-top:14px;align-items:start}
.market-grid #niftyWrap{display:grid;gap:14px;align-content:start}
.market-grid #buynow{margin-top:0}
.market-grid #niftyWrap .panel{margin-top:0}
.overview-grid{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(260px,.7fr);gap:14px;margin-top:14px}
.overview-grid .panel{margin-top:0}
.overview-grid .kpis{margin-top:0;grid-template-columns:repeat(2,minmax(0,1fr));align-content:start}
.overview-grid .kpi:first-child{grid-column:1 / -1;background:linear-gradient(135deg,var(--card),var(--accent-soft));border-color:color-mix(in srgb,var(--accent) 35%,var(--line))}
.overview-grid .kpi:first-child .v{font-size:32px}
.session-panel{height:100%}
.session-panel .body{height:calc(100% - 37px);display:flex;align-items:center}
.reports-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:14px}
.reports-grid .panel{margin-top:0}
.lower-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:14px}
.lower-grid .panel{margin-top:0}
@media (max-width:900px){
  .market-grid,.overview-grid,.reports-grid,.lower-grid{grid-template-columns:1fr}
  .overview-grid .kpis{grid-template-columns:repeat(4,minmax(120px,1fr))}
  .overview-grid .kpi:first-child{grid-column:auto}
}
@media (max-width:620px){
  .wrap{padding:0 10px 36px}
  .top{margin:0 -10px;padding:9px 10px;gap:9px}
  .nav{order:5;width:100%}.nav a{flex:1;text-align:center}
  .overview-grid .kpis{grid-template-columns:repeat(2,minmax(0,1fr))}
  .kpi .v.hero{font-size:30px}
  .market-grid{gap:10px}.reports-grid,.lower-grid{gap:10px}
}
</style></head>
<body>
<div class="wrap">
__TOPBAR__

  <section class="panel" id="liveTickerPanel" style="margin-top:14px">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 13px;background:var(--cell);border-bottom:1px solid var(--line)">
      <div style="display:flex;align-items:center;gap:8px;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase">
        <span class="led" style="background:var(--good);box-shadow:0 0 8px var(--good)"></span>
        <span style="color:var(--ink)">Live Market Rates <span style="font-weight:400;color:var(--ink-3)">(Fyers Zero-Delay Stream)</span></span>
      </div>
      <div style="font-size:11px;color:var(--ink-3);font-family:ui-monospace,monospace" id="liveTickerUpdated">Syncing live stream…</div>
    </div>
    <div id="liveTickerItems" style="display:flex;gap:10px;padding:10px 13px;overflow-x:auto;align-items:stretch">
      <span class="muted" style="font-size:11.5px">Loading live market quotes…</span>
    </div>
  </section>

  <!-- 15k to 1 Lakh Survival & Compounding Challenge Panel -->
  <section class="panel" id="challengePanel" style="margin-top:14px;border:1px solid color-mix(in srgb,var(--accent) 35%,var(--line));background:linear-gradient(180deg,color-mix(in srgb,var(--card) 92%,var(--accent-soft)),var(--card))">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:10px 14px;background:var(--cell);border-bottom:1px solid var(--line);flex-wrap:wrap;gap:8px">
      <div style="display:flex;align-items:center;gap:9px">
        <span style="font-size:16px">🎯</span>
        <span style="font-size:12px;font-weight:750;letter-spacing:.08em;text-transform:uppercase;color:var(--ink)">
          15K → 1 LAC Survival &amp; Compounding Challenge
        </span>
        <span id="chPhaseBadge" class="badge" style="font-size:10px;padding:2px 7px;background:var(--accent-soft);color:var(--accent);font-weight:700">PHASE 1</span>
        <span id="chHealthBadge" class="badge" style="font-size:10px;padding:2px 7px;background:var(--good-soft, rgba(0,200,100,0.1));color:var(--good);font-weight:700">100% SURVIVAL HEALTH</span>
      </div>
      <div style="display:flex;align-items:center;gap:10px;font-size:11.5px">
        <span class="muted" id="chTargetMeta">Goal: ₹1,00,000 INR (6.67x)</span>
        <button class="btn sm" id="btnRecalcChallenge" style="padding:2px 8px;font-size:10.5px">Refresh Stats</button>
        <button class="btn sm" id="btnResetChallenge" style="padding:2px 8px;font-size:10.5px;color:var(--bad)">Reset to ₹15k</button>
      </div>
    </div>
    
    <div class="body" style="padding:14px">
      <!-- Progress Bar with Checkpoints -->
      <div style="margin-bottom:14px">
        <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;font-size:12px">
          <div>
            <span style="font-weight:700;font-size:20px;color:var(--ink)" id="chEquity">₹15,000.00</span>
            <span id="chRoiMultiple" class="up" style="margin-left:8px;font-weight:600;font-size:12.5px">+0.0% · 1.00x</span>
          </div>
          <div style="text-align:right">
            <span style="color:var(--ink-2)" id="chDistance">₹85,000 to ₹1 Lakh Goal</span>
            <span style="font-weight:700;color:var(--accent);margin-left:6px" id="chPct">0.0%</span>
          </div>
        </div>
        <div style="position:relative;background:var(--line);border-radius:999px;height:12px;overflow:hidden;box-shadow:inset 0 1px 3px rgba(0,0,0,0.2)">
          <div id="chProgressBar" style="height:100%;width:0%;background:linear-gradient(90deg,var(--accent),#10b981);border-radius:999px;transition:width 0.6s ease"></div>
        </div>
        <div style="display:flex;justify-content:space-between;margin-top:6px;font-size:10px;color:var(--ink-3);font-family:ui-monospace,monospace">
          <span>🚩 ₹15K Start</span>
          <span>🛡️ ₹25K Cushion</span>
          <span>🚀 ₹50K Halfway</span>
          <span>💎 ₹75K Sprint</span>
          <span>🏆 ₹100K Rich Guy</span>
        </div>
      </div>

      <!-- Challenge Metrics Grid -->
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:12px">
        <div style="background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:8px 10px">
          <div class="k" style="font-size:9.5px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.08em">Risk Per Trade</div>
          <div id="chRiskTrade" style="font-size:15px;font-weight:700;margin-top:2px;color:var(--ink)">₹225.00</div>
          <div id="chRiskSub" style="font-size:10px;color:var(--ink-2);margin-top:2px">1.5% of equity (Dynamic)</div>
        </div>
        <div style="background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:8px 10px">
          <div class="k" style="font-size:9.5px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.08em">Daily Loss Limit</div>
          <div id="chDailyStop" style="font-size:15px;font-weight:700;margin-top:2px;color:var(--bad)">-₹525.00</div>
          <div id="chDailyStopSub" style="font-size:10px;color:var(--ink-2);margin-top:2px">3.5% hard stop cap</div>
        </div>
        <div style="background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:8px 10px">
          <div class="k" style="font-size:9.5px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.08em">Max Positions</div>
          <div id="chMaxPos" style="font-size:15px;font-weight:700;margin-top:2px;color:var(--ink)">2 concurrent</div>
          <div id="chPosCap" style="font-size:10px;color:var(--ink-2);margin-top:2px">Max pos: ₹45,000</div>
        </div>
        <div style="background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:8px 10px">
          <div class="k" style="font-size:9.5px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.08em">Drawdown &amp; Streak</div>
          <div id="chDrawdown" style="font-size:15px;font-weight:700;margin-top:2px;color:var(--good)">0.0% DD</div>
          <div id="chStreak" style="font-size:10px;color:var(--ink-2);margin-top:2px">0 losses today · Normal</div>
        </div>
      </div>

      <!-- AI Survival Agent Directive Callout -->
      <div id="chAiDirectiveBox" style="background:var(--cell);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:6px;padding:9px 12px;font-size:12px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:3px">
          <span style="font-weight:700;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--accent)">🤖 AI Survival Agent Directive</span>
          <span id="chDirectiveTime" style="font-size:10px;color:var(--ink-3);font-family:ui-monospace,monospace">Just now</span>
        </div>
        <div id="chDirectiveText" style="color:var(--ink);line-height:1.4">Loading survival directive…</div>
        <div id="chDirectiveSub" style="margin-top:4px;font-size:11px;color:var(--ink-2)"></div>
      </div>
    </div>
  </section>

  <div class="desk-kicker">Market command center <span class="muted">/ India cash &amp; F&amp;O</span></div>
  <div class="market-grid">
    <section class="panel" id="buynow">
      <h2>Primary setup <span class="sub" id="bnMeta">— checking the F&amp;O scanner…</span></h2>
      <div class="body" id="bnBody"></div>
    </section>
    <div id="niftyWrap"></div>
  </div>

  <div class="overview-grid">
    <div class="kpis" id="tiles"></div>
    <section class="panel session-panel">
      <h2>Session controls <span class="sub">risk &amp; state</span></h2>
      <div class="body"><div class="cfg" id="cfg"></div></div>
    </section>
  </div>

  <section class="panel" style="margin-top:14px">
    <h2>Equity curve <span class="sub">cumulative net P&amp;L · closed trades through selected session</span></h2>
    <div class="body" id="chartwrap">
      <svg id="chart" width="100%" height="240" role="img"
           aria-label="Cumulative net P&amp;L line chart; values also in the trades table below"></svg>
      <div id="tooltip"></div>
    </div>
  </section>

  <div class="reports-grid">
    <section class="panel">
      <h2>Buy vs Sell <span class="sub">selected session</span></h2>
      <div class="scroll"><table id="sides"></table></div>
    </section>
    <section class="panel">
      <h2>By strategy <span class="sub">selected session</span></h2>
      <div class="scroll"><table id="strats"></table></div>
    </section>
  </div>

  <section class="panel" style="margin-top:14px">
    <div style="display:flex;justify-content:space-between;align-items:center;padding:9px 13px;border-bottom:1px solid var(--line);background:var(--cell);flex-wrap:wrap;gap:8px">
      <h2 style="border-bottom:none;padding:0;background:none;margin:0">Trades <span class="sub">with the supervisor's verdict</span></h2>
      <div style="display:flex;gap:8px;align-items:center">
        <button class="btn" id="btnTriggerSupervisor" style="font-size:11.5px;padding:4px 10px">⚡ Review Pending Trades</button>
        <button class="btn go" id="btnNewTrade" style="font-size:11.5px;padding:4px 10px">+ Take Paper Trade</button>
      </div>
    </div>
    <div id="tradeForm" style="display:none;padding:12px;background:var(--card);border-bottom:1px solid var(--line);font-size:12px">
      <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
        <label><b>Symbol:</b> <input type="text" id="tSym" value="RELIANCE" style="width:100px;padding:5px 8px;border-radius:6px;border:1px solid var(--line);background:var(--cell);color:var(--ink);font-weight:600;text-transform:uppercase"></label>
        <label><b>Side:</b> <select id="tSide" style="padding:5px 8px;border-radius:6px;border:1px solid var(--line);background:var(--cell);color:var(--ink)"><option value="BUY">BUY (Long)</option><option value="SELL">SELL (Short)</option></select></label>
        <label><b>Qty:</b> <input type="number" id="tQty" value="10" min="1" style="width:65px;padding:5px 8px;border-radius:6px;border:1px solid var(--line);background:var(--cell);color:var(--ink)"></label>
        <label><b>Price (INR):</b> <input type="number" id="tPx" placeholder="Market (LTP)" step="0.05" style="width:110px;padding:5px 8px;border-radius:6px;border:1px solid var(--line);background:var(--cell);color:var(--ink)"></label>
        <button class="btn go" id="btnSubmitTrade" style="padding:5px 14px">Execute Order</button>
        <button class="btn" id="btnCancelTrade" style="padding:5px 10px">Cancel</button>
        <span id="tradeMsg" style="font-size:12px;font-weight:600"></span>
      </div>
    </div>
    <div class="scroll"><table id="trades"></table></div>
  </section>

  <div class="lower-grid">
    <section class="panel">
      <h2>Risk events <span class="sub">rejections · last 50</span></h2>
      <div class="scroll"><table id="rej"></table></div>
    </section>
    <section class="panel">
      <h2>Agent audit <span class="sub">LLM calls · last 50</span></h2>
      <div class="scroll"><table id="alog"></table></div>
    </section>
  </div>

  <section class="panel" style="margin-top:14px">
    <h2>EOD journal <span class="sub">written by the journal agent</span></h2>
    <div class="body"><pre class="report" id="report"></pre></div>
  </section>

  <div class="foot" id="foot"></div>
</div>

<script>
"use strict";
let DATA=null, selDate=null;

const fmt=(n,d=2)=> n==null ? "—" : Number(n).toLocaleString("en-IN",{minimumFractionDigits:d,maximumFractionDigits:d});
const t2time=ts=> new Date(ts*1000).toLocaleTimeString("en-IN",{hour12:false});
// "10/8/2026" reads as either 10 Aug or 8 Oct depending on the reader; spell the
// month so a timestamp in a log is never ambiguous.
const t2stamp=ts=> new Date(ts*1000).toLocaleString("en-IN",
  {day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});

function el(tag, cls, text){const e=document.createElement(tag); if(cls)e.className=cls;
  if(text!=null)e.textContent=text; return e;}

// The scanner runs its own background scan; this only reads the cached result,
// so polling it is cheap and never blocks this page.
async function loadFno(){
  try{
    const r = await fetch("/api/fno", {cache:"no-store"});
    const st = await r.json();
    renderBuyNow(st);
    renderNifty(st);
  }catch(e){
    document.getElementById("bnMeta").textContent = "— scanner unreachable";
    document.getElementById("bnBody").replaceChildren(
      el("div","bn-warn","Could not reach the scanner API: "+e));
  }
}

__NIFTYJS__
__CONTEXTJS__

function renderNifty(st){
  const wrap = document.getElementById("niftyWrap");
  wrap.replaceChildren();
  const ix = st && st.scan ? st.scan.index_options : null;
  if(!ix && !st.scan.chain && !st.scan.pivots) return;
  if(!ix){
    const only = contextPanel(st.scan.chain, st.scan.pivots, v => fmt(v));
    if(only) wrap.append(only);
    return;
  }
  wrap.append(niftyPanel(ix, v => fmt(v)));
  const ctx = contextPanel(st.scan.chain, st.scan.pivots, v => fmt(v));
  if(ctx) wrap.append(ctx);
}

function renderBuyNow(st){
  const meta=document.getElementById("bnMeta"), box=document.getElementById("bnBody");
  box.replaceChildren();
  const s=st.scan;
  const age = st.age_sec==null ? "" :
    " · updated "+(st.age_sec<60 ? st.age_sec+"s" : Math.round(st.age_sec/60)+"m")+" ago";

  if(!s){
    meta.textContent = st.scanning ? "— scanning the F&O universe…"
      : st.error ? "— scan failed" : "— no scan yet";
    box.append(el("div","muted", st.error || "The first scan is running; this fills in shortly."));
    return;
  }
  meta.textContent = "— " + s.window + (st.scanning ? " · rescanning" : "") + age;

  // Data integrity gates the call, exactly as it does in the scanner itself.
  if(!s.data_ok){
    box.append(el("div","bn-warn", s.stale_banner));
    box.append(el("div","muted","No entry, stop or target is produced. "
      + "Open the scanner for the data notes."));
    const l1=el("div","bn-more"); l1.append(link()); box.append(l1);
    return;
  }

  const picks = s.picks || [];
  if(window.ScannerAlerts && picks.length){
    picks.forEach(p => window.ScannerAlerts.checkAndAlert(p, "BUY"));
  }
  if(!picks.length){
    const watch=(s.candidates||[]).filter(c=>c.verdict==="WATCH").length;
    if(s.signals_allowed === false){
      // Not "nothing qualifies" — the scanner is not permitted to answer yet.
      box.append(el("div","bn-none","TOO EARLY TO SAY"));
      box.append(el("div","muted", s.window_note
        || "this window does not emit signals"));
      box.append(el("div","muted",
        "The 09:15\u201309:30 range has to finish before any breakout can be "
        + "graded, so the first signals are possible from 09:30."));
    } else {
    box.append(el("div","bn-none", s.no_trade || "NO HIGH-QUALITY LONG SETUP"));
    box.append(el("div","muted", "Nothing clears the rules right now"
      + (watch ? " — "+watch+" name"+(watch>1?"s":"")+" on watch." : ".")
      + " Not forcing a trade is the correct output most mornings."));
    }
    const l2=el("div","bn-more"); l2.append(link()); box.append(l2);
    return;
  }

  const c=picks[0], t=c.trade;
  const hero=el("div","bn-hero");
  hero.append(el("span","bn-sym", c.symbol));
  const px=el("span","bn-px", "₹"+fmt(c.price));
  hero.append(px);
  if(c.pct_change!=null){
    const ch=el("span","bn-px "+(c.pct_change>=0?"up":"down"),
      (c.pct_change>=0?"+":"")+c.pct_change.toFixed(2)+"%");
    hero.append(ch);
  }
  hero.append(el("span","badge buy","BUY · "+c.grade+" · "+
    (c.score%1?c.score.toFixed(1):c.score.toFixed(0))+"/100"));
  hero.append(el("span","muted", c.sector+" · "+c.contract));
  box.append(hero);

  if(t){
    const g=el("div","bn-levels");
    const cell=(k,v,cls,note)=>{const d=el("div"); d.append(el("div","k",k));
      d.append(el("div","v "+(cls||""),v)); if(note) d.append(el("div","k",note));
      g.append(d);};
    cell("Entry zone","₹"+fmt(t.entry_low)+" – "+fmt(t.entry_high),"","buy inside this band");
    cell("Stop loss","₹"+fmt(t.stop),"sl",t.stop_basis);
    const t1Note = t.target1_hit ? "✓ ACHIEVED" : "1:"+t.rr1;
    const t2Note = t.target2_hit ? "✓ ACHIEVED" : "1:"+t.rr2;
    cell("Target 1","₹"+fmt(t.target1),"tg",t1Note);
    cell("Target 2","₹"+fmt(t.target2),"tg",t2Note);
    cell("Risk","₹"+fmt(t.risk),"",t.risk_pct.toFixed(2)+"% of price");
    box.append(g);
  }
  // How the same long could be taken in calls — breakeven first, because a
  // call can lose while the stock setup works exactly as planned.
  if(c.options){
    const o=c.options, row=el("div","bn-opt");
    row.append(el("span","k","Call option"));
    if(o.quote){
      const q=o.quote;
      row.append(el("span","strike", q.strike+" CE @ "+fmt(q.ltp)));
      row.append(el("span",null,q.expiry+" · "+q.moneyness));
      const be=el("span"); be.append(document.createTextNode("breakeven "));
      be.append(el("b",null,fmt(o.breakeven)));
      be.append(document.createTextNode(o.clears_t1 ? " — under target 1, so T1 pays"
        : o.clears_t2 ? " — above target 1; only the 1:3 target pays"
        : " — above both targets"));
      row.append(be);
      (o.warnings||[]).forEach(w=>row.append(el("span","warn","⚠ "+w)));
    } else {
      row.append(el("span","nocall","NO CALL"));
      row.append(el("span",null,(o.rejections||["no candidate strike"])[0]));
    }
    box.append(row);
  }

  if(c.reasons && c.reasons.length)
    box.append(el("div","bn-why","Why: "+c.reasons.slice(0,2).join(" · ")));

  const more=el("div","bn-more");
  picks.slice(1).forEach(o=>more.append(el("span","bn-chip",
    o.symbol+"  ₹"+fmt(o.price)+"  "+o.grade)));
  const watch=(s.candidates||[]).filter(x=>x.verdict==="WATCH");
  watch.slice(0,3).forEach(o=>more.append(el("span","bn-chip",
    "watch: "+o.symbol+" "+(o.score%1?o.score.toFixed(1):o.score.toFixed(0)))));
  more.append(link());
  box.append(more);
}

function link(){
  const a=el("a",null,"open the scanner →");
  a.href="/fno";
  return a;
}

async function load(){
  try{
    const q = selDate ? "?date="+encodeURIComponent(selDate) : "";
    const r = await fetch("/api/data"+q, {cache:"no-store"});
    if(!r.ok) return;
    DATA = await r.json();
    selDate = DATA.date;
    render();
    document.getElementById("refreshed").textContent =
      "updated " + new Date().toLocaleTimeString("en-IN",{hour12:false});
    document.getElementById("foot").textContent =
      "Paper trading — no live orders. This dashboard keeps only the last "
      + (DATA.retention_days || 5) + " trading sessions on disk; older trades, "
      + "rejections, agent logs, journal reports and cached candles are pruned "
      + "automatically when the dashboard starts.";
  }catch(e){
    console.warn("Could not reach /api/data:", e);
  }
}

function render(){
  const d=DATA;
  document.getElementById("win").textContent =
    `paper mode · LLM: ${d.provider} (${d.model}) · ${d.date||"no data"}`;

  // date filter
  const sel=document.getElementById("dateSel"); sel.replaceChildren();
  (d.dates.length?d.dates:[d.date||"no data"]).forEach(dt=>{
    const label = dt === "ALL" ? "All Sessions (Full History)" : dt;
    const o=el("option",null,label); o.value=dt; if(dt===d.date)o.selected=true; sel.append(o);
  });
  sel.onchange=()=>{selDate=sel.value; load();};

  renderChallenge(d.challenge);
  renderTiles(d); renderChart(d); renderBreakdown(d); renderCfg(d);
  renderTrades(d); renderRej(d); renderAlog(d);
  const rep=document.getElementById("report");
  rep.textContent = d.report || "No journal report for this date yet — run: python -m trading.agents.eod_journal";
}

function renderChallenge(ch){
  const p = document.getElementById("challengePanel");
  if(!ch){ if(p) p.style.display="none"; return; }
  p.style.display = "";

  document.getElementById("chPhaseBadge").textContent = (ch.phase || "Phase 1").toUpperCase();
  
  const hBadge = document.getElementById("chHealthBadge");
  const health = ch.survival_health ?? 100;
  hBadge.textContent = `${health.toFixed(1)}% SURVIVAL HEALTH · ${ch.survival_status}`;
  if(health >= 80){
    hBadge.style.background = "var(--good-soft, rgba(16,185,129,0.15))";
    hBadge.style.color = "var(--good)";
  } else if(health >= 50){
    hBadge.style.background = "var(--warn-soft, rgba(245,158,11,0.15))";
    hBadge.style.color = "var(--warn)";
  } else {
    hBadge.style.background = "var(--bad-soft, rgba(239,68,68,0.15))";
    hBadge.style.color = "var(--bad)";
  }

  const eq = ch.current_equity ?? 15000;
  document.getElementById("chEquity").textContent = "₹" + fmt(eq);
  
  const roiElem = document.getElementById("chRoiMultiple");
  const roi = ch.roi_pct ?? 0;
  const mult = ch.multiple ?? 1;
  const roiSign = roi >= 0 ? "+" : "";
  roiElem.textContent = `${roiSign}${roi.toFixed(2)}% · ${mult.toFixed(2)}x Multiple`;
  roiElem.className = roi >= 0 ? "up" : "down";

  document.getElementById("chDistance").textContent = `₹${fmt(ch.distance_to_target)} to ₹1 Lakh Goal`;
  const pct = ch.progress_pct ?? 0;
  document.getElementById("chPct").textContent = `${pct.toFixed(1)}%`;
  document.getElementById("chProgressBar").style.width = `${Math.min(100, Math.max(0, pct))}%`;

  document.getElementById("chRiskTrade").textContent = `₹${fmt(ch.risk_per_trade)}`;
  document.getElementById("chRiskSub").textContent = `${ch.risk_pct}% of equity (${ch.phase_num === 1 ? 'Shielded' : 'Compounding'})`;

  document.getElementById("chDailyStop").textContent = `-₹${fmt(Math.abs(ch.daily_loss_limit))}`;
  document.getElementById("chDailyStopSub").textContent = `${ch.phase_num === 1 ? '3.5%' : '4.0%'} hard stop cap`;

  document.getElementById("chMaxPos").textContent = `${ch.max_open_positions} concurrent`;
  document.getElementById("chPosCap").textContent = `Max pos value: ₹${fmt(ch.max_position_value, 0)}`;

  const dd = ch.drawdown_pct ?? 0;
  const ddElem = document.getElementById("chDrawdown");
  ddElem.textContent = `${dd.toFixed(1)}% DD`;
  ddElem.className = dd > 8 ? "down" : dd > 4 ? "warn" : "up";

  const stElem = document.getElementById("chStreak");
  if(ch.circuit_breaker_active){
    stElem.textContent = `🛑 CIRCUIT BREAKER: ${ch.circuit_breaker_reason || 'Halted'}`;
    stElem.style.color = "var(--bad)";
  } else if(ch.defense_mode){
    stElem.textContent = `🛡️ DEFENSE ACTIVE: Risk reduced 50%`;
    stElem.style.color = "var(--warn)";
  } else {
    stElem.textContent = `${ch.today_losses_count || 0} losses today · Normal Operations`;
    stElem.style.color = "var(--ink-2)";
  }

  // AI Directive
  document.getElementById("chDirectiveText").textContent = ch.ai_directive || "Survival Agent active: maintaining capital discipline.";
  document.getElementById("chDirectiveTime").textContent = ch.ai_directive_time ? `Updated ${ch.ai_directive_time.split(' ')[1]}` : "Active";
  
  const detail = ch.ai_directive_detail;
  if(detail && detail.key_focus){
    document.getElementById("chDirectiveSub").textContent = `Key Focus: ${detail.key_focus} · Action: ${detail.risk_action}`;
  } else {
    document.getElementById("chDirectiveSub").textContent = "";
  }
}

function renderTiles(d){
  const s=d.stats, box=document.getElementById("tiles"); box.replaceChildren();
  const mk=(label,value,opts={})=>{
    const c=el("div","kpi");
    c.append(el("div","k",label));
    const v=el("div","v mono"+(opts.hero?" hero":""),value);
    if(opts.dir) v.classList.add(opts.dir);
    c.append(v);
    if(opts.delta) c.append(el("div","f",opts.delta));
    box.append(c);
  };
  const pnl=s.net_pnl??0;
  let pnlDelta = (pnl>=0?"▲":"▼")+" vs start of day";
  if(s.open > 0 && s.unrealized_pnl != null){
    const uSign = s.unrealized_pnl >= 0 ? "+" : "";
    pnlDelta = `Realized: ₹${fmt(s.realized_pnl)} · Live MTM: ${uSign}₹${fmt(s.unrealized_pnl)}`;
  }
  mk("Net P&L (INR)", fmt(pnl), {hero:true, dir:pnl>=0?"up":"down", delta: pnlDelta});
  mk("Win rate", s.win_rate==null?"—":fmt(s.win_rate,1)+"%");
  mk("Trades", String(s.trades)+(s.open?` (${s.open} open)`:""));
  mk("Charges (INR)", fmt(s.charges));
  mk("Rejections today", String(s.rejections_today));
}

function renderCfg(d){
  const c=document.getElementById("cfg"); c.replaceChildren();
  if(!d.day_config){c.append(el("span","muted","No today_config.json — pre-market agent hasn't run.")); return;}
  const cfg=d.day_config;
  const add=(k,v)=>{const s=el("span"); const b=el("b",null,k+": "); s.append(b,String(v)); c.append(s);};
  add("date",cfg.date||"—"); add("regime",cfg.regime);
  add("risk multiplier",cfg.risk_multiplier);
  add("blocked",(cfg.blocked_symbols&&cfg.blocked_symbols.length)?cfg.blocked_symbols.join(", "):"none");
  const r=el("span","muted"); r.textContent="“"+(cfg.rationale||"")+"”"; c.append(r);
}

const VGLY={approve:"\u2713", caution:"\u25D4", veto:"\u2715", ok:"\u2713", err:"\u2715"};
function verdictBadge(v){
  if(!v) return el("span","muted","pending");
  const b=el("span","badge sm "+v);
  b.append(el("span","g", VGLY[v]||""));
  b.append(document.createTextNode(v));
  return b;
}

function renderTrades(d){
  const t=document.getElementById("trades"); t.replaceChildren();
  if(!d.trades.length){t.replaceWith(t); t.append(el("caption","empty","No trades for this date."));return;}
  const head=el("tr");
  ["#","time","symbol","side","qty","entry","exit","P&L","charges","strategy","verdict","reasons"]
    .forEach((h,i)=>{const th=el("th",[4,5,6,7,8].includes(i)?"num":null,h); head.append(th);});
  t.append(head);
  d.trades.forEach(tr=>{
    const row=el("tr");
    row.append(el("td","mono",String(tr.id)));
    row.append(el("td","mono",t2time(tr.ts)));
    row.append(el("td",null,tr.symbol));
    row.append(el("td",null,tr.side));
    row.append(el("td","num",String(tr.qty)));
    row.append(el("td","num",fmt(tr.entry_price)));
    const isClosed = tr.status === "closed" && tr.exit_price != null;
    const exitTd = el("td","num");
    if(isClosed){
      exitTd.textContent = fmt(tr.exit_price);
    } else {
      const b = el("span","badge buy","OPEN");
      b.style.marginRight = "5px";
      const ltpSpan = el("span", "mono", tr.current_price != null ? `₹${fmt(tr.current_price)} ` : "");
      ltpSpan.style.fontSize = "11px";
      ltpSpan.style.marginRight = "5px";
      const cBtn = el("button","btn sm","Close");
      cBtn.style.padding = "2px 6px";
      cBtn.style.fontSize = "10.5px";
      cBtn.onclick = async ()=>{
        cBtn.disabled = true;
        cBtn.textContent = "Closing…";
        try{
          const res = await fetch("/api/close", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({trade_id: tr.id})
          });
          const j = await res.json();
          if(j.ok){ load(); } else { alert(j.error || "Failed to close trade"); cBtn.disabled=false; cBtn.textContent="Close"; }
        }catch(e){ alert("Error: "+e); cBtn.disabled=false; cBtn.textContent="Close"; }
      };
      exitTd.append(b, ltpSpan, cBtn);
    }
    row.append(exitTd);

    let pnlVal = isClosed ? tr.pnl : tr.unrealized_pnl;
    const pnlTd = el("td","num "+((pnlVal??0)>=0?"pnl-pos":"pnl-neg"));
    if(isClosed){
      pnlTd.textContent = pnlVal==null ? "—" : fmt(pnlVal);
    } else {
      if(pnlVal != null){
        const sign = pnlVal >= 0 ? "+" : "";
        pnlTd.innerHTML = `<b>${sign}${fmt(pnlVal)}</b> <span style="font-size:9.5px;padding:1px 4px;border-radius:3px;font-weight:600;background:${pnlVal>=0?'rgba(16,185,129,0.2)':'rgba(239,68,68,0.2)'};color:${pnlVal>=0?'#10b981':'#ef4444'}">LIVE</span>`;
      } else {
        pnlTd.textContent = "—";
      }
    }
    row.append(pnlTd);

    const chgVal = isClosed ? tr.charges : tr.est_charges;
    const chgTd = el("td", "num", chgVal==null ? "—" : (isClosed ? fmt(chgVal) : `~${fmt(chgVal)}`));
    row.append(chgTd);
    row.append(el("td","small",tr.strategy_id||"—"));
    const vtd=el("td"); vtd.append(verdictBadge(tr.agent_verdict));
    if(tr.agent_confidence!=null) vtd.append(el("span","small"," "+Number(tr.agent_confidence).toFixed(2)));
    row.append(vtd);
    let reasons="—";
    try{const rr=JSON.parse(tr.agent_reasons||"[]"); if(rr.length)reasons=rr.join("; ");}catch(e){}
    const rtd=el("td","small",reasons); rtd.style.maxWidth="320px"; row.append(rtd);
    t.append(row);
  });
}

function renderRej(d){
  const t=document.getElementById("rej"); t.replaceChildren();
  if(!d.rejections.length){t.append(el("caption","empty","No rejections logged."));return;}
  const head=el("tr");
  ["time","symbol","side","qty","reason"].forEach(h=>head.append(el("th",null,h)));
  t.append(head);
  d.rejections.forEach(r=>{
    let sig={}; try{sig=JSON.parse(r.signal_json);}catch(e){}
    const row=el("tr");
    row.append(el("td","mono",t2stamp(r.ts)));
    row.append(el("td",null,sig.symbol||"—"));
    row.append(el("td",null,sig.side||"—"));
    row.append(el("td","num",sig.qty!=null?String(sig.qty):"—"));
    row.append(el("td","small",r.reason));
    t.append(row);
  });
}

function renderAlog(d){
  const t=document.getElementById("alog"); t.replaceChildren();
  if(!d.agent_log.length){t.append(el("caption","empty","No LLM calls logged yet."));return;}
  const head=el("tr");
  ["time","agent","model","status","response / error"].forEach(h=>head.append(el("th",null,h)));
  t.append(head);
  d.agent_log.forEach(a=>{
    const row=el("tr");
    row.append(el("td","mono",t2stamp(a.ts)));
    row.append(el("td",null,a.agent));
    row.append(el("td","small",a.model||"—"));
    const st=el("td"); st.append(el("span","badge "+(a.ok?"ok":"err"), a.ok?"ok":"failed")); row.append(st);
    const msg=a.ok ? (a.response||"") : (a.error||"");
    const m=el("td","small", msg.length>200 ? msg.slice(0,200)+"…" : msg);
    m.style.maxWidth="420px"; row.append(m);
    t.append(row);
  });
}

function renderBreakdown(d){
  const fill=(tableId, rows, label)=>{
    const t=document.getElementById(tableId); t.replaceChildren();
    if(!rows || !rows.length){t.append(el("caption","empty","No trades for this date."));return;}
    const head=el("tr");
    [label,"trades","closed","win rate","charges","net P&L"].forEach((h,i)=>
      head.append(el("th", i>0?"num":null, h)));
    t.append(head);
    rows.forEach(g=>{
      const row=el("tr");
      const name=el("td",null, g.name==="BUY" ? "BUY (long)" : g.name==="SELL" ? "SELL (short)" : g.name);
      row.append(name);
      row.append(el("td","num",String(g.trades)));
      row.append(el("td","num",String(g.closed)));
      row.append(el("td","num", g.win_rate==null?"—":fmt(g.win_rate,1)+"%"));
      row.append(el("td","num",fmt(g.charges)));
      row.append(el("td","num "+(g.net_pnl>=0?"pnl-pos":"pnl-neg"), fmt(g.net_pnl)));
      t.append(row);
    });
  };
  fill("sides", d.side_report, "side");
  fill("strats", d.strategy_report, "strategy");
}

// --- cumulative P&L line chart (SVG) ---
function renderChart(d){
  const svg=document.getElementById("chart");
  svg.replaceChildren();
  const closed=d.trades.filter(t=>t.status==="closed" && t.exit_ts!=null)
                       .sort((a,b)=>a.exit_ts-b.exit_ts);
  const W=svg.clientWidth||1040, H=240, M={t:16,r:20,b:26,l:56};
  svg.setAttribute("viewBox",`0 0 ${W} ${H}`);
  const css=getComputedStyle(document.documentElement);
  const C={line:css.getPropertyValue("--series-1").trim(),
           grid:css.getPropertyValue("--grid").trim(),
           axis:css.getPropertyValue("--axis").trim(),
           muted:css.getPropertyValue("--muted").trim(),
           surface:css.getPropertyValue("--surface-1").trim(),
           ink:css.getPropertyValue("--ink").trim()};
  const ns="http://www.w3.org/2000/svg";
  const mk=(tag,attrs)=>{const e=document.createElementNS(ns,tag);
    for(const k in attrs)e.setAttribute(k,attrs[k]); return e;};

  if(!closed.length){
    svg.style.display="none";
    let note=document.getElementById("chartEmpty");
    if(!note){ note=el("div","empty","No closed trades in this session yet.");
      note.id="chartEmpty"; svg.parentNode.append(note); }
    note.style.display="";
    return;
  }
  svg.style.display="";
  const note=document.getElementById("chartEmpty");
  if(note) note.style.display="none";

  let cum=0;
  const pts=[{x:closed[0].ts, y:0, label:"start"}];
  closed.forEach(t=>{cum+=t.pnl||0; pts.push({x:t.exit_ts,y:cum,trade:t,cum});});

  const xs=pts.map(p=>p.x), ys=pts.map(p=>p.y);
  const xmin=Math.min(...xs), xmax=Math.max(...xs)||xmin+1;
  let ymin=Math.min(0,...ys), ymax=Math.max(0,...ys);
  if(ymin===ymax){ymax+=1;}
  const pad=(ymax-ymin)*0.1; ymin-=pad; ymax+=pad;
  const X=v=> M.l+(v-xmin)/(xmax-xmin||1)*(W-M.l-M.r);
  const Y=v=> H-M.b-(v-ymin)/(ymax-ymin)*(H-M.t-M.b);

  // gridlines + clean y ticks
  const span=ymax-ymin, rawStep=span/4,
        mag=Math.pow(10,Math.floor(Math.log10(rawStep))),
        step=[1,2,5,10].map(m=>m*mag).find(s=>s>=rawStep)||mag*10;
  for(let v=Math.ceil(ymin/step)*step; v<=ymax; v+=step){
    svg.append(mk("line",{x1:M.l,x2:W-M.r,y1:Y(v),y2:Y(v),stroke:C.grid,"stroke-width":1}));
    const t=mk("text",{x:M.l-8,y:Y(v)+4,"text-anchor":"end",fill:C.muted,
      "font-size":"11","font-variant-numeric":"tabular-nums"});
    t.textContent=(Math.round(v)||0).toLocaleString("en-IN"); svg.append(t);
  }
  // zero baseline emphasized
  if(ymin<0&&ymax>0)
    svg.append(mk("line",{x1:M.l,x2:W-M.r,y1:Y(0),y2:Y(0),stroke:C.axis,"stroke-width":1}));
  // x baseline
  svg.append(mk("line",{x1:M.l,x2:W-M.r,y1:H-M.b,y2:H-M.b,stroke:C.axis,"stroke-width":1}));
  // x labels: first + last time
  [[pts[0],"start"],[pts[pts.length-1],"end"]].forEach(([p])=>{
    const t=mk("text",{x:X(p.x),y:H-8,"text-anchor":"middle",fill:C.muted,"font-size":"11"});
    t.textContent=t2time(p.x); svg.append(t);
  });

  const path=pts.map((p,i)=>(i?"L":"M")+X(p.x).toFixed(1)+","+Y(p.y).toFixed(1)).join(" ");
  // area wash ~10%
  svg.append(mk("path",{d:path+` L${X(xmax).toFixed(1)},${Y(Math.max(ymin,0)).toFixed(1)} L${X(xmin).toFixed(1)},${Y(Math.max(ymin,0)).toFixed(1)} Z`,
    fill:C.line,"fill-opacity":"0.1",stroke:"none"}));
  svg.append(mk("path",{d:path,fill:"none",stroke:C.line,"stroke-width":2,
    "stroke-linejoin":"round","stroke-linecap":"round"}));

  // end marker: ≥8px dot with 2px surface ring + end label (value at the end)
  const last=pts[pts.length-1];
  svg.append(mk("circle",{cx:X(last.x),cy:Y(last.y),r:6,fill:C.line,
    stroke:C.surface,"stroke-width":2}));
  const lbl=mk("text",{x:Math.min(X(last.x)+10,W-M.r),y:Y(last.y)+4,fill:C.ink,
    "font-size":"12","font-weight":"600"});
  lbl.textContent=fmt(last.y);
  if(X(last.x)+70>W-M.r){lbl.setAttribute("x",X(last.x)-10);lbl.setAttribute("text-anchor","end");}
  svg.append(lbl);

  // crosshair + tooltip (snap to nearest point)
  const cross=mk("line",{y1:M.t,y2:H-M.b,stroke:C.axis,"stroke-width":1,visibility:"hidden"});
  const dot=mk("circle",{r:5,fill:C.line,stroke:C.surface,"stroke-width":2,visibility:"hidden"});
  svg.append(cross,dot);
  const tip=document.getElementById("tooltip"), wrap=document.getElementById("chartwrap");
  const hover=mk("rect",{x:M.l,y:M.t,width:W-M.l-M.r,height:H-M.t-M.b,fill:"transparent"});
  svg.append(hover);
  const show=(clientX)=>{
    const box=svg.getBoundingClientRect(), sx=(clientX-box.left)*(W/box.width);
    let best=pts[0],bd=1e18;
    pts.forEach(p=>{const d0=Math.abs(X(p.x)-sx); if(d0<bd){bd=d0;best=p;}});
    cross.setAttribute("x1",X(best.x));cross.setAttribute("x2",X(best.x));
    cross.setAttribute("visibility","visible");
    dot.setAttribute("cx",X(best.x));dot.setAttribute("cy",Y(best.y));
    dot.setAttribute("visibility","visible");
    tip.replaceChildren();
    const v=el("div","v"); v.append(el("span","k"),document.createTextNode(fmt(best.y)+" INR cum."));
    tip.append(v);
    if(best.trade){
      tip.append(el("div","small",t2time(best.x)+" · "+best.trade.symbol+" "+best.trade.side+" ×"+best.trade.qty));
      tip.append(el("div","small","trade P&L: "+fmt(best.trade.pnl)));
    } else tip.append(el("div","small","session start"));
    tip.style.display="block";
    const wb=wrap.getBoundingClientRect();
    let lx=(X(best.x)/W)*wb.width+12;
    if(lx+160>wb.width) lx-=180;
    tip.style.left=lx+"px"; tip.style.top=(Y(best.y)/H)*240-10+"px";
  };
  hover.addEventListener("pointermove",e=>show(e.clientX));
  hover.addEventListener("pointerleave",()=>{tip.style.display="none";
    cross.setAttribute("visibility","hidden");dot.setAttribute("visibility","hidden");});
}

const btnNewTrade = document.getElementById("btnNewTrade");
const tradeForm = document.getElementById("tradeForm");
const btnCancelTrade = document.getElementById("btnCancelTrade");
const btnSubmitTrade = document.getElementById("btnSubmitTrade");
const tradeMsg = document.getElementById("tradeMsg");

if (btnNewTrade) {
  btnNewTrade.onclick = () => {
    tradeForm.style.display = tradeForm.style.display === "none" ? "block" : "none";
    tradeMsg.textContent = "";
  };
}
if (btnCancelTrade) {
  btnCancelTrade.onclick = () => {
    tradeForm.style.display = "none";
    tradeMsg.textContent = "";
  };
}
if (btnSubmitTrade) {
  btnSubmitTrade.onclick = async () => {
    btnSubmitTrade.disabled = true;
    tradeMsg.style.color = "var(--ink-2)";
    tradeMsg.textContent = "Executing order…";
    try {
      const sym = (document.getElementById("tSym").value || "").trim().toUpperCase();
      const side = document.getElementById("tSide").value;
      const qty = parseInt(document.getElementById("tQty").value, 10) || 1;
      const px = parseFloat(document.getElementById("tPx").value) || 0;
      const res = await fetch("/api/trade", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({symbol: sym, side: side, qty: qty, price: px})
      });
      const j = await res.json();
      if (j.ok) {
        tradeMsg.style.color = "var(--good)";
        tradeMsg.textContent = `✓ Trade #${j.trade_id} executed!`;
        setTimeout(() => { tradeForm.style.display = "none"; load(); }, 700);
      } else {
        tradeMsg.style.color = "var(--bad)";
        tradeMsg.textContent = `✗ ${j.reason || j.error || "Order rejected"}`;
      }
    } catch (e) {
      tradeMsg.style.color = "var(--bad)";
      tradeMsg.textContent = `Error: ${e}`;
    } finally {
      btnSubmitTrade.disabled = false;
    }
  };
}

async function loadQuotes(){
  try{
    const r = await fetch("/api/quotes", {cache:"no-store"});
    if(!r.ok) return;
    const res = await r.json();
    const itemsBox = document.getElementById("liveTickerItems");
    const updatedEl = document.getElementById("liveTickerUpdated");
    if(!itemsBox) return;
    if(!res.quotes || !res.quotes.length){
      itemsBox.innerHTML = '<span class="muted" style="font-size:11.5px">Waiting for real-time market stream…</span>';
      return;
    }
    itemsBox.innerHTML = "";
    res.quotes.forEach(q => {
      const v = q.v || {};
      const rawName = (v.short_name || q.n || "").replace("-INDEX","").replace("-EQ","").replace("NSE:","");
      const lp = v.lp || 0;
      const ch = v.ch || 0;
      const chp = v.chp || 0;
      const isUp = ch >= 0;
      const card = el("div", "ticker-item");
      card.style.cssText = "background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:7px 11px;min-width:142px;flex:0 0 auto;display:flex;flex-direction:column;gap:3px;box-shadow:var(--shadow)";
      card.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;gap:6px">
          <span style="font-weight:700;font-size:11px;letter-spacing:.04em;color:var(--ink)">${rawName}</span>
          <span class="badge sm ${isUp ? 'bull' : 'bear'}" style="font-size:9.5px;padding:1px 5px">${chp >= 0 ? '+' : ''}${chp.toFixed(2)}%</span>
        </div>
        <div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin-top:2px">
          <span style="font-size:14px;font-weight:700;font-family:ui-monospace,monospace;color:${isUp ? 'var(--up)' : 'var(--down)'}">₹${fmt(lp)}</span>
          <span style="font-size:10px;color:var(--ink-3);font-family:ui-monospace,monospace">${ch >= 0 ? '+' : ''}${fmt(ch)}</span>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:9.5px;color:var(--ink-3);margin-top:1px">
          <span>H: ${fmt(v.high_price)}</span>
          <span>L: ${fmt(v.low_price)}</span>
        </div>
      `;
      itemsBox.appendChild(card);
    });
    if(updatedEl){
      updatedEl.textContent = "⚡ Live: " + new Date().toLocaleTimeString("en-IN",{hour12:false});
    }
  }catch(e){
    console.warn("loadQuotes error:", e);
  }
}

const btnRecalcChallenge = document.getElementById("btnRecalcChallenge");
if (btnRecalcChallenge) {
  btnRecalcChallenge.onclick = async () => {
    btnRecalcChallenge.disabled = true;
    btnRecalcChallenge.textContent = "Updating…";
    try {
      await fetch("/api/challenge/recalc");
      await load();
    } catch(e) {
      console.warn("Recalc challenge error:", e);
    } finally {
      btnRecalcChallenge.disabled = false;
      btnRecalcChallenge.textContent = "Refresh Stats";
    }
  };
}

const btnResetChallenge = document.getElementById("btnResetChallenge");
if (btnResetChallenge) {
  btnResetChallenge.onclick = async () => {
    if (!confirm("Are you sure you want to reset the 15k to 1 Lakh Challenge starting fresh from ₹15,000 today?")) {
      return;
    }
    btnResetChallenge.disabled = true;
    btnResetChallenge.textContent = "Resetting…";
    try {
      await fetch("/api/challenge/reset", {method: "POST"});
      await load();
    } catch(e) {
      alert("Error resetting challenge: " + e);
    } finally {
      btnResetChallenge.disabled = false;
      btnResetChallenge.textContent = "Reset to ₹15k";
    }
  };
}

const btnTriggerSupervisor = document.getElementById("btnTriggerSupervisor");
if (btnTriggerSupervisor) {
  btnTriggerSupervisor.onclick = async () => {
    btnTriggerSupervisor.disabled = true;
    btnTriggerSupervisor.textContent = "Reviewing…";
    try {
      await fetch("/api/supervisor/review", {method: "POST"});
      await load();
    } catch(e) {
      alert("Supervisor review error: " + e);
    } finally {
      btnTriggerSupervisor.disabled = false;
      btnTriggerSupervisor.textContent = "⚡ Review Pending Trades";
    }
  };
}

load();
loadFno();
loadQuotes();
setInterval(load, 10000);
setInterval(loadFno, 15000);
setInterval(loadQuotes, 3000);
__THEMEJS__
window.addEventListener("resize", ()=>DATA&&renderChart(DATA));
</script>
</body></html>
"""


_TOPBAR_RIGHT = """    <select class="btn" id="dateSel" aria-label="Trading session"></select>
    <div class="live"><span class="led"></span><span id="refreshed">—</span></div>"""

PAGE = (_PAGE_TEMPLATE
        .replace("__THEME__", ui_theme.CSS)
        .replace("__TOPBAR__", ui_theme.topbar("Trading Agent Dashboard", "dashboard",
                                               _TOPBAR_RIGHT))
        .replace("__NIFTYJS__", ui_theme.NIFTY_JS)
        .replace("__CONTEXTJS__", ui_theme.CONTEXT_JS)
        .replace("__THEMEJS__", ui_theme.THEME_JS))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        # The F&O scanner page and its API live in trading.fno.web; they are
        # served from here too so one process covers both views. "/" is not
        # delegated — that is this dashboard's own page.
        fno = fno_web.serve(url.path, parse_qs(url.query))
        if fno is not None:
            return self._send(*fno)
        if url.path == "/api/data":
            q = parse_qs(url.query)
            body = json.dumps(get_data(q.get("date", [None])[0]),
                              default=str).encode()
            self._send(200, "application/json", body)
        elif url.path == "/api/challenge/recalc":
            try:
                from trading.challenge import recalculate_state
                st = recalculate_state()
                self._send(200, "application/json", json.dumps(st, default=str).encode())
            except Exception as e:
                self._send(500, "application/json", json.dumps({"error": str(e)}).encode())
        elif url.path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode())
        elif url.path == "/favicon.ico":
            self._send(204, "image/x-icon", b"")
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length)) if length > 0 else {}
        except Exception:
            body = {}

        if url.path == "/api/trade":
            res = _handle_trade(body)
            self._send(200 if res.get("ok") else 400, "application/json", json.dumps(res).encode())
        elif url.path == "/api/close":
            res = _handle_close(body)
            self._send(200 if res.get("ok") else 400, "application/json", json.dumps(res).encode())
        elif url.path == "/api/telegram-alert":
            res = _handle_telegram_alert(body)
            self._send(200 if res.get("ok") else 400, "application/json", json.dumps(res).encode())
        elif url.path == "/api/supervisor/review":
            try:
                from trading.agents.supervisor import run as run_supervisor
                run_supervisor(loop=False)
                self._send(200, "application/json", json.dumps({"ok": True}).encode())
            except Exception as e:
                self._send(500, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
        elif url.path == "/api/challenge/reset":
            try:
                from trading.challenge import reset_challenge
                st = reset_challenge()
                self._send(200, "application/json", json.dumps({"ok": True, "state": st}, default=str).encode())
            except Exception as e:
                self._send(500, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code: int, ctype: str, body: bytes):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, fmt, *args):  # quiet
        pass


def main(argv: list[str] | None = None):
    import argparse

    from trading import retention

    ap = argparse.ArgumentParser(prog="python -m trading.dashboard")
    ap.add_argument("port", nargs="?", type=int, default=8787)
    ap.add_argument("--keep-days", type=int, default=RETENTION_DAYS,
                    help=f"trading sessions of history to keep (default {RETENTION_DAYS})")
    ap.add_argument("--no-prune", action="store_true",
                    help="leave the ledger alone — keep every session on disk")
    args = ap.parse_args(argv)

    if args.no_prune:
        print("retention: pruning disabled for this run (--no-prune)")
    else:
        print(retention.summary(retention.prune(args.keep_days)))

    fyers.start_background_server()
    server = fno_web._bind(args.port, Handler, "the dashboard")
    if server is None:
        return 1
    return fno_web._serve_forever(
        server, f"dashboard: http://localhost:{args.port}")


if __name__ == "__main__":
    main()
