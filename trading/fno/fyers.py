"""Real-time candles and quotes from Fyers API v3.

Direct integration using official fyers_apiv3 library with:
1. Automatic OAuth flow and token persistence in data/fyers_token.json.
2. Built-in OAuth callback server on port 3001 (matching redirect URI).
3. Zero-delay 5-minute OHLCV candles for equities and indices.
4. Fallback to yfinance if token is expired or Fyers is unreachable.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pandas as pd
from zoneinfo import ZoneInfo

from trading.config import (
    FYERS_APP_ID, FYERS_SECRET_ID, FYERS_REDIRECT_URI, FYERS_TOKEN_PATH,
)
from trading.fno import config as C
from trading.fno.data import LiveFeed
from trading.fno.models import IST, Candles, Provenance

DEFAULT_BASE = "http://localhost:3001"
TIMEOUT = 15

# Fyers symbol conventions. Equities are NSE:<SYM>-EQ; indices have their own
# names that do not follow the yfinance ones.
INDEX_SYMBOLS = {
    "^NSEI": "NSE:NIFTY50-INDEX",
    "^NSEBANK": "NSE:NIFTYBANK-INDEX",
}

_QUOTES_CACHE: dict[str, tuple[float, list[dict]]] = {}
_QUOTES_CACHE_TTL = 1.5  # seconds (ultra-fast for trailing stops, well within Fyers 10 req/sec limit)


def _quotes_from_yfinance(symbols: list[str]) -> list[dict]:
    """Fallback quote fetcher using yfinance for resilient live dashboard ticker."""
    import yfinance as yf
    sym_map = {}
    yf_tickers = []
    for s in symbols:
        clean = s.replace("NSE:", "").replace("-EQ", "").replace("-INDEX", "").strip()
        if clean in ("NIFTY50", "NIFTY", "^NSEI"):
            yt = "^NSEI"
        elif clean in ("NIFTYBANK", "BANKNIFTY", "^NSEBANK"):
            yt = "^NSEBANK"
        else:
            yt = f"{clean}.NS"
        sym_map[yt] = s
        yf_tickers.append(yt)
    out = []
    try:
        t_str = " ".join(set(yf_tickers))
        df = yf.download(t_str, period="2d", interval="1d", group_by="ticker", progress=False)
        for yt, orig_sym in sym_map.items():
            try:
                sub = df[yt] if (len(set(yf_tickers)) > 1 and yt in df.columns.levels[0]) else df
                if sub is not None and not sub.empty:
                    c = float(sub["Close"].iloc[-1])
                    prev = float(sub["Close"].iloc[-2]) if len(sub) > 1 else c
                    h = float(sub["High"].iloc[-1])
                    l = float(sub["Low"].iloc[-1])
                    ch = c - prev
                    chp = (ch / prev) * 100 if prev else 0.0
                    raw = orig_sym.replace("-INDEX", "").replace("-EQ", "").replace("NSE:", "")
                    out.append({
                        "n": orig_sym,
                        "s": "ok",
                        "v": {
                            "lp": round(c, 2),
                            "ch": round(ch, 2),
                            "chp": round(chp, 2),
                            "high_price": round(h, 2),
                            "low_price": round(l, 2),
                            "short_name": raw,
                        }
                    })
            except Exception:
                continue
    except Exception:
        pass
    return out


class FyersError(RuntimeError):
    pass


def get_auth_link(redirect_uri: str = FYERS_REDIRECT_URI) -> str:
    """Generate the Fyers OAuth login URL."""
    from fyers_apiv3 import fyersModel

    session = fyersModel.SessionModel(
        client_id=FYERS_APP_ID,
        secret_key=FYERS_SECRET_ID,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code",
    )
    return session.generate_authcode()


def exchange_token(auth_code: str, redirect_uri: str = FYERS_REDIRECT_URI) -> dict:
    """Exchange authorization code for an access token and persist to disk."""
    from fyers_apiv3 import fyersModel

    session = fyersModel.SessionModel(
        client_id=FYERS_APP_ID,
        secret_key=FYERS_SECRET_ID,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code",
    )
    session.set_token(auth_code)
    resp = session.generate_token()

    if not isinstance(resp, dict) or resp.get("s") != "ok" or "access_token" not in resp:
        msg = resp.get("message") if isinstance(resp, dict) else str(resp)
        raise FyersError(f"Failed to generate access token: {msg}")

    token = resp["access_token"]
    today = datetime.now(IST).strftime("%Y-%m-%d")

    profile = {}
    try:
        fyers = fyersModel.FyersModel(client_id=FYERS_APP_ID, token=token, is_async=False, log_path="")
        prof_res = fyers.get_profile()
        if isinstance(prof_res, dict) and prof_res.get("s") == "ok":
            profile = prof_res.get("data", {})
    except Exception:
        pass

    data = {
        "access_token": token,
        "date": today,
        "created_at": datetime.now(IST).isoformat(),
        "profile": profile,
    }
    FYERS_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    FYERS_TOKEN_PATH.write_text(json.dumps(data, indent=2))
    return data


def load_token() -> tuple[str | None, dict | None]:
    """Load cached token if valid for today."""
    if not FYERS_TOKEN_PATH.exists():
        return None, None
    try:
        data = json.loads(FYERS_TOKEN_PATH.read_text())
        token = data.get("access_token")
        today = datetime.now(IST).strftime("%Y-%m-%d")
        if token and data.get("date") == today:
            return token, data.get("profile") or {}
    except Exception:
        pass
    return None, None

_LIVE_STREAM_LTP: dict[str, tuple[float, float]] = {}


def format_fyers_symbol(s: str) -> str:
    clean = s.strip()
    if clean.startswith("NSE:") or clean.startswith("MCX:"):
        return clean
    if clean in ("NIFTY", "NIFTY50", "NIFTY 50", "^NSEI"):
        return "NSE:NIFTY50-INDEX"
    if clean in ("BANKNIFTY", "BANK NIFTY", "^NSEBANK"):
        return "NSE:NIFTYBANK-INDEX"
    if "CE" in clean or "PE" in clean or "OPT" in clean:
        return f"NSE:{clean}"
    return f"NSE:{clean}-EQ"


class FyersStreamManager:
    """Singleton WebSocket connection manager using FyersDataSocket.
    Streams tick-by-tick market data directly over a persistent TCP connection with
    ZERO REST API requests consumed. Eliminates REST quota consumption entirely."""
    _instance: FyersStreamManager | None = None
    _lock = threading.Lock()

    def __init__(self):
        self._ws = None
        self._connected = False
        self._subscribed: set[str] = set()
        self._thread: threading.Thread | None = None

    @classmethod
    def get_instance(cls) -> FyersStreamManager:
        with cls._lock:
            if cls._instance is None:
                cls._instance = FyersStreamManager()
            return cls._instance

    def start(self):
        token, _ = load_token()
        if not token:
            return
        if self._connected or (self._thread and self._thread.is_alive()):
            return

        def _run():
            try:
                from fyers_apiv3.FyersWebsocket import data_ws
                full_token = f"{FYERS_APP_ID}:{token}"

                def on_connect():
                    self._connected = True
                    if self._subscribed:
                        try:
                            self._ws.subscribe(symbols=list(self._subscribed), data_type="SymbolUpdate")
                        except Exception:
                            pass

                def on_message(message):
                    if isinstance(message, dict) and "symbol" in message and "ltp" in message:
                        sym = str(message["symbol"]).strip()
                        raw = sym.replace("NSE:", "").replace("-EQ", "").replace("-INDEX", "").strip()
                        try:
                            ltp = float(message["ltp"])
                            now = time.time()
                            _LIVE_STREAM_LTP[sym] = (now, ltp)
                            _LIVE_STREAM_LTP[raw] = (now, ltp)
                            _LIVE_STREAM_LTP[f"NSE:{raw}"] = (now, ltp)
                        except (ValueError, TypeError):
                            pass

                def on_error(msg):
                    pass

                def on_close(msg):
                    self._connected = False

                self._ws = data_ws.FyersDataSocket(
                    access_token=full_token,
                    write_to_file=False,
                    litemode=False,
                    reconnect=True,
                    on_connect=on_connect,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                self._ws.connect()
            except Exception:
                self._connected = False

        self._thread = threading.Thread(target=_run, daemon=True, name="FyersWebSocketStream")
        self._thread.start()

    def subscribe(self, symbols: list[str]):
        if not symbols:
            return
        formatted = [format_fyers_symbol(s) for s in symbols]
        new_syms = [s for s in formatted if s not in self._subscribed]
        if not new_syms:
            return
        self._subscribed.update(new_syms)
        if self._connected and self._ws:
            try:
                self._ws.subscribe(symbols=new_syms, data_type="SymbolUpdate")
            except Exception:
                pass
        else:
            self.start()

    def get_ltp(self, symbol: str, max_age_secs: float = 30.0) -> float | None:
        clean = symbol.replace("NSE:", "").replace("-EQ", "").replace("-INDEX", "").strip()
        formatted = format_fyers_symbol(symbol)
        now = time.time()
        for k in (formatted, symbol, clean, f"NSE:{clean}"):
            if k in _LIVE_STREAM_LTP:
                ts, ltp = _LIVE_STREAM_LTP[k]
                if (now - ts) <= max_age_secs:
                    return ltp
        return None


class FyersClient:
    """HTTP client & direct Python client for Fyers API v3."""

    def __init__(self, base: str = DEFAULT_BASE, timeout: int = TIMEOUT):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._fyers_instance = None

    def _get_direct_model(self):
        """The SDK client, or None when there is no token or no SDK.

        The import is guarded because fyers_apiv3 is an optional dependency: a
        missing package must degrade the feed to delayed data, not crash the
        scan inside FyersFeed.__init__.
        """
        token, _ = load_token()
        if not token:
            return None
        try:
            from fyers_apiv3 import fyersModel
        except ImportError:
            return None
        return fyersModel.FyersModel(
            client_id=FYERS_APP_ID,
            token=token,
            is_async=False,
            log_path="",
        )

    def _get(self, path: str, params: dict | None = None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read())
            except Exception:  # noqa: BLE001
                raise FyersError(f"{exc.code} from {path}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FyersError(f"cannot reach the Fyers server at {self.base} ({exc})") from exc

    def status(self) -> tuple[bool, str]:
        """(connected, human-readable reason)."""
        # 0. If a custom base was explicitly specified, check it
        if self.base != DEFAULT_BASE:
            try:
                data = self._get("/api/fyers/status")
                if data.get("connected"):
                    prof = (data.get("profile") or {}).get("name") or "connected"
                    return True, f"Fyers live ({prof})"
                if data.get("expired"):
                    return False, "Fyers token expired — please re-authenticate"
                return False, f"cannot reach the Fyers server at {self.base}"
            except FyersError as exc:
                return False, str(exc)
            except Exception as exc:
                return False, f"cannot reach the Fyers server at {self.base} ({exc})"

        # 1. Check direct token first. A token file on disk is not proof the
        #    session works, so the profile call has to come back clean before
        #    this reports live — claiming live on an unverified token hides a
        #    dead feed behind a green pill.
        token, prof = load_token()
        if token:
            model = self._get_direct_model()
            if model is None:
                return False, ("Fyers token present but fyers_apiv3 is not "
                               "installed — run: pip install fyers-apiv3")
            try:
                p = model.get_profile()
            except Exception as exc:                    # noqa: BLE001
                return False, (f"Fyers token could not be verified ({exc}) — "
                               "re-authenticate")
            if isinstance(p, dict) and (p.get("s") == "error"
                                        or p.get("code") in (-8, -99, -17)):
                if p.get("code") in (-8, -99, -17):
                    return False, "Fyers token expired — please re-authenticate"
                return False, (f"Fyers rejected the token: "
                               f"{p.get('message') or p.get('code')} — re-authenticate")
            name = (prof or {}).get("name") or "User"
            return True, f"Fyers live ({name})"

        # 2. Check local proxy server
        try:
            data = self._get("/api/fyers/status")
            if data.get("connected"):
                prof = (data.get("profile") or {}).get("name") or "connected"
                return True, f"Fyers live ({prof})"
            if data.get("expired"):
                return False, "Fyers token expired — please re-authenticate"
        except Exception:
            pass

        auth_url = get_auth_link()
        return False, f"Fyers not connected — click to connect: {auth_url}"

    def history(self, symbol: str, resolution: str = "5", days: int = 5) -> list[dict]:
        """Fetch historical candles."""
        # 1. Direct model query
        model = self._get_direct_model()
        if model:
            now = datetime.now(IST)
            range_from = (now - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
            range_to = now.strftime("%Y-%m-%d")
            req = {
                "symbol": symbol,
                "resolution": str(resolution),
                "date_format": "1",
                "range_from": range_from,
                "range_to": range_to,
                "cont_flag": "1",
            }
            try:
                res = model.history(data=req)
            except Exception as exc:                    # noqa: BLE001
                # Transport failure is per-symbol, not fatal: raise FyersError
                # so FyersFeed records it and falls back to yfinance for this
                # name instead of the exception escaping the whole scan.
                raise FyersError(f"Fyers history request failed ({exc})") from exc
            if isinstance(res, dict) and res.get("s") == "ok":
                candles = res.get("candles") or []
                # Fyers candles format: [timestamp, open, high, low, close, volume]
                out = []
                for c in candles:
                    if len(c) >= 6:
                        out.append({
                            "timestamp": c[0],
                            "open": c[1],
                            "high": c[2],
                            "low": c[3],
                            "close": c[4],
                            "volume": c[5],
                        })
                return out
            elif isinstance(res, dict) and res.get("s") == "error":
                raise FyersError(res.get("message") or "Fyers history error")

        # 2. Fallback to HTTP proxy only if not already talking to our own local server
        if not model and "localhost:3001" not in self.base and "127.0.0.1:3001" not in self.base:
            try:
                data = self._get("/api/fyers/history",
                                 {"symbol": symbol, "resolution": resolution, "days": days})
                if data.get("error"):
                    raise FyersError(str(data["error"]))
                return data.get("candles") or []
            except Exception:
                pass
        return []

    def quotes(self, symbols: list[str]) -> list[dict]:
        """Fetch quotes for a list of symbols (e.g. ['NSE:NIFTY50-INDEX', 'NSE:RELIANCE-EQ'])."""
        if not symbols:
            return []
        sym_str = ",".join(sorted(set(symbols)))
        now = time.time()

        # Check in-memory cache to prevent burning Fyers 5000/day quota on rapid polling
        cached = _QUOTES_CACHE.get(sym_str)
        if cached and (now - cached[0]) < _QUOTES_CACHE_TTL:
            return cached[1]

        # 1. Direct model query
        model = self._get_direct_model()
        if model:
            try:
                res = model.quotes({"symbols": sym_str})
                if isinstance(res, dict) and res.get("s") == "ok" and "d" in res and res["d"]:
                    _QUOTES_CACHE[sym_str] = (now, res["d"])
                    return res["d"]
            except Exception:
                pass

        # 2. Fallback to yfinance if Fyers limit reached or offline
        fallback_quotes = _quotes_from_yfinance(symbols)
        if fallback_quotes:
            _QUOTES_CACHE[sym_str] = (now, fallback_quotes)
            return fallback_quotes

        return []

    def option_chain(self, symbol: str, strikecount: int = 10) -> dict | None:
        """Fetch real-time option chain for an index or stock from Fyers."""
        model = self._get_direct_model()
        if model:
            try:
                res = model.optionchain(data={"symbol": symbol, "strikecount": strikecount})
                if isinstance(res, dict) and res.get("s") == "ok" and "data" in res:
                    return res["data"]
            except Exception:
                pass
        return None


def to_frame(candles: list[dict]) -> pd.DataFrame | None:
    """Fyers candle dicts -> the IST-indexed OHLCV frame the scanner expects."""
    rows = [c for c in candles if c.get("timestamp") and c.get("close") is not None]
    if not rows:
        return None
    idx = pd.DatetimeIndex(
        [pd.Timestamp(int(c["timestamp"]), unit="s", tz="UTC") for c in rows]
    ).tz_convert(IST)
    df = pd.DataFrame({
        "Open": [float(c["open"]) for c in rows],
        "High": [float(c["high"]) for c in rows],
        "Low": [float(c["low"]) for c in rows],
        "Close": [float(c["close"]) for c in rows],
        "Volume": [float(c.get("volume") or 0) for c in rows],
    }, index=idx).sort_index()
    if df.index.has_duplicates:
        df = df[~df.index.duplicated(keep="last")]
    return df


class FyersFeed(LiveFeed):
    """Fyers real-time candles feed with automatic yfinance fallback."""

    source_candles = "fyers"

    def __init__(self, period: str = "5d", base: str = DEFAULT_BASE,
                 fallback: bool = True):
        super().__init__(period=period)
        self.client = FyersClient(base)
        self.fallback = fallback
        self.connected, self.status_note = self.client.status()
        if not self.connected:
            self.errors.append(self.status_note + (
                " — falling back to the delayed yfinance feed" if fallback
                else " — no candles this run"))

    def _days(self) -> int:
        try:
            return max(2, int(str(self.period).rstrip("d")))
        except ValueError:
            return 5

    def _fetch(self, symbol: str, instrument: str) -> Candles | None:
        fetched = datetime.now(IST)
        try:
            raw = self.client.history(symbol, C.BAR_INTERVAL.rstrip("m"), self._days())
        except FyersError as exc:
            self.errors.append(f"{instrument}: Fyers history failed ({exc})")
            return None
        df = to_frame(raw)
        if df is None:
            return None
        df = self._drop_forming(df)
        if df is None:
            return None
        end = (df.index[-1] + pd.Timedelta(minutes=C.BAR_MINUTES)).to_pydatetime()
        return Candles(df, Provenance(self.source_candles, instrument,
                                      C.BAR_INTERVAL, end, fetched, delayed=False))

    @staticmethod
    def _drop_forming(df: pd.DataFrame) -> pd.DataFrame | None:
        """Fyers stamps a bar by its start too, so the last one may be forming."""
        end = df.index[-1] + pd.Timedelta(minutes=C.BAR_MINUTES)
        if end > pd.Timestamp(datetime.now(IST)):
            df = df.iloc[:-1]
        return df if not df.empty else None

    def candles(self, symbols: list[str]) -> dict[str, Candles]:
        if not self.connected:
            return super().candles(symbols) if self.fallback else {}
        out: dict[str, Candles] = {}
        missing: list[str] = []
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(12, len(symbols) or 1)) as ex:
            futs = {ex.submit(self._fetch, f"NSE:{sym}-EQ", sym): sym for sym in symbols}
            for fut in concurrent.futures.as_completed(futs):
                sym = futs[fut]
                try:
                    got = fut.result()
                    if got is not None:
                        out[sym] = got
                    else:
                        missing.append(sym)
                except Exception:
                    missing.append(sym)
        if missing and self.fallback:
            self.errors.append(f"Fyers returned no candles for {', '.join(missing)} "
                               "— using the delayed feed for those")
            out.update(super().candles(missing))
        return out

    def index_candles(self, ticker: str) -> Candles | None:
        if not self.connected:
            return super().index_candles(ticker) if self.fallback else None
        mapped = INDEX_SYMBOLS.get(ticker)
        if mapped is None:
            return super().index_candles(ticker)
        got = self._fetch(mapped, ticker)
        return got if got is not None else (
            super().index_candles(ticker) if self.fallback else None)

    def _fyers_to_option_quotes(self, data: dict, underlying_name: str) -> list:
        from trading.fno.options import OptionQuote
        chain = data.get("optionsChain") or []
        expiry_data = data.get("expiryData") or []
        front_expiry = expiry_data[0].get("date") if expiry_data else ""
        spot = None
        for it in chain:
            if it.get("strike_price") == -1:
                spot = float(it.get("ltp") or 0)
                break

        out = []
        now = datetime.now(IST)
        for it in chain:
            strike = it.get("strike_price")
            opt_type = it.get("option_type")
            if strike is None or strike == -1 or not opt_type:
                continue
            sym = it.get("symbol", "")
            kind = "Call" if opt_type == "CE" else "Put"
            ltp = float(it.get("ltp") or 0)
            oi = float(it.get("oi") or 0)
            vol = float(it.get("volume") or 0)
            pchange = float(it.get("ltpchp") or 0)
            out.append(OptionQuote(
                underlying=underlying_name,
                identifier=sym,
                option_type=kind,
                strike=float(strike),
                expiry=front_expiry or "near",
                last_price=ltp,
                pct_change=pchange,
                open_interest=oi,
                volume=vol,
                underlying_value=spot,
                prov=Provenance(self.source_candles, sym, "option_chain", now, now)
            ))
        return out

    def index_option_board(self, ticker: str = "^NSEI") -> list:
        if self.connected:
            mapped = INDEX_SYMBOLS.get(ticker, "NSE:NIFTY50-INDEX")
            name = "BANKNIFTY" if "BANK" in mapped else "NIFTY"
            data = self.client.option_chain(mapped, strikecount=12)
            if data:
                return self._fyers_to_option_quotes(data, name)
        return super().index_option_board()

    def banknifty_option_board(self) -> list:
        if self.connected:
            data = self.client.option_chain("NSE:NIFTYBANK-INDEX", strikecount=12)
            if data:
                return self._fyers_to_option_quotes(data, "BANKNIFTY")
        return super().banknifty_option_board()

    def stock_option_board(self, symbol: str) -> list:
        if self.connected:
            fyers_sym = f"NSE:{symbol}-EQ" if not symbol.startswith("NSE:") else symbol
            clean_sym = symbol.replace("NSE:", "").replace("-EQ", "")
            data = self.client.option_chain(fyers_sym, strikecount=8)
            if data:
                return self._fyers_to_option_quotes(data, clean_sym)
        return super().stock_option_board(symbol)


# --- Built-in OAuth Server on port 3001 ---

class FyersAuthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)

        if url.path == "/api/fyers/callback":
            auth_code = query.get("auth_code", [None])[0]
            if not auth_code:
                return self._send_html(400, "<h3>Error: No auth_code received from Fyers.</h3>")
            try:
                data = exchange_token(auth_code)
                name = (data.get("profile") or {}).get("name") or "Trader"
                html = f"""<!doctype html>
