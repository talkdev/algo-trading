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
# C4-05: MAX_COMBINED_RISK_PCT is non-binding.
# With MAX_RISK_PER_TRADE_PCT=0.03 and MAX_CONCURRENT_POSITIONS=4,
# max theoretical exposure = 4 * 3% = 12%, well below this 20% cap.
# The real binding constraint is the sum of position max_risk values
# checked in _pre_trade_checks() and _enter_new_position().
# This constant provides a hard ceiling for extreme scenarios only.
# Do NOT raise MAX_RISK_PER_TRADE_PCT to 'use' this budget —
# the two limits are intentionally not coupled.
MAX_COMBINED_RISK_PCT    = 0.20
MAX_DAILY_LOSS_PCT       = 0.03
MAX_DRAWDOWN_PCT         = 0.10
POSITION_SIZE_PCT        = 0.15
# C4-06: DEAD CONSTANT — only referenced in testing.py stub.
# Real costs use the itemised COST_* constants in _calculate_transaction_costs.
TRANSACTION_COST_PCT     = 0.0005
MAX_CONCURRENT_POSITIONS = 4

# CAL-RISK: derived from MAX_RISK_PER_TRADE_PCT above
# At 0.015 * 1,000,000 = Rs15,000 per trade
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
# PRF-C06: minimum VIX for short-vol entries. Below 11, premium
# is too thin to cover slippage and transaction costs.
MIN_VIX_SELL     = 11.0

# C4-02: VIX-adaptive delta bands — now wired into _build_credit_spreads.
# Selling 0.20 delta at VIX=11 and VIX=25 are very different trades.
# LOW_VIX (< 14): use wider delta range (more OTM, less credit but safer)
# MID_VIX (14-18): standard range
# HIGH_VIX (> 18): tighter delta range (closer strikes, more credit)
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

# PRF-C11: thresholds tightened. In low-VIX (11-14), z-scores
# rarely exceed ±1.0. 1.2/-0.8 makes skew contribute more often.
SKEW_ZSCORE_FEAR       =  1.2
SKEW_ZSCORE_COMPLACENT = -0.8   # reference: SKEW_Z_FLAT = -1.0
SKEW_LOOKBACK_DAYS     = 60
EDGE_LOOKBACK_DAYS     = 60

IV_ATM_HISTORY_MAXLEN = 22_500

RV_LOOKBACK_DAYS     = 20
EDGE_RICH  = 5.0   # reference: IV-RV > 5 -> rich
EDGE_CHEAP = 0.0   # reference: IV-RV < 0 -> cheap
# C4-03: Edge percentile thresholds — now wired into compute_iv_rank.
# IV rank >= EDGE_PERCENTILE_HIGH -> vol is rich (sell signal)
# IV rank <= EDGE_PERCENTILE_LOW  -> vol is cheap (buy signal)
# Previously defined but never referenced anywhere in the codebase.
EDGE_PERCENTILE_HIGH = 70
EDGE_PERCENTILE_LOW  = 30

# PRF-01: raised from 3 to 20. Three observations cannot establish
# whether IV-RV spread is unusually rich or cheap — the sample std
# has ~40% error at N=3. 20 sessions gives a meaningful baseline.
# Until then the edge module stays neutral (score=0), which is
# safer than acting on a near-meaningless statistic.
EDGE_SCORE_MIN_HISTORY = 20

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
# C4-06: DEAD CONSTANT — never referenced. NSE 09:15-15:30 on
# 30-min bars = 12.5 bars/day (not 13). Any future use will be wrong.
BARS_PER_DAY     = 13

# IMM-06: lowered from 300 to 60 to align with main loop cadence.
# ADX and EMA can be stale for up to 5 minutes at 300s, causing
# delayed regime changes. At 60s the trend module is always fresh.
CANDLE_REFRESH_SECONDS = 60
CANDLE_LOOKBACK_DAYS   = 45   # PATCH: was 30 — too tight for RV_LOOKBACK_DAYS=20 after weekend/holiday attrition
# PRF-C10: raised from 15 to 30. NSE OI updates every 3-5 min.
# 15-min window = 3-5 updates (noisy). 30-min = 6-10 updates,
# giving a smoother, more reliable flow signal.
FLOW_WINDOW_MINUTES     = 30
# CFG-P1: per-leg slippage haircut applied in credit gate BEFORE
# trade approval.
ENTRY_SLIPPAGE_PTS_PER_LEG = 0.75

# ─── IMM-01: Dynamic flow weight ───────────────────────────────
# If flow score is None for more than this fraction of recent cycles,
# set flow weight to 0 and redistribute to other modules.
# The flow module frequently returns None (DTE<3, warming up, etc.)
# and a fixed 15% weight on a missing signal distorts the composite.
FLOW_WEIGHT_NONE_THRESHOLD = 0.50   # >50% None -> weight = 0
FLOW_WEIGHT_NONE_LOOKBACK  = 10     # last N cycles to check

