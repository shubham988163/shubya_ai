"""Forex & Crypto 4-Agent AI Council Desk (Gemini 3.8 Flash).

The 4 AI Agents:
1. Technical Analyst: Audits 9/21 EMA, 200 SMA, Session VWAP, ORB Breakout, ATR, and RVOL.
2. Bullish Researcher: Formulates the upside momentum thesis, positive catalysts, and continuation potential.
3. Bearish Researcher: Devil's advocate stress-testing for false breakouts, liquidity traps, overhead resistance, and macro risks.
4. Trader Decision Agent: Chief Risk Officer synthesizing the debate into an APPROVE / CAUTION / VETO verdict and conviction score (1-10).
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from trading.agents.llm import call_structured
from trading.config import GEMINI_MODEL
from trading.delta_exchange import DeltaClient
from trading.ledger import Ledger

logger = logging.getLogger("trading.forex_council")

from trading.forex_reflection import get_recent_lessons

# --- Live Crypto & Forex Real-Time News Fetcher ---

SYMBOL_NEWS_QUERIES: dict[str, str] = {
    "BTCUSD": "Bitcoin OR BTC crypto market",
    "ETHUSD": "Ethereum OR ETH crypto market",
    "SOLUSD": "Solana OR SOL crypto ecosystem",
    "XAUTUSD": "Gold price OR Federal Reserve dollar interest rates OR inflation",
    "BNBUSD": "Binance Coin OR BNB crypto ecosystem",
    "XRPUSD": "Ripple OR XRP crypto news SEC",
    "DOGEUSD": "Dogecoin OR DOGE crypto market",
    "ADAUSD": "Cardano OR ADA crypto blockchain",
}


def fetch_crypto_forex_news(symbol: str, limit: int = 4) -> list[dict[str, str]]:
    """Fetch live market headlines for a Forex/Crypto asset from reputable financial sources via Google News RSS."""
    query = SYMBOL_NEWS_QUERIES.get(symbol, f"{symbol} crypto forex market")
    encoded = urllib.parse.quote_plus(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"

    news_items: list[dict[str, str]] = []
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
        )
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            content = resp.read()
            root = ET.fromstring(content)
            for item in root.findall(".//item")[:limit]:
                title = item.find("title").text if item.find("title") is not None else ""
                source_elem = item.find("source")
                source = source_elem.text if source_elem is not None else "Financial News"
                pub_date = item.find("pubDate").text if item.find("pubDate") is not None else ""
                link = item.find("link").text if item.find("link") is not None else ""
                if title:
                    news_items.append({
                        "title": title,
                        "source": source,
                        "pub_date": pub_date,
                        "link": link,
                    })
    except Exception as exc:
        logger.debug(f"Failed to fetch live news for {symbol}: {exc}")

    return news_items


# --- 4-Agent Council Pydantic Output Model ---


class ForexCouncilVerdict(BaseModel):
    symbol: str
    verdict: Literal["APPROVE", "CAUTION", "VETO"] = Field(
        description="APPROVE (execute trade), CAUTION (wait/hold), or VETO (reject setup due to false breakout or excessive risk)"
    )
    action: Literal["BUY", "SELL", "HOLD"] = Field(
        description="Final action command: BUY, SELL, or HOLD"
    )
    conviction: int = Field(
        description="Final conviction score from 1 (lowest) to 10 (highest). Must be >=8 for APPROVE",
        ge=1,
        le=10,
    )
    technical_analysis: str = Field(
        description="Technical Analyst View: Evaluation of 9/21 EMA crossover, 200 SMA regime, VWAP slope, ORB breakout, ATR, and RVOL"
    )
    bull_case: str = Field(
        description="Bullish Researcher Thesis: Upside continuation catalysts, accumulation signs, and momentum confirmation"
    )
    bear_case: str = Field(
        description="Bearish Researcher Audit: Trap detection, liquidity grab risks, false breakouts, overhead resistance, and macro headwinds"
    )
    decision_rationale: str = Field(
        description="Trader Decision Agent Synthesis: Risk-reward ratio evaluation, structural stop check, and final execution mandate"
    )
    recommended_entry: float = Field(description="Entry price level")
    recommended_stop_loss: float = Field(description="Structural stop-loss level")
    recommended_target: float = Field(description="1:2 R:R profit target level")


FOREX_COUNCIL_SYSTEM = """You are the 4 AI Agents Trading Council evaluating 24/7 Forex and Cryptocurrency breakout setups on Delta Exchange.
Your panel consists of 4 distinct specialist personas who debate and reach a binding consensus:

1. Technical Analyst:
   - Scrutinizes chart structure: 9/21 EMA trend alignment, 200 SMA macro filter, Session VWAP pullback, and 1-hour Opening Range Breakout (ORB).
   - Validates that volume expansion (RVOL >= 1.2x) and RSI momentum confirm the price move.

2. Bullish Researcher:
   - Formulates the Long breakout thesis based on institutional momentum, trend continuation, positive news catalysts, and upside targets.

