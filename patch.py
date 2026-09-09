#!/usr/bin/env python3
"""v4.1 config relocation patch: env.txt content moves into core.py.

What this patch does
--------------------
1. CODE (core.py, 2 edits, atomic + idempotent):
   - Shrinks ENV_TEMPLATE to a token-only file. All engine tunables already
     live as documented defaults in load_config() — verified byte-for-byte
     against the previously shipped env.txt values — so no default changes.
   - Extends the load_config() docstring to record that the in-code
     defaults are the authoritative v4.1 tuning.
2. ENV (env.txt trim, backup + verify + report):
   - Backs up env.txt to env.txt.pre-v41.bak (never overwritten).
   - Rewrites env.txt keeping ONLY:
       * UPSTOX_ACCESS_TOKEN (always, value preserved exactly),
       * your other Upstox credentials (API key / secret / redirect URI)
         if filled in — a patch must never delete stored secrets, and
       * any tuning key whose value DIFFERS from the new in-code default
         (deliberate tuning keeps working — behaviour is unchanged).
     Everything else (redundant copies of defaults, dead keys the engine
     never reads, empty credential placeholders) is dropped and reported.
   - Re-parses the written file and CONFIRMS the token and kept keys are
     byte-identical; on any mismatch the backup is restored and we exit 1.

Contract (same as patch.py / patch_v40.py)
------------------------------------------
- Idempotent: re-running is a no-op (already-applied code edits are
  detected; an already-trimmed env.txt is left untouched, no new backup).
- Atomic code step: all anchors are verified BEFORE any write; on any
  anchor miss the patch exits 1 with ZERO files modified (env trim is
  not attempted either).
- --check: dry run, prints what WOULD change, writes nothing, exits 0.
- Applies with or without patch_v40 (anchors avoid all v40 regions).

Usage
-----
    cd /path/to/algo-trading        # repo root (where core.py lives)
    python3 patch_v41.py            # apply (code + env trim)
    python3 patch_v41.py --check    # dry run, no writes

Exit codes: 0 = ok (or clean --check), 1 = anchor miss / verify failure.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CORE = ROOT / "core.py"
ENV = ROOT / "env.txt"
BACKUP = ROOT / "env.txt.pre-v41.bak"

TOKEN_KEY = "UPSTOX_ACCESS_TOKEN"
UNUSED_CREDENTIALS = ("UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_REDIRECT_URI")

# ── In-code defaults mirror (KEY -> (type, default)) ─────────────────────────
# Generated from load_config() in core.py. The trimmer drops a key iff its
# raw parsed value equals this default (raw comparison is conservative:
# anything that could behave differently is KEPT).
DEFAULTS = {
    'ABORT_VIX_ABSOLUTE': ('float', 24.0),
    'ABORT_VIX_SPIKE_PCT': ('float', 15.0),
    'ADX_FAST_RESAMPLE': ('str', "300s"),
    'ADX_PERIOD': ('int', 14),
    'ADX_STRONG_THRESHOLD': ('float', 28.0),
    'ADX_TREND_THRESHOLD': ('float', 20.0),
    'ATM_IV_VIX_CAP': ('float', 1.35),
    'BROKERAGE_PER_ORDER': ('float', 20.0),
    'CALIBRATION_INTERVAL_SEC': ('int', 3600),
    'CHEAP_BUYBACK_AFTER_TIME': ('time', (13, 0)),
    'CHEAP_BUYBACK_PTS': ('float', 5.0),
    'CONDOR_WEAK_SIDE_MIN_FRAC': ('float', 0.30),
    'CREDIT_RATIO_VIX_REF': ('float', 13.5),
    'CREDIT_RISK_RATIO_DTE0_EARLY': ('float', 0.16),
    'CREDIT_RISK_RATIO_DTE0_LATE': ('float', 0.10),
    'CREDIT_RISK_RATIO_DTE0_MID': ('float', 0.13),
    'DAY_MOVE_RANGE_FACTOR': ('float', 1.93),
    'DAY_MOVE_USED_BLOCK_PCT': ('float', 125.0),
    'DAY_SIZE_FRIDAY': ('float', 0.55),
    'DAY_SIZE_MONDAY': ('float', 0.60),
    'DAY_SIZE_THURSDAY': ('float', 0.65),
    'DAY_SIZE_TUESDAY': ('float', 0.85),
    'DAY_SIZE_WEDNESDAY': ('float', 0.65),
    'DB_PATH': ('path', "data/nifty_algo_v3.db"),
    'DEFINED_RISK_ONLY_ON_EVENT': ('bool', True),
    'DELTA_CLOSE_DTE0': ('float', 0.45),
    'DELTA_CLOSE_DTE1P': ('float', 0.30),
    'DELTA_CLOSE_THRESHOLD': ('float', 0.28),
    'EMA_FAST': ('int', 9),
    'EMA_SLOW': ('int', 21),
    'EM_BAND_HI': ('float', 1.35),
    'EM_BAND_LO': ('float', 0.80),
    'ENTRY_SLIPPAGE_MULT': ('float', 0.35),
    'EVENT_SIZE_MULTIPLIER': ('float', 0.25),
    'EV_BLEND_MARKET_W': ('float', 0.30),
    'EV_BLEND_MODEL_W': ('float', 0.40),
    'EV_BLEND_PRIOR_W': ('float', 0.30),
    'EV_REGIME_ALIGN_BONUS': ('float', 0.05),
    'EV_STRONG_SELL_PRIOR_BONUS': ('float', 0.05),
    'EXCHANGE_TXN_RATE': ('float', 0.0003553),
    'EXIT_SLIPPAGE_MULT': ('float', 2.25),
    'GAMMA_TAIL_PROB_DTE0': ('float', 0.055),
    'GAMMA_TAIL_PROB_DTE1P': ('float', 0.025),
    'GIFT_NIFTY_INSTRUMENT_KEY': ('str', ""),
    'HARD_EXIT_TIME': ('time', (15, 0)),
    'HV_LOOKBACK_DAYS': ('int', 20),
    'IV_SIGMA_CAP_RATIO': ('float', 1.15),
    'LIVE_RATES_VERIFIED': ('bool', False),
    'LOG_DIR': ('path', "logs"),
    'LOG_LEVEL': ('str', "INFO"),
    'MAX_BROKERAGE_FRAC_OF_CREDIT': ('float', 0.15),
    'MAX_CONCURRENT_POSITIONS': ('int', 1),
    'MAX_DAILY_LOSS_PCT': ('float', 0.02),
    'MAX_DTE_TRADEABLE': ('int', 4),
    'MAX_ENTRIES_PER_DAY': ('int', 3),
    'MAX_FRICTION_FRAC_OF_CREDIT': ('float', 0.28),
    'MAX_RETRIES': ('int', 3),
    'MAX_RISK_PER_TRADE_PCT': ('float', 0.006),
    'MIN_BARS_FOR_ADX': ('int', 20),
    'MIN_BARS_FOR_EMA_SLOW': ('int', 25),
    'MIN_EV_FRAC_OF_CREDIT': ('float', 0.03),
    'MIN_EV_FRAC_OF_FRICTION': ('float', 0.35),
    'MIN_LOTS_FRACTION': ('float', 0.60),
    'MIN_TARGET_OVER_FRICTION': ('float', 1.25),
    'MIN_TRADING_DAYS_FOR_CALIBRATION': ('int', 20),
    'MTF_RESAMPLE_15': ('str', "900s"),
    'MTF_RESAMPLE_60': ('str', "3600s"),
    'NIFTY_LOT_SIZE': ('int', 65),
    'NIFTY_STRIKE_STEP': ('int', 50),
    'OI_BUILDUP_THRESHOLD': ('float', 0.08),
    'OI_CHANGE_LOOKBACK_MIN': ('int', 30),
    'OI_UNWIND_THRESHOLD': ('float', -0.08),
    'OI_WALL_MODERATE': ('float', 1.7),
    'OI_WALL_STRONG': ('float', 2.5),
    'OR_PCT_MODERATE': ('float', 0.0055),
    'OR_PCT_NARROW': ('float', 0.0036),
    'OR_PCT_VERY_NARROW': ('float', 0.0020),
    'OR_PCT_WIDE': ('float', 0.0078),
    'OR_STRADDLE_MODERATE': ('float', 0.62),
    'OR_STRADDLE_NARROW': ('float', 0.42),
    'OR_STRADDLE_VERY_NARROW': ('float', 0.24),
    'OR_STRADDLE_WIDE': ('float', 0.86),
    'PAPER_TRADE_MODE': ('bool', True),
    'PCR_BEARISH_THRESHOLD': ('float', 1.28),
    'PCR_BULLISH_THRESHOLD': ('float', 0.72),
    'PHANTOM_TRADE_TRACKING': ('bool', True),
    'PRICE_STOP_MAX_FRAC_OF_DIST': ('float', 0.40),
    'PRICE_STOP_MIN_PTS': ('float', 25.0),
    'PRICE_STOP_STRADDLE_MULT': ('float', 0.42),
    'PRICE_STOP_WING_FRAC': ('float', 0.30),
    'PROFIT_LOCK_PCT_DTE0': ('float', 0.40),
    'PROFIT_LOCK_PCT_DTE1PLUS': ('float', 0.25),
    'PROX_GAP_FRAC_DTE0': ('float', 0.70),
    'REGIME_CALC_INTERVAL_SEC': ('int', 15),
    'REGIME_PERSISTENCE_CYCLES': ('int', 3),
    'REQUEST_TIMEOUT_SECONDS': ('float', 10.0),
    'SEBI_RATE': ('float', 0.000001),
    'SHORT_DELTA_FLAT': ('float', 0.32),
    'SHORT_DELTA_STRONG': ('float', 0.28),
    'SHORT_DELTA_TREND': ('float', 0.30),
    'SKEW_BEARISH_THRESHOLD': ('float', 3.0),
    'SKEW_BULLISH_THRESHOLD': ('float', 0.95),
    'SPOT_BAR_INTERVAL_SEC': ('int', 60),
    'SPOT_PROXIMITY_PCT': ('float', 0.0016),
    'SPOT_PROXIMITY_PTS': ('int', 40),
    'SPOT_VELOCITY_PCT': ('float', 0.0014),
    'SPREAD_ABS_TOLERANCE': ('float', 0.85),
    'STAMP_DUTY_BUY_OPTIONS': ('float', 0.00003),
    'STARTING_CAPITAL': ('float', 1_000_000.0),
    'STOP_EFFICACY': ('float', 0.55),
    'STOP_MULT_DTE0': ('float', 1.60),
    'STOP_MULT_DTE1': ('float', 1.55),
    'STOP_MULT_DTE2P': ('float', 1.70),
    'STRADDLE_EXPLOSION_PCT': ('float', 18.0),
    'STRADDLE_ROC_ALERT_PCT': ('float', 12.0),
    'STRADDLE_ROC_WINDOW_MIN': ('int', 15),
    'STT_OPTIONS_EXERCISE': ('float', 0.00125),
    'STT_OPTIONS_SELL': ('float', 0.001),
    'TARGET_PCT_DTE0': ('float', 0.70),
    'TARGET_PCT_DTE1': ('float', 0.45),
    'TARGET_PCT_DTE2P': ('float', 0.40),
    'TRADING_WINDOW_LAST_ENTRY': ('time', (14, 0)),
    'TRADING_WINDOW_START': ('time', (9, 45)),
    'TUESDAY_EARLY_EXIT_ENABLED': ('bool', True),
    'TUESDAY_HARD_EXIT': ('time', (15, 0)),
    'TUESDAY_LAST_ENTRY': ('time', (12, 30)),
    'UPSTOX_ACCESS_TOKEN': ('str', ""),
    'UPSTOX_API_KEY': ('str', ""),
    'UPSTOX_API_SECRET': ('str', ""),
    'UPSTOX_REDIRECT_URI': ('str', ""),
    'VIX_ELEVATED': ('float', 28.0),
    'VIX_FAIL_LIMIT': ('int', 5),
    'VIX_LOW': ('float', 16.0),
    'VIX_NORMAL': ('float', 22.0),
    'VIX_SUPPRESSED': ('float', 12.5),
    'VRP_FAIR_THRESHOLD': ('float', 1.0),
    'VRP_SELL_THRESHOLD': ('float', 2.0),
    'VRP_SMOOTHING_CYCLES': ('int', 5),
    'WING_COST_FRAC_MAX': ('float', 0.50),
}


EDITS = [
    {
        "name": "core.py: token-only ENV_TEMPLATE (v4.1 relocation)",
        "file": CORE,
        "old": r'''ENV_TEMPLATE = """\
