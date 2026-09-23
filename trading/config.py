"""Central configuration for the trading system.

All paths are anchored to the project root so cron jobs work regardless of CWD.
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- Trading Mode: "paper" vs "live" ---
# Default is "paper". When you are ready to trade with real money in Fyers,
# change this to "live" (or set the TRADING_MODE=live environment variable).
TRADING_MODE = os.environ.get("TRADING_MODE", "paper")

# --- Telegram Notifications ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8609336942:AAGU7IMNSDcbPTfe4ykhXamnLplZRtwb1OI")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "629517899")

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

# What the strategy engine scans — expanded to Nifty 50 so it actively
# monitors all leading liquid Indian stocks for ORB breakout opportunities.
SCAN_UNIVERSE = NIFTY50
YF_SUFFIX = ".NS"


# --- data retention ---
# The dashboard is a working screen, not an archive. Keep the last N *sessions*
# (not calendar days — a long weekend would otherwise eat one) of trades,
# rejections, agent logs, journal reports and cached candles.
# Pruning is destructive: raise this, or pass --keep-days, before it runs if you
# want a longer history. See trading/retention.py.
RETENTION_DAYS = 5

DB_PATH = PROJECT_ROOT / "data" / "ledger.db"
TODAY_CONFIG_PATH = PROJECT_ROOT / "data" / "today_config.json"
FYERS_TOKEN_PATH = PROJECT_ROOT / "data" / "fyers_token.json"
REPORTS_DIR = PROJECT_ROOT / "reports"
LOGS_DIR = PROJECT_ROOT / "logs"

# --- Fyers API v3 (Real-time live market feed) ---
FYERS_APP_ID = "J8ZMHWBTBW-100"
FYERS_SECRET_ID = "KLFH4NCSIV"
FYERS_REDIRECT_URI = "http://localhost:3001/api/fyers/callback"

# --- Strategy Engine Selection ---
ACTIVE_STRATEGY = os.environ.get("ACTIVE_STRATEGY", "orb_200ma")
ENABLE_FNO_AUTO_TRADER = os.environ.get("ENABLE_FNO_AUTO_TRADER", "0") in ("1", "true", "True")

# --- Strategy: ORB + Multi-TF 200MA Trend Filter ---
ORB_MINUTES = 15
ORB_MIN_RANGE_PCT = 0.3
ORB_MAX_RANGE_PCT = 1.5
ORB_USE_1H_FILTER = True
ORB_USE_30M_FILTER = True
ORB_MA_LEN = 200
ORB_ATR_LEN = 14
ORB_SL_ATR_MULT = 1.0
ORB_TP_R_MULT = 1.8
ORB_USE_VOL_FILTER = True
ORB_ONE_TRADE_PER_SIDE = True
ORB_NO_NEW_ENTRIES_AFTER = "14:30"

# --- Strategy: EMA crossover (legacy / secondary) ---
FAST_EMA = 9
SLOW_EMA = 21
# 15m: the only net-positive config in the Jun-30→Jul-13 backtest
# (+145 net / +1083 gross over 10 sessions; every 5m variant lost after
# charges — 5m moves are too small to clear ~0.16% round-trip friction).
CANDLE_INTERVAL = "15m"
SWING_LOOKBACK = 10             # bars used to find the swing low/high for the stop
RR_TARGET = 2.0                 # target = entry +/- 2x risk (1:2)
# Account sizing calibrated specifically for 15k to 1 Lakh Survival & Compounding Challenge:
ACCOUNT_CAPITAL = 15_000.0

CHALLENGE_TARGET = 100_000.0
RISK_PER_TRADE = 225.0          # INR risked per trade base (1.5% of 15k) -> safe, realistic position sizing

def get_dynamic_risk_per_trade() -> float:
    """Return dynamic risk per trade from the 15k->1 Lakh challenge engine."""
    try:
        from trading.challenge import get_challenge_risk
        return get_challenge_risk()
    except Exception:
        return RISK_PER_TRADE

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
# Scaled to ACCOUNT_CAPITAL = 15,000 INR:
DAILY_LOSS_LIMIT = -600.0       # INR; hard stop for the day (~4% max drawdown cap)
MAX_POSITION_VALUE = 50_000.0   # INR per position (within 5x intraday MIS leverage)
MAX_OPEN_POSITIONS = 3          # max 3 simultaneous positions (preserves margin)
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