<html>
<head><title>Fyers Connected</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;background:#0d1117;color:#e6edf3;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px;text-align:center;max-width:440px;box-shadow:0 8px 24px rgba(0,0,0,0.4);}}
h2{{color:#3fb950;margin-top:0;}}
a{{display:inline-block;margin-top:20px;background:#238636;color:#fff;text-decoration:none;padding:10px 20px;border-radius:6px;font-weight:600;}}
a:hover{{background:#2ea043;}}
</style>
</head>
<body>
<div class="card">
  <h2>&#10003; Fyers Connected!</h2>
  <p>Welcome, <b>{name}</b>. Your Fyers API v3 session is now active with zero-delay live market data.</p>
  <p style="color:#8b949e;font-size:13px">You can now return to the Trading Dashboard.</p>
  <a href="http://localhost:8787/">Open Dashboard</a>
</div>
</body>
</html>"""
                return self._send_html(200, html)
            except Exception as exc:
                return self._send_html(500, f"<h3>Failed to exchange token: {exc}</h3>")

        elif url.path == "/api/fyers/login":
            auth_link = get_auth_link()
            self.send_response(302)
            self.send_header("Location", auth_link)
            self.end_headers()

        elif url.path == "/api/fyers/status":
            token, prof = load_token()
            res = {
                "connected": token is not None,
                "profile": prof or {},
                "auth_url": get_auth_link(),
                "app_id": FYERS_APP_ID,
            }
            return self._send_json(200, res)

        elif url.path == "/api/fyers/history":
            sym = query.get("symbol", ["NSE:RELIANCE-EQ"])[0]
            res = query.get("resolution", ["5"])[0]
            days = int(query.get("days", ["5"])[0])
            try:
                client = FyersClient()
                model = client._get_direct_model()
                if not model:
                    return self._send_json(401, {"error": "Fyers token not configured"})
                now = datetime.now(IST)
                range_from = (now - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
                range_to = now.strftime("%Y-%m-%d")
                req = {
                    "symbol": sym,
                    "resolution": str(res),
                    "date_format": "1",
                    "range_from": range_from,
                    "range_to": range_to,
                    "cont_flag": "1",
                }
                hres = model.history(data=req)
                candles = []
                if isinstance(hres, dict) and hres.get("s") == "ok":
                    for c in (hres.get("candles") or []):
                        if len(c) >= 6:
                            candles.append({
                                "timestamp": c[0], "open": c[1], "high": c[2],
                                "low": c[3], "close": c[4], "volume": c[5]
                            })
                return self._send_json(200, {"candles": candles})
            except Exception as exc:
                return self._send_json(500, {"error": str(exc)})

        else:
            auth_link = get_auth_link()
            token, prof = load_token()
            status_text = f"Connected as {(prof or {}).get('name', 'User')}" if token else "Not connected"
            html = f"""<!doctype html>
<html>
<head><title>Fyers OAuth Service</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;background:#0d1117;color:#e6edf3;padding:40px;line-height:1.6;}}
.btn{{background:#238636;color:#fff;text-decoration:none;padding:10px 18px;border-radius:6px;font-weight:600;display:inline-block;}}
</style>
</head>
<body>
  <h2>Fyers API v3 Integration Service</h2>
  <p><b>Status:</b> {status_text}</p>
  <p><b>App ID:</b> {FYERS_APP_ID}</p>
  <p><b>Redirect URI:</b> {FYERS_REDIRECT_URI}</p>
  <p><a class="btn" href="{auth_link}">Connect Fyers Account</a></p>
</body>
</html>"""
            return self._send_html(200, html)

    def _send_html(self, code: int, body: str):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _send_json(self, code: int, data: dict):
        b = json.dumps(data, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, fmt, *args):
        pass


def start_background_server(port: int = 3001) -> threading.Thread | None:
    """Start the Fyers OAuth helper server in a background daemon thread if not already running."""
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), FyersAuthHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True, name="fyers-oauth-server")
        t.start()
        return t
    except OSError:
        # Port already in use / another instance running
        return None


def run_server(port: int = 3001):
    """Run the standalone Fyers OAuth helper server."""
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), FyersAuthHandler)
        print(f"Fyers OAuth Server listening on http://localhost:{port}")
        print(f"Login URL: {get_auth_link()}")
        server.serve_forever()
    except OSError as exc:
        print(f"Port {port} is in use: {exc}")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3001
    run_server(port)

