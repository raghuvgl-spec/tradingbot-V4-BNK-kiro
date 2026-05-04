import os
from pathlib import Path
from dotenv import load_dotenv
from datetime import time as dtime

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

load_dotenv()
load_dotenv(ENV_PATH, override=True)

# ------------------------------------------------------------------
# Core credentials
# ------------------------------------------------------------------
API_KEY = os.getenv("API_KEY", "").strip()
CLIENT_ID = os.getenv("CLIENT_ID", "").strip()
PASSWORD = os.getenv("PASSWORD", "").strip()
TOTP_SECRET = os.getenv("TOTP_SECRET", "").strip()

# ------------------------------------------------------------------
# Trading mode
# ------------------------------------------------------------------
PAPER_TRADING = os.getenv("PAPER_TRADING", "True").lower() == "true"
DEBUG_MODE = os.getenv("DEBUG_MODE", "False").lower() == "true"

# Live index / options configuration
LIVE_SYMBOL = os.getenv("LIVE_SYMBOL", "BANKNIFTY").strip().upper()
INDEX_SYMBOL = LIVE_SYMBOL
INDEX_TOKEN = os.getenv("INDEX_TOKEN", "99926009").strip()
INDEX_EXCHANGE = os.getenv("INDEX_EXCHANGE", "NSE").strip().upper()



ATR_TRAIL_MULTIPLIER = 1.0
ENTRY_DISTANCE_ATR_MULT = 1.5
MIN_ATR_THRESHOLD = 12

REENTRY_EMA_GAP_MULTIPLIER = 0.25
REENTRY_PRICE_GAP_MULTIPLIER = 0.15


# Debug / futures mode
DEBUG_SYMBOL = os.getenv("DEBUG_SYMBOL", "CRUDEOIL18MAY26FUT").strip().upper()
DEBUG_TOKEN = os.getenv("DEBUG_TOKEN", "488290").strip()
DEBUG_EXCHANGE_TYPE = int(os.getenv("DEBUG_EXCHANGE_TYPE", "5"))
DEBUG_ORDER_EXCHANGE = os.getenv("DEBUG_ORDER_EXCHANGE", "MCX").strip().upper()

TRADE_MODE = "FUTURE" if DEBUG_MODE else "OPTIONS"

START_MODE = 'auto'

# PENDING ENTRY CLEANING 
PENDING_MAX_AGE_MINUTES = 3
PENDING_MAX_DRIFT_ATR_MULTIPLIER = 0.8
CANCEL_PENDING_IF_TREND_WEAKENS = True

# ------------------------------------------------------------------
# Strategy parameters
# ------------------------------------------------------------------
FAST_EMA_PERIOD = int(os.getenv("FAST_EMA_PERIOD", "9"))
SLOW_EMA_PERIOD = int(os.getenv("SLOW_EMA_PERIOD", "21"))

LOT_SIZE = int(os.getenv("LOT_SIZE", "30"))
LOTS = int(os.getenv("LOTS", "1"))
QTY = LOTS * LOT_SIZE
MAX_TRADES = int(os.getenv("MAX_TRADES", "50"))

SL_POINTS = float(os.getenv("SL_POINTS", "20"))
TARGET_POINTS = float(os.getenv("TARGET_POINTS", "40"))
TRAIL_STEP = float(os.getenv("TRAIL_STEP", "15"))
TRAIL_SL = float(os.getenv("TRAIL_SL", "10"))

PARTIAL_BOOK_AT = float(os.getenv("PARTIAL_BOOK_AT", "30"))
PARTIAL_LOTS = int(os.getenv("PARTIAL_LOTS", "1"))

MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "-15000"))
MAX_CONSECUTIVE_SL = int(os.getenv("MAX_CONSECUTIVE_SL", "25"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "2"))

REENTRY_MAX_PULLBACK_ATR = float(os.getenv("REENTRY_MAX_PULLBACK_ATR", "1.2"))
REENTRY_MIN_RECLAIM_BODY_ATR = float(os.getenv("REENTRY_MIN_RECLAIM_BODY_ATR", "0.20"))
REENTRY_BREAK_BUFFER_ATR = float(os.getenv("REENTRY_BREAK_BUFFER_ATR", "0.10"))
REENTRY_MIN_CANDLE_BODY_RATIO = float(os.getenv("REENTRY_MIN_CANDLE_BODY_RATIO", "0.35"))


