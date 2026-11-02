"""Web interface and API router for 24/7 Forex and Crypto trading on Delta Exchange."""
from __future__ import annotations

import json
from typing import Any

from trading import ui_theme
from trading.delta_exchange import DeltaClient, FOREX_WATCHLIST
from trading.forex_engine import ForexEngine

_FOREX_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Forex &amp; Crypto — Delta Exchange 24/7 Trading</title>
<script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
<style>
__THEME__
.forex-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin-top:14px}
.forex-card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;box-shadow:var(--shadow);transition:all .2s ease;cursor:pointer;position:relative}
.forex-card:hover{border-color:var(--accent);transform:translateY(-2px);box-shadow:0 6px 20px rgba(47,159,219,.15)}
.forex-card.active{border-color:var(--accent);background:color-mix(in srgb,var(--accent-soft) 40%,var(--card));box-shadow:0 0 16px rgba(47,159,219,.25)}
.forex-card .chart-hint{font-size:10px;color:var(--accent);font-weight:700;display:flex;align-items:center;gap:4px;margin-top:6px;opacity:0.85;transition:opacity .15s}
.forex-card:hover .chart-hint{opacity:1}
.forex-card .sym{font-size:15px;font-weight:700;color:var(--ink);display:flex;align-items:center;justify-content:space-between}
.forex-card .px{font-size:22px;font-weight:700;margin-top:6px;font-variant-numeric:tabular-nums;font-family:ui-monospace,monospace}
.forex-card .chg{font-size:12px;font-weight:600;margin-top:3px;font-family:ui-monospace,monospace}
.forex-card .chg.up{color:var(--good)}
.forex-card .chg.down{color:var(--bad)}
.forex-card .sub{font-size:10.5px;color:var(--ink-3);margin-top:4px}

.signals-container{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:14px;margin-top:14px}
.signal-card{background:var(--cell);border:1px solid var(--line);border-radius:10px;padding:14px;box-shadow:var(--shadow);display:flex;flex-direction:column;gap:10px;position:relative}
.signal-card.a-plus{border-color:rgba(52,211,153,.4);box-shadow:0 0 15px rgba(52,211,153,.08)}
.signal-header{display:flex;align-items:center;justify-content:space-between}
.signal-title{font-size:16px;font-weight:700;display:flex;align-items:center;gap:8px}
.strategy-badges{display:flex;flex-wrap:wrap;gap:5px}
.strategy-pill{font-size:10px;font-weight:600;padding:2px 7px;border-radius:5px;background:var(--card);border:1px solid var(--line);color:var(--ink-2)}
.levels-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);border-radius:6px;overflow:hidden;margin:6px 0}
.level-cell{background:var(--card);padding:7px 9px;text-align:center}
.level-cell .k{font-size:9px;text-transform:uppercase;color:var(--ink-3);letter-spacing:.05em}
.level-cell .v{font-size:13px;font-weight:700;margin-top:2px;font-family:ui-monospace,monospace}
.level-cell.sl .v{color:var(--bad)}
.level-cell.t1 .v{color:var(--good)}
.level-cell.t2 .v{color:var(--good)}
.btn-exec{width:100%;padding:9px;border-radius:7px;border:none;font-weight:700;font-size:12px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:6px;transition:all .15s ease}
.btn-exec.buy{background:var(--good);color:#04070f}
.btn-exec.sell{background:var(--bad);color:#fff}
.btn-exec:hover{opacity:.9;transform:scale(0.99)}
.btn-exec:disabled{opacity:.4;cursor:not-allowed}

.status-box{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin-top:14px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}

/* 4-Agent AI Council Modal & Cards */
.council-overlay{display:none;position:fixed;inset:0;background:rgba(2,6,17,0.85);backdrop-filter:blur(8px);z-index:9999;overflow-y:auto;padding:24px 16px}
.council-modal{max-width:980px;margin:16px auto 40px auto;background:var(--card);border:1px solid var(--line-2);border-radius:14px;box-shadow:0 24px 60px rgba(0,0,0,0.8);overflow:hidden;animation:popIn .2s ease-out}
@keyframes popIn{from{opacity:0;transform:scale(0.97) translateY(12px)}to{opacity:1;transform:scale(1) translateY(0)}}

.agent-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:12px;margin-top:10px}
.agent-card{background:var(--cell);border:1px solid var(--line);border-radius:10px;padding:14px;display:flex;flex-direction:column;gap:8px}
.agent-hd{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);padding-bottom:8px}
.agent-title{font-size:11.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;display:flex;align-items:center;gap:7px;color:var(--ink)}
.news-item{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:9px 12px;display:flex;flex-direction:column;gap:4px;transition:border-color .15s ease}
.news-item:hover{border-color:var(--accent)}
</style>
</head>
<body>
__TOPBAR__

