"""Pre-market analyst agent (Role 1) — run by cron at 8:45 AM IST, before open.

Feeds overnight market context to the agent and asks for a structured verdict:
market regime, symbols to avoid today, and a risk multiplier. Writes the result
to data/today_config.json, which the ExecutionRouter reads at startup.

The agent tunes the day's parameters. It never touches orders, and the router
clamps everything it produces.

Usage:  python -m trading.agents.premarket
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from trading.config import TODAY_CONFIG_PATH, FALLBACK_DAY_CONFIG, WATCHLIST, SCAN_UNIVERSE
from trading.ledger import Ledger
from trading.agents.llm import call_structured

SYSTEM = (
    "You are a risk analyst for a personal intraday NSE equity scalping system. "
    "Your job is to set conservative day-level risk parameters, not to predict "
    "prices. When information is missing or ambiguous, reduce risk. "
    "risk_multiplier scales position size: 1.0 = normal, 0.5 = half size, "
    "0.0 = no trading today. Block a symbol only for a concrete reason "
    "(earnings today, corporate action, circuit risk, extreme news)."
)


class PremarketVerdict(BaseModel):
    regime: Literal["trending", "choppy", "event_risk"]
    risk_multiplier: float = Field(description="0.0 to 1.0 position-size scaler")
    blocked_symbols: list[str] = Field(description="Symbols to avoid today, from the watchlist only")
    rationale: str = Field(description="One or two lines explaining the verdict")


def gather_overnight_context() -> dict[str, str]:
    """Collect overnight inputs. Each source degrades to 'unavailable' rather
    than failing — the agent is told what it doesn't know.

    Extend these with real feeds as you build: GIFT Nifty quote, SGX/US close,
    news API headlines, NSE events calendar. yfinance is used if installed.
    """
    ctx = {
        "gift_nifty": "unavailable",
        "global_markets": "unavailable",
        "news_headlines": "unavailable",
        "events_calendar": "unavailable",
    }
    try:
        import yfinance as yf  # optional dependency

        snapshots = []
        for name, ticker in [("S&P 500", "^GSPC"), ("Nasdaq", "^IXIC"),
                             ("Nikkei", "^N225"), ("India VIX", "^INDIAVIX")]:
            try:
                # 5d + dropna: the latest row can be NaN while a session is
                # unconsolidated — NaNs here once read as "all data lost" and
                # made the agent halt a normal day.
                h = yf.Ticker(ticker).history(period="5d")["Close"].dropna()
                if len(h) >= 2:
                    chg = (h.iloc[-1] / h.iloc[-2] - 1) * 100
                    snapshots.append(f"{name}: {h.iloc[-1]:.0f} ({chg:+.2f}%)")
            except Exception:  # noqa: BLE001
                continue
        if snapshots:
            ctx["global_markets"] = "; ".join(snapshots)
    except ImportError:
        pass
    return ctx


def run() -> dict:
    ledger = Ledger()
    ctx = gather_overnight_context()

    prompt = f"""Date: {datetime.now().strftime('%A %Y-%m-%d')}
Core watchlist: {', '.join(WATCHLIST)}
Full scan universe: Nifty 50 constituents ({len(SCAN_UNIVERSE)} symbols) — you may block any of them.

Overnight data:
- GIFT Nifty: {ctx['gift_nifty']}
- Global markets: {ctx['global_markets']}
- News headlines: {ctx['news_headlines']}
- Today's events (earnings/expiry/macro): {ctx['events_calendar']}

Set today's risk parameters for the scalping system."""

    fallback = PremarketVerdict(**FALLBACK_DAY_CONFIG)
    verdict = call_structured("premarket", SYSTEM, prompt,
                              PremarketVerdict, fallback, ledger=ledger)

    # Clamp everything before it can influence the router — belt and braces.
    config = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "regime": verdict.regime,
        "risk_multiplier": max(0.0, min(1.0, verdict.risk_multiplier)),
        "blocked_symbols": [s for s in verdict.blocked_symbols if s in SCAN_UNIVERSE],
        "rationale": verdict.rationale[:500],
    }

    TODAY_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TODAY_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)

    print(f"today_config.json written: {json.dumps(config)}")
    from trading.notify import notify
    notify(f"🌅 Pre-market: {config['regime']} · {config['risk_multiplier']}x risk",
           config["rationale"][:180])
    return config


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
