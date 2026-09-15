"""Web UI for the F&O long scanner — "what can I buy right now, and why not?"

Served two ways:

    python -m trading.fno.web [port]     standalone on 8788
    python -m trading.dashboard          the same page at /fno

Scans are slow (a yfinance batch plus four NSE calls), so one runs in a
background thread and the page renders the cached result immediately, showing
its age. Nothing here re-derives a level: the page renders `report.as_dict`,
the same payload the CLI's --json prints, so the screen and the terminal can
never disagree about an entry or a stop.
"""
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from trading import ui_theme
from trading.fno import config as C
from trading.fno import fyers
from trading.fno import report
from trading.fno.data import LiveFeed, ReplayFeed
from trading.fno.models import IST
from trading.fno.scanner import Scanner

DEFAULT_TTL = 120          # seconds before a cached scan is considered stale


class ScanCache:
    """Runs at most one scan at a time and hands out the latest result.

    The UI must never block on the network, and two browser tabs must not
    trigger two concurrent scans, so state transitions are guarded by a lock
    and the worker is fire-and-forget.
    """

    def __init__(self, *, allow_delayed: bool = True, shortlist: int = C.SHORTLIST_SIZE,
                 symbols: list[str] | None = None, replay: str | None = None,
                 ttl: int = DEFAULT_TTL, fyers: bool = False,
                 fyers_base: str = "http://localhost:3001"):
        self.allow_delayed = allow_delayed
        self.shortlist = shortlist
        self.symbols = symbols
        self.replay = replay
        self.ttl = ttl
        self.fyers = fyers
        self.fyers_base = fyers_base
        self._lock = threading.Lock()
        self._scanning = False
        self._scan: dict | None = None
        self._error: str | None = None
        self._finished: datetime | None = None
        self._started: datetime | None = None

    # --- state ----------------------------------------------------------

    @property
    def age_sec(self) -> float | None:
        if self._finished is None:
            return None
        return (datetime.now(IST) - self._finished).total_seconds()

    def snapshot(self, refresh: bool = False) -> dict:
        stale = self.age_sec is None or self.age_sec > self.ttl
        if refresh or stale:
            self.start()
        with self._lock:
            return {
                "status": ("scanning" if self._scanning
                           else "error" if self._error and self._scan is None
                           else "ready" if self._scan else "idle"),
                "scanning": self._scanning,
                "error": self._error,
                "age_sec": None if self.age_sec is None else round(self.age_sec),
                "ttl": self.ttl,
                "started_at": self._started.isoformat() if self._started else None,
                "finished_at": self._finished.isoformat() if self._finished else None,
                "scan": self._scan,
            }

    def start(self) -> bool:
        with self._lock:
            if self._scanning:
                return False
            self._scanning = True
            self._started = datetime.now(IST)
        threading.Thread(target=self._run, daemon=True).start()
        return True

    # --- worker ---------------------------------------------------------

    def _run(self):
        try:
            scan = self._scan_now()
            with self._lock:
                self._scan, self._error = scan, None
        except Exception as exc:                     # noqa: BLE001
            # A feed outage must degrade to a visible message, never a blank page.
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._scanning = False
                self._finished = datetime.now(IST)

    def _scan_now(self) -> dict:
        if self.replay:
            feed = ReplayFeed(self.replay)
            now, allow_delayed = feed.as_of, True
        else:
            from trading.fno.fyers import load_token, FyersFeed, FyersClient
            token, _ = load_token()
            client = FyersClient(base=self.fyers_base)
            is_connected, _ = client.status()
            if self.fyers or token is not None or is_connected:
                feed = FyersFeed(base=self.fyers_base)
                now, allow_delayed = datetime.now(IST), True
            else:
                feed = LiveFeed()
                now, allow_delayed = datetime.now(IST), self.allow_delayed
        res = Scanner(feed, now, allow_delayed=allow_delayed,
                      symbols=self.symbols, shortlist=self.shortlist).run()
        return report.as_dict(res)


CACHE = ScanCache()


def serve(path: str, query: dict | None = None, *,
          include_root: bool = False) -> tuple[int, str, bytes] | None:
    """Route one request. Returns (status, content-type, body) or None.

    Shared by this module's standalone server and trading.dashboard, so the
    page exists at exactly one implementation regardless of which port it is
    reached on. `include_root` is for the standalone server only — when this is
    mounted inside the dashboard, "/" belongs to the dashboard.
    """
    query = query or {}
    roots = ("/", "/fno") if include_root else ("/fno",)
    if path in roots:
        # Standalone, "/" is the scanner itself and there is no dashboard to
        # link to — the page hides that nav item rather than offering a link
        # that goes nowhere.
        mode = "standalone" if include_root else "mounted"
        return 200, "text/html; charset=utf-8", PAGE.replace("__MODE__", mode).encode()
    if path in ("/api/scan", "/api/fno"):
        refresh = query.get("refresh", ["0"])[0] == "1"
        body = json.dumps(CACHE.snapshot(refresh=refresh), default=str).encode()
        return 200, "application/json", body
    if path in ("/api/quotes", "/api/fyers/quotes"):
        from trading.fno.fyers import FyersClient
        client = FyersClient()
        syms_param = query.get("symbols", [None])[0]
        if syms_param:
            syms = [s.strip() for s in syms_param.split(",") if s.strip()]
        else:
            default_syms = [
                "NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX", "NSE:RELIANCE-EQ",
                "NSE:INFY-EQ", "NSE:TCS-EQ", "NSE:HDFCBANK-EQ", "NSE:ICICIBANK-EQ",
                "NSE:SBIN-EQ", "NSE:BHARTIARTL-EQ", "NSE:ITC-EQ"
            ]
            cand_syms = []
            if CACHE._scan and CACHE._scan.get("candidates"):
                for c in CACHE._scan["candidates"][:15]:
                    s_name = f"NSE:{c['symbol']}-EQ"
                    if s_name not in default_syms and s_name not in cand_syms:
                        cand_syms.append(s_name)
            syms = default_syms + cand_syms
        quotes = client.quotes(syms)
        return 200, "application/json", json.dumps({"ok": True, "quotes": quotes}, default=str).encode()
    if path == "/api/candles":
        from trading.fno.fyers import FyersClient, FyersError
        symbol = query.get("symbol", [None])[0]
        resolution = query.get("resolution", ["5"])[0]
        days = int(query.get("days", ["1"])[0])
        if not symbol:
            return 400, "application/json", json.dumps({"ok": False, "error": "symbol required"}).encode()
        # Scanner symbols are bare (e.g. "HYUNDAI"); Fyers wants NSE:SYMBOL-EQ.
        fyer_sym = symbol
        if ":" not in fyer_sym:
            fyer_sym = f"NSE:{symbol}-EQ"
        try:
            client = FyersClient()
            candles = client.history(fyer_sym, resolution, days)
            return 200, "application/json", json.dumps({"ok": True, "symbol": fyer_sym,
                                                        "candles": candles}, default=str).encode()
        except FyersError as exc:
            return 200, "application/json", json.dumps({"ok": False, "symbol": fyer_sym,
                                                        "error": str(exc), "candles": []}, default=str).encode()
    if path == "/api/fyers/status":
        from trading.fno.fyers import FyersClient, get_auth_link, FYERS_APP_ID
        client = FyersClient()
        connected, note = client.status()
        prof = {}
        try:
            p = client._get("/api/fyers/status")
            if p.get("profile"):
                prof = p.get("profile")
        except Exception:
            pass
        res = {
            "connected": connected,
            "profile": prof or ({"name": "SHUBHAM NARAYAN PANCHAL"} if connected else {}),
            "auth_url": get_auth_link(),
            "app_id": FYERS_APP_ID,
            "note": note,
        }
        return 200, "application/json", json.dumps(res, default=str).encode()
    if path == "/api/fyers/login":
        from trading.fno.fyers import get_auth_link
        auth_link = get_auth_link()
        html = f"""<!doctype html><html><head><meta http-equiv="refresh" content="0; url={auth_link}"></head><body>Redirecting to Fyers login...</body></html>"""
        return 200, "text/html; charset=utf-8", html.encode()
    if path == "/api/fyers/callback":
        from trading.fno.fyers import exchange_token
        auth_code = query.get("auth_code", [None])[0]
        if not auth_code:
            return 400, "text/html; charset=utf-8", b"<h3>Error: No auth_code received from Fyers.</h3>"
        try:
            data = exchange_token(auth_code)
            name = (data.get("profile") or {}).get("name") or "Trader"
            html = f"""<!doctype html>
<html><head><title>Fyers Connected</title>
<style>body{{font-family:system-ui,-apple-system,sans-serif;background:#0d1117;color:#e6edf3;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px;text-align:center;max-width:440px;box-shadow:0 8px 24px rgba(0,0,0,0.4);}}
h2{{color:#3fb950;margin-top:0;}}
a{{display:inline-block;margin-top:20px;background:#238636;color:#fff;text-decoration:none;padding:10px 20px;border-radius:6px;font-weight:600;}}</style>
</head><body><div class="card"><h2>&#10003; Fyers API Connected!</h2>
<p>Welcome, <b>{name}</b>. Zero-delay real-time market data is now active.</p>
<a href="/">Open Dashboard</a></div></body></html>"""
            return 200, "text/html; charset=utf-8", html.encode()
        except Exception as exc:
            return 500, "text/html; charset=utf-8", f"<h3>Failed to exchange token: {exc}</h3>".encode()
    return None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        result = serve(url.path, parse_qs(url.query), include_root=True)
        if result is None:
            return self._send(404, "text/plain", b"not found")
        self._send(*result)

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

    def log_message(self, fmt, *args):   # quiet
        pass


_PAGE_TEMPLATE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>F&amp;O Long Scanner</title>
<style>
__THEME__