3. Bearish Researcher (Devil's Advocate & Trap Specialist):
   - Actively looks for reasons NOT to take the trade.
   - Checks for crypto's #1 killer: **False Breakouts & Liquidity Grabs** (wicks pushing above highs only to trap buyers before a dump).
   - Checks for overhead supply, bearish RSI divergence, negative regulatory or macro headlines, and weekend low-volume chop.

4. Trader Decision Agent (Chief Risk Officer & Arbitrator):
   - Synthesizes the debate. Enforces strict asymmetric Risk-to-Reward (minimum 1:2 R:R).
   - Ensures the stop loss is placed at a strict structural level (swing point or ATR cushion).
   - Issues the binding verdict:
     * APPROVE (Action: BUY or SELL, Conviction: 8-10): All 4 pillars pass, breakout is genuine, high probability.
     * CAUTION (Action: HOLD, Conviction: 5-7): Mixed signals or choppy market; wait for retest.
     * VETO (Action: HOLD, Conviction: 1-4): Clear liquidity trap, false breakout, counter-trend, or severe opposing news.

Ground your analysis strictly on the live market facts, quantitative indicators, and real-time headlines provided."""


def debate_forex_setup(
    setup: dict[str, Any],
    news: list[dict[str, str]] | None = None,
    client: DeltaClient | None = None,
    ledger: Ledger | None = None,
    planned_risk_usd: float = 2.50,
) -> ForexCouncilVerdict:
    """Run the 4 AI Agents debate on a Forex/Crypto setup and return the validated council verdict."""
    sym = setup.get("symbol", "")
    price = float(setup.get("price", 0.0))
    sl = float(setup.get("stop_loss", 0.0))
    target = float(setup.get("target1", 0.0))
    side = setup.get("side", "BUY")
    quant_score = setup.get("score", 50)
    reasons = setup.get("reasons", [])

    # Fetch live real-time news if not provided
    if news is None:
        news = fetch_crypto_forex_news(sym, limit=4)

    news_text = "\n".join([f"- [{n['source']}] {n['title']} ({n.get('pub_date', '')})" for n in news]) if news else "No breaking negative news headlines found."

    # Get recent continuous-learning lessons from closed trades
    past_lessons = get_recent_lessons(limit=5)
    lessons_text = "\n".join([f"  • {l}" for l in past_lessons])

    # Get Delta 24h ticker context if available
    delta_info = ""
    if client:
        try:
            tickers = client.get_tickers([sym])
            t = tickers.get(sym, {})
            if t:
                delta_info = (
                    f"Delta 24h Stats: Mark Price=${t.get('mark_price')}, 24h Change={t.get('change_24h_pct', 0):.2f}%, "
                    f"24h High=${t.get('high_24h')}, 24h Low=${t.get('low_24h')}, 24h Volume=${t.get('volume_24h'):,.0f}"
                )
        except Exception:
            pass

    prompt = f"""EVALUATE THIS FOREX / CRYPTO TRADE SETUP:

Asset: {sym}
Action Direction: {side}
Current Mark Price: ${price:,.4f}
Proposed Structural Stop-Loss: ${sl:,.4f}
Proposed 1:2 Profit Target: ${target:,.4f}
Risk per unit: ${abs(price - sl):,.4f} (Planned Risk: ${planned_risk_usd:,.2f} dynamic compounding budget)
Quantitative Strategy Score: {quant_score}/100
Technical Setup Factors:
{chr(10).join(f'  • {r}' for r in reasons)}

{delta_info}

CONTINUOUS LEARNING — LESSONS FROM RECENT TRADES (AVOID REPEATING LOSSES):
{lessons_text}

REAL-TIME NEWS & MACRO SENTIMENT HEADLINES:
{news_text}

Conduct the 4 AI Agents Debate (Technical Analyst, Bullish Researcher, Bearish Researcher, Trader Decision Agent) and output your structured consensus verdict."""

    # Deterministic fallback if Gemini is offline
    is_approved = quant_score >= 75
    fallback = ForexCouncilVerdict(
        symbol=sym,
        verdict="APPROVE" if is_approved else "CAUTION",
        action=side if is_approved else "HOLD",
        conviction=min(10, max(5, int(quant_score / 10))),
        technical_analysis=f"Deterministic Quant Analysis: Setup satisfies strategy indicators with score {quant_score}/100.",
        bull_case=f"Momentum supports {side} with price aligned with short-term EMAs and VWAP.",
        bear_case="Potential volatility risk; monitor stop-loss strictly.",
        decision_rationale=f"Quantitative rules {'approve' if is_approved else 'caution'} trade with ${planned_risk_usd:,.2f} risk budget.",
        recommended_entry=price,
        recommended_stop_loss=sl,
        recommended_target=target,
    )

    try:
        verdict = call_structured(
            agent_name="forex_council",
            system=FOREX_COUNCIL_SYSTEM,
            prompt=prompt,
            output_model=ForexCouncilVerdict,
            fallback=fallback,
            ledger=ledger,
        )
        return verdict
    except Exception as e:
        logger.warning(f"Forex Council LLM call failed for {sym}, using fallback: {e}")
        return fallback