<main style="max-width:1440px;margin:0 auto;padding:16px">

  <!-- Status & Mode Bar -->
  <div class="status-box">
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <span class="badge ok" id="statusBadge"><span class="g">●</span> Delta Exchange Online</span>
      <span style="font-size:12px;color:var(--ink-2)" id="statusMessage">Connecting to Delta Exchange India…</span>
    </div>
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <span style="font-size:12px;color:var(--ink-3)">Mode:</span>
      <span class="badge" style="border-color:var(--accent);color:var(--accent);font-weight:700" id="modePill">PAPER TRADING</span>

      <button class="btn" onclick="toggleCouncil()" id="btnCouncil" style="background:var(--cell);border-color:var(--accent);font-weight:700;padding:6px 14px;cursor:pointer" title="Click to toggle automated 4-agent gate">
        🤖 4-Agent Council: <span id="councilText" style="color:var(--good);margin-left:4px">ACTIVE</span>
      </button>

      <button class="btn" onclick="runScan()" id="btnScan" style="background:var(--accent);color:#fff;border-color:var(--accent);font-weight:700;padding:6px 14px">
        ⚡ Scan Forex &amp; Crypto Setups
      </button>
    </div>
  </div>

  <!-- $100 -> $1,000 Compounding Milestone & Survival Tracker -->
  <div style="background:linear-gradient(135deg, rgba(16,185,129,0.08) 0%, rgba(99,102,241,0.08) 100%);border:1px solid rgba(16,185,129,0.35);border-radius:12px;padding:16px 20px;margin-top:14px;box-shadow:var(--shadow)">
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px">
      <div style="display:flex;align-items:center;gap:12px">
        <span style="font-size:26px">🎯</span>
        <div>
          <div style="font-size:15px;font-weight:800;color:var(--ink);letter-spacing:0.02em;display:flex;align-items:center;gap:8px">
            <span>$100 → $1,000 Compounding &amp; Survival Challenge</span>
            <span class="badge sm ok" style="font-weight:700">● 10x Exponential Roadmap</span>
          </div>
          <div style="font-size:11.5px;color:var(--ink-2);margin-top:2px">
            Dynamic Equity Sizing (2.5% Risk), 6% Daily Circuit Breaker, and AI Post-Mortem Autopsies to survive &amp; scale from $100 to $1,000+.
          </div>
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:10px">
        <span class="badge ok" style="font-size:11px;font-weight:700" id="challengeStatusBadge">🛡️ SURVIVAL KERNEL ACTIVE</span>
        <button class="btn sm" onclick="resetChallenge()" style="background:var(--cell);border-color:var(--line-2);font-size:11px;cursor:pointer" title="Reset paper balance to clean $100 starting state">🔄 Reset to $100</button>
      </div>
    </div>

    <!-- Progress Bar to $1,000 -->
    <div style="margin-top:14px">
      <div style="display:flex;justify-content:space-between;font-size:11.5px;margin-bottom:6px;font-family:ui-monospace,monospace">
        <span><b>Initial Capital:</b> $100.00</span>
        <span style="font-weight:700;color:var(--good)" id="challengeEquityText">Current Equity: $100.00 (+0.0%)</span>
        <span><b>Milestone 1:</b> $1,000.00 (10x)</span>
      </div>
      <div style="width:100%;height:10px;background:var(--line);border-radius:6px;overflow:hidden;position:relative">
        <div id="challengeProgressBar" style="height:100%;width:0%;background:linear-gradient(90deg, #10b981 0%, #3b82f6 100%);transition:width 0.4s ease;border-radius:6px"></div>
      </div>
    </div>

    <!-- Dynamic Sizing & Safety Parameters Grid -->
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-top:14px">
      <div style="background:var(--card);border:1px solid var(--line);border-radius:8px;padding:9px 12px">
        <div style="font-size:10px;text-transform:uppercase;color:var(--ink-3)">Dynamic Risk / Trade</div>
        <div style="font-size:15px;font-weight:700;color:var(--good);margin-top:2px;font-family:ui-monospace,monospace" id="statDynamicRisk">$2.50 (2.5%)</div>
      </div>
      <div style="background:var(--card);border:1px solid var(--line);border-radius:8px;padding:9px 12px">
        <div style="font-size:10px;text-transform:uppercase;color:var(--ink-3)">Max Daily Loss Limit</div>
        <div style="font-size:15px;font-weight:700;color:var(--bad);margin-top:2px;font-family:ui-monospace,monospace" id="statDailyLimit">-$6.00 (6.0%)</div>
      </div>
      <div style="background:var(--card);border:1px solid var(--line);border-radius:8px;padding:9px 12px">
        <div style="font-size:10px;text-transform:uppercase;color:var(--ink-3)">Max Open Concurrent</div>
        <div style="font-size:15px;font-weight:700;color:var(--ink);margin-top:2px;font-family:ui-monospace,monospace">2 Positions (5% Cap)</div>
      </div>
      <div style="background:var(--card);border:1px solid var(--line);border-radius:8px;padding:9px 12px">
        <div style="font-size:10px;text-transform:uppercase;color:var(--ink-3)">Progress to 10x Goal</div>
        <div style="font-size:15px;font-weight:700;color:var(--accent);margin-top:2px;font-family:ui-monospace,monospace" id="statProgressPct">0.0%</div>
      </div>
    </div>
  </div>

  <!-- Portfolio Performance & Trade Statistics KPIs -->
  <div class="kpis" style="margin-top:14px">
    <div class="kpi">
      <div class="k">Total Trades Given</div>
      <div class="v" id="kpiTotalTrades">0</div>
      <div class="f" id="kpiTotalTradesSub">0 Active • 0 Closed</div>
    </div>
    <div class="kpi">
      <div class="k">Net Realized P&amp;L</div>
      <div class="v" id="kpiRealizedPnl">$0.00</div>
      <div class="f" id="kpiWinRate">Win Rate: 0.0% (0W / 0L)</div>
    </div>
    <div class="kpi">
      <div class="k">Live Unrealized P&amp;L</div>
      <div class="v" id="kpiOpenPnl">$0.00</div>
      <div class="f" id="kpiOpenCount">0 open positions</div>
    </div>
    <div class="kpi">
      <div class="k">Cumulative Total P&amp;L</div>
      <div class="v hero" id="kpiTotalPnl">$0.00</div>
      <div class="f">Realized + Unrealized</div>
    </div>
  </div>

  <!-- Live 24/7 Market Tickers -->
  <div style="margin-top:18px">
    <div style="display:flex;align-items:center;justify-content:space-between">
      <h2 style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3);margin:0">
        🌐 24/7 Live Forex &amp; Crypto Pairs (Delta Exchange India) — <span style="color:var(--accent);font-weight:600">Click any pair to view live chart</span>
      </h2>
      <span style="font-size:11px;color:var(--ink-3)" id="tickerUpdateTime">Updating…</span>
    </div>
    <div class="forex-grid" id="forexGrid">
      <div class="empty">Loading Delta Exchange live tickers…</div>
    </div>
  </div>

  <!-- Interactive Real-Time Candlestick & Technical Chart Station -->
  <div style="margin-top:20px" class="panel" id="chartStationPanel">
    <div style="background:var(--cell);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px">
      <div style="display:flex;align-items:center;gap:12px">
        <span style="font-size:22px">📊</span>
        <div>
          <div style="font-size:14px;font-weight:700;color:var(--ink);display:flex;align-items:center;gap:8px">
            <span id="chartTitle">Bitcoin / USD Perpetual (BTCUSD)</span>
            <span class="badge sm ok" id="chartLiveBadge" style="font-weight:700">● Live Real-Time Feed</span>
          </div>
          <div style="font-size:11px;color:var(--ink-3)" id="chartSubtitle">
            Delta Exchange 24/7 Perpetual • Real-time Candlesticks &amp; Technical Indicators
          </div>
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <!-- Timeframe Selector -->
        <div class="seg" id="tfSelector">
          <button onclick="setChartTf('1')" id="tf_1">1m</button>
          <button onclick="setChartTf('5')" id="tf_5" aria-pressed="true">5m</button>
          <button onclick="setChartTf('15')" id="tf_15">15m</button>
          <button onclick="setChartTf('60')" id="tf_60">1h</button>
          <button onclick="setChartTf('240')" id="tf_240">4h</button>
          <button onclick="setChartTf('D')" id="tf_D">1D</button>
        </div>

        <!-- Symbol Search input -->
        <div style="display:flex;align-items:center;gap:4px">
          <input type="text" id="symbolInput" placeholder="Symbol (e.g. BTCUSD, RELIANCE, NIFTY)" style="background:var(--card);border:1px solid var(--line);border-radius:6px;padding:5px 9px;color:var(--ink);font-size:11px;width:170px" onkeydown="if(event.key==='Enter') searchSymbolChart()">
          <button class="btn sm" onclick="searchSymbolChart()" style="padding:5px 9px">Go</button>
        </div>

        <!-- Open in Fullscreen / New Page -->
        <button class="btn sm" onclick="openChartModal()" title="View in full-screen modal">⤢ Fullscreen</button>
        <button class="btn sm" onclick="openDedicatedChartPage()" title="Open dedicated chart page">↗ New Page</button>
      </div>
    </div>

    <!-- Quick Switcher Bar -->
    <div style="background:var(--bg-2);padding:8px 14px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:6px;overflow-x:auto;font-size:11px">
      <span style="color:var(--ink-3);font-size:10px;text-transform:uppercase;margin-right:4px">Forex &amp; Crypto:</span>
      <button class="btn sm" onclick="selectChartSymbol('BTCUSD')" id="pill_BTCUSD">BTCUSD</button>
      <button class="btn sm" onclick="selectChartSymbol('ETHUSD')" id="pill_ETHUSD">ETHUSD</button>
      <button class="btn sm" onclick="selectChartSymbol('SOLUSD')" id="pill_SOLUSD">SOLUSD</button>
      <button class="btn sm" onclick="selectChartSymbol('XAUTUSD')" id="pill_XAUTUSD">XAUTUSD (Gold)</button>
      <button class="btn sm" onclick="selectChartSymbol('XRPUSD')" id="pill_XRPUSD">XRPUSD</button>
      <button class="btn sm" onclick="selectChartSymbol('BNBUSD')" id="pill_BNBUSD">BNBUSD</button>
      <button class="btn sm" onclick="selectChartSymbol('DOGEUSD')" id="pill_DOGEUSD">DOGEUSD</button>
      <button class="btn sm" onclick="selectChartSymbol('ADAUSD')" id="pill_ADAUSD">ADAUSD</button>
      <span style="border-left:1px solid var(--line);margin:0 4px;height:16px"></span>
      <span style="color:var(--ink-3);font-size:10px;text-transform:uppercase;margin-right:4px">Stocks / Indices:</span>
      <button class="btn sm" onclick="selectChartSymbol('NSE:NIFTY')" id="pill_NIFTY">NIFTY 50</button>
      <button class="btn sm" onclick="selectChartSymbol('NSE:BANKNIFTY')" id="pill_BANKNIFTY">BANK NIFTY</button>
      <button class="btn sm" onclick="selectChartSymbol('NSE:RELIANCE')" id="pill_RELIANCE">RELIANCE</button>
      <button class="btn sm" onclick="selectChartSymbol('NSE:TCS')" id="pill_TCS">TCS</button>
      <button class="btn sm" onclick="selectChartSymbol('NSE:HDFCBANK')" id="pill_HDFCBANK">HDFCBANK</button>
    </div>

    <!-- Live Strategy Parameters & Confluence HUD -->
    <div id="chartParametersHud" style="background:var(--card);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;flex-direction:column;gap:10px">
      <!-- HUD Top Bar -->
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
        <div style="display:flex;align-items:center;gap:10px">
          <span style="font-size:12px;font-weight:700;color:var(--ink-3);text-transform:uppercase;letter-spacing:.06em">🎯 Strategy Confluence HUD:</span>
          <span id="hudActionBadge" class="badge sm ok" style="font-weight:700;font-size:11px">Analyzing Strategy Parameters…</span>
          <span id="hudScoreBadge" class="badge sm" style="font-weight:700;font-size:11px;background:var(--cell-2);color:var(--ink-2)">Score: --/100</span>
        </div>
        <div style="display:flex;align-items:center;gap:12px;font-size:11px;color:var(--ink-3)">
          <span id="hudPriceBadge" style="font-family:ui-monospace,monospace;font-weight:700;font-size:13px;color:var(--ink)">Mark: $--</span>
          <span id="hudUpdateTime">Live 3s Refresh</span>
        </div>
      </div>

      <!-- 6 Metrics Cards Grid -->
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px" id="hudCardsGrid">
        <!-- 9/21 EMA -->
        <div style="background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:9px 12px">
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:10.5px;color:var(--ink-3);font-weight:700;text-transform:uppercase">
            <span>📈 9/21 EMA Trend</span>
            <span id="hudEmaPill" class="badge sm" style="font-size:9px">--</span>
          </div>
          <div style="font-size:12.5px;font-weight:700;font-family:ui-monospace,monospace;color:var(--ink);margin-top:4px" id="hudEmaValues">$-- / $--</div>
          <div style="font-size:10px;color:var(--ink-3);margin-top:2px" id="hudEmaNote">Fast 9 vs Slow 21 EMA</div>
        </div>

        <!-- VWAP -->
        <div style="background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:9px 12px">
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:10.5px;color:var(--ink-3);font-weight:700;text-transform:uppercase">
            <span>🎯 Session VWAP</span>
            <span id="hudVwapPill" class="badge sm" style="font-size:9px">--</span>
          </div>
          <div style="font-size:12.5px;font-weight:700;font-family:ui-monospace,monospace;color:var(--ink);margin-top:4px" id="hudVwapVal">$--</div>
          <div style="font-size:10px;color:var(--ink-3);margin-top:2px" id="hudVwapNote">Institutional Fair Value</div>
        </div>

        <!-- 200 SMA -->
        <div style="background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:9px 12px">
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:10.5px;color:var(--ink-3);font-weight:700;text-transform:uppercase">
            <span>⚓ 200 SMA Filter</span>
            <span id="hudSmaPill" class="badge sm" style="font-size:9px">--</span>
          </div>
          <div style="font-size:12.5px;font-weight:700;font-family:ui-monospace,monospace;color:var(--ink);margin-top:4px" id="hudSmaVal">$--</div>
          <div style="font-size:10px;color:var(--ink-3);margin-top:2px" id="hudSmaNote">Macro Trend Regime</div>
        </div>

        <!-- 1H Range (ORB) -->
        <div style="background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:9px 12px">
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:10.5px;color:var(--ink-3);font-weight:700;text-transform:uppercase">
            <span>⏱️ 1H Opening Range</span>
            <span id="hudRangePill" class="badge sm" style="font-size:9px">--</span>
          </div>
          <div style="font-size:11.5px;font-weight:700;font-family:ui-monospace,monospace;color:var(--ink);margin-top:4px" id="hudRangeVals">H: $-- | L: $--</div>
          <div style="font-size:10px;color:var(--ink-3);margin-top:2px" id="hudRangeWidth">Width: --%</div>
        </div>

        <!-- Volume (RVOL) -->
        <div style="background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:9px 12px">
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:10.5px;color:var(--ink-3);font-weight:700;text-transform:uppercase">
            <span>⚡ Volume (RVOL)</span>
            <span id="hudRvolPill" class="badge sm" style="font-size:9px">--</span>
          </div>
          <div style="font-size:12.5px;font-weight:700;font-family:ui-monospace,monospace;color:var(--ink);margin-top:4px" id="hudRvolVal">--x Avg</div>
          <div style="font-size:10px;color:var(--ink-3);margin-top:2px" id="hudRvolNote">Req &ge; 1.2x for Breakout</div>
        </div>

        <!-- RSI & ATR -->
        <div style="background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:9px 12px">
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:10.5px;color:var(--ink-3);font-weight:700;text-transform:uppercase">
            <span>📊 RSI &amp; ATR Noise</span>
            <span id="hudRsiPill" class="badge sm" style="font-size:9px">--</span>
          </div>
          <div style="font-size:11.5px;font-weight:700;font-family:ui-monospace,monospace;color:var(--ink);margin-top:4px" id="hudRsiAtrVal">RSI: -- | ATR: $--</div>
          <div style="font-size:10px;color:var(--ink-3);margin-top:2px" id="hudAtrNote">Volatility Buffer</div>
        </div>
      </div>

      <!-- Strategy Triggers & Levels Banner -->
      <div style="background:var(--bg-2);border:1px solid var(--line);border-radius:8px;padding:8px 12px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;font-size:11.5px">
        <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
          <span style="color:var(--ink-3);font-weight:700;font-size:10px;text-transform:uppercase">Strategy Execution Levels:</span>
          <div>🟢 <b>Long Breakout:</b> <span style="font-family:ui-monospace,monospace;color:var(--good)" id="hudLongTrigger">>$--</span></div>
          <div>🔴 <b>Short Breakdown:</b> <span style="font-family:ui-monospace,monospace;color:var(--bad)" id="hudShortTrigger"><$--</span></div>
          <div>🛡️ <b>Planned SL:</b> <span style="font-family:ui-monospace,monospace;color:var(--bad)" id="hudPlannedSl">$--</span></div>
          <div>🎯 <b>1:2 Target:</b> <span style="font-family:ui-monospace,monospace;color:var(--good)" id="hudPlannedT1">$--</span></div>
          <div>🎯 <b>1:3 Target:</b> <span style="font-family:ui-monospace,monospace;color:var(--good)" id="hudPlannedT2">$--</span></div>
        </div>
        <div id="hudReasonsList" style="font-size:10.5px;color:var(--ink-2);display:flex;gap:4px;flex-wrap:wrap">
          <!-- Live Reasons pills injected -->
        </div>
      </div>
    </div>

    <!-- Chart Container -->
    <div style="height:560px;width:100%;background:var(--card);position:relative" id="chartContainer">
      <div id="tradingview_embed_box" style="width:100%;height:100%"></div>
    </div>
  </div>

  <!-- Active Trades & Positions -->
  <div style="margin-top:24px" class="panel">
    <h2>
      <span>⚡ Active Forex &amp; Crypto Positions</span>
      <span class="sub" id="posCount">(0 open)</span>
    </h2>
    <div style="background:var(--cell);border:1px solid var(--line);border-radius:8px;padding:9px 12px;margin:10px 0 14px 0;display:flex;align-items:center;justify-content:space-between;font-size:11.5px;color:var(--ink-2);flex-wrap:wrap;gap:8px">
      <span>🛡️ <b>Automated Trailing Stop-Loss Active:</b> Locks Breakeven at <b>+1.0R gain</b> (zero risk). At <b>+1.5R</b>, stop-loss dynamically trails mark price with a <b>1.0R buffer</b>.</span>
      <span class="badge sm ok" style="font-weight:700">● 24/7 Monitored</span>
    </div>
    <div style="overflow-x:auto">
      <table>
        <thead>
          <tr>
            <th>Trade ID</th>
            <th>Opened</th>
            <th>Symbol</th>
            <th>Side</th>
            <th class="num">Size</th>
            <th class="num">Entry Price</th>
            <th class="num">Mark Price</th>
            <th class="num">P&amp;L ($)</th>
            <th class="num">P&amp;L (%)</th>
            <th>Initial SL</th>
            <th>Active Trailed SL</th>
            <th>Target</th>
            <th>Trailing Status</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody id="posBody">
          <tr><td colspan="14" class="empty">No open Forex / Crypto positions. Click "Execute Trade" on any setup below to enter.</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Completed Trades History -->
  <div style="margin-top:24px" class="panel">
    <h2>
      <span>📜 Completed Trades History</span>
      <span class="sub" id="histCount">(0 closed)</span>
    </h2>
    <div style="overflow-x:auto">
      <table>
        <thead>
          <tr>
            <th>Trade ID</th>
            <th>Opened Time</th>
            <th>Closed Time</th>
            <th>Symbol</th>
            <th>Side</th>
            <th class="num">Size</th>
            <th class="num">Entry Price</th>
            <th class="num">Exit Price</th>
            <th class="num">Realized P&amp;L ($)</th>
            <th class="num">Return (%)</th>
            <th>Initial SL</th>
            <th>Exit Reason</th>
            <th>Executor</th>
          </tr>
        </thead>
        <tbody id="histBody">
          <tr><td colspan="13" class="empty">Loading trade history…</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- AI Post-Trade Autopsy & Continuous Learning Journal -->
  <div style="margin-top:24px" class="panel">
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
      <h2>
        <span>🧠 Continuous Learning &amp; AI Post-Trade Autopsy Journal</span>
        <span class="sub" id="reflectionsCount">(0 autopsies recorded)</span>
      </h2>
      <span style="font-size:11.5px;color:var(--ink-3)">
        Every closed trade is autonomously audited for root causes and actionable lessons to prevent repeating mistakes.
      </span>
    </div>

    <div id="reflectionsContainer" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:12px;margin-top:14px">
      <div class="empty" style="grid-column:1/-1">Loading trade autopsies &amp; continuous learning journal…</div>
    </div>
  </div>

  <!-- Multi-Strategy Setups Board -->
  <div style="margin-top:24px">
    <div style="display:flex;align-items:center;justify-content:space-between">
      <div>
        <h2 style="font-size:14px;letter-spacing:.04em;color:var(--ink);margin:0">
          🎯 Multi-Strategy Qualifying Signals (ORB + AVWAP + 9/21 EMA + 200 SMA)
        </h2>
        <div style="font-size:11.5px;color:var(--ink-3);margin-top:3px">
          Continuous 24/7 scanning across Bitcoin, Ethereum, Gold (XAUT), Solana, and Major Crypto Perpetuals.
        </div>
      </div>
      <span class="badge sm approve" id="setupCountBadge">0 Setups Available</span>
    </div>

    <div class="signals-container" id="signalsGrid">
      <div class="empty" style="grid-column:1/-1;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:24px">
        Scanning Delta Exchange for qualifying breakouts…
      </div>
    </div>
  </div>

  <!-- 4-Agent Council Debate & Live News Modal -->
  <div id="councilModal" class="council-overlay">
    <div class="council-modal">
      <div style="background:var(--cell);border-bottom:1px solid var(--line);padding:14px 20px;display:flex;align-items:center;justify-content:space-between">
        <div style="display:flex;align-items:center;gap:12px">
          <span style="font-size:24px">🤖</span>
          <div>
            <div style="font-size:15px;font-weight:700;color:var(--ink);letter-spacing:0.02em" id="cmTitle">
              4-Agent AI Council Debate &amp; Real-Time Market News
            </div>
            <div style="font-size:11px;color:var(--ink-3)" id="cmSubtitle">
              Specialist multi-agent council (Technical Analyst, Bull, Bear, Chief Trader) powered by Gemini 3.8 Flash
            </div>
          </div>
        </div>
        <button class="btn sm" onclick="closeCouncilModal()" style="padding:5px 12px;font-size:12px">✕ Close</button>
      </div>

      <div id="cmContent" style="padding:20px;display:flex;flex-direction:column;gap:16px">
        <!-- Injected dynamically -->
      </div>
    </div>
  </div>

  <!-- Fullscreen Chart Modal -->
  <div id="chartFullscreenModal" class="council-overlay" style="display:none">
    <div style="max-width:96vw;height:90vh;margin:20px auto;background:var(--card);border:1px solid var(--line-2);border-radius:12px;overflow:hidden;display:flex;flex-direction:column;box-shadow:0 24px 60px rgba(0,0,0,0.85)">
      <div style="background:var(--cell);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;align-items:center;justify-content:space-between">
        <div style="display:flex;align-items:center;gap:10px">
          <span style="font-size:20px">📊</span>
          <span style="font-weight:700;font-size:15px;color:var(--ink)" id="fsChartTitle">BTCUSD — Real-Time Technical Chart</span>
        </div>
        <button class="btn sm" onclick="closeChartModal()" style="padding:5px 12px">✕ Close Fullscreen</button>
      </div>
      <div id="fsChartContainer" style="flex:1;width:100%;background:#04070f;position:relative"></div>
    </div>
  </div>