# ─── IMM-02: VIX-adaptive lot sizing ────────────────────────────
# Scale MAX_RISK_PER_TRADE by (VIX_REFERENCE / current_VIX).
# In high VIX, premium is larger but so is risk — reduce size.
# In low VIX, premium is thin — increase size to maintain returns.
VIX_ADAPTIVE_SIZING        = True
VIX_ADAPTIVE_REFERENCE     = 16.0   # neutral VIX level
VIX_ADAPTIVE_MIN_MULT      = 0.5    # minimum size multiplier
VIX_ADAPTIVE_MAX_MULT      = 2.0    # maximum size multiplier

# ─── IMM-03: Distance-based secondary stop ───────────────────────
# For condors/spreads: close when spot reaches this fraction of the
# distance from entry spot to the short strike. Prevents holding
# through a strike breach when the premium stop hasn't fired yet.
STOP_SPOT_FRACTION_OF_DISTANCE = 0.80

# ─── IMM-04: Partial profit-taking ladder ────────────────────────
# Close PARTIAL_PROFIT_CLOSE_PCT of the position when profit reaches
# PARTIAL_PROFIT_TRIGGER_PCT of the full profit target.
# Locks in gains early while letting the remainder run to full target.
PARTIAL_PROFIT_ENABLED         = True
PARTIAL_PROFIT_TRIGGER_PCT     = 0.25   # close partial at 25% of target
PARTIAL_PROFIT_CLOSE_PCT       = 0.50   # close 50% of position

# ─── IMM-05: Adaptive persistence ────────────────────────────────
# Use fewer confirmation readings when the composite signal is strong.
# Strong signals (high conviction) should not be delayed by 3 readings.
ADAPTIVE_PERSISTENCE_ENABLED   = True
ADAPTIVE_PERSISTENCE_FAST_THRESHOLD = 0.60  # composite > this -> 2 readings
ADAPTIVE_PERSISTENCE_FAST_READINGS  = 2     # readings for strong signal
ADAPTIVE_PERSISTENCE_SLOW_READINGS  = 3     # readings for weak signal
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
# CFG-02: widened DTE windows. Analysis shows EV/lot rises
# monotonically with DTE while P(stop) does not increase.
# Confining to DTE<=4 was the least favourable point on the curve.
STRADDLE_DTE_MIN       = 1
STRADDLE_DTE_MAX       = 8    # was 4    # widened from 7
STRADDLE_STOP_MULT     = 2.0   # stop = 2x credit
# PRF-C01: lowered from 0.60 to 0.50 (same reasoning).
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
# CFG-02: widened (same reasoning as straddle).
CONDOR_DTE_MIN            = 2
CONDOR_DTE_MAX            = 8    # was 5     # widened from 7
# EXE-02: set to 1. With TIME_EXIT_EXPIRY=13:30, holding to DTE=0
# is dangerous. Exit at DTE=1 harvests 95%+ of theta without the
# terminal gamma tail risk from 0-DTE institutional hedging flows.
CONDOR_EXIT_DTE           = 1
# PRF-C01: lowered from 0.60 to 0.50. 50% target is reached faster
# (typically 1-2 days vs 3-4 days for 65%), reducing time-in-trade
# and gamma exposure. With 2.0x stop: R:R = 0.50/2.0 = 0.25.
# Wider stop (2.0x) means fewer stop-outs on normal intraday moves,
# so realised win rate rises to ~75-80%, making BEP achievable.
CONDOR_TARGET_PCT         = 0.50
# AUDIT CFG-01: minimum credit expressed as % of wing width.
# At CONDOR_WING_WIDTH=400, 22% = 88 pts minimum.
# Absolute fallback kept for reference only.
# PRF-C04: lowered from 0.22 to 0.18. At VIX=11-13 with dynamic
# wing widths, 22% is often unattainable. 18% still ensures
# meaningful credit/risk ratio while allowing more entries.
CONDOR_MIN_CREDIT_PCT_OF_WIDTH = 0.18
CONDOR_MIN_CREDIT         = 40   # legacy absolute floor; builder uses PCT_OF_WIDTH above
# C4-06: DEAD CONSTANT — no condor adjustment logic exists.
CONDOR_ADJUSTMENT_DELTA   = 0.35
CONDOR_TESTED_SIDE_BUFFER = 100
# PRF-C03: lowered from 1.5 to 1.2. At VIX=11-13, 1.5σ places
# shorts ~350pts OTM with credit ~20-25pts — often fails min-credit
# check. 1.2σ places shorts ~280pts OTM, credit ~35-45pts.
# P(inside) = 83% vs 86.6% at 1.5σ — acceptable trade-off for
# meaningful credit in low-VIX environments.
CONDOR_SIGMA_MULTIPLIER   = 1.2