# ------------------------------------------------------------------
# Execution safety parameters
# ------------------------------------------------------------------
USE_DISCOUNT_ENTRY = os.getenv("USE_DISCOUNT_ENTRY", "False").lower() == "true"
PENDING_TRADE_TIMEOUT_SEC = int(os.getenv("PENDING_TRADE_TIMEOUT_SEC", "60"))
REQUIRE_OPTION_LTP = os.getenv("REQUIRE_OPTION_LTP", "True").lower() == "true"
WS_STALE_SECONDS = int(os.getenv("WS_STALE_SECONDS", "20"))
RECONCILE_WITH_BROKER_ON_STARTUP = os.getenv(
    "RECONCILE_WITH_BROKER_ON_STARTUP", "True"
).lower() == "true"


# ------------------------------------------------------------------
# ATR / exit engine parameters
# ------------------------------------------------------------------
ATR_SL_MULTIPLIER = float(os.getenv("ATR_SL_MULTIPLIER", "1.2"))
ATR_TARGET_MULTIPLIER = float(os.getenv("ATR_TARGET_MULTIPLIER", "2.5"))
USE_ATR_TRAILING = os.getenv("USE_ATR_TRAILING", "True").lower() == "true"
ATR_TRAIL_STEP_MULTIPLIER = float(os.getenv("ATR_TRAIL_STEP_MULTIPLIER", "0.30"))
ATR_TRAIL_SL_MULTIPLIER = float(os.getenv("ATR_TRAIL_SL_MULTIPLIER", "0.15"))
TRAIL_ONLY_MODE = os.getenv("TRAIL_ONLY_MODE", "False").lower() == "true"
ENABLE_MOMENTUM_EXIT = os.getenv("ENABLE_MOMENTUM_EXIT", "True").lower() == "true"
ATR_MOMENTUM_PROFIT = float(os.getenv("ATR_MOMENTUM_PROFIT", "2.0"))
ATR_MOMENTUM_DISTANCE = float(os.getenv("ATR_MOMENTUM_DISTANCE", "2.5"))
ENABLE_TRADE_PROTECTION = os.getenv("ENABLE_TRADE_PROTECTION", "False").lower() == "true"
ATR_MAX_LOSS_MULTIPLIER = float(os.getenv("ATR_MAX_LOSS_MULTIPLIER", "1.8"))
ATR_PROFIT_LOCK_MULTIPLIER = float(os.getenv("ATR_PROFIT_LOCK_MULTIPLIER", "0.6"))

ATR_PROFIT_GIVEBACK = float(os.getenv("ATR_PROFIT_GIVEBACK", "0.18") or 0.18)
PROFIT_GIVEBACK_PCT = float(os.getenv("PROFIT_GIVEBACK_PCT", "0.10"))