</main>

<script>
let TICKERS = {};
let SETUPS = [];
let POSITIONS = [];
let ALL_TRADES = [];
let HISTORY = [];
let COUNCIL_ENABLED = true;

function fmt(n, d = 2) {
  if (n === null || n === undefined || isNaN(n)) return "0.00";
  return Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

async function fetchStatus() {
  try {
    const res = await fetch("/api/forex/status");
    if (!res.ok) return;
    const st = await res.json();
    document.getElementById("statusMessage").textContent = st.message || "Online";
    document.getElementById("modePill").textContent = (st.mode || "PAPER").toUpperCase() + " MODE";
    const badge = document.getElementById("statusBadge");
    if (st.connected) {
      badge.className = "badge ok";
      badge.innerHTML = '<span class="g">●</span> Delta Exchange Online';
    } else {
      badge.className = "badge veto";
      badge.innerHTML = '<span class="g">●</span> Delta Offline';
    }
  } catch (err) {
    console.warn("Status fetch failed:", err);
  }
}

async function fetchCouncilStatus() {
  try {
    const res = await fetch("/api/forex/council_status");
    if (!res.ok) return;
    const data = await res.json();
    COUNCIL_ENABLED = !!data.council_enabled;
    const el = document.getElementById("councilText");
    if (el) {
      el.textContent = COUNCIL_ENABLED ? "ACTIVE" : "BYPASSED";
      el.style.color = COUNCIL_ENABLED ? "var(--good)" : "var(--warn)";
    }
  } catch (err) {
    console.warn("Council status fetch failed:", err);
  }
}

async function toggleCouncil() {
  try {
    const res = await fetch("/api/forex/council_toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !COUNCIL_ENABLED }),
    });
    const data = await res.json();
    COUNCIL_ENABLED = !!data.council_enabled;
    const el = document.getElementById("councilText");
    if (el) {
      el.textContent = COUNCIL_ENABLED ? "ACTIVE" : "BYPASSED";
      el.style.color = COUNCIL_ENABLED ? "var(--good)" : "var(--warn)";
    }
    alert(`4-Agent AI Council Gate is now ${COUNCIL_ENABLED ? 'ACTIVE (Trades require >=8/10 consensus)' : 'BYPASSED (Trades execute purely on Quant)'}`);
  } catch (err) {
    alert("Failed to toggle council: " + err.message);
  }
}

async function fetchTickers() {
  try {
    const res = await fetch("/api/forex/tickers");
    if (!res.ok) return;
    TICKERS = await res.json();
    renderTickers();
    document.getElementById("tickerUpdateTime").textContent = "Live: " + new Date().toLocaleTimeString();
  } catch (err) {
    console.warn("Tickers fetch failed:", err);
  }
}

let ACTIVE_CHART_SYMBOL = "BTCUSD";
let ACTIVE_CHART_TF = "5";

const ASSET_NAMES = {
  "BTCUSD": "Bitcoin / USD Perpetual (BTCUSD)",
  "ETHUSD": "Ethereum / USD Perpetual (ETHUSD)",
  "SOLUSD": "Solana / USD Perpetual (SOLUSD)",
  "XAUTUSD": "Tether Gold / USD Perpetual (XAUTUSD)",
  "XRPUSD": "Ripple / USD Perpetual (XRPUSD)",
  "BNBUSD": "Binance Coin / USD Perpetual (BNBUSD)",
  "DOGEUSD": "Dogecoin / USD Perpetual (DOGEUSD)",
  "ADAUSD": "Cardano / USD Perpetual (ADAUSD)",
  "NSE:NIFTY": "NIFTY 50 Index (NSE)",
  "NSE:BANKNIFTY": "BANK NIFTY Index (NSE)",
  "NSE:RELIANCE": "Reliance Industries Ltd (NSE)",
  "NSE:TCS": "Tata Consultancy Services (NSE)",
  "NSE:HDFCBANK": "HDFC Bank Ltd (NSE)",
};

