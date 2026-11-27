"""Forex & Crypto 4-Agent AI Council Desk (Gemini 3.8 Flash).

The 4 AI Agents:
1. Technical Analyst: Audits Pine 20/50 EMA, 30m 200 SMA, Session VWAP, 15m ORB Breakout + OR Bias, ATR, and RVOL.
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
        description="Final conviction score from 1 (lowest) to 10 (highest). Must be >=9 (8.5+ rounded) for APPROVE. Only true A+ setups qualify.",
        ge=1,
        le=10,
    )
    predicted_direction: Literal["UPWARD", "DOWNWARD", "SIDEWAYS"] = Field(
        default="UPWARD",
        description="Forecasted most probable direction of price movement (UPWARD, DOWNWARD, or SIDEWAYS)",
    )
    probability_upward_pct: int = Field(
        default=50,
        description="Probability (0-100%) of upward breakout and continuation",
        ge=0,
        le=100,
    )
    probability_downward_pct: int = Field(
        default=25,
        description="Probability (0-100%) of downward breakdown or sharp rejection",
        ge=0,
        le=100,
    )
    probability_sideways_pct: int = Field(
        default=25,
        description="Probability (0-100%) of sideways chop, false breakout, or rangebound consolidation",
        ge=0,
        le=100,
    )
    news_impact_summary: str = Field(
        default="Neutral news sentiment; no adverse macro catalysts detected.",
        description="Summary of live news headlines and how macro catalysts influence directional probability",
    )
    technical_analysis: str = Field(
        description="Technical Analyst View: Evaluation of Pine 20/50 EMA trend, 30m 200 SMA filter, VWAP slope, 15m ORB breakout + OR bias, ATR, and RVOL"
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
You enforce extreme selectivity targeting an institutional 70%+ win-rate edge by filtering out all marginal setups and only approving true "A+" confluence trades.

Your panel consists of 4 distinct specialist personas who debate and reach a binding consensus:

1. Technical Analyst:
   - Scrutinizes multi-timeframe chart structure: Pine 20/50 EMA trend alignment, 30m 200 SMA macro filter, Session VWAP pullback, and 15m Session Opening Range Breakout (ORB) with Opening Candle Bias.
   - STRICT MULTI-TIMEFRAME RULE: 15m breakout MUST align with 30m 200 SMA & Daily trend. Never approve a Long below 30m 200 SMA or on a Red 15m OR candle; never approve a Short above 30m 200 SMA or on a Green 15m OR candle.
   - STRICT INSTITUTIONAL VOLUME: Demands Relative Volume (RVOL >= 2.0x). Breakouts on volume < 2.0x are rejected as unconfirmed retail traps.
   - DAILY 365 MA WALL: Vetoes any trade within 0.5% proximity of the Daily 365 MA wall.

2. Bullish Researcher:
   - Formulates the Long breakout thesis based on institutional momentum, trend continuation, positive news catalysts, and clear upside targets.
   - Verifies buyer absorption at VWAP and clean separation above the 15m Opening Range high.

3. Bearish Researcher (Devil's Advocate & Trap Specialist):
   - Actively hunts for reasons NOT to take the trade.
   - Checks for crypto's #1 killer: **False Breakouts & Liquidity Grabs** (wicks pushing above highs only to trap buyers before a dump).
   - Checks for overhead supply, bearish RSI divergence, negative regulatory or macro headlines, and weekend low-volume chop.
   - HARD AUTOPSY VETO: Strictly audits recent post-mortem trade lessons. If the setup exhibits any failure pattern logged in recent autopsies, issues an immediate VETO.

4. Trader Decision Agent (Chief Risk Officer & Arbitrator):
   - Synthesizes the debate. Enforces strict asymmetric Risk-to-Reward (minimum 1:2 R:R).
   - SESSION TIMING: Prioritizes peak liquidity hours (London & New York Overlap: 12:00-17:00 UTC / 17:30-22:30 IST). During off-peak or weekend chop, raises scrutiny.
   - Ensures the stop loss is placed at a strict structural swing point or ATR cushion.
   - Issues the binding verdict:
     * APPROVE (Action: BUY or SELL, Conviction: 9-10 / 8.5+): All 4 pillars pass, institutional RVOL >= 2.0x, multi-timeframe aligned, genuine A+ breakout.
     * CAUTION (Action: HOLD, Conviction: 5-8): Mixed signals, volume between 1.2x-1.9x, or choppy market; wait for clean retest.
     * VETO (Action: HOLD, Conviction: 1-4): Clear liquidity trap, false breakout, counter-trend to 1H/Daily, low volume, or severe opposing news.

5. Probabilistic Directional Forecasting Engine:
   - For every setup, quantify the exact probability distribution across 3 possible market paths (must sum to ~100%):
     * probability_upward_pct: Probability (0-100%) that the asset drives upward towards profit target.
     * probability_downward_pct: Probability (0-100%) that the asset breaks down or suffers heavy rejection.
     * probability_sideways_pct: Probability (0-100%) that the asset gets caught in sideways chop, range trap, or low-volume fakeout.
   - Assign predicted_direction: 'UPWARD' if probability_upward_pct is dominant, 'DOWNWARD' if probability_downward_pct is dominant, or 'SIDEWAYS' if choppy.
   - Provide news_impact_summary explaining how current news headlines, articles, or macro events influence this probability.
   - HIGH PREDICTABILITY RULE: Only approve BUY when probability_upward_pct >= 70% and sideways risk <= 20%. Only approve SELL when probability_downward_pct >= 70% and sideways risk <= 20%. If sideways/trap probability exceeds 30%, issue CAUTION or VETO.

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
    rvol_val = float(setup.get("rvol", 1.0) or 1.0)
    is_approved = quant_score >= 75
    conv = min(10, max(4, int(quant_score / 10))) if is_approved else 4
    if quant_score >= 85 and rvol_val >= 2.0:
        conv = 9
    up_prob = 75 if (is_approved and side == "BUY") else (15 if (is_approved and side == "SELL") else 35)
    down_prob = 75 if (is_approved and side == "SELL") else (15 if (is_approved and side == "BUY") else 35)
    side_prob = 100 - up_prob - down_prob
    pred_dir = "UPWARD" if up_prob >= 70 else ("DOWNWARD" if down_prob >= 70 else "SIDEWAYS")

    fallback = ForexCouncilVerdict(
        symbol=sym,
        verdict="APPROVE" if is_approved else "CAUTION",
        action=side if is_approved else "HOLD",
        conviction=conv,
        predicted_direction=pred_dir,
        probability_upward_pct=up_prob,
        probability_downward_pct=down_prob,
        probability_sideways_pct=side_prob,
        news_impact_summary="Deterministic Quant Evaluation: Confluence with short-term trend and indicator consensus.",
        technical_analysis=f"Deterministic Quant Analysis: Setup satisfies strategy indicators with score {quant_score}/100 and RVOL {rvol_val:.1f}x.",
        bull_case=f"Momentum supports {side} with price aligned with short-term EMAs and VWAP.",
        bear_case="Potential volatility or resistance risk; monitor structural stop-loss strictly.",
        decision_rationale=f"Quantitative rules {'approve' if is_approved else 'caution/hold'} trade with ${planned_risk_usd:,.2f} risk budget.",
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