ENABLE_EARLY_EMA_PROTECTION = os.getenv("ENABLE_EARLY_EMA_PROTECTION", "False").lower() == "true"
EARLY_EXIT_EMA_BUFFER = float(os.getenv("EARLY_EXIT_EMA_BUFFER", "0.0"))
EARLY_EXIT_MIN_HOLD_SEC = int(os.getenv("EARLY_EXIT_MIN_HOLD_SEC", "120"))
EARLY_EXIT_ONLY_IF_LOSING = os.getenv("EARLY_EXIT_ONLY_IF_LOSING", "True").lower() == "true"
REENTRY_ARM_ON_EARLY_EXIT = os.getenv("REENTRY_ARM_ON_EARLY_EXIT", "True").lower() == "true"
REENTRY_WINDOW_SEC = int(os.getenv("REENTRY_WINDOW_SEC", "600"))
ENTRY_TRAIL_STEP = float(os.getenv("ENTRY_TRAIL_STEP", "0"))
ENTRY_UP_MOVE = float(os.getenv("ENTRY_UP_MOVE", "0"))
ENTRY_DOWN_MOVE = float(os.getenv("ENTRY_DOWN_MOVE", "0"))
MIN_DISCOUNT = float(os.getenv("MIN_DISCOUNT", "0"))
ENABLE_PARTIAL_BOOKING = os.getenv("ENABLE_PARTIAL_BOOKING", "True").lower() == "true"
DISCOUNT_POINTS = float(os.getenv("DISCOUNT_POINTS", "0"))
ORDER_TAG_PREFIX = os.getenv("ORDER_TAG_PREFIX", "TBOT").strip()[:12]
MAX_REENTRIES = int(os.getenv("MAX_REENTRIES", "2"))
MIN_EMA_GAP_ATR =float(os.getenv("MIN_EMA_GAP_ATR", "0.25"))
REENTRY_MIN_EMA_GAP_ATR = float(os.getenv("REENTRY_MIN_EMA_GAP_ATR", "0.35"))
MIN_CANDLE_RANGE_ATR=float(os.getenv("MIN_CANDLE_RANGE_ATR", "0.50"))
VWAP_NO_TRADE_ATR=float(os.getenv("VWAP_NO_TRADE_ATR", "0.30"))
MIN_BODY_RATIO=float(os.getenv("MIN_BODY_RATIO", "0.45"))

USE_PREBUY_ENTRY = os.getenv("USE_PREBUY_ENTRY", "True").lower() == "true"
PREBUY_MAX_EMA_GAP = float(os.getenv("PREBUY_MAX_EMA_GAP", "4.5"))
PREBUY_MIN_GAP_SHRINK = float(os.getenv("PREBUY_MIN_GAP_SHRINK", "0.8"))
# ------------------------------------------------------------------
# Startup / warmup parameters
# ------------------------------------------------------------------
STARTUP_PREWARM_ENABLED = os.getenv("STARTUP_PREWARM_ENABLED", "True").lower() == "true"
STARTUP_PREWARM_CANDLES = int(os.getenv("STARTUP_PREWARM_CANDLES", "60"))

# ------------------------------------------------------------------
# Market timings
# ------------------------------------------------------------------
# ------------------------------------------------------------------
# Market timings (configurable via .env)
# ------------------------------------------------------------------
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(
    int(os.getenv("MARKET_CLOSE_HOUR", "23")),
    int(os.getenv("MARKET_CLOSE_MINUTE", "30"))
)
START_TIME = dtime(
    int(os.getenv("START_HOUR", "9")),
    int(os.getenv("START_MINUTE", "15"))
)
END_TIME = dtime(
    int(os.getenv("END_HOUR", "23")),
    int(os.getenv("END_MINUTE", "30"))
)

# ------------------------------------------------------------------
# Paths — instrument-specific files
# ------------------------------------------------------------------
BOT_STATE_FILE = DATA_DIR / "bot_state.json"
BOT_CONTROL_FILE = DATA_DIR / "bot_control.json"
BOT_LOG_FILE = LOG_DIR / "bot.log"

# Active instrument tag used for file naming
_INSTRUMENT_TAG = "crude" if DEBUG_MODE else LIVE_SYMBOL.lower()

MARKET_DATA_FILE = DATA_DIR / f"market_data_{_INSTRUMENT_TAG}.csv"
TRADE_LOG_FILE   = DATA_DIR / f"trade_log_{_INSTRUMENT_TAG}.xlsx"
TICK_LOG_FILE    = DATA_DIR / f"tick_log_{_INSTRUMENT_TAG}.csv"