function getTradingViewSymbol(sym) {
  const upper = (sym || "BTCUSD").toUpperCase().trim();
  const mapping = {
    "BTCUSD": "BINANCE:BTCUSDT",
    "ETHUSD": "BINANCE:ETHUSDT",
    "SOLUSD": "BINANCE:SOLUSDT",
    "XAUTUSD": "TVC:GOLD",
    "XRPUSD": "BINANCE:XRPUSDT",
    "DOGEUSD": "BINANCE:DOGEUSDT",
    "BNBUSD": "BINANCE:BNBUSDT",
    "ADAUSD": "BINANCE:ADAUSDT",
    "NIFTY": "NSE:NIFTY",
    "BANKNIFTY": "NSE:BANKNIFTY",
    "RELIANCE": "NSE:RELIANCE",
    "TCS": "NSE:TCS",
    "INFY": "NSE:INFY",
    "HDFCBANK": "NSE:HDFCBANK",
  };
  if (mapping[upper]) return mapping[upper];
  if (upper.startsWith("NSE:") || upper.startsWith("BSE:") || upper.startsWith("BINANCE:") || upper.startsWith("TVC:") || upper.startsWith("COINBASE:")) {
    return upper;
  }
  return "BINANCE:" + upper + "T";
}

function selectChartSymbol(sym, shouldScroll = false) {
  if (!sym) return;
  ACTIVE_CHART_SYMBOL = sym.toUpperCase().trim();

  // Update title & subtitle
  const titleEl = document.getElementById("chartTitle");
  if (titleEl) {
    titleEl.textContent = ASSET_NAMES[ACTIVE_CHART_SYMBOL] || (ACTIVE_CHART_SYMBOL + " Live Candlestick Chart");
  }
  const fsTitle = document.getElementById("fsChartTitle");
  if (fsTitle) {
    fsTitle.textContent = (ASSET_NAMES[ACTIVE_CHART_SYMBOL] || ACTIVE_CHART_SYMBOL) + " — Real-Time Technical Chart";
  }

  // Update quick switcher pills active state
  document.querySelectorAll("[id^='pill_']").forEach(btn => {
    btn.style.borderColor = "var(--line)";
    btn.style.color = "var(--ink)";
    btn.style.background = "var(--cell)";
  });
  const rawId = ACTIVE_CHART_SYMBOL.replace("NSE:", "").replace("BINANCE:", "");
  const activePill = document.getElementById("pill_" + rawId) || document.getElementById("pill_" + ACTIVE_CHART_SYMBOL);
  if (activePill) {
    activePill.style.borderColor = "var(--accent)";
    activePill.style.color = "var(--accent)";
    activePill.style.background = "var(--accent-soft)";
  }

  // Re-render embedded chart
  renderTradingViewWidget("tradingview_embed_box", ACTIVE_CHART_SYMBOL, ACTIVE_CHART_TF);

  // Update URL parameter without reload
  const newUrl = new URL(window.location);
  newUrl.searchParams.set("symbol", ACTIVE_CHART_SYMBOL);
  window.history.replaceState({}, "", newUrl);

  // Re-render ticker cards to highlight active
  renderTickers();

  // Fetch live strategy parameters for the active chart symbol
  fetchActiveSymbolParameters(ACTIVE_CHART_SYMBOL);

  // Smooth scroll to chart panel if requested
  if (shouldScroll) {
    const panel = document.getElementById("chartStationPanel");
    if (panel) {
      panel.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }
}

async function fetchActiveSymbolParameters(sym) {
  try {
    const s = sym || ACTIVE_CHART_SYMBOL;
    const res = await fetch(`/api/forex/parameters?symbol=${encodeURIComponent(s)}`);
    if (!res.ok) return;
    const d = await res.json();
    if (!d.ok) return;

    const dec = d.decimals || 2;
    // Action & Score badges
    const actBadge = document.getElementById("hudActionBadge");
    if (actBadge) {
      actBadge.className = `badge sm ${d.action_badge}`;
      actBadge.textContent = d.action;
    }
    const scoreBadge = document.getElementById("hudScoreBadge");
    if (scoreBadge) {
      scoreBadge.textContent = `Score: ${d.score}/100 (Conviction ${d.conviction}/10)`;
    }
    const priceBadge = document.getElementById("hudPriceBadge");
    if (priceBadge) {
      priceBadge.textContent = `Mark: $${fmt(d.mark_price, dec)}`;
    }
    const updateEl = document.getElementById("hudUpdateTime");
    if (updateEl) {
      updateEl.textContent = `Live: ${d.updated_at || new Date().toLocaleTimeString()}`;
    }

    // 1. EMA
    const isEmaBull = d.ema_9 > d.ema_21;
    const emaPill = document.getElementById("hudEmaPill");
    if (emaPill) {
      emaPill.className = `badge sm ${isEmaBull ? 'bull' : 'bear'}`;
      emaPill.textContent = isEmaBull ? "🟢 Bullish" : "🔴 Bearish";
    }
    const emaVal = document.getElementById("hudEmaValues");
    if (emaVal) emaVal.textContent = `$${fmt(d.ema_9, dec)} / $${fmt(d.ema_21, dec)}`;
    const emaNote = document.getElementById("hudEmaNote");
    if (emaNote) emaNote.textContent = d.ema_trend;

    // 2. VWAP
    const isAboveVwap = d.mark_price >= d.vwap;
    const vwapPill = document.getElementById("hudVwapPill");
    if (vwapPill) {
      vwapPill.className = `badge sm ${isAboveVwap ? 'bull' : 'bear'}`;
      vwapPill.textContent = isAboveVwap ? "Above" : "Below";
    }
    const vwapVal = document.getElementById("hudVwapVal");
    if (vwapVal) vwapVal.textContent = `$${fmt(d.vwap, dec)}`;
    const vwapNote = document.getElementById("hudVwapNote");
    if (vwapNote) vwapNote.textContent = d.vwap_status;

    // 3. 200 SMA
    const isAboveSma = d.mark_price >= d.sma_200;
    const smaPill = document.getElementById("hudSmaPill");
    if (smaPill) {
      smaPill.className = `badge sm ${isAboveSma ? 'bull' : 'bear'}`;
      smaPill.textContent = isAboveSma ? "Uptrend" : "Downtrend";
    }
    const smaVal = document.getElementById("hudSmaVal");
    if (smaVal) smaVal.textContent = `$${fmt(d.sma_200, dec)}`;
    const smaNote = document.getElementById("hudSmaNote");
    if (smaNote) smaNote.textContent = d.sma_status;

    // 4. 1H Range
    const rangePill = document.getElementById("hudRangePill");
    if (rangePill) {
      rangePill.className = `badge sm ${d.in_range ? 'caution' : 'approve'}`;
      rangePill.textContent = d.in_range ? "Trapped In Range" : "Breakout";
    }
    const rangeVals = document.getElementById("hudRangeVals");
    if (rangeVals) rangeVals.textContent = `H: $${fmt(d.range_high, dec)} | L: $${fmt(d.range_low, dec)}`;
    const rangeWidth = document.getElementById("hudRangeWidth");
    if (rangeWidth) rangeWidth.textContent = `Width: ${d.range_width_pct}% ${d.in_range ? '(Consolidating)' : '(Expanding)'}`;

    // 5. Volume RVOL
    const isVolSurge = d.rvol >= 1.2;
    const rvolPill = document.getElementById("hudRvolPill");
    if (rvolPill) {
      rvolPill.className = `badge sm ${isVolSurge ? 'bull' : 'caution'}`;
      rvolPill.textContent = isVolSurge ? "Surge" : "Subdued";
    }
    const rvolVal = document.getElementById("hudRvolVal");
    if (rvolVal) rvolVal.textContent = `${d.rvol}x Volume`;
    const rvolNote = document.getElementById("hudRvolNote");
    if (rvolNote) rvolNote.textContent = d.rvol_status;

    // 6. RSI & ATR
    const rsiPill = document.getElementById("hudRsiPill");
    if (rsiPill) {
      rsiPill.className = `badge sm ${d.rsi >= 55 ? 'bull' : (d.rsi <= 45 ? 'bear' : 'ok')}`;
      rsiPill.textContent = d.rsi >= 55 ? "Bullish" : (d.rsi <= 45 ? "Bearish" : "Neutral");
    }
    const rsiAtrVal = document.getElementById("hudRsiAtrVal");
    if (rsiAtrVal) rsiAtrVal.textContent = `RSI: ${d.rsi} | ATR: $${fmt(d.atr, dec)}`;
    const atrNote = document.getElementById("hudAtrNote");
    if (atrNote) atrNote.textContent = `Noise Cushion: $${fmt(d.atr * 1.5, dec)}`;

    // Triggers & Levels
    const longTrig = document.getElementById("hudLongTrigger");
    if (longTrig) longTrig.textContent = `> $${fmt(d.long_trigger, dec)}`;
    const shortTrig = document.getElementById("hudShortTrigger");
    if (shortTrig) shortTrig.textContent = `< $${fmt(d.short_trigger, dec)}`;
    const slEl = document.getElementById("hudPlannedSl");
    if (slEl) slEl.textContent = `$${fmt(d.planned_sl, dec)}`;
    const t1El = document.getElementById("hudPlannedT1");
    if (t1El) t1El.textContent = `$${fmt(d.planned_target1, dec)}`;
    const t2El = document.getElementById("hudPlannedT2");
    if (t2El) t2El.textContent = `$${fmt(d.planned_target2, dec)}`;

    // Reasons pills
    const reasonsCont = document.getElementById("hudReasonsList");
    if (reasonsCont && d.reasons) {
      reasonsCont.innerHTML = d.reasons.map(r => `<span style="background:var(--cell-2);border:1px solid var(--line);border-radius:4px;padding:2px 6px;color:var(--ink-2);font-size:10px">✓ ${r}</span>`).join("");
    }
  } catch (err) {
    console.warn("Failed to fetch symbol parameters:", err);
  }
}

function setChartTf(tf) {
  ACTIVE_CHART_TF = tf;
  document.querySelectorAll("#tfSelector button").forEach(b => b.removeAttribute("aria-pressed"));
  const btn = document.getElementById("tf_" + tf);
  if (btn) btn.setAttribute("aria-pressed", "true");
  renderTradingViewWidget("tradingview_embed_box", ACTIVE_CHART_SYMBOL, ACTIVE_CHART_TF);
  const fsModal = document.getElementById("chartFullscreenModal");
  if (fsModal && fsModal.style.display === "block") {
    renderTradingViewWidget("fsChartContainer", ACTIVE_CHART_SYMBOL, ACTIVE_CHART_TF);
  }
}

function searchSymbolChart() {
  const inp = document.getElementById("symbolInput");
  if (!inp || !inp.value.trim()) return;
  selectChartSymbol(inp.value.trim(), true);
  inp.value = "";
}

function renderTradingViewWidget(containerId, sym, tf) {
  const tvSym = getTradingViewSymbol(sym);
  const container = document.getElementById(containerId);
  if (!container) return;
  container.innerHTML = "";

  const widgetUrl = `https://s.tradingview.com/widgetembed/?frameElementId=tradingview_${Date.now()}&symbol=${encodeURIComponent(tvSym)}&interval=${tf}&hidesidetoolbar=0&symboledit=1&saveimage=1&toolbarbg=0b1220&studies=RSI%40tv-basicstudies%1FVolume%40tv-basicstudies%1FMASimple%40tv-basicstudies&theme=dark&style=1&timezone=Asia%2FKolkata`;

  const iframe = document.createElement("iframe");
  iframe.style.width = "100%";
  iframe.style.height = "100%";
  iframe.style.border = "none";
  iframe.src = widgetUrl;
  container.appendChild(iframe);
}

function openChartModal() {
  const modal = document.getElementById("chartFullscreenModal");
  if (!modal) return;
  modal.style.display = "block";
  document.body.style.overflow = "hidden";
  renderTradingViewWidget("fsChartContainer", ACTIVE_CHART_SYMBOL, ACTIVE_CHART_TF);
}

function closeChartModal() {
  const modal = document.getElementById("chartFullscreenModal");
  if (!modal) return;
  modal.style.display = "none";
  document.body.style.overflow = "auto";
  const fs = document.getElementById("fsChartContainer");
  if (fs) fs.innerHTML = "";
}

function openDedicatedChartPage() {
  window.open(`/chart?symbol=${encodeURIComponent(ACTIVE_CHART_SYMBOL)}`, "_blank");
}

function renderTickers() {
  const grid = document.getElementById("forexGrid");
  const keys = Object.keys(TICKERS);
  if (!keys.length) return;

  grid.innerHTML = "";
  keys.forEach(sym => {
    const t = TICKERS[sym];
    const isUp = t.change_24h >= 0;
    const isActive = ACTIVE_CHART_SYMBOL === sym;
    const card = document.createElement("div");
    card.className = "forex-card" + (isActive ? " active" : "");
    card.style.cursor = "pointer";
    card.onclick = () => selectChartSymbol(sym, true);
    card.title = `Click to load live interactive chart for ${sym}`;

    const decimals = t.mark_price > 500 ? 2 : (t.mark_price > 10 ? 3 : 4);
    card.innerHTML = `
      <div class="sym">
        <span>${sym}</span>
        <span class="chg ${isUp ? 'up' : 'down'}">${isUp ? '+' : ''}${fmt(t.change_24h, 2)}%</span>
      </div>
      <div class="px">$${fmt(t.mark_price, decimals)}</div>
      <div class="sub" style="display:flex;justify-content:space-between">
        <span>H: $${fmt(t.high, decimals)}</span>
        <span>L: $${fmt(t.low, decimals)}</span>
      </div>
      <div class="chart-hint">
        <span>📊 View Live Chart</span>
        <span style="margin-left:auto">${isActive ? '● Active' : '↗'}</span>
      </div>
    `;
    grid.appendChild(card);
  });
}

async function fetchPositionsAndHistory() {
  try {
    const [posRes, histRes] = await Promise.all([
      fetch("/api/forex/positions"),
      fetch("/api/forex/history")
    ]);
    if (posRes.ok) {
      POSITIONS = await posRes.json();
      renderPositions();
    }
    if (histRes.ok) {
      ALL_TRADES = await histRes.json();
      HISTORY = ALL_TRADES.filter(p => p.status === "CLOSED").slice().reverse();
      renderHistory();
    }
    renderKPIs();
  } catch (err) {
    console.warn("Positions/History fetch failed:", err);
  }
}

// Backward compatible alias
async function fetchPositions() {
  return fetchPositionsAndHistory();
}

function renderPositions() {
  const tbody = document.getElementById("posBody");
  document.getElementById("posCount").textContent = `(${POSITIONS.length} open)`;
  if (!POSITIONS.length) {
    tbody.innerHTML = '<tr><td colspan="14" class="empty">No open Forex / Crypto positions. Click "Execute Trade" on any setup below to enter.</td></tr>';
    return;
  }

  tbody.innerHTML = "";
  POSITIONS.forEach(p => {
    const tr = document.createElement("tr");
    const isWin = p.pnl >= 0;
    const pnlClass = isWin ? "up" : "down";
    const decimals = p.entry_price > 500 ? 2 : (p.entry_price > 10 ? 3 : 4);
    const sideBadge = p.side === "BUY" ?
      '<span class="badge sm bull">BUY</span>' :
      '<span class="badge sm bear">SELL</span>';

    const initialSl = p.initial_stop != null ? p.initial_stop : p.stop_loss;
    const currentSl = p.stop_loss;
    const isBuy = p.side === "BUY";
    const beLevel = p.entry_price;
    const rDist = Math.abs(p.entry_price - initialSl) || (p.entry_price * 0.002);
    const beTriggerPrice = isBuy ? (p.entry_price + rDist) : (p.entry_price - rDist);
    const trailTriggerPrice = isBuy ? (p.entry_price + 1.5 * rDist) : (p.entry_price - 1.5 * rDist);
    const isBeLocked = (isBuy && currentSl >= beLevel - 0.01) || (!isBuy && currentSl <= beLevel + 0.01);
    const isTrailed = (isBuy && currentSl > beLevel + 0.01) || (!isBuy && currentSl < beLevel - 0.01);

    let trailSlHtml = "";
    let statusNoteHtml = "";

    if (isTrailed) {
      trailSlHtml = `
        <div style="font-family:ui-monospace,monospace;font-weight:700;color:var(--good);font-size:12px">🚀 $${fmt(currentSl, decimals)}</div>
        <div style="font-size:9px;color:var(--good);font-weight:600">Trailed (+1.0R buffer)</div>
      `;
      statusNoteHtml = `<span class="badge sm bull" style="font-size:10px">🚀 Trailing Active</span>`;
    } else if (isBeLocked) {
      trailSlHtml = `
        <div style="font-family:ui-monospace,monospace;font-weight:700;color:var(--good);font-size:12px">🛡️ $${fmt(currentSl, decimals)}</div>
        <div style="font-size:9px;color:var(--good);font-weight:600">Breakeven Locked</div>
      `;
      statusNoteHtml = `<span class="badge sm ok" style="font-size:10px">🛡️ Zero Risk (BE)</span>`;
    } else {
      trailSlHtml = `
        <div style="font-family:ui-monospace,monospace;font-weight:600;color:var(--ink-2);font-size:12px">$${fmt(currentSl, decimals)}</div>
        <div style="font-size:9px;color:var(--ink-3)">BE @ 1.0R ($${fmt(beTriggerPrice, decimals)})</div>
      `;
      statusNoteHtml = `<span class="badge sm" style="background:var(--cell);color:var(--ink-2);font-size:10px">⏳ Awaiting 1.0R BE</span>`;
    }

    tr.innerHTML = `
      <td class="mono" style="font-size:11px;color:var(--ink-3)">#${p.id}</td>
      <td style="font-size:11px;color:var(--ink-2)">${p.entry_time ? p.entry_time.split(' ')[1] : '—'}</td>
      <td style="font-weight:700;color:var(--ink)">${p.symbol}</td>
      <td>${sideBadge}</td>
      <td class="num mono">${p.size}</td>
      <td class="num mono">$${fmt(p.entry_price, decimals)}</td>
      <td class="num mono" style="font-weight:700">$${fmt(p.current_price, decimals)}</td>
      <td class="num mono ${pnlClass}" style="font-weight:700">${p.pnl >= 0 ? '+' : ''}$${fmt(p.pnl, 2)}</td>
      <td class="num mono ${pnlClass}">${p.pnl_pct >= 0 ? '+' : ''}${fmt(p.pnl_pct, 2)}%</td>
      <td class="num mono" style="color:var(--bad);font-weight:600">$${fmt(initialSl, decimals)}</td>
      <td class="num mono">${trailSlHtml}</td>
      <td class="num mono" style="color:var(--good)">$${fmt(p.target, decimals)}</td>
      <td>${statusNoteHtml}</td>
      <td>
        <button class="btn sm" onclick="closePosition(${p.id})" style="border-color:var(--bad);color:var(--bad);padding:2px 7px;font-size:10px">
          ✕ Square Off
        </button>
      </td>
    `;
    tbody.appendChild(tr);
  });
}

function renderKPIs() {
  const totalTrades = ALL_TRADES.length;
  const openCount = POSITIONS.length;
  const closedTrades = ALL_TRADES.filter(p => p.status === "CLOSED");
  const closedCount = closedTrades.length;

  const openPnl = POSITIONS.reduce((acc, p) => acc + (Number(p.pnl) || 0), 0);
  const realizedPnl = closedTrades.reduce((acc, p) => acc + (Number(p.pnl) || 0), 0);
  const totalPnl = openPnl + realizedPnl;

  const wins = closedTrades.filter(p => (Number(p.pnl) || 0) > 0).length;
  const losses = closedTrades.filter(p => (Number(p.pnl) || 0) < 0).length;
  const winRate = closedCount > 0 ? ((wins / closedCount) * 100).toFixed(1) : "0.0";

  const totEl = document.getElementById("kpiTotalTrades");
  if (totEl) totEl.textContent = totalTrades;
  const totSub = document.getElementById("kpiTotalTradesSub");
  if (totSub) totSub.textContent = `${openCount} Active • ${closedCount} Closed`;

  const realEl = document.getElementById("kpiRealizedPnl");
  if (realEl) {
    realEl.className = "v " + (realizedPnl >= 0 ? "up" : "down");
    realEl.textContent = (realizedPnl >= 0 ? "+" : "") + "$" + fmt(realizedPnl, 2);
  }
  const winEl = document.getElementById("kpiWinRate");
  if (winEl) winEl.textContent = `Win Rate: ${winRate}% (${wins}W / ${losses}L)`;

  const openEl = document.getElementById("kpiOpenPnl");
  if (openEl) {
    openEl.className = "v " + (openPnl >= 0 ? "up" : "down");
    openEl.textContent = (openPnl >= 0 ? "+" : "") + "$" + fmt(openPnl, 2);
  }
  const openSub = document.getElementById("kpiOpenCount");
  if (openSub) openSub.textContent = `${openCount} active position${openCount === 1 ? '' : 's'}`;

  const totalEl = document.getElementById("kpiTotalPnl");
  if (totalEl) {
    totalEl.className = "v hero " + (totalPnl >= 0 ? "up" : "down");
    totalEl.textContent = (totalPnl >= 0 ? "+" : "") + "$" + fmt(totalPnl, 2);
  }

  renderChallenge(realizedPnl, openPnl);
}

function renderChallenge(realizedPnl, openPnl) {
  const startCap = 100.0;
  const currentEquity = Math.max(0, startCap + realizedPnl + openPnl);
  const targetMilestone = 1000.0;
  const gainDollar = currentEquity - startCap;
  const gainPct = (gainDollar / startCap) * 100.0;
  const progress = Math.min(100.0, Math.max(0.0, ((currentEquity - startCap) / (targetMilestone - startCap)) * 100.0));

  const eqText = document.getElementById("challengeEquityText");
  if (eqText) {
    eqText.className = gainDollar >= 0 ? "up" : "down";
    eqText.textContent = `Current Equity: $${fmt(currentEquity, 2)} (${gainDollar >= 0 ? '+' : ''}${fmt(gainPct, 1)}%)`;
  }

  const bar = document.getElementById("challengeProgressBar");
  if (bar) bar.style.width = `${progress.toFixed(1)}%`;

  const dynRisk = Math.max(1.0, currentEquity * 0.025);
  const dailyLimit = Math.max(5.0, currentEquity * 0.06);

  const riskEl = document.getElementById("statDynamicRisk");
  if (riskEl) riskEl.textContent = `$${fmt(dynRisk, 2)} (2.5%)`;

  const limitEl = document.getElementById("statDailyLimit");
  if (limitEl) limitEl.textContent = `-$${fmt(dailyLimit, 2)} (6.0%)`;

  const progEl = document.getElementById("statProgressPct");
  if (progEl) progEl.textContent = `${progress.toFixed(1)}%`;

  const badge = document.getElementById("challengeStatusBadge");
  if (badge) {
    if (currentEquity <= 50.0) {
      badge.className = "badge veto";
      badge.textContent = "⚠️ CAPITAL CRITICAL (<$50)";
    } else if (gainDollar < -dailyLimit) {
      badge.className = "badge caution";
      badge.textContent = "🛑 DAILY DRAWDOWN DEFENSE";
    } else {
      badge.className = "badge ok";
      badge.textContent = "🛡️ SURVIVAL KERNEL ACTIVE";
    }
  }
}

function renderHistory() {
  const tbody = document.getElementById("histBody");
  if (!tbody) return;
  const countEl = document.getElementById("histCount");
  if (countEl) countEl.textContent = `(${HISTORY.length} closed)`;

  if (!HISTORY.length) {
    tbody.innerHTML = '<tr><td colspan="13" class="empty">No completed trades recorded yet.</td></tr>';
    return;
  }

  tbody.innerHTML = "";
  HISTORY.forEach(p => {
    const tr = document.createElement("tr");
    const isWin = (Number(p.pnl) || 0) >= 0;
    const pnlClass = isWin ? "up" : "down";
    const decimals = p.entry_price > 500 ? 2 : (p.entry_price > 10 ? 3 : 4);
    const sideBadge = p.side === "BUY" ?
      '<span class="badge sm bull">BUY</span>' :
      '<span class="badge sm bear">SELL</span>';

    const exitPrice = p.exit_price != null ? p.exit_price : p.current_price;
    const initialSl = p.initial_stop != null ? p.initial_stop : p.stop_loss;

    let exitReasonBadge = '<span class="badge sm">Closed</span>';
    const reasonStr = (p.exit_reason || "").toLowerCase();
    if (reasonStr.includes("target")) {
      exitReasonBadge = '<span class="badge sm bull">🎯 Target Hit</span>';
    } else if (reasonStr.includes("stop") && isWin) {
      exitReasonBadge = '<span class="badge sm bull">🚀 Trailed Profit</span>';
    } else if (reasonStr.includes("stop")) {
      exitReasonBadge = '<span class="badge sm bear">🛑 Stop Hit</span>';
    } else if (reasonStr.includes("breakeven") || reasonStr.includes("be")) {
      exitReasonBadge = '<span class="badge sm ok">🛡️ Breakeven</span>';
    } else if (reasonStr.includes("manual") || reasonStr.includes("square")) {
      exitReasonBadge = '<span class="badge sm caution">⚡ Square-Off</span>';
    }

    const executorBadge = (p.executor && p.executor.includes("Council")) ?
      '<span class="badge sm approve" style="font-size:9.5px">🤖 AI Council</span>' :
      '<span class="badge sm" style="font-size:9.5px;color:var(--ink-3)">⚡ Quant Engine</span>';

    tr.innerHTML = `
      <td class="mono" style="font-size:11px;color:var(--ink-3)">#${p.id}</td>
      <td style="font-size:11px;color:var(--ink-2)">${p.entry_time || '—'}</td>
      <td style="font-size:11px;color:var(--ink-2)">${p.exit_time || '—'}</td>
      <td style="font-weight:700;color:var(--ink)">${p.symbol}</td>
      <td>${sideBadge}</td>
      <td class="num mono">${p.size}</td>
      <td class="num mono">$${fmt(p.entry_price, decimals)}</td>
      <td class="num mono" style="font-weight:700">$${fmt(exitPrice, decimals)}</td>
      <td class="num mono ${pnlClass}" style="font-weight:700">${(p.pnl || 0) >= 0 ? '+' : ''}$${fmt(p.pnl || 0, 2)}</td>
      <td class="num mono ${pnlClass}">${(p.pnl_pct || 0) >= 0 ? '+' : ''}${fmt(p.pnl_pct || 0, 2)}%</td>
      <td class="num mono" style="color:var(--bad)">$${fmt(initialSl, decimals)}</td>
      <td>${exitReasonBadge}</td>
      <td>${executorBadge}</td>
    `;
    tbody.appendChild(tr);
  });
}

let REFLECTIONS = [];

async function fetchReflections() {
  try {
    const res = await fetch("/api/forex/reflections");
    if (!res.ok) return;
    REFLECTIONS = await res.json();
    renderReflections();
  } catch (err) {
    console.warn("Reflections fetch failed:", err);
  }
}

function renderReflections() {
  const container = document.getElementById("reflectionsContainer");
  if (!container) return;
  const countEl = document.getElementById("reflectionsCount");
  if (countEl) countEl.textContent = `(${REFLECTIONS.length} autopsies recorded)`;

  if (!REFLECTIONS.length) {
    container.innerHTML = '<div class="empty" style="grid-column:1/-1;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px">No trade autopsies recorded yet. Every completed trade automatically undergoes an AI post-mortem autopsy to extract root causes and actionable lessons.</div>';
    return;
  }

  container.innerHTML = "";
  REFLECTIONS.slice().reverse().forEach(r => {
    const card = document.createElement("div");
    card.style.background = "var(--card)";
    card.style.border = "1px solid var(--line)";
    card.style.borderRadius = "10px";
    card.style.padding = "14px";
    card.style.display = "flex";
    card.style.flexDirection = "column";
    card.style.gap = "8px";
    card.style.boxShadow = "var(--shadow)";

    const isWin = r.outcome === "WIN";
    const isLoss = r.outcome === "LOSS";
    const outcomeBadge = isWin ?
      '<span class="badge sm bull" style="font-weight:700">🟢 WIN</span>' :
      (isLoss ? '<span class="badge sm bear" style="font-weight:700">🔴 LOSS</span>' : '<span class="badge sm ok" style="font-weight:700">⚪ BREAKEVEN</span>');

    const pnlClass = isWin ? "up" : (isLoss ? "down" : "");

    card.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);padding-bottom:8px">
        <div style="display:flex;align-items:center;gap:8px">
          <span style="font-weight:800;font-size:14px;color:var(--ink)">${r.symbol}</span>
          <span class="badge sm" style="font-size:10px">${r.side}</span>
          ${outcomeBadge}
        </div>
        <div style="font-family:ui-monospace,monospace;font-weight:700;font-size:13px" class="${pnlClass}">
          ${(r.pnl || 0) >= 0 ? '+' : ''}$${fmt(r.pnl, 2)} (${(r.pnl_pct || 0) >= 0 ? '+' : ''}${fmt(r.pnl_pct, 2)}%)
        </div>
      </div>

      <div style="font-size:11.5px;color:var(--ink-2);line-height:1.4">
        <b>🔍 Root Cause:</b> ${r.root_cause || '—'}
      </div>

      <div style="background:rgba(99,102,241,0.08);border:1px solid rgba(99,102,241,0.25);border-radius:6px;padding:8px 10px;font-size:11.5px;color:var(--ink);line-height:1.4">
        💡 <b>Actionable Lesson Learned:</b> ${r.lesson_learned || '—'}
      </div>

      <div style="font-size:11px;color:var(--ink-3);display:flex;justify-content:space-between;align-items:center;padding-top:4px">
        <span>🛡️ <b>Rule:</b> ${r.rule_recommendation || '—'}</span>
        <span style="font-family:ui-monospace,monospace">${r.timestamp || ''}</span>
      </div>
    `;
    container.appendChild(card);
  });
}

async function resetChallenge() {
  if (!confirm("Are you sure you want to reset the Forex Paper Trading Desk to a clean $100 starting balance?\\n\\nThis will archive existing paper positions and restart the $100 -> $1,000 Challenge.")) return;
  try {
    const res = await fetch("/api/forex/reset_challenge", {
      method: "POST",
      headers: { "Content-Type": "application/json" }
    });
    const data = await res.json();
    alert(data.message || "Challenge reset to $100.00.");
    fetchPositionsAndHistory();
    fetchReflections();
  } catch (err) {
    alert("Reset failed: " + err.message);
  }
}

async function runScan() {
  const btn = document.getElementById("btnScan");
  btn.disabled = true;
  btn.textContent = "⏳ Running Strategy Analysis…";
  try {
    const res = await fetch("/api/forex/scan");
    if (!res.ok) throw new Error("Scan request failed");
    SETUPS = await res.json();
    renderSetups();
  } catch (err) {
    alert("Scan failed: " + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "⚡ Scan Forex & Crypto Setups";
  }
}

function renderSetups() {
  const container = document.getElementById("signalsGrid");
  document.getElementById("setupCountBadge").textContent = `${SETUPS.length} Setups Qualified`;
  if (!SETUPS.length) {
    container.innerHTML = '<div class="empty" style="grid-column:1/-1;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:24px">No qualifying setups at this moment. Strategy requirements: 9/21 EMA crossover + VWAP hold + ORB breakout with RVOL > 1.2.</div>';
    return;
  }

  container.innerHTML = "";
  SETUPS.forEach((s, idx) => {
    const card = document.createElement("div");
    card.className = "signal-card" + (s.grade === "A+" ? " a-plus" : "");
    const isBuy = s.side === "BUY";
    const sideBadge = isBuy ?
      '<span class="badge bull">BUY / LONG</span>' :
      '<span class="badge bear">SELL / SHORT</span>';
    const gradeBadge = `<span class="badge ${s.grade === 'A+' ? 'approve' : 'caution'}">${s.grade} (Score ${s.score})</span>`;
    const decimals = s.price > 500 ? 2 : (s.price > 10 ? 3 : 4);

    const reasonsHtml = (s.reasons || []).map(r => `<div style="font-size:11px;color:var(--ink-2)">✓ ${r}</div>`).join("");

    card.innerHTML = `
      <div class="signal-header">
        <div class="signal-title">
          <span>${s.symbol}</span>
          ${sideBadge}
        </div>
        ${gradeBadge}
      </div>

      <div class="strategy-badges">
        <span class="strategy-pill">ORB Breakout</span>
        <span class="strategy-pill">AVWAP Scalp</span>
        <span class="strategy-pill">9/21 EMA Trend</span>
        <span class="strategy-pill">Conviction: ${s.conviction}/10</span>
      </div>

      <div class="levels-grid">
        <div class="level-cell">
          <div class="k">Entry</div>
          <div class="v">$${fmt(s.price, decimals)}</div>
        </div>
        <div class="level-cell sl">
          <div class="k">Stop Loss</div>
          <div class="v">$${fmt(s.stop_loss, decimals)}</div>
        </div>
        <div class="level-cell t1">
          <div class="k">Target 1</div>
          <div class="v">$${fmt(s.target1, decimals)}</div>
        </div>
        <div class="level-cell t2">
          <div class="k">Target 2</div>
          <div class="v">$${fmt(s.target2, decimals)}</div>
        </div>
      </div>

      <div style="background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 10px;margin-top:2px">
        <div style="font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--ink-3);margin-bottom:4px;font-weight:700">Strategy Confluence Factors:</div>
        ${reasonsHtml}
      </div>

      <div style="display:flex;gap:8px;margin-top:6px">
        <button class="btn btn-debate" id="btnDebate_${idx}" data-symbol="${s.symbol}" onclick="openCouncilDebate(${idx})" style="flex:1;background:var(--cell-2);border-color:var(--accent);color:var(--accent);font-weight:700;font-size:11px;padding:8px;justify-content:center;gap:6px">
          🤖 4-Agent Debate &amp; News
        </button>
        <button class="btn-exec ${isBuy ? 'buy' : 'sell'}" onclick="executeTrade(${idx})" style="flex:1.1;margin:0;padding:8px">
          🚀 Execute ${s.side}
        </button>
      </div>
    `;
    container.appendChild(card);
  });
}

function closeCouncilModal() {
  document.getElementById("councilModal").style.display = "none";
}

window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeCouncilModal();
});

async function openCouncilDebate(index) {
  const s = SETUPS[index];
  if (!s) return;

  const modal = document.getElementById("councilModal");
  modal.style.display = "block";
  document.getElementById("cmTitle").textContent = `🤖 4-Agent AI Council Debate: ${s.symbol} (${s.side})`;
  document.getElementById("cmSubtitle").textContent = `Live 24/7 Delta Exchange Market Audit with Real-Time Financial News Stream`;

  const content = document.getElementById("cmContent");
  content.innerHTML = `
    <div style="text-align:center;padding:50px 16px">
      <div class="spin" style="width:32px;height:32px;border-width:3px;border-color:var(--accent);border-top-color:transparent;margin:0 auto 16px auto"></div>
      <div style="font-size:16px;font-weight:700;color:var(--ink)">Convening 4 AI Agents for ${s.symbol}…</div>
      <div style="font-size:12px;color:var(--ink-3);margin-top:8px;max-width:560px;margin-left:auto;margin-right:auto">
        Querying real-time financial headlines (Reuters, Bloomberg, FXStreet, CoinDesk) and running Gemini 3.8 Flash multi-agent debate…
      </div>
    </div>
  `;

  try {
    const res = await fetch("/api/forex/council_debate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ setup: s }),
    });
    if (!res.ok) throw new Error("Council debate request failed");
    const data = await res.json();
    renderCouncilDebateResults(data, index);
  } catch (err) {
    content.innerHTML = `
      <div style="background:var(--bad-soft);border:1px solid var(--bad);border-radius:8px;padding:16px;color:var(--bad)">
        <b>Debate Error:</b> ${err.message}
      </div>
    `;
  }
}

function renderCouncilDebateResults(data, setupIdx) {
  const v = data.verdict;
  const s = data.setup;
  const news = data.news || [];
  const content = document.getElementById("cmContent");

  const vClass = v.verdict === "APPROVE" ? "approve" : (v.verdict === "CAUTION" ? "caution" : "veto");
  const vColor = v.verdict === "APPROVE" ? "var(--good)" : (v.verdict === "CAUTION" ? "var(--warn)" : "var(--bad)");
  const decimals = s.price > 500 ? 2 : (s.price > 10 ? 3 : 4);

  // Format News items
  let newsHtml = "";
  if (news.length) {
    newsHtml = news.map(n => `
      <div class="news-item">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
          <span style="font-size:10px;font-weight:700;color:var(--accent);text-transform:uppercase">${n.source}</span>
          <span style="font-size:10px;color:var(--ink-3)">${n.pub_date ? n.pub_date.replace(/:\d\d GMT.*/, '') : ''}</span>
        </div>
        <div style="font-size:12.5px;font-weight:600;color:var(--ink);line-height:1.35">
          <a href="${n.link}" target="_blank" style="text-decoration:none;color:inherit" title="Open source article">
            ${n.title} ↗
          </a>
        </div>
      </div>
    `).join("");
  } else {
    newsHtml = `<div style="font-size:12px;color:var(--ink-3);padding:10px;background:var(--card);border-radius:6px">No adverse headlines found on Google News. Market sentiment neutral.</div>`;
  }

  content.innerHTML = `
    <!-- Top Verdict Banner -->
    <div style="background:var(--cell);border:2px solid ${vColor};border-radius:10px;padding:14px 18px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px">
      <div style="display:flex;align-items:center;gap:12px">
        <span class="badge ${vClass}" style="font-size:14px;padding:6px 14px">
          ● ${v.verdict}
        </span>
        <div>
          <div style="font-size:16px;font-weight:800;color:var(--ink)">
            Consensus Action: <span style="color:${vColor}">${v.action}</span>
          </div>
          <div style="font-size:11.5px;color:var(--ink-2);margin-top:2px">
            Conviction Score: <b>${v.conviction}/10</b> &bull; Min 8/10 Required to Execute
          </div>
        </div>
      </div>
      <div style="display:flex;gap:14px;font-size:12px;font-family:ui-monospace,monospace">
        <div><span style="color:var(--ink-3)">ENTRY:</span> <b>$${fmt(v.recommended_entry, decimals)}</b></div>
        <div><span style="color:var(--bad)">STOP:</span> <b>$${fmt(v.recommended_stop_loss, decimals)}</b></div>
        <div><span style="color:var(--good)">TARGET:</span> <b>$${fmt(v.recommended_target, decimals)}</b></div>
      </div>
    </div>

    <!-- Live Financial News Stream -->
    <div>
      <div style="display:flex;align-items:center;gap:8px;font-size:11.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:var(--ink-3);margin-bottom:8px">
        <span>📰 Real-Time Financial News Headlines (${s.symbol})</span>
        <span class="badge sm" style="font-size:9px">LIVE RSS</span>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:8px">
        ${newsHtml}
      </div>
    </div>

    <!-- 4 Agents Debate Grid -->
    <div>
      <div style="font-size:11.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:var(--ink-3);margin-bottom:8px">
        💬 4-Agent Debate Synthesis
      </div>
      <div class="agent-grid">

        <!-- Agent 1: Technical Analyst -->
        <div class="agent-card">
          <div class="agent-hd">
            <span class="agent-title">📈 Agent 1: Technical Analyst</span>
            <span class="badge sm ok">Chart Audit</span>
          </div>
          <div style="font-size:12.5px;color:var(--ink-2);line-height:1.45">
            ${v.technical_analysis}
          </div>
        </div>

        <!-- Agent 2: Bullish Researcher -->
        <div class="agent-card">
          <div class="agent-hd">
            <span class="agent-title">🐂 Agent 2: Bullish Researcher</span>
            <span class="badge sm bull">Momentum Case</span>
          </div>
          <div style="font-size:12.5px;color:var(--ink-2);line-height:1.45">
            ${v.bull_case}
          </div>
        </div>

        <!-- Agent 3: Bearish Devil's Advocate -->
        <div class="agent-card">
          <div class="agent-hd">
            <span class="agent-title">🐻 Agent 3: Bearish Researcher</span>
            <span class="badge sm bear">Trap &amp; Risk Check</span>
          </div>
          <div style="font-size:12.5px;color:var(--ink-2);line-height:1.45">
            ${v.bear_case}
          </div>
        </div>

        <!-- Agent 4: Chief Trader & Arbitrator -->
        <div class="agent-card" style="border-color:${vColor}">
          <div class="agent-hd">
            <span class="agent-title">🎯 Agent 4: Chief Decision Engine</span>
            <span class="badge sm ${vClass}">${v.verdict}</span>
          </div>
          <div style="font-size:12.5px;color:var(--ink-2);line-height:1.45">
            ${v.decision_rationale}
          </div>
        </div>

      </div>
    </div>

    <!-- Modal Actions Footer -->
    <div style="display:flex;align-items:center;justify-content:flex-end;gap:10px;margin-top:8px;padding-top:14px;border-top:1px solid var(--line)">
      <button class="btn" onclick="closeCouncilModal()" style="padding:8px 16px">Close Window</button>
      ${v.verdict !== "VETO" ? `
        <button class="btn-exec ${s.side === 'BUY' ? 'buy' : 'sell'}" onclick="executeTrade(${setupIdx}); closeCouncilModal();" style="width:auto;padding:8px 24px">
          🚀 Execute ${s.side} with Council Backing
        </button>
      ` : `
        <button class="btn-exec" disabled style="width:auto;padding:8px 20px;background:var(--cell-2);color:var(--ink-3);border:1px solid var(--bad)">
          ⛔ Trade Blocked by Council (Vetoed)
        </button>
      `}
    </div>
  `;
}

async function executeTrade(index) {
  const s = SETUPS[index];
  if (!s) return;
  if (!confirm(`Confirm execution:\n\nSymbol: ${s.symbol}\nSide: ${s.side}\nPrice: $${s.price}\nStop Loss: $${s.stop_loss}\nTarget: $${s.target1}`)) {
    return;
  }

  try {
    const res = await fetch("/api/forex/trade", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol: s.symbol,
        side: s.side,
        size: s.suggested_size || 1.0,
        price: s.price,
        stop_loss: s.stop_loss,
        target: s.target1,
        conviction: s.conviction,
        strategy_id: "forex_orb_vwap_multi",
      })
    });
    const data = await res.json();
    if (data.ok) {
      alert(`✓ Trade Executed Successfully!\n\nTrade ID: #${data.trade_id}\nMode: ${data.mode.toUpperCase()}\nSymbol: ${s.symbol}`);
      fetchPositions();
    } else {
      alert(`✕ Execution Rejected: ${data.reason || "Unknown error"}`);
    }
  } catch (err) {
    alert("Execution failed: " + err.message);
  }
}