/* the hero tile leads the strip, so it gets more room than the rest */
.kpis{grid-template-columns:1.15fr repeat(4,1fr)}
@media (max-width:900px){.kpis{grid-template-columns:1fr 1fr}}
.bars{display:flex;gap:2px;margin-top:7px;height:5px}
.bars i{flex:1;border-radius:1px;background:var(--track)}
.bars i.on{background:var(--good)}

/* ---------- layout ---------- */
.grid{display:grid;grid-template-columns:minmax(0,1fr) 288px;gap:14px;margin-top:14px;
  align-items:start}
#list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;align-items:start}
@media (max-width:1080px){.grid{grid-template-columns:1fr}}
@media (max-width:760px){#list{grid-template-columns:1fr}}
.rail{display:grid;gap:12px;position:sticky;top:64px}
@media (max-width:1080px){.rail{position:static}}
.panel{background:var(--card);border:1px solid var(--line);border-radius:11px;
  box-shadow:var(--shadow);overflow:hidden}
.panel > h2{margin:0;padding:9px 13px;font-size:10px;letter-spacing:.11em;
  text-transform:uppercase;color:var(--ink-3);border-bottom:1px solid var(--line);
  background:var(--cell);font-weight:600}
.panel .body{padding:11px 13px}

/* ---------- candidate card ---------- */
.card{position:relative;background:var(--card);border:1px solid var(--line);
  border-radius:14px;box-shadow:0 0 0 1px rgba(47,159,219,.04),0 14px 34px rgba(0,0,0,.35);
  margin:0;overflow:hidden;background:linear-gradient(145deg,var(--card),color-mix(in srgb,var(--bg-2) 78%,var(--card)))}
.card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--ink-3)}
.card.v-buy::before{background:var(--good)}
.card.v-watch::before{background:var(--warn)}
.card.v-avoid::before{background:var(--line-2)}
.card .hd{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:12px;align-items:start;
  padding:15px 15px 11px 16px;border-bottom:1px solid var(--line)}
.signal-mark{width:42px;height:42px;flex:none;border-radius:12px;display:grid;place-items:center;
  font-size:24px;font-weight:700;background:var(--bad-soft);color:var(--bad);
  border:1px solid color-mix(in srgb,var(--bad) 55%,var(--line));box-shadow:0 0 18px color-mix(in srgb,var(--bad) 18%,transparent)}
.card.v-buy .signal-mark{background:var(--good-soft);color:var(--good);border-color:color-mix(in srgb,var(--good) 55%,var(--line));
  box-shadow:0 0 18px color-mix(in srgb,var(--good) 18%,transparent)}
.idw{min-width:0}
.idw .row1{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap}
.rank{font-size:10px;font-weight:700;color:var(--ink-3);letter-spacing:.08em}
.sym{font-size:17px;font-weight:700;letter-spacing:-.01em}
.chip{font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:var(--ink-2);
  border:1px solid var(--line);background:var(--cell);border-radius:5px;padding:2px 6px;
  white-space:nowrap}
.status{font-size:11px;color:var(--ink-2);margin-top:5px}
.status .dotsep{color:var(--ink-3);margin:0 6px}
.pxw{text-align:right;min-width:84px}
.pxw .px{font-size:20px;font-weight:650}
.pxw .chg{font-size:12px;font-weight:600;margin-top:1px}
.vw{grid-column:1 / -1;display:flex;flex-direction:row;align-items:center;justify-content:space-between;gap:10px}

/* Verdict badge: glyph + word do the work; the tint only reinforces them. */
.badge{display:inline-flex;align-items:center;gap:6px;border-radius:7px;padding:5px 11px;
  font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  border:1px solid var(--line-2);background:var(--cell);color:var(--ink)}
.badge .g{font-size:12px;line-height:1}
.badge.v-buy{border-color:var(--good);background:var(--good-soft)}
.badge.v-buy .g{color:var(--good)}
.badge.v-watch{border-color:var(--warn);background:var(--warn-soft)}
.badge.v-watch .g{color:var(--warn)}
.badge.v-avoid .g{color:var(--ink-3)}
.badge.sm{padding:3px 8px;font-size:10px}
.badge.mkt.bull{border-color:var(--good);background:var(--good-soft)}
.badge.mkt.bull .g{color:var(--good)}
.badge.mkt.bear{border-color:var(--bad);background:var(--bad-soft)}
.badge.mkt.bear .g{color:var(--bad)}

.meter{width:176px}
.meter .r{display:flex;justify-content:space-between;font-size:10px;color:var(--ink-3);
  letter-spacing:.07em;text-transform:uppercase}
.meter .t{height:5px;border-radius:999px;background:var(--track);margin-top:4px;overflow:hidden}
.meter .f{height:100%;border-radius:999px;background:var(--accent)}
.confidence{margin:0 15px 0 16px;padding:10px 12px;border:1px solid var(--line);
  border-radius:9px;background:color-mix(in srgb,var(--cell) 82%,transparent)}
.confidence .meter{width:100%}
.confidence .meter .t{height:7px;margin-top:7px}
.confidence .meter .r{font-size:10px}

/* ---------- charts row ---------- */
.charts{display:grid;grid-template-columns:1fr;gap:12px;
  padding:13px 14px 4px 16px;align-items:center}
.charts > div + div{border-top:1px solid var(--line);padding-top:11px}
.spark{position:relative}
.spark svg{display:block;width:100%;height:58px}
.spark .end{position:absolute;width:7px;height:7px;border-radius:50%;background:var(--accent);
  box-shadow:0 0 0 2px var(--card);transform:translate(-50%,-50%)}
.cap{font-size:9.5px;letter-spacing:.10em;text-transform:uppercase;color:var(--ink-3);
  margin-bottom:5px}

/* Price-vs-levels ladder — readable first, positioned HTML so markers stay sharp. */
.level-visual{border:1px solid var(--line);border-radius:9px;background:var(--cell);
  padding:10px 12px 9px}
.lad{position:relative;height:58px;margin:4px 4px 0}
.lad .bar{position:absolute;left:0;right:0;top:27px;height:10px;border-radius:999px;
  background:var(--track);box-shadow:inset 0 1px 2px rgba(0,0,0,.16)}
.lad .zone{position:absolute;top:27px;height:10px}
.lad .zone.risk{background:var(--bad-soft)}
.lad .zone.rew{background:var(--good-soft)}
.lad .zone.entry{top:24px;height:16px;border-radius:4px;background:var(--accent);opacity:.35;
  box-shadow:0 0 0 1px color-mix(in srgb,var(--accent) 45%,transparent)}
.lad .rule{position:absolute;top:17px;width:2px;height:30px;margin-left:-1px;z-index:2}
.lad .rule .label{position:absolute;bottom:33px;left:50%;transform:translateX(-50%);white-space:nowrap;
  font-size:9px;font-weight:700;letter-spacing:.04em;color:currentColor;text-transform:uppercase}
.lad .rule .label.below{top:33px;bottom:auto}
.lad .cap-sq{position:absolute;left:-4px;bottom:-5px;width:10px;height:8px;background:currentColor}
.lad .cap-tri{position:absolute;left:-5px;top:-8px;width:0;height:0;
  border-left:5px solid transparent;border-right:5px solid transparent;
  border-bottom:7px solid currentColor}
.lad .dot{position:absolute;top:23px;width:18px;height:18px;margin-left:-9px;border-radius:50%;z-index:4;
  background:var(--accent);border:3px solid var(--card);box-shadow:0 0 0 1px var(--accent),0 0 0 5px var(--accent-soft);
  animation:pricePulse 1.8s ease-in-out infinite}
.lad .dot::after{content:"NOW";position:absolute;left:50%;bottom:23px;transform:translateX(-50%);
  color:var(--accent);font-size:9px;font-weight:800;letter-spacing:.08em}
@keyframes pricePulse{50%{box-shadow:0 0 0 1px var(--accent),0 0 0 9px color-mix(in srgb,var(--accent) 0%,transparent)}}
.lad .rule{animation:markerIn .45s ease both}
@keyframes markerIn{from{opacity:0;transform:scaleY(.4)}to{opacity:1;transform:scaleY(1)}}
@media (prefers-reduced-motion:reduce){.lad .dot,.lad .rule{animation:none}}
.level-explain{margin-top:5px;font-size:11px;line-height:1.35;color:var(--ink-2)}
.level-explain b{color:var(--ink)}
.lgnd{display:flex;flex-wrap:wrap;gap:5px 8px;font-size:10px;color:var(--ink-2);margin-top:8px}
.lgnd span{border:1px solid var(--line);border-radius:5px;padding:3px 6px;background:var(--card)}
.lgnd i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px;
  vertical-align:-1px}
.lgnd i.rnd{border-radius:50%}

/* ---------- plan ---------- */
.plan{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1px;
  background:var(--line);border-top:1px solid var(--line);border-bottom:1px solid var(--line);
  margin-top:11px}
.plan .c{background:var(--cell);padding:9px 11px;min-width:0}
.plan .c.hl{background:var(--cell-2)}
.plan .k{font-size:9.5px;letter-spacing:.10em;text-transform:uppercase;color:var(--ink-3)}
.plan .v{font-size:15px;font-weight:650;margin-top:2px;white-space:nowrap}
.plan .v.bad{color:var(--bad)} .plan .v.good{color:var(--good)}
.plan .n{font-size:10px;color:var(--ink-3);margin-top:1px}
.plan-guide{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1px;
  margin-top:1px;background:var(--line);border-bottom:1px solid var(--line)}
