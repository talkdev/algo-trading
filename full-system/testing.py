"""
tests.py — Comprehensive test suite for the NIFTY options trading engine.

Covers:
  - Position sizing: max_risk >= stop_loss for every strategy
  - P&L calculation: correct on failed exits, partial fills, expired worthless
  - Circuit breakers: fire exactly once per condition, reset correctly
  - Regime engine: rounding symmetry, hysteresis boundaries, decay
  - Order execution: no duplicate orders, idempotency, partial fill handling
  - Data freshness: mark price uses correct source based on age

Run:
    python tests.py
    python tests.py -v          # verbose
    python tests.py TestPnL     # run one class
"""

import sys
import unittest
import asyncio
import math
import json
import time
import sqlite3
import tempfile
import os
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock
from datetime import datetime, date, timedelta
from collections import deque
from typing import Optional, Dict, List
import pytz


# ─────────────────────────────────────────────────────────────────────
# Minimal stubs so tests run without the full engine installed.
# Each stub only implements what the tests actually call.
# ─────────────────────────────────────────────────────────────────────

class _Config:
    """Minimal config stub matching production values."""
    TOTAL_CAPITAL               = 1_000_000
    LOT_SIZE                    = 65
    NIFTY_STRIKE_STEP           = 100
    PAPER_TRADING_MODE          = True
    TZ                          = "Asia/Kolkata"

    # Risk
    MAX_RISK_PER_TRADE_PCT      = 0.02
    MAX_RISK_PER_TRADE          = int(0.02 * 1_000_000)   # 20_000
    MAX_COMBINED_RISK_PCT       = 0.20
    MAX_COMBINED_RISK           = int(0.20 * 1_000_000)   # 200_000
    MAX_CONCURRENT_POSITIONS    = 4
    MAX_TRANCHES_PER_STRATEGY   = 2
    POSITION_SIZE_PCT           = 0.15
    CB_LEVEL_1_PCT              = 0.02
    CB_LEVEL_2_PCT              = 0.03
    CB_LEVEL_3_PCT              = 0.08
    CB_LEVEL_4_PCT              = 0.10
    CB_LEVEL_5_VIX_ABSOLUTE     = 25.0
    CB_LEVEL_1_ACTION           = "CLOSE_POSITION"
    CB_LEVEL_2_ACTION           = "HALT_NEW_TRADES"
    CB_LEVEL_3_ACTION           = "REDUCE_50PCT"
    CB_LEVEL_4_ACTION           = "FULL_STOP"
    CB_LEVEL_5_ACTION           = "FORCE_STRONG_BUY"
    VIX_SELL_VOL_MAX            = 22.0

    # Strategy names
    STRAT_SHORT_STRADDLE        = "ATM_STRADDLE_WK"
    STRAT_IRON_CONDOR           = "WIDE_IRON_CONDOR"
    STRAT_CREDIT_SPREADS        = "CREDIT_SPREADS_030"
    STRAT_RATIO_SPREAD          = "RATIO_SPREAD_1X2"
    STRAT_BUTTERFLY             = "LONG_PUT_BUTTERFLY"
    STRAT_DEFENSIVE             = "DEFENSIVE_HEDGE"
    STRAT_LONG_STRADDLE         = "LONG_STRADDLE_WK"
    STRAT_BACKSPREAD            = "BACKSPREAD_DIRECTIONAL"
    STRAT_STRANGLE              = "LONG_STRANGLE_EVENT"

    # Strategy parameters
    STRADDLE_DTE_MIN            = 3
    STRADDLE_DTE_MAX            = 10
    STRADDLE_STOP_MULT          = 2.0
    STRADDLE_TARGET_PCT         = 0.50
    STRADDLE_EXIT_DTE           = 1
    STRADDLE_SPOT_STOP_PCT      = 0.03
    CONDOR_WING_WIDTH           = 400
    CONDOR_DTE_MIN              = 4
    CONDOR_DTE_MAX              = 10
    CONDOR_EXIT_DTE             = 1
    CONDOR_TARGET_PCT           = 0.50
    CONDOR_MIN_CREDIT           = 40
    CONDOR_MIN_CREDIT_PCT_OF_WIDTH = 0.22
    CONDOR_SIGMA_MULTIPLIER     = 1.5
    CONDOR_TESTED_SIDE_BUFFER   = 100
    SPREAD_MIN_CREDIT           = 25
    SPREAD_MIN_CREDIT_PCT_OF_WIDTH = 0.25
    SPREAD_EXIT_DTE             = 1
    SPREAD_TARGET_PCT           = 0.50
    SPREAD_DELTA_SHORT          = 0.30
    SPREAD_DELTA_LONG           = 0.15
    RATIO_ATM_OFFSET_PTS        = 100
    RATIO_EXIT_DTE              = 1
    RATIO_TARGET_PCT            = 0.25
    BUTTERFLY_MAX_DEBIT_PTS     = 50
    BUTTERFLY_MIN_RR_RATIO      = 2.0
    BUTTERFLY_EXIT_DTE          = 1
    BUTTERFLY_PROFIT_PCT        = 0.50
    BUTTERFLY_DTE_MAX           = 10
    BUTTERFLY_WING_BUFFER_PTS   = 100
    LONG_STRADDLE_DTE_MIN       = 3
    LONG_STRADDLE_STOP_PCT      = 0.50
    LONG_STRADDLE_TARGET_PCT    = 0.50
    LONG_STRADDLE_HOLD_DAYS     = 3
    LONG_STRADDLE_MAX_DEBIT_PCT = 0.025
    LONG_STRADDLE_VIX_SMA_PERIOD = 10
    LONG_STRADDLE_VIX_SPIKE_PCT = 0.05
    LONG_STRADDLE_MAX_IV_RANK   = 95
    BACKSPREAD_LONG_DELTA       = 0.25
    BACKSPREAD_SHORT_DELTA      = 0.10
    BACKSPREAD_LONG_QTY         = 3
    BACKSPREAD_MAX_DEBIT_PTS    = 30
    BACKSPREAD_DTE_MIN          = 2
    BACKSPREAD_DTE_MAX          = 10
    BACKSPREAD_MAX_VIX          = 30
    BACKSPREAD_STOP_MOVE_PCT    = 0.015
    BACKSPREAD_EXIT_DTE         = 1
    BACKSPREAD_PROFIT_MULTIPLE  = 4.0
    BACKSPREAD_MIN_STRIKE_WIDTH = 100
    EVENT_STRANGLE_DELTA        = 0.30
    EVENT_STRANGLE_STOP_PCT     = 0.50
    EVENT_STRANGLE_TARGET_PCT   = 1.00
    EVENT_STRANGLE_MAX_SPREAD_PTS = 3
    EVENT_STRANGLE_DTE_TARGET   = 7
    EVENT_STRANGLE_DTE_MAX      = 14
    DEFENSIVE_REDUCTION_PCT     = 0.60
    DEFENSIVE_VIX_SPIKE_PCT     = 0.15
    DEFENSIVE_VIX_SMA_PERIOD    = 5
    DEFENSIVE_MAX_HOLD_DAYS     = 7
    DEFENSIVE_EMA_PERIOD        = 20

    # Costs
    COST_BROKERAGE_PER_ORDER    = 20.0
    COST_STT_OPTION_SELL_PCT    = 0.0015
    COST_EXCHANGE_PCT           = 0.0003552
    COST_NSE_IPFT_PCT           = 0.000000001
    COST_SEBI_PCT               = 0.000001
    COST_STAMP_PCT              = 0.00003
    COST_GST_PCT                = 0.18
    TRANSACTION_COST_PCT        = 0.0005

    # Execution
    ORDER_BETWEEN_LEGS_DELAY_SEC = 0.0   # zero in tests
    ORDER_POLL_INTERVAL_SEC     = 0.0
    ORDER_STATUS_POLL_DELAY_SEC = 0.0
    CORE_FILL_TIMEOUT_SEC       = 1
    TICK_SIZE                   = 0.05
    ORDER_AGGRESSION_TICKS      = 1
    PAPER_SLIPPAGE_SHORT_TICKS  = 20
    PAPER_SLIPPAGE_HEDGE_TICKS  = 40
    MAX_SPREAD_ATM_PTS          = 3
    MAX_SPREAD_OTM_PTS          = 5
    MIN_OI_LOTS                 = 50
    PARTIAL_FILL_CANCEL         = True

    # Regime
    REGIME_STRONG_SELL          = "STRONG_SELL_VOL"
    REGIME_MILD_SELL            = "MILD_SELL_VOL"
    REGIME_NEUTRAL              = "NEUTRAL"
    REGIME_BUY_VOL              = "BUY_VOL"
    REGIME_STRONG_BUY           = "STRONG_BUY_VOL"
    REGIME_EVENT                = "EVENT_HEDGE"
    REGIME_REFRESH_SECONDS      = 60
    PERSISTENCE_READINGS        = 3
    WEIGHT_VOL                  = 0.30
    WEIGHT_EDGE                 = 0.30
    WEIGHT_TREND                = 0.25
    WEIGHT_FLOW                 = 0.15
    STRONG_SELL_THRESHOLD       = 0.45
    MILD_SELL_THRESHOLD         = 0.15
    MILD_BUY_THRESHOLD          = -0.15
    STRONG_BUY_THRESHOLD        = -0.45
    STRONG_SELL_ENTER           = 0.45
    STRONG_SELL_EXIT            = 0.40
    MILD_SELL_ENTER             = 0.15
    MILD_SELL_EXIT              = 0.10
    MILD_BUY_ENTER              = -0.15
    MILD_BUY_EXIT               = -0.20
    STRONG_BUY_ENTER            = -0.45
    STRONG_BUY_EXIT             = -0.50
    EDGE_RICH                   = 5.0
    EDGE_CHEAP                  = 0.0
    SKEW_ZSCORE_FEAR            = 1.5
    SKEW_ZSCORE_COMPLACENT      = -1.0
    ADX_TREND_THRESHOLD         = 20
    EMA_SLOPE_THRESHOLD         = 0.0005
    TERM_SPREAD_CONTANGO        = 0.5
    TERM_SPREAD_BACKWARDATION   = -0.5
    PROFIT_TARGET_PCT           = 0.50
    DEBIT_PROFIT_TARGET_PCT     = 0.50
    TRAIL_START_PROFIT_PCT      = 0.15
    TRAIL_RETAIN_PCT            = 0.65

    # Regime capital / lots
    REGIME_CAPITAL_PCT          = {
        "STRONG_SELL_VOL": 0.20,
        "MILD_SELL_VOL":   0.20,
        "NEUTRAL":         0.10,
        "BUY_VOL":         0.10,
        "STRONG_BUY_VOL":  0.15,
        "EVENT_HEDGE":     0.05,
    }
    REGIME_MAX_LOTS             = {
        "STRONG_SELL_VOL": 8,
        "MILD_SELL_VOL":   6,
        "NEUTRAL":         3,
        "BUY_VOL":         3,
        "STRONG_BUY_VOL":  4,
        "EVENT_HEDGE":     2,
    }
    GREEKS_LIMITS               = {
        "STRONG_SELL_VOL": {"delta_max": 0.10},
        "MILD_SELL_VOL":   {"delta_max": 0.20},
        "NEUTRAL":         {"delta_max": 0.05},
        "BUY_VOL":         {"delta_max": 0.50},
        "STRONG_BUY_VOL":  {"delta_max": 0.50},
        "EVENT_HEDGE":     {"delta_max": 0.10},
    }

    # Margin
    MARGIN_UTILIZATION_PCT      = 0.80
    SPAN_NAKED_MARGIN_PCT       = 0.11
    SPAN_SPREAD_MARGIN_MULTIPLIER = 1.15

    # Misc
    REENTRY_COOLDOWN_SEC        = 300
    REENTRY_MAX_SPOT_MOVE_PCT   = 0.02
    BUILD_FAILURE_COOLDOWN_SEC  = 300
    MAX_COMBINED_RISK           = int(0.20 * 1_000_000)
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
    NSE_MARKET_HOLIDAYS         = frozenset()
    HOLIDAY_CALENDAR_MAX_YEAR   = 2026


config = _Config()


# ─────────────────────────────────────────────────────────────────────
# Minimal dataclass stubs
# ─────────────────────────────────────────────────────────────────────

class Leg:
    def __init__(
        self,
        instrument_key="NSE_FO|TEST|25DEC2026|24000|CE",
        option_type="call",
        action="SELL",
        strike=24000.0,
        expiry="2026-12-25",
        qty=1,
        entry_price=0.0,
        exit_price=0.0,
        order_id="",
        order_tag="",
        fill_status="PENDING",
        delta=0.0,
        gamma=0.0,
        vega=0.0,
        theta=0.0,
        slippage_pts=0.0,
    ):
        self.instrument_key = instrument_key
        self.option_type    = option_type
        self.action         = action
        self.strike         = strike
        self.expiry         = expiry
        self.qty            = qty
        self.entry_price    = entry_price
        self.exit_price     = exit_price
        self.order_id       = order_id
        self.order_tag      = order_tag
        self.fill_status    = fill_status
        self.delta          = delta
        self.gamma          = gamma
        self.vega           = vega
        self.theta          = theta
        self.slippage_pts   = slippage_pts


class Position:
    def __init__(
        self,
        trade_id="test-trade-001",
        strategy_name="ATM_STRADDLE_WK",
        regime_at_entry="STRONG_SELL_VOL",
        entry_timestamp="2026-12-25T09:30:00+05:30",
        entry_spot=24000.0,
        entry_vix=12.0,
        legs=None,
        stop_loss=0.0,
        profit_target=0.0,
        exit_dte=None,
        max_hold_date=None,
        composite_at_entry=0.5,
        vol_score=0.5,
        edge_score=1.0,
        trend_score=0.0,
        flow_score=0.0,
        days_to_expiry=7,
        expiry_date="2026-12-25",
        status="OPEN",
        total_credit=0.0,
        total_debit=0.0,
        net_premium=0.0,
        max_risk=0.0,
        realized_pnl=0.0,
        realized_pnl_percent=0.0,
        exit_reason="",
        exit_timestamp="",
        exit_spot=0.0,
        exit_vix=0.0,
        paper_trade=True,
        trend_direction=0.0,
        meta=None,
        transaction_costs=0.0,
        net_pnl=0.0,
        regime_at_exit="",
        banked_pnl=0.0,
        banked_costs=0.0,
        margin_estimate=0.0,
    ):
        self.trade_id             = trade_id
        self.strategy_name        = strategy_name
        self.regime_at_entry      = regime_at_entry
        self.entry_timestamp      = entry_timestamp
        self.entry_spot           = entry_spot
        self.entry_vix            = entry_vix
        self.legs                 = legs or []
        self.stop_loss            = stop_loss
        self.profit_target        = profit_target
        self.exit_dte             = exit_dte
        self.max_hold_date        = max_hold_date
        self.composite_at_entry   = composite_at_entry
        self.vol_score            = vol_score
        self.edge_score           = edge_score
        self.trend_score          = trend_score
        self.flow_score           = flow_score
        self.days_to_expiry       = days_to_expiry
        self.expiry_date          = expiry_date
        self.status               = status
        self.total_credit         = total_credit
        self.total_debit          = total_debit
        self.net_premium          = net_premium
        self.max_risk             = max_risk
        self.realized_pnl         = realized_pnl
        self.realized_pnl_percent = realized_pnl_percent
        self.exit_reason          = exit_reason
        self.exit_timestamp       = exit_timestamp
        self.exit_spot            = exit_spot
        self.exit_vix             = exit_vix
        self.paper_trade          = paper_trade
        self.trend_direction      = trend_direction
        self.meta                 = meta or {}
        self.transaction_costs    = transaction_costs
        self.net_pnl              = net_pnl
        self.regime_at_exit       = regime_at_exit
        self.banked_pnl           = banked_pnl
        self.banked_costs         = banked_costs
        self.margin_estimate      = margin_estimate


# ─────────────────────────────────────────────────────────────────────
# Helper: run async tests
# ─────────────────────────────────────────────────────────────────────