# ------------------------------------------------------------------
# Trade Intelligence settings
# ------------------------------------------------------------------
TRADE_DB_PATH = DATA_DIR / "trade_intelligence.db"
PATTERN_TOLERANCE_PCT = float(os.getenv("PATTERN_TOLERANCE_PCT", "0.20"))
PATTERN_SKIP_THRESHOLD = float(os.getenv("PATTERN_SKIP_THRESHOLD", "0.70"))
PATTERN_CONFIRM_THRESHOLD = float(os.getenv("PATTERN_CONFIRM_THRESHOLD", "0.60"))
PATTERN_MIN_TRADES = int(os.getenv("PATTERN_MIN_TRADES", "20"))
PATTERN_FALLBACK_N = int(os.getenv("PATTERN_FALLBACK_N", "10"))
PATTERN_LOG_ONLY = os.getenv("PATTERN_LOG_ONLY", "True").lower() == "true"
PATTERN_RECENCY_5D_WEIGHT = float(os.getenv("PATTERN_RECENCY_5D_WEIGHT", "2.0"))
PATTERN_RECENCY_10D_WEIGHT = float(os.getenv("PATTERN_RECENCY_10D_WEIGHT", "1.5"))
ANALYZER_MIN_TRADES = int(os.getenv("ANALYZER_MIN_TRADES", "10"))

# ------------------------------------------------------------------
# Sanity checks
# ------------------------------------------------------------------
if LOTS <= 0:
    raise ValueError("LOTS must be at least 1")

if LOT_SIZE <= 0:
    raise ValueError("LOT_SIZE must be positive")

if QTY % LOT_SIZE != 0:
    raise ValueError("Quantity must be a multiple of lot size")

if PARTIAL_LOTS < 0:
    raise ValueError("PARTIAL_LOTS cannot be negative")

if PARTIAL_LOTS > LOTS:
    raise ValueError("PARTIAL_LOTS cannot be greater than LOTS")


print("ENV PATH:", ENV_PATH)
print("ENV EXISTS:", ENV_PATH.exists())
print("BASE_DIR =", BASE_DIR)
print("DATA_DIR =", DATA_DIR)
print("BOT_STATE_FILE =", BOT_STATE_FILE)
print("BOT_CONTROL_FILE =", BOT_CONTROL_FILE)
print("MARKET_DATA_FILE =", MARKET_DATA_FILE)
print("PAPER_TRADING =", PAPER_TRADING)
print("\n🔧 CONFIG LOADED VALUES:")
print(f"TRADE_MODE = {TRADE_MODE}")
print(f"LIVE_SYMBOL = {LIVE_SYMBOL}")
print(f"SL_POINTS = {SL_POINTS}")
print(f"TARGET_POINTS = {TARGET_POINTS}")
print(f"TRAIL_STEP = {TRAIL_STEP}")
print(f"TRAIL_SL = {TRAIL_SL}")
print(f"MAX_TRADES = {MAX_TRADES}")
print(f"MAX_DAILY_LOSS = {MAX_DAILY_LOSS}")
print(f"MAX_CONSECUTIVE_SL = {MAX_CONSECUTIVE_SL}")
print(f"COOLDOWN_MINUTES = {COOLDOWN_MINUTES}")
print(f"PENDING_TRADE_TIMEOUT_SEC = {PENDING_TRADE_TIMEOUT_SEC}")
print(f"REQUIRE_OPTION_LTP = {REQUIRE_OPTION_LTP}")
print(f"WS_STALE_SECONDS = {WS_STALE_SECONDS}")
print(f"FAST_EMA_PERIOD = {FAST_EMA_PERIOD}")
print(f"SLOW_EMA_PERIOD = {SLOW_EMA_PERIOD}")
print(f"ATR_SL_MULTIPLIER = {ATR_SL_MULTIPLIER}")
print(f"ATR_TARGET_MULTIPLIER = {ATR_TARGET_MULTIPLIER}")
print(f"USE_ATR_TRAILING = {USE_ATR_TRAILING}")
print(f"ATR_TRAIL_STEP_MULTIPLIER = {ATR_TRAIL_STEP_MULTIPLIER}")
print(f"ATR_TRAIL_SL_MULTIPLIER = {ATR_TRAIL_SL_MULTIPLIER}")
print(f"ENABLE_EARLY_EMA_PROTECTION = {ENABLE_EARLY_EMA_PROTECTION}")
print(f"STARTUP_PREWARM_ENABLED = {STARTUP_PREWARM_ENABLED}")
print(f"STARTUP_PREWARM_CANDLES = {STARTUP_PREWARM_CANDLES}")
print(f"COOLDOWN_MINUTES = {COOLDOWN_MINUTES}")
print(f"ENV PATH:= {ENV_PATH}")
print("-" * 40)