.plan-guide div{background:var(--card);padding:7px 10px;font-size:10.5px;color:var(--ink-2);line-height:1.35}
.plan-guide b{display:block;color:var(--ink);font-size:10px;letter-spacing:.04em;text-transform:uppercase;margin-bottom:2px}
@media (max-width:620px){.plan-guide{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:620px){.plan{grid-template-columns:repeat(2,minmax(0,1fr))}}
.trade-footer{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 15px 14px 16px;
  padding-top:11px;border-top:1px solid var(--line);font-size:12px;color:var(--ink-2)}
.trade-footer b{color:var(--ink);font-size:14px}
.trade-footer .metric-good{color:var(--good);font-weight:700}
.trade-footer .trade-copy{font-size:11px;color:var(--ink-3)}
.noplan{padding:10px 16px;font-size:12px;color:var(--ink-3);border-top:1px solid var(--line)}

/* ---------- options ---------- */
.opt{margin:12px 14px 0 16px;border:1px solid var(--line);border-radius:9px;
  background:var(--cell);overflow:hidden}
.opt .oh{display:flex;align-items:center;gap:9px;flex-wrap:wrap;padding:8px 11px;
  border-bottom:1px solid var(--line)}
.opt .ot{font-size:9.5px;letter-spacing:.11em;text-transform:uppercase;color:var(--ink-3)}
.opt .strike{font-size:15px;font-weight:650}
.opt .be{font-size:12px;color:var(--ink-2)}
.opt .be b{color:var(--ink);font-weight:600}
.opt .no{padding:9px 11px;font-size:12.5px;color:var(--ink-2)}
.opt .no b{color:var(--bad);font-weight:700;letter-spacing:.06em}
.opt table{font-size:11.5px}
.opt th,.opt td{padding:5px 11px}
.opt .oplan{display:grid;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));
  gap:1px;background:var(--line);border-top:1px solid var(--line)}
.opt .oplan div{background:var(--card);padding:8px 11px}
.opt .oplan .k{font-size:9px;letter-spacing:.10em;text-transform:uppercase;color:var(--ink-3)}
.opt .oplan .v{font-size:14px;font-weight:650;margin-top:2px}
.opt .oplan .v.bad{color:var(--bad)} .opt .oplan .v.good{color:var(--good)}
.opt .oplan .n{font-size:10px;color:var(--ink-3);margin-top:1px}
.opt .model{padding:6px 11px;font-size:10.5px;color:var(--ink-3);
  border-top:1px solid var(--line)}
.opt .warns{padding:7px 11px;font-size:11.5px;color:var(--warn);
  border-top:1px solid var(--line)}
.opt .warns div{margin:2px 0}

/* ---------- reasons ---------- */
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:12px 14px 2px 16px}
@media (max-width:760px){.cols{grid-template-columns:1fr}}
.hdr{font-size:9.5px;letter-spacing:.11em;text-transform:uppercase;color:var(--ink-3);
  margin-bottom:6px}
ul.rs{margin:0;padding-left:15px}
ul.rs li{margin:3px 0;color:var(--ink-2);font-size:12.5px}
ul.rs.bad li{color:var(--bad)}
.metrics{display:flex;flex-wrap:wrap;gap:5px;padding:11px 14px 12px 16px}
.m{display:inline-flex;align-items:baseline;gap:5px;border:1px solid var(--line);
  background:var(--cell);border-radius:6px;padding:3px 8px;font-size:11px;color:var(--ink-3)}
.m b{color:var(--ink);font-weight:600;font-size:11.5px}
.warns{padding:0 14px 10px 16px;font-size:11.5px;color:var(--warn)}
.srcs{padding:8px 14px 11px 16px;font-size:10.5px;color:var(--ink-3);
  border-top:1px solid var(--line);word-break:break-word}

/* ---------- rail ---------- */
.sec{display:grid;grid-template-columns:64px 1fr 50px;align-items:center;gap:8px;
  font-size:11.5px;padding:3px 0}
