# -*- coding: utf-8 -*-
"""
================================================================================
 config.py — ALL hardcoded configuration for the NIFTY regime-based options
 trading engine.  Per the system spec: no runtime input; every parameter,
 flag and path lives here.  Edit this file, then run `python main.py`.
================================================================================
"""
from datetime import time, date, timedelta

# ----------------------------------------------------------------------------
# 1. FLAGS & PATHS
# ----------------------------------------------------------------------------
PAPER_TRADING_MODE = True          # True = simulated fills (never touches a real
                                   #         brokerage account).
                                   # False = real orders via Upstox (LIVE).
DATA_SOURCE = "simulated"          # "simulated" -> offline DemoProvider feed
                                   # "upstox"    -> real Upstox REST + WebSocket
                                   # Use "upstox" only if env.txt is present with
                                   # a valid UPSTOX_ACCESS_TOKEN.

ALLOW_NON_TRADING_DAY_RUN = False  # For testing only (demo mode forces True with a warning)

ENV_FILE = "env.txt"               # UPSTOX_API_KEY / UPSTOX_API_SECRET / UPSTOX_ACCESS_TOKEN

LOG_DIR = "./data"                 # relative to script root
STATE_DB = "state.db"              # SQLite checkpoint / recovery store
TRADE_ANALYSIS_CSV = "trade_analysis.csv"   # one row per CLOSED trade
AUDIT_LOG_CSV = "audit_log.csv"             # daily-rotated (gzip), JSONL
REGIME_LOG_CSV = "regime_log.csv"           # one row per detection cycle
REGIME_STATE_JSON = "regime_state.json"     # backward-compatible JSON snapshot
EVENTS_FILE = "events.json"                # macro calendar for Step-6 override
SEED_STATE_JSON = "regime_state_seed.json" # optional skew-history seed (ignored if absent)

# ----------------------------------------------------------------------------
# 2. VIX & DELTA BANDS
# ----------------------------------------------------------------------------
LOW_VIX = 12.0
HIGH_VIX = 18.0

MIN_DELTA_LOWVIX, MAX_DELTA_LOWVIX = 0.22, 0.28
MIN_DELTA, MAX_DELTA = 0.20, 0.25
MIN_DELTA_HIVIX, MAX_DELTA_HIVIX = 0.15, 0.20
MIN_PREMIUM, MAX_PREMIUM = 75.0, 130.0

# ----------------------------------------------------------------------------
# 3. EDGE & TREND THRESHOLDS
# ----------------------------------------------------------------------------
EDGE_RICH = 5.0          # IV_ATM - RV > 5%  -> seller edge (+1)
EDGE_CHEAP = 0.0         # IV_ATM - RV < 0%  -> buyer edge (-1)
TREND_ADX = 25.0         # ADX above this + EMA slope -> trending
RANGE_ADX = 22.0         # ADX below this -> range-bound
EMA_SLOPE_MIN_PCT = 0.05 # |EMA50 - EMA50(20 bars ago)| > 0.05% of spot

# regime detector plumbing
RV_WINDOW = 20                    # trading days for realised vol
RV_ANNUALISE = 252
SKEW_HISTORY_DAYS = 30            # z-score lookback for 25-delta skew
SKEW_MIN_DAYS = 10                # min history before skew z is trusted
SKEW_Z_STEEP = 1.5                # z >  1.5 -> fear      -> Skew_Score -1
SKEW_Z_FLAT = -1.0                # z < -1.0 -> complacent -> Skew_Score +1
TERM_THRESHOLD = 0.5              # V_fwd - V_spot > 0.5 contango / < -0.5 backwardation
TREND_BARS_REQUIRED = 75          # EMA50 + slope window + ADX warmup
SPREAD_AVG_MIN = 60               # minutes of spread history for baseline
SPREAD_SPAN_MIN = 20              # min span (min) before baseline is trusted
FLOW_MIN_AGE = 600                # reference OI snapshot min age (s)  ~10 min
FLOW_TARGET_AGE = 900             # target age (s)                    ~15 min
FLOW_MAX_AGE = 1800               # max age (s)                       ~30 min
MIN_OI = 50.0                     # quality filter: min OI (lots) / bid > 0
RISK_FREE = 6.5 / 100.0           # risk-free rate for BS fallback greeks
HIST_DAYS_5M = 8                  # calendar days of 5-min candles to fetch