async function closePosition(tradeId) {
  if (!confirm(`Are you sure you want to square off position #${tradeId} at market price?`)) return;
  try {
    const res = await fetch("/api/forex/close", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ trade_id: tradeId })
    });
    const data = await res.json();
    if (data.ok) {
      alert(`✓ Position #${tradeId} squared off.`);
      fetchPositions();
    } else {
      alert(`✕ Error: ${data.reason}`);
    }
  } catch (err) {
    alert("Square-off failed: " + err.message);
  }
}

// Initial loads & polling
const urlParams = new URLSearchParams(window.location.search);
const initSym = urlParams.get("symbol") || "BTCUSD";
selectChartSymbol(initSym, false);

fetchStatus();
fetchCouncilStatus();
fetchTickers();
fetchPositionsAndHistory();
fetchReflections();
fetchActiveSymbolParameters(initSym);
runScan();

setInterval(fetchTickers, 3000);
setInterval(() => fetchActiveSymbolParameters(ACTIVE_CHART_SYMBOL), 3000);
setInterval(fetchPositionsAndHistory, 3500);
setInterval(fetchReflections, 5000);
setInterval(fetchStatus, 15000);
__THEMEJS__
</script>
</body>
</html>
"""

_TOPBAR_RIGHT = """<a href="/" class="btn" style="text-decoration:none">← NSE Equity Dashboard</a>
    <div class="live"><span class="led" style="background:#f59e0b"></span><span>Delta 24/7 Live</span></div>"""

FOREX_PAGE = (
    _FOREX_TEMPLATE
    .replace("__THEME__", ui_theme.CSS)
    .replace(
        "__TOPBAR__",
        ui_theme.topbar("🌐 Forex & Crypto (Delta Exchange)", "forex", _TOPBAR_RIGHT),
    )
    .replace("__THEMEJS__", ui_theme.THEME_JS)
)


def serve_get(path: str, query: dict) -> tuple[int, str, bytes] | None:
    """Serve Forex GET endpoints."""
    if path in ("/forex", "/crypto", "/delta", "/chart"):
        return (200, "text/html; charset=utf-8", FOREX_PAGE.encode("utf-8"))

    if path == "/api/forex/candles":
        client = DeltaClient()
        sym_list = query.get("symbol", ["BTCUSD"])
        sym = sym_list[0] if sym_list else "BTCUSD"
        res_list = query.get("resolution", ["5"])
        resolution = res_list[0] if res_list else "5"
        bars_list = query.get("bars", [60])
        bars = int(bars_list[0]) if bars_list else 60
        df = client.get_candles(sym, resolution=resolution, bars=bars)
        if df is not None and not df.empty:
            candles = [
                {
                    "t": str(idx),
                    "o": float(row["Open"]),
                    "h": float(row["High"]),
                    "l": float(row["Low"]),
                    "c": float(row["Close"]),
                    "v": float(row["Volume"]),
                }
                for idx, row in df.iterrows()
            ]
            return (200, "application/json", json.dumps({"ok": True, "symbol": sym, "candles": candles}).encode("utf-8"))
        return (200, "application/json", json.dumps({"ok": False, "symbol": sym, "candles": []}).encode("utf-8"))

    if path == "/api/forex/status":
        client = DeltaClient()
        return (200, "application/json", json.dumps(client.status()).encode("utf-8"))

    if path == "/api/forex/tickers":
        client = DeltaClient()
        return (200, "application/json", json.dumps(client.get_tickers()).encode("utf-8"))

    if path == "/api/forex/scan":
        engine = ForexEngine()
        setups = engine.scan_all()
        return (200, "application/json", json.dumps(setups).encode("utf-8"))

    if path == "/api/forex/positions":
        client = DeltaClient()
        return (200, "application/json", json.dumps(client.get_positions()).encode("utf-8"))

    if path == "/api/forex/history":
        client = DeltaClient()
        return (200, "application/json", json.dumps(client.get_all_positions()).encode("utf-8"))

    if path == "/api/forex/auto_status":
        from trading.forex_trader import ForexAutoTrader
        trader = ForexAutoTrader()
        return (200, "application/json", json.dumps({
            "enabled": trader.is_enabled,
            "council_enabled": trader.is_council_enabled,
            "daily_realized_pnl": trader.get_daily_pnl(),
            "config": trader.config,
        }).encode("utf-8"))

    if path == "/api/forex/council_status":
        from trading.forex_trader import ForexAutoTrader
        trader = ForexAutoTrader()
        return (200, "application/json", json.dumps({
            "ok": True,
            "council_enabled": trader.is_council_enabled,
        }).encode("utf-8"))

    if path == "/api/forex/news":
        from trading.forex_council import fetch_crypto_forex_news
        sym_list = query.get("symbol", ["BTCUSD"])
        sym = sym_list[0] if sym_list else "BTCUSD"
        news = fetch_crypto_forex_news(sym, limit=4)
        return (200, "application/json", json.dumps({
            "ok": True,
            "symbol": sym,
            "news": news,
        }).encode("utf-8"))

    if path == "/api/forex/backtest":
        from trading.forex_backtest import ForexBacktester
        bt = ForexBacktester()
        res = bt.backtest_all(bars=200)
        return (200, "application/json", json.dumps(res).encode("utf-8"))

    if path == "/api/forex/reflections":
        from trading.forex_reflection import auto_reflect_on_closed_trades, load_reflections
        client = DeltaClient()
        auto_reflect_on_closed_trades(client)
        refs = load_reflections()
        return (200, "application/json", json.dumps(refs).encode("utf-8"))

    if path == "/api/forex/equity":
        from trading.forex_trader import ForexAutoTrader
        trader = ForexAutoTrader()
        eq = trader.get_account_equity()
        return (200, "application/json", json.dumps(eq).encode("utf-8"))

    if path == "/api/forex/parameters":
        engine = ForexEngine()
        sym_list = query.get("symbol", ["BTCUSD"])
        sym = sym_list[0] if sym_list else "BTCUSD"
        params = engine.get_symbol_parameters(sym)
        return (200, "application/json", json.dumps(params).encode("utf-8"))

    return None


def serve_post(path: str, body: dict) -> tuple[int, str, bytes] | None:
    """Serve Forex POST endpoints."""
    if path == "/api/forex/scan":
        engine = ForexEngine()
        setups = engine.scan_all()
        return (200, "application/json", json.dumps(setups).encode("utf-8"))

    if path == "/api/forex/auto_toggle":
        from trading.forex_trader import ForexAutoTrader
        trader = ForexAutoTrader()
        enable = body.get("enabled")
        if enable is None:
            enable = not trader.is_enabled
        trader.set_enabled(enable)
        return (200, "application/json", json.dumps({"ok": True, "enabled": trader.is_enabled}).encode("utf-8"))

    if path == "/api/forex/council_toggle":
        from trading.forex_trader import ForexAutoTrader
        trader = ForexAutoTrader()
        enable = body.get("enabled")
        if enable is None:
            enable = not trader.is_council_enabled
        trader.set_council_enabled(enable)
        return (200, "application/json", json.dumps({"ok": True, "council_enabled": trader.is_council_enabled}).encode("utf-8"))

    if path == "/api/forex/council_debate":
        from trading.forex_council import debate_forex_setup, fetch_crypto_forex_news
        client = DeltaClient()
        setup = body.get("setup")
        sym = body.get("symbol")
        if not setup and sym:
            engine = ForexEngine(client=client)
            setup = engine.analyze_symbol(sym)
            if not setup:
                tickers = client.get_tickers([sym])
                t = tickers.get(sym, {})
                px = float(t.get("mark_price", 100.0))
                setup = {
                    "symbol": sym,
                    "side": "BUY",
                    "price": px,
                    "stop_loss": px * 0.99,
                    "target1": px * 1.02,
                    "target2": px * 1.04,
                    "score": 65,
                    "conviction": 7,
                    "reasons": ["Manual Council Audit Request"],
                }
        if not setup:
            return (400, "application/json", json.dumps({"ok": False, "reason": "No setup or symbol provided"}).encode("utf-8"))

        sym = setup.get("symbol", "BTCUSD")
        news = fetch_crypto_forex_news(sym, limit=4)
        verdict = debate_forex_setup(setup, news=news, client=client)
        return (200, "application/json", json.dumps({
            "ok": True,
            "symbol": sym,
            "verdict": verdict.model_dump(),
            "news": news,
            "setup": setup,
        }).encode("utf-8"))

    if path == "/api/forex/auto_pass":
        from trading.forex_trader import ForexAutoTrader
        trader = ForexAutoTrader()
        res = trader.run_pass()
        return (200, "application/json", json.dumps(res).encode("utf-8"))

    if path == "/api/forex/trade":
        client = DeltaClient()
        symbol = body.get("symbol", "BTCUSD")
        side = body.get("side", "BUY")
        size = float(body.get("size") or 1.0)
        price = float(body.get("price") or 0.0)
        stop_loss = float(body.get("stop_loss") or (price * 0.98))
        target = float(body.get("target") or (price * 1.04))
        strategy_id = body.get("strategy_id", "forex_orb_vwap_multi")
        conviction = int(body.get("conviction") or 8)

        res = client.execute_trade(
            symbol=symbol,
            side=side,
            size=size,
            price=price,
            stop_loss=stop_loss,
            target=target,
            strategy_id=strategy_id,
            conviction=conviction,
        )
        return (200 if res.get("ok") else 400, "application/json", json.dumps(res).encode("utf-8"))

    if path == "/api/forex/close":
        client = DeltaClient()
        trade_id = body.get("trade_id")
        res = client.close_position(trade_id)
        return (200 if res.get("ok") else 400, "application/json", json.dumps(res).encode("utf-8"))

    if path == "/api/forex/reset_challenge":
        from trading.delta_exchange import POSITIONS_FILE
        from trading.forex_reflection import REFLECTIONS_FILE
        if POSITIONS_FILE.exists():
            try:
                POSITIONS_FILE.unlink()
            except Exception:
                pass
        if REFLECTIONS_FILE.exists():
            try:
                REFLECTIONS_FILE.unlink()
            except Exception:
                pass
        return (200, "application/json", json.dumps({
            "ok": True,
            "message": "Challenge reset: paper desk initialized with clean $100.00 capital.",
        }).encode("utf-8"))

    return None