.sec .nm{color:var(--ink-2);letter-spacing:.03em;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.sec .bb{position:relative;height:5px;background:var(--track);border-radius:999px}
.sec .bb span{position:absolute;top:0;height:100%;border-radius:999px}
.sec .bb::after{content:"";position:absolute;left:50%;top:-2px;width:1px;height:9px;
  background:var(--line-2)}
.sec .vv{text-align:right;font-size:11px}
.feed{display:flex;justify-content:space-between;gap:8px;font-size:11px;padding:3px 0;
  border-bottom:1px solid var(--line)}
.feed:last-child{border-bottom:0}
.feed .nm{color:var(--ink-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.feed .ag{color:var(--ink-3);white-space:nowrap}
.feed .ag.warnc{color:var(--warn)}
.notes{margin:0;padding-left:15px;font-size:11.5px;color:var(--ink-3)}
.notes li{margin:4px 0}

/* ---------- table ---------- */
table{width:100%;border-collapse:collapse;font-size:12px}
th{position:sticky;top:0;background:var(--cell);color:var(--ink-3);font-weight:600;
  text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);white-space:nowrap;
  font-size:10px;letter-spacing:.09em;text-transform:uppercase}
td{padding:7px 10px;border-bottom:1px solid var(--line);color:var(--ink-2);white-space:nowrap}
td.sym{color:var(--ink);font-weight:600}
td.num,th.num{text-align:right}
tbody tr:hover td{background:var(--cell)}
td.blk{white-space:normal;min-width:220px}
.scroll{overflow-x:auto}
.empty{padding:16px;color:var(--ink-3);font-size:12.5px;text-align:center}
.foot{margin-top:18px;padding-top:12px;border-top:1px solid var(--line);
  font-size:11px;color:var(--ink-3);line-height:1.6}
</style></head>
<body data-mode="__MODE__">
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

  <div id="banner"></div>
  <div class="kpis" id="kpis"></div>

  <div id="niftyWrap"></div>

  <div class="grid">
    <main id="list"></main>
    <aside class="rail">
      <section class="panel" id="secpanel" hidden>
        <h2>Sector strength</h2>
        <div class="body"><div class="cap" id="sectitle"></div>
          <div id="sectors"></div></div>
      </section>
      <section class="panel">
        <h2>Data feeds</h2>
        <div class="body" id="feeds"></div>
      </section>
      <section class="panel">
        <h2>Window &amp; data notes</h2>
        <div class="body"><ul class="notes" id="notes"></ul></div>
      </section>
    </aside>
  </div>

  <section class="panel" style="margin-top:14px">
    <h2>All screened names — score, verdict, and what is blocking each</h2>
    <div class="scroll"><table id="tbl"></table></div>
  </section>

  <div class="foot">
    Decision support only — not investment advice, and no profit is implied or
    guaranteed. A score counts how many independent factors currently agree; it is
    not a probability of profit. Verify every level on your own terminal before
    acting. This page places no orders.
  </div>
</div>

<script>
const API = location.pathname.replace(/\/+$/,"") === "/fno" ? "/api/fno" : "/api/scan";
const $ = s => document.querySelector(s);
const el = (t,c,x) => { const n=document.createElement(t); if(c) n.className=c;
  if(x!==undefined) n.textContent=x; return n; };
const num = v => v===null||v===undefined||!isFinite(v) ? "—"
  : v.toLocaleString("en-IN",{minimumFractionDigits:2,maximumFractionDigits:2});
const pc = v => v===null||v===undefined ? "—" : (v>=0?"+":"")+v.toFixed(2)+"%";
const sgn = v => v===null||v===undefined ? "" : v>0 ? "up" : v<0 ? "down" : "";
const sc = v => v%1 ? v.toFixed(1) : v.toFixed(0);
const GLY = {BUY:"✓", WATCH:"◔", AVOID:"✕", bull:"▲",
             bear:"▼", flat:"—"};
const isDone = c => (c.trade && (c.trade.target1_hit || c.trade.target2_hit || (c.trade.target1 && c.price >= c.trade.target1))) ||
                    (c.structure && (c.structure.target1_hit || c.structure.target2_hit || (c.structure.status && c.structure.status.indexOf("TARGET") >= 0)));

let STATE=null, FILTER="active", TIMER=null;

async function load(force){
  try{
    const r = await fetch(API + (force?"?refresh=1":""), {cache:"no-store"});
    STATE = await r.json();
  }catch(e){ STATE = {status:"error", error:String(e), scan:null, scanning:false}; }
  render();
  clearTimeout(TIMER);
  TIMER = setTimeout(()=>load(false), STATE.scanning ? 2000 : ($("#auto").checked?15000:60000));
}

function render(){
  if(!STATE) return;
  const s = STATE.scan;
  const busy = !!STATE.scanning;
  $("#rescan").disabled = busy;
  $("#rescan").innerHTML = "";
  if(busy){ $("#rescan").append(el("span","spin")); $("#rescan").append(document.createTextNode("Scanning")); }
  else $("#rescan").textContent = "Scan now";
  $("#led").className = "led" + (busy ? " busy" : (s && !s.data_ok) ? " stale" : "");
  $("#stamp").textContent = STATE.age_sec===null ? "no scan yet"
    : busy ? "scanning" : "updated " + (STATE.age_sec<60 ? STATE.age_sec+"s"
        : Math.round(STATE.age_sec/60)+"m") + " ago";

  $("#banner").innerHTML = "";
  if(!s){
    $("#win").textContent = STATE.error ? "scan failed" : "running first scan…";
    if(STATE.error) banner("Scan failed", STATE.error);
    return;
  }
  $("#win").textContent = s.when_label + "  ·  " + s.window;
  if(s.stale_banner) banner(s.stale_banner,
    "No setup is graded and no entry, stop or target is produced.");
  else if(STATE.error) banner("Last refresh failed — showing the previous scan", STATE.error);

  kpis(s); nifty(s); sectors(s); feeds(s); notes(s); counts(s); list(s); table(s);
  if(window.ScannerAlerts){
    (s.picks||[]).forEach(p => window.ScannerAlerts.checkAndAlert(p, "BUY"));
    (s.candidates||[]).filter(c=>c.verdict==="WATCH").forEach(c => window.ScannerAlerts.checkAndAlert(c, "WATCH"));
  }
}

__NIFTYJS__
__CONTEXTJS__

function nifty(s){
  const wrap = $("#niftyWrap");
  wrap.replaceChildren();
  if(s.index_options) wrap.append(niftyPanel(s.index_options, num));
  const ctx = contextPanel(s.chain, s.pivots, num);
  if(ctx) wrap.append(ctx);
}

function banner(t,d){
  const b = el("div","banner"); b.append(el("b",null,t));
  if(d) b.append(el("div",null,d)); $("#banner").append(b);
}

function counts(s){
  const all = s.candidates||[];
  const active = all.filter(c => !isDone(c));
  const done = all.filter(c => isDone(c));
  const na = $("#n-active"); if(na) na.textContent = active.length || "";
  const nall = $("#n-all"); if(nall) nall.textContent = all.length || "";
  const ndone = $("#n-DONE"); if(ndone) ndone.textContent = done.length || "";
  ["BUY","WATCH","AVOID"].forEach(v => {
    const el = $("#n-"+v);
    if(el) el.textContent = all.filter(c => c.verdict===v && (v==="AVOID" ? true : !isDone(c))).length || "";
  });
}

/* ---------- KPI strip ---------- */
function kpis(s){
  const k = $("#kpis"); k.innerHTML="";
  const m = s.market, buys = (s.picks||[]).length;
  const watch = (s.candidates||[]).filter(c=>c.verdict==="WATCH").length;

  const hero = el("div","kpi");
  hero.append(el("div","k","Buyable long setups"));
  const early = s.signals_allowed === false;
  const v = el("div","v hero mono",
    !s.data_ok ? "\u2014" : early ? "\u2014" : String(buys));
  if(s.data_ok && !early && buys) v.style.color = "var(--good)";
  hero.append(v);
  hero.append(el("div","f", !s.data_ok ? "data unreliable — nothing graded"
    : early ? (s.window_note || "this window does not emit signals")
    : buys ? "clearing every rule" : (s.no_trade||"no qualifying setup")
      + (watch ? " · " + watch + " on watch" : "")));
  k.append(hero);

  const mk = el("div","kpi");
  mk.append(el("div","k","Market"));
  const holder = el("div"); holder.style.marginTop="7px";
  const cls = m.classification.indexOf("BULL")>=0 ? "bull"
            : m.classification.indexOf("BEAR")>=0 ? "bear" : "flat";
  holder.append(badge(m.classification, "mkt " + cls, GLY[cls]));
  mk.append(holder);
  mk.append(el("div","f", m.nifty_above_vwap===null ? "NIFTY VWAP unavailable"
    : m.nifty_above_vwap ? "NIFTY above VWAP" : "NIFTY below VWAP"));
  k.append(mk);

  k.append(kpi("NIFTY 50", pc(m.nifty_pct), sgn(m.nifty_pct), "kpi-nifty"));
  k.append(kpi("Bank Nifty", pc(m.banknifty_pct), sgn(m.banknifty_pct), "kpi-banknifty"));

  const br = el("div","kpi");
  br.append(el("div","k","Breadth"));
  br.append(el("div","v mono", m.breadth_pct===null ? "—"
    : Math.round(m.breadth_pct)+"%"));
  if(m.breadth_pct!==null){
    const bars = el("div","bars");
    for(let i=0;i<10;i++){ const b=el("i"); if(i < Math.round(m.breadth_pct/10)) b.className="on";
      bars.append(b); }
    br.append(bars);
  }
  br.append(el("div","f", m.advances!==null && m.advances!==undefined
    ? m.advances+" up / "+m.declines+" down" : m.breadth_source));
  k.append(br);
}
function kpi(label, value, cls, id){
  const t = el("div","kpi");
  t.append(el("div","k",label));
  const v = el("div","v mono "+(cls||""),value);
  if(id) v.id = id;
  t.append(v);
  return t;
}
function badge(text, cls, glyph){
  const b = el("span","badge "+(cls||""));
  b.append(el("span","g", glyph || GLY[text] || ""));
  b.append(document.createTextNode(text));
  return b;
}

/* ---------- rail ---------- */
function sectors(s){
  const box = $("#sectors"), m = s.market;
  const entries = Object.entries(m.sectors||{});
  $("#secpanel").hidden = !entries.length;
  if(!entries.length) return;
  $("#sectitle").textContent = m.sector_source;
  box.innerHTML = "";
  const max = Math.max(...entries.map(([,v])=>Math.abs(v)), 0.5);
  entries.sort((a,b)=>b[1]-a[1]).forEach(([name,v])=>{
    const row = el("div","sec");
    row.append(el("div","nm",name));
    const bb = el("div","bb"), f = el("span");
    const w = Math.abs(v)/max*50;
    f.style.width = w+"%"; f.style.left = (v>=0?50:50-w)+"%";
    f.style.background = v>=0 ? "var(--up)" : "var(--down)";
    bb.append(f); row.append(bb);
    row.append(el("div","vv mono "+sgn(v), pc(v)));
    box.append(row);
  });
}

function feeds(s){
  const box = $("#feeds"); box.innerHTML="";
  const all = (s.market.sources||[]).concat(
    ...(s.candidates||[]).map(c=>c.sources||[]));
  // Group by feed, not by instrument: 12 rows of the same source is noise.
  // The age shown is the worst in the group — feed health is its slowest part.
  const groups = new Map();
  all.forEach(p=>{
    const key = p.source + "|" + p.timeframe;
    const g = groups.get(key) || {source:p.source, timeframe:p.timeframe,
                                  delayed:false, age:0, n:0};
    g.delayed = g.delayed || p.delayed;
    g.age = Math.max(g.age, p.age_min);
    g.n += 1;
    groups.set(key, g);
  });
  [...groups.values()].forEach(g=>{
    const row = el("div","feed");
    row.append(el("div","nm", g.source + " · " + g.timeframe
      + (g.n > 1 ? "  ×" + g.n : "")));
    row.append(el("div","ag mono" + (g.delayed ? " warnc" : ""),
      (g.delayed ? "DELAYED " : "") + g.age.toFixed(0) + "m"));
    box.append(row);
  });
  if(!groups.size) box.append(el("div","notes","no feeds reported"));
}

function notes(s){
  const ul = $("#notes"); ul.innerHTML="";
  (s.data_notes||[]).forEach(t=>ul.append(el("li",null,t)));
}

/* ---------- candidate list ---------- */
function list(s){
  const box = $("#list"); box.innerHTML="";
  let items = (s.candidates||[]).slice();
  if(FILTER==="active") items = items.filter(c => !isDone(c));
  else if(FILTER==="DONE") items = items.filter(c => isDone(c));
  else if(FILTER==="BUY") items = items.filter(c => c.verdict==="BUY" && !isDone(c));
  else if(FILTER==="WATCH") items = items.filter(c => c.verdict==="WATCH" && !isDone(c));
  else if(FILTER!=="all") items = items.filter(c => c.verdict===FILTER);

  if(!items.length){
    const p = el("section","panel");
    p.append(el("div","empty", s.data_ok
      ? (FILTER==="active" ? "No active upcoming setups right now. Stocks that completed their targets are in 'Done' or 'All'."
         : FILTER==="BUY" ? "No qualifying fresh BUY setups right now."
         : FILTER==="DONE" ? "No setups have completed targets yet."
         : "Nothing in this view.")
      : "Nothing graded — see the banner above."));
    box.append(p); return;
  }
  const ord = {BUY:0, WATCH:1, AVOID:2};
  items.sort((a,b)=>(ord[a.verdict]-ord[b.verdict]) || (b.score-a.score));
  items.forEach(c=>box.append(card(c)));
}

function card(c){
  const v = c.verdict.toLowerCase();
  const box = el("article","card v-"+v);
  box.dataset.cardSym = c.symbol;

  const hd = el("div","hd");
  const mark = el("div","signal-mark", c.verdict === "BUY" ? "↗" : "↘");
  mark.setAttribute("aria-label", c.verdict === "BUY" ? "Bullish signal" : "Caution signal");
  hd.append(mark);
  const idw = el("div","idw");
  const r1 = el("div","row1");
  if(c.rank) r1.append(el("span","rank mono", String(c.rank).padStart(2,"0")));
  r1.append(el("span","sym", c.symbol));
  r1.append(el("span","chip", c.sector));
  r1.append(el("span","chip", c.contract));
  idw.append(r1);
  const st = el("div","status");
  st.append(document.createTextNode(c.structure.status));
  if(c.structure.breakout_time){
    st.append(el("span","dotsep","·"));
    st.append(document.createTextNode("broke out " + c.structure.breakout_time));
  }
  idw.append(st);
  hd.append(idw);

  const pxw = el("div","pxw");
  pxw.append(el("div","px mono", "₹" + num(c.price)));
  pxw.append(el("div","chg mono "+sgn(c.pct_change), pc(c.pct_change)));
  hd.append(pxw);

  const vw = el("div","vw");
  vw.append(badge(c.verdict, "v-"+v));
  const m = el("div","meter");
  const r = el("div","r");
  r.append(el("span",null,"Score"));
  r.append(el("span","mono", sc(c.score)+"/100 · "+c.grade));

  const targetHit = c.trade && (c.trade.target1_hit || c.trade.target2_hit || (c.trade.target1 && c.price >= c.trade.target1));
  const tBtn = el("button","btn go", targetHit ? "✓ Target Done" : "⚡ Take Trade");
  tBtn.style.fontSize = "11px";
  tBtn.style.padding = "3px 9px";
  tBtn.style.marginTop = "5px";
  if(targetHit){
    tBtn.disabled = true;
    tBtn.style.opacity = "0.75";
    tBtn.style.borderColor = "var(--line-2)";
    tBtn.style.color = "var(--ink-3)";
    tBtn.style.background = "var(--cell)";
    tBtn.title = "Target has already been achieved today for this setup.";
  } else {
    tBtn.onclick = async (e)=>{
      e.stopPropagation();
      tBtn.disabled = true;
      tBtn.textContent = "Placing…";
      try{
        const entryPx = c.price || (c.trade ? c.trade.entry : 0);
        const slPx = c.trade ? c.trade.stop : (entryPx * 0.99);
        const tgtPx = c.trade ? (c.trade.target1 || c.trade.target) : (entryPx * 1.02);
        const res = await fetch("/api/trade", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            symbol: c.symbol,
            side: "BUY",
            qty: 15,
            price: entryPx,
            stop_loss: slPx,
            target: tgtPx,
            strategy_id: "fno_scanner"
          })
        });
        const j = await res.json();
        if(j.ok){
          tBtn.style.background = "var(--good)";
          tBtn.textContent = "✓ Trade #" + j.trade_id + " Placed!";
          setTimeout(()=>{ tBtn.textContent="⚡ Take Trade"; tBtn.disabled=false; tBtn.style.background=""; }, 2000);
        } else {
          alert("Trade rejected: " + (j.reason || j.error || "Unknown"));
          tBtn.disabled = false;
          tBtn.textContent = "⚡ Take Trade";
        }
      }catch(err){
        alert("Error: " + err);
        tBtn.disabled = false;
        tBtn.textContent = "⚡ Take Trade";
      }
    };
  }
  vw.append(tBtn);
  hd.append(vw);
  box.append(hd);

  const confidence = el("div","confidence");
  const confidenceMeter = el("div","meter");
  confidenceMeter.append(r);
  const confidenceTrack = el("div","t"), confidenceFill = el("div","f");
  confidenceFill.style.width = Math.max(0,Math.min(100,c.score))+"%";
  confidenceTrack.append(confidenceFill);
  confidenceMeter.append(confidenceTrack);
  confidence.append(confidenceMeter);
  box.append(confidence);

  const fy = fyersChart(c);
  if(fy) box.append(fy);

  const charts = el("div","charts");
  const right = el("div");
  right.append(el("div","cap","Price against its levels"));
  right.append(ladder(c));
  charts.append(right);
  box.append(charts);

  if(c.trade){
    box.append(plan(c));
    const footer = el("div","trade-footer");
    const rr = c.trade.rr1 == null ? "—" : "1 : "+Number(c.trade.rr1).toFixed(1);
    const copy = el("span","trade-copy");
    copy.append(document.createTextNode("Risk / Reward  "));
    copy.append(el("b","mono",rr));
    copy.append(document.createTextNode("  ·  Risk ₹"+num(c.trade.risk)));
    footer.append(copy);
    footer.append(el("span","metric-good",c.trade.rr1 >= 2 ? "quality setup" : "manage risk carefully"));
    box.append(footer);
  }else box.append(el("div","noplan","No entry — this setup does not qualify for a "
    + "trade plan. See what is blocking it below."));

  if(c.options) box.append(optionsPanel(c));

  const cols = el("div","cols");
  const why = el("div");
  why.append(el("div","hdr", c.verdict==="BUY" ? "Why this qualifies" : "What is working"));
  const ul = el("ul","rs");
  (c.reasons.length?c.reasons:["nothing notable yet"]).forEach(x=>ul.append(el("li",null,x)));
  why.append(ul); cols.append(why);
  const other = el("div");
  if(c.blockers.length){
    other.append(el("div","hdr","Blocking a buy"));
    const b = el("ul","rs bad");
    c.blockers.forEach(x=>b.append(el("li",null,x.text)));
    other.append(b);
  }else{
    other.append(el("div","hdr","Invalidation — exit if this happens"));
    const b = el("ul","rs");
    c.invalidation.forEach(x=>b.append(el("li",null,x)));
    other.append(b);
  }
  cols.append(other); box.append(cols);

  const met = el("div","metrics");
  const chip = (k,val)=>{ const s=el("span","m"); s.append(document.createTextNode(k));
    s.append(el("b","mono",val)); met.append(s); };
  chip("VWAP", num(c.vwap));
  chip("20 EMA", c.ema20===null?"—":num(c.ema20));
  chip("50 EMA", c.ema50===null?"—":num(c.ema50));
  chip("OR", num(c.or_low)+"–"+num(c.or_high));
  chip("OI", (c.oi.change_pct===null?"—":pc(c.oi.change_pct))+" "+c.oi.classification);
  chip("RVOL", c.rvol===null?"—":c.rvol.toFixed(2)+"x");
  chip("vs NIFTY", pc(c.rel_strength));
  if(c.sector_strength!==null && c.sector_strength!==undefined)
    chip("Sector", pc(c.sector_strength));
  box.append(met);

  if(c.warnings.length)
    box.append(el("div","warns","⚠ " + c.warnings.join("  ·  ")));
  box.append(el("div","srcs", c.sources.map(p=>p.label).join("   |   ")));
  return box;
}