# NIFTY Intraday Options Engine v3.0 — Configuration
# Fill in all values before running. Never commit this file to version control.

# ── Upstox API Credentials ──────────────────────────────────────────────────
UPSTOX_API_KEY=
UPSTOX_API_SECRET=
UPSTOX_REDIRECT_URI=
UPSTOX_ACCESS_TOKEN=

# ── Trading Mode ─────────────────────────────────────────────────────────────
PAPER_TRADE_MODE=true
LIVE_RATES_VERIFIED=false

# ── Capital & Risk ────────────────────────────────────────────────────────────
STARTING_CAPITAL=1000000
MAX_DAILY_LOSS_PCT=0.02
MAX_RISK_PER_TRADE_PCT=0.006

# ── NIFTY Contract Spec ───────────────────────────────────────────────────────
# NSE revised the NIFTY 50 market lot from 75 to 65 for the January 2026
# cycle (first weekly expiry 06-Jan-2026, first monthly 27-Jan-2026).
NIFTY_LOT_SIZE=65
NIFTY_STRIKE_STEP=50

# ── Transaction Costs ─────────────────────────────────────────────────────────
# STT on the SALE of an option is 0.10% of the premium (statutory,
# w.e.f. 01-Oct-2024). v3.1 carried 0.15%, overstating the single
# largest variable cost of a premium-selling book by 50% and
# rejecting structurally sound trades on cost grounds.
STT_OPTIONS_SELL=0.001
# STT on EXERCISE is 0.125% of intrinsic value, payable by the buyer.
STT_OPTIONS_EXERCISE=0.00125
BROKERAGE_PER_ORDER=20.0
# NSE options: Rs 3,503 per crore of premium + Rs 50/cr IPFT = 0.03553%.
EXCHANGE_TXN_RATE=0.0003553
SEBI_RATE=0.000001
STAMP_DUTY_BUY_OPTIONS=0.00003