WEIGHTS = {"vol": 0.30, "edge": 0.30, "trend": 0.25, "flow": 0.15}

REGIME_STRONG_SELL, REGIME_MILD_SELL, REGIME_NEUTRAL, REGIME_BUY, REGIME_STRONG_BUY = (
    "STRONG_SELL_VOL", "MILD_SELL_VOL", "NEUTRAL", "BUY_VOL", "STRONG_BUY_VOL")
REGIME_EVENT_HEDGE = "EVENT_HEDGE"

REGIME_ACTION = {
    REGIME_STRONG_SELL: "Deploy max size on Short Straddles / Iron Condors.",
    REGIME_MILD_SELL: "Deploy moderate size; prefer credit spreads over naked straddles.",
    REGIME_NEUTRAL: "Hold current positions; do not initiate new entries.",
    REGIME_BUY: "Reduce short size by 60%; consider long Put hedges.",
    REGIME_STRONG_BUY: "Flatten all short positions; deploy Long Straddles / Strangles.",
    REGIME_EVENT_HEDGE: "MACRO OVERRIDE: flatten shorts, switch to long-gamma or sit flat.",
}

# ----------------------------------------------------------------------------
# 4. POSITION SIZING & RISK
# ----------------------------------------------------------------------------
INITIAL_CAPITAL = 2_000_000.0      # paper starting capital (₹) for % sizing
MAX_RISK_PER_TRADE = 0.02          # 2% of capital
MAX_COMBINED_RISK = 0.04           # 4% of capital
MAX_DAILY_LOSS = -3000             # points (circuit breaker)
TRAIL_START_PROFIT = 2000          # points
TRAIL_RETAIN_PCT = 0.65
SL_BASE_PERCENT = 0.30             # base stop as % of premium
SL_REFERENCE_VIX = 14.0
SL_MIN_PERCENT = 0.18
SL_MAX_PERCENT = 0.40

# ----------------------------------------------------------------------------
# 5. STATIC STOP & PROFIT TARGETS
# ----------------------------------------------------------------------------
STATIC_STOP_PCT = 0.10             # 10% spot stop for short straddle
PROFIT_TARGET_PCT = 0.50           # close at 50% of max credit
IRON_CONDOR_WING_WIDTH = 300       # points
MIN_CREDIT = 100                   # min credit for Iron Condor / Credit Spreads
STRADDLE_DAY_HOLD = 3              # long ATM straddle hold (calendar days)
TIME_EXIT_DAYS_STRADDLE = 21       # exit short straddle 21 days to expiry
TIME_EXIT_DAYS_CONDOR = 7          # exit condor / credit spreads 7 days to expiry
RATIO_TIME_EXIT_DAYS = 14          # exit ratio spreads 14 days to expiry
LONG_STRADDLE_MAX_DEBIT_PCT = 0.025  # max debit <= 2.5% of spot
LONG_STRADDLE_TIME_EXIT_DAY = 3    # 3rd calendar day close at 15:15
BACKSPREAD_MAX_DEBIT = 30.0        # net debit <= 30 points
BACKSPREAD_MIN_WIDTH = 100         # 25d-10d strike width >= 100 pts
BUTTERFLY_MAX_DEBIT = 20.0         # net debit <= 20 pts
BUTTERFLY_MIN_RR = 4.0             # max profit / max loss >= 4:1
HEDGE_REDUCE_PCT = 0.60            # reduce shorts by 60% on BUY_VOL
GAMMA_LIMIT_PCT = 0.50             # gamma exposure trigger (>50% of limit -> F)
SPOT_200_EMA_DAYS = 200            # days for 200-EMA filter
VIX_SMA_FAST = 5                   # 5-period VIX SMA
VIX_SMA_SLOW = 10                  # 10-period VIX SMA

# ----------------------------------------------------------------------------
# 6. ORDER TIMEOUTS (seconds)
# ----------------------------------------------------------------------------
ORDER_FILL_TIMEOUT = 60            # total for all legs (multi-leg)
HEDGE_FILL_TIMEOUT = 30
CORE_FILL_TIMEOUT = 30
SL_FILL_TIMEOUT = 30
PARTIAL_FILL_CANCEL = True         # cancel remaining on partial fill
STAGGER_MS = 200                   # delay between order placements / status polls

# ----------------------------------------------------------------------------
# 7. NSE HOLIDAYS  (reviewed as of 2026-08-30 per spec)
# ----------------------------------------------------------------------------
NSE_MARKET_HOLIDAYS = frozenset({
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
    "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28",
    "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20",
    "2026-11-10", "2026-11-24", "2026-12-25",
})
NSE_SPECIAL_TRADING_DAYS = frozenset({"2026-02-01"})
HOLIDAY_CALENDAR_REVIEWED_ON = date(2026, 8, 30)

# ----------------------------------------------------------------------------
# 8. SESSION TIMING (Asia/Kolkata)
# ----------------------------------------------------------------------------
ENTRY_VIX_TIME = time(9, 15)
STRIKE_SELECT_TIME = time(9, 19)
EXEC_START_TIME = time(9, 19, 30)
EXEC_END_TIME = time(9, 20, 30)
TIME_EXIT_NORMAL = time(15, 15)
TIME_EXIT_EXPIRY = time(15, 0)
TIME_LAST_IGNORE = time(14, 45)    # ignore regime changes after this
EXPIRY_SQUARE_OFF = time(14, 45)   # force square-off on expiry day

# ----------------------------------------------------------------------------
# 9. UPSTOX API & RATE LIMITS
# ----------------------------------------------------------------------------
UPSTOX_BASE_V2 = "https://api.upstox.com/v2"
UPSTOX_BASE_V3 = "https://api.upstox.com/v3"
INSTRUMENT_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
RATE_LIMIT_PER_SEC = 50
RATE_LIMIT_BURST = 10
RETRY_BACKOFF_BASE = 1.0
RETRY_MAX_BACKOFF = 60.0
WS_URL_V3 = "wss://api.upstox.com/v3/feed/market-data-feed"
WS_FEED_STALE_SEC = 30             # kill-switch if feed silent this long

KEY_NIFTY = "NSE_INDEX|Nifty 50"
KEY_VIX = "NSE_INDEX|India VIX"
NIFTY_LOT_SIZE = 65                # fallback; real value read from instrument master

# ----------------------------------------------------------------------------
# 10. MACRO OVERRIDE WINDOW (Step 6)
# ----------------------------------------------------------------------------
EVENT_PRE_HOURS = 6                # 6h before ...
EVENT_POST_HOURS = 2               # ... 2h after a high-impact event

# ----------------------------------------------------------------------------
# 11. DEMO / TEST MODE
# ----------------------------------------------------------------------------
DEMO_CYCLE_SECONDS = 4             # wall-clock sleep between demo cycles
DEMO_VIRTUAL_STEP_MIN = 5          # each demo cycle advances virtual clock 5 min
DEMO_ENTRY_ANYTIME = True          # demo: allow entries outside 09:19:30-09:20:30
DEMO_STRICT_VALIDATION = False     # demo: lenient strategy validations
DEMO_ACCOUNT_CAPITAL = 2_000_000.0

# ----------------------------------------------------------------------------
# 12. TRANSACTION COST ESTIMATOR (paper accounting)
# ----------------------------------------------------------------------------
COST_BROKERAGE_OPTION = 20.0       # ₹ per order (Upstox flat)
COST_STT_OPTION_SELL_PCT = 0.001   # 0.1% of premium on sell side (post Oct-2024)
COST_STT_EXERCISE_PCT = 0.00125
COST_EXCHANGE_PCT = 0.0005         # NSE options transaction charge ~0.05%
COST_SEBI_PCT = 0.000001           # ₹10/crore
COST_STAMP_PCT = 0.00003           # 0.003% on buy side
COST_GST_PCT = 0.18                # 18% GST on (brokerage + exchange + sebi)

# ----------------------------------------------------------------------------
# derived helpers
# ----------------------------------------------------------------------------
DAILY_LOG_MAX_BYTES = 25 * 1024 * 1024   # rotate audit log above this
CHECKPOINT_HEARTBEAT_S = 10              # state checkpoint every 10 s

def is_trading_day(d: date) -> bool:
    """Weekday, not a hardcoded NSE holiday (special trading days allowed)."""
    if d.weekday() >= 5:
        return False
    iso = d.isoformat()
    if iso in NSE_MARKET_HOLIDAYS:
        return False
    return True

def is_expiry_day(d: date) -> bool:
    """Weekly NIFTY expiry = Thursday, unless a holiday shifts it (approx)."""
    return d.weekday() == 3