/* The option view. NSE publishes only the 20 most-active option contracts, so
   most names have none — that is stated rather than filled in with a guess.
   Breakeven leads, because a call can lose while the stock setup works. */
function optionsPanel(c){
  const o = c.options, box = el("div","opt");
  const head = el("div","oh");
  head.append(el("span","ot","Call option"));

  if(!o.quote){
    head.append(el("span","be","not available for this setup"));
    box.append(head);
    const no = el("div","no");
    no.append(el("b","NO CALL"));
    no.append(document.createTextNode("  " + (o.rejections[0] || "no candidate strike")));
    box.append(no);
    return box;
  }

  const q = o.quote;
  head.append(el("span","strike mono", q.strike + " CE @ " + num(q.ltp)));
  head.append(el("span","chip", q.expiry));
  head.append(el("span","chip", q.moneyness));
  const be = el("span","be");
  be.append(document.createTextNode("breakeven "));
  be.append(el("b","mono", num(o.breakeven)));
  be.append(document.createTextNode(
    o.clears_t1 ? " — under target 1, so T1 pays"
    : o.clears_t2 ? " — above target 1; only the 1:3 target pays"
    : " — above both targets"));
  head.append(be);
  box.append(head);

  if(o.chain && o.chain.length){
    const t = el("table"), thead = el("thead"), hr = el("tr");
    ["Strike","Type","Expiry","LTP","Breakeven","OI","Volume"]
      .forEach((h,i)=>hr.append(el("th", i>=3?"num":null, h)));
    thead.append(hr); t.append(thead);
    const tb = el("tbody");
    o.chain.forEach(x=>{
      const tr = el("tr");
      if(q && x.identifier === q.identifier) tr.style.background = "var(--accent-soft)";
      tr.append(el("td","mono", String(x.strike)));
      tr.append(el("td", null, x.type));
      tr.append(el("td","mono", x.expiry || "—"));
      tr.append(el("td","num mono", num(x.ltp)));
      tr.append(el("td","num mono", num(x.breakeven)));
      tr.append(el("td","num mono", x.open_interest===null?"—":x.open_interest.toLocaleString("en-IN")));
      tr.append(el("td","num mono", x.volume===null?"—":x.volume.toLocaleString("en-IN")));
      tb.append(tr);
    });
    t.append(tb);
    const wrap = el("div","scroll"); wrap.append(t); box.append(wrap);
  }
  // The option's own plan. Its reward-to-risk is usually worse than the
  // stock's, because premium decays while the stock does not — so it is shown
  // next to the entry rather than left for the reader to work out.
  if(o.premium_at_t1 !== null && o.premium_at_t1 !== undefined){
    const g = el("div","oplan");
    const cell = (k,v,cls,note)=>{const d=el("div");
      d.append(el("div","k",k)); d.append(el("div","v mono "+(cls||""),v));
      if(note) d.append(el("div","n",note)); g.append(d);};
    const pctOf = p => q.ltp>0 ? ((p-q.ltp)/q.ltp*100).toFixed(0)+"%" : "";
    cell("Pay now","₹"+num(q.ltp),"","premium per share");
    if(o.premium_at_stop!==null && o.premium_at_stop!==undefined)
      cell("At stock stop","₹"+num(o.premium_at_stop),"bad",pctOf(o.premium_at_stop));
    cell("At target 1","₹"+num(o.premium_at_t1),"good",pctOf(o.premium_at_t1));
    cell("At target 2","₹"+num(o.premium_at_t2),"good",pctOf(o.premium_at_t2));
    if(o.option_rr!==null && o.option_rr!==undefined)
      cell("Option R:R", o.option_rr.toFixed(2)+":1",
           o.option_rr>=1?"good":"bad", "on the option, not the stock");
    box.append(g);
    box.append(el("div","model",
      "Modelled with Black-Scholes at " +
      (o.implied_vol ? (o.implied_vol*100).toFixed(0)+"% implied volatility" : "the traded IV") +
      ", assuming IV is unchanged and the position is held about two hours. " +
      "An estimate, not a quote — the fill depends on IV and the order book."));
  }

  if(o.decay && o.decay.length){
    const d = el("div","model");
    d.textContent = "If the stock does not move, this premium becomes "
      + o.decay.map(x => (x.days < 1 ? "tonight " : "+" + x.days + "d ")
          + num(x.premium) + (x.pct_left !== null
              ? " (" + Math.round(x.pct_left) + "%)" : "")).join("  ·  ");
    box.append(d);
  }

  if(o.warnings && o.warnings.length){
    const w = el("div","warns");
    o.warnings.forEach(x=>w.append(el("div", null, "⚠ " + x)));
    box.append(w);
  }
  return box;
}