# ── v3.1 Profitability Calibration ────────────────────────────────────────────
# Opening range classified as a fraction of spot (scale-invariant) and against
# the opening ATM straddle. The more conservative of the two wins.
OR_PCT_VERY_NARROW=0.0020
OR_PCT_NARROW=0.0036
OR_PCT_MODERATE=0.0055
OR_PCT_WIDE=0.0078
OR_STRADDLE_VERY_NARROW=0.24
OR_STRADDLE_NARROW=0.42
OR_STRADDLE_MODERATE=0.62
OR_STRADDLE_WIDE=0.86
# Structural distances as a fraction of spot.
SPOT_PROXIMITY_PCT=0.0016
SPOT_VELOCITY_PCT=0.0014
# Fraction of the structural (wing) loss a working stop is assumed to avoid.
# 0.0 sizes on the full wing loss; hard-capped at 0.80 in code.
STOP_EFFICACY=0.55
# Probability the stop is jumped and the structure prints toward the wing.
GAMMA_TAIL_PROB_DTE0=0.055
GAMMA_TAIL_PROB_DTE1P=0.025
# Slippage in multiples of the half-spread, per leg.
ENTRY_SLIPPAGE_MULT=0.35
EXIT_SLIPPAGE_MULT=2.25
# Rupee tolerance added to the relative bid/ask gate (cheap wings).
SPREAD_ABS_TOLERANCE=0.85
MAX_DTE_TRADEABLE=4

