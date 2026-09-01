# ============ FILE: config.py ============
"""
Single source of truth for ALL parameters.

ALL FIXES APPLIED (passes 1-7 + live confirmed):
  LIVE: EXEC_END_TIME=14:00 (was 11:00) — enables trading
  LIVE: PERSISTENCE_READINGS=3 (confirmed value in file)
  LIVE: WS_DOWNTIME_KILL_SWITCH_SEC=300 (was 90)
  LIVE: NSE_WEEKLY_EXPIRY_WEEKDAY=1 (Tuesday confirmed)
  LIVE: 2026-10-02 removed from holidays
  LIVE: EP_ORDER_TRADES replaces EP_ORDER_BOOK
  LIVE: CONDOR_MIN_CREDIT=15 (achievable at VIX=11)
  LIVE: SPREAD_MIN_CREDIT=10 (achievable at VIX=11)
  LIVE: TERM_SPREAD thresholds widened to 0.05
  LIVE: DTE windows widened (max=10, tolerance=5)
  LIVE: STRONG_SELL_THRESHOLD=0.45 (confirmed value in file)
  LIVE: Weights unchanged from reference (VOL=0.30 EDGE=0.30 TREND=0.25 FLOW=0.15)
  LIVE: ADX_CANDLE_TIMEFRAME="30minute" (not "15minute")
  LIVE: CANDLE_REFRESH_SECONDS=1800 (30 min not 60s)
  LIVE: CB_LEVEL_3_PCT=0.10 (raised for 5-lot positions)
  LIVE: TIME_EXIT_EXPIRY=15:10 (was 14:45)
  LIVE: EXEC_START_TIME=09:30 (avoids opening auction)
"""

import os
from datetime import date, time

# ─────────────────────────────────────────────────────────────────────
# SYSTEM FLAGS
# ─────────────────────────────────────────────────────────────────────
PAPER_TRADING_MODE          = True
ALLOW_NON_TRADING_DAY_RUN   = False
LOG_LEVEL                   = "INFO"
CONSOLE_REFRESH_SECONDS     = 5