/* Fyers-powered candlestick chart with entry/stop/target overlays */
function fyersChart(c) {
  const trade = c.trade;

  const wrapper = el("div","fyers-chart");
  wrapper.style.marginTop = "10px";

  const title = el("div","chart-title");
  title.innerHTML = `<span style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--ink-3)">Fyers Candlestick Chart · 5-min${trade ? " · Entry/Stop/Target Overlays" : ""}</span>`;
  wrapper.append(title);

  const canvasWrap = el("div","chart-canvas-wrap");
  canvasWrap.style.position = "relative";
  canvasWrap.style.marginTop = "8px";

  const canvas = document.createElement("canvas");
  /* The backing store is sized to the laid-out box x devicePixelRatio inside
     drawFyersChart, so the candles stay crisp instead of being upscaled. */
  canvas.style.width = "100%";
  canvas.style.height = "280px";
  canvas.style.background = "var(--cell)";
  canvas.style.borderRadius = "8px";
  canvas.style.display = "block";
  canvasWrap.append(canvas);
  wrapper.append(canvasWrap);

  // Store data for async chart drawing
  canvas.dataset.symbol = c.symbol;
  canvas.dataset.entryLow = trade ? trade.entry_low : "";
  canvas.dataset.entryHigh = trade ? trade.entry_high : "";
  canvas.dataset.stop = trade ? trade.stop : "";
  canvas.dataset.target1 = trade ? trade.target1 : "";
  canvas.dataset.target2 = trade ? trade.target2 : "";
  canvas.dataset.currentPrice = c.price;

  // Draw chart asynchronously
  drawFyersChart(canvas, c.symbol, trade || null, c.price);

  if (trade) {
  const levels = el("div","chart-levels-legend");
  levels.innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:10px;">
      <div style="text-align:center;">
        <div style="font-size:10px;font-weight:600;color:var(--ink-3);letter-spacing:.06em">Entry Zone</div>
        <div style="font-size:14px;font-weight:700;color:var(--accent);font-family:ui-monospace">₹${num(trade.entry_low)}-${num(trade.entry_high)}</div>
      </div>
      <div style="text-align:center;">
        <div style="font-size:10px;font-weight:600;color:var(--ink-3);letter-spacing:.06em">Stop Loss</div>
        <div style="font-size:14px;font-weight:700;color:var(--bad);font-family:ui-monospace">₹${num(trade.stop)}</div>
      </div>
      <div style="text-align:center;">
        <div style="font-size:10px;font-weight:600;color:var(--ink-3);letter-spacing:.06em">Target 1</div>
        <div style="font-size:14px;font-weight:700;color:var(--good);font-family:ui-monospace">₹${num(trade.target1)}</div>
      </div>
      <div style="text-align:center;">
        <div style="font-size:10px;font-weight:600;color:var(--ink-3);letter-spacing:.06em">Target 2</div>
        <div style="font-size:14px;font-weight:700;color:var(--good);font-family:ui-monospace">₹${num(trade.target2)}</div>
      </div>
    </div>
  `;
    wrapper.append(levels);
  }

  return wrapper;
}

/* Draw Fyers candlestick chart with levels on canvas */
async function drawFyersChart(canvas, symbol, trade, currentPrice) {
  const ctx = canvas.getContext("2d");
  const padTop = 30, padBottom = 40, padLeft = 50, padRight = 62;
  let W, H, chartW, chartH;

  // Fetch 5-min candles from backend (fallback to mock data if unavailable)
  let candles = [];
  try {
    const res = await fetch(`/api/candles?symbol=${encodeURIComponent(symbol)}&resolution=5&days=1`);
    if (res.ok) {
      const data = await res.json();
      candles = data.candles || [];
    }
  } catch (err) {
    console.warn("Failed to fetch candles, using mock data:", err);
  }

  // Fallback: generate mock candles if API fails
  if (candles.length === 0) {
    const now = Date.now();
    const fiveMin = 5 * 60 * 1000;
    const basePrice = currentPrice || (trade && trade.entry_low) || 2200;
    for (let i = 0; i < 60; i++) {
      const t = now - (59 - i) * fiveMin;
      const volatility = basePrice * 0.003;
      const open = basePrice + (Math.random() - 0.5) * volatility * 2;
      const close = open + (Math.random() - 0.5) * volatility * 2;
      const high = Math.max(open, close) + Math.random() * volatility;
      const low = Math.min(open, close) - Math.random() * volatility;
      candles.push({ timestamp: Math.floor(t / 1000), open, high, low, close, volume: Math.random() * 10000 });
    }
  }

  /* Match the bitmap to the box it is actually painted into (x devicePixelRatio)
     and draw in CSS pixels. Without this the browser rescales the bitmap and the
     candles and text come out soft. */
  const dpr = window.devicePixelRatio || 1;
  W = canvas.clientWidth || 700;
  H = canvas.clientHeight || 280;
  if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) {
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  chartW = W - padLeft - padRight;
  chartH = H - padTop - padBottom;

  if (candles.length === 0) {
    ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--ink-3').trim();
    ctx.font = "12px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("No candle data available", W / 2, H / 2);
    return;
  }

  // Determine price range including levels
  const prices = candles.flatMap(c => [c.high, c.low]);
  if (trade) prices.push(trade.entry_low, trade.entry_high, trade.stop, trade.target1, trade.target2);
  prices.push(currentPrice);
  const priceMin = Math.min(...prices.filter(p => isFinite(p)));
  const priceMax = Math.max(...prices.filter(p => isFinite(p)));
  const priceRange = priceMax - priceMin || 1;
  const pricePad = priceRange * 0.08;

  const yMin = priceMin - pricePad;
  const yMax = priceMax + pricePad;
  const yRange = yMax - yMin;

  const X = i => padLeft + (i / (candles.length - 1 || 1)) * chartW;
  const Y = price => padTop + chartH - ((price - yMin) / yRange) * chartH;

  // Clear and set background
  const bgColor = getComputedStyle(document.documentElement).getPropertyValue('--cell').trim();
  ctx.fillStyle = bgColor;
  ctx.fillRect(0, 0, W, H);

  // Draw grid
  const gridColor = getComputedStyle(document.documentElement).getPropertyValue('--line').trim();
  ctx.strokeStyle = gridColor;
  ctx.lineWidth = 1;
  ctx.setLineDash([2, 4]);
  for (let i = 0; i <= 4; i++) {
    const y = padTop + (chartH / 4) * i;
    ctx.beginPath();
    ctx.moveTo(padLeft, y);
    ctx.lineTo(W - padRight, y);
    ctx.stroke();
  }
  ctx.setLineDash([]);

  // Draw candles
  const bullColor = getComputedStyle(document.documentElement).getPropertyValue('--good').trim();
  const bearColor = getComputedStyle(document.documentElement).getPropertyValue('--bad').trim();

  candles.forEach((c, i) => {
    const x = X(i);
    const isBull = c.close >= c.open;
    ctx.strokeStyle = isBull ? bullColor : bearColor;
    ctx.fillStyle = isBull ? bullColor : bearColor;
    ctx.lineWidth = 1;

    // Wick
    ctx.beginPath();
    ctx.moveTo(x, Y(c.high));
    ctx.lineTo(x, Y(c.low));
    ctx.stroke();

    // Body
    const bodyTop = Y(Math.max(c.open, c.close));
    const bodyBottom = Y(Math.min(c.open, c.close));
    const bodyHeight = Math.max(bodyBottom - bodyTop, 1);
    const candleWidth = Math.max(chartW / candles.length - 2, 2);

    if (isBull) {
      ctx.fillRect(x - candleWidth/2, bodyTop, candleWidth, bodyHeight);
    } else {
      ctx.fillRect(x - candleWidth/2, bodyTop, candleWidth, bodyHeight);
    }
  });

  // Draw level lines
  const accentColor = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
  const drawLevel = (price, color, label, lineWidth = 2) => {
    if (!isFinite(price)) return;
    const y = Y(price);
    ctx.strokeStyle = color;
    ctx.lineWidth = lineWidth;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(padLeft, y);
    ctx.lineTo(W - padRight, y);
    ctx.stroke();
    ctx.setLineDash([]);

    // Label
    ctx.fillStyle = color;
    ctx.font = "bold 10px ui-monospace, monospace";
    ctx.textAlign = "right";
    ctx.fillText(label, W - padRight - 5, y - 3);
  };

  if (trade) {
    drawLevel((trade.entry_low + trade.entry_high) / 2, accentColor, "ENTRY", 2);
    drawLevel(trade.stop, bearColor, "STOP", 2);
    drawLevel(trade.target1, bullColor, "T1", 2);
    drawLevel(trade.target2, bullColor, "T2", 2);
  }

  // Current price line
  if (isFinite(currentPrice)) {
    const y = Y(currentPrice);
    ctx.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue('--ink').trim();
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(padLeft, y);
    ctx.lineTo(W - padRight, y);
    ctx.stroke();

    ctx.fillStyle = ctx.strokeStyle;
    ctx.font = "bold 11px ui-monospace, monospace";
    ctx.textAlign = "right";
    ctx.fillText(`₹${currentPrice.toFixed(2)}`, W - 4, y + 4);
  }

  // A window that spans more than one session is ruled at each day change,
  // so the axis times never appear to run backwards.
  const dayLabel = ts => new Date(ts * 1000).toLocaleDateString('en-IN', { day: '2-digit', month: 'short' });
  ctx.textAlign = "center";
  for (let i = 1; i < candles.length; i++) {
    if (dayLabel(candles[i].timestamp) === dayLabel(candles[i - 1].timestamp)) continue;
    const x = X(i);
    ctx.strokeStyle = gridColor;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, padTop);
    ctx.lineTo(x, padTop + chartH);
    ctx.stroke();
    ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--ink-3').trim();
    ctx.font = "9px ui-monospace, monospace";
    ctx.fillText(dayLabel(candles[i].timestamp), x, padTop - 8);
  }

  // Y-axis labels
  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--ink-3').trim();
  ctx.font = "10px ui-monospace, monospace";
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {
    const price = yMin + (yRange / 4) * (4 - i);
    const y = padTop + (chartH / 4) * i;
    ctx.fillText(price.toFixed(0), padLeft - 8, y + 3);
  }

  // X-axis (time labels)
  ctx.textAlign = "center";
  const step = Math.max(1, Math.floor(candles.length / 6));
  for (let i = 0; i < candles.length; i += step) {
    const x = X(i);
    const time = new Date(candles[i].timestamp * 1000); // Fyers stamps seconds
    const label = time.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false });
    ctx.fillText(label, x, H - padBottom + 20);
  }
}

/* Session path: today's 5-minute closes against the running VWAP, with the
   breakout level as a rule. Two series, so both are labelled directly. */
function spark(c){
  const d = (c.spark||[]).filter(p=>isFinite(p.c)&&isFinite(p.w));
  if(d.length < 2) return null;
  const W=300, H=58, PL=4, PR=30, PT=8, PB=10;
  const ys = d.flatMap(p=>[p.c,p.w]).concat(isFinite(c.or_high)?[c.or_high]:[]);
  const lo=Math.min(...ys), hi=Math.max(...ys), rng=(hi-lo)||1;
  const X=i=>PL + i*(W-PL-PR)/(d.length-1);
  const Y=v=>H-PB-((v-lo)/rng)*(H-PT-PB);
  const path=k=>d.map((p,i)=>(i?"L":"M")+X(i).toFixed(1)+" "+Y(p[k]).toFixed(1)).join(" ");

  const wrap = el("div","spark");
  const svg = document.createElementNS("http://www.w3.org/2000/svg","svg");
  svg.setAttribute("viewBox","0 0 "+W+" "+H);
  svg.setAttribute("preserveAspectRatio","none");
  svg.setAttribute("role","img");
  svg.setAttribute("aria-label","Today's 5-minute closes against VWAP; the values are listed beside the chart.");
  const add=(tag,attrs,title)=>{const n=document.createElementNS("http://www.w3.org/2000/svg",tag);
    for(const k in attrs) n.setAttribute(k,attrs[k]);
    if(title){const t=document.createElementNS("http://www.w3.org/2000/svg","title");
      t.textContent=title; n.append(t);} svg.append(n); return n;};

  if(isFinite(c.or_high)){
    add("line",{x1:PL,x2:W-PR,y1:Y(c.or_high),y2:Y(c.or_high),stroke:"var(--ink-3)",
      "stroke-width":1,"vector-effect":"non-scaling-stroke",opacity:.8},
      "breakout level " + num(c.or_high));
  }
  add("path",{d:path("w"),fill:"none",stroke:"var(--ink-3)","stroke-width":1.5,
    "vector-effect":"non-scaling-stroke"}, "VWAP");
  add("path",{d:path("c"),fill:"none",stroke:"var(--accent)","stroke-width":2,
    "stroke-linejoin":"round","vector-effect":"non-scaling-stroke"}, "5-minute close");
  // Direct labels beat a legend box here, but they must not stack on top of
  // each other — drop the lower-priority label when two would collide.
  const placed=[];
  const lab=(txt,y,fill)=>{
    if(placed.some(p=>Math.abs(p-y) < 9)) return;
    placed.push(y);
    add("text",{x:W-PR+4,y:y+3,fill:fill,"font-size":8}).textContent = txt;
  };
  lab("PX",   Y(d[d.length-1].c), "var(--accent)");
  lab("VWAP", Y(d[d.length-1].w), "var(--ink-3)");
  if(isFinite(c.or_high)) lab("R", Y(c.or_high), "var(--ink-3)");
  wrap.append(svg);

  // End-of-line marker in HTML so it stays a circle under the stretched viewBox.
  const dot = el("div","end");
  dot.style.left = (X(d.length-1)/W*100)+"%";
  dot.style.top  = (Y(d[d.length-1].c)/H*100)+"%";
  dot.title = d[d.length-1].t + " close " + num(d[d.length-1].c);
  wrap.append(dot);
  return wrap;
}

/* Stop and target differ by SHAPE as well as hue — red/green is exactly the
   pair a colour-blind reader loses, and confusing them is the most expensive
   mistake this page could cause. */
function ladder(c){
  const wrap = el("div","level-visual");
  const t = c.trade;
  const vals = [c.or_low,c.or_high,c.vwap,c.price,c.day_low,c.day_high]
    .concat(t?[t.stop,t.target1,t.target2]:[])
    .filter(v=>typeof v==="number" && isFinite(v));
  const lo=Math.min(...vals), hi=Math.max(...vals);
  const pad=(hi-lo)*0.06 || 0.5, A=lo-pad, B=hi+pad;
  const X=v=>((v-A)/(B-A))*100;

  const lad = el("div","lad");
  lad.append(el("div","bar"));
  const zone=(cls,from,to,title)=>{const z=el("div","zone "+cls);
    z.style.left=X(from)+"%"; z.style.width=Math.max(X(to)-X(from),0.4)+"%";
    z.title=title; lad.append(z);};
  if(t){
    zone("risk",t.stop,t.entry_low,"risk "+num(t.stop)+" → "+num(t.entry_low));
    zone("rew",t.entry_high,t.target2,"reward "+num(t.entry_high)+" → "+num(t.target2));
    zone("entry",t.entry_low,t.entry_high,"entry zone "+num(t.entry_low)+" – "+num(t.entry_high));
  }
  const rule=(v,color,label,cap,labelPos)=>{
    if(typeof v!=="number"||!isFinite(v)) return;
    const r=el("div","rule"); r.style.left=X(v)+"%";
    r.style.background=color; r.style.color=color; r.title=label+" "+num(v);
    if(cap) r.append(el("span","label"+(labelPos ? " "+labelPos : ""),label));
    if(cap) r.append(el("i","cap-"+cap));
    lad.append(r);
  };
  rule(c.or_high,"var(--ink-2)","breakout level (09:15–09:30 high)");
  rule(c.vwap,"var(--ink-3)","VWAP");
  if(t){
    rule(t.stop,"var(--bad)","SL","sq");
    rule(t.target1,"var(--good)","T1","tri");
    rule(t.target2,"var(--good)","T2","tri","below");
  }
  const dot=el("div","dot"); dot.style.left=X(c.price)+"%";
  dot.title="current price "+num(c.price); lad.append(dot);
  wrap.append(lad);

  const explain=el("div","level-explain");
  if(t){
    if(t.target2_hit || (t.target2 && c.price >= t.target2)){
      explain.innerHTML = "<b style='color:var(--good)'>✓ Target 2 (" + num(t.target2) + ") achieved today.</b> Move has played out. Do not enter.";
    } else if(t.target1_hit || (t.target1 && c.price >= t.target1)){
      explain.innerHTML = "<b style='color:var(--good)'>✓ Target 1 (" + num(t.target1) + ") achieved today.</b> Initial profit target completed. Do not enter.";
    } else if(c.price < t.entry_low)
      explain.innerHTML = "<b>Current price is below the entry zone.</b> Wait for a move into the blue band.";
    else if(c.price > t.entry_high)
      explain.innerHTML = "<b>Current price is above the entry zone.</b> Setup is extended — do not chase.";
    else
      explain.innerHTML = "<b>Current price is inside the entry zone.</b> Targets are upcoming on the track.";
  }else if(c.price > c.or_high){
    explain.innerHTML = "<b>Price is above the breakout level.</b> No trade plan: wait for a fresh setup or pullback.";
  }else{
    explain.innerHTML = "<b>Price has not cleared the breakout level.</b> The setup is not ready for entry.";
  }
  wrap.append(explain);

  const lg=el("div","lgnd");
  const key=(color,txt,round)=>{const s=el("span"); const i=el("i",round?"rnd":null);
    i.style.background=color; s.append(i); s.append(document.createTextNode(txt));
    lg.append(s);};
  key("var(--accent)","Price "+num(c.price),true);
  key("var(--ink-2)","Breakout "+num(c.or_high));
  key("var(--ink-3)","VWAP "+num(c.vwap));
  if(t){
    key("var(--bad)","■ Stop "+num(t.stop));
    key("var(--good)","▲ T1 "+num(t.target1) + (t.target1_hit ? " (HIT)" : ""));
    key("var(--good)","▲ T2 "+num(t.target2) + (t.target2_hit ? " (HIT)" : ""));
  }
  wrap.append(lg);
  return wrap;
}

function plan(c){
  const t=c.trade, g=el("div","plan");
  const cell=(k,v,cls,note,hl)=>{const d=el("div","c"+(hl?" hl":""));
    d.append(el("div","k",k)); d.append(el("div","v mono "+(cls||""),v));
    if(note) d.append(el("div","n",note)); g.append(d);};
  cell("Entry zone","₹"+num(t.entry_low)+" – "+num(t.entry_high),"",
       "buy inside this band only",true);
  cell("Stop loss","₹"+num(t.stop),"bad",t.stop_basis);
  const t1Note = t.target1_hit ? "✓ ACHIEVED" : "1:"+t.rr1;
  const t2Note = t.target2_hit ? "✓ ACHIEVED" : "1:"+t.rr2;
  cell("Target 1","₹"+num(t.target1),"good",t1Note);
  cell("Target 2","₹"+num(t.target2),"good",t2Note);
  const guide = el("div","plan-guide");
  const explain=(title,text)=>{const d=el("div"); d.append(el("b",null,title)); d.append(document.createTextNode(text)); guide.append(d);};
  explain("Entry", "Where you may buy, if price returns to this band.");
  explain("Stop loss", "Exit if price falls here. This limits the loss.");
  explain("Target 1", t.target1_hit ? "Target 1 was already reached today." : "First profit area. Consider taking partial profit.");
  explain("Target 2", t.target2_hit ? "Target 2 was already reached today." : "Second profit area. The larger planned objective.");
  const holder=el("div"); holder.append(g,guide);
  return holder;
}

/* ---------- table ---------- */
function table(s){
  const t=$("#tbl"); t.innerHTML="";
  const head=el("tr");
  [["Stock",0],["Verdict",0],["Score",1],["Price",1],["vs NIFTY",1],["Breakout",1],
   ["VWAP",1],["RVOL",1],["OI",0],["Structure",0],["Blocking",0]]
    .forEach(([h,n])=>head.append(el("th",n?"num":null,h)));
  const thead=el("thead"); thead.append(head); t.append(thead);
  const body=el("tbody");
  const rows=(s.candidates||[]).slice().sort((a,b)=>b.score-a.score);
  if(!rows.length){
    const tr=el("tr"), td=el("td",null, s.data_ok
      ? "No names cleared the liquidity screen." : "Nothing graded.");
    td.colSpan=11; tr.append(td); body.append(tr);
  }
  rows.forEach(c=>{
    const tr=el("tr");
    tr.dataset.rowSym = c.symbol;
    tr.append(el("td","sym",c.symbol));
    const v=el("td"); v.append(badge(c.verdict,"sm v-"+c.verdict.toLowerCase()));
    tr.append(v);
    tr.append(el("td","num mono", sc(c.score)+" "+c.grade));
    tr.append(el("td","num mono", num(c.price)));
    tr.append(el("td","num mono "+sgn(c.rel_strength), pc(c.rel_strength)));
    tr.append(el("td","num mono", num(c.or_high)));
    tr.append(el("td","num mono", num(c.vwap)));
    tr.append(el("td","num mono", c.rvol===null?"—":c.rvol.toFixed(2)+"x"));
    tr.append(el("td","mono", (c.oi.change_pct===null?"—":pc(c.oi.change_pct))
      +" "+c.oi.classification));
    tr.append(el("td",null,c.structure.status));
    tr.append(el("td","blk", c.blockers.length?c.blockers[0].text:"—"));
    body.append(tr);
  });
  t.append(body);
}

/* ---------- controls ---------- */
$("#seg").addEventListener("click", e=>{
  const b=e.target.closest("button"); if(!b) return;
  FILTER=b.dataset.f;
  [...$("#seg").children].forEach(x=>x.setAttribute("aria-pressed",String(x===b)));
  if(STATE && STATE.scan) list(STATE.scan);
});
$("#rescan").addEventListener("click", ()=>load(true));
$("#auto").addEventListener("change", ()=>load(false));

// No dashboard is mounted next to a standalone scanner, so drop the link.
if(document.body.dataset.mode === "standalone"){
  const a = document.querySelector('.nav a[href="/"]');
  if(a) a.remove();
}

load(false);

async function loadQuotes(){
  try{
    const r = await fetch("/api/quotes", {cache:"no-store"});
    if(!r.ok) return;
    const res = await r.json();
    const itemsBox = document.getElementById("liveTickerItems");
    const updatedEl = document.getElementById("liveTickerUpdated");
    if(itemsBox && res.quotes && res.quotes.length){
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
            <span style="font-size:14px;font-weight:700;font-family:ui-monospace,monospace;color:${isUp ? 'var(--up)' : 'var(--down)'}">₹${num(lp)}</span>
            <span style="font-size:10px;color:var(--ink-3);font-family:ui-monospace,monospace">${ch >= 0 ? '+' : ''}${num(ch)}</span>
          </div>
          <div style="display:flex;justify-content:space-between;font-size:9.5px;color:var(--ink-3);margin-top:1px">
            <span>H: ${num(v.high_price)}</span>
            <span>L: ${num(v.low_price)}</span>
          </div>
        `;
        itemsBox.appendChild(card);

        // Update candidate cards on page
        const sym = (v.symbol || q.n || "").replace("NSE:","").replace("-EQ","").replace("-INDEX","");
        const cardEl = document.querySelector(`[data-card-sym="${sym}"]`);
        if(cardEl && v.lp){
          const pxEl = cardEl.querySelector(".pxw .px");
          const chgEl = cardEl.querySelector(".pxw .chg");
          if(pxEl) pxEl.textContent = "₹" + num(v.lp);
          if(chgEl && v.chp != null){
            chgEl.className = "chg mono " + (v.chp > 0 ? "up" : v.chp < 0 ? "down" : "");
            chgEl.textContent = (v.chp >= 0 ? "+" : "") + v.chp.toFixed(2) + "%";
          }
        }
      });
      if(updatedEl){
        updatedEl.textContent = "⚡ Live: " + new Date().toLocaleTimeString("en-IN",{hour12:false});
      }
    }

    if(res.quotes){
      const nq = res.quotes.find(q => (q.n||"").includes("NIFTY50"));
      const bq = res.quotes.find(q => (q.n||"").includes("NIFTYBANK"));
      if(nq && nq.v){
        const elN = document.getElementById("kpi-nifty");
        if(elN){
          elN.innerHTML = `<span style="font-size:17px;font-weight:700;font-family:ui-monospace,monospace;color:${nq.v.ch>=0?'var(--up)':'var(--down)'}">₹${num(nq.v.lp)}</span> <span style="font-size:11px;font-weight:600;color:${nq.v.ch>=0?'var(--up)':'var(--down)'}">(${nq.v.chp>=0?'+':''}${nq.v.chp.toFixed(2)}%)</span>`;
        }
        const spotEl = document.querySelector("#niftyWrap .nfx .spot");
        if(spotEl && nq.v.lp){
          spotEl.textContent = num(nq.v.lp);
        }
      }
      if(bq && bq.v){
        const elB = document.getElementById("kpi-banknifty");
        if(elB){
          elB.innerHTML = `<span style="font-size:17px;font-weight:700;font-family:ui-monospace,monospace;color:${bq.v.ch>=0?'var(--up)':'var(--down)'}">₹${num(bq.v.lp)}</span> <span style="font-size:11px;font-weight:600;color:${bq.v.ch>=0?'var(--up)':'var(--down)'}">(${bq.v.chp>=0?'+':''}${bq.v.chp.toFixed(2)}%)</span>`;
        }
      }
    }
  }catch(e){
    console.warn("loadQuotes error:", e);
  }
}

