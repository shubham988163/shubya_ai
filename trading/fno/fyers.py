"""Real-time candles from Fyers, via the TradeBrahma server that already owns
the OAuth session.

Why through that server rather than calling Fyers directly: it holds the app
secret and the daily access token, and it already implements the login flow. So
this module never touches a credential — it calls `http://localhost:3001` and
gets candles back. The cost is that the Node server has to be running; when it
is not, or the token has expired, the scanner says so and falls back to the
delayed yfinance feed rather than pretending.

Access tokens last about a day, so **reconnecting is a daily step**: open
TradeBrahma → Broker Settings → Fyers API v3 → Connect. `status()` distinguishes
"server down" from "token expired" because the fix differs.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import pandas as pd

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


class FyersError(RuntimeError):
    pass


class FyersClient:
    """Thin HTTP client for the TradeBrahma Fyers proxy."""

    def __init__(self, base: str = DEFAULT_BASE, timeout: int = TIMEOUT):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            # The proxy answers 401 with a JSON body worth surfacing.
            try:
                return json.loads(exc.read())
            except Exception:                       # noqa: BLE001
                raise FyersError(f"{exc.code} from {path}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FyersError(f"cannot reach the Fyers server at {self.base} "
                             f"({exc}) — is `npm run server` running?") from exc

    def status(self) -> tuple[bool, str]:
        """(connected, human-readable reason)."""
        try:
            data = self._get("/api/fyers/status")
        except FyersError as exc:
            return False, str(exc)
        if data.get("connected"):
            prof = (data.get("profile") or {}).get("name") or "connected"
            return True, f"Fyers live ({prof})"
        if data.get("expired"):
            return False, ("Fyers token expired — reconnect in TradeBrahma → "
                           "Broker Settings → Fyers API v3")
        return False, ("Fyers is not connected — open TradeBrahma → Broker "
                       "Settings → Fyers API v3 → Connect (tokens last ~24h)")

    def history(self, symbol: str, resolution: str = "5", days: int = 5) -> list[dict]:
        data = self._get("/api/fyers/history",
                         {"symbol": symbol, "resolution": resolution, "days": days})
        if data.get("error"):
            raise FyersError(str(data["error"]))
        return data.get("candles") or []


def to_frame(candles: list[dict]) -> pd.DataFrame | None:
    """Fyers candle dicts → the IST-indexed OHLCV frame the scanner expects."""
    rows = [c for c in candles if c.get("timestamp") and c.get("close") is not None]
    if not rows:
        return None
    idx = pd.DatetimeIndex(
        [pd.Timestamp(int(c["timestamp"]), unit="s", tz="UTC") for c in rows]
    ).tz_convert(IST)
    return pd.DataFrame({
        "Open": [float(c["open"]) for c in rows],
        "High": [float(c["high"]) for c in rows],
        "Low": [float(c["low"]) for c in rows],
        "Close": [float(c["close"]) for c in rows],
        "Volume": [float(c.get("volume") or 0) for c in rows],
    }, index=idx).sort_index()


class FyersFeed(LiveFeed):
    """Fyers candles where available; everything else unchanged.

    Open interest, the F&O movers board, the index snapshot and the option
    board still come from NSE — Fyers is used for the one thing NSE will not
    give us in real time, which is intraday OHLCV.
    """

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
        for sym in symbols:
            got = self._fetch(f"NSE:{sym}-EQ", sym)
            if got is None:
                missing.append(sym)
            else:
                out[sym] = got
        # A name Fyers cannot serve should not silently vanish from the scan.
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