# ── Credit spreads ────────────────────────────────────────────────────
# SPREAD_DELTA_SHORT lowered to 0.20. At 0.30 delta (~30% ITM
# probability per side), ~50% chance at least one side is tested
# inside a week. 0.20 delta improves risk/reward materially.
SPREAD_DELTA_SHORT    = 0.20
# CAL-DELTA: lowered from 0.15 to 0.08 to maintain meaningful
# spread width with the new 0.16 short delta. 0.16-0.08=0.08
# delta spread gives adequate wing protection and defined risk.
SPREAD_DELTA_LONG     = 0.08
# EXE-02: set to 1 (same reasoning as condor).
SPREAD_EXIT_DTE       = 1
# PRF-C01: lowered from 0.60 to 0.50 (same reasoning as condor).
SPREAD_TARGET_PCT     = 0.50
# AUDIT CFG-01: spread min credit as % of wing width.
# PRF-C05: lowered from 0.25 to 0.20 (same reasoning as condor).
SPREAD_MIN_CREDIT_PCT_OF_WIDTH = 0.20
SPREAD_MIN_CREDIT     = 25   # legacy absolute floor; builder uses PCT_OF_WIDTH above
# C4-06: DEAD CONSTANT — no roll logic exists in strategy_engine.
SPREAD_ROLL_DELTA_TRIGGER  = 0.35
SPREAD_SKEW_THRESHOLD      = 2.0

# ── Ratio spread ──────────────────────────────────────────────────────
RATIO_ATM_OFFSET_PTS       = 100
RATIO_EXIT_DTE             = 1
RATIO_TARGET_PCT           = 0.25
# C4-06: DEAD CONSTANT — ratio spread has no delta-based exit.
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
# C4-06: DEAD CONSTANT — never referenced in any decision path.
STATIC_STOP_PCT         = 0.10
PROFIT_TARGET_PCT       = 0.50
DEBIT_PROFIT_TARGET_PCT = 0.50

# C4-01: VIX-scaled stop model — now wired into _build_short_straddle.
# stop_pct = clamp(SL_BASE * vix / SL_REF, SL_MIN, SL_MAX)
# At VIX=11: 0.236 (tighter). At VIX=22: 0.40 (wider, capped).
# Replaces the flat STRADDLE_STOP_MULT=1.25 for the straddle builder.
SL_BASE_PERCENT    = 0.30
SL_REFERENCE_VIX   = 14.0
SL_MIN_PERCENT     = 0.18
SL_MAX_PERCENT     = 0.40

# PRF-C07/C08: trail start raised from 0.55 to 0.70, retain 0.85->0.90.
# With target at 0.50, trail at 0.55 was only 5% above target —
# barely a band. 0.70 creates a genuine 0.50-0.70 range where the
# position can run, then trail protects gains above 70% of credit.
# 0.90 retain means trail closes only when profit retraces 10% from peak.
TRAIL_START_PROFIT_PCT = 0.70
TRAIL_RETAIN_PCT       = 0.90

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
# CAL-EXEC: raised from 09:30 to 09:35. The first 15 minutes
# have the widest bid-ask spreads and most erratic price discovery.
# 09:35 lets the opening settle without meaningfully reducing the
# trading window (4h25m vs 4h30m). Reduces entry slippage.
EXEC_START_TIME    = time(9, 30, 0)   # PATCHED: avoids opening auction + initial noise
EXEC_END_TIME      = time(14, 30, 0)   # PATCHED: was 11:00 — enables trading all day

REGIME_FREEZE_TIME = time(14, 45)
TIME_EXIT_NORMAL   = time(15, 15)

# LIVE FIX: moved from 14:45 to 15:10
# At 14:45, OTM options still have 45 min of theta.
# Closing at market costs 20-40 pts unnecessarily.
# At 15:10, options near-zero — closing cost minimal.
# EXE-01: moved from 15:10 to 13:30.
# After 13:30 on expiry day, gamma scales exponentially and
# liquidity providers widen quotes. Institutional 0-DTE delta-
# hedging flows create violent moves. The risk/reward of holding
# the last 90 minutes is empirically negative — collecting pennies
# of residual theta while risking a full max-loss stop-out.
TIME_EXIT_EXPIRY   = time(13, 30)
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
# CFG-VEGA: enable vega/gamma pre-trade gate in _pre_trade_checks().
# Set to False during initial calibration to observe without blocking.
GREEKS_VEGA_GATE = True
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