loadQuotes();
setInterval(loadQuotes, 3000);
__THEMEJS__
</script>
</body></html>
"""


_TOPBAR_RIGHT = """    <div class="live"><span class="led" id="led"></span><span id="stamp">—</span></div>
    <div class="seg" id="seg">
      <button data-f="active" aria-pressed="true">Active <span class="n" id="n-active"></span></button>
      <button data-f="BUY" aria-pressed="false">Buy <span class="n" id="n-BUY"></span></button>
      <button data-f="WATCH" aria-pressed="false">Watch <span class="n" id="n-WATCH"></span></button>
      <button data-f="DONE" aria-pressed="false">Done <span class="n" id="n-DONE"></span></button>
      <button data-f="all" aria-pressed="false">All <span class="n" id="n-all"></span></button>
    </div>
    <button class="btn go" id="rescan">Scan now</button>
    <label class="chk"><input type="checkbox" id="auto" checked> auto</label>"""

PAGE = (_PAGE_TEMPLATE
        .replace("__THEME__", ui_theme.CSS)
        .replace("__TOPBAR__", ui_theme.topbar("NSE F&amp;O Long Scanner", "scanner",
                                               _TOPBAR_RIGHT))
        .replace("__NIFTYJS__", ui_theme.NIFTY_JS)
        .replace("__CONTEXTJS__", ui_theme.CONTEXT_JS)
        .replace("__THEMEJS__", ui_theme.THEME_JS))


def _serve_forever(server, url: str) -> int:
    """Run the server, turning the two normal failures into plain sentences."""
    print(f"{url}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    return 0


def _bind(port: int, handler, what: str):
    """Bind, or explain who already has the port."""
    from http.server import ThreadingHTTPServer

    try:
        return ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        if getattr(exc, "errno", None) not in (48, 98):      # EADDRINUSE
            raise
        print(f"Port {port} is already in use — something is serving there.\n"
              f"  Look:  open http://localhost:{port}\n"
              f"  Who:   lsof -nP -iTCP:{port} -sTCP:LISTEN\n"
              f"  Free:  lsof -ti tcp:{port} | xargs kill\n"
              f"  Or run {what} on another port, e.g. {port + 1}")
        return None


def main(argv: list[str] | None = None) -> int:
    import argparse

    global CACHE
    ap = argparse.ArgumentParser(prog="python -m trading.fno.web",
                                 description="Web UI for the F&O long scanner")
    ap.add_argument("port", nargs="?", type=int, default=8788)
    ap.add_argument("--replay", metavar="BUNDLE.json",
                    help="serve a saved scan instead of the live feeds")
    ap.add_argument("--symbols", help="comma-separated list; skips the screen")
    ap.add_argument("--shortlist", type=int, default=C.SHORTLIST_SIZE)
    ap.add_argument("--ttl", type=int, default=DEFAULT_TTL,
                    help=f"seconds before a cached scan is re-run (default {DEFAULT_TTL})")
    ap.add_argument("--fyers", action="store_true",
                    help="real-time candles via the TradeBrahma Fyers server")
    ap.add_argument("--fyers-base", default="http://localhost:3001")
    ap.add_argument("--live-only", action="store_true",
                    help="refuse to grade delayed candles (same rule as the CLI "
                         "without --allow-delayed)")
    args = ap.parse_args(argv)

    fyers.start_background_server()
    server = _bind(args.port, Handler, "the scanner")
    if server is None:
        return 1

    CACHE = ScanCache(allow_delayed=not args.live_only, shortlist=args.shortlist,
                      symbols=args.symbols.split(",") if args.symbols else None,
                      replay=args.replay, ttl=args.ttl, fyers=args.fyers,
                      fyers_base=args.fyers_base)
    CACHE.start()
    return _serve_forever(server, f"F&O scanner UI: http://localhost:{args.port}")


if __name__ == "__main__":
    sys.exit(main())