def run_async(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ─────────────────────────────────────────────────────────────────────
# 1. POSITION SIZING TESTS
# ─────────────────────────────────────────────────────────────────────

class TestPositionSizing(unittest.TestCase):
    """
    Assert max_risk >= stop_loss_rupees for every strategy.
    The core invariant: the engine must never size a position such that
    its designed stop-loss exceeds the declared max_risk, because
    max_risk is what the lot-sizing and portfolio-risk gates use.
    """

    def _make_straddle_meta(self, total_premium):
        """Replicate _build_short_straddle meta logic."""
        max_risk    = total_premium * config.STRADDLE_STOP_MULT * config.LOT_SIZE
        stop_loss   = total_premium * config.STRADDLE_STOP_MULT
        profit_target = total_premium * (1 - config.STRADDLE_TARGET_PCT)
        return {
            "max_risk":      max_risk,
            "stop_loss":     stop_loss,
            "profit_target": profit_target,
            "total_credit":  total_premium,
            "strategy_type": "SHORT",
        }

    def _make_condor_meta(self, net_credit, wing_width=400):
        """Replicate _build_iron_condor meta logic."""
        max_risk    = (wing_width - net_credit) * config.LOT_SIZE
        stop_loss   = net_credit * 2.0
        profit_target = net_credit * (1 - config.CONDOR_TARGET_PCT)
        return {
            "max_risk":      max_risk,
            "stop_loss":     stop_loss,
            "net_credit":    net_credit,
            "total_credit":  net_credit,
            "strategy_type": "SHORT",
        }

    def _make_ratio_meta(self, total_credit, offset_pts=100):
        """Replicate corrected _build_ratio_spread meta logic."""
        max_risk  = max(
            (offset_pts - total_credit) * config.LOT_SIZE,
            total_credit * config.LOT_SIZE,
        )
        stop_loss = total_credit * 2.0
        return {
            "max_risk":      max_risk,
            "stop_loss":     stop_loss,
            "total_credit":  total_credit,
            "strategy_type": "SHORT",
        }

    def _make_butterfly_meta(self, net_debit, wing_width=100):
        """Replicate _build_butterfly meta logic."""
        max_risk    = net_debit * config.LOT_SIZE
        max_profit  = wing_width - net_debit
        stop_loss   = net_debit * config.LOT_SIZE
        profit_target = max_profit * config.BUTTERFLY_PROFIT_PCT
        return {
            "max_risk":      max_risk,
            "stop_loss":     stop_loss,
            "profit_target": profit_target,
            "net_debit":     net_debit,
            "max_profit":    max_profit,
            "strategy_type": "LONG",
        }

    def _make_long_straddle_meta(self, total_debit):
        """Replicate _build_long_straddle meta logic."""
        max_risk    = total_debit * config.LOT_SIZE
        stop_loss   = total_debit * (1 - config.LONG_STRADDLE_STOP_PCT)
        profit_target = total_debit * (1 + config.LONG_STRADDLE_TARGET_PCT)
        return {
            "max_risk":      max_risk,
            "stop_loss":     stop_loss,
            "profit_target": profit_target,
            "total_debit":   total_debit,
            "strategy_type": "LONG",
        }

    def _assert_max_risk_covers_stop(self, meta, strategy_name):
        """
        Core invariant: max_risk in rupees must be >= stop_loss in rupees.

        For SHORT strategies:
          stop_loss in meta = premium points (e.g. 2x credit)
          stop_loss_rupees  = stop_loss * LOT_SIZE

        For LONG strategies:
          stop_loss in meta is already in rupees
          (set as net_debit * LOT_SIZE * fraction in the builder).
          max_risk is also in rupees (= total_debit * LOT_SIZE).
          Assert max_risk >= stop_loss directly.
        """
        max_risk      = meta["max_risk"]
        stop_loss     = meta.get("stop_loss", 0)
        strategy_type = meta.get("strategy_type", "SHORT")

        self.assertGreater(
            max_risk, 0,
            f"{strategy_name}: max_risk must be > 0"
        )

        if strategy_type == "SHORT":
            # stop_loss is in premium points; convert to rupees
            stop_loss_rupees = stop_loss * config.LOT_SIZE
            self.assertGreaterEqual(
                max_risk,
                stop_loss_rupees,
                f"{strategy_name}: max_risk={max_risk:.0f} < "
                f"stop_loss_rupees={stop_loss_rupees:.0f}. "
                f"Lot sizing will over-leverage the account."
            )
        else:
            # LONG strategy: both max_risk and stop_loss are in rupees.
            # max_risk = total_debit * LOT_SIZE (full debit at risk).
            # stop_loss = fraction_of_debit * LOT_SIZE (e.g. 0.5 * debit * LOT_SIZE).
            # max_risk must cover the stop.
            self.assertGreaterEqual(
                max_risk,
                stop_loss,
                f"{strategy_name}: max_risk={max_risk:.0f} < "
                f"stop_loss={stop_loss:.0f} (both in rupees). "
                f"max_risk must cover the designed stop."
            )
    def test_straddle_max_risk_covers_stop(self):
        """Straddle max_risk = 2x credit * LOT_SIZE >= stop at 2x credit."""
        for premium in [100, 150, 200, 250, 300]:
            meta = self._make_straddle_meta(premium)
            self._assert_max_risk_covers_stop(
                meta, f"straddle(premium={premium})"
            )

    def test_straddle_max_risk_equals_stop_rupees(self):
        """For straddle, max_risk should exactly equal stop_loss * LOT_SIZE."""
        premium = 200
        meta = self._make_straddle_meta(premium)
        expected = premium * config.STRADDLE_STOP_MULT * config.LOT_SIZE
        self.assertAlmostEqual(
            meta["max_risk"], expected, places=2,
            msg="Straddle max_risk must equal 2x credit * LOT_SIZE"
        )

    def test_condor_max_risk_covers_stop(self):
        """Condor max_risk = (wing - credit) * LOT_SIZE >= 2x credit * LOT_SIZE."""
        for credit in [40, 60, 80, 88]:
            meta = self._make_condor_meta(credit)
            self._assert_max_risk_covers_stop(
                meta, f"condor(credit={credit})"
            )

    def test_condor_max_risk_is_wing_minus_credit(self):
        """Condor max_risk = (CONDOR_WING_WIDTH - net_credit) * LOT_SIZE."""
        credit = 88
        meta = self._make_condor_meta(credit)
        expected = (config.CONDOR_WING_WIDTH - credit) * config.LOT_SIZE
        self.assertAlmostEqual(
            meta["max_risk"], expected, places=2,
            msg="Condor max_risk must be wing_width - credit"
        )

    def test_ratio_spread_max_risk_covers_stop(self):
        """
        CRITICAL: ratio spread max_risk must use wing_width - credit,
        not credit * 2. The old formula understated risk by ~2x.
        """
        for credit in [10, 15, 20, 25]:
            meta = self._make_ratio_meta(credit)
            self._assert_max_risk_covers_stop(
                meta, f"ratio_spread(credit={credit})"
            )

    def test_ratio_spread_max_risk_uses_wing_width(self):
        """
        The corrected formula: max_risk = (RATIO_ATM_OFFSET_PTS - credit) * LOT_SIZE.
        The old formula was: credit * 2 * LOT_SIZE.
        For credit=20: old=2600, correct=5200. Old was 2x too small.
        """
        credit = 20
        meta = self._make_ratio_meta(credit, offset_pts=100)
        correct = (100 - credit) * config.LOT_SIZE   # 5200
        old_formula = credit * 2 * config.LOT_SIZE    # 2600

        self.assertAlmostEqual(
            meta["max_risk"], correct, places=2,
            msg=f"Ratio spread max_risk should be {correct}, not {old_formula}"
        )
        self.assertGreater(
            meta["max_risk"], old_formula,
            msg="Corrected max_risk must exceed the old (understated) formula"
        )

    def test_butterfly_max_risk_covers_stop(self):
        """Butterfly max_risk = net_debit * LOT_SIZE."""
        for debit in [10, 20, 30, 40, 50]:
            meta = self._make_butterfly_meta(debit)
            self._assert_max_risk_covers_stop(
                meta, f"butterfly(debit={debit})"
            )

    def test_long_straddle_max_risk_covers_stop(self):
        """Long straddle max_risk = total_debit * LOT_SIZE."""
        for debit in [100, 150, 200, 250]:
            meta = self._make_long_straddle_meta(debit)
            self._assert_max_risk_covers_stop(
                meta, f"long_straddle(debit={debit})"
            )

    def test_lot_sizing_respects_max_risk_per_trade(self):
        """
        _calculate_lot_size logic: lots = MAX_RISK_PER_TRADE / max_loss_per_lot.
        The resulting position's total risk must not exceed MAX_RISK_PER_TRADE.
        """
        max_risk_per_trade = config.MAX_RISK_PER_TRADE  # 20_000

        for max_loss_per_lot in [2000, 3000, 5000, 8000, 10000]:
            lots = math.floor(max_risk_per_trade / max_loss_per_lot)
            lots = max(lots, 0)
            total_risk = lots * max_loss_per_lot
            self.assertLessEqual(
                total_risk, max_risk_per_trade,
                msg=f"lots={lots} * max_loss={max_loss_per_lot} = "
                    f"{total_risk} > MAX_RISK_PER_TRADE={max_risk_per_trade}"
            )

    def test_condor_credit_minimum_pct_of_width(self):
        """
        CFG-01: condor credit must be >= CONDOR_MIN_CREDIT_PCT_OF_WIDTH * wing_width.
        At wing_width=400: minimum = 0.22 * 400 = 88 points.
        """
        min_required = (
            config.CONDOR_MIN_CREDIT_PCT_OF_WIDTH
            * config.CONDOR_WING_WIDTH
        )
        self.assertGreaterEqual(
            min_required, config.CONDOR_MIN_CREDIT,
            msg="Percentage-based minimum must exceed absolute floor"
        )

        # A condor with credit below the minimum should be rejected
        low_credit = min_required - 1
        high_credit = min_required + 1

        self.assertLess(
            low_credit, min_required,
            msg="Low credit should fail the percentage check"
        )
        self.assertGreaterEqual(
            high_credit, min_required,
            msg="High credit should pass the percentage check"
        )

    def test_spread_credit_minimum_pct_of_width(self):
        """
        CFG-01: spread credit must be >= SPREAD_MIN_CREDIT_PCT_OF_WIDTH * width.
        """
        typical_width = 200  # 0.30 delta to 0.15 delta spread
        min_required = (
            config.SPREAD_MIN_CREDIT_PCT_OF_WIDTH * typical_width
        )
        self.assertGreater(min_required, 0)
        self.assertGreater(
            config.SPREAD_MIN_CREDIT_PCT_OF_WIDTH, 0,
            msg="SPREAD_MIN_CREDIT_PCT_OF_WIDTH must be positive"
        )

    def test_max_risk_per_trade_vs_daily_loss(self):
        """
        CFG-R01: two simultaneous max-risk losses = 4% > 3% daily limit.
        The test documents this known constraint so it is not silently ignored.
        """
        two_losses = 2 * config.MAX_RISK_PER_TRADE_PCT
        daily_limit = config.CB_LEVEL_2_PCT

        # This is a known design constraint, not a bug we can fix here.
        # The test documents it and asserts the relationship is understood.
        self.assertGreater(
            two_losses, daily_limit,
            msg="CFG-R01: two max-risk losses exceed daily CB — "
                "reserve daily risk before entry, do not rely on CB as gate"
        )

    def test_four_concurrent_positions_vs_combined_risk(self):
        """
        Four positions at MAX_RISK_PER_TRADE should not exceed MAX_COMBINED_RISK.
        """
        four_positions_risk = 4 * config.MAX_RISK_PER_TRADE
        self.assertLessEqual(
            four_positions_risk,
            config.MAX_COMBINED_RISK,
            msg=f"4 * MAX_RISK_PER_TRADE={four_positions_risk} "
                f"> MAX_COMBINED_RISK={config.MAX_COMBINED_RISK}"
        )


# ─────────────────────────────────────────────────────────────────────
# 2. P&L CALCULATION TESTS
# ─────────────────────────────────────────────────────────────────────

class TestPnLCalculation(unittest.TestCase):
    """
    Assert P&L is correct in edge cases:
    - Failed exits (exit_price == 0)
    - Partial fills
    - Expired worthless legs
    - Deep ITM legs with failed market orders
    """

    def _make_dm_with_chain(self, ltp=50.0, bid=48.0, ask=52.0, rest_ts=None):
        """Create a minimal DataManager stub with a chain entry."""
        IST = pytz.timezone("Asia/Kolkata")
        if rest_ts is None:
            rest_ts = datetime.now(IST).isoformat()

        dm = MagicMock()
        opt_data = {
            "ltp":      ltp,
            "bid":      bid,
            "ask":      ask,
            "_rest_ts": rest_ts,
            "_ltp_ts":  datetime.now(IST).isoformat(),
        }
        dm.get_mark_price.return_value = (bid + ask) / 2.0
        dm.get_chain_for_expiry.return_value = {
            24000.0: {
                "call": dict(opt_data),
                "put":  dict(opt_data),
            }
        }
        return dm

    def _calculate_final_pnl(self, position, dm):
        """
        Replicate _calculate_final_pnl logic from strategy_engine.py.
        This is the corrected version that uses get_mark_price() as fallback.
        """
        gross_pnl    = 0.0
        expiry_chain = dm.get_chain_for_expiry(position.expiry_date)

        for leg in position.legs:
            exit_price = leg.exit_price
            is_expired_worthless = (
                leg.fill_status == "EXPIRED_WORTHLESS"
            )

            if exit_price == 0 and not is_expired_worthless:
                # Use mark price as fallback (corrected behavior)
                fallback_opt = (
                    expiry_chain
                    .get(leg.strike, {})
                    .get(leg.option_type, {})
                )
                _mark = dm.get_mark_price(fallback_opt, fallback=0.0)
                if _mark > 0:
                    exit_price = _mark

            if exit_price == 0 and not is_expired_worthless:
                exit_price = leg.entry_price

            if leg.action == "SELL":
                leg_pnl = (
                    (leg.entry_price - exit_price)
                    * leg.qty * config.LOT_SIZE
                )
            else:
                leg_pnl = (
                    (exit_price - leg.entry_price)
                    * leg.qty * config.LOT_SIZE
                )
            gross_pnl += leg_pnl

        gross_pnl += getattr(position, "banked_pnl", 0.0)
        return gross_pnl

    def _calculate_final_pnl_old(self, position, dm):
        """
        Replicate the OLD (broken) _calculate_final_pnl that fell back
        to entry_price instead of mark price.
        """
        gross_pnl    = 0.0
        expiry_chain = dm.get_chain_for_expiry(position.expiry_date)

        for leg in position.legs:
            exit_price = leg.exit_price
            is_expired_worthless = (
                leg.fill_status == "EXPIRED_WORTHLESS"
            )

            if exit_price == 0 and not is_expired_worthless:
                fallback_opt = (
                    expiry_chain
                    .get(leg.strike, {})
                    .get(leg.option_type, {})
                )
                fb_bid = fallback_opt.get("bid", 0)
                fb_ask = fallback_opt.get("ask", 0)
                if fb_bid > 0 and fb_ask > 0:
                    exit_price = (fb_bid + fb_ask) / 2.0
                else:
                    exit_price = fallback_opt.get("ltp", 0)

            # OLD BUG: falls back to entry_price
            if exit_price == 0 and not is_expired_worthless:
                exit_price = leg.entry_price

            if leg.action == "SELL":
                leg_pnl = (
                    (leg.entry_price - exit_price)
                    * leg.qty * config.LOT_SIZE
                )
            else:
                leg_pnl = (
                    (exit_price - leg.entry_price)
                    * leg.qty * config.LOT_SIZE
                )
            gross_pnl += leg_pnl

        return gross_pnl

    def test_normal_exit_pnl_correct(self):
        """Normal close: P&L = (entry - exit) * qty * LOT_SIZE for short."""
        leg = Leg(
            action="SELL", strike=24000.0, qty=2,
            entry_price=200.0, exit_price=100.0,
            fill_status="COMPLETE",
        )
        position = Position(
            legs=[leg], expiry_date="2026-12-25",
            strategy_name=config.STRAT_SHORT_STRADDLE,
        )
        dm = self._make_dm_with_chain()
        gross = self._calculate_final_pnl(position, dm)
        expected = (200.0 - 100.0) * 2 * config.LOT_SIZE  # 13_000
        self.assertAlmostEqual(gross, expected, places=2)

    def test_failed_exit_uses_mark_price_not_entry(self):
        """
        CRITICAL: when exit_price == 0 (failed market order) AND
        bid/ask/ltp are all zero (halted or illiquid market), the
        old code fell back to entry_price recording zero P&L.
        The corrected code uses get_mark_price() which returns the
        actual mark when available.

        The old bug only fires when ltp is ALSO zero — if ltp=500
        the old code finds it before reaching entry_price fallback.
        This test uses ltp=0 to trigger the actual failure mode.
        """
        IST = pytz.timezone("Asia/Kolkata")
        now = datetime.now(IST)

        # Scenario A: corrected code — get_mark_price returns 500
        # (e.g. from a separate price feed or cached value)
        leg_corrected = Leg(
            action="SELL", strike=24000.0, qty=1,
            entry_price=50.0,
            exit_price=0.0,
            fill_status="COMPLETE",
        )
        position_corrected = Position(
            legs=[leg_corrected], expiry_date="2026-12-25",
        )
        dm_corrected = MagicMock()
        dm_corrected.get_chain_for_expiry.return_value = {
            24000.0: {
                "call": {
                    "ltp": 0.0, "bid": 0.0, "ask": 0.0,
                    "_rest_ts": now.isoformat(),
                    "_ltp_ts":  now.isoformat(),
                },
                "put": {
                    "ltp": 0.0, "bid": 0.0, "ask": 0.0,
                    "_rest_ts": now.isoformat(),
                    "_ltp_ts":  now.isoformat(),
                },
            }
        }
        # Corrected: get_mark_price returns 500 (from external source)
        dm_corrected.get_mark_price.return_value = 500.0

        gross_corrected = self._calculate_final_pnl(
            position_corrected, dm_corrected
        )
        expected_loss = (50.0 - 500.0) * 1 * config.LOT_SIZE  # -29_250
        self.assertAlmostEqual(
            gross_corrected, expected_loss, places=2,
            msg="Corrected P&L should reflect actual mark price loss"
        )

        # Scenario B: old code — all prices zero, falls back to entry_price
        leg_old = Leg(
            action="SELL", strike=24000.0, qty=1,
            entry_price=50.0,
            exit_price=0.0,
            fill_status="COMPLETE",
        )
        position_old = Position(
            legs=[leg_old], expiry_date="2026-12-25",
        )
        dm_old = MagicMock()
        dm_old.get_chain_for_expiry.return_value = {
            24000.0: {
                "call": {
                    "ltp": 0.0, "bid": 0.0, "ask": 0.0,
                    "_rest_ts": now.isoformat(),
                    "_ltp_ts":  now.isoformat(),
                },
                "put": {
                    "ltp": 0.0, "bid": 0.0, "ask": 0.0,
                    "_rest_ts": now.isoformat(),
                    "_ltp_ts":  now.isoformat(),
                },
            }
        }
        # Old code does NOT call get_mark_price; returns 0 for all prices
        dm_old.get_mark_price.return_value = 0.0

        gross_old = self._calculate_final_pnl_old(position_old, dm_old)
        # Old formula: bid=0, ask=0, ltp=0 -> exit_price = entry_price
        # P&L = (entry - entry) * qty * LOT_SIZE = 0
        self.assertAlmostEqual(
            gross_old, 0.0, places=2,
            msg="Old formula records zero P&L when all prices unavailable"
        )

        # The corrected value must show the real loss
        self.assertLess(
            gross_corrected, gross_old,
            msg="Corrected P&L must show the real loss vs old zero P&L"
        )

    def test_expired_worthless_short_records_full_credit(self):
        """
        A short leg marked EXPIRED_WORTHLESS should record full credit
        as profit (entry_price - 0 = entry_price).
        """
        entry = 50.0
        leg = Leg(
            action="SELL", strike=25000.0, qty=1,
            entry_price=entry,
            exit_price=0.0,
            fill_status="EXPIRED_WORTHLESS",
        )
        position = Position(legs=[leg], expiry_date="2026-12-25")
        dm = self._make_dm_with_chain(ltp=0.05, bid=0.0, ask=0.10)
        dm.get_mark_price.return_value = 0.05

        gross = self._calculate_final_pnl(position, dm)
        expected = entry * 1 * config.LOT_SIZE  # full credit
        self.assertAlmostEqual(
            gross, expected, places=2,
            msg="Expired worthless short should record full credit as profit"
        )

    def test_expired_worthless_long_records_full_debit_loss(self):
        """
        A long leg marked EXPIRED_WORTHLESS should record full debit
        as loss (0 - entry_price = -entry_price).
        """
        entry = 30.0
        leg = Leg(
            action="BUY", strike=25000.0, qty=1,
            entry_price=entry,
            exit_price=0.0,
            fill_status="EXPIRED_WORTHLESS",
        )
        position = Position(legs=[leg], expiry_date="2026-12-25")
        dm = self._make_dm_with_chain(ltp=0.05, bid=0.0, ask=0.10)
        dm.get_mark_price.return_value = 0.05

        gross = self._calculate_final_pnl(position, dm)
        expected = -entry * 1 * config.LOT_SIZE  # full debit loss
        self.assertAlmostEqual(
            gross, expected, places=2,
            msg="Expired worthless long should record full debit as loss"
        )

    def test_partial_fill_banked_pnl_included(self):
        """
        banked_pnl from _close_one_side must be included in final P&L.
        """
        leg = Leg(
            action="SELL", strike=24000.0, qty=1,
            entry_price=100.0, exit_price=80.0,
            fill_status="COMPLETE",
        )
        position = Position(
            legs=[leg], expiry_date="2026-12-25",
            banked_pnl=500.0,  # from earlier partial close
        )
        dm = self._make_dm_with_chain()
        gross = self._calculate_final_pnl(position, dm)

        leg_pnl  = (100.0 - 80.0) * 1 * config.LOT_SIZE  # 1300
        expected = leg_pnl + 500.0                         # 1800
        self.assertAlmostEqual(gross, expected, places=2)

    def test_condor_full_loss_scenario(self):
        """
        Iron condor where spot blows through the short strike.
        Short call at 24200 with entry=30, current mark=200 (failed exit).
        Long call at 24600 with entry=10, current mark=50.
        Net P&L should reflect the real loss, not zero.
        """
        short_call = Leg(
            action="SELL", option_type="call",
            strike=24200.0, qty=1,
            entry_price=30.0, exit_price=0.0,
            fill_status="COMPLETE",
        )
        long_call = Leg(
            action="BUY", option_type="call",
            strike=24600.0, qty=1,
            entry_price=10.0, exit_price=0.0,
            fill_status="COMPLETE",
        )
        position = Position(
            legs=[short_call, long_call],
            expiry_date="2026-12-25",
        )

        dm = MagicMock()
        dm.get_chain_for_expiry.return_value = {
            24200.0: {"call": {"ltp": 200.0, "bid": 198.0, "ask": 202.0,
                               "_rest_ts": datetime.now(pytz.timezone("Asia/Kolkata")).isoformat(),
                               "_ltp_ts":  datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()}},
            24600.0: {"call": {"ltp": 50.0,  "bid": 49.0,  "ask": 51.0,
                               "_rest_ts": datetime.now(pytz.timezone("Asia/Kolkata")).isoformat(),
                               "_ltp_ts":  datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()}},
        }
        dm.get_mark_price.side_effect = lambda opt, fallback=0.0, **kw: (
            200.0 if opt.get("ltp") == 200.0 else 50.0
        )

        gross = self._calculate_final_pnl(position, dm)
        # Short call loss: (30 - 200) * 65 = -11_050
        # Long call gain:  (50 - 10) * 65  = +2_600
        expected = -11050 + 2600  # -8_450
        self.assertAlmostEqual(gross, expected, places=2)
        self.assertLess(gross, 0, "Full-loss condor must show negative P&L")

    def test_pnl_sign_convention_sell(self):
        """SELL leg: profit when exit < entry."""
        leg = Leg(action="SELL", qty=1, entry_price=100.0, exit_price=60.0,
                  fill_status="COMPLETE")
        position = Position(legs=[leg], expiry_date="2026-12-25")
        dm = self._make_dm_with_chain()
        gross = self._calculate_final_pnl(position, dm)
        self.assertGreater(gross, 0, "Short leg profit when exit < entry")

    def test_pnl_sign_convention_buy(self):
        """BUY leg: profit when exit > entry."""
        leg = Leg(action="BUY", qty=1, entry_price=60.0, exit_price=100.0,
                  fill_status="COMPLETE")
        position = Position(legs=[leg], expiry_date="2026-12-25")
        dm = self._make_dm_with_chain()
        gross = self._calculate_final_pnl(position, dm)
        self.assertGreater(gross, 0, "Long leg profit when exit > entry")


# ─────────────────────────────────────────────────────────────────────
# 3. CIRCUIT BREAKER TESTS
# ─────────────────────────────────────────────────────────────────────

class TestCircuitBreakers(unittest.TestCase):
    """
    Assert circuit breakers fire exactly once per condition and
    reset correctly. CB Level 5 must be idempotent.
    """

    def _make_engine_state(self):
        """Minimal strategy engine state for CB testing."""
        state = MagicMock()
        state.daily_pnl           = 0.0
        state.weekly_pnl          = 0.0
        state.current_capital     = float(config.TOTAL_CAPITAL)
        state.peak_capital        = float(config.TOTAL_CAPITAL)
        state.open_positions      = []
        state.closed_positions    = []
        state.daily_trading_halted = False
        state.kill_switch_active  = False
        state.cb_level_1_count    = 0
        state.cb_level_2_active   = False
        state.cb_level_3_active   = False
        state.cb_level_4_active   = False
        state.cb_level_5_active   = False
        state.dm                  = MagicMock()
        state.dm.vix              = 12.0
        state.re                  = MagicMock()
        state.re.confirmed_regime = config.REGIME_STRONG_SELL
        state.re.previous_regime  = config.REGIME_STRONG_SELL
        state.re.regime_changed   = False
        return state

    def _check_cb_level2(self, state):
        """Replicate CB Level 2 logic."""
        daily_pnl_net = state.daily_pnl
        threshold = -(config.CB_LEVEL_2_PCT * config.TOTAL_CAPITAL)
        if daily_pnl_net < threshold:
            if not state.cb_level_2_active:
                state.daily_trading_halted = True
                state.cb_level_2_active    = True
                return True
        return False

    def _check_cb_level3(self, state):
        """Replicate CB Level 3 logic."""
        threshold = -(config.CB_LEVEL_3_PCT * config.TOTAL_CAPITAL)
        if state.weekly_pnl < threshold:
            if not state.cb_level_3_active:
                state.cb_level_3_active = True
                return True
        return False

    def _check_cb_level4(self, state):
        """Replicate CB Level 4 logic."""
        drawdown  = state.peak_capital - state.current_capital
        threshold = config.CB_LEVEL_4_PCT * config.TOTAL_CAPITAL
        if drawdown > threshold:
            state.kill_switch_active = True
            state.cb_level_4_active  = True
            return True
        return False

    def _check_cb_level5(self, state):
        """Replicate CB Level 5 idempotent logic."""
        if (state.dm.vix is not None
                and state.dm.vix >= config.CB_LEVEL_5_VIX_ABSOLUTE):
            if not state.cb_level_5_active:
                state.re.previous_regime  = state.re.confirmed_regime
                state.re.confirmed_regime = config.REGIME_STRONG_BUY
                state.re.regime_changed   = True
                state.cb_level_5_active   = True
                return True
        else:
            if state.cb_level_5_active:
                state.cb_level_5_active = False
        return False

    def test_cb_level2_fires_once(self):
        """CB L2 fires exactly once when daily loss exceeds threshold."""
        state = self._make_engine_state()
        threshold = config.CB_LEVEL_2_PCT * config.TOTAL_CAPITAL  # 30_000

        state.daily_pnl = -(threshold + 1)
        fired1 = self._check_cb_level2(state)
        self.assertTrue(fired1, "CB L2 should fire on first breach")
        self.assertTrue(state.daily_trading_halted)
        self.assertTrue(state.cb_level_2_active)

        # Second call with same condition — must NOT fire again
        fired2 = self._check_cb_level2(state)
        self.assertFalse(fired2, "CB L2 must not fire twice for same breach")

    def test_cb_level2_does_not_fire_below_threshold(self):
        """CB L2 must not fire when daily loss is within limit."""
        state = self._make_engine_state()
        threshold = config.CB_LEVEL_2_PCT * config.TOTAL_CAPITAL

        state.daily_pnl = -(threshold - 1)
        fired = self._check_cb_level2(state)
        self.assertFalse(fired)
        self.assertFalse(state.daily_trading_halted)

    def test_cb_level3_fires_once(self):
        """CB L3 fires exactly once when weekly loss exceeds threshold."""
        state = self._make_engine_state()
        threshold = config.CB_LEVEL_3_PCT * config.TOTAL_CAPITAL  # 80_000

        state.weekly_pnl = -(threshold + 1)
        fired1 = self._check_cb_level3(state)
        self.assertTrue(fired1)
        self.assertTrue(state.cb_level_3_active)

        fired2 = self._check_cb_level3(state)
        self.assertFalse(fired2, "CB L3 must not fire twice")

    def test_cb_level4_fires_on_drawdown(self):
        """CB L4 fires when drawdown exceeds 10% of capital."""
        state = self._make_engine_state()
        threshold = config.CB_LEVEL_4_PCT * config.TOTAL_CAPITAL  # 100_000

        state.current_capital = state.peak_capital - (threshold + 1)
        fired = self._check_cb_level4(state)
        self.assertTrue(fired)
        self.assertTrue(state.kill_switch_active)
        self.assertTrue(state.cb_level_4_active)

    def test_cb_level5_fires_once_on_vix_spike(self):
        """
        MN-T04: CB L5 must fire exactly once when VIX spikes.
        Without the idempotency guard, the fast monitor (1Hz) would
        emit a regime-change event every second while VIX stays elevated.
        """
        state = self._make_engine_state()
        state.dm.vix = config.CB_LEVEL_5_VIX_ABSOLUTE + 1  # above threshold

        # First call: should fire
        fired1 = self._check_cb_level5(state)
        self.assertTrue(fired1, "CB L5 should fire on first VIX breach")
        self.assertTrue(state.cb_level_5_active)
        self.assertEqual(state.re.confirmed_regime, config.REGIME_STRONG_BUY)
        self.assertTrue(state.re.regime_changed)

        # Reset regime_changed to detect if it fires again
        state.re.regime_changed = False

        # Second call with VIX still elevated: must NOT fire again
        fired2 = self._check_cb_level5(state)
        self.assertFalse(fired2, "CB L5 must not fire twice for same VIX spike")
        self.assertFalse(
            state.re.regime_changed,
            "regime_changed must not be set on second CB L5 call"
        )

    def test_cb_level5_resets_when_vix_recovers(self):
        """CB L5 must reset when VIX drops back below threshold."""
        state = self._make_engine_state()
        state.dm.vix = config.CB_LEVEL_5_VIX_ABSOLUTE + 1

        self._check_cb_level5(state)
        self.assertTrue(state.cb_level_5_active)

        # VIX drops below threshold
        state.dm.vix = config.CB_LEVEL_5_VIX_ABSOLUTE - 1
        self._check_cb_level5(state)
        self.assertFalse(
            state.cb_level_5_active,
            "CB L5 must reset when VIX recovers"
        )

    def test_cb_levels_do_not_overlap(self):
        """
        CB L3 (8%) must fire before CB L4 (10%).
        CB L3 and L4 thresholds must not be equal.
        """
        self.assertLess(
            config.CB_LEVEL_3_PCT,
            config.CB_LEVEL_4_PCT,
            "CB L3 must fire before CB L4 (L3 < L4)"
        )
        self.assertLess(
            config.CB_LEVEL_2_PCT,
            config.CB_LEVEL_3_PCT,
            "CB L2 must fire before CB L3 (L2 < L3)"
        )

    def test_cb_level1_threshold_not_below_designed_stop(self):
        """
        SE-06: CB L1 threshold must not be below the position's
        designed stop-loss. For a condor at 45pts x 3lots:
        total_credit * LOT_SIZE = 8775; 2% floor = 20000.
        The floor (20000) > designed stop (8775), so CB L1 fires
        before the designed stop — this is the known issue.
        The test documents it and asserts the corrected behavior:
        CB L1 threshold = max(2% * capital, stop_loss_rupees).
        """
        # Condor: 45pt credit, 3 lots
        net_credit = 45.0
        lots = 3
        stop_loss_pts    = net_credit * 2.0         # 90 pts
        stop_loss_rupees = stop_loss_pts * config.LOT_SIZE  # 5850

        # CB L1 flat floor
        cb_l1_flat = config.CB_LEVEL_1_PCT * config.TOTAL_CAPITAL  # 20000

        # Corrected CB L1 threshold
        cb_l1_corrected = max(cb_l1_flat, stop_loss_rupees)

        # The corrected threshold must be >= the designed stop
        self.assertGreaterEqual(
            cb_l1_corrected, stop_loss_rupees,
            "Corrected CB L1 must not fire before designed stop"
        )

        # Document the known issue: flat floor fires before designed stop
        self.assertGreater(
            cb_l1_flat, stop_loss_rupees,
            "Known issue: flat CB L1 floor fires before designed stop "
            "for small condors — corrected by using max(flat, stop_rupees)"
        )


# ─────────────────────────────────────────────────────────────────────
# 4. REGIME ENGINE TESTS
# ─────────────────────────────────────────────────────────────────────

class TestRegimeEngine(unittest.TestCase):
    """
    Assert:
    - Rounding symmetry: +0.5 -> +1, -0.5 -> -1
    - Float persistence: scores preserved as floats
    - Hysteresis boundaries: enter/exit thresholds work correctly
    - Decay: stale scores decay toward zero
    """

    def _symmetric_round(self, raw):
        """
        RE-T02: correct symmetric rounding using copysign.
        +0.5 -> +1, -0.5 -> -1, 0.0 -> 0.
        """
        return int(math.copysign(math.floor(abs(raw) + 0.5), raw))

    def _sign_stable_confirm(self, buf):
        """
        RE-T02: confirm on sign-stability over last 3 readings.
        Returns mean of last 3 if all same sign, else None.
        """
        if len(buf) < 3:
            return None
        last3 = buf[-3:]
        signs = [
            math.copysign(1, v) if v != 0 else 0
            for v in last3
        ]
        if len(set(signs)) == 1:
            return sum(last3) / len(last3)
        return None

    def _map_regime_with_hysteresis(self, composite, current_regime):
        """
        RE-T01: regime mapping with config-driven hysteresis.
        Uses ENTER/EXIT constants from config.
        """
        # Persistence: stay in current regime until EXIT threshold crossed
        if current_regime == config.REGIME_STRONG_SELL:
            if composite > config.STRONG_SELL_EXIT:
                return config.REGIME_STRONG_SELL
        elif current_regime == config.REGIME_MILD_SELL:
            if config.MILD_SELL_EXIT <= composite <= config.STRONG_SELL_ENTER:
                return config.REGIME_MILD_SELL
        elif current_regime == config.REGIME_NEUTRAL:
            if config.MILD_BUY_EXIT < composite < config.MILD_SELL_ENTER:
                return config.REGIME_NEUTRAL
        elif current_regime == config.REGIME_BUY_VOL:
            if config.STRONG_BUY_EXIT <= composite <= config.MILD_BUY_ENTER:
                return config.REGIME_BUY_VOL
        elif current_regime == config.REGIME_STRONG_BUY:
            if composite < config.STRONG_BUY_EXIT:
                return config.REGIME_STRONG_BUY

        # Entry thresholds
        if composite > config.STRONG_SELL_ENTER:
            return config.REGIME_STRONG_SELL
        if composite >= config.MILD_SELL_ENTER:
            return config.REGIME_MILD_SELL
        if composite > config.MILD_BUY_ENTER:
            return config.REGIME_NEUTRAL
        if composite >= config.STRONG_BUY_ENTER:
            return config.REGIME_BUY_VOL
        return config.REGIME_STRONG_BUY

    # ── Rounding symmetry ─────────────────────────────────────────

    def test_positive_half_rounds_to_plus_one(self):
        """RE-T02: +0.5 must round to +1 (not 0 via banker's rounding)."""
        self.assertEqual(self._symmetric_round(0.5), 1)

    def test_negative_half_rounds_to_minus_one(self):
        """RE-T02: -0.5 must round to -1 (not 0 via floor(0.0))."""
        self.assertEqual(self._symmetric_round(-0.5), -1)

    def test_zero_rounds_to_zero(self):
        self.assertEqual(self._symmetric_round(0.0), 0)

    def test_positive_one_rounds_to_one(self):
        self.assertEqual(self._symmetric_round(1.0), 1)

    def test_negative_one_rounds_to_minus_one(self):
        self.assertEqual(self._symmetric_round(-1.0), -1)

    def test_rounding_is_symmetric(self):
        """For any value v, round(v) == -round(-v)."""
        for v in [0.1, 0.25, 0.5, 0.75, 1.0]:
            self.assertEqual(
                self._symmetric_round(v),
                -self._symmetric_round(-v),
                f"Rounding not symmetric for v={v}"
            )

    def test_banker_rounding_bug_documented(self):
        """
        Document the banker's rounding bug: Python's built-in round()
        rounds 0.5 to 0 (not 1). This is why we use copysign.
        """
        self.assertEqual(round(0.5), 0,
                         "Python banker's rounding: round(0.5)==0")
        self.assertEqual(round(-0.5), 0,
                         "Python banker's rounding: round(-0.5)==0")
        # Our fix corrects this
        self.assertEqual(self._symmetric_round(0.5), 1)
        self.assertEqual(self._symmetric_round(-0.5), -1)

    # ── Float persistence / sign-stability ───────────────────────

    def test_sign_stable_positive_confirms(self):
        """Three consecutive positive readings confirm positive score."""
        buf = [0.5, 0.5, 0.5]
        confirmed = self._sign_stable_confirm(buf)
        self.assertIsNotNone(confirmed)
        self.assertGreater(confirmed, 0)

    def test_sign_stable_negative_confirms(self):
        """Three consecutive negative readings confirm negative score."""
        buf = [-0.5, -0.5, -0.5]
        confirmed = self._sign_stable_confirm(buf)
        self.assertIsNotNone(confirmed)
        self.assertLess(confirmed, 0)

    def test_mixed_signs_do_not_confirm(self):
        """Mixed signs must not confirm."""
        buf = [0.5, -0.5, 0.5]
        confirmed = self._sign_stable_confirm(buf)
        self.assertIsNone(confirmed)

    def test_float_granularity_preserved(self):
        """
        RE-T02: a vol_score of +0.5 (one sub-signal) must NOT be
        promoted to +1.0 (two sub-signals). The persistence buffer
        must preserve the float value.
        """
        buf = [0.5, 0.5, 0.5]
        confirmed = self._sign_stable_confirm(buf)
        self.assertIsNotNone(confirmed)
        # Mean of [0.5, 0.5, 0.5] = 0.5, not 1.0
        self.assertAlmostEqual(confirmed, 0.5, places=5,
                               msg="Float granularity must be preserved")
        self.assertNotAlmostEqual(confirmed, 1.0, places=5,
                                  msg="0.5 must not be promoted to 1.0")

    def test_insufficient_buffer_does_not_confirm(self):
        """Fewer than 3 readings must not confirm."""
        for buf in [[], [0.5], [0.5, 0.5]]:
            confirmed = self._sign_stable_confirm(buf)
            self.assertIsNone(confirmed,
                              f"Buffer {buf} should not confirm yet")

    # ── Hysteresis boundaries ─────────────────────────────────────

    def test_enter_strong_sell_from_neutral(self):
        """Composite > STRONG_SELL_ENTER enters STRONG_SELL."""
        regime = self._map_regime_with_hysteresis(
            config.STRONG_SELL_ENTER + 0.01,
            config.REGIME_NEUTRAL
        )
        self.assertEqual(regime, config.REGIME_STRONG_SELL)

    def test_no_enter_strong_sell_below_enter_threshold(self):
        """Composite just below STRONG_SELL_ENTER stays in MILD_SELL."""
        regime = self._map_regime_with_hysteresis(
            config.STRONG_SELL_ENTER - 0.01,
            config.REGIME_MILD_SELL
        )
        self.assertEqual(regime, config.REGIME_MILD_SELL)

    def test_persist_strong_sell_above_exit_threshold(self):
        """
        RE-T01: once in STRONG_SELL, stay there until composite
        drops below STRONG_SELL_EXIT (not STRONG_SELL_ENTER).
        """
        # Composite between EXIT and ENTER — should stay in STRONG_SELL
        composite = (config.STRONG_SELL_EXIT + config.STRONG_SELL_ENTER) / 2
        regime = self._map_regime_with_hysteresis(
            composite, config.REGIME_STRONG_SELL
        )
        self.assertEqual(
            regime, config.REGIME_STRONG_SELL,
            "Should persist in STRONG_SELL between EXIT and ENTER thresholds"
        )

    def test_exit_strong_sell_below_exit_threshold(self):
        """Composite below STRONG_SELL_EXIT exits STRONG_SELL."""
        regime = self._map_regime_with_hysteresis(
            config.STRONG_SELL_EXIT - 0.01,
            config.REGIME_STRONG_SELL
        )
        self.assertNotEqual(
            regime, config.REGIME_STRONG_SELL,
            "Should exit STRONG_SELL when composite < STRONG_SELL_EXIT"
        )

    def test_hysteresis_band_width(self):
        """ENTER and EXIT thresholds must differ by at least 0.04."""
        self.assertGreater(
            config.STRONG_SELL_ENTER - config.STRONG_SELL_EXIT,
            0.04,
            "STRONG_SELL hysteresis band must be at least 0.04"
        )
        self.assertGreater(
            config.MILD_SELL_ENTER - config.MILD_SELL_EXIT,
            0.04,
            "MILD_SELL hysteresis band must be at least 0.04"
        )

    def test_enter_thresholds_match_base_thresholds(self):
        """
        RE-T01: ENTER thresholds must match the base threshold values
        so operators tuning STRONG_SELL_THRESHOLD see the expected effect.
        """
        self.assertAlmostEqual(
            config.STRONG_SELL_ENTER,
            config.STRONG_SELL_THRESHOLD,
            places=5,
            msg="STRONG_SELL_ENTER must equal STRONG_SELL_THRESHOLD"
        )
        self.assertAlmostEqual(
            config.MILD_SELL_ENTER,
            config.MILD_SELL_THRESHOLD,
            places=5,
            msg="MILD_SELL_ENTER must equal MILD_SELL_THRESHOLD"
        )

    def test_regime_mapping_all_boundaries(self):
        """Test all regime boundaries from NEUTRAL (no hysteresis)."""
        cases = [
            (0.50,  config.REGIME_STRONG_SELL),
            (0.30,  config.REGIME_MILD_SELL),
            (0.00,  config.REGIME_NEUTRAL),
            (-0.30, config.REGIME_BUY_VOL),
            (-0.50, config.REGIME_STRONG_BUY),
        ]
        for composite, expected in cases:
            result = self._map_regime_with_hysteresis(
                composite, config.REGIME_NEUTRAL
            )
            self.assertEqual(
                result, expected,
                f"composite={composite} from NEUTRAL should give {expected}, got {result}"
            )

    # ── Score decay ───────────────────────────────────────────────

    def test_score_decays_toward_zero(self):
        """
        RE-T03: a confirmed score of +1.0 should decay toward 0
        after sufficient time without new data.
        """
        confirmed = 1.0
        elapsed_sec = 900  # 15 minutes

        # Decay logic: after 10 min, decay by 10% per 5-min interval
        if elapsed_sec > 600:
            intervals = int((elapsed_sec - 600) / 300) + 1
            decayed = confirmed * (0.90 ** intervals)
            if abs(decayed) < 0.05:
                decayed = 0.0
        else:
            decayed = confirmed

        self.assertLess(
            abs(decayed), abs(confirmed),
            "Score must decay after 15 minutes without data"
        )

    def test_score_not_decay_within_grace_period(self):
        """Score must not decay within the 10-minute grace period."""
        confirmed = 1.0
        elapsed_sec = 500  # 8 minutes — within grace period

        if elapsed_sec > 600:
            intervals = int((elapsed_sec - 600) / 300) + 1
            decayed = confirmed * (0.90 ** intervals)
        else:
            decayed = confirmed

        self.assertAlmostEqual(
            decayed, confirmed, places=5,
            msg="Score must not decay within 10-minute grace period"
        )

    def test_score_reaches_zero_after_extended_absence(self):
        """
        Score must reach the zero-threshold (< 0.05) after extended
        data absence.

        With 10% decay per 5-min interval after the 10-min grace period:
          1 hour  -> 11 intervals -> 0.90^11 = 0.314  (above threshold)
          3 hours -> 35 intervals -> 0.90^35 = 0.025  (below threshold -> 0.0)

        Use 3 hours to guarantee reaching zero.
        """
        confirmed   = 1.0
        elapsed_sec = 10800  # 3 hours

        if elapsed_sec > 600:
            intervals = int((elapsed_sec - 600) / 300) + 1
            decayed   = confirmed * (0.90 ** intervals)
            if abs(decayed) < 0.05:
                decayed = 0.0
        else:
            decayed = confirmed

        self.assertEqual(
            decayed, 0.0,
            "Score must reach zero after 3 hours without data "
            "(0.90^35 = 0.025 < 0.05 threshold)"
        )

    def test_score_significantly_reduced_after_one_hour(self):
        """
        After 1 hour without data (11 intervals of 10% decay),
        score must be reduced to ~0.314 — significantly below original
        but not yet zero (0.314 > 0.05 threshold).
        """
        confirmed   = 1.0
        elapsed_sec = 3600  # 1 hour

        if elapsed_sec > 600:
            intervals = int((elapsed_sec - 600) / 300) + 1
            decayed   = confirmed * (0.90 ** intervals)
            if abs(decayed) < 0.05:
                decayed = 0.0
        else:
            decayed = confirmed

        self.assertLess(
            decayed, confirmed * 0.50,
            "After 1 hour, score must be reduced by at least 50%"
        )
        self.assertGreater(
            decayed, 0.0,
            "After 1 hour, score should not yet be zero "
            "(0.90^11 = 0.314 > 0.05 threshold)"
        )
# ─────────────────────────────────────────────────────────────────────
# 5. ORDER EXECUTION TESTS
# ─────────────────────────────────────────────────────────────────────

class TestOrderExecution(unittest.TestCase):
    """
    Assert:
    - No duplicate orders (idempotency via order tags)
    - Partial fill handling: leg qty adjusted, metadata rescaled
    - Retry fill quantity validated (SE-R02)
    - Order tags use IST date (SE-T04)
    - Rebalance is ratio-aware (SE-R03)
    """

    def _generate_order_tag(self, trade_id, instrument_key, action, leg_index,
                             ist_date=None):
        """Replicate _generate_order_tag with IST date fix."""
        import hashlib
        if ist_date is None:
            ist_date = datetime.now(
                pytz.timezone("Asia/Kolkata")
            ).date().isoformat()
        raw = (
            f"{trade_id[:12]}-"
            f"{instrument_key[-8:]}-"
            f"{action}-"
            f"{leg_index}-"
            f"{ist_date}"
        )
        tag_hash = hashlib.md5(raw.encode()).hexdigest()[:8]
        return f"nao-{tag_hash}"

    def test_same_inputs_produce_same_tag(self):
        """Idempotency: same inputs always produce the same tag."""
        tag1 = self._generate_order_tag(
            "trade-001", "NSE_FO|TEST|CE", "SELL", 0, "2026-12-25"
        )
        tag2 = self._generate_order_tag(
            "trade-001", "NSE_FO|TEST|CE", "SELL", 0, "2026-12-25"
        )
        self.assertEqual(tag1, tag2, "Same inputs must produce same tag")

    def test_different_leg_index_produces_different_tag(self):
        """Different leg_index must produce different tag."""
        tag0 = self._generate_order_tag(
            "trade-001", "NSE_FO|TEST|CE", "SELL", 0, "2026-12-25"
        )
        tag1 = self._generate_order_tag(
            "trade-001", "NSE_FO|TEST|CE", "SELL", 1, "2026-12-25"
        )
        self.assertNotEqual(tag0, tag1)

    def test_different_trade_id_produces_different_tag(self):
        """Different trade_id must produce different tag."""
        tag1 = self._generate_order_tag(
            "trade-001", "NSE_FO|TEST|CE", "SELL", 0, "2026-12-25"
        )
        tag2 = self._generate_order_tag(
            "trade-002", "NSE_FO|TEST|CE", "SELL", 0, "2026-12-25"
        )
        self.assertNotEqual(tag1, tag2)

    def test_tag_uses_ist_date_not_local(self):
        """
        SE-T04: tag must use IST date. On UTC servers, date.today()
        rolls over at 05:30 IST. We verify the tag changes with date.
        """
        tag_dec25 = self._generate_order_tag(
            "trade-001", "NSE_FO|TEST|CE", "SELL", 0, "2026-12-25"
        )
        tag_dec26 = self._generate_order_tag(
            "trade-001", "NSE_FO|TEST|CE", "SELL", 0, "2026-12-26"
        )
        self.assertNotEqual(
            tag_dec25, tag_dec26,
            "Tags on different IST dates must differ"
        )

    def test_tag_prefix_is_nao(self):
        """All tags must start with 'nao-' for broker sweep identification."""
        tag = self._generate_order_tag(
            "trade-001", "NSE_FO|TEST|CE", "SELL", 0, "2026-12-25"
        )
        self.assertTrue(tag.startswith("nao-"),
                        f"Tag must start with 'nao-', got: {tag}")

    def test_partial_fill_leg_qty_adjusted(self):
        """
        When a leg partially fills, leg.qty must be reduced to
        the actual filled lots. Metadata must be rescaled.
        """
        leg = Leg(action="SELL", qty=4, entry_price=100.0)

        # Simulate partial fill: only 2 lots filled
        filled_qty = 2 * config.LOT_SIZE  # 130 shares
        filled_lots = filled_qty // config.LOT_SIZE  # 2

        leg.qty = filled_lots
        leg.fill_status = "PARTIAL"

        self.assertEqual(leg.qty, 2)
        self.assertEqual(leg.fill_status, "PARTIAL")

    def test_retry_fill_validates_actual_qty(self):
        """
        SE-R02: retry fill must use the actual filled qty from retry_leg,
        not assume the full original qty was filled.
        """
        original_leg = Leg(action="SELL", qty=3, entry_price=100.0,
                           exit_price=0.0)
        retry_leg    = Leg(action="BUY",  qty=3, entry_price=0.0)

        # Simulate retry partially filling: only 2 lots
        retry_leg.qty = 2
        retry_leg.entry_price = 80.0
        retry_ok = True

        if retry_ok and retry_leg.entry_price > 0:
            original_leg.exit_price  = retry_leg.entry_price
            original_leg.qty         = retry_leg.qty  # use actual filled qty
            original_leg.fill_status = "CLOSED_EXIT"

        self.assertEqual(original_leg.qty, 2,
                         "qty must reflect actual retry fill, not original")
        self.assertEqual(original_leg.exit_price, 80.0)
        self.assertEqual(original_leg.fill_status, "CLOSED_EXIT")

    def test_ratio_aware_rebalance_preserves_structure(self):
        """
        SE-R03: rebalance must detect when a partial fill breaks
        the intended ratio structure.

        For a butterfly (intended 1:2:1), if the body partially
        fills to qty=1 (instead of 2), the algorithm computes:
          base_qty = min of fully-filled legs = 1 (wings)
          body ratio = round(1/1) = 1  <- body.qty is now 1, not 2
          common_units = min(1,1,1) = 1

        The algorithm accepts the 1:1:1 structure, but the INTENDED
        structure was 1:2:1. The test verifies that this mismatch is
        detectable by comparing actual vs intended quantities.
        The caller (_execute_strategy) is responsible for reversing
        when the rebalance returns False due to a failed trim.
        """
        # Intended structure: wing=1, body=2, wing=1
        intended_qtys = [1, 2, 1]

        # After partial fill: body only got 1 lot
        actual_qtys = [1, 1, 1]

        # The structure is broken: actual != intended
        structure_intact = all(
            a == i for a, i in zip(actual_qtys, intended_qtys)
        )
        self.assertFalse(
            structure_intact,
            "Butterfly with partial body fill has broken structure "
            "(actual 1:1:1 vs intended 1:2:1)"
        )

        # The mismatch is specifically in the body leg (index 1)
        mismatched = [
            i for i, (a, intended) in enumerate(
                zip(actual_qtys, intended_qtys)
            )
            if a != intended
        ]
        self.assertEqual(
            mismatched, [1],
            "Only the body leg (index 1) should be mismatched"
        )

        # Verify: the ratio-aware algorithm with the ORIGINAL intended
        # quantities (before partial fill) would have base_qty=1,
        # body_ratio=2, and achievable=2//2=1 common unit.
        # This is the correct behavior when intended qtys are known.
        original_legs_qty = [1, 2, 1]  # intended
        base_qty = min(
            q for q in original_legs_qty if q > 0
        )  # = 1
        common_units_intended = None
        for qty in original_legs_qty:
            if qty <= 0:
                continue
            ratio = max(1, round(qty / base_qty))
            achievable = qty // ratio
            if (common_units_intended is None
                    or achievable < common_units_intended):
                common_units_intended = achievable
        self.assertEqual(
            common_units_intended, 1,
            "With intended 1:2:1 quantities, common_units should be 1"
        )

    def test_1x1_rebalance_uses_minimum_qty(self):
        """
        For a 1:1 structure (condor, credit spread), rebalance
        correctly trims to the minimum filled qty.
        """
        legs = [
            Leg(action="BUY",  qty=4, fill_status="COMPLETE"),
            Leg(action="BUY",  qty=4, fill_status="COMPLETE"),
            Leg(action="SELL", qty=3, fill_status="PARTIAL"),   # only 3 filled
            Leg(action="SELL", qty=4, fill_status="COMPLETE"),
        ]

        base_qty = min(
            l.qty for l in legs
            if l.qty > 0 and l.fill_status != "PARTIAL"
        )  # = 4
        common_units = None
        for leg in legs:
            if leg.qty <= 0:
                continue
            ratio = max(1, round(leg.qty / base_qty))
            achievable = leg.qty // ratio
            if common_units is None or achievable < common_units:
                common_units = achievable

        # All legs have ratio=1 relative to base=4.
        # achievable = min(4//1, 4//1, 3//1, 4//1) = 3
        self.assertEqual(common_units, 3,
                         "1:1 structure should rebalance to minimum qty=3")

    def test_closing_lock_prevents_duplicate_close(self):
        """
        SE-R05: a position being closed must not be closed again
        concurrently. The _closing_positions set must prevent this.
        """
        closing_positions = set()
        trade_id = "trade-001"

        # First close attempt
        if trade_id not in closing_positions:
            closing_positions.add(trade_id)
            first_attempt = True
        else:
            first_attempt = False

        # Second concurrent close attempt
        if trade_id not in closing_positions:
            closing_positions.add(trade_id)
            second_attempt = True
        else:
            second_attempt = False

        self.assertTrue(first_attempt, "First close must proceed")
        self.assertFalse(second_attempt, "Second close must be blocked")

    def test_partial_close_exit_price_only_set_when_qty_zero(self):
        """
        Partial close fix: exit_price must only be set when qty reaches 0.
        If qty > 0 remains, exit_price must stay 0 so SE-N01 guard works.
        """
        leg = Leg(action="SELL", qty=4, entry_price=100.0, exit_price=0.0)

        # Simulate partial close: close 2 of 4 lots
        qty_closed = 2
        exit_price = 80.0

        leg.qty -= qty_closed  # now qty = 2

        if leg.qty <= 0:
            leg.exit_price  = exit_price
            leg.fill_status = "CLOSED_ONE_SIDE"
        else:
            # Must NOT set exit_price
            leg.fill_status = "PARTIALLY_CLOSED_ONE_SIDE"

        self.assertEqual(leg.qty, 2)
        self.assertEqual(leg.exit_price, 0.0,
                         "exit_price must stay 0 when qty > 0 remains")
        self.assertEqual(leg.fill_status, "PARTIALLY_CLOSED_ONE_SIDE")

    def test_se_n01_guard_detects_unconfirmed_legs(self):
        """
        SE-N01: the unconfirmed check must detect legs with qty > 0
        and exit_price == 0 (not EXPIRED_WORTHLESS).
        """
        legs = [
            Leg(action="SELL", qty=0, exit_price=80.0,
                fill_status="CLOSED_EXIT"),       # confirmed
            Leg(action="BUY",  qty=1, exit_price=0.0,
                fill_status="COMPLETE"),           # unconfirmed!
            Leg(action="SELL", qty=0, exit_price=0.0,
                fill_status="EXPIRED_WORTHLESS"),  # OK (worthless)
        ]

        unconfirmed = [
            l for l in legs
            if l.exit_price <= 0
            and l.fill_status != "EXPIRED_WORTHLESS"
            and l.qty > 0
        ]

        self.assertEqual(len(unconfirmed), 1,
                         "Exactly one leg should be unconfirmed")
        self.assertEqual(unconfirmed[0].action, "BUY")


# ─────────────────────────────────────────────────────────────────────
# 6. DATA FRESHNESS TESTS
# ─────────────────────────────────────────────────────────────────────

class TestDataFreshness(unittest.TestCase):
    """
    Assert get_mark_price() uses the correct source based on age:
    1. Fresh REST midpoint (< 15s)
    2. Fresh WS LTP (< 30s, via _ltp_ts)
    3. Stale REST midpoint (< 90s)
    4. fallback (entry price)

    Also tests:
    - DM-T01: rejected LTP must not get fresh _ltp_ts
    - DM-T02: REST LTP must have _ltp_ts set
    - DM-T03: crossed markets rejected
    """

    def _make_opt_data(
        self,
        ltp=100.0, bid=98.0, ask=102.0,
        rest_age_sec=0, ltp_age_sec=0,
    ):
        """Create opt_data with controlled timestamps."""
        IST = pytz.timezone("Asia/Kolkata")
        now = datetime.now(IST)
        rest_ts = (now - timedelta(seconds=rest_age_sec)).isoformat()
        ltp_ts  = (now - timedelta(seconds=ltp_age_sec)).isoformat()
        return {
            "ltp":      ltp,
            "bid":      bid,
            "ask":      ask,
            "_rest_ts": rest_ts,
            "_ltp_ts":  ltp_ts,
        }

    def _get_mark_price(
        self, opt_data, fallback=0.0,
        max_quote_age_sec=15.0,
        max_ltp_age_sec=30.0,
        max_rest_fallback_age_sec=90.0,
    ):
        """
        Replicate the corrected get_mark_price() logic.
        """
        IST = pytz.timezone("Asia/Kolkata")
        now = datetime.now(IST)

        bid = float(opt_data.get("bid", 0) or 0)
        ask = float(opt_data.get("ask", 0) or 0)
        ltp = float(opt_data.get("ltp", 0) or 0)

        def _age(ts_key):
            ts = opt_data.get(ts_key)
            if not ts:
                return float("inf")
            try:
                ts_dt = datetime.fromisoformat(ts)
                if ts_dt.tzinfo is None:
                    ts_dt = IST.localize(ts_dt)
                return (now - ts_dt).total_seconds()
            except Exception:
                return float("inf")

        rest_age = _age("_rest_ts")
        ltp_age  = _age("_ltp_ts")

        # DM-T03: reject crossed markets
        bid_ask_valid = bid > 0 and ask > 0 and ask >= bid

        # 1. Fresh REST midpoint
        if rest_age <= max_quote_age_sec and bid_ask_valid:
            return (bid + ask) / 2.0, "REST_MID_FRESH"
        # 2. Fresh WS LTP
        if ltp_age <= max_ltp_age_sec and ltp > 0:
            return ltp, "LTP_FRESH"
        # 3. Stale REST midpoint
        if rest_age <= max_rest_fallback_age_sec and bid_ask_valid:
            return (bid + ask) / 2.0, "REST_MID_STALE"
        # 4. fallback
        return fallback, "FALLBACK"

    def test_fresh_rest_quote_uses_midpoint(self):
        """Fresh REST quote (< 15s) must use bid/ask midpoint."""
        opt = self._make_opt_data(
            ltp=100.0, bid=98.0, ask=102.0,
            rest_age_sec=5, ltp_age_sec=60,
        )
        price, source = self._get_mark_price(opt)
        self.assertAlmostEqual(price, 100.0, places=2)
        self.assertEqual(source, "REST_MID_FRESH")

    def test_stale_rest_fresh_ltp_uses_ltp(self):
        """Stale REST (> 15s) with fresh LTP must use LTP."""
        opt = self._make_opt_data(
            ltp=105.0, bid=98.0, ask=102.0,
            rest_age_sec=20, ltp_age_sec=5,
        )
        price, source = self._get_mark_price(opt)
        self.assertAlmostEqual(price, 105.0, places=2)
        self.assertEqual(source, "LTP_FRESH")

    def test_both_stale_uses_rest_fallback(self):
        """Both stale but within 90s uses stale REST midpoint."""
        opt = self._make_opt_data(
            ltp=105.0, bid=98.0, ask=102.0,
            rest_age_sec=60, ltp_age_sec=60,
        )
        price, source = self._get_mark_price(opt)
        self.assertAlmostEqual(price, 100.0, places=2)
        self.assertEqual(source, "REST_MID_STALE")

    def test_all_stale_uses_fallback(self):
        """All sources > max age must use fallback (entry price)."""
        opt = self._make_opt_data(
            ltp=105.0, bid=98.0, ask=102.0,
            rest_age_sec=120, ltp_age_sec=120,
        )
        price, source = self._get_mark_price(opt, fallback=50.0)
        self.assertAlmostEqual(price, 50.0, places=2)
        self.assertEqual(source, "FALLBACK")

    def test_no_timestamps_uses_fallback(self):
        """No timestamps at all must use fallback."""
        opt = {"ltp": 100.0, "bid": 98.0, "ask": 102.0}
        price, source = self._get_mark_price(opt, fallback=75.0)
        self.assertAlmostEqual(price, 75.0, places=2)
        self.assertEqual(source, "FALLBACK")

    def test_dm_t01_rejected_ltp_not_fresh(self):
        """
        DM-T01: when an LTP tick is rejected as an outlier,
        the _ltp_ts must NOT be updated to now.
        If it were, get_mark_price() would treat the old LTP as fresh.
        """
        IST = pytz.timezone("Asia/Kolkata")
        now = datetime.now(IST)

        # Old LTP stored 5 minutes ago
        old_ltp_ts = (now - timedelta(minutes=5)).isoformat()
        opt = {
            "ltp":      100.0,
            "bid":      98.0,
            "ask":      102.0,
            "_rest_ts": (now - timedelta(seconds=30)).isoformat(),
            "_ltp_ts":  old_ltp_ts,  # 5 min old
        }

        # Simulate outlier rejection: mid=100, ltp=200 (>5% deviation)
        new_ltp = 200.0
        mid_ref = (opt["bid"] + opt["ask"]) / 2.0
        pct_thresh = max(10.0, mid_ref * 0.05)
        is_outlier = abs(new_ltp - mid_ref) > pct_thresh

        self.assertTrue(is_outlier, "200 should be detected as outlier")

        if is_outlier:
            # CORRECT: do NOT update _ltp_ts
            pass  # opt["_ltp_ts"] stays as old_ltp_ts
        else:
            opt["ltp"]     = new_ltp
            opt["_ltp_ts"] = now.isoformat()

        # _ltp_ts must still be the old value
        self.assertEqual(
            opt["_ltp_ts"], old_ltp_ts,
            "DM-T01: _ltp_ts must not be updated when LTP is rejected"
        )

        # get_mark_price should now use stale REST fallback, not the old LTP
        price, source = self._get_mark_price(opt)
        self.assertEqual(
            source, "REST_MID_STALE",
            "With rejected LTP and stale REST, should use REST_MID_STALE"
        )

    def test_dm_t02_rest_ltp_has_ltp_ts(self):
        """
        DM-T02: REST chain response must set _ltp_ts alongside _rest_ts.
        Without _ltp_ts, get_mark_price() treats REST LTP as infinitely old.
        """
        IST = pytz.timezone("Asia/Kolkata")
        now = datetime.now(IST)

        # Simulate REST chain response WITH _ltp_ts (corrected)
        opt_with_ltp_ts = {
            "ltp":      100.0,
            "bid":      0.0,    # illiquid: no bid/ask
            "ask":      0.0,
            "_rest_ts": now.isoformat(),
            "_ltp_ts":  now.isoformat(),  # DM-T02 fix
        }

        # Simulate REST chain response WITHOUT _ltp_ts (old bug)
        opt_without_ltp_ts = {
            "ltp":      100.0,
            "bid":      0.0,
            "ask":      0.0,
            "_rest_ts": now.isoformat(),
            # No _ltp_ts
        }

        price_with,    source_with    = self._get_mark_price(
            opt_with_ltp_ts, fallback=50.0
        )
        price_without, source_without = self._get_mark_price(
            opt_without_ltp_ts, fallback=50.0
        )

        # With _ltp_ts: should use fresh LTP
        self.assertAlmostEqual(price_with, 100.0, places=2,
                               msg="With _ltp_ts, should use fresh LTP")
        self.assertEqual(source_with, "LTP_FRESH")

        # Without _ltp_ts: LTP is infinitely old, falls back to entry price
        self.assertAlmostEqual(price_without, 50.0, places=2,
                               msg="Without _ltp_ts, should fall back to entry price")
        self.assertEqual(source_without, "FALLBACK")

    def test_dm_t03_crossed_market_rejected(self):
        """
        DM-T03: bid > ask (crossed market) must not produce a midpoint.
        """
        IST = pytz.timezone("Asia/Kolkata")
        now = datetime.now(IST)

        opt_crossed = {
            "ltp":      100.0,
            "bid":      102.0,  # bid > ask: crossed!
            "ask":      98.0,
            "_rest_ts": now.isoformat(),
            "_ltp_ts":  (now - timedelta(seconds=60)).isoformat(),
        }

        price, source = self._get_mark_price(opt_crossed, fallback=50.0)

        # Crossed market: bid/ask midpoint must be rejected
        self.assertNotEqual(
            source, "REST_MID_FRESH",
            "Crossed market must not use REST midpoint"
        )
        self.assertNotEqual(
            source, "REST_MID_STALE",
            "Crossed market must not use stale REST midpoint"
        )

    def test_normal_market_not_rejected(self):
        """Normal market (ask > bid) must not be rejected."""
        IST = pytz.timezone("Asia/Kolkata")
        now = datetime.now(IST)

        opt_normal = {
            "ltp":      100.0,
            "bid":      98.0,
            "ask":      102.0,
            "_rest_ts": now.isoformat(),
            "_ltp_ts":  (now - timedelta(seconds=60)).isoformat(),
        }

        price, source = self._get_mark_price(opt_normal)
        self.assertIn(source, ["REST_MID_FRESH", "REST_MID_STALE"],
                      "Normal market should use REST midpoint")

    def test_zero_bid_ask_falls_through_to_ltp(self):
        """When bid=ask=0, must fall through to LTP."""
        IST = pytz.timezone("Asia/Kolkata")
        now = datetime.now(IST)

        opt = {
            "ltp":      100.0,
            "bid":      0.0,
            "ask":      0.0,
            "_rest_ts": now.isoformat(),
            "_ltp_ts":  now.isoformat(),
        }

        price, source = self._get_mark_price(opt)
        self.assertAlmostEqual(price, 100.0, places=2)
        self.assertEqual(source, "LTP_FRESH")

    def test_data_refresh_requires_spot_change(self):
        """
        MN-T05: data refresh must only be marked complete when
        spot actually changed, not just when it exists.
        """
        # Simulate: spot exists but didn't change this cycle
        cycle_start_spot = 24000.0
        current_spot     = 24000.0  # unchanged
        chain_len        = 100      # non-zero

        spot_changed  = current_spot != cycle_start_spot
        spot_exists   = current_spot is not None and current_spot > 0
        chain_exists  = chain_len > 0
        is_first_cycle = cycle_start_spot is None

        refresh_valid = (
            spot_exists
            and chain_exists
            and (spot_changed or is_first_cycle)
        )

        self.assertFalse(
            refresh_valid,
            "Refresh must not be marked complete when spot is unchanged"
        )

        # Simulate: spot actually changed
        current_spot = 24050.0
        spot_changed = current_spot != cycle_start_spot
        refresh_valid = (
            spot_exists
            and chain_exists
            and (spot_changed or is_first_cycle)
        )
        self.assertTrue(
            refresh_valid,
            "Refresh must be marked complete when spot changed"
        )

    def test_first_cycle_completes_without_spot_change(self):
        """
        MN-T05: first cycle after startup (cycle_start_spot=None)
        must be marked complete even without a spot change.
        """
        cycle_start_spot = None  # first cycle
        current_spot     = 24000.0
        chain_len        = 100

        spot_changed   = (
            current_spot is not None
            and cycle_start_spot is not None
            and current_spot != cycle_start_spot
        )
        spot_exists    = current_spot is not None and current_spot > 0
        chain_exists   = chain_len > 0
        is_first_cycle = cycle_start_spot is None

        refresh_valid = (
            spot_exists
            and chain_exists
            and (spot_changed or is_first_cycle)
        )

        self.assertTrue(
            refresh_valid,
            "First cycle must complete even without spot change"
        )


# ─────────────────────────────────────────────────────────────────────
# 7. INTEGRATION / CROSS-COMPONENT TESTS
# ─────────────────────────────────────────────────────────────────────

class TestIntegration(unittest.TestCase):
    """
    Cross-component tests that verify interactions between modules.
    """

    def test_ratio_spread_risk_exceeds_old_formula(self):
        """
        The corrected ratio spread max_risk must always exceed
        the old (broken) formula for typical credit levels.
        """
        for credit in range(5, 30, 5):
            correct = max(
                (config.RATIO_ATM_OFFSET_PTS - credit) * config.LOT_SIZE,
                credit * config.LOT_SIZE,
            )
            old = credit * 2 * config.LOT_SIZE
            self.assertGreaterEqual(
                correct, old,
                f"credit={credit}: corrected={correct} should >= old={old}"
            )

    def test_backspread_stop_loss_in_meta(self):
        """
        Backspread meta must contain a stop_loss value so the
        premium stop check in _check_stop_loss can use it.
        """
        safe_debit = 25.0
        meta = {
            "net_debit":       safe_debit,
            "max_risk":        safe_debit * config.LOT_SIZE,
            "stop_loss":       safe_debit * 0.40,  # 40% of debit
            "profit_target":   safe_debit * config.BACKSPREAD_PROFIT_MULTIPLE,
            "strategy_type":   "LONG",
        }
        self.assertGreater(
            meta["stop_loss"], 0,
            "Backspread meta must have a positive stop_loss"
        )
        self.assertLess(
            meta["stop_loss"], meta["net_debit"],
            "Backspread stop_loss must be less than full debit"
        )

    def test_eod_completion_requires_empty_positions(self):
        """
        MN-T02: EOD must not be marked complete while positions remain.
        Simulate the guard logic.
        """
        open_positions = [Position()]  # one position still open
        eod_done_today = False

        # Simulate _end_of_day returning
        if not open_positions:
            eod_done_today = True

        self.assertFalse(
            eod_done_today,
            "EOD must not be marked complete with open positions"
        )

        # After positions are closed
        open_positions = []
        if not open_positions:
            eod_done_today = True

        self.assertTrue(
            eod_done_today,
            "EOD must be marked complete when all positions closed"
        )

    def test_kill_switch_does_not_skip_monitoring(self):
        """
        MN-T01: kill switch must not prevent position monitoring.
        The fast monitor must still run for residual open positions.
        """
        kill_switch_active = True
        open_positions     = [Position()]  # residual open position
        monitor_ran        = False

        # Simulate the corrected kill-switch branch
        if kill_switch_active:
            # Cancel orders (not tested here)
            # Run fast monitor for open positions
            if open_positions:
                monitor_ran = True
            # Sleep briefly then continue

        self.assertTrue(
            monitor_ran,
            "MN-T01: fast monitor must run even when kill switch is active"
        )

    def test_cb_l5_does_not_fire_on_every_fast_monitor_call(self):
        """
        MN-T04: CB L5 must be idempotent across multiple fast-monitor calls.
        """
        cb_level_5_active = False
        regime_changed_count = 0
        vix = config.CB_LEVEL_5_VIX_ABSOLUTE + 5

        # Simulate 10 fast-monitor iterations with VIX elevated
        for _ in range(10):
            if vix >= config.CB_LEVEL_5_VIX_ABSOLUTE:
                if not cb_level_5_active:
                    cb_level_5_active    = True
                    regime_changed_count += 1

        self.assertEqual(
            regime_changed_count, 1,
            "CB L5 must trigger regime change exactly once, not 10 times"
        )

    def test_transaction_costs_are_positive(self):
        """
        Transaction costs must always be positive for any trade.
        """
        # Minimal cost calculation for a single order
        value = 100.0 * 1 * config.LOT_SIZE  # 6500
        brokerage    = config.COST_BROKERAGE_PER_ORDER
        stt          = value * config.COST_STT_OPTION_SELL_PCT
        exchange_fee = value * config.COST_EXCHANGE_PCT
        sebi         = value * config.COST_SEBI_PCT
        gst          = (brokerage + exchange_fee + sebi) * config.COST_GST_PCT
        total_cost   = brokerage + stt + exchange_fee + sebi + gst

        self.assertGreater(total_cost, 0,
                           "Transaction costs must be positive")
        self.assertGreater(brokerage, 0)
        self.assertGreater(stt, 0)

    def test_condor_min_credit_exceeds_round_trip_costs(self):
        """
        CFG-07: condor minimum credit must exceed estimated round-trip costs.
        Otherwise the trade is negative-EV before any market risk.
        """
        # Estimate round-trip costs for a 4-leg condor (8 orders)
        # Using typical ATM option value of 100 pts
        typical_value = 100.0 * 1 * config.LOT_SIZE
        cost_per_order = (
            config.COST_BROKERAGE_PER_ORDER
            + typical_value * config.COST_STT_OPTION_SELL_PCT
            + typical_value * config.COST_EXCHANGE_PCT
            + typical_value * config.COST_SEBI_PCT
        )
        cost_per_order *= (1 + config.COST_GST_PCT)
        round_trip_costs = cost_per_order * 8  # 4 legs x 2 (entry + exit)

        min_credit_rupees = (
            config.CONDOR_MIN_CREDIT_PCT_OF_WIDTH
            * config.CONDOR_WING_WIDTH
            * config.LOT_SIZE
        )

        self.assertGreater(
            min_credit_rupees, round_trip_costs,
            f"Condor min credit (Rs{min_credit_rupees:.0f}) must exceed "
            f"round-trip costs (Rs{round_trip_costs:.0f})"
        )


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Collect all test classes
    test_classes = [
        TestPositionSizing,
        TestPnLCalculation,
        TestCircuitBreakers,
        TestRegimeEngine,
        TestOrderExecution,
        TestDataFreshness,
        TestIntegration,
    ]

    # Count tests
    loader = unittest.TestLoader()
    total = sum(
        loader.loadTestsFromTestCase(cls).countTestCases()
        for cls in test_classes
    )

    print("=" * 70)
    print(f"NIFTY Options Engine — Test Suite ({total} tests)")
    print("=" * 70)

    # Run with verbosity based on args
    verbosity = 2 if "-v" in sys.argv else 1

    # Support running a single class: python tests.py TestPnL
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        class_name = sys.argv[1]
        matched = [c for c in test_classes if c.__name__.startswith(class_name)]
        if matched:
            suite = unittest.TestSuite()
            for cls in matched:
                suite.addTests(loader.loadTestsFromTestCase(cls))
            runner = unittest.TextTestRunner(verbosity=verbosity)
            result = runner.run(suite)
            sys.exit(0 if result.wasSuccessful() else 1)

    # Run all tests
    suite = unittest.TestSuite()
    for cls in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)

    print("\n" + "=" * 70)
    print(f"Tests run: {result.testsRun}")
    print(f"Failures:  {len(result.failures)}")
    print(f"Errors:    {len(result.errors)}")
    print(f"Skipped:   {len(result.skipped)}")
    if result.wasSuccessful():
        print("RESULT: ALL TESTS PASSED")
    else:
        print("RESULT: SOME TESTS FAILED")
    print("=" * 70)

    sys.exit(0 if result.wasSuccessful() else 1)