# ── Trading Windows ───────────────────────────────────────────────────────────
TRADING_WINDOW_START=09:45
TRADING_WINDOW_LAST_ENTRY=14:00
HARD_EXIT_TIME=15:00
TUESDAY_HARD_EXIT=15:00
TUESDAY_LAST_ENTRY=12:30

# ── Position Limits ───────────────────────────────────────────────────────────
MAX_CONCURRENT_POSITIONS=1
MAX_ENTRIES_PER_DAY=3

# ── Paths ─────────────────────────────────────────────────────────────────────
DB_PATH=data/nifty_algo_v3.db
LOG_DIR=logs
LOG_LEVEL=INFO

# ── API Settings ──────────────────────────────────────────────────────────────
REQUEST_TIMEOUT_SECONDS=10
MAX_RETRIES=3

# ── Technical Analysis ────────────────────────────────────────────────────────
ADX_PERIOD=14
ADX_TREND_THRESHOLD=20.0
ADX_STRONG_THRESHOLD=28.0
EMA_FAST=9
EMA_SLOW=21
MTF_RESAMPLE_15=900s
MTF_RESAMPLE_60=3600s
MIN_BARS_FOR_ADX=20
MIN_BARS_FOR_EMA_SLOW=25

# ── VIX Regime Thresholds ─────────────────────────────────────────────────────
VIX_SUPPRESSED=12.5
VIX_LOW=16.0
VIX_NORMAL=22.0
VIX_ELEVATED=28.0

# ── ABORT Triggers ────────────────────────────────────────────────────────────
ABORT_VIX_SPIKE_PCT=15.0
ABORT_VIX_ABSOLUTE=24.0
VIX_FAIL_LIMIT=5

# ── VRP Thresholds (overridden by calibration) ────────────────────────────────
VRP_SELL_THRESHOLD=2.0
VRP_FAIR_THRESHOLD=1.0
VRP_SMOOTHING_CYCLES=5

# ── Regime Settings ───────────────────────────────────────────────────────────
REGIME_CALC_INTERVAL_SEC=15
REGIME_PERSISTENCE_CYCLES=3
# v3.2: day_move_used_pct is now the realised range as a percentage of
# the range the market PRICED for the elapsed part of the session
# (opening straddle x sqrt(elapsed fraction)). 100 = exactly on plan.
DAY_MOVE_USED_BLOCK_PCT=125.0

# ── OI / Positioning Thresholds (overridden by calibration) ──────────────────
OI_CHANGE_LOOKBACK_MIN=30
OI_BUILDUP_THRESHOLD=0.08
OI_UNWIND_THRESHOLD=-0.08
OI_WALL_STRONG=2.5
OI_WALL_MODERATE=1.7
PCR_BULLISH_THRESHOLD=0.72
PCR_BEARISH_THRESHOLD=1.28
SKEW_BEARISH_THRESHOLD=3.0
SKEW_BULLISH_THRESHOLD=0.95

# ── Exit Rules ────────────────────────────────────────────────────────────────
DELTA_CLOSE_THRESHOLD=0.28
SPOT_PROXIMITY_PTS=40
PRICE_STOP_STRADDLE_MULT=0.42
PROFIT_LOCK_PCT_DTE0=0.40
PROFIT_LOCK_PCT_DTE1PLUS=0.25
CHEAP_BUYBACK_PTS=5.0
CHEAP_BUYBACK_AFTER_TIME=13:00

