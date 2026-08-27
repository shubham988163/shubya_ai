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
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

from trading import ui_theme
from trading.config import (DB_PATH, REPORTS_DIR, RETENTION_DAYS, TODAY_CONFIG_PATH)
from trading.fno import web as fno_web
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

    trades = [dict(r) for r in conn.execute(
        "SELECT * FROM trades WHERE date = ? ORDER BY ts", (date,)).fetchall()] if date else []

    rejections = [dict(r) for r in conn.execute(
        "SELECT * FROM rejections ORDER BY ts DESC LIMIT 50").fetchall()]

    agent_log = [dict(r) for r in conn.execute(
        "SELECT id, ts, agent, model, ok, error, "
        "substr(COALESCE(response,''),1,400) AS response "
        "FROM agent_log ORDER BY ts DESC LIMIT 50").fetchall()]
    conn.close()

    closed = [t for t in trades if t["status"] == "closed"]
    wins = [t for t in closed if (t["pnl"] or 0) > 0]
    stats = {
        "net_pnl": round(sum(t["pnl"] or 0 for t in closed), 2),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "trades": len(trades),
        "open": len(trades) - len(closed),
        "charges": round(sum(t["charges"] or 0 for t in closed), 2),
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
            report = p.read_text()

    try:
        from trading.agents.llm import provider
        from trading.config import GEMINI_MODEL, ANTHROPIC_MODEL
        prov = provider()
        model = GEMINI_MODEL if prov == "gemini" else ANTHROPIC_MODEL
    except Exception:  # noqa: BLE001
        prov, model = "?", "?"

    return {"date": date, "dates": dates, "stats": stats, "trades": trades,
            "rejections": rejections, "agent_log": agent_log,
            "day_config": day_config, "report": report,
            "provider": prov, "model": model,
            "retention_days": RETENTION_DAYS,
            "side_report": _breakdown(trades, "side"),
            "strategy_report": _breakdown(trades, "strategy_id"),
            "day_pnl_all_time": ledger.day_realized_pnl(date) if date else 0}


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
</style></head>
<body>
<div class="wrap">
__TOPBAR__

  <section class="panel" id="buynow">
    <h2>Buy now <span class="sub" id="bnMeta">— checking the F&amp;O scanner…</span></h2>
    <div class="body" id="bnBody"></div>
  </section>

  <div id="niftyWrap"></div>

  <div class="kpis" id="tiles"></div>

  <section class="panel">
    <h2>Cumulative net P&amp;L <span class="sub">closed trades through the selected session</span></h2>
    <div class="body" id="chartwrap">
      <svg id="chart" width="100%" height="240" role="img"
           aria-label="Cumulative net P&L line chart; values also in the trades table below"></svg>
      <div id="tooltip"></div>
    </div>
  </section>

  <section class="panel">
    <h2>Buy vs Sell <span class="sub">selected session</span></h2>
    <div class="scroll"><table id="sides"></table></div>
  </section>

  <section class="panel">
    <h2>By strategy <span class="sub">selected session</span></h2>
    <div class="scroll"><table id="strats"></table></div>
  </section>

  <section class="panel">
    <h2>Pre-market agent <span class="sub">today's config</span></h2>
    <div class="body"><div class="cfg" id="cfg"></div></div>
  </section>

  <section class="panel">
    <h2>Trades <span class="sub">with the supervisor's verdict</span></h2>
    <div class="scroll"><table id="trades"></table></div>
  </section>

  <section class="panel">
    <h2>Rejections <span class="sub">risk kernel &amp; rate limiter, last 50</span></h2>
    <div class="scroll"><table id="rej"></table></div>
  </section>

  <section class="panel">
    <h2>LLM audit log <span class="sub">last 50 calls</span></h2>
    <div class="scroll"><table id="alog"></table></div>
  </section>

  <section class="panel">
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
    cell("Target 1","₹"+fmt(t.target1),"tg","1:"+t.rr1);
    cell("Target 2","₹"+fmt(t.target2),"tg","1:"+t.rr2);
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
  const q = selDate ? "?date="+encodeURIComponent(selDate) : "";
  const r = await fetch("/api/data"+q);
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
}

function render(){
  const d=DATA;
  document.getElementById("win").textContent =
    `paper mode · LLM: ${d.provider} (${d.model}) · ${d.date||"no data"}`;

  // date filter
  const sel=document.getElementById("dateSel"); sel.replaceChildren();
  (d.dates.length?d.dates:[d.date||"no data"]).forEach(dt=>{
    const o=el("option",null,dt); o.value=dt; if(dt===d.date)o.selected=true; sel.append(o);
  });
  sel.onchange=()=>{selDate=sel.value; load();};

  renderTiles(d); renderChart(d); renderBreakdown(d); renderCfg(d);
  renderTrades(d); renderRej(d); renderAlog(d);
  const rep=document.getElementById("report");
  rep.textContent = d.report || "No journal report for this date yet — run: python -m trading.agents.eod_journal";
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
  mk("Net P&L (INR)", fmt(pnl), {hero:true, dir:pnl>=0?"up":"down",
     delta:(pnl>=0?"▲":"▼")+" vs start of day"});
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
    row.append(el("td","num",tr.exit_price==null?"open":fmt(tr.exit_price)));
    const pnl=el("td","num "+((tr.pnl??0)>=0?"pnl-pos":"pnl-neg"),
                 tr.pnl==null?"—":fmt(tr.pnl));
    row.append(pnl);
    row.append(el("td","num",tr.charges==null?"—":fmt(tr.charges)));
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

load();
loadFno();
setInterval(load, 10000);
setInterval(loadFno, 15000);
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
        elif url.path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode())
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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

    server = fno_web._bind(args.port, Handler, "the dashboard")
    if server is None:
        return 1
    return fno_web._serve_forever(
        server, f"dashboard: http://localhost:{args.port}")


if __name__ == "__main__":
    main()