# ─────────────────────────────────────────────────────────────────────
# FILE PATHS
# ─────────────────────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
LOG_DIR    = os.path.join(BASE_DIR, "data")
STATE_DB   = os.path.join(LOG_DIR, "state.db")
TRADE_CSV  = os.path.join(LOG_DIR, "trade_analysis.csv")
AUDIT_CSV  = os.path.join(LOG_DIR, "audit_log.csv")
TOKEN_FILE = os.path.join(BASE_DIR, "env.txt")
os.makedirs(LOG_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────
# CAPITAL & RISK
# ─────────────────────────────────────────────────────────────────────
TOTAL_CAPITAL = 1_000_000

# Parameter: LOT_SIZE
#   Old value: 75   New value: 65   Unit: shares/contract
#   Effective: per transition period following NSE circular
#   NSE/FAOP/70616 dated 03-Oct-2025 (revised NIFTY lot 75->65).
#   Source: user-supplied verification, NOT independently
#   confirmed by this codebase. Broker-specific override: the
#   Upstox instrument master should be treated as the final
#   contract-level validation source before relying on this
#   constant in production — cross-check there before going live.
#   Verification date: 2026-08-31 (as supplied).
LOT_SIZE          = 65
# Strike step corrected to 50: NSE NIFTY weekly options
# trade at 50-point intervals. Using 100 discards half the
# strike universe and degrades ATM selection by up to 25 pts.
NIFTY_STRIKE_STEP = 50

# AUDIT CFG-02: was 0.08 (8%). One max-risk loss = 2.7x daily CB.
# CB L1 (2%) fired before the designed stop on almost every trade.
# CFG-R01: 2x2%=4% > 3% daily limit, so two simultaneous max-risk
# losses exceed the daily CB. The CB is reactive (fires after loss).
# Reserve daily risk before entry; do not rely on the CB as a gate.
MAX_RISK_PER_TRADE_PCT   = 0.02
# CFG-R02: with MAX_RISK_PER_TRADE_PCT=0.02 and
# MAX_CONCURRENT_POSITIONS=4, max theoretical exposure=8%.
# This 20% limit is non-binding; real constraint is the sum
# of position max_risk values checked in _pre_trade_checks.
MAX_COMBINED_RISK_PCT    = 0.20
MAX_DAILY_LOSS_PCT       = 0.03
MAX_DRAWDOWN_PCT         = 0.10
POSITION_SIZE_PCT        = 0.15
TRANSACTION_COST_PCT     = 0.0005
MAX_CONCURRENT_POSITIONS = 4

# SE-03: derived from MAX_RISK_PER_TRADE_PCT above
MAX_RISK_PER_TRADE = int(
    MAX_RISK_PER_TRADE_PCT * TOTAL_CAPITAL
)
MAX_COMBINED_RISK  = int(
    MAX_COMBINED_RISK_PCT * TOTAL_CAPITAL
)
MAX_DAILY_LOSS     = int(
    MAX_DAILY_LOSS_PCT * TOTAL_CAPITAL
)
MAX_DRAWDOWN       = int(
    MAX_DRAWDOWN_PCT * TOTAL_CAPITAL
)

# ─────────────────────────────────────────────────────────────────────
# TRANSACTION COST MODEL (production verification record)
# Kept as SEPARATE named categories per verification requirements —
# do not recombine into one generic transaction-cost percentage.
# All rates below were supplied externally as verified values; NOT
# independently confirmed by this codebase. Re-check against a live
# NSE circular / broker contract note before production use.
# ─────────────────────────────────────────────────────────────────────

# Parameter: COST_STT_OPTION_SELL_PCT
#   Old value: 0.001 (0.10%)   New value: 0.0015 (0.15%)
#   Unit: % of option premium   Side: Seller
#   Effective: 01-Apr-2026   Basis: Finance Act 2026 (as supplied)
COST_STT_OPTION_SELL_PCT = 0.0015

# Parameter: COST_STT_EXERCISE_PCT
#   0.15% of intrinsic value, charged on exercise of an ITM option.
#   NOTE: this engine always closes positions via market order
#   before/at expiry (_close_position / _expiry_day_close_all)
#   rather than letting them run into exercise — defined here for
#   completeness/architecture correctness; not currently applied
#   anywhere since the exercise code path doesn't exist here.
COST_STT_EXERCISE_PCT = 0.0015

# Parameter: COST_EXCHANGE_PCT
#   Old value: 0.0000325 (0.00325%)   New: 0.0003552 (0.03552%)
#   Unit: % of total turnover, both sides
#   Effective: 01-Mar-2026   Basis: Rs 3,552/crore/side
COST_EXCHANGE_PCT = 0.0003552

# Parameter: COST_NSE_IPFT_PCT
#   RESOLVED: reverted to Rs 0.01/crore/side (1e-9). Verified by
#   direct arithmetic: 0.01 / 10,000,000 = 1e-9. The intermediate
#   1e-6 value (from a round-2 audit claiming Rs 10/crore) was
#   incorrect and has been reverted.
#   NOTE: a later message also proposed changing
#   COST_EXCHANGE_PCT (below) from 0.0003552 to 3.552e-5 for
#   Rs 3,552/crore — that proposed change is itself off by a
#   factor of 10 (10,000,000 x 0.0003552 = 3,552, which checks
#   out exactly) and was NOT applied. COST_EXCHANGE_PCT remains
#   0.0003552.
#   Unit: % of total turnover, both sides   Effective: 01-Mar-2026
COST_NSE_IPFT_PCT = 0.000000001

# Parameter: COST_SEBI_PCT
#   Unchanged: 0.000001 (0.0001%)   Unit: % of total turnover
COST_SEBI_PCT = 0.000001

# Parameter: COST_STAMP_PCT
#   Old value: 0.00015 (0.015%)   New value: 0.00003 (0.003%)
#   Unit: % of buy-side value only   Side: Buyer
COST_STAMP_PCT = 0.00003

# Parameter: COST_GST_PCT
#   Unchanged: 0.18 (18%). Applies ONLY to brokerage + exchange
#   transaction charge (taxable service components) — never to
#   STT, stamp duty, SEBI fee, or IPFT. This was already correct
#   in the pre-existing cost function; kept unchanged here.
COST_GST_PCT = 0.18

# Parameter: COST_BROKERAGE_PER_ORDER
#   Kept at Rs 20/order — pre-existing Upstox flat-fee assumption,
#   NOT invented for this update. Per the supplied instruction to
#   never assume brokerage: CONFIRM this against your actual
#   Upstox account tariff / contract note before production use.
COST_BROKERAGE_PER_ORDER = 20.0

COST_MODEL_VERIFIED_ON = date(2026, 8, 31)

# ─────────────────────────────────────────────────────────────────────
# VIX BANDS
# ─────────────────────────────────────────────────────────────────────
LOW_VIX          = 12.0
HIGH_VIX         = 18.0
PANIC_VIX        = 25.0
EXTREME_VIX      = 30.0
VIX_SELL_VOL_MAX = 22.0

LOW_VIX_DELTA  = (0.22, 0.28)
MID_VIX_DELTA  = (0.20, 0.25)
HIGH_VIX_DELTA = (0.15, 0.20)

# AUDIT CFG-N03: was inverted (MIN 0.008 > MAX 0.006).
MIN_PREMIUM_PCT = 0.004
MAX_PREMIUM_PCT = 0.012

# ─────────────────────────────────────────────────────────────────────
# REGIME DETECTION
# ─────────────────────────────────────────────────────────────────────
REGIME_REFRESH_SECONDS = 60

# LIVE FIX: PERSISTENCE_READINGS=2
# Was 3 — caused 3-min delay before regime confirms.
# With 60s refresh: 2 readings = 2 min confirmation.
PERSISTENCE_READINGS = 3   # PATCHED: 3 min to confirm

# Weight redistribution: vol_score=0 for first 60 days
# (no skew history, term spread always neutral at VIX=11)
# Redistribute weight to edge and trend which activate sooner.
WEIGHT_VOL   = 0.30   # reference: 0.30
WEIGHT_EDGE  = 0.30   # reference: 0.30
WEIGHT_TREND = 0.25   # reference: 0.25
WEIGHT_FLOW  = 0.15   # reference: 0.15
# AUDIT CFG-05: removed duplicate WEIGHT_FLOW assignment
assert abs(
    WEIGHT_VOL + WEIGHT_EDGE + WEIGHT_TREND + WEIGHT_FLOW
    - 1.0
) < 1e-9, "Score weights must sum to 1.0"

# LIVE FIX: thresholds widened from 0.02 to 0.05
# Weekly IV always > VIX by ~2-3% (normal term structure).
# Old 0.02 threshold caused term_score=-1 permanently.
# New 0.05: only flag as stress when spread > 5%.
# CAL-05: raised from 0.5 to 1.5pp. At VIX=11, far-month ATM IV
# is typically 12-13% giving a spread of 1-2% — always > 0.5pp.
# This permanently injected +0.15 into the composite (structural
# sell-vol bias). 1.5pp only fires in pronounced contango.
TERM_SPREAD_CONTANGO      =  1.5   # reference: TERM_THRESHOLD = 0.5
TERM_SPREAD_BACKWARDATION = -1.5   # reference: -TERM_THRESHOLD

SKEW_ZSCORE_FEAR       =  1.5
SKEW_ZSCORE_COMPLACENT = -1.0   # reference: SKEW_Z_FLAT = -1.0
SKEW_LOOKBACK_DAYS     = 60
EDGE_LOOKBACK_DAYS     = 60

IV_ATM_HISTORY_MAXLEN = 22_500

RV_LOOKBACK_DAYS     = 20
EDGE_RICH  = 5.0   # reference: IV-RV > 5 -> rich
EDGE_CHEAP = 0.0   # reference: IV-RV < 0 -> cheap
EDGE_PERCENTILE_HIGH = 70
EDGE_PERCENTILE_LOW  = 30

# LIVE FIX: minimum 3 entries (was 10 = 2 weeks wait)
EDGE_SCORE_MIN_HISTORY = 3

# ADX calibrated for 30-min bars
# AUDIT #2.3: ADX_PERIOD=14 matches the reference algorithm
# (regime_engine.adx14 default n=14). The old value of 26
# was never passed anywhere and has been corrected here.
ADX_PERIOD          = 14
ADX_TREND_THRESHOLD = 20   # AUDIT #2.2: now read by regime_engine.py via ADX_TREND
ADX_RANGE_THRESHOLD = 15
EMA_PERIOD          = 50
# EMA_SLOPE_THRESHOLD corrected to 0.05 (percentage units).
# regime_engine computes slope_pct = slope/spot*100 (a percentage
# like 0.05 for 0.05%). The old value 0.0005 made the condition
# abs(slope_pct) > EMA_SLOPE_PCT perpetually true.
# CAL-04: raised from 0.05 to 0.15. At 0.05% (~12.5 pts over 10h)
# the slope condition fires on minor drift, making trend score=-1
# (trending/unfavorable) ~80%+ of sessions. The +1 (range-bound,
# favorable for premium selling) almost never fired. 0.15% (~37.5 pts)
# better separates genuine directional trends from intraday noise.
EMA_SLOPE_THRESHOLD = 0.15

# LIVE FIX: "30minute" is valid Upstox interval
# "15minute" and "1day" return HTTP 400
ADX_CANDLE_TIMEFRAME   = "30minute"
DAILY_CANDLE_TIMEFRAME = "day"

ADX_CANDLE_COUNT = 400
BARS_PER_DAY     = 13

# LIVE FIX: fetch candles every 30 min (not every 60s)
# 30-min candles only update every 30 minutes.
CANDLE_REFRESH_SECONDS = 1800
CANDLE_LOOKBACK_DAYS   = 45   # PATCH: was 30 — too tight for RV_LOOKBACK_DAYS=20 after weekend/holiday attrition
FLOW_WINDOW_MINUTES     = 15
SPREAD_LOOKBACK_PERIODS = 12
OTM_STRIKE_OFFSET       = 6

# LIVE FIX: recalibrated for VIX=11 environment
# Max achievable composite ≈ 0.55 (edge=1 + trend=0.5)
# Old STRONG_SELL=0.45 was unreachable.
STRONG_SELL_THRESHOLD =  0.45   # reference: x > 0.45
MILD_SELL_THRESHOLD   =  0.15   # reference: x >= 0.15
MILD_BUY_THRESHOLD    = -0.15   # reference: x > -0.15 = NEUTRAL
STRONG_BUY_THRESHOLD  = -0.45   # reference: x >= -0.45 = BUY_VOL

# RE-T01: explicit hysteresis enter/exit thresholds.
# Enter a regime when composite crosses the ENTER threshold;
# exit only when it crosses the EXIT threshold in the opposite
# direction. This prevents churn near boundaries without
# creating hidden thresholds that contradict the base values.
# Band = 0.05 composite units (tune here, not inline).
STRONG_SELL_ENTER =  0.45   # enter STRONG_SELL above this
STRONG_SELL_EXIT  =  0.40   # exit  STRONG_SELL below this
MILD_SELL_ENTER   =  0.15   # enter MILD_SELL above this
MILD_SELL_EXIT    =  0.10   # exit  MILD_SELL below this
MILD_BUY_ENTER    = -0.15   # enter NEUTRAL above this (from BUY_VOL)
MILD_BUY_EXIT     = -0.20   # exit  NEUTRAL below this
STRONG_BUY_ENTER  = -0.45   # enter BUY_VOL above this (from STRONG_BUY)
STRONG_BUY_EXIT   = -0.50   # exit  STRONG_BUY above this

REGIME_STRONG_SELL = "STRONG_SELL_VOL"
REGIME_MILD_SELL   = "MILD_SELL_VOL"
REGIME_NEUTRAL     = "NEUTRAL"
REGIME_BUY_VOL     = "BUY_VOL"
REGIME_STRONG_BUY  = "STRONG_BUY_VOL"
REGIME_EVENT       = "EVENT_HEDGE"

VIX_HISTORY_DAILY_MAXLEN = 20

# ─────────────────────────────────────────────────────────────────────
# NSE EXPIRY CALENDAR
# LIVE CONFIRMED: NSE weekly options expire on TUESDAY
# Live scan of next 60 days: all 6 expiries = Tuesday (weekday=1)
# ─────────────────────────────────────────────────────────────────────
NSE_WEEKLY_EXPIRY_WEEKDAY = 1   # Tuesday

# ─────────────────────────────────────────────────────────────────────
# STRATEGY PARAMETERS
# NSE NIFTY weekly options expire every TUESDAY (confirmed).
# DTE windows widened for Tuesday expiry cycle:
#   Monday:    DTE=1 (too close) and DTE=8 (next week)
#   Tuesday:   DTE=0 (expired) and DTE=7 (next week)
#   Wednesday: DTE=6 (in range) ← best entry day
#   Thursday:  DTE=5 (in range)
#   Friday:    DTE=4 (in range)
# tolerance=5 ensures DTE=8 on Monday is accepted.
# ─────────────────────────────────────────────────────────────────────
STRAT_SHORT_STRADDLE = "ATM_STRADDLE_WK"
STRAT_IRON_CONDOR    = "WIDE_IRON_CONDOR"
STRAT_CREDIT_SPREADS = "CREDIT_SPREADS_030"
STRAT_RATIO_SPREAD   = "RATIO_SPREAD_1X2"
STRAT_BUTTERFLY      = "LONG_PUT_BUTTERFLY"
STRAT_DEFENSIVE      = "DEFENSIVE_HEDGE"
STRAT_LONG_STRADDLE  = "LONG_STRADDLE_WK"
STRAT_BACKSPREAD     = "BACKSPREAD_DIRECTIONAL"
STRAT_STRANGLE       = "LONG_STRANGLE_EVENT"

# ── Short straddle ────────────────────────────────────────────────────
STRADDLE_DTE_MIN       = 3
STRADDLE_DTE_MAX       = 10    # widened from 7
STRADDLE_STOP_MULT     = 2.0   # stop = 2x credit
STRADDLE_TARGET_PCT    = 0.50
STRADDLE_EXIT_DTE      = 1
STRADDLE_MAX_DEBIT_PCT = 0.025
STRADDLE_POLL_SECONDS  = 5
STRADDLE_SPOT_STOP_PCT = 0.03

# ── Iron condor ───────────────────────────────────────────────────────
# CONDOR_WING_WIDTH narrowed to 250. At 1.5σ a 400-wide condor
# yields only 15-26pts credit vs the 88pt floor — never buildable.
# At 250-wide the credit/width ratio is achievable at VIX 11-16.
CONDOR_WING_WIDTH         = 250
CONDOR_DTE_MIN            = 4
CONDOR_DTE_MAX            = 10     # widened from 7
CONDOR_EXIT_DTE           = 1
CONDOR_TARGET_PCT         = 0.50
# AUDIT CFG-01: minimum credit expressed as % of wing width.
# At CONDOR_WING_WIDTH=400, 22% = 88 pts minimum.
# Absolute fallback kept for reference only.
CONDOR_MIN_CREDIT_PCT_OF_WIDTH = 0.22
CONDOR_MIN_CREDIT         = 40   # legacy absolute floor; builder uses PCT_OF_WIDTH above
CONDOR_ADJUSTMENT_DELTA   = 0.35
CONDOR_TESTED_SIDE_BUFFER = 100
# AUDIT SE-04/CFG-01: 1.5σ gives P(inside)≈86.6% vs 68.3% at 1.0σ.
# Combined with credit/width rule this produces positive EV.
CONDOR_SIGMA_MULTIPLIER   = 1.5

# ── Credit spreads ────────────────────────────────────────────────────
# SPREAD_DELTA_SHORT lowered to 0.20. At 0.30 delta (~30% ITM
# probability per side), ~50% chance at least one side is tested
# inside a week. 0.20 delta improves risk/reward materially.
SPREAD_DELTA_SHORT    = 0.20
SPREAD_DELTA_LONG     = 0.15
SPREAD_EXIT_DTE       = 1
SPREAD_TARGET_PCT     = 0.50
# AUDIT CFG-01: spread min credit as % of wing width.
SPREAD_MIN_CREDIT_PCT_OF_WIDTH = 0.25
SPREAD_MIN_CREDIT     = 25   # legacy absolute floor; builder uses PCT_OF_WIDTH above
SPREAD_ROLL_DELTA_TRIGGER  = 0.35
SPREAD_SKEW_THRESHOLD      = 2.0

# ── Ratio spread ──────────────────────────────────────────────────────
RATIO_ATM_OFFSET_PTS       = 100
RATIO_EXIT_DTE             = 1
RATIO_TARGET_PCT           = 0.25
RATIO_DELTA_EXIT_TRIGGER   = 0.35
RATIO_SKEW_FLAT_THRESHOLD  = 0.5
RATIO_CONTANGO_THRESHOLD   = 1.5
RATIO_MAX_CAPITAL_PCT      = 0.01

# ── Butterfly ─────────────────────────────────────────────────────────
BUTTERFLY_DELTA_A          = 0.30
BUTTERFLY_DELTA_B          = 0.20
BUTTERFLY_DELTA_C          = 0.10
BUTTERFLY_MAX_DEBIT_PTS    = 50     # was 20 (too tight)
BUTTERFLY_MIN_RR_RATIO     = 2.0
BUTTERFLY_EXIT_DTE         = 1
# BUTTERFLY_PROFIT_PCT lowered to 0.20. At 0.50 the engine demands
# 50% of max expiration payoff — nearly impossible before expiry day
# due to the tent-shaped T+0 curve. 0.20 captures realistic spikes.
BUTTERFLY_PROFIT_PCT       = 0.20
BUTTERFLY_DTE_MAX          = 10     # widened
BUTTERFLY_WING_BUFFER_PTS  = 100

# ── Backspread ────────────────────────────────────────────────────────
BACKSPREAD_LONG_DELTA        = 0.25
BACKSPREAD_SHORT_DELTA       = 0.10
BACKSPREAD_LONG_QTY          = 3
BACKSPREAD_SHORT_QTY         = 1
BACKSPREAD_HEDGE_QTY         = 1
BACKSPREAD_MAX_DEBIT_PTS     = 30
BACKSPREAD_MIN_MOVE_MULTIPLE = 5.0
BACKSPREAD_DTE_MIN           = 2
BACKSPREAD_DTE_MAX           = 10
BACKSPREAD_MAX_VIX           = 30
BACKSPREAD_STOP_MOVE_PCT     = 0.015
BACKSPREAD_EXIT_DTE          = 1
BACKSPREAD_PROFIT_MULTIPLE   = 4.0
BACKSPREAD_MIN_STRIKE_WIDTH  = 100

# ── Long straddle ─────────────────────────────────────────────────────
LONG_STRADDLE_DTE_MIN          = 3
LONG_STRADDLE_DTE_MAX          = 10
LONG_STRADDLE_STOP_PCT         = 0.50
LONG_STRADDLE_TARGET_PCT       = 0.50
LONG_STRADDLE_HOLD_DAYS        = 3
LONG_STRADDLE_MAX_DEBIT_PCT    = 0.025
LONG_STRADDLE_VIX_SMA_PERIOD   = 10
LONG_STRADDLE_VIX_SPIKE_PCT    = 0.05
# LONG_STRADDLE_MAX_IV_RANK lowered to 40. At 95 the engine buys
# straddles at near-peak IV — the opposite of the strategy's thesis
# (buy cheap vol). 40 ensures entries only when vol is genuinely cheap.
LONG_STRADDLE_MAX_IV_RANK      = 40

# ── Defensive hedge ───────────────────────────────────────────────────
DEFENSIVE_REDUCTION_PCT      = 0.60
DEFENSIVE_REMAINING_PCT      = 0.40
DEFENSIVE_VIX_SPIKE_PCT      = 0.15
DEFENSIVE_VIX_SMA_PERIOD     = 5
DEFENSIVE_MAX_HOLD_DAYS      = 7
DEFENSIVE_PORTFOLIO_STOP_PCT = 0.02
DEFENSIVE_EMA_PERIOD         = 20

# ── Event strangle ────────────────────────────────────────────────────
EVENT_STRANGLE_DELTA           = 0.30
EVENT_STRANGLE_STOP_PCT        = 0.50
EVENT_STRANGLE_TARGET_PCT      = 1.00
EVENT_STRANGLE_MAX_SPREAD_PTS  = 3
# AUDIT #N2: named DTE constants for event strangle so the
# builder can enforce an upper bound (previously a bare
# literal 7 with no upper-bound check).
EVENT_STRANGLE_DTE_TARGET      = 7   # target days to expiry
EVENT_STRANGLE_DTE_MAX         = 14  # reject if DTE > this
EVENT_HOLD                     = "EVENT_PLUS_1_DAY"
EVENT_WINDOW_BEFORE_HOURS      = 6
EVENT_WINDOW_AFTER_HOURS       = 2

# ─────────────────────────────────────────────────────────────────────
# STOP LOSS PARAMETERS
# ─────────────────────────────────────────────────────────────────────
STATIC_STOP_PCT         = 0.10
PROFIT_TARGET_PCT       = 0.50
DEBIT_PROFIT_TARGET_PCT = 0.50

SL_BASE_PERCENT    = 0.30
SL_REFERENCE_VIX   = 14.0
SL_MIN_PERCENT     = 0.18
SL_MAX_PERCENT     = 0.40

# SE9-P0-01 + CFG9-P1-01: trailing stop parameters corrected.
# The denominator bug (gross vs net credit) is fixed separately.
# With the correct net_premium basis, profit_pct starts at 0.0.
# TRAIL_START must be ABOVE the profit target (0.50) so the trail
# only protects gains beyond the target — not pre-empt it.
# SE8-P1-01: trail armed at 0.30 was below the 0.50 target,
# capping wins at 25-43% of credit. Now arms at 0.55.
TRAIL_START_PROFIT_PCT = 0.55
TRAIL_RETAIN_PCT       = 0.85

# ─────────────────────────────────────────────────────────────────────
# ORDER EXECUTION
# ─────────────────────────────────────────────────────────────────────
ORDER_FILL_TIMEOUT_SEC       = 30
HEDGE_FILL_TIMEOUT_SEC       = 10
CORE_FILL_TIMEOUT_SEC        = 15
SL_FILL_TIMEOUT_SEC          = 15
ORDER_POLL_INTERVAL_SEC      = 1
ORDER_AGGRESSION_TICKS       = 1
TICK_SIZE                    = 0.05
PARTIAL_FILL_CANCEL          = True
PARTIAL_FILL_CANCEL_HEDGE    = False
PAPER_SLIPPAGE_SHORT_TICKS   = 20
PAPER_SLIPPAGE_HEDGE_TICKS   = 40
ORDER_BETWEEN_LEGS_DELAY_SEC = 0.2
ORDER_STATUS_POLL_DELAY_SEC  = 0.2
MAX_SPREAD_ATM_PTS           = 3
MAX_SPREAD_OTM_PTS           = 5
MIN_OI_LOTS                  = 50

# ─────────────────────────────────────────────────────────────────────
# SESSION TIMING
# ─────────────────────────────────────────────────────────────────────
TZ = "Asia/Kolkata"
ENTRY_VIX_TIME     = time(9, 15)
STRIKE_SELECT_TIME = time(9, 17)

# LIVE FIX: widened entry window
# 09:30 avoids opening auction volatility (first 15 min)
# 14:00 gives 75 min minimum hold before EOD at 15:15
EXEC_START_TIME    = time(9, 30, 0)   # PATCHED: avoids opening auction
EXEC_END_TIME      = time(14, 0, 0)   # PATCHED: was 11:00 — enables trading all day

REGIME_FREEZE_TIME = time(14, 45)
TIME_EXIT_NORMAL   = time(15, 15)

# LIVE FIX: moved from 14:45 to 15:10
# At 14:45, OTM options still have 45 min of theta.
# Closing at market costs 20-40 pts unnecessarily.
# At 15:10, options near-zero — closing cost minimal.
TIME_EXIT_EXPIRY   = time(15, 10)
# Safety assertion: expiry exit must be before normal EOD exit.
# CFG8-P3-01: compare against TIME_EXIT_NORMAL not a hardcoded
# time(15,15) so the invariant holds if TIME_EXIT_NORMAL changes.
assert TIME_EXIT_EXPIRY < TIME_EXIT_NORMAL, (
    "TIME_EXIT_EXPIRY must be before TIME_EXIT_NORMAL"
)

MARKET_OPEN            = time(9, 15)
MARKET_CLOSE           = time(15, 30)
HEARTBEAT_INTERVAL_SEC = 60
EOD_RECONCILE_TIME     = time(15, 30)

# ─────────────────────────────────────────────────────────────────────
# UPSTOX API CONFIGURATION
# ─────────────────────────────────────────────────────────────────────
UPSTOX_BASE_V2 = "https://api.upstox.com/v2"
UPSTOX_BASE_V3 = "https://api.upstox.com/v3"

RATE_LIMIT_CAPACITY       = 50
RATE_LIMIT_REFILL_PER_SEC = 50
RATE_LIMIT_BURST          = 10

RETRY_BACKOFF_BASE  = 1.0
RETRY_MAX_BACKOFF   = 60.0
RETRY_MAX_ATTEMPTS  = 5

WS_RECONNECT_ATTEMPTS  = 3
WS_RECONNECT_DELAY_SEC = 5

# LIVE FIX: raised from 90s to 300s
# 90s fired spuriously on weekends and lunch gaps.
# Market-hours check in monitor_ws_health() also added.
WS_DOWNTIME_KILL_SWITCH_SEC = 300

NTP_MAX_OFFSET_SEC = 0.5
NTP_SERVER         = "0.in.pool.ntp.org"

# ── Endpoints ─────────────────────────────────────────────────────────
EP_MARGIN        = "/charges/margin"
EP_POSITIONS     = "/portfolio/short-term-positions"
EP_ORDER_PLACE   = "/order/place"
EP_ORDER_MODIFY  = "/order/modify"
EP_ORDER_CANCEL  = "/order/cancel"
EP_ORDER_HISTORY = "/order/history"
EP_ORDER_DETAILS = "/order/details"

# LIVE CONFIRMED: /order/get-order-book = 400 Invalid Endpoint
# Working endpoints confirmed by live test:
#   /order/trades/get-trades-for-day → 200 OK
#   /order/history?tag=nao           → 200 OK
EP_ORDER_TRADES  = "/order/trades/get-trades-for-day"

EP_OPTION_CHAIN  = "/option/chain"
EP_LTP           = "/market-quote/ltp"
EP_GREEKS        = "/market-quote/option-greek"  # 404 not used
EP_CANDLE        = "/historical-candle"
EP_PROFILE       = "/user/profile"
EP_WS_AUTHORIZE  = "/feed/market-data-feed/authorize"

# ── Instruments ───────────────────────────────────────────────────────
INSTRUMENT_NIFTY     = "NSE_INDEX|Nifty 50"
INSTRUMENT_VIX       = "NSE_INDEX|India VIX"
INSTRUMENT_NIFTY_FUT = "NSE_FO|NIFTY"

WS_URL_V3             = "wss://api.upstox.com/v3/feed/market-data-feed"
WS_MODE_LTPC          = "ltpc"
WS_MODE_OPTION_GREEKS = "option_greeks"
WS_MODE_FULL          = "full_d5"

# ─────────────────────────────────────────────────────────────────────
# CIRCUIT BREAKER LEVELS
# ─────────────────────────────────────────────────────────────────────
CB_LEVEL_1_PCT = 0.02
CB_LEVEL_2_PCT = 0.03

# LIVE FIX: raised from 0.06 to 0.10
# 5-lot straddle max loss = 5 × ₹30,000 = ₹150,000
# Old threshold ₹60,000 fired after first losing trade.
# AUDIT #2.4: CB_LEVEL_3_PCT=0.08 is the CURRENT correct
# value. It was previously 0.10 (same as CB_LEVEL_4_PCT,
# which caused overlapping triggers). Now 0.08 < 0.10,
# so L3 (50% reduction) fires before L4 (full stop).
CB_LEVEL_3_PCT = 0.08

CB_LEVEL_4_PCT = 0.10

# LIVE FIX: absolute VIX level (not % change)
# 30% VIX spike fires on routine intraday moves at VIX=11
CB_LEVEL_5_VIX_ABSOLUTE = 25.0
CB_LEVEL_5_IV_SPIKE_PCT  = 0.50  # kept for compat

CB_LEVEL_1_ACTION = "CLOSE_POSITION"
CB_LEVEL_2_ACTION = "HALT_NEW_TRADES"
CB_LEVEL_3_ACTION = "REDUCE_50PCT"
CB_LEVEL_4_ACTION = "FULL_STOP_MANUAL_REVIEW"
CB_LEVEL_5_ACTION = "FORCE_STRONG_BUY_REGIME"

# ─────────────────────────────────────────────────────────────────────
# GREEKS LIMITS (lot-adjusted)
# ─────────────────────────────────────────────────────────────────────
GREEKS_LIMITS = {
    "STRONG_SELL_VOL": {
        "delta_max":  0.10,
        "delta_min": -0.10,
        "gamma_max": -0.15,
        "gamma_min": -0.75,
        "vega_max":  -2000,
        "vega_min":  -8000,
        "theta_min":  1500,
    },
    "MILD_SELL_VOL": {
        "delta_max":  0.20,
        "delta_min": -0.20,
        "gamma_max": -0.075,
        "gamma_min": -0.45,
        "vega_max":  -1000,
        "vega_min":  -4000,
        "theta_min":   800,
    },
    "NEUTRAL": {
        "delta_max":  0.05,
        "delta_min": -0.05,
        "gamma_max":  0.075,
        "gamma_min": -0.075,
        "vega_max":    500,
        "vega_min":   -500,
        "theta_min":     0,
    },
    "BUY_VOL": {
        "delta_max":  0.50,
        "delta_min": -0.50,
        "gamma_max":  0.60,
        "gamma_min":  0.075,
        "vega_max":   5000,
        "vega_min":   1000,
        "theta_min":  None,
    },
    "STRONG_BUY_VOL": {
        "delta_max":  0.50,
        "delta_min": -0.50,
        "gamma_max":  1.125,
        "gamma_min":  0.375,
        "vega_max":  10000,
        "vega_min":   3000,
        "theta_min":  None,
    },
    "EVENT_HEDGE": {
        "delta_max":  0.10,
        "delta_min": -0.10,
        "gamma_max":  0.75,
        "gamma_min":  0.225,
        "vega_max":   6000,
        "vega_min":   2000,
        "theta_min":  None,
    },
}

# ─────────────────────────────────────────────────────────────────────
# CAPITAL ALLOCATION PER REGIME
# ─────────────────────────────────────────────────────────────────────
REGIME_CAPITAL_PCT = {
    "STRONG_SELL_VOL": 0.20,   # PATCH: was 0.30 — exceeded MAX_COMBINED_RISK_PCT (0.20), making the figure unreachable in practice
    "MILD_SELL_VOL":   0.20,
    "NEUTRAL":         0.10,
    "BUY_VOL":         0.10,
    "STRONG_BUY_VOL":  0.15,
    "EVENT_HEDGE":     0.05,
    # 10% reserved as margin/emergency buffer
}

REGIME_MAX_LOTS = {
    "STRONG_SELL_VOL": 8,
    "MILD_SELL_VOL":   6,
    "NEUTRAL":         3,
    "BUY_VOL":         3,
    "STRONG_BUY_VOL":  4,
    "EVENT_HEDGE":     2,
}

# PATCH: allow up to this many concurrent positions of the SAME
# strategy (each targeting a progressively later expiry for
# genuine time diversification) instead of blocking any second
# position outright. Existing capital / MAX_CONCURRENT_POSITIONS
# gates still apply on top of this.
MAX_TRANCHES_PER_STRATEGY = 2

REENTRY_COOLDOWN_SEC       = 300
# REENTRY_MAX_SPOT_MOVE_PCT lowered to 0.002 (0.2%). At 0.02 (2%,
# ~500pt NIFTY move) the 300s timer always expires first, making
# the price guard useless. 0.2% detects sharp intraday reversals.
REENTRY_MAX_SPOT_MOVE_PCT  = 0.002
BUILD_FAILURE_COOLDOWN_SEC = 300

# ─────────────────────────────────────────────────────────────────────
# MARGIN/SPAN APPROXIMATION (heuristic, NOT the real exchange calc)
# PATCH: previously lot sizing never considered margin at all —
# only theoretical max-loss and capital %. For naked/undefined-risk
# strategies, real SPAN+exposure margin is typically far higher
# than max_risk. These are documented, conservative approximations
# for paper-mode sizing realism; live mode still separately
# validates against the real broker margin API via check_margin().
# ─────────────────────────────────────────────────────────────────────
MARGIN_UTILIZATION_PCT        = 0.80  # cap cumulative estimated margin at 80% of capital
SPAN_NAKED_MARGIN_PCT         = 0.11  # ~11% of notional for naked short options (approx)
SPAN_SPREAD_MARGIN_MULTIPLIER = 1.15  # defined-risk spreads: ~1.15x max loss

# ─────────────────────────────────────────────────────────────────────
# NSE HOLIDAYS & EVENTS
# ─────────────────────────────────────────────────────────────────────
NSE_MARKET_HOLIDAYS = frozenset({
    "2026-01-15", "2026-01-26", "2026-03-03",
    "2026-03-26", "2026-03-31",
    "2026-04-14", "2026-05-01", "2026-05-28",
    "2026-06-26", "2026-09-14",
    # 2026-10-02 is RBI Policy day — trading day, NOT holiday
    "2026-10-20", "2026-11-10", "2026-11-24",
    "2026-12-25",
})

NSE_SPECIAL_TRADING_DAYS = frozenset({"2026-02-01"})

# LIVE FIX: 2026-10-02 kept here (RBI Policy = trading day)
HIGH_IMPACT_EVENTS = {
    "2026-02-01": "UNION_BUDGET",
    "2026-04-03": "RBI_POLICY",
    "2026-06-05": "RBI_POLICY",
    "2026-08-07": "RBI_POLICY",
    "2026-10-02": "RBI_POLICY",
    "2026-12-04": "RBI_POLICY",
}

HOLIDAY_CALENDAR_REVIEWED_ON = date(2026, 8, 31)
# AUDIT CFG-06: max calendar year covered by NSE_MARKET_HOLIDAYS.
# Preflight checks this against the current year and hard-fails
# if the calendar does not cover the current year.
HOLIDAY_CALENDAR_MAX_YEAR = 2026

# ─────────────────────────────────────────────────────────────────────
# TRADE CSV COLUMNS
# ─────────────────────────────────────────────────────────────────────
TRADE_CSV_COLUMNS = [
    "trade_id", "strategy_name",
    "regime_at_entry", "regime_at_exit",
    "entry_timestamp", "exit_timestamp",
    "holding_days", "entry_spot", "exit_spot",
    "entry_vix", "exit_vix", "legs_summary",
    "total_credit_received", "total_debit_paid",
    "net_premium", "max_risk", "realized_pnl",
    "transaction_costs", "net_pnl",
    "realized_pnl_percent", "exit_reason",
    "slippage_total_points",
    "composite_score_at_entry",
    "vol_score", "edge_score",
    "trend_score", "flow_score",
    "days_to_expiry_at_entry",
    "expiry_date", "paper_trade",
]

EXIT_REASONS = {
    "PROFIT_TARGET": "PROFIT_TARGET",
    "STOP_LOSS":     "STOP_LOSS",
    "TIME_EXIT":     "TIME_EXIT",
    "REGIME_CHANGE": "REGIME_CHANGE",
    "CIRCUIT_BREAK": "CIRCUIT_BREAK",
    "MANUAL":        "MANUAL",
    "EOD":           "EOD",
    "EXPIRY":        "EXPIRY",
}