# ── Calibration ───────────────────────────────────────────────────────────────
MIN_TRADING_DAYS_FOR_CALIBRATION=5
CALIBRATION_INTERVAL_SEC=3600
SPOT_BAR_INTERVAL_SEC=60
HV_LOOKBACK_DAYS=20

# ── Event / Special Day Settings ─────────────────────────────────────────────
EVENT_SIZE_MULTIPLIER=0.25
DEFINED_RISK_ONLY_ON_EVENT=true
TUESDAY_EARLY_EXIT_ENABLED=true

# ── Straddle Settings ─────────────────────────────────────────────────────────
STRADDLE_EXPLOSION_PCT=18.0
STRADDLE_ROC_WINDOW_MIN=15
STRADDLE_ROC_ALERT_PCT=12.0

# ── Phantom Trade Tracking ────────────────────────────────────────────────────
PHANTOM_TRADE_TRACKING=true

# ── v3.2 Profitability Calibration ────────────────────────────────────────────
# Premium stop as a multiple of the NET credit received, by DTE. v3.1 used a
# flat 2.5 (loss = 1.5x credit) against a 35% target, i.e. an 81% break-even
# win rate. These values put break-even in the 57-65% band, which a 0.15-0.22
# delta NIFTY short structure genuinely achieves.
# v3.3: raised from 1.40. The 1.4x stop on a DTE-0 vertical converts a
# ~25-point NIFTY counter-rally into a stop-out: at delta 0.22-0.36 the
# short leg gains 0.3-0.5x the move, so 0.4 x credit (~4 points on a 10
# point credit) IS a 25 point move. Replay of 2026-09-08 (a real VIX-11
# expiry downtrend) showed the position's max adverse premium move of
# +27% in 37 minutes with the trend then resuming lower - the 1.4x line
# was inside intraday noise. 1.6x keeps the loss at ~0.6x credit while
# giving a normal pullback room; the structural wing and the delta /
# proximity backstops remain the hard lines.
STOP_MULT_DTE0=1.60
STOP_MULT_DTE1=1.55
STOP_MULT_DTE2P=1.70
# Profit target as a fraction of the net credit, by DTE. Read together with
# the stop multiples above: 0.50 against 1.40 is reward/risk 1.25 and a
# break-even win rate near 55%, versus 81% under v3.1.
TARGET_PCT_DTE0=0.70
TARGET_PCT_DTE1=0.45
TARGET_PCT_DTE2P=0.40
# Short-leg delta at which a leg is closed. v3.1 used one flat 0.28 for every
# DTE, which is barely above the delta the engine sells at.
# v3.3: raised from 0.35 to 0.45. The engine now sells 0.22-0.36 delta on
# expiry afternoon; a close line 0.13 above the entry delta fired on
# ordinary drift at exactly the time delta moves fastest. 0.45 is the
# "structure decisively wrong" line desks use on 0DTE verticals, and it
# no longer sits on top of the sell window.
DELTA_CLOSE_DTE0=0.45
DELTA_CLOSE_DTE1P=0.30
# Spot backstop: how far INSIDE the short strike the spot stop sits, as a
# fraction of the wing, floored in points and capped as a fraction of the
# short-strike distance. Replaces 0.42 x opening straddle.
PRICE_STOP_WING_FRAC=0.30
PRICE_STOP_MIN_PTS=25
PRICE_STOP_MAX_FRAC_OF_DIST=0.40
# v3.3: proximity-to-short defense as a FRACTION of the entry gap to the
# short strike. The absolute 40pt band is larger than the whole gap for
# the delta 0.3-0.4 shorts a VIX-11 expiry offers (~45pts), so the trade
# would be closed at entry+5pts by its own safety. Executing the exit at
# 70% of the gap travelled scales the defense with the structure and the
# vol environment automatically.
PROX_GAP_FRAC_DTE0=0.70
# Delta-primary strike selection. Target short delta by trend strength.
# v3.3: raised from 0.22/0.18/0.15. On a 50-point strike grid with VIX 11,
# 0.18-0.22 targets land ~100-150 points OTM where the entire 0DTE credit
# is 5-10 points - unpayable against ~1.3 points of round-trip friction
# per lot (measured 2026-09-08: delta 0.224 short -> credit 10.2, ratio
# 0.108, structurally rejected all day). Professional 0DTE sellers work
# the 0.25-0.40 delta band after midday; 0.32/0.30/0.28 puts the engine
# there without selling the money.
SHORT_DELTA_FLAT=0.32
SHORT_DELTA_TREND=0.30
SHORT_DELTA_STRONG=0.28
# Sanity band for the short strike, as a multiple of the expected REMAINING
# move (opening straddle scaled by sqrt of the session fraction left).
EM_BAND_LO=0.80
EM_BAND_HI=1.35
# Round-trip friction must not exceed this fraction of the net credit, and
# brokerage alone must not exceed this fraction of it.
MAX_FRICTION_FRAC_OF_CREDIT=0.28
MAX_BROKERAGE_FRAC_OF_CREDIT=0.15
# The profit target must clear the whole round trip by this factor.
MIN_TARGET_OVER_FRICTION=1.25
# Below this many lots the trade is skipped rather than rounded up to one lot.
MIN_LOTS_FRACTION=0.60
# A long wing costing more than this fraction of the short it protects hands
# back too much of the premium to be worth buying at that strike.
WING_COST_FRAC_MAX=0.50
# An iron condor whose weaker side contributes less than this fraction of the
# gross credit is paying two extra legs of friction for nothing.
CONDOR_WEAK_SIDE_MIN_FRAC=0.30
# -- v3.3 Profitability Calibration (2026 VIX-11 regime) ---------------------
# DTE-0 credit/risk ladder, VIX-scaled. These are the FRACTIONS of
# (wing - credit) the net credit must reach, by minutes remaining. The
# absolute v3.2 ladder was calibrated against the premium a VIX 13.5
# session pays; at VIX 11 the market pays ~0.8x of that, so every
# requirement is now scaled by clamp(vix / CREDIT_RATIO_VIX_REF ...).
CREDIT_RISK_RATIO_DTE0_EARLY=0.16
CREDIT_RISK_RATIO_DTE0_MID=0.13
CREDIT_RISK_RATIO_DTE0_LATE=0.10
CREDIT_RATIO_VIX_REF=13.5
# The EV gate's barrier model may not trust the vendor-stamped 0DTE IV
# (measured on 2026-09-08: 21.6% stamped vs 10.2% implied by the ATM
# straddle price itself vs India VIX 11.1). When the straddle publishes a
# smaller sigma, the IV-derived estimate is capped at this multiple of it.
IV_SIGMA_CAP_RATIO=1.15
# Maximum cap on the ATM IV used for sigma, as a multiple of the day's
# India VIX. If the stamp is more than this above the cash VIX it is
# treated as a sqrt(T) artefact, not information.
ATM_IV_VIX_CAP=1.35
# Minimum edge the EV gate may accept, as a fraction of net credit and of
# round-trip friction. v3.2 used max(3% credit, 35% friction, 0.75pts);
# the hard 0.75 point floor is ~8% of an entire VIX-11 expiry credit and
# rejected structures whose whole expectancy was sound but small.
MIN_EV_FRAC_OF_CREDIT=0.03
MIN_EV_FRAC_OF_FRICTION=0.35
# EV p_win blend weights: the lognormal touch model, the OR-conditional
# empirical prior, and the market-implied (1 - short delta) probability.
# v3.2 blended model:prior 50/50, which lets a poisoned sigma floor the
# verdict. The chain's own delta is an independent, market-quoted vote.
EV_BLEND_MODEL_W=0.40
EV_BLEND_PRIOR_W=0.30
EV_BLEND_MARKET_W=0.30
# STRONG_SELL_PREMIUM adds to the empirical prior (the vol stack's own
# consensus that the chain is paying above realised risk), and a sold
# structure whose threat side sits against the confirmed trend direction
# earns a small bounded alignment bonus.
EV_STRONG_SELL_PRIOR_BONUS=0.05
EV_REGIME_ALIGN_BONUS=0.05
# Fast intraday trend timeframe. 15-minute ADX cannot mature inside a NIFTY
# session (it needs 2*period+1 = 29 bars; the session has 25).
ADX_FAST_RESAMPLE=300s

