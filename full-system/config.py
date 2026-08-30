# ============ FILE: config.py ============
"""
Single source of truth for ALL parameters.
No classes. No functions. Only constants and derived values.
"""

import os
from datetime import date, time

# ─────────────────────────────────────────────────────────────────────
# 2.2 SYSTEM FLAGS
# ─────────────────────────────────────────────────────────────────────
PAPER_TRADING_MODE = True
ALLOW_NON_TRADING_DAY_RUN = False
LOG_LEVEL = "INFO"
CONSOLE_REFRESH_SECONDS = 5

# ─────────────────────────────────────────────────────────────────────
# 2.3 FILE PATHS
# ─────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "data")
STATE_DB = os.path.join(LOG_DIR, "state.db")
TRADE_CSV = os.path.join(LOG_DIR, "trade_analysis.csv")
AUDIT_CSV = os.path.join(LOG_DIR, "audit_log.csv")
TOKEN_FILE = os.path.join(BASE_DIR, "env.txt")
os.makedirs(LOG_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────
# 2.4 CAPITAL & RISK
# ─────────────────────────────────────────────────────────────────────
TOTAL_CAPITAL = 1_000_000
MAX_RISK_PER_TRADE_PCT = 0.02
MAX_COMBINED_RISK_PCT = 0.04
MAX_DAILY_LOSS_PCT = 0.03
MAX_DRAWDOWN_PCT = 0.10
LOT_SIZE = 50
NIFTY_STRIKE_STEP = 50

# Derived values
MAX_RISK_PER_TRADE = int(MAX_RISK_PER_TRADE_PCT * TOTAL_CAPITAL)
MAX_COMBINED_RISK = int(MAX_COMBINED_RISK_PCT * TOTAL_CAPITAL)
MAX_DAILY_LOSS = int(MAX_DAILY_LOSS_PCT * TOTAL_CAPITAL)
MAX_DRAWDOWN = int(MAX_DRAWDOWN_PCT * TOTAL_CAPITAL)

# ─────────────────────────────────────────────────────────────────────
# 2.5 VIX BANDS & DELTA SELECTION
# ─────────────────────────────────────────────────────────────────────
LOW_VIX = 12.0
HIGH_VIX = 18.0
PANIC_VIX = 25.0
EXTREME_VIX = 30.0

LOW_VIX_DELTA = (0.22, 0.28)
MID_VIX_DELTA = (0.20, 0.25)
HIGH_VIX_DELTA = (0.15, 0.20)

MIN_PREMIUM_PCT = 0.003
MAX_PREMIUM_PCT = 0.006

# ─────────────────────────────────────────────────────────────────────
# 2.6 REGIME DETECTION PARAMETERS
# ─────────────────────────────────────────────────────────────────────
REGIME_REFRESH_SECONDS = 300
PERSISTENCE_READINGS = 3

WEIGHT_VOL = 0.30
WEIGHT_EDGE = 0.30
WEIGHT_TREND = 0.25
WEIGHT_FLOW = 0.15
assert abs(WEIGHT_VOL + WEIGHT_EDGE + WEIGHT_TREND + WEIGHT_FLOW - 1.0) < 1e-9, \
    "Score weights must sum to 1.0"

TERM_SPREAD_CONTANGO = 0.5
TERM_SPREAD_BACKWARDATION = -0.5
SKEW_ZSCORE_FEAR = 1.5
SKEW_ZSCORE_COMPLACENT = -1.5
SKEW_LOOKBACK_DAYS = 60

RV_LOOKBACK_DAYS = 20
EDGE_PERCENTILE_HIGH = 70
EDGE_PERCENTILE_LOW = 30
EDGE_LOOKBACK_DAYS = 60

ADX_PERIOD = 14
ADX_TREND_THRESHOLD = 25
ADX_RANGE_THRESHOLD = 22
EMA_PERIOD = 50
EMA_SLOPE_THRESHOLD = 0.0005
ADX_CANDLE_TIMEFRAME = "15minute"
ADX_CANDLE_COUNT = 60

FLOW_WINDOW_MINUTES = 15
SPREAD_LOOKBACK_PERIODS = 12
OTM_STRIKE_OFFSET = 3

STRONG_SELL_THRESHOLD = 0.45
MILD_SELL_THRESHOLD = 0.15
MILD_BUY_THRESHOLD = -0.15
STRONG_BUY_THRESHOLD = -0.45

REGIME_STRONG_SELL = "STRONG_SELL_VOL"
REGIME_MILD_SELL = "MILD_SELL_VOL"
REGIME_NEUTRAL = "NEUTRAL"
REGIME_BUY_VOL = "BUY_VOL"
REGIME_STRONG_BUY = "STRONG_BUY_VOL"
REGIME_EVENT = "EVENT_HEDGE"

# ─────────────────────────────────────────────────────────────────────
# 2.7 STRATEGY PARAMETERS
# ─────────────────────────────────────────────────────────────────────
STRAT_SHORT_STRADDLE = "ATM_STRADDLE_45D"
STRAT_IRON_CONDOR = "WIDE_IRON_CONDOR"
STRAT_CREDIT_SPREADS = "CREDIT_SPREADS_030"
STRAT_RATIO_SPREAD = "RATIO_SPREAD_1X2"
STRAT_BUTTERFLY = "LONG_PUT_BUTTERFLY"
STRAT_DEFENSIVE = "DEFENSIVE_HEDGE"
STRAT_LONG_STRADDLE = "LONG_STRADDLE_3D"
STRAT_BACKSPREAD = "BACKSPREAD_DIRECTIONAL"
STRAT_STRANGLE = "LONG_STRANGLE_EVENT"

STRADDLE_DTE_MIN = 40
STRADDLE_DTE_MAX = 50
STRADDLE_STOP_PCT = 0.10
STRADDLE_TARGET_PCT = 0.50
STRADDLE_EXIT_DTE = 21
STRADDLE_MAX_DEBIT_PCT = 0.025
STRADDLE_POLL_SECONDS = 5

CONDOR_WING_WIDTH = 300
CONDOR_DTE_MIN = 30
CONDOR_DTE_MAX = 45
CONDOR_EXIT_DTE = 7
CONDOR_TARGET_PCT = 0.50
CONDOR_MIN_CREDIT = 100
CONDOR_ADJUSTMENT_DELTA = 0.35
CONDOR_TESTED_SIDE_BUFFER = 150

SPREAD_DELTA_SHORT = 0.30
SPREAD_DELTA_LONG = 0.15
SPREAD_EXIT_DTE = 7
SPREAD_TARGET_PCT = 0.50
SPREAD_MIN_CREDIT = 50
SPREAD_ROLL_DELTA_TRIGGER = 0.35
SPREAD_SKEW_THRESHOLD = 2.0

RATIO_ATM_OFFSET_PTS = 50
RATIO_EXIT_DTE = 14
RATIO_TARGET_PCT = 0.40
RATIO_DELTA_EXIT_TRIGGER = 0.35
RATIO_SKEW_FLAT_THRESHOLD = 0.5
RATIO_CONTANGO_THRESHOLD = 1.5
RATIO_MAX_CAPITAL_PCT = 0.01

BUTTERFLY_DELTA_A = 0.30
BUTTERFLY_DELTA_B = 0.20
BUTTERFLY_DELTA_C = 0.10
BUTTERFLY_MAX_DEBIT_PTS = 20
BUTTERFLY_MIN_RR_RATIO = 4.0
BUTTERFLY_EXIT_DTE = 2
BUTTERFLY_PROFIT_PCT = 0.50
BUTTERFLY_DTE_MAX = 7
BUTTERFLY_WING_BUFFER_PTS = 50

BACKSPREAD_LONG_DELTA = 0.25
BACKSPREAD_SHORT_DELTA = 0.10
BACKSPREAD_LONG_QTY = 3
BACKSPREAD_SHORT_QTY = 1
BACKSPREAD_HEDGE_QTY = 1
BACKSPREAD_MAX_DEBIT_PTS = 30
BACKSPREAD_MIN_MOVE_MULTIPLE = 5.0
BACKSPREAD_DTE_MIN = 7
BACKSPREAD_DTE_MAX = 10
BACKSPREAD_MAX_VIX = 30
BACKSPREAD_STOP_MOVE_PCT = 0.015
BACKSPREAD_EXIT_DTE = 2
BACKSPREAD_PROFIT_MULTIPLE = 10.0
BACKSPREAD_MIN_STRIKE_WIDTH = 100

LONG_STRADDLE_DTE_MIN = 25
LONG_STRADDLE_DTE_MAX = 40
LONG_STRADDLE_STOP_PCT = 0.50
LONG_STRADDLE_TARGET_PCT = 0.50
LONG_STRADDLE_HOLD_DAYS = 3
LONG_STRADDLE_MAX_DEBIT_PCT = 0.025
LONG_STRADDLE_VIX_SMA_PERIOD = 10
LONG_STRADDLE_VIX_SPIKE_PCT = 0.20
LONG_STRADDLE_MAX_IV_RANK = 80

DEFENSIVE_REDUCTION_PCT = 0.60
DEFENSIVE_REMAINING_PCT = 0.40
DEFENSIVE_VIX_SPIKE_PCT = 0.15
DEFENSIVE_VIX_SMA_PERIOD = 5
DEFENSIVE_MAX_HOLD_DAYS = 3
DEFENSIVE_PORTFOLIO_STOP_PCT = 0.02
DEFENSIVE_EMA_PERIOD = 20

EVENT_STRANGLE_DELTA = 0.30
EVENT_STRANGLE_STOP_PCT = 0.50
EVENT_STRANGLE_TARGET_PCT = 1.00
EVENT_STRANGLE_MAX_SPREAD_PTS = 3
EVENT_HOLD = "EVENT_PLUS_1_DAY"
EVENT_WINDOW_BEFORE_HOURS = 6
EVENT_WINDOW_AFTER_HOURS = 2

# ─────────────────────────────────────────────────────────────────────
# 2.8 STOP LOSS PARAMETERS
# ─────────────────────────────────────────────────────────────────────
STATIC_STOP_PCT = 0.10
PROFIT_TARGET_PCT = 0.50
SL_BASE_PERCENT = 0.30
SL_REFERENCE_VIX = 14.0
SL_MIN_PERCENT = 0.18
SL_MAX_PERCENT = 0.40
TRAIL_START_PROFIT_PTS = 2000
TRAIL_RETAIN_PCT = 0.65
# SL_PCT = SL_BASE * (SL_REFERENCE_VIX / current_vix)
# SL_PCT = max(SL_MIN_PERCENT, min(SL_MAX_PERCENT, SL_PCT))

# ─────────────────────────────────────────────────────────────────────
# 2.9 ORDER EXECUTION PARAMETERS
# ─────────────────────────────────────────────────────────────────────
ORDER_FILL_TIMEOUT_SEC = 60
HEDGE_FILL_TIMEOUT_SEC = 30
CORE_FILL_TIMEOUT_SEC = 30
SL_FILL_TIMEOUT_SEC = 30
ORDER_POLL_INTERVAL_SEC = 1
ORDER_AGGRESSION_TICKS = 1
TICK_SIZE = 0.05
PARTIAL_FILL_CANCEL = True
PARTIAL_FILL_CANCEL_HEDGE = False
PAPER_SLIPPAGE_SHORT_TICKS = 2
PAPER_SLIPPAGE_HEDGE_TICKS = 5
ORDER_BETWEEN_LEGS_DELAY_SEC = 0.2
ORDER_STATUS_POLL_DELAY_SEC = 0.2
MAX_SPREAD_ATM_PTS = 3
MAX_SPREAD_OTM_PTS = 5
MIN_OI_LOTS = 50

# ─────────────────────────────────────────────────────────────────────
# 2.10 SESSION TIMING
# ─────────────────────────────────────────────────────────────────────
TZ = "Asia/Kolkata"
ENTRY_VIX_TIME = time(9, 15)
STRIKE_SELECT_TIME = time(9, 19)
EXEC_START_TIME = time(9, 19, 30)
EXEC_END_TIME = time(9, 25, 0)
REGIME_FREEZE_TIME = time(14, 45)
TIME_EXIT_NORMAL = time(15, 15)
TIME_EXIT_EXPIRY = time(14, 45)
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
HEARTBEAT_INTERVAL_SEC = 10
EOD_RECONCILE_TIME = time(15, 30)

# ─────────────────────────────────────────────────────────────────────
# 2.11 UPSTOX API CONFIGURATION
# ─────────────────────────────────────────────────────────────────────
UPSTOX_BASE_V2 = "https://api.upstox.com/v2"
UPSTOX_BASE_V3 = "https://api.upstox.com/v3"
RATE_LIMIT_CAPACITY = 50
RATE_LIMIT_REFILL_PER_SEC = 50
RATE_LIMIT_BURST = 10
RETRY_BACKOFF_BASE = 1.0
RETRY_MAX_BACKOFF = 60.0
RETRY_MAX_ATTEMPTS = 5
WS_RECONNECT_ATTEMPTS = 3
WS_RECONNECT_DELAY_SEC = 5
WS_DOWNTIME_KILL_SWITCH_SEC = 30
NTP_MAX_OFFSET_SEC = 0.5
NTP_SERVER = "pool.ntp.org"

EP_MARGIN = "/charges/margin"
EP_POSITIONS = "/portfolio/short-term-positions"
EP_ORDER_PLACE = "/order/place"
EP_ORDER_MODIFY = "/order/modify"
EP_ORDER_CANCEL = "/order/cancel"
EP_ORDER_HISTORY = "/order/history"
EP_ORDER_DETAILS = "/order/details"
EP_OPTION_CHAIN = "/option/chain"
EP_LTP = "/market-quote/ltp"
EP_GREEKS = "/market-quote/option-greek"
EP_CANDLE = "/historical-candle"
EP_PROFILE = "/user/profile"

INSTRUMENT_NIFTY = "NSE_INDEX|Nifty 50"
INSTRUMENT_VIX = "NSE_INDEX|India VIX"
INSTRUMENT_NIFTY_FUT = "NSE_FO|NIFTY"

WS_URL_V3 = "wss://api.upstox.com/v3/feed/market-data-feed"
WS_MODE_LTPC = "ltpc"
WS_MODE_FULL = "full"

# ─────────────────────────────────────────────────────────────────────
# 2.12 CIRCUIT BREAKER LEVELS
# ─────────────────────────────────────────────────────────────────────
CB_LEVEL_1_PCT = 0.02
CB_LEVEL_2_PCT = 0.03
CB_LEVEL_3_PCT = 0.06
CB_LEVEL_4_PCT = 0.10
CB_LEVEL_5_IV_SPIKE_PCT = 0.30

CB_LEVEL_1_ACTION = "CLOSE_POSITION"
CB_LEVEL_2_ACTION = "HALT_NEW_TRADES"
CB_LEVEL_3_ACTION = "REDUCE_50PCT"
CB_LEVEL_4_ACTION = "FULL_STOP_MANUAL_REVIEW"
CB_LEVEL_5_ACTION = "FORCE_STRONG_BUY_REGIME"

# ─────────────────────────────────────────────────────────────────────
# 2.13 GREEKS LIMITS PER REGIME
# ─────────────────────────────────────────────────────────────────────
GREEKS_LIMITS = {
    "STRONG_SELL_VOL": {
        "delta_max": 0.10, "delta_min": -0.10,
        "gamma_max": -0.002, "gamma_min": -0.010,
        "vega_max": -2000, "vega_min": -8000,
        "theta_min": 1500
    },
    "MILD_SELL_VOL": {
        "delta_max": 0.20, "delta_min": -0.20,
        "gamma_max": -0.001, "gamma_min": -0.006,
        "vega_max": -1000, "vega_min": -4000,
        "theta_min": 800
    },
    "NEUTRAL": {
        "delta_max": 0.05, "delta_min": -0.05,
        "gamma_max": 0.001, "gamma_min": -0.001,
        "vega_max": 500, "vega_min": -500,
        "theta_min": 0
    },
    "BUY_VOL": {
        "delta_max": 0.00, "delta_min": -0.50,
        "gamma_max": 0.008, "gamma_min": 0.001,
        "vega_max": 5000, "vega_min": 1000,
        "theta_min": None
    },
    "STRONG_BUY_VOL": {
        "delta_max": 0.50, "delta_min": -0.50,
        "gamma_max": 0.015, "gamma_min": 0.005,
        "vega_max": 10000, "vega_min": 3000,
        "theta_min": None
    },
    "EVENT_HEDGE": {
        "delta_max": 0.10, "delta_min": -0.10,
        "gamma_max": 0.010, "gamma_min": 0.003,
        "vega_max": 6000, "vega_min": 2000,
        "theta_min": None
    }
}

# ─────────────────────────────────────────────────────────────────────
# 2.14 CAPITAL ALLOCATION PER REGIME
# ─────────────────────────────────────────────────────────────────────
REGIME_CAPITAL_PCT = {
    "STRONG_SELL_VOL": 0.30,
    "MILD_SELL_VOL":   0.20,
    "NEUTRAL":         0.00,
    "BUY_VOL":         0.10,
    "STRONG_BUY_VOL":  0.15,
    "EVENT_HEDGE":     0.05
}

REGIME_MAX_LOTS = {
    "STRONG_SELL_VOL": 6,
    "MILD_SELL_VOL":   4,
    "NEUTRAL":         0,
    "BUY_VOL":         2,
    "STRONG_BUY_VOL":  3,
    "EVENT_HEDGE":     1
}

# ─────────────────────────────────────────────────────────────────────
# 2.15 NSE HOLIDAYS & EVENTS
# ─────────────────────────────────────────────────────────────────────
NSE_MARKET_HOLIDAYS = frozenset({
    "2026-01-15", "2026-01-26", "2026-03-03",
    "2026-03-26", "2026-03-31", "2026-04-03",
    "2026-04-14", "2026-05-01", "2026-05-28",
    "2026-06-26", "2026-09-14", "2026-10-02",
    "2026-10-20", "2026-11-10", "2026-11-24",
    "2026-12-25"
})

NSE_SPECIAL_TRADING_DAYS = frozenset({"2026-02-01"})

HIGH_IMPACT_EVENTS = {
    "2026-02-01": "UNION_BUDGET",
    "2026-04-03": "RBI_POLICY",
    "2026-06-05": "RBI_POLICY",
    "2026-08-07": "RBI_POLICY",
    "2026-10-02": "RBI_POLICY",
    "2026-12-04": "RBI_POLICY"
}

HOLIDAY_CALENDAR_REVIEWED_ON = date(2026, 8, 30)

# ─────────────────────────────────────────────────────────────────────
# 2.16 TRADE CSV COLUMNS
# ─────────────────────────────────────────────────────────────────────
TRADE_CSV_COLUMNS = [
    "trade_id", "strategy_name",
    "regime_at_entry", "regime_at_exit",
    "entry_timestamp", "exit_timestamp",
    "holding_days", "entry_spot", "exit_spot",
    "entry_vix", "exit_vix", "legs_summary",
    "total_credit_received", "total_debit_paid",
    "net_premium", "max_risk", "realized_pnl",
    "realized_pnl_percent", "exit_reason",
    "slippage_total_points", "transaction_costs",
    "composite_score_at_entry", "vol_score",
    "edge_score", "trend_score", "flow_score",
    "days_to_expiry_at_entry", "expiry_date",
    "paper_trade"
]

EXIT_REASONS = {
    "PROFIT_TARGET": "PROFIT_TARGET",
    "STOP_LOSS":     "STOP_LOSS",
    "TIME_EXIT":     "TIME_EXIT",
    "REGIME_CHANGE": "REGIME_CHANGE",
    "CIRCUIT_BREAK": "CIRCUIT_BREAK",
    "MANUAL":        "MANUAL",
    "EOD":           "EOD",
    "EXPIRY":        "EXPIRY"
}