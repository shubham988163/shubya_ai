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

from trading.config import DB_PATH, TODAY_CONFIG_PATH, REPORTS_DIR
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
            "side_report": _breakdown(trades, "side"),
            "strategy_report": _breakdown(trades, "strategy_id"),
            "day_pnl_all_time": ledger.day_realized_pnl(date) if date else 0}


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trading Agent Dashboard</title>
<style>
:root{
  --surface-1:#fcfcfb; --page:#f9f9f7;
  --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10);
  --series-1:#2a78d6;
  --good:#0ca30c; --warning:#fab219; --critical:#d03b3b;
  --delta-good:#006300; --delta-bad:#d03b3b;
}
@media (prefers-color-scheme: dark){:root{
  --surface-1:#1a1a19; --page:#0d0d0d;
  --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
  --series-1:#3987e5;
  --delta-good:#0ca30c;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
  font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:18px;font-weight:600;margin:0}
.sub{color:var(--muted);font-size:12px;margin-top:2px}
.filters{display:flex;gap:10px;align-items:center;margin:16px 0}
.filters select{background:var(--surface-1);color:var(--ink);
  border:1px solid var(--border);border-radius:8px;padding:6px 10px;font:inherit}
.filters .live{color:var(--muted);font-size:12px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.tile .label{color:var(--ink-2);font-size:12px}
.tile .value{font-size:26px;font-weight:600;margin-top:2px}
.tile .value.hero{font-size:48px;line-height:1.1}
.tile .delta{font-size:12px;margin-top:2px}
.up{color:var(--delta-good)} .down{color:var(--delta-bad)}
section{margin-top:20px}
h2{font-size:13px;font-weight:600;color:var(--ink-2);margin:0 0 8px;text-transform:none}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:var(--muted);font-weight:500;text-align:left;padding:6px 8px;border-bottom:1px solid var(--grid)}
td{padding:6px 8px;border-bottom:1px solid var(--grid);vertical-align:top}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.badge{display:inline-flex;align-items:center;gap:5px;font-size:12px}
.badge::before{content:"";width:8px;height:8px;border-radius:50%;background:currentColor}
.badge.approve{color:var(--good)} .badge.caution{color:var(--warning)} .badge.veto{color:var(--critical)}
.badge.err{color:var(--critical)} .badge.ok{color:var(--good)}
.pnl-pos{color:var(--delta-good)} .pnl-neg{color:var(--delta-bad)}
.mono{font-variant-numeric:tabular-nums}
.small{font-size:12px;color:var(--ink-2)}
.muted{color:var(--muted)}
pre.report{white-space:pre-wrap;font:13px/1.55 system-ui,sans-serif;margin:0;color:var(--ink-2)}
#chartwrap{position:relative}
#tooltip{position:absolute;pointer-events:none;background:var(--surface-1);
  border:1px solid var(--border);border-radius:8px;padding:8px 10px;font-size:12px;
  box-shadow:0 2px 10px rgba(0,0,0,.12);display:none;min-width:140px;z-index:2}
#tooltip .v{font-weight:600;font-size:14px}
#tooltip .k{display:inline-block;width:12px;height:0;border-top:2px solid var(--series-1);
  vertical-align:middle;margin-right:6px}
.cfg{display:flex;flex-wrap:wrap;gap:14px;font-size:13px}
.cfg b{font-weight:600}
.empty{color:var(--muted);padding:14px 0;font-size:13px}
.overflow{overflow-x:auto}
</style></head>
<body>
<div class="wrap">
  <h1>Trading Agent Dashboard</h1>
  <div class="sub" id="meta"></div>

  <div class="filters">
    <select id="dateSel" aria-label="Trading date"></select>
    <span class="live" id="refreshed"></span>
  </div>

  <div class="tiles" id="tiles"></div>

  <section class="card">
    <h2>Cumulative net P&amp;L (INR) — closed trades through the day</h2>
    <div id="chartwrap">
      <svg id="chart" width="100%" height="240" role="img"
           aria-label="Cumulative net P&L line chart; values also in the trades table below"></svg>
      <div id="tooltip"></div>
    </div>
  </section>

  <section class="card">
    <h2>Buy vs Sell — selected day</h2>
    <div class="overflow"><table id="sides"></table></div>
  </section>

  <section class="card">
    <h2>By strategy — selected day</h2>
    <div class="overflow"><table id="strats"></table></div>
  </section>

  <section class="card">
    <h2>Pre-market agent — today's config</h2>
    <div class="cfg" id="cfg"></div>
  </section>

  <section class="card">
    <h2>Trades &amp; supervisor verdicts</h2>
    <div class="overflow"><table id="trades"></table></div>
  </section>

  <section class="card">
    <h2>Rejections — risk kernel &amp; rate limiter (last 50)</h2>
    <div class="overflow"><table id="rej"></table></div>
  </section>

  <section class="card">
    <h2>LLM audit log (last 50 calls)</h2>
    <div class="overflow"><table id="alog"></table></div>
  </section>

  <section class="card">
    <h2>EOD journal report</h2>
    <pre class="report" id="report"></pre>
  </section>
</div>

<script>
"use strict";
let DATA=null, selDate=null;

const fmt=(n,d=2)=> n==null ? "—" : Number(n).toLocaleString("en-IN",{minimumFractionDigits:d,maximumFractionDigits:d});
const t2time=ts=> new Date(ts*1000).toLocaleTimeString("en-IN",{hour12:false});

function el(tag, cls, text){const e=document.createElement(tag); if(cls)e.className=cls;
  if(text!=null)e.textContent=text; return e;}

async function load(){
  const q = selDate ? "?date="+encodeURIComponent(selDate) : "";
  const r = await fetch("/api/data"+q);
  DATA = await r.json();
  selDate = DATA.date;
  render();
  document.getElementById("refreshed").textContent =
    "auto-refreshes · updated " + new Date().toLocaleTimeString("en-IN",{hour12:false});
}

function render(){
  const d=DATA;
  document.getElementById("meta").textContent =
    `paper mode · LLM: ${d.provider} (${d.model})`;

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
    const c=el("div","card tile");
    c.append(el("div","label",label));
    const v=el("div","value"+(opts.hero?" hero":""),value);
    if(opts.dir) v.classList.add(opts.dir);
    c.append(v);
    if(opts.delta) c.append(el("div","delta "+(opts.dir||""),opts.delta));
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

function verdictBadge(v){
  if(!v) return el("span","muted","pending");
  const b=el("span","badge "+v, v);
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
    row.append(el("td","mono",new Date(r.ts*1000).toLocaleString("en-IN",{hour12:false})));
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
    row.append(el("td","mono",new Date(a.ts*1000).toLocaleString("en-IN",{hour12:false})));
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
    const t=mk("text",{x:W/2,y:H/2,"text-anchor":"middle",fill:C.muted,"font-size":"13"});
    t.textContent="No closed trades yet for this date."; svg.append(t); return;
  }

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
setInterval(load, 10000);
window.addEventListener("resize", ()=>DATA&&renderChart(DATA));
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
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


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"dashboard: http://localhost:{port}  (Ctrl-C to stop)")
    server.serve_forever()


if __name__ == "__main__":
    main()
