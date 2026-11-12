"""Central configuration for the trading system.

All paths are anchored to the project root so cron jobs work regardless of CWD.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


# --- Trading universe ---
# Core watchlist. This is both what the strategy engine scans and what the
# pre-market agent is shown, so it is the only set of names the agent can block.
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
FYERS_APP_ID = os.getenv("FYERS_APP_ID", "J8ZMHWBTBW-100")
FYERS_SECRET_ID = os.getenv("FYERS_SECRET_ID", "KLFH4NCSIV")
FYERS_REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI", "http://localhost:3001/api/fyers/callback")

# --- Strategy: EMA crossover ---
FAST_EMA = 9
SLOW_EMA = 21
CANDLE_INTERVAL = "15m"
SWING_LOOKBACK = 10
RR_TARGET = 2.0
# Account sizing: capital set to 27,000 INR.
ACCOUNT_CAPITAL = 27_000.0
RISK_PER_TRADE = 135.0           # INR risked per trade (0.5% of capital)
POLL_SECONDS = 60
SQUAREOFF_TIME = "15:15"
MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:30"

# --- Strategy: AVWAP scalp ---
AVWAP_RR = 1.5
AVWAP_ATR_MULT = 0.5
AVWAP_RSI_LEN = 14
AVWAP_EMA_LEN = 20
AVWAP_VOL_MULT = 1.2

# --- Risk kernel (hard limits — the AI agent can NEVER override these) ---
# Scaled to accommodate stock lot-wise option trading (up to 2-3 standard lot trades):
DAILY_LOSS_LIMIT = -2700.0      # INR; hard stop for the day (10% of 27k capital)
DAILY_PROFIT_TARGET = 3_500.0   # INR; stop opening new trades once hit for the day
MAX_POSITION_VALUE = 5_400.0    # INR per position (20% of capital for equity shares)
MAX_OPEN_POSITIONS = 5          # portfolio cap (max 100% of capital deployed)
MAX_ORDERS_PER_SEC = 8

# --- Fill simulation (paper mode) ---
SLIPPAGE_PCT = 0.0005           # 0.05% assumed slippage
# Rough intraday cost model per round trip (brokerage + STT + charges).
# Verify against Zerodha's brokerage calculator — STT rates changed in 2026.
CHARGES_PCT_ROUND_TRIP = 0.0006
MAX_COST_RISK_RATIO = 0.30      # Max friction as a fraction of risk (30%)

# --- Exit management: deterministic trailing stop ---
TRAIL_ENABLED = True             # Enabled: trailing stop moves to breakeven & trails to protect profit
BREAKEVEN_AT_R = 1.0            # favourable excursion (in R) that moves the stop to breakeven
OPTION_BREAKEVEN_AT_R = 0.3     # for options: arm breakeven at 0.3R gain (rapid risk elimination)
OPTION_BREAKEVEN_PCT = 0.02     # for options: arm breakeven at +2% gain (locks cost + brokerage)
TRAIL_ATR_MULT = 2.0            # chandelier trail distance in ATR(14) units
PARTIAL_AT_R = 1.5              # R multiple that triggers partial booking (None disables)
PARTIAL_PCT = 0.5               # fraction of the position booked at the partial
AUTO_EXIT_ON_STOP = True        # Automatically close open positions when trailing stop or target is hit

# --- Position Deduplication & Re-entry Cooldown ---
ALLOW_DUPLICATE_SYMBOLS = False      # Strictly disallow opening duplicate trades on the same underlying stock/option
REENTRY_COOLDOWN_MINUTES = 10         # Standard cooldown between trades on the same underlying (gives time for new base)
LOSS_COOLDOWN_MINUTES = 20           # Strict cooldown after a losing trade on the same underlying (prevents chop/revenge)
MAX_LOSSES_PER_SYMBOL_DAY = 2        # Lockout underlying after 2 losing trades in the same session
MAX_DAILY_TRADES = 10                # Maximum total quality trades allowed per day (plenty of room for good setups)
AGENT_TRADE_INDEX_OPTIONS = True    # Automatically evaluate and execute NIFTY & BANK NIFTY index option breakout setups

# --- AI agent ---
# Provider is auto-selected in trading/agents/llm.py: Gemini when
# GEMINI_API_KEY is set, Anthropic otherwise (override with LLM_PROVIDER).
ANTHROPIC_MODEL = "claude-opus-4-8"
# Google Gemini: gemini-3.1-flash-lite (fast responsive reasoning) with gemini-3.5-flash-lite fallback
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")
AGENT_MAX_TOKENS = 16000
SUPERVISOR_POLL_SECONDS = 5     # how often the async supervisor checks for new trades

# Multi-Agent stock evaluator parameters
AGENT_MIN_CONVICTION = 7        # Conviction score out of 10 required to execute a trade
AGENT_MAX_TRADES_PER_SCAN = 3   # Max new trades to execute in a single scan pass

# --- AI agent: the F&O long scanner as a second candidate source ---
# The scanner (trading/fno/) and the agent were two disjoint pipelines: the
# scanner printed BUY/WATCH cards that nothing ever traded. When enabled, the
# agent reads the scanner's verdicts AND its plan (entry band, structural stop,
# 1:2 / 1:3 targets) and runs them through the same council and risk kernel.
# Opt-in and OFF by default, like TRAIL_ENABLED: nothing changes until flipped.
AGENT_SCANNER_ENABLED = True
# Deliberately NOT an "orb_ma200*" id, so ExecutionRouter.risk_check's 200MA
# trend gate does not apply to it -- the same pattern ema_9_21 and avwap_scalp
# already use. The agent re-applies that gate itself before routing; see
# scanner_preflight() in agents/agent_trader.py. Do not retire the agent-side
# check on the assumption the router is covering it.
AGENT_SCANNER_STRATEGY_ID = "fno_scanner"
AGENT_SCANNER_TIERS = ("BUY", "WATCH")   # ("BUY",) would be the BUY picks only
AGENT_SCANNER_MAX = 3                    # names taken from one scan per pass
# Enter only while price is inside the scanner's own entry band (plus chase tolerance).
AGENT_SCANNER_REQUIRE_ENTRY_BAND = True
AGENT_SCANNER_CHASE_TOLERANCE_PCT = 0.0075  # 0.75% chase buffer above entry_high (e.g. ₹20 on ₹2500)
AGENT_SCANNER_ENFORCE_200MA = False         # Do not require long-term 200 SMA on intraday momentum scanner breakouts
MIN_RISK_MULTIPLIER_FLOOR = 0.25            # Floor to prevent total paralysis from missing feeds


# Safe defaults used whenever the pre-market agent fails or returns invalid output
FALLBACK_DAY_CONFIG = {
    "regime": "choppy",
    "risk_multiplier": 0.5,
    "blocked_symbols": [],
    "rationale": "fallback: pre-market agent unavailable — trading at half size",
}

# Feeds that would tell the agent whether today carries event risk (an earnings
# print, an expiry, a macro release). Neither is wired up yet.
RISK_CRITICAL_FEEDS = ("events_calendar", "news_headlines")

# While any of those feeds is dark, the agent cannot know what it doesn't know,
# so the risk multiplier is capped here rather than trusting the model to
# self-report. On 2026-10-06 it read "unavailable" as "no specific earnings or
# events reported" and justified full 1.0x size on that basis — five days
# earlier, on the same darkness, it had halved risk instead. The cap is not a
# judgement on the model's reasoning; it is that the spread between those two
# readings is 2x position size, and only one of them is safe.
#
# This only ever REDUCES risk — it can never promote. 1.0x becomes reachable
# again when the feeds behind RISK_CRITICAL_FEEDS actually exist.
DARK_FEED_RISK_CAP = 0.75

# --- Options Trading Settings ---
INDEX_LOT_SIZES: dict[str, int] = {
    "NIFTY": 65,
    "NIFTY 50": 65,
    "NSE:NIFTY50-INDEX": 65,
    "BANKNIFTY": 30,
    "BANK NIFTY": 30,
    "NSE:NIFTYBANK-INDEX": 30,
    "SENSEX": 20,
    "BANKEX": 30,
    "FINNIFTY": 60,
    "MIDCPNIFTY": 120,
    "NIFTYNXT50": 25,
    "SENSEX50": 75,
}

STOCK_LOT_SIZES: dict[str, int] = {
    # Large-Cap & Core Watchlist (Active contracts specification)
    "RELIANCE": 500, "HDFCBANK": 650, "ICICIBANK": 700, "INFY": 400, "TCS": 225,
    "SBIN": 750, "BHARTIARTL": 475, "ITC": 1725, "LT": 175, "AXISBANK": 625,
    "KOTAKBANK": 2000, "TATAMOTORS": 1600, "TATASTEEL": 2750, "BAJFINANCE": 750, "MARUTI": 50,
    "TITAN": 175, "ADANIENT": 309, "WIPRO": 3000, "HINDUNILVR": 300, "SUNPHARMA": 350,
    "PAGEIND": 20, "IDEA": 71475,
    # F&O Universe - Financials & Non-Banking
    "MUTHOOTFIN": 250, "CDSL": 375, "BAJAJFINSV": 500, "CHOLAFIN": 625, "SHRIRAMFIN": 150,
    "HDFCAMC": 150, "HDFCLIFE": 1100, "ICICIGI": 500, "ICICIPRULI": 1500, "IRFC": 3750,
    "LICHSGFIN": 1000, "PFC": 1300, "RECLTD": 1000, "SBILIFE": 750, "AUBANK": 500,
    "BANDHANBNK": 2500, "BANKBARODA": 2925, "CANBK": 6750, "FEDERALBNK": 5000, "IDFCFIRSTB": 7500,
    "INDUSINDBK": 500, "PNB": 8000,
    # IT & Tech
    "COFORGE": 150, "HCLTECH": 350, "LTIM": 150, "MPHASIS": 275, "PERSISTENT": 100,
    "TECHM": 600, "NAUKRI": 75, "ZOMATO": 2100, "NYKAA": 1875,
    "PAYTM": 900, "POLICYBZR": 350,
    # Auto & Ancillaries
    "ASHOKLEY": 5000, "BAJAJ-AUTO": 125, "BALKRISIND": 300, "BHARATFORG": 500, "EICHERMOT": 150,
    "EXIDEIND": 1800, "HEROMOTOCO": 150, "M&M": 350, "MOTHERSON": 6150, "MRF": 5, "TVSMOTOR": 350,
    # Metals & Mining
    "APLAPOLLO": 700, "HINDALCO": 1400, "HINDCOPPER": 2650, "JINDALSTEL": 625, "JSWSTEEL": 675,
    "NATIONALUM": 3750, "SAIL": 4000, "VEDL": 1150, "COALINDIA": 2100,
    # Pharma & Healthcare
    "ALKEM": 100, "APOLLOHOSP": 125, "AUROPHARMA": 550, "BIOCON": 2500, "CIPLA": 650,
    "DIVISLAB": 200, "DRREDDY": 125, "LAURUSLABS": 1700, "LUPIN": 425,
    "TORNTPHARM": 250, "ZYDUSLIFE": 900,
    # Energy, Oil & Power
    "ADANIENSOL": 700, "ADANIGREEN": 600, "BPCL": 1800, "GAIL": 2650, "HINDPETRO": 1350,
    "IOC": 4875, "NHPC": 6750, "NTPC": 1500, "OIL": 1350, "ONGC": 3850, "POWERGRID": 1800, "TATAPOWER": 1350,
    # FMCG & Retail
    "BRITANNIA": 200, "COLPAL": 200, "DABUR": 1250, "GODREJCP": 500,
    "JUBLFOOD": 1250, "MARICO": 1200, "NESTLEIND": 250, "TATACONSUM": 900,
    "TRENT": 100, "UBL": 400, "VBL": 1500,
    # Cement, Infra & Realty
    "ACC": 300, "AMBUJACEM": 900, "DLF": 825, "GODREJPROP": 200, "GRASIM": 475,
    "LODHA": 425, "OBEROIRLTY": 350, "PRESTIGE": 400, "SHREECEM": 25, "ULTRACEMCO": 100,
    # Telecom, Media & Capital Goods
    "INDUSTOWER": 1700, "PVRINOX": 400, "ZEEL": 3000, "ABB": 125,
    "ADANIPORTS": 400, "ASIANPAINT": 200, "BDL": 400, "BEL": 2850,
    "CONCOR": 1000, "CUMMINSIND": 300, "DEEPAKNTR": 300, "HAL": 150, "HAVELLS": 500,
    "INDIGO": 150, "IRCTC": 875, "PIDILITIND": 250, "POLYCAB": 125, "SIEMENS": 125,
    "SRF": 375, "TATACHEM": 550, "UPL": 1300,
}

DEFAULT_STOCK_OPTION_LOT = 250
MAX_OPTION_POSITION_VALUE = 27_000.0  # INR; hard cap per option trade (allows full 27k capital deployment)
MAX_OPTION_RISK_PER_TRADE = 1350.0    # INR; max risk per option trade (5% of 27k capital)
OPTION_STOP_LOSS_PCT = 0.10           # 10% maximum stop loss (cuts loss small instead of 30% collapse)
MIN_OPTION_STOP_LOSS_PCT = 0.03       # 3% minimum stop loss distance (stops tighter than 3% get noise-stopped)
OPTION_STRIKE_TYPE = "ITM"            # "ITM" (In-The-Money only) or "ATM"
OPTION_ITM_DEPTH = 1                  # 1 = Nearest ITM strike (1 strike ITM for peak liquidity & delta)
AGENT_TRADE_OPTIONS = True            # When True, the agent executes trade signals as ITM option contracts


def get_max_viable_option_premium(
    symbol: str,
    max_risk: float = MAX_OPTION_RISK_PER_TRADE,
    max_pos_val: float = MAX_OPTION_POSITION_VALUE,
    sl_pct: float = OPTION_STOP_LOSS_PCT,
) -> float:
    """Calculate the maximum option premium permissible for a stock so that:
       1) lot_size * (premium * sl_pct) <= max_risk (₹1,250 default)
       2) lot_size * premium <= max_pos_val (₹10,000 default)
    """
    from trading.options_engine import get_lot_size
    lot_size = get_lot_size(symbol)
    if lot_size <= 0:
        return 0.0
    prem_from_risk = max_risk / (lot_size * max(0.01, sl_pct))
    prem_from_cap = max_pos_val / lot_size
    return round(min(prem_from_risk, prem_from_cap), 2)


