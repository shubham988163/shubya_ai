"""Central configuration for the trading system.

All paths are anchored to the project root so cron jobs work regardless of CWD.
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- Trading universe ---
# Core watchlist (used by the pre-market agent's prompt focus).
WATCHLIST = ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN"]

# Full scan universe for the strategy engine — Nifty 50 constituents.
# Index composition changes ~semi-annually; edit as needed.
NIFTY50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "BPCL", "BRITANNIA", "CIPLA", "COALINDIA", "DRREDDY",
    "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK",
    "INFY", "ITC", "JSWSTEEL", "KOTAKBANK", "LT",
    "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN",
    # TATAMOTORS removed — ticker changed after the 2025 CV/PV demerger; add
    # the successor ticker(s) here if you want it back.
    "SUNPHARMA", "TATACONSUM", "TATASTEEL", "TCS",
    "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
]

# What the strategy engine scans. The full-Nifty-50 sweep (Jul 9–10) lost
# money in choppy conditions; the 6-stock core watchlist (Jul 7 setup) is the
# only configuration that has been net-profitable so far.
SCAN_UNIVERSE = WATCHLIST
YF_SUFFIX = ".NS"

DB_PATH = PROJECT_ROOT / "data" / "ledger.db"
TODAY_CONFIG_PATH = PROJECT_ROOT / "data" / "today_config.json"
REPORTS_DIR = PROJECT_ROOT / "reports"
LOGS_DIR = PROJECT_ROOT / "logs"

# --- Strategy: EMA crossover ---
FAST_EMA = 9
SLOW_EMA = 21
# 15m: the only net-positive config in the Jun-30→Jul-13 backtest
# (+145 net / +1083 gross over 10 sessions; every 5m variant lost after
# charges — 5m moves are too small to clear ~0.16% round-trip friction).
CANDLE_INTERVAL = "15m"
SWING_LOOKBACK = 10             # bars used to find the swing low/high for the stop
RR_TARGET = 2.0                 # target = entry +/- 2x risk (1:2)
RISK_PER_TRADE = 200.0          # INR risked per trade -> sizes the position
POLL_SECONDS = 60               # live-loop poll interval (delayed data)
SQUAREOFF_TIME = "15:15"        # IST intraday square-off
MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:30"

# --- Strategy: AVWAP scalp (python port of tradingview/avwap_scalp.pine) ---
AVWAP_RR = 1.5                  # target = entry +/- 1.5x risk
AVWAP_ATR_MULT = 0.5            # stop = candle extreme +/- 0.5x ATR(14)
AVWAP_RSI_LEN = 14
AVWAP_EMA_LEN = 20              # trend filter
AVWAP_VOL_MULT = 1.2            # volume surge threshold vs 20-bar average

# --- Risk kernel (hard limits — the AI agent can NEVER override these) ---
DAILY_LOSS_LIMIT = -2000.0      # INR; hard stop for the day
MAX_POSITION_VALUE = 50_000.0   # INR per position
MAX_OPEN_POSITIONS = 5          # portfolio-level cap on concurrent positions
MAX_ORDERS_PER_SEC = 8          # SEBI 10-OPS threshold with buffer

# --- Fill simulation (paper mode) ---
SLIPPAGE_PCT = 0.0005           # 0.05% assumed slippage
# Rough intraday cost model per round trip (brokerage + STT + charges).
# Verify against Zerodha's brokerage calculator — STT rates changed in 2026.
CHARGES_PCT_ROUND_TRIP = 0.0006

# --- AI agent ---
# Provider is auto-selected in trading/agents/llm.py: Gemini when
# GEMINI_API_KEY is set, Anthropic otherwise (override with LLM_PROVIDER).
ANTHROPIC_MODEL = "claude-opus-4-8"
# gemini-2.5-flash works on the free tier; switch to gemini-2.5-pro (or a
# gemini-3 model) once billing is enabled on the Google AI Studio project.
GEMINI_MODEL = "gemini-2.5-flash"
AGENT_MAX_TOKENS = 16000
SUPERVISOR_POLL_SECONDS = 5     # how often the async supervisor checks for new trades

# Safe defaults used whenever the pre-market agent fails or returns invalid output
FALLBACK_DAY_CONFIG = {
    "regime": "choppy",
    "risk_multiplier": 0.5,
    "blocked_symbols": [],
    "rationale": "fallback: pre-market agent unavailable — trading at half size",
}