# ── Misc ──────────────────────────────────────────────────────────────────────
GIFT_NIFTY_INSTRUMENT_KEY=
"""''',
        "new": r'''ENV_TEMPLATE = """# ─────────────────────────────────────────────
# NIFTY Algo Trading Engine v4.1 — env.txt
# ─────────────────────────────────────────────
# This file holds ONE thing: today's Upstox access token. Nothing else.
#
# v4.1 moved every engine tunable (windows, costs, thresholds, exits,
# calibration, sizing) INTO the code — see the documented defaults in
# `load_config()` in core.py. Those defaults reproduce the previously
# shipped env.txt values exactly, so behaviour is unchanged.
#
# Extra/unknown keys in this file are ignored by the engine. Your API
# key / secret / redirect URI are NOT needed here either: they are only
# used on the Upstox login page when you generate the token below.
#
# Daily ritual: paste today's token after UPSTOX_ACCESS_TOKEN= and run.
# ─────────────────────────────────────────────

UPSTOX_ACCESS_TOKEN=
"""''',
    },
    {
        "name": "core.py: load_config docstring records v4.1 authority",
        "file": CORE,
        "old": r'''    Load Config from env.txt (file values override OS environment).
    Applies safety checks and clamps dangerous values.
    """''',
        "new": r'''    Load Config from env.txt (file values override OS environment).
    Applies safety checks and clamps dangerous values.

    v4.1: every default below is the authoritative engine tuning — it
    reproduces the previously shipped env.txt values exactly, so a
    token-only env.txt behaves identically to the old full file. Any key
    still present in env.txt (or OS env) overrides its default, so
    deliberate tuning keeps working; unknown keys are ignored.
    """''',
    },
]


# ── value parsing (mirrors core.load_env_file + _get_* semantics) ────────────

def parse_env_file(path: Path) -> dict:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _as_bool(val):
    if val is None or val == "":
        return None
    return val.strip().lower() in ("1", "true", "yes", "on")


def _as_float(val):
    try:
        return float(val) if val not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _as_int(val):
    try:
        return int(val) if val not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _as_time(val):
    if not val:
        return None
    try:
        parts = val.strip().split(":")
        return (int(parts[0]), int(parts[1]))
    except Exception:
        return None


def _norm_path(val: str) -> str:
    p = Path(val)
    if not p.is_absolute():
        p = ROOT / p
    return str(p)


def equals_default(key: str, value: str) -> bool:
    """True iff this env value behaves exactly like the key being absent."""
    typ, default = DEFAULTS[key]
    if typ == "bool":
        parsed = _as_bool(value)
        return parsed is None or parsed == default
    if typ == "float":
        parsed = _as_float(value)
        return parsed is None or parsed == default
    if typ == "int":
        parsed = _as_int(value)
        return parsed is None or parsed == default
    if typ == "time":
        parsed = _as_time(value)
        return parsed is None or parsed == default
    if typ == "path":
        return _norm_path(value) == _norm_path(default)
    # str
    if key == "ADX_FAST_RESAMPLE" and value == "":
        return True  # `or "300s"` fallback in load_config
    return value == default


# ── code edit engine ──────────────────────────────────────────────────────────

def check_code():
    """Return (would_apply, already) lists without writing."""
    if not CORE.exists():
        print(f"FATAL: {CORE} not found. Run from the repo root.")
        sys.exit(1)
    src = CORE.read_text(encoding="utf-8")
    would_apply, already = [], []
    for e in EDITS:
        old_c, new_c = src.count(e["old"]), src.count(e["new"])
        if new_c >= 1 and old_c == 0:
            already.append(e["name"])
        elif old_c == 1:
            would_apply.append(e["name"])
        else:
            print(f"AMBIGUOUS anchor for {e['name']}: old x{old_c}, new x{new_c}")
            sys.exit(1)
    return would_apply, already


def apply_code():
    would_apply, already = check_code()
    for name in already:
        print(f"  [already] {name}")
    if not would_apply:
        return 0
    src = CORE.read_text(encoding="utf-8")
    for e in EDITS:
        if e["name"] in already:
            continue
        assert src.count(e["old"]) == 1, e["name"]  # re-verified pre-write
        src = src.replace(e["old"], e["new"], 1)
        print(f"  [applied] {e['name']}")
    CORE.write_text(src, encoding="utf-8")
    return len(would_apply)


# ── env trim ──────────────────────────────────────────────────────────────────

def plan_trim(env: dict):
    """Split keys into (keep, drop_default, drop_dead, drop_cred)."""
    keep, drop_default, drop_dead, drop_cred = {}, {}, {}, {}
    for key, value in env.items():
        if key == TOKEN_KEY:
            keep[key] = value
        elif key in UNUSED_CREDENTIALS:
            # Never delete a stored secret: keep non-empty credentials in
            # place, drop only empty placeholders.
            if value:
                keep[key] = value
            else:
                drop_cred[key] = value
        elif key in DEFAULTS:
            # OS-env guard: file currently overrides OS env; if OS carries a
            # DIFFERENT value, dropping the file key would surface it.
            os_val = os.environ.get(key)
            if os_val is not None and os_val != value:
                keep[key] = value  # + warned in report
            elif equals_default(key, value):
                drop_default[key] = value
            else:
                keep[key] = value
        else:
            drop_dead[key] = value  # never read by the engine
    return keep, drop_default, drop_dead, drop_cred


def render_trimmed(keep: dict) -> str:
    lines = [
        "# NIFTY Algo Trading Engine v4.1 — env.txt (trimmed by patch_v41.py)",
        "# Full backup of your previous file: env.txt.pre-v41.bak",
        "# Only the access token + keys differing from in-code defaults live here.",
        "",
        f"{TOKEN_KEY}={keep.get(TOKEN_KEY, '')}",
    ]
    creds = [(k, v) for k, v in keep.items() if k in UNUSED_CREDENTIALS]
    rest = [(k, v) for k, v in keep.items()
            if k != TOKEN_KEY and k not in UNUSED_CREDENTIALS]
    if creds:
        lines += ["", "# --- Upstox credentials (stored in place) ---"]
        lines += [f"{k}={v}" for k, v in creds]
    if rest:
        lines += ["", "# --- kept: differs from in-code default (deliberate tuning) ---"]
        lines += [f"{k}={v}" for k, v in rest]
    return "\n".join(lines) + "\n"


def report_trim(env, keep, drop_default, drop_dead, drop_cred, prefix=""):
    print(f"{prefix}env.txt keys: {len(env)} -> keep {len(keep)}, "
          f"drop {len(drop_default) + len(drop_dead) + len(drop_cred)}")
    if TOKEN_KEY in keep:
        tok = keep[TOKEN_KEY]
        masked = (tok[:4] + "..." + tok[-2:]) if len(tok) > 8 else ("<empty>" if not tok else "***")
        print(f"{prefix}  keep {TOKEN_KEY}={masked} (value preserved exactly)")
        if not tok:
            print(f"{prefix}  WARNING: token is empty — paste today's token before running.")
    for k, v in keep.items():
        if k == TOKEN_KEY:
            continue
        why = "differs from default"
        if k in UNUSED_CREDENTIALS:
            why = "credential stored in place (never deleted by this patch)"
            v = (v[:4] + "..." + v[-2:]) if len(v) > 8 else "***"
        if os.environ.get(k) is not None and os.environ.get(k) != v:
            why = f"kept: OS env also sets {k}={os.environ.get(k)!r}"
        print(f"{prefix}  keep {k}={v} ({why})")
    for k in sorted(drop_cred):
        print(f"{prefix}  drop {k} (empty placeholder; preserved in backup)")
    if drop_dead:
        print(f"{prefix}  drop dead keys (never read by engine): "
              + ", ".join(sorted(drop_dead)))
    if drop_default:
        print(f"{prefix}  drop redundant (= in-code default, {len(drop_default)} keys)")
    for k in sorted(set(os.environ) & set(DEFAULTS)):
        if k not in env or (k in env and env[k] == os.environ[k]):
            print(f"{prefix}  note: {k} also set in OS environment (file trim "
                  f"does not change OS env)")


def apply_trim(check_only: bool):
    if not ENV.exists():
        print("  [trim] no env.txt — nothing to trim (fresh setups get the "
              "token-only template).")
        return 0
    env = parse_env_file(ENV)
    keep, drop_default, drop_dead, drop_cred = plan_trim(env)
    if check_only:
        report_trim(env, keep, drop_default, drop_dead, drop_cred, prefix="  [trim?] ")
        return 0
    new_text = render_trimmed(keep)
    if ENV.read_text(encoding="utf-8") == new_text:
        print("  [trim] env.txt already trimmed — no-op (no new backup).")
        report_trim(env, keep, drop_default, drop_dead, drop_cred, prefix="  [trim] ")
        return 0
    if not BACKUP.exists():
        BACKUP.write_text(ENV.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"  [trim] backup written: {BACKUP.name}")
    else:
        print(f"  [trim] backup already exists, kept as-is: {BACKUP.name}")
    ENV.write_text(new_text, encoding="utf-8")
    # verify: re-parse, token + kept keys must be identical
    reread = parse_env_file(ENV)
    ok = all(reread.get(k) == v for k, v in keep.items())
    if not ok or reread.get(TOKEN_KEY) != env.get(TOKEN_KEY):
        ENV.write_text(BACKUP.read_text(encoding="utf-8"), encoding="utf-8")
        print("  [trim] VERIFY FAILED — backup restored, exiting 1.")
        return 1
    print(f"  [trim] env.txt rewritten: {len(env)} -> {len(keep)} keys, "
          f"verified OK.")
    report_trim(env, keep, drop_default, drop_dead, drop_cred, prefix="  [trim] ")
    return 0


def main(argv):
    check_only = len(argv) > 1 and argv[1] == "--check"
    if len(argv) > 1 and argv[1] not in ("--check",):
        print(f"usage: {Path(argv[0]).name} [--check]")
        return 1
    mode = "CHECK (dry run)" if check_only else "APPLY"
    print(f"[patch_v41] {mode}: v4.1 env.txt -> core.py relocation")
    print("[patch_v41] step 1/2: code edits")
    if check_only:
        would_apply, already = check_code()
        for name in already:
            print(f"  [already] {name}")
        for name in would_apply:
            print(f"  [would-apply] {name}")
        print("[patch_v41] step 2/2: env trim")
        apply_trim(check_only=True)
        print("[patch_v41] --check done, nothing written.")
        return 0
    applied = apply_code()
    print("[patch_v41] step 2/2: env trim")
    rc = apply_trim(check_only=False)
    if rc != 0:
        return 1
    print(f"[patch_v41] done: {applied} code edit(s) applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))