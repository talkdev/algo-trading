# ============ FILE: strategy_engine.py ============
"""
Strategy selection, construction, execution, position management,
risk management, circuit breakers, and trade logging.
"""

import asyncio
import sqlite3
import csv
import uuid
import json
import math
import logging
import numpy as np
import pandas as pd
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Optional, Tuple, List, Dict, Any
import pytz
import config
from data_manager import DataManager
from regime_engine import RegimeEngine

logger = logging.getLogger(__name__)


@dataclass
class Leg:
    """Represents a single option leg in a strategy."""
    instrument_key: str
    option_type:    str
    action:         str
    strike:         float
    expiry:         str
    qty:            int
    entry_price:    float = 0.0
    exit_price:     float = 0.0
    order_id:       str = ""
    fill_status:    str = "PENDING"
    delta:          float = 0.0
    gamma:          float = 0.0
    vega:           float = 0.0
    theta:          float = 0.0
    slippage_pts:   float = 0.0


@dataclass
class Position:
    """Represents a complete multi-leg options position."""
    trade_id:          str
    strategy_name:     str
    regime_at_entry:   str
    entry_timestamp:   str
    entry_spot:        float
    entry_vix:         float
    legs:              List[Leg]
    stop_loss:         float
    profit_target:     float
    exit_dte:          Optional[int]
    max_hold_date:     Optional[str]
    composite_at_entry: float
    vol_score:         float
    edge_score:        float
    trend_score:       float
    flow_score:        float
    days_to_expiry:    int
    expiry_date:       str
    status:            str = "OPEN"
    total_credit:      float = 0.0
    total_debit:       float = 0.0
    net_premium:       float = 0.0
    max_risk:          float = 0.0
    realized_pnl:      float = 0.0
    realized_pnl_percent: float = 0.0
    exit_reason:       str = ""
    exit_timestamp:    str = ""
    exit_spot:         float = 0.0
    exit_vix:          float = 0.0
    paper_trade:       bool = True
    trend_direction:   float = 0.0
    meta:              Dict = field(default_factory=dict)


class StrategyEngine:
    """
    Manages strategy selection, construction, execution,
    position monitoring, risk management, and circuit breakers.
    """

    def __init__(
        self,
        data_manager: DataManager,
        regime_engine: RegimeEngine,
        db_path: str
    ) -> None:
        """Initialize StrategyEngine with data manager, regime engine, and db path."""
        self.dm = data_manager
        self.re = regime_engine
        self.db_path = db_path
        self._IST = pytz.timezone(config.TZ)

        self.open_positions: List[Position] = []
        self.closed_positions: List[Position] = []

        self.daily_pnl: float = 0.0
        self.weekly_pnl: float = 0.0
        self.peak_capital: float = float(config.TOTAL_CAPITAL)
        self.current_capital: float = float(config.TOTAL_CAPITAL)
        self.daily_trading_halted: bool = False
        self.kill_switch_active: bool = False
        self.cooling_period_end: Optional[datetime] = None

        self.cb_level_1_count: int = 0
        self.cb_level_2_active: bool = False
        self.cb_level_3_active: bool = False
        self.cb_level_4_active: bool = False

        self._last_trading_date: Optional[date] = None

    async def run_cycle(self) -> None:
        """Main method called every 5 minutes after regime refresh."""
        logger.info("Strategy cycle started")

        # Step 1: Check kill switch
        if self.kill_switch_active:
            logger.info("Kill switch active — no action this cycle")
            return

        # Daily reset check
        today = date.today()
        if self._last_trading_date != today:
            self.reset_daily_state()
            self._last_trading_date = today
            if today.weekday() == 0:
                self.reset_weekly_state()

        # Step 2: Update all position P&Ls
        await self._update_all_pnls()

        # Step 3: Check circuit breakers
        await self._check_circuit_breakers()
        if self.kill_switch_active:
            return

        # Step 4: Monitor existing positions
        await self._monitor_all_positions()

        # Step 5: Handle regime transitions
        if self.re.regime_changed:
            await self._handle_regime_transition()
            self.re.regime_changed = False

        # Step 6: Consider new position entry
        if self._should_enter_new_position():
            await self._enter_new_position()

        # Step 7: Check Greeks limits
        self._check_greeks_limits()

        # Step 8: Save state to SQLite
        self._save_all_positions_to_sqlite()

        # Step 9: Log portfolio summary
        self._log_portfolio_summary()

        logger.info("Strategy cycle complete")

    async def _update_all_pnls(self) -> None:
        """Update MTM P&L for all open positions from live chain LTP."""
        today_str = date.today().isoformat()

        for position in self.open_positions:
            position_value = 0.0
            for leg in position.legs:
                ltp = (
                    self.dm.option_chain
                    .get(leg.strike, {})
                    .get(leg.option_type, {})
                    .get("ltp", 0)
                )
                if ltp == 0 or ltp is None:
                    ltp = leg.entry_price
                    logger.warning(
                        f"LTP=0 for {leg.option_type} strike={leg.strike} "
                        f"— using entry_price={leg.entry_price}"
                    )

                if leg.action == "SELL":
                    leg_pnl = (leg.entry_price - ltp) * leg.qty * config.LOT_SIZE
                else:
                    leg_pnl = (ltp - leg.entry_price) * leg.qty * config.LOT_SIZE

                position_value += leg_pnl

            position.realized_pnl = position_value

        self.daily_pnl = sum(
            p.realized_pnl
            for p in self.open_positions + self.closed_positions
            if p.entry_timestamp and p.entry_timestamp[:10] == today_str
        )

    async def _check_circuit_breakers(self) -> None:
        """Check and enforce all 5 circuit breaker levels."""

        # LEVEL 1 — Single position loss
        for position in list(self.open_positions):
            if position.realized_pnl < -(config.CB_LEVEL_1_PCT * config.TOTAL_CAPITAL):
                logger.critical(
                    f"CB L1 TRIGGERED: position={position.trade_id} "
                    f"pnl={position.realized_pnl:.2f} "
                    f"threshold={-(config.CB_LEVEL_1_PCT * config.TOTAL_CAPITAL):.2f} "
                    f"action={config.CB_LEVEL_1_ACTION}"
                )
                self._log_circuit_breaker(
                    1,
                    f"position_loss={position.realized_pnl:.2f}",
                    config.CB_LEVEL_1_ACTION
                )
                await self._close_position(
                    position, config.EXIT_REASONS["CIRCUIT_BREAK"]
                )
                self.cb_level_1_count += 1

        # LEVEL 2 — Daily loss
        if self.daily_pnl < -(config.CB_LEVEL_2_PCT * config.TOTAL_CAPITAL):
            if not self.cb_level_2_active:
                logger.critical(
                    f"CB L2 TRIGGERED: daily_pnl={self.daily_pnl:.2f} "
                    f"threshold={-(config.CB_LEVEL_2_PCT * config.TOTAL_CAPITAL):.2f} "
                    f"action={config.CB_LEVEL_2_ACTION}"
                )
                self._log_circuit_breaker(
                    2,
                    f"daily_pnl={self.daily_pnl:.2f}",
                    config.CB_LEVEL_2_ACTION
                )
                self.daily_trading_halted = True
                self.cb_level_2_active = True

        # LEVEL 3 — Weekly loss
        if self.weekly_pnl < -(config.CB_LEVEL_3_PCT * config.TOTAL_CAPITAL):
            if not self.cb_level_3_active:
                logger.critical(
                    f"CB L3 TRIGGERED: weekly_pnl={self.weekly_pnl:.2f} "
                    f"action={config.CB_LEVEL_3_ACTION}"
                )
                self._log_circuit_breaker(
                    3,
                    f"weekly_pnl={self.weekly_pnl:.2f}",
                    config.CB_LEVEL_3_ACTION
                )
                await self._reduce_all_positions_50pct()
                self.cb_level_3_active = True

        # LEVEL 4 — Max drawdown
        drawdown = self.peak_capital - self.current_capital
        if drawdown > config.CB_LEVEL_4_PCT * config.TOTAL_CAPITAL:
            logger.critical(
                f"CB L4 TRIGGERED: drawdown={drawdown:.2f} "
                f"threshold={config.CB_LEVEL_4_PCT * config.TOTAL_CAPITAL:.2f} "
                f"action={config.CB_LEVEL_4_ACTION}"
            )
            self._log_circuit_breaker(
                4,
                f"drawdown={drawdown:.2f}",
                config.CB_LEVEL_4_ACTION
            )
            await self._emergency_flatten_all()
            self.kill_switch_active = True
            self.cb_level_4_active = True

        # LEVEL 5 — IV spike
        if self.dm.prev_vix and self.dm.vix and self.dm.prev_vix > 0:
            iv_change = (self.dm.vix - self.dm.prev_vix) / self.dm.prev_vix
            if iv_change > config.CB_LEVEL_5_IV_SPIKE_PCT:
                logger.critical(
                    f"CB L5 TRIGGERED: vix_change={iv_change * 100:.1f}% "
                    f"prev_vix={self.dm.prev_vix:.2f} "
                    f"curr_vix={self.dm.vix:.2f} "
                    f"action={config.CB_LEVEL_5_ACTION}"
                )
                self._log_circuit_breaker(
                    5,
                    f"iv_spike={iv_change * 100:.1f}%",
                    config.CB_LEVEL_5_ACTION
                )
                self.re.previous_regime = self.re.confirmed_regime
                self.re.confirmed_regime = config.REGIME_STRONG_BUY
                self.re.regime_changed = True

    async def _monitor_all_positions(self) -> None:
        """Monitor all open positions for exit conditions."""
        for position in list(self.open_positions):
            if position.status != "OPEN":
                continue

            # CHECK 1 — Stop loss
            stop_hit = await self._check_stop_loss(position)
            if stop_hit:
                continue

            # CHECK 2 — Profit target
            target_hit = await self._check_profit_target(position)
            if target_hit:
                continue

            # CHECK 3 — DTE exit
            dte_hit = await self._check_dte_exit(position)
            if dte_hit:
                continue

            # CHECK 4 — Max hold days
            hold_hit = self._check_max_hold(position)
            if hold_hit:
                await self._close_position(
                    position, config.EXIT_REASONS["TIME_EXIT"]
                )
                continue

            # CHECK 5 — Time-based exit
            now_time = datetime.now(self._IST).time()
            if now_time >= config.TIME_EXIT_NORMAL:
                if position.strategy_name not in [
                    config.STRAT_SHORT_STRADDLE,
                    config.STRAT_IRON_CONDOR,
                    config.STRAT_CREDIT_SPREADS
                ]:
                    await self._close_position(
                        position, config.EXIT_REASONS["EOD"]
                    )
                    continue

            # CHECK 6 — Strategy-specific adjustments
            if position.strategy_name == config.STRAT_IRON_CONDOR:
                await self._monitor_condor_adjustment(position)
            elif position.strategy_name == config.STRAT_CREDIT_SPREADS:
                await self._monitor_spread_delta(position)
            elif position.strategy_name == config.STRAT_RATIO_SPREAD:
                await self._monitor_ratio_delta(position)
            elif position.strategy_name == config.STRAT_BACKSPREAD:
                await self._monitor_backspread_adjustment(position)

    async def _check_stop_loss(self, position: Position) -> bool:
        """Check and trigger stop loss for position based on strategy type."""
        strategy = position.strategy_name

        if strategy == config.STRAT_SHORT_STRADDLE:
            stop_up = position.entry_spot * (1 + config.STRADDLE_STOP_PCT)
            stop_down = position.entry_spot * (1 - config.STRADDLE_STOP_PCT)
            if self.dm.spot is None:
                return False
            if self.dm.spot >= stop_up or self.dm.spot <= stop_down:
                logger.info(
                    f"Straddle spot stop hit: spot={self.dm.spot:.2f} "
                    f"stop_up={stop_up:.2f} stop_down={stop_down:.2f}"
                )
                await self._close_position(
                    position, config.EXIT_REASONS["STOP_LOSS"]
                )
                return True

        elif strategy in [config.STRAT_LONG_STRADDLE, config.STRAT_STRANGLE]:
            stop_val = position.total_debit * (1 - config.LONG_STRADDLE_STOP_PCT)
            current_val = self._get_position_value(position)
            if current_val <= stop_val:
                logger.info(
                    f"Long straddle premium stop hit: "
                    f"current_val={current_val:.2f} stop_val={stop_val:.2f}"
                )
                await self._close_position(
                    position, config.EXIT_REASONS["STOP_LOSS"]
                )
                return True

        elif strategy == config.STRAT_BACKSPREAD:
            trend = position.trend_direction
            if self.dm.spot is None:
                return False
            if trend >= 0:
                stop_level = position.entry_spot * (1 - config.BACKSPREAD_STOP_MOVE_PCT)
                if self.dm.spot < stop_level:
                    logger.info(
                        f"Backspread bullish stop hit: "
                        f"spot={self.dm.spot:.2f} stop={stop_level:.2f}"
                    )
                    await self._close_position(
                        position, config.EXIT_REASONS["STOP_LOSS"]
                    )
                    return True
            else:
                stop_level = position.entry_spot * (1 + config.BACKSPREAD_STOP_MOVE_PCT)
                if self.dm.spot > stop_level:
                    logger.info(
                        f"Backspread bearish stop hit: "
                        f"spot={self.dm.spot:.2f} stop={stop_level:.2f}"
                    )
                    await self._close_position(
                        position, config.EXIT_REASONS["STOP_LOSS"]
                    )
                    return True

        elif strategy in [config.STRAT_IRON_CONDOR, config.STRAT_CREDIT_SPREADS]:
            if self.dm.spot is None:
                return False
            short_call_strike = self._get_short_strike(position, "call")
            short_put_strike = self._get_short_strike(position, "put")
            if short_call_strike and self.dm.spot >= short_call_strike:
                logger.info(
                    f"Condor/Spread call side breached: "
                    f"spot={self.dm.spot:.2f} short_call={short_call_strike:.2f}"
                )
                await self._close_one_side(
                    position, "call", config.EXIT_REASONS["STOP_LOSS"]
                )
                return False
            if short_put_strike and self.dm.spot <= short_put_strike:
                logger.info(
                    f"Condor/Spread put side breached: "
                    f"spot={self.dm.spot:.2f} short_put={short_put_strike:.2f}"
                )
                await self._close_one_side(
                    position, "put", config.EXIT_REASONS["STOP_LOSS"]
                )
                return False

        elif strategy == config.STRAT_BUTTERFLY:
            if self.dm.spot is None:
                return False
            upper_wing = self._get_upper_wing_strike(position)
            lower_wing = self._get_lower_wing_strike(position)
            if upper_wing and self.dm.spot > upper_wing + config.BUTTERFLY_WING_BUFFER_PTS:
                logger.info(
                    f"Butterfly upper wing breached: "
                    f"spot={self.dm.spot:.2f} upper={upper_wing:.2f}"
                )
                await self._close_position(
                    position, config.EXIT_REASONS["STOP_LOSS"]
                )
                return True
            if lower_wing and self.dm.spot < lower_wing - config.BUTTERFLY_WING_BUFFER_PTS:
                logger.info(
                    f"Butterfly lower wing breached: "
                    f"spot={self.dm.spot:.2f} lower={lower_wing:.2f}"
                )
                await self._close_position(
                    position, config.EXIT_REASONS["STOP_LOSS"]
                )
                return True

        elif strategy == config.STRAT_DEFENSIVE:
            portfolio_stop = position.entry_spot * (
                1 - config.DEFENSIVE_PORTFOLIO_STOP_PCT
            )
            if self.dm.spot and self.dm.spot < portfolio_stop:
                logger.info(
                    f"Defensive hedge portfolio stop hit: "
                    f"spot={self.dm.spot:.2f} stop={portfolio_stop:.2f}"
                )
                await self._close_position(
                    position, config.EXIT_REASONS["STOP_LOSS"]
                )
                return True

        return False

    async def _check_profit_target(self, position: Position) -> bool:
        """Check and trigger profit target for position based on strategy type."""
        strategy = position.strategy_name

        if strategy in [
            config.STRAT_SHORT_STRADDLE,
            config.STRAT_IRON_CONDOR,
            config.STRAT_CREDIT_SPREADS,
            config.STRAT_RATIO_SPREAD
        ]:
            current_value = self._get_position_current_premium(position)
            target_credit = position.total_credit * (1 - config.PROFIT_TARGET_PCT)
            if current_value <= target_credit and position.total_credit > 0:
                logger.info(
                    f"Profit target hit: {strategy} "
                    f"current={current_value:.2f} target={target_credit:.2f}"
                )
                await self._close_position(
                    position, config.EXIT_REASONS["PROFIT_TARGET"]
                )
                return True

        elif strategy in [config.STRAT_LONG_STRADDLE, config.STRAT_STRANGLE]:
            current_val = self._get_position_value(position)
            target_val = position.total_debit * (1 + config.LONG_STRADDLE_TARGET_PCT)
            if current_val >= target_val and position.total_debit > 0:
                logger.info(
                    f"Long straddle target hit: "
                    f"current={current_val:.2f} target={target_val:.2f}"
                )
                await self._close_position(
                    position, config.EXIT_REASONS["PROFIT_TARGET"]
                )
                return True

        elif strategy == config.STRAT_BACKSPREAD:
            current_val = self._get_position_value(position)
            target_val = position.total_debit * config.BACKSPREAD_PROFIT_MULTIPLE
            if current_val >= target_val and position.total_debit > 0:
                logger.info(
                    f"Backspread 10x target hit: "
                    f"current={current_val:.2f} target={target_val:.2f}"
                )
                await self._close_position(
                    position, config.EXIT_REASONS["PROFIT_TARGET"]
                )
                return True

        elif strategy == config.STRAT_BUTTERFLY:
            current_val = self._get_position_value(position)
            max_profit = position.meta.get("max_profit", 0)
            if max_profit > 0 and current_val >= max_profit * config.BUTTERFLY_PROFIT_PCT:
                logger.info(
                    f"Butterfly 50% profit target hit: "
                    f"current={current_val:.2f} target={max_profit * 0.5:.2f}"
                )
                await self._close_position(
                    position, config.EXIT_REASONS["PROFIT_TARGET"]
                )
                return True

        return False

    async def _check_dte_exit(self, position: Position) -> bool:
        """Check and trigger DTE-based exit for position."""
        if not position.expiry_date:
            return False

        try:
            expiry = datetime.strptime(position.expiry_date, "%Y-%m-%d").date()
            dte = (expiry - date.today()).days
        except ValueError:
            return False

        strategy = position.strategy_name

        exit_dte_map = {
            config.STRAT_SHORT_STRADDLE:  config.STRADDLE_EXIT_DTE,
            config.STRAT_IRON_CONDOR:     config.CONDOR_EXIT_DTE,
            config.STRAT_CREDIT_SPREADS:  config.SPREAD_EXIT_DTE,
            config.STRAT_RATIO_SPREAD:    config.RATIO_EXIT_DTE,
            config.STRAT_BUTTERFLY:       config.BUTTERFLY_EXIT_DTE,
            config.STRAT_BACKSPREAD:      config.BACKSPREAD_EXIT_DTE,
        }

        exit_dte = exit_dte_map.get(strategy)
        if exit_dte is not None and dte <= exit_dte:
            logger.info(
                f"DTE exit triggered: {strategy} dte={dte} "
                f"exit_dte={exit_dte}"
            )
            await self._close_position(
                position, config.EXIT_REASONS["TIME_EXIT"]
            )
            return True

        return False

    def _check_max_hold(self, position: Position) -> bool:
        """Check if position has exceeded maximum hold date."""
        if not position.max_hold_date:
            return False
        try:
            max_date = datetime.strptime(
                position.max_hold_date, "%Y-%m-%d"
            ).date()
            if date.today() >= max_date:
                logger.info(
                    f"Max hold date reached: {position.trade_id} "
                    f"max_hold_date={position.max_hold_date}"
                )
                return True
        except ValueError:
            pass
        return False

    async def _handle_regime_transition(self) -> None:
        """Apply regime transition rules A through F."""
        from_regime = self.re.previous_regime
        to_regime = self.re.confirmed_regime

        logger.info(
            f"Regime transition: {from_regime} -> {to_regime}"
        )

        # RULE A — ANY -> STRONG_BUY_VOL
        if to_regime == config.REGIME_STRONG_BUY:
            logger.info("RULE A: Flatten ALL shorts immediately")
            for position in list(self.open_positions):
                has_shorts = any(
                    leg.action == "SELL" for leg in position.legs
                )
                if has_shorts:
                    await self._close_position(
                        position,
                        config.EXIT_REASONS["REGIME_CHANGE"],
                        use_market=True
                    )
            self.cooling_period_end = datetime.now(self._IST) + timedelta(minutes=30)
            logger.info(
                f"Cooling period set until {self.cooling_period_end.isoformat()}"
            )
            return

        # RULE B — STRONG_SELL -> MILD_SELL or MILD_SELL -> NEUTRAL
        if ((from_regime == config.REGIME_STRONG_SELL and
             to_regime == config.REGIME_MILD_SELL) or
                (from_regime == config.REGIME_MILD_SELL and
                 to_regime == config.REGIME_NEUTRAL)):
            logger.info("RULE B: Close 50% of shorts")
            for position in list(self.open_positions):
                await self._reduce_position_50pct(position)
            return

        # RULE C — STRONG_SELL -> NEUTRAL
        if (from_regime == config.REGIME_STRONG_SELL and
                to_regime == config.REGIME_NEUTRAL):
            logger.info("RULE C: Close 75% of shorts")
            for position in list(self.open_positions):
                await self._reduce_position_pct(position, 0.75)
            return

        # RULE D — MILD_SELL -> BUY_VOL
        if (from_regime == config.REGIME_MILD_SELL and
                to_regime == config.REGIME_BUY_VOL):
            logger.info("RULE D: Convert shorts to spreads")
            for position in list(self.open_positions):
                await self._convert_shorts_to_spreads(position)
            return

        # RULE E — NEUTRAL -> STRONG regime
        if (from_regime == config.REGIME_NEUTRAL and
                to_regime in [config.REGIME_STRONG_SELL, config.REGIME_STRONG_BUY]):
            if self.re.persistence_count < 3:
                logger.info(
                    f"RULE E: Waiting for 3 confirmations "
                    f"(current={self.re.persistence_count})"
                )
                return
            else:
                logger.info(
                    f"RULE E: 3 confirmations reached — allowing entry"
                )
                return

        # RULE F — Same category transition
        if self._same_category(from_regime, to_regime):
            logger.info("RULE F: Move stops to breakeven")
            for position in self.open_positions:
                self._move_stop_to_breakeven(position)
            return

    def _same_category(self, r1: str, r2: str) -> bool:
        """Check if two regimes are in the same category."""
        sell_regimes = {config.REGIME_STRONG_SELL, config.REGIME_MILD_SELL}
        buy_regimes = {config.REGIME_BUY_VOL, config.REGIME_STRONG_BUY}
        if r1 in sell_regimes and r2 in sell_regimes:
            return True
        if r1 in buy_regimes and r2 in buy_regimes:
            return True
        if r1 == config.REGIME_NEUTRAL and r2 == config.REGIME_NEUTRAL:
            return True
        return False

    def _should_enter_new_position(self) -> bool:
        """Check all gates before allowing new position entry."""
        if self.kill_switch_active:
            logger.info("Entry gate: kill switch active")
            return False

        if self.daily_trading_halted:
            logger.info("Entry gate: daily trading halted")
            return False

        now = datetime.now(self._IST)
        now_time = now.time()

        if now_time < config.EXEC_START_TIME:
            logger.info(
                f"Entry gate: before exec start time "
                f"{config.EXEC_START_TIME}"
            )
            return False

        if now_time > config.EXEC_END_TIME:
            logger.info(
                f"Entry gate: after exec end time "
                f"{config.EXEC_END_TIME}"
            )
            return False

        if now_time > config.REGIME_FREEZE_TIME:
            logger.info("Entry gate: after regime freeze time")
            return False

        regime = self.re.confirmed_regime
        if regime == config.REGIME_NEUTRAL:
            iv_rank = self.dm.compute_iv_rank()
            adx = self.dm.adx or 99
            if iv_rank <= 50 or adx >= 20:
                logger.info(
                    f"Entry gate: NEUTRAL regime — "
                    f"iv_rank={iv_rank:.1f} adx={adx:.2f}"
                )
                return False

        if self.cooling_period_end:
            if now < self.cooling_period_end:
                logger.info(
                    f"Entry gate: cooling period active until "
                    f"{self.cooling_period_end.isoformat()}"
                )
                return False
            else:
                self.cooling_period_end = None

        if (self.re.previous_regime == config.REGIME_NEUTRAL and
                self.re.confirmed_regime in [
                    config.REGIME_STRONG_SELL,
                    config.REGIME_STRONG_BUY
                ]):
            if self.re.persistence_count < 3:
                logger.info(
                    f"Entry gate: waiting for 3 confirmations "
                    f"from NEUTRAL breakout "
                    f"(current={self.re.persistence_count})"
                )
                return False

        if len(self.open_positions) >= 2:
            logger.info(
                f"Entry gate: max concurrent positions reached "
                f"({len(self.open_positions)}/2)"
            )
            return False

        deployed = sum(p.max_risk for p in self.open_positions)
        regime_capital = config.REGIME_CAPITAL_PCT.get(regime, 0) * config.TOTAL_CAPITAL
        if deployed >= regime_capital:
            logger.info(
                f"Entry gate: capital fully deployed "
                f"deployed={deployed:.0f} "
                f"regime_capital={regime_capital:.0f}"
            )
            return False

        logger.info(
            f"Entry gate: all checks passed — "
            f"regime={regime} deployed={deployed:.0f}"
        )
        return True

    async def _enter_new_position(self) -> None:
        """Select, build, validate, and execute a new strategy position."""
        regime = self.re.confirmed_regime

        strategy_name = self._select_strategy(regime)
        if strategy_name is None:
            logger.info(f"No strategy selected for regime={regime}")
            return

        logger.info(f"Selected strategy: {strategy_name} for regime={regime}")

        legs, meta = await self._build_strategy(strategy_name)
        if legs is None:
            logger.info(f"Strategy build failed for {strategy_name}")
            return

        if not await self._pre_trade_checks(strategy_name, legs):
            logger.info(f"Pre-trade checks failed for {strategy_name}")
            return

        lots = self._calculate_lot_size(strategy_name, meta)
        if lots < 1:
            logger.info(f"Lot size=0 for {strategy_name} — skipping")
            return

        for leg in legs:
            leg.qty = leg.qty * lots

        success = await self._execute_strategy(strategy_name, legs, meta)
        if not success:
            logger.warning(f"Strategy execution failed for {strategy_name}")
            return

        position = self._create_position_record(strategy_name, legs, meta)
        self.open_positions.append(position)
        self.dm.save_position(self._position_to_dict(position))
        logger.info(
            f"New position entered: {strategy_name} "
            f"trade_id={position.trade_id[:8]} "
            f"lots={lots}"
        )

    def _select_strategy(self, regime: str) -> Optional[str]:
        """Select appropriate strategy based on regime and market conditions."""
        adx = self.dm.adx or 0
        atr_contract = self.dm.is_atr_contracting()
        put_iv = self._get_25d_put_iv()
        call_iv = self._get_25d_call_iv()
        skew_diff = put_iv - call_iv
        term_spread = (self.dm.forward_iv or 0) - (self.dm.vix or 0)
        trend_score = self.re.confirmed_trend
        iv_rank = self.dm.compute_iv_rank()
        has_shorts = self._has_short_positions()
        spot = self.dm.spot or 0
        ema_200 = self._get_ema_200()

        if regime == config.REGIME_STRONG_SELL:
            dte = self._get_dte_for_target(
                config.STRADDLE_DTE_MIN, config.STRADDLE_DTE_MAX
            )
            if adx < config.ADX_RANGE_THRESHOLD and atr_contract:
                logger.info(
                    f"Strategy select: STRADDLE "
                    f"(adx={adx:.1f}<{config.ADX_RANGE_THRESHOLD}, "
                    f"atr_contracting={atr_contract})"
                )
                return config.STRAT_SHORT_STRADDLE
            elif (config.ADX_RANGE_THRESHOLD <= adx <= 28 or
                  skew_diff >= config.SPREAD_SKEW_THRESHOLD):
                logger.info(
                    f"Strategy select: CONDOR "
                    f"(adx={adx:.1f}, skew_diff={skew_diff:.2f})"
                )
                return config.STRAT_IRON_CONDOR
            elif dte and dte > 30:
                logger.info(
                    f"Strategy select: CONDOR (dte={dte}>30)"
                )
                return config.STRAT_IRON_CONDOR
            else:
                logger.info("Strategy select: STRADDLE (default STRONG_SELL)")
                return config.STRAT_SHORT_STRADDLE

        elif regime == config.REGIME_MILD_SELL:
            if skew_diff >= config.SPREAD_SKEW_THRESHOLD:
                logger.info(
                    f"Strategy select: CREDIT_SPREADS "
                    f"(skew_diff={skew_diff:.2f}>={config.SPREAD_SKEW_THRESHOLD})"
                )
                return config.STRAT_CREDIT_SPREADS
            elif (skew_diff < config.RATIO_SKEW_FLAT_THRESHOLD and
                  term_spread > config.RATIO_CONTANGO_THRESHOLD):
                logger.info(
                    f"Strategy select: RATIO_SPREAD "
                    f"(skew_diff={skew_diff:.2f}, term_spread={term_spread:.2f})"
                )
                return config.STRAT_RATIO_SPREAD
            else:
                logger.info("Strategy select: CREDIT_SPREADS (default MILD_SELL)")
                return config.STRAT_CREDIT_SPREADS

        elif regime == config.REGIME_NEUTRAL:
            if iv_rank > 50 and adx < 20:
                logger.info(
                    f"Strategy select: CONDOR "
                    f"(iv_rank={iv_rank:.1f}>50, adx={adx:.1f}<20)"
                )
                return config.STRAT_IRON_CONDOR
            logger.info("Strategy select: None (NEUTRAL conditions not met)")
            return None

        elif regime == config.REGIME_BUY_VOL:
            if not has_shorts and spot > ema_200:
                logger.info(
                    f"Strategy select: BUTTERFLY "
                    f"(no_shorts, spot={spot:.0f}>ema200={ema_200:.0f})"
                )
                return config.STRAT_BUTTERFLY
            elif has_shorts and self._gamma_above_50pct_limit():
                logger.info("Strategy select: DEFENSIVE (gamma limit breach)")
                return config.STRAT_DEFENSIVE
            else:
                logger.info("Strategy select: BUTTERFLY (default BUY_VOL)")
                return config.STRAT_BUTTERFLY

        elif regime == config.REGIME_STRONG_BUY:
            if self.dm.vix and self.dm.vix > config.BACKSPREAD_MAX_VIX:
                logger.info(
                    f"Strategy select: LONG_STRADDLE "
                    f"(vix={self.dm.vix:.1f}>{config.BACKSPREAD_MAX_VIX})"
                )
                return config.STRAT_LONG_STRADDLE
            if trend_score == 0:
                logger.info("Strategy select: LONG_STRADDLE (trend=0)")
                return config.STRAT_LONG_STRADDLE
            elif (abs(trend_score) == 1 and
                  skew_diff < config.RATIO_SKEW_FLAT_THRESHOLD):
                logger.info(
                    f"Strategy select: BACKSPREAD "
                    f"(trend={trend_score:.0f}, skew_diff={skew_diff:.2f})"
                )
                return config.STRAT_BACKSPREAD
            else:
                logger.info("Strategy select: LONG_STRADDLE (default STRONG_BUY)")
                return config.STRAT_LONG_STRADDLE

        elif regime == config.REGIME_EVENT:
            call_spread = self._get_otm_bid_ask("call")
            put_spread = self._get_otm_bid_ask("put")
            if (call_spread < config.EVENT_STRANGLE_MAX_SPREAD_PTS and
                    put_spread < config.EVENT_STRANGLE_MAX_SPREAD_PTS):
                logger.info(
                    f"Strategy select: STRANGLE "
                    f"(call_spread={call_spread:.2f}, put_spread={put_spread:.2f})"
                )
                return config.STRAT_STRANGLE
            else:
                logger.info(
                    f"Strategy select: LONG_STRADDLE "
                    f"(spreads too wide: call={call_spread:.2f}, put={put_spread:.2f})"
                )
                return config.STRAT_LONG_STRADDLE

        return None

    async def _build_strategy(
        self, strategy_name: str
    ) -> Tuple[Optional[List[Leg]], Dict]:
        """Dispatch to appropriate strategy builder."""
        builders = {
            config.STRAT_SHORT_STRADDLE: self._build_short_straddle,
            config.STRAT_IRON_CONDOR:    self._build_iron_condor,
            config.STRAT_CREDIT_SPREADS: self._build_credit_spreads,
            config.STRAT_RATIO_SPREAD:   self._build_ratio_spread,
            config.STRAT_BUTTERFLY:      self._build_butterfly,
            config.STRAT_DEFENSIVE:      self._build_defensive_hedge,
            config.STRAT_LONG_STRADDLE:  self._build_long_straddle,
            config.STRAT_BACKSPREAD:     self._build_backspread,
            config.STRAT_STRANGLE:       self._build_long_strangle,
        }
        builder = builders.get(strategy_name)
        if builder is None:
            logger.warning(f"No builder for strategy: {strategy_name}")
            return (None, {})
        return await builder()

    async def _build_short_straddle(self) -> Tuple[Optional[List[Leg]], Dict]:
        """Build ATM short straddle legs."""
        expiry = self.dm.get_expiry_by_dte(
            config.STRADDLE_DTE_MIN + 5, tolerance=5
        )
        if expiry is None:
            logger.warning("Short straddle: no valid expiry found")
            return (None, {})

        atm = self.dm.atm_strike
        if atm is None:
            logger.warning("Short straddle: ATM strike is None")
            return (None, {})

        chain = self.dm.option_chain
        if atm not in chain:
            logger.warning(f"Short straddle: ATM {atm} not in chain")
            return (None, {})

        call_data = chain[atm]["call"]
        put_data = chain[atm]["put"]

        if (call_data["ask"] - call_data["bid"]) > config.MAX_SPREAD_ATM_PTS:
            logger.warning(
                f"Short straddle: ATM call spread too wide "
                f"{call_data['ask'] - call_data['bid']:.2f}"
            )
            return (None, {})
        if (put_data["ask"] - put_data["bid"]) > config.MAX_SPREAD_ATM_PTS:
            logger.warning(
                f"Short straddle: ATM put spread too wide "
                f"{put_data['ask'] - put_data['bid']:.2f}"
            )
            return (None, {})

        dte = (datetime.strptime(expiry, "%Y-%m-%d").date() - date.today()).days
        if dte < 5:
            logger.warning(f"Short straddle: DTE={dte} < 5 — reject")
            return (None, {})

        total_premium = call_data["ltp"] + put_data["ltp"]

        legs = [
            Leg(
                instrument_key=call_data["instrument_key"],
                option_type="call", action="SELL",
                strike=atm, expiry=expiry, qty=1,
                delta=call_data["delta"],
                gamma=call_data["gamma"],
                vega=call_data["vega"],
                theta=call_data["theta"]
            ),
            Leg(
                instrument_key=put_data["instrument_key"],
                option_type="put", action="SELL",
                strike=atm, expiry=expiry, qty=1,
                delta=put_data["delta"],
                gamma=put_data["gamma"],
                vega=put_data["vega"],
                theta=put_data["theta"]
            )
        ]

        meta = {
            "total_premium":  total_premium,
            "stop_loss_up":   (self.dm.spot or 0) * (1 + config.STRADDLE_STOP_PCT),
            "stop_loss_down": (self.dm.spot or 0) * (1 - config.STRADDLE_STOP_PCT),
            "profit_target":  total_premium * (1 - config.STRADDLE_TARGET_PCT),
            "stop_loss":      total_premium * config.STRADDLE_STOP_PCT,
            "exit_dte":       config.STRADDLE_EXIT_DTE,
            "max_hold_date":  None,
            "max_risk":       total_premium * config.LOT_SIZE,
            "strategy_type":  "SHORT"
        }

        logger.info(
            f"Short straddle built: atm={atm} expiry={expiry} "
            f"premium={total_premium:.2f} dte={dte}"
        )
        return (legs, meta)

    async def _build_iron_condor(self) -> Tuple[Optional[List[Leg]], Dict]:
        """Build wide iron condor legs."""
        expiry = self.dm.get_expiry_by_dte(
            config.CONDOR_DTE_MIN + 7, tolerance=7
        )
        if expiry is None:
            logger.warning("Iron condor: no valid expiry found")
            return (None, {})

        spot = self.dm.spot
        if spot is None:
            return (None, {})

        chain = self.dm.option_chain
        dte = (datetime.strptime(expiry, "%Y-%m-%d").date() - date.today()).days

        if dte < 5:
            logger.warning(f"Iron condor: DTE={dte} < 5 — reject")
            return (None, {})

        vix = self.dm.vix or 16.0
        expected_move = spot * (vix / 100) * ((dte / 365) ** 0.5)

        short_call_target = spot + 1.5 * expected_move
        short_put_target = spot - 1.5 * expected_move

        short_call = round(
            short_call_target / config.NIFTY_STRIKE_STEP
        ) * config.NIFTY_STRIKE_STEP
        short_put = round(
            short_put_target / config.NIFTY_STRIKE_STEP
        ) * config.NIFTY_STRIKE_STEP

        long_call = short_call + config.CONDOR_WING_WIDTH
        long_put = short_put - config.CONDOR_WING_WIDTH

        for strike in [short_call, short_put, long_call, long_put]:
            if strike not in chain:
                logger.warning(f"Iron condor: strike {strike} not in chain")
                return (None, {})

        for strike, opt_type, max_spread in [
            (short_call, "call", config.MAX_SPREAD_ATM_PTS),
            (short_put, "put", config.MAX_SPREAD_ATM_PTS),
            (long_call, "call", config.MAX_SPREAD_OTM_PTS),
            (long_put, "put", config.MAX_SPREAD_OTM_PTS),
        ]:
            spread = chain[strike][opt_type]["ask"] - chain[strike][opt_type]["bid"]
            if spread > max_spread:
                logger.warning(
                    f"Iron condor: spread too wide at {strike} {opt_type}: "
                    f"{spread:.2f} > {max_spread}"
                )
                return (None, {})

        sc_prem = chain[short_call]["call"]["ltp"]
        sp_prem = chain[short_put]["put"]["ltp"]
        lc_prem = chain[long_call]["call"]["ltp"]
        lp_prem = chain[long_put]["put"]["ltp"]

        net_credit = sc_prem + sp_prem - lc_prem - lp_prem

        if net_credit < config.CONDOR_MIN_CREDIT:
            logger.warning(
                f"Iron condor: net_credit={net_credit:.2f} < "
                f"min={config.CONDOR_MIN_CREDIT}"
            )
            return (None, {})

        max_risk = config.CONDOR_WING_WIDTH - net_credit

        # Long legs first (RULE O1)
        legs = [
            Leg(
                instrument_key=chain[long_call]["call"]["instrument_key"],
                option_type="call", action="BUY",
                strike=long_call, expiry=expiry, qty=1,
                delta=chain[long_call]["call"]["delta"],
                gamma=chain[long_call]["call"]["gamma"],
                vega=chain[long_call]["call"]["vega"],
                theta=chain[long_call]["call"]["theta"]
            ),
            Leg(
                instrument_key=chain[long_put]["put"]["instrument_key"],
                option_type="put", action="BUY",
                strike=long_put, expiry=expiry, qty=1,
                delta=chain[long_put]["put"]["delta"],
                gamma=chain[long_put]["put"]["gamma"],
                vega=chain[long_put]["put"]["vega"],
                theta=chain[long_put]["put"]["theta"]
            ),
            Leg(
                instrument_key=chain[short_call]["call"]["instrument_key"],
                option_type="call", action="SELL",
                strike=short_call, expiry=expiry, qty=1,
                delta=chain[short_call]["call"]["delta"],
                gamma=chain[short_call]["call"]["gamma"],
                vega=chain[short_call]["call"]["vega"],
                theta=chain[short_call]["call"]["theta"]
            ),
            Leg(
                instrument_key=chain[short_put]["put"]["instrument_key"],
                option_type="put", action="SELL",
                strike=short_put, expiry=expiry, qty=1,
                delta=chain[short_put]["put"]["delta"],
                gamma=chain[short_put]["put"]["gamma"],
                vega=chain[short_put]["put"]["vega"],
                theta=chain[short_put]["put"]["theta"]
            )
        ]

        meta = {
            "net_credit":    net_credit,
            "max_risk":      max_risk * config.LOT_SIZE,
            "short_call":    short_call,
            "short_put":     short_put,
            "long_call":     long_call,
            "long_put":      long_put,
            "profit_target": net_credit * (1 - config.CONDOR_TARGET_PCT),
            "stop_loss":     net_credit * config.STRADDLE_STOP_PCT,
            "exit_dte":      config.CONDOR_EXIT_DTE,
            "max_hold_date": None,
            "strategy_type": "SHORT",
            "total_credit":  net_credit
        }

        logger.info(
            f"Iron condor built: sc={short_call} sp={short_put} "
            f"lc={long_call} lp={long_put} "
            f"credit={net_credit:.2f} dte={dte}"
        )
        return (legs, meta)

    async def _build_credit_spreads(self) -> Tuple[Optional[List[Leg]], Dict]:
        """Build bull put + bear call credit spreads."""
        expiry = self.dm.get_expiry_by_dte(
            config.CONDOR_DTE_MIN + 7, tolerance=7
        )
        if expiry is None:
            logger.warning("Credit spreads: no valid expiry found")
            return (None, {})

        dte = (datetime.strptime(expiry, "%Y-%m-%d").date() - date.today()).days
        if dte < 5:
            logger.warning(f"Credit spreads: DTE={dte} < 5 — reject")
            return (None, {})

        short_put_strike = self.dm.get_strike_by_delta("put", config.SPREAD_DELTA_SHORT)
        long_put_strike = self.dm.get_strike_by_delta("put", config.SPREAD_DELTA_LONG)
        short_call_strike = self.dm.get_strike_by_delta("call", config.SPREAD_DELTA_SHORT)
        long_call_strike = self.dm.get_strike_by_delta("call", config.SPREAD_DELTA_LONG)

        if any(s is None for s in [
            short_put_strike, long_put_strike,
            short_call_strike, long_call_strike
        ]):
            logger.warning("Credit spreads: could not find all delta strikes")
            return (None, {})

        if short_put_strike <= long_put_strike:
            logger.warning(
                f"Credit spreads: invalid bull put structure "
                f"short={short_put_strike} long={long_put_strike}"
            )
            return (None, {})

        if short_call_strike >= long_call_strike:
            logger.warning(
                f"Credit spreads: invalid bear call structure "
                f"short={short_call_strike} long={long_call_strike}"
            )
            return (None, {})

        chain = self.dm.option_chain
        for strike in [short_put_strike, long_put_strike,
                       short_call_strike, long_call_strike]:
            if strike not in chain:
                logger.warning(f"Credit spreads: strike {strike} not in chain")
                return (None, {})

        sp_prem = chain[short_put_strike]["put"]["ltp"]
        lp_prem = chain[long_put_strike]["put"]["ltp"]
        sc_prem = chain[short_call_strike]["call"]["ltp"]
        lc_prem = chain[long_call_strike]["call"]["ltp"]

        put_credit = sp_prem - lp_prem
        call_credit = sc_prem - lc_prem
        total_credit = put_credit + call_credit

        if total_credit < config.SPREAD_MIN_CREDIT:
            logger.warning(
                f"Credit spreads: total_credit={total_credit:.2f} < "
                f"min={config.SPREAD_MIN_CREDIT}"
            )
            return (None, {})

        put_spread_width = short_put_strike - long_put_strike
        call_spread_width = long_call_strike - short_call_strike
        max_risk = (max(put_spread_width, call_spread_width) - total_credit) * config.LOT_SIZE

        # Long legs first (RULE O1)
        legs = [
            Leg(
                instrument_key=chain[long_put_strike]["put"]["instrument_key"],
                option_type="put", action="BUY",
                strike=long_put_strike, expiry=expiry, qty=1,
                delta=chain[long_put_strike]["put"]["delta"],
                gamma=chain[long_put_strike]["put"]["gamma"],
                vega=chain[long_put_strike]["put"]["vega"],
                theta=chain[long_put_strike]["put"]["theta"]
            ),
            Leg(
                instrument_key=chain[long_call_strike]["call"]["instrument_key"],
                option_type="call", action="BUY",
                strike=long_call_strike, expiry=expiry, qty=1,
                delta=chain[long_call_strike]["call"]["delta"],
                gamma=chain[long_call_strike]["call"]["gamma"],
                vega=chain[long_call_strike]["call"]["vega"],
                theta=chain[long_call_strike]["call"]["theta"]
            ),
            Leg(
                instrument_key=chain[short_put_strike]["put"]["instrument_key"],
                option_type="put", action="SELL",
                strike=short_put_strike, expiry=expiry, qty=1,
                delta=chain[short_put_strike]["put"]["delta"],
                gamma=chain[short_put_strike]["put"]["gamma"],
                vega=chain[short_put_strike]["put"]["vega"],
                theta=chain[short_put_strike]["put"]["theta"]
            ),
            Leg(
                instrument_key=chain[short_call_strike]["call"]["instrument_key"],
                option_type="call", action="SELL",
                strike=short_call_strike, expiry=expiry, qty=1,
                delta=chain[short_call_strike]["call"]["delta"],
                gamma=chain[short_call_strike]["call"]["gamma"],
                vega=chain[short_call_strike]["call"]["vega"],
                theta=chain[short_call_strike]["call"]["theta"]
            )
        ]

        meta = {
            "total_credit":  total_credit,
            "max_risk":      max_risk,
            "short_put":     short_put_strike,
            "long_put":      long_put_strike,
            "short_call":    short_call_strike,
            "long_call":     long_call_strike,
            "profit_target": total_credit * (1 - config.SPREAD_TARGET_PCT),
            "stop_loss":     total_credit * config.STRADDLE_STOP_PCT,
            "exit_dte":      config.SPREAD_EXIT_DTE,
            "max_hold_date": None,
            "strategy_type": "SHORT"
        }

        logger.info(
            f"Credit spreads built: sp={short_put_strike} lp={long_put_strike} "
            f"sc={short_call_strike} lc={long_call_strike} "
            f"credit={total_credit:.2f} dte={dte}"
        )
        return (legs, meta)

    async def _build_ratio_spread(self) -> Tuple[Optional[List[Leg]], Dict]:
        """Build 1x2 ratio spread with separate qty=1 orders."""
        expiry = self.dm.get_expiry_by_dte(
            config.CONDOR_DTE_MIN + 7, tolerance=7
        )
        if expiry is None:
            logger.warning("Ratio spread: no valid expiry found")
            return (None, {})

        dte = (datetime.strptime(expiry, "%Y-%m-%d").date() - date.today()).days
        if dte < config.RATIO_EXIT_DTE + 1:
            logger.warning(f"Ratio spread: DTE={dte} too low")
            return (None, {})

        atm = self.dm.atm_strike
        if atm is None:
            return (None, {})

        chain = self.dm.option_chain

        call_short_strike = atm
        call_long_strike = atm + config.RATIO_ATM_OFFSET_PTS
        put_short_strike = atm
        put_long_strike = atm - config.RATIO_ATM_OFFSET_PTS

        for s in [call_short_strike, call_long_strike,
                  put_short_strike, put_long_strike]:
            if s not in chain:
                logger.warning(f"Ratio spread: strike {s} not in chain")
                return (None, {})

        cs_prem = chain[call_short_strike]["call"]["ltp"]
        cl_prem = chain[call_long_strike]["call"]["ltp"]
        ps_prem = chain[put_short_strike]["put"]["ltp"]
        pl_prem = chain[put_long_strike]["put"]["ltp"]

        call_credit = cs_prem - (2 * cl_prem)
        put_credit = ps_prem - (2 * pl_prem)
        total_credit = call_credit + put_credit

        if total_credit <= 0:
            logger.warning(
                f"Ratio spread: net debit={total_credit:.2f} — abort"
            )
            return (None, {})

        # Two separate BUY legs for each long (RULE O2)
        legs = [
            Leg(
                instrument_key=chain[call_long_strike]["call"]["instrument_key"],
                option_type="call", action="BUY",
                strike=call_long_strike, expiry=expiry, qty=1,
                delta=chain[call_long_strike]["call"]["delta"],
                gamma=chain[call_long_strike]["call"]["gamma"],
                vega=chain[call_long_strike]["call"]["vega"],
                theta=chain[call_long_strike]["call"]["theta"]
            ),
            Leg(
                instrument_key=chain[call_long_strike]["call"]["instrument_key"],
                option_type="call", action="BUY",
                strike=call_long_strike, expiry=expiry, qty=1,
                delta=chain[call_long_strike]["call"]["delta"],
                gamma=chain[call_long_strike]["call"]["gamma"],
                vega=chain[call_long_strike]["call"]["vega"],
                theta=chain[call_long_strike]["call"]["theta"]
            ),
            Leg(
                instrument_key=chain[put_long_strike]["put"]["instrument_key"],
                option_type="put", action="BUY",
                strike=put_long_strike, expiry=expiry, qty=1,
                delta=chain[put_long_strike]["put"]["delta"],
                gamma=chain[put_long_strike]["put"]["gamma"],
                vega=chain[put_long_strike]["put"]["vega"],
                theta=chain[put_long_strike]["put"]["theta"]
            ),
            Leg(
                instrument_key=chain[put_long_strike]["put"]["instrument_key"],
                option_type="put", action="BUY",
                strike=put_long_strike, expiry=expiry, qty=1,
                delta=chain[put_long_strike]["put"]["delta"],
                gamma=chain[put_long_strike]["put"]["gamma"],
                vega=chain[put_long_strike]["put"]["vega"],
                theta=chain[put_long_strike]["put"]["theta"]
            ),
            Leg(
                instrument_key=chain[call_short_strike]["call"]["instrument_key"],
                option_type="call", action="SELL",
                strike=call_short_strike, expiry=expiry, qty=1,
                delta=chain[call_short_strike]["call"]["delta"],
                gamma=chain[call_short_strike]["call"]["gamma"],
                vega=chain[call_short_strike]["call"]["vega"],
                theta=chain[call_short_strike]["call"]["theta"]
            ),
            Leg(
                instrument_key=chain[put_short_strike]["put"]["instrument_key"],
                option_type="put", action="SELL",
                strike=put_short_strike, expiry=expiry, qty=1,
                delta=chain[put_short_strike]["put"]["delta"],
                gamma=chain[put_short_strike]["put"]["gamma"],
                vega=chain[put_short_strike]["put"]["vega"],
                theta=chain[put_short_strike]["put"]["theta"]
            )
        ]

        meta = {
            "total_credit":  total_credit,
            "max_risk":      total_credit * 2 * config.LOT_SIZE,
            "profit_target": total_credit * (1 - config.RATIO_TARGET_PCT),
            "stop_loss":     total_credit * config.STRADDLE_STOP_PCT,
            "exit_dte":      config.RATIO_EXIT_DTE,
            "max_hold_date": None,
            "strategy_type": "SHORT",
            "call_short":    call_short_strike,
            "put_short":     put_short_strike
        }

        logger.info(
            f"Ratio spread built: atm={atm} credit={total_credit:.2f} dte={dte}"
        )
        return (legs, meta)

    async def _build_butterfly(self) -> Tuple[Optional[List[Leg]], Dict]:
        """Build long put butterfly with equidistant wings."""
        expiry = self.dm.get_expiry_by_dte(4, tolerance=3)
        if expiry is None:
            logger.warning("Butterfly: no valid expiry found")
            return (None, {})

        dte = (datetime.strptime(expiry, "%Y-%m-%d").date() - date.today()).days
        if dte > config.BUTTERFLY_DTE_MAX:
            logger.warning(
                f"Butterfly: DTE={dte} > max={config.BUTTERFLY_DTE_MAX}"
            )
            return (None, {})

        strike_a = self.dm.get_strike_by_delta("put", config.BUTTERFLY_DELTA_A)
        strike_b = self.dm.get_strike_by_delta("put", config.BUTTERFLY_DELTA_B)
        strike_c = self.dm.get_strike_by_delta("put", config.BUTTERFLY_DELTA_C)

        if any(s is None for s in [strike_a, strike_b, strike_c]):
            logger.warning("Butterfly: could not find all delta strikes")
            return (None, {})

        chain = self.dm.option_chain

        # Validate equidistant wings
        width_ab = strike_b - strike_a
        width_bc = strike_c - strike_b
        if abs(width_ab - width_bc) > config.NIFTY_STRIKE_STEP:
            strike_c = strike_b + width_ab
            strike_c = round(
                strike_c / config.NIFTY_STRIKE_STEP
            ) * config.NIFTY_STRIKE_STEP
            if strike_c not in chain:
                logger.warning(
                    f"Butterfly: adjusted strike_c={strike_c} not in chain"
                )
                return (None, {})

        for s in [strike_a, strike_b, strike_c]:
            if s not in chain:
                logger.warning(f"Butterfly: strike {s} not in chain")
                return (None, {})

        prem_a = chain[strike_a]["put"]["ltp"]
        prem_b = chain[strike_b]["put"]["ltp"]
        prem_c = chain[strike_c]["put"]["ltp"]

        net_debit = (prem_a + prem_c) - (2 * prem_b)

        if net_debit > config.BUTTERFLY_MAX_DEBIT_PTS:
            logger.warning(
                f"Butterfly: net_debit={net_debit:.2f} > "
                f"max={config.BUTTERFLY_MAX_DEBIT_PTS}"
            )
            return (None, {})

        if net_debit <= 0:
            logger.warning(
                f"Butterfly: net_debit={net_debit:.2f} <= 0 — invalid"
            )
            return (None, {})

        max_profit = (strike_b - strike_a) - net_debit
        rr_ratio = max_profit / net_debit if net_debit > 0 else 0

        if rr_ratio < config.BUTTERFLY_MIN_RR_RATIO:
            logger.warning(
                f"Butterfly: rr_ratio={rr_ratio:.2f} < "
                f"min={config.BUTTERFLY_MIN_RR_RATIO}"
            )
            return (None, {})

        # Long legs first, then two separate SELL orders (RULE O1, O2)
        legs = [
            Leg(
                instrument_key=chain[strike_a]["put"]["instrument_key"],
                option_type="put", action="BUY",
                strike=strike_a, expiry=expiry, qty=1,
                delta=chain[strike_a]["put"]["delta"],
                gamma=chain[strike_a]["put"]["gamma"],
                vega=chain[strike_a]["put"]["vega"],
                theta=chain[strike_a]["put"]["theta"]
            ),
            Leg(
                instrument_key=chain[strike_c]["put"]["instrument_key"],
                option_type="put", action="BUY",
                strike=strike_c, expiry=expiry, qty=1,
                delta=chain[strike_c]["put"]["delta"],
                gamma=chain[strike_c]["put"]["gamma"],
                vega=chain[strike_c]["put"]["vega"],
                theta=chain[strike_c]["put"]["theta"]
            ),
            Leg(
                instrument_key=chain[strike_b]["put"]["instrument_key"],
                option_type="put", action="SELL",
                strike=strike_b, expiry=expiry, qty=1,
                delta=chain[strike_b]["put"]["delta"],
                gamma=chain[strike_b]["put"]["gamma"],
                vega=chain[strike_b]["put"]["vega"],
                theta=chain[strike_b]["put"]["theta"]
            ),
            Leg(
                instrument_key=chain[strike_b]["put"]["instrument_key"],
                option_type="put", action="SELL",
                strike=strike_b, expiry=expiry, qty=1,
                delta=chain[strike_b]["put"]["delta"],
                gamma=chain[strike_b]["put"]["gamma"],
                vega=chain[strike_b]["put"]["vega"],
                theta=chain[strike_b]["put"]["theta"]
            )
        ]

        meta = {
            "net_debit":     net_debit,
            "max_risk":      net_debit * config.LOT_SIZE,
            "max_profit":    max_profit,
            "rr_ratio":      rr_ratio,
            "strike_a":      strike_a,
            "strike_b":      strike_b,
            "strike_c":      strike_c,
            "profit_target": max_profit * config.BUTTERFLY_PROFIT_PCT,
            "stop_loss":     net_debit * config.LOT_SIZE,
            "exit_dte":      config.BUTTERFLY_EXIT_DTE,
            "max_hold_date": (
                date.today() + timedelta(days=dte - 1)
            ).strftime("%Y-%m-%d"),
            "strategy_type": "LONG"
        }

        logger.info(
            f"Butterfly built: a={strike_a} b={strike_b} c={strike_c} "
            f"debit={net_debit:.2f} rr={rr_ratio:.2f} dte={dte}"
        )
        return (legs, meta)

    async def _build_long_straddle(self) -> Tuple[Optional[List[Leg]], Dict]:
        """Build ATM long straddle for volatility expansion."""
        expiry = self.dm.get_expiry_by_dte(
            config.LONG_STRADDLE_DTE_MIN + 5, tolerance=5
        )
        if expiry is None:
            logger.warning("Long straddle: no valid expiry found")
            return (None, {})

        dte = (datetime.strptime(expiry, "%Y-%m-%d").date() - date.today()).days
        if dte < config.LONG_STRADDLE_DTE_MIN:
            logger.warning(
                f"Long straddle: DTE={dte} < min={config.LONG_STRADDLE_DTE_MIN}"
            )
            return (None, {})

        atm = self.dm.atm_strike
        if atm is None:
            logger.warning("Long straddle: ATM strike is None")
            return (None, {})

        chain = self.dm.option_chain
        if atm not in chain:
            logger.warning(f"Long straddle: ATM {atm} not in chain")
            return (None, {})

        spot = self.dm.spot or 0
        vix = self.dm.vix or 16.0
        call_data = chain[atm]["call"]
        put_data = chain[atm]["put"]

        total_debit = call_data["ltp"] + put_data["ltp"]

        max_allowed = spot * config.LONG_STRADDLE_MAX_DEBIT_PCT
        if total_debit > max_allowed:
            logger.warning(
                f"Long straddle: debit={total_debit:.2f} > "
                f"max_allowed={max_allowed:.2f}"
            )
            return (None, {})

        # Validate VIX spike
        if len(self.dm.vix_history_20d) >= config.LONG_STRADDLE_VIX_SMA_PERIOD:
            vix_arr = list(self.dm.vix_history_20d)
            vix_sma = float(np.mean(
                vix_arr[-config.LONG_STRADDLE_VIX_SMA_PERIOD:]
            ))
            if vix_sma > 0:
                vix_spike = (vix - vix_sma) / vix_sma
                if vix_spike < config.LONG_STRADDLE_VIX_SPIKE_PCT:
                    logger.warning(
                        f"Long straddle: VIX not spiking enough "
                        f"vix={vix:.2f} sma={vix_sma:.2f} "
                        f"spike={vix_spike * 100:.1f}% < "
                        f"{config.LONG_STRADDLE_VIX_SPIKE_PCT * 100:.0f}%"
                    )
                    return (None, {})

        # Validate IV rank
        iv_rank = self.dm.compute_iv_rank()
        if iv_rank > config.LONG_STRADDLE_MAX_IV_RANK:
            logger.warning(
                f"Long straddle: IV rank={iv_rank:.1f} > "
                f"max={config.LONG_STRADDLE_MAX_IV_RANK}"
            )
            return (None, {})

        # Validate bid-ask spreads
        if (call_data["ask"] - call_data["bid"]) > config.MAX_SPREAD_ATM_PTS:
            logger.warning("Long straddle: call spread too wide")
            return (None, {})
        if (put_data["ask"] - put_data["bid"]) > config.MAX_SPREAD_ATM_PTS:
            logger.warning("Long straddle: put spread too wide")
            return (None, {})

        max_hold_date = (
            date.today() + timedelta(days=config.LONG_STRADDLE_HOLD_DAYS)
        ).strftime("%Y-%m-%d")

        legs = [
            Leg(
                instrument_key=call_data["instrument_key"],
                option_type="call", action="BUY",
                strike=atm, expiry=expiry, qty=1,
                delta=call_data["delta"],
                gamma=call_data["gamma"],
                vega=call_data["vega"],
                theta=call_data["theta"]
            ),
            Leg(
                instrument_key=put_data["instrument_key"],
                option_type="put", action="BUY",
                strike=atm, expiry=expiry, qty=1,
                delta=put_data["delta"],
                gamma=put_data["gamma"],
                vega=put_data["vega"],
                theta=put_data["theta"]
            )
        ]

        meta = {
            "total_debit":   total_debit,
            "max_risk":      total_debit * config.LOT_SIZE,
            "stop_loss":     total_debit * (1 - config.LONG_STRADDLE_STOP_PCT),
            "profit_target": total_debit * (1 + config.LONG_STRADDLE_TARGET_PCT),
            "exit_dte":      None,
            "max_hold_date": max_hold_date,
            "strategy_type": "LONG"
        }

        logger.info(
            f"Long straddle built: atm={atm} expiry={expiry} "
            f"debit={total_debit:.2f} dte={dte}"
        )
        return (legs, meta)

    async def _build_backspread(self) -> Tuple[Optional[List[Leg]], Dict]:
        """Build directional backspread (3x long, 1x short) with hedge."""
        expiry = self.dm.get_expiry_by_dte(
            config.BACKSPREAD_DTE_MIN + 2, tolerance=2
        )
        if expiry is None:
            logger.warning("Backspread: no valid expiry found")
            return (None, {})

        dte = (datetime.strptime(expiry, "%Y-%m-%d").date() - date.today()).days
        if dte < config.BACKSPREAD_DTE_MIN or dte > config.BACKSPREAD_DTE_MAX:
            logger.warning(
                f"Backspread: DTE={dte} outside range "
                f"[{config.BACKSPREAD_DTE_MIN},{config.BACKSPREAD_DTE_MAX}]"
            )
            return (None, {})

        vix = self.dm.vix or 16.0
        if vix > config.BACKSPREAD_MAX_VIX:
            logger.warning(
                f"Backspread: VIX={vix:.1f} > max={config.BACKSPREAD_MAX_VIX}"
            )
            return (None, {})

        trend = self.re.confirmed_trend
        chain = self.dm.option_chain
        spot = self.dm.spot or 0

        if trend >= 0:
            # Bullish backspread
            long_strike_3x = self.dm.get_strike_by_delta(
                "call", config.BACKSPREAD_LONG_DELTA
            )
            short_strike = self.dm.get_strike_by_delta(
                "call", config.BACKSPREAD_SHORT_DELTA
            )
            hedge_strike = self.dm.get_strike_by_delta(
                "put", config.BACKSPREAD_LONG_DELTA
            )
            hedge_short = self.dm.get_strike_by_delta(
                "put", config.BACKSPREAD_SHORT_DELTA
            )

            if any(s is None for s in [
                long_strike_3x, short_strike, hedge_strike, hedge_short
            ]):
                logger.warning("Backspread: could not find all strikes")
                return (None, {})

            if (short_strike - long_strike_3x) < config.BACKSPREAD_MIN_STRIKE_WIDTH:
                logger.warning(
                    f"Backspread: strikes too close "
                    f"short={short_strike} long={long_strike_3x}"
                )
                return (None, {})

            for s in [long_strike_3x, short_strike, hedge_strike, hedge_short]:
                if s not in chain:
                    logger.warning(f"Backspread: strike {s} not in chain")
                    return (None, {})

            long_prem_3x = chain[long_strike_3x]["call"]["ltp"]
            short_prem = chain[short_strike]["call"]["ltp"]
            hedge_prem = chain[hedge_strike]["put"]["ltp"]
            hedge_s_prem = chain[hedge_short]["put"]["ltp"]

            net_debit = (
                (long_prem_3x * config.BACKSPREAD_LONG_QTY) +
                hedge_prem - short_prem - hedge_s_prem
            )

            if net_debit > config.BACKSPREAD_MAX_DEBIT_PTS:
                logger.warning(
                    f"Backspread: net_debit={net_debit:.2f} > "
                    f"max={config.BACKSPREAD_MAX_DEBIT_PTS}"
                )
                return (None, {})

            if net_debit <= 0:
                logger.info(
                    f"Backspread: net_credit={abs(net_debit):.2f} — proceeding"
                )

            expected_move = spot * (vix / 100) * ((dte / 365) ** 0.5)
            potential_profit = (
                (short_strike - long_strike_3x) *
                (config.BACKSPREAD_LONG_QTY - config.BACKSPREAD_SHORT_QTY)
            )
            if potential_profit < net_debit * config.BACKSPREAD_MIN_MOVE_MULTIPLE:
                logger.warning(
                    f"Backspread: insufficient profit potential "
                    f"potential={potential_profit:.0f} "
                    f"required={net_debit * config.BACKSPREAD_MIN_MOVE_MULTIPLE:.0f}"
                )
                return (None, {})

            # 3 separate BUY legs + 1 hedge BUY, then SELL legs (RULE O1, O2)
            legs = [
                Leg(
                    instrument_key=chain[long_strike_3x]["call"]["instrument_key"],
                    option_type="call", action="BUY",
                    strike=long_strike_3x, expiry=expiry, qty=1,
                    delta=chain[long_strike_3x]["call"]["delta"],
                    gamma=chain[long_strike_3x]["call"]["gamma"],
                    vega=chain[long_strike_3x]["call"]["vega"],
                    theta=chain[long_strike_3x]["call"]["theta"]
                ),
                Leg(
                    instrument_key=chain[long_strike_3x]["call"]["instrument_key"],
                    option_type="call", action="BUY",
                    strike=long_strike_3x, expiry=expiry, qty=1,
                    delta=chain[long_strike_3x]["call"]["delta"],
                    gamma=chain[long_strike_3x]["call"]["gamma"],
                    vega=chain[long_strike_3x]["call"]["vega"],
                    theta=chain[long_strike_3x]["call"]["theta"]
                ),
                Leg(
                    instrument_key=chain[long_strike_3x]["call"]["instrument_key"],
                    option_type="call", action="BUY",
                    strike=long_strike_3x, expiry=expiry, qty=1,
                    delta=chain[long_strike_3x]["call"]["delta"],
                    gamma=chain[long_strike_3x]["call"]["gamma"],
                    vega=chain[long_strike_3x]["call"]["vega"],
                    theta=chain[long_strike_3x]["call"]["theta"]
                ),
                Leg(
                    instrument_key=chain[hedge_strike]["put"]["instrument_key"],
                    option_type="put", action="BUY",
                    strike=hedge_strike, expiry=expiry, qty=1,
                    delta=chain[hedge_strike]["put"]["delta"],
                    gamma=chain[hedge_strike]["put"]["gamma"],
                    vega=chain[hedge_strike]["put"]["vega"],
                    theta=chain[hedge_strike]["put"]["theta"]
                ),
                Leg(
                    instrument_key=chain[short_strike]["call"]["instrument_key"],
                    option_type="call", action="SELL",
                    strike=short_strike, expiry=expiry, qty=1,
                    delta=chain[short_strike]["call"]["delta"],
                    gamma=chain[short_strike]["call"]["gamma"],
                    vega=chain[short_strike]["call"]["vega"],
                    theta=chain[short_strike]["call"]["theta"]
                ),
                Leg(
                    instrument_key=chain[hedge_short]["put"]["instrument_key"],
                    option_type="put", action="SELL",
                    strike=hedge_short, expiry=expiry, qty=1,
                    delta=chain[hedge_short]["put"]["delta"],
                    gamma=chain[hedge_short]["put"]["gamma"],
                    vega=chain[hedge_short]["put"]["vega"],
                    theta=chain[hedge_short]["put"]["theta"]
                )
            ]

        else:
            # Bearish backspread
            long_strike_3x = self.dm.get_strike_by_delta(
                "put", config.BACKSPREAD_LONG_DELTA
            )
            short_strike = self.dm.get_strike_by_delta(
                "put", config.BACKSPREAD_SHORT_DELTA
            )
            hedge_strike = self.dm.get_strike_by_delta(
                "call", config.BACKSPREAD_LONG_DELTA
            )
            hedge_short = self.dm.get_strike_by_delta(
                "call", config.BACKSPREAD_SHORT_DELTA
            )

            if any(s is None for s in [
                long_strike_3x, short_strike, hedge_strike, hedge_short
            ]):
                logger.warning("Backspread bearish: could not find all strikes")
                return (None, {})

            for s in [long_strike_3x, short_strike, hedge_strike, hedge_short]:
                if s not in chain:
                    logger.warning(f"Backspread bearish: strike {s} not in chain")
                    return (None, {})

            long_prem_3x = chain[long_strike_3x]["put"]["ltp"]
            short_prem = chain[short_strike]["put"]["ltp"]
            hedge_prem = chain[hedge_strike]["call"]["ltp"]
            hedge_s_prem = chain[hedge_short]["call"]["ltp"]

            net_debit = (
                (long_prem_3x * config.BACKSPREAD_LONG_QTY) +
                hedge_prem - short_prem - hedge_s_prem
            )

            if net_debit > config.BACKSPREAD_MAX_DEBIT_PTS:
                logger.warning(
                    f"Backspread bearish: net_debit={net_debit:.2f} too high"
                )
                return (None, {})

            legs = [
                Leg(
                    instrument_key=chain[long_strike_3x]["put"]["instrument_key"],
                    option_type="put", action="BUY",
                    strike=long_strike_3x, expiry=expiry, qty=1,
                    delta=chain[long_strike_3x]["put"]["delta"],
                    gamma=chain[long_strike_3x]["put"]["gamma"],
                    vega=chain[long_strike_3x]["put"]["vega"],
                    theta=chain[long_strike_3x]["put"]["theta"]
                ),
                Leg(
                    instrument_key=chain[long_strike_3x]["put"]["instrument_key"],
                    option_type="put", action="BUY",
                    strike=long_strike_3x, expiry=expiry, qty=1,
                    delta=chain[long_strike_3x]["put"]["delta"],
                    gamma=chain[long_strike_3x]["put"]["gamma"],
                    vega=chain[long_strike_3x]["put"]["vega"],
                    theta=chain[long_strike_3x]["put"]["theta"]
                ),
                Leg(
                    instrument_key=chain[long_strike_3x]["put"]["instrument_key"],
                    option_type="put", action="BUY",
                    strike=long_strike_3x, expiry=expiry, qty=1,
                    delta=chain[long_strike_3x]["put"]["delta"],
                    gamma=chain[long_strike_3x]["put"]["gamma"],
                    vega=chain[long_strike_3x]["put"]["vega"],
                    theta=chain[long_strike_3x]["put"]["theta"]
                ),
                Leg(
                    instrument_key=chain[hedge_strike]["call"]["instrument_key"],
                    option_type="call", action="BUY",
                    strike=hedge_strike, expiry=expiry, qty=1,
                    delta=chain[hedge_strike]["call"]["delta"],
                    gamma=chain[hedge_strike]["call"]["gamma"],
                    vega=chain[hedge_strike]["call"]["vega"],
                    theta=chain[hedge_strike]["call"]["theta"]
                ),
                Leg(
                    instrument_key=chain[short_strike]["put"]["instrument_key"],
                    option_type="put", action="SELL",
                    strike=short_strike, expiry=expiry, qty=1,
                    delta=chain[short_strike]["put"]["delta"],
                    gamma=chain[short_strike]["put"]["gamma"],
                    vega=chain[short_strike]["put"]["vega"],
                    theta=chain[short_strike]["put"]["theta"]
                ),
                Leg(
                    instrument_key=chain[hedge_short]["call"]["instrument_key"],
                    option_type="call", action="SELL",
                    strike=hedge_short, expiry=expiry, qty=1,
                    delta=chain[hedge_short]["call"]["delta"],
                    gamma=chain[hedge_short]["call"]["gamma"],
                    vega=chain[hedge_short]["call"]["vega"],
                    theta=chain[hedge_short]["call"]["theta"]
                )
            ]

        max_hold_date = (
            date.today() + timedelta(days=config.LONG_STRADDLE_HOLD_DAYS)
        ).strftime("%Y-%m-%d")

        meta = {
            "net_debit":       max(net_debit, 0.05),
            "max_risk":        max(net_debit, 0.05) * config.LOT_SIZE,
            "profit_target":   max(net_debit, 0.05) * config.BACKSPREAD_PROFIT_MULTIPLE,
            "stop_loss":       max(net_debit, 0.05) * 0.40,
            "exit_dte":        config.BACKSPREAD_EXIT_DTE,
            "max_hold_date":   max_hold_date,
            "strategy_type":   "LONG",
            "trend_direction": trend
        }

        logger.info(
            f"Backspread built: trend={'bullish' if trend >= 0 else 'bearish'} "
            f"net_debit={net_debit:.2f} dte={dte}"
        )
        return (legs, meta)

    async def _build_long_strangle(self) -> Tuple[Optional[List[Leg]], Dict]:
        """Build long strangle for event volatility."""
        expiry = self.dm.get_expiry_by_dte(7, tolerance=3)
        if expiry is None:
            logger.warning("Long strangle: no valid expiry found")
            return (None, {})

        call_strike = self.dm.get_strike_by_delta(
            "call", config.EVENT_STRANGLE_DELTA
        )
        put_strike = self.dm.get_strike_by_delta(
            "put", config.EVENT_STRANGLE_DELTA
        )

        if call_strike is None or put_strike is None:
            logger.warning("Long strangle: could not find delta strikes")
            return (None, {})

        chain = self.dm.option_chain
        for s in [call_strike, put_strike]:
            if s not in chain:
                logger.warning(f"Long strangle: strike {s} not in chain")
                return (None, {})

        call_spread = (
            chain[call_strike]["call"]["ask"] -
            chain[call_strike]["call"]["bid"]
        )
        put_spread = (
            chain[put_strike]["put"]["ask"] -
            chain[put_strike]["put"]["bid"]
        )

        if call_spread > config.EVENT_STRANGLE_MAX_SPREAD_PTS:
            logger.warning(
                f"Long strangle: call spread={call_spread:.2f} too wide"
            )
            return (None, {})

        if put_spread > config.EVENT_STRANGLE_MAX_SPREAD_PTS:
            logger.warning(
                f"Long strangle: put spread={put_spread:.2f} too wide "
                f"— falling back to straddle"
            )
            return await self._build_long_straddle()

        call_prem = chain[call_strike]["call"]["ltp"]
        put_prem = chain[put_strike]["put"]["ltp"]
        total_debit = call_prem + put_prem

        max_hold_date = (
            date.today() + timedelta(days=2)
        ).strftime("%Y-%m-%d")

        legs = [
            Leg(
                instrument_key=chain[call_strike]["call"]["instrument_key"],
                option_type="call", action="BUY",
                strike=call_strike, expiry=expiry, qty=1,
                delta=chain[call_strike]["call"]["delta"],
                gamma=chain[call_strike]["call"]["gamma"],
                vega=chain[call_strike]["call"]["vega"],
                theta=chain[call_strike]["call"]["theta"]
            ),
            Leg(
                instrument_key=chain[put_strike]["put"]["instrument_key"],
                option_type="put", action="BUY",
                strike=put_strike, expiry=expiry, qty=1,
                delta=chain[put_strike]["put"]["delta"],
                gamma=chain[put_strike]["put"]["gamma"],
                vega=chain[put_strike]["put"]["vega"],
                theta=chain[put_strike]["put"]["theta"]
            )
        ]

        meta = {
            "total_debit":   total_debit,
            "max_risk":      total_debit * config.LOT_SIZE,
            "stop_loss":     total_debit * (1 - config.EVENT_STRANGLE_STOP_PCT),
            "profit_target": total_debit * (1 + config.EVENT_STRANGLE_TARGET_PCT),
            "exit_dte":      None,
            "max_hold_date": max_hold_date,
            "strategy_type": "LONG"
        }

        logger.info(
            f"Long strangle built: call={call_strike} put={put_strike} "
            f"debit={total_debit:.2f}"
        )
        return (legs, meta)

    async def _build_defensive_hedge(self) -> Tuple[Optional[List[Leg]], Dict]:
        """Build defensive hedge by reducing shorts and buying ATM put."""
        import math as _math

        short_legs = [
            leg for pos in self.open_positions
            for leg in pos.legs
            if leg.action == "SELL"
        ]

        if not short_legs:
            logger.info("Defensive hedge: no shorts to hedge — skip")
            return (None, {})

        if len(self.dm.vix_history_20d) >= config.DEFENSIVE_VIX_SMA_PERIOD:
            vix_arr = list(self.dm.vix_history_20d)
            vix_sma = float(np.mean(
                vix_arr[-config.DEFENSIVE_VIX_SMA_PERIOD:]
            ))
            if vix_sma > 0:
                vix_spike = (self.dm.vix - vix_sma) / vix_sma
                if vix_spike < config.DEFENSIVE_VIX_SPIKE_PCT:
                    logger.info(
                        f"Defensive hedge: VIX not spiking enough "
                        f"vix={self.dm.vix:.2f} sma={vix_sma:.2f} "
                        f"spike={vix_spike * 100:.1f}%"
                    )
                    return (None, {})

        ema_20 = self._compute_ema_n(config.DEFENSIVE_EMA_PERIOD)
        if self.dm.spot and self.dm.spot > ema_20:
            logger.info(
                f"Defensive hedge: spot={self.dm.spot:.2f} > "
                f"ema20={ema_20:.2f} — skip"
            )
            return (None, {})

        total_delta = sum(
            leg.delta * leg.qty * config.LOT_SIZE
            for pos in self.open_positions
            for leg in pos.legs
            if leg.action == "SELL"
        )

        reduction_legs = []
        for pos in self.open_positions:
            for leg in pos.legs:
                if leg.action == "SELL":
                    reduce_qty = _math.ceil(
                        leg.qty * config.DEFENSIVE_REDUCTION_PCT
                    )
                    reduction_legs.append({
                        "position":   pos,
                        "leg":        leg,
                        "reduce_qty": reduce_qty
                    })

        atm = self.dm.atm_strike
        if atm is None:
            logger.warning("Defensive hedge: ATM strike is None")
            return (None, {})

        chain = self.dm.option_chain
        if atm not in chain:
            logger.warning(f"Defensive hedge: ATM {atm} not in chain")
            return (None, {})

        expiry = self.dm.get_expiry_by_dte(
            config.LONG_STRADDLE_DTE_MIN, tolerance=5
        )
        if expiry is None:
            logger.warning("Defensive hedge: no valid expiry found")
            return (None, {})

        atm_put_data = chain[atm]["put"]
        atm_put_delta = abs(atm_put_data["delta"])

        remaining_delta = total_delta * (1 - config.DEFENSIVE_REDUCTION_PCT)
        hedge_qty = _math.ceil(
            abs(remaining_delta) / (atm_put_delta * config.LOT_SIZE + 1e-10)
        )
        hedge_qty = max(1, hedge_qty)

        legs = [
            Leg(
                instrument_key=atm_put_data["instrument_key"],
                option_type="put", action="BUY",
                strike=atm, expiry=expiry,
                qty=hedge_qty,
                delta=atm_put_data["delta"],
                gamma=atm_put_data["gamma"],
                vega=atm_put_data["vega"],
                theta=atm_put_data["theta"]
            )
        ]

        max_hold_date = (
            date.today() + timedelta(days=config.DEFENSIVE_MAX_HOLD_DAYS)
        ).strftime("%Y-%m-%d")

        meta = {
            "total_debit":    atm_put_data["ltp"] * hedge_qty,
            "max_risk":       atm_put_data["ltp"] * hedge_qty * config.LOT_SIZE,
            "stop_loss":      (
                atm_put_data["ltp"] * hedge_qty *
                (1 - config.EVENT_STRANGLE_STOP_PCT)
            ),
            "profit_target":  None,
            "exit_dte":       None,
            "max_hold_date":  max_hold_date,
            "strategy_type":  "LONG",
            "reduction_legs": reduction_legs,
            "hedge_qty":      hedge_qty
        }

        logger.info(
            f"Defensive hedge built: atm={atm} hedge_qty={hedge_qty} "
            f"debit={atm_put_data['ltp'] * hedge_qty:.2f}"
        )
        return (legs, meta)

    async def _pre_trade_checks(
        self, strategy_name: str, legs: List[Leg]
    ) -> bool:
        """Run all pre-trade validation checks."""
        # Check 1 — DTE for sell strategies
        for leg in legs:
            if leg.action == "SELL":
                try:
                    expiry_date = datetime.strptime(
                        leg.expiry, "%Y-%m-%d"
                    ).date()
                    dte = (expiry_date - date.today()).days
                    if dte < 5:
                        logger.warning(
                            f"Pre-trade: DTE={dte} < 5 for SELL leg "
                            f"strike={leg.strike}"
                        )
                        return False
                except ValueError:
                    logger.warning(
                        f"Pre-trade: invalid expiry format {leg.expiry}"
                    )
                    return False

        # Check 2 — Bid-ask spread per leg
        chain = self.dm.option_chain
        for leg in legs:
            strike_data = chain.get(leg.strike, {})
            opt_data = strike_data.get(leg.option_type, {})
            spread = opt_data.get("ask", 0) - opt_data.get("bid", 0)
            is_atm = (
                self.dm.atm_strike is not None and
                leg.strike == self.dm.atm_strike
            )
            max_spread = (
                config.MAX_SPREAD_ATM_PTS if is_atm
                else config.MAX_SPREAD_OTM_PTS
            )
            if spread > max_spread:
                logger.warning(
                    f"Pre-trade: spread={spread:.2f} > max={max_spread} "
                    f"for {leg.option_type} strike={leg.strike}"
                )
                return False

        # Check 3 — Portfolio risk limit
        estimated_risk = self._estimate_max_loss(strategy_name, legs)
        current_risk = sum(p.max_risk for p in self.open_positions)
        if current_risk + estimated_risk > config.MAX_COMBINED_RISK:
            logger.warning(
                f"Pre-trade: portfolio risk limit breach "
                f"current={current_risk:.0f} "
                f"new={estimated_risk:.0f} "
                f"max={config.MAX_COMBINED_RISK}"
            )
            return False

        # Check 4 — Greeks impact
        new_greeks = self._estimate_greeks_impact(legs)
        regime = self.re.confirmed_regime
        limits = config.GREEKS_LIMITS.get(regime, {})
        portfolio_greeks = self._get_portfolio_greeks()

        post_delta = portfolio_greeks["delta"] + new_greeks["delta"]
        delta_max = limits.get("delta_max", 99)
        if abs(post_delta) > delta_max:
            logger.warning(
                f"Pre-trade: delta limit breach "
                f"post_delta={post_delta:.3f} max={delta_max}"
            )
            return False

        # Check 5 — Margin check (live mode only)
        if not config.PAPER_TRADING_MODE:
            margin_legs = [
                {
                    "instrument_key":   leg.instrument_key,
                    "quantity":         leg.qty * config.LOT_SIZE,
                    "transaction_type": leg.action,
                    "product":          "D",
                    "price":            leg.entry_price
                }
                for leg in legs
            ]
            margin_ok, required = await self.dm.check_margin(margin_legs)
            if not margin_ok:
                logger.warning(
                    f"Pre-trade: insufficient margin required={required:.0f}"
                )
                return False

        logger.info(
            f"Pre-trade checks passed for {strategy_name} "
            f"estimated_risk={estimated_risk:.0f}"
        )
        return True

    async def _execute_strategy(
        self,
        strategy_name: str,
        legs: List[Leg],
        meta: Dict
    ) -> bool:
        """Execute all legs of a strategy in correct order."""
        # Handle defensive hedge — reduce shorts first
        if strategy_name == config.STRAT_DEFENSIVE:
            reduction_success = await self._execute_reductions(
                meta.get("reduction_legs", [])
            )
            if not reduction_success:
                logger.warning(
                    "Defensive: short reduction failed — abort hedge"
                )
                return False
            await asyncio.sleep(config.ORDER_BETWEEN_LEGS_DELAY_SEC)

        long_legs = [l for l in legs if l.action == "BUY"]
        short_legs = [l for l in legs if l.action == "SELL"]

        filled_legs: List[Leg] = []

        # Execute long legs first (RULE O1)
        for leg in long_legs:
            success, order_id = await self._place_single_leg(
                leg, use_market=False
            )
            if not success:
                logger.warning(
                    f"Long leg failed: {leg.option_type} "
                    f"strike={leg.strike} — aborting strategy"
                )
                await self._cancel_and_reverse(filled_legs)
                return False
            leg.order_id = order_id
            leg.fill_status = "COMPLETE"
            filled_legs.append(leg)
            self._log_order("PENDING", order_id, leg, "FILLED", leg.entry_price)
            await asyncio.sleep(config.ORDER_BETWEEN_LEGS_DELAY_SEC)

        # Execute short legs second (RULE O1)
        for leg in short_legs:
            success, order_id = await self._place_single_leg(
                leg, use_market=False
            )
            if not success:
                logger.warning(
                    f"Short leg failed: {leg.option_type} "
                    f"strike={leg.strike} — aborting strategy"
                )
                await self._cancel_and_reverse(filled_legs)
                return False
            leg.order_id = order_id
            leg.fill_status = "COMPLETE"
            filled_legs.append(leg)
            self._log_order("PENDING", order_id, leg, "FILLED", leg.entry_price)
            await asyncio.sleep(config.ORDER_BETWEEN_LEGS_DELAY_SEC)

        logger.info(
            f"All {len(filled_legs)} legs filled for {strategy_name}"
        )
        return True

    async def _place_single_leg(
        self, leg: Leg, use_market: bool = False
    ) -> Tuple[bool, str]:
        """Place a single option order and wait for fill."""
        if config.PAPER_TRADING_MODE:
            return await self._simulate_fill(leg)

        chain = self.dm.option_chain
        opt_data = chain.get(leg.strike, {}).get(leg.option_type, {})

        if use_market:
            order_type = "MARKET"
            price = 0
        else:
            order_type = "LIMIT"
            if leg.action == "BUY":
                price = (
                    opt_data.get("ask", 0) +
                    config.ORDER_AGGRESSION_TICKS * config.TICK_SIZE
                )
            else:
                price = (
                    opt_data.get("bid", 0) -
                    config.ORDER_AGGRESSION_TICKS * config.TICK_SIZE
                )
            price = max(config.TICK_SIZE, round(price / config.TICK_SIZE) * config.TICK_SIZE)

        payload = {
            "quantity":          leg.qty * config.LOT_SIZE,
            "product":           "D",
            "validity":          "DAY",
            "price":             price,
            "tag":               "NIFTY_ALGO",
            "instrument_token":  leg.instrument_key,
            "order_type":        order_type,
            "transaction_type":  leg.action,
            "disclosed_quantity": 0,
            "trigger_price":     0,
            "is_amo":            False
        }

        try:
            response = await self.dm._api_post(config.EP_ORDER_PLACE, payload)
            order_id = (
                response.get("data", {}).get("order_id", "") or
                response.get("order_id", "")
            )
            if not order_id:
                logger.warning(
                    f"No order_id in response for "
                    f"{leg.action} {leg.option_type} {leg.strike}"
                )
                return (False, "")

            logger.info(
                f"Order placed: {order_id} "
                f"{leg.action} {leg.option_type} {leg.strike} "
                f"qty={leg.qty * config.LOT_SIZE} price={price}"
            )

            filled = await self._wait_for_fill(
                order_id, config.CORE_FILL_TIMEOUT_SEC
            )
            if not filled:
                await self._cancel_order(order_id)
                return (False, order_id)

            fill_price = await self._get_fill_price(order_id)
            leg.entry_price = fill_price if fill_price > 0 else price

            expected = price if order_type == "LIMIT" else opt_data.get("ltp", price)
            slippage = abs(leg.entry_price - expected)
            leg.slippage_pts = slippage
            if slippage > 2:
                logger.warning(
                    f"High slippage: {slippage:.2f} pts for "
                    f"{leg.action} {leg.option_type} {leg.strike}"
                )

            return (True, order_id)

        except Exception as e:
            logger.error(
                f"Order placement error for "
                f"{leg.action} {leg.option_type} {leg.strike}: {e}"
            )
            return (False, "")

    async def _simulate_fill(self, leg: Leg
    ) -> Tuple[bool, str]:
        """Simulate order fill for paper trading with slippage model."""
        chain = self.dm.option_chain
        opt_data = chain.get(leg.strike, {}).get(leg.option_type, {})
        ltp = opt_data.get("ltp", 0)

        if ltp == 0 or ltp is None:
            logger.warning(
                f"Paper fill: LTP=0 for {leg.option_type} "
                f"strike={leg.strike} — cannot simulate fill"
            )
            return (False, "")

        if leg.action == "SELL":
            slippage = config.PAPER_SLIPPAGE_SHORT_TICKS * config.TICK_SIZE
            fill_price = ltp - slippage
        else:
            slippage = config.PAPER_SLIPPAGE_HEDGE_TICKS * config.TICK_SIZE
            fill_price = ltp + slippage

        fill_price = max(
            config.TICK_SIZE,
            round(fill_price / config.TICK_SIZE) * config.TICK_SIZE
        )

        leg.entry_price = fill_price
        leg.slippage_pts = slippage
        leg.fill_status = "COMPLETE"

        order_id = f"PAPER_{uuid.uuid4().hex[:8]}"

        logger.info(
            f"Paper fill: {leg.action} {leg.option_type} "
            f"strike={leg.strike} ltp={ltp:.2f} "
            f"fill={fill_price:.2f} slippage={slippage:.2f}"
        )
        return (True, order_id)

    async def _wait_for_fill(
        self, order_id: str, timeout_sec: int
    ) -> bool:
        """Poll order status until filled or timeout."""
        start = asyncio.get_event_loop().time()

        while True:
            elapsed = asyncio.get_event_loop().time() - start
            if elapsed > timeout_sec:
                logger.warning(
                    f"Fill timeout after {timeout_sec}s: order_id={order_id}"
                )
                return False

            await asyncio.sleep(config.ORDER_POLL_INTERVAL_SEC)

            status = await self._get_order_status(order_id)
            if status == "complete":
                logger.info(f"Order filled: {order_id}")
                return True
            elif status in ["rejected", "cancelled"]:
                logger.warning(
                    f"Order {status}: order_id={order_id}"
                )
                return False
            # Still pending — continue polling

    async def _get_order_status(self, order_id: str) -> str:
        """Fetch current order status from broker."""
        try:
            await asyncio.sleep(config.ORDER_STATUS_POLL_DELAY_SEC)
            response = await self.dm._api_get(
                config.EP_ORDER_HISTORY,
                {"order_id": order_id}
            )
            orders = response if isinstance(response, list) else []
            if orders:
                return str(orders[-1].get("status", "unknown")).lower()
            return "unknown"
        except Exception as e:
            logger.warning(f"_get_order_status error for {order_id}: {e}")
            return "unknown"

    async def _get_fill_price(self, order_id: str) -> float:
        """Fetch average fill price for completed order."""
        try:
            response = await self.dm._api_get(
                config.EP_ORDER_HISTORY,
                {"order_id": order_id}
            )
            orders = response if isinstance(response, list) else []
            if orders:
                return float(orders[-1].get("average_price", 0))
            return 0.0
        except Exception as e:
            logger.warning(f"_get_fill_price error for {order_id}: {e}")
            return 0.0

    async def _cancel_order(self, order_id: str) -> None:
        """Cancel a pending order."""
        try:
            await self.dm._api_delete(
                f"{config.EP_ORDER_CANCEL}/{order_id}"
            )
            logger.info(f"Order cancelled: {order_id}")
        except Exception as e:
            logger.warning(f"Cancel failed for {order_id}: {e}")

    async def _cancel_and_reverse(self, filled_legs: List[Leg]) -> None:
        """Reverse all filled legs at market to abort partial position."""
        logger.warning(
            f"Aborting strategy — reversing {len(filled_legs)} filled legs"
        )
        for leg in reversed(filled_legs):
            reverse_action = "BUY" if leg.action == "SELL" else "SELL"
            reverse_leg = Leg(
                instrument_key=leg.instrument_key,
                option_type=leg.option_type,
                action=reverse_action,
                strike=leg.strike,
                expiry=leg.expiry,
                qty=leg.qty
            )
            try:
                await self._place_single_leg(reverse_leg, use_market=True)
                logger.info(
                    f"Reversed: {reverse_action} {leg.option_type} "
                    f"strike={leg.strike}"
                )
            except Exception as e:
                logger.error(
                    f"Reverse failed for {leg.option_type} "
                    f"strike={leg.strike}: {e}"
                )
            await asyncio.sleep(config.ORDER_BETWEEN_LEGS_DELAY_SEC)

    async def _close_position(
        self,
        position: Position,
        exit_reason: str,
        use_market: bool = False
    ) -> None:
        """Close all legs of a position."""
        if position.status != "OPEN":
            return

        logger.info(
            f"Closing position: {position.trade_id[:8]} "
            f"strategy={position.strategy_name} "
            f"reason={exit_reason}"
        )

        use_market_order = (
            use_market or
            exit_reason in [
                config.EXIT_REASONS["STOP_LOSS"],
                config.EXIT_REASONS["CIRCUIT_BREAK"],
                config.EXIT_REASONS["REGIME_CHANGE"]
            ]
        )

        # Close short legs first to free margin
        short_legs = [l for l in position.legs if l.action == "SELL"]
        long_legs = [l for l in position.legs if l.action == "BUY"]

        for leg in short_legs + long_legs:
            if leg.qty <= 0:
                continue

            close_action = "BUY" if leg.action == "SELL" else "SELL"
            close_leg = Leg(
                instrument_key=leg.instrument_key,
                option_type=leg.option_type,
                action=close_action,
                strike=leg.strike,
                expiry=leg.expiry,
                qty=leg.qty
            )

            success, order_id = await self._place_single_leg(
                close_leg, use_market=use_market_order
            )

            if not success:
                logger.warning(
                    f"Close leg failed — retrying at market: "
                    f"{leg.option_type} strike={leg.strike}"
                )
                retry_leg = Leg(
                    instrument_key=leg.instrument_key,
                    option_type=leg.option_type,
                    action=close_action,
                    strike=leg.strike,
                    expiry=leg.expiry,
                    qty=leg.qty
                )
                await self._place_single_leg(retry_leg, use_market=True)
                leg.exit_price = retry_leg.entry_price
            else:
                leg.exit_price = close_leg.entry_price

            await asyncio.sleep(config.ORDER_BETWEEN_LEGS_DELAY_SEC)

        # Record exit data
        IST = pytz.timezone(config.TZ)
        position.exit_reason = exit_reason
        position.exit_timestamp = datetime.now(IST).isoformat()
        position.exit_spot = self.dm.spot or 0.0
        position.exit_vix = self.dm.vix or 0.0
        position.status = "CLOSED"
        position.realized_pnl = self._calculate_final_pnl(position)
        position.realized_pnl_percent = (
            (position.realized_pnl / config.TOTAL_CAPITAL) * 100
            if config.TOTAL_CAPITAL > 0 else 0.0
        )

        if position in self.open_positions:
            self.open_positions.remove(position)
        self.closed_positions.append(position)

        self.daily_pnl += position.realized_pnl
        self.weekly_pnl += position.realized_pnl
        self.current_capital += position.realized_pnl
        if self.current_capital > self.peak_capital:
            self.peak_capital = self.current_capital

        self.dm.close_position(
            position.trade_id,
            self._position_to_dict(position)
        )

        logger.info(
            f"Position closed: {position.trade_id[:8]} "
            f"pnl=₹{position.realized_pnl:,.2f} "
            f"reason={exit_reason}"
        )

    def _calculate_lot_size(
        self, strategy_name: str, meta: Dict
    ) -> int:
        """Calculate appropriate lot size based on risk and regime limits."""
        max_loss_per_lot = meta.get("max_risk", 0)
        if max_loss_per_lot <= 0:
            logger.warning(
                f"Cannot compute max loss for {strategy_name} — using 1 lot"
            )
            return 1

        risk_per_trade = config.MAX_RISK_PER_TRADE
        lots = math.floor(risk_per_trade / max_loss_per_lot)

        regime = self.re.confirmed_regime
        max_lots = config.REGIME_MAX_LOTS.get(regime, 1)
        lots = min(lots, max_lots)
        lots = max(lots, 1)

        regime_capital = (
            config.REGIME_CAPITAL_PCT.get(regime, 0) * config.TOTAL_CAPITAL
        )
        deployed_capital = sum(p.max_risk for p in self.open_positions)
        available_capital = regime_capital - deployed_capital

        if available_capital <= 0:
            logger.info(
                f"No capital available for regime={regime}"
            )
            return 0

        # Special cap for ratio spread
        if strategy_name == config.STRAT_RATIO_SPREAD:
            ratio_cap = config.RATIO_MAX_CAPITAL_PCT * config.TOTAL_CAPITAL
            available_capital = min(available_capital, ratio_cap)

        lots_by_capital = math.floor(available_capital / max_loss_per_lot)
        lots = min(lots, lots_by_capital)
        lots = max(lots, 0)

        logger.info(
            f"Lot size: {lots} for {strategy_name} "
            f"risk_per_trade={risk_per_trade} "
            f"max_loss_per_lot={max_loss_per_lot:.0f} "
            f"available_capital={available_capital:.0f}"
        )
        return lots

    def _calculate_final_pnl(self, position: Position) -> float:
        """Calculate realized P&L from entry and exit prices."""
        total_pnl = 0.0
        for leg in position.legs:
            if leg.exit_price == 0:
                continue
            if leg.action == "SELL":
                leg_pnl = (
                    (leg.entry_price - leg.exit_price) *
                    leg.qty * config.LOT_SIZE
                )
            else:
                leg_pnl = (
                    (leg.exit_price - leg.entry_price) *
                    leg.qty * config.LOT_SIZE
                )
            total_pnl += leg_pnl
        return total_pnl

    def _get_position_value(self, position: Position) -> float:
        """Get current total value of position from live LTP."""
        total_value = 0.0
        chain = self.dm.option_chain
        for leg in position.legs:
            ltp = (
                chain.get(leg.strike, {})
                .get(leg.option_type, {})
                .get("ltp", 0)
            )
            if ltp == 0:
                ltp = leg.entry_price
            total_value += ltp * leg.qty
        return total_value

    def _get_position_current_premium(self, position: Position) -> float:
        """Get current total premium (for credit strategies)."""
        total_premium = 0.0
        chain = self.dm.option_chain
        for leg in position.legs:
            if leg.action == "SELL":
                ltp = (
                    chain.get(leg.strike, {})
                    .get(leg.option_type, {})
                    .get("ltp", 0)
                )
                if ltp == 0:
                    ltp = leg.entry_price
                total_premium += ltp * leg.qty
        return total_premium

    def _get_portfolio_greeks(self) -> Dict[str, float]:
        """Compute aggregate portfolio Greeks across all open positions."""
        total_delta = 0.0
        total_gamma = 0.0
        total_vega = 0.0
        total_theta = 0.0

        for position in self.open_positions:
            for leg in position.legs:
                sign = +1 if leg.action == "BUY" else -1
                total_delta += sign * leg.delta * leg.qty * config.LOT_SIZE
                total_gamma += sign * leg.gamma * leg.qty * config.LOT_SIZE
                total_vega += sign * leg.vega * leg.qty * config.LOT_SIZE
                total_theta += sign * leg.theta * leg.qty * config.LOT_SIZE

        return {
            "delta": total_delta,
            "gamma": total_gamma,
            "vega":  total_vega,
            "theta": total_theta
        }

    def _estimate_greeks_impact(self, legs: List[Leg]) -> Dict[str, float]:
        """Estimate Greeks impact of new legs on portfolio."""
        delta = 0.0
        gamma = 0.0
        vega = 0.0
        theta = 0.0
        for leg in legs:
            sign = +1 if leg.action == "BUY" else -1
            delta += sign * leg.delta * leg.qty * config.LOT_SIZE
            gamma += sign * leg.gamma * leg.qty * config.LOT_SIZE
            vega += sign * leg.vega * leg.qty * config.LOT_SIZE
            theta += sign * leg.theta * leg.qty * config.LOT_SIZE
        return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta}

    def _estimate_max_loss(
        self, strategy_name: str, legs: List[Leg]
    ) -> float:
        """Estimate maximum possible loss for strategy."""
        if strategy_name == config.STRAT_SHORT_STRADDLE:
            total_prem = sum(
                l.entry_price for l in legs if l.action == "SELL"
            )
            return total_prem * config.STRADDLE_STOP_PCT * config.LOT_SIZE

        elif strategy_name == config.STRAT_IRON_CONDOR:
            net_credit = (
                sum(l.entry_price for l in legs if l.action == "SELL") -
                sum(l.entry_price for l in legs if l.action == "BUY")
            )
            return (config.CONDOR_WING_WIDTH - net_credit) * config.LOT_SIZE

        elif strategy_name == config.STRAT_CREDIT_SPREADS:
            net_credit = (
                sum(l.entry_price for l in legs if l.action == "SELL") -
                sum(l.entry_price for l in legs if l.action == "BUY")
            )
            spread_width = config.CONDOR_WING_WIDTH / 2
            return max(0, (spread_width - net_credit) * config.LOT_SIZE)

        elif strategy_name in [
            config.STRAT_LONG_STRADDLE,
            config.STRAT_STRANGLE,
            config.STRAT_BUTTERFLY,
            config.STRAT_BACKSPREAD,
            config.STRAT_DEFENSIVE
        ]:
            total_debit = (
                sum(l.entry_price for l in legs if l.action == "BUY") -
                sum(l.entry_price for l in legs if l.action == "SELL")
            )
            return max(0, total_debit * config.LOT_SIZE)

        elif strategy_name == config.STRAT_RATIO_SPREAD:
            total_debit = (
                sum(l.entry_price for l in legs if l.action == "BUY") -
                sum(l.entry_price for l in legs if l.action == "SELL")
            )
            return max(0, total_debit * 2 * config.LOT_SIZE)

        return float(config.MAX_RISK_PER_TRADE)

    def _check_greeks_limits(self) -> None:
        """Check portfolio Greeks against regime limits and hedge if needed."""
        regime = self.re.confirmed_regime
        limits = config.GREEKS_LIMITS.get(regime, {})
        greeks = self._get_portfolio_greeks()

        delta_max = limits.get("delta_max", 99)
        if abs(greeks["delta"]) > delta_max:
            logger.warning(
                f"Delta breach: {greeks['delta']:.3f} > limit={delta_max} "
                f"— scheduling hedge"
            )
            asyncio.create_task(
                self._hedge_delta(greeks["delta"], delta_max)
            )

        gamma_max = limits.get("gamma_max", 99)
        gamma_min = limits.get("gamma_min", -99)
        if gamma_min is not None and greeks["gamma"] < gamma_min:
            logger.warning(
                f"Gamma below minimum: {greeks['gamma']:.5f} < {gamma_min}"
            )
        if gamma_max is not None and greeks["gamma"] > gamma_max:
            logger.warning(
                f"Gamma above maximum: {greeks['gamma']:.5f} > {gamma_max}"
            )

        vega_max = limits.get("vega_max", 99999)
        vega_min = limits.get("vega_min", -99999)
        if vega_min is not None and greeks["vega"] < vega_min:
            logger.warning(
                f"Vega below minimum: {greeks['vega']:.1f} < {vega_min}"
            )
        if vega_max is not None and greeks["vega"] > vega_max:
            logger.warning(
                f"Vega above maximum: {greeks['vega']:.1f} > {vega_max}"
            )

        theta_min = limits.get("theta_min")
        if theta_min is not None:
            if greeks["theta"] < theta_min:
                logger.warning(
                    f"Theta below minimum: {greeks['theta']:.1f} < {theta_min}"
                )

    async def _hedge_delta(
        self, current_delta: float, delta_limit: float
    ) -> None:
        """Hedge excess delta using Nifty futures."""
        excess = abs(current_delta) - delta_limit
        if excess <= 0:
            return

        futures_lots = math.ceil(excess / 1.0)
        action = "SELL" if current_delta > delta_limit else "BUY"

        if config.PAPER_TRADING_MODE:
            logger.info(
                f"Paper delta hedge: {action} {futures_lots} Nifty futures "
                f"current_delta={current_delta:.3f} limit={delta_limit}"
            )
            return

        payload = {
            "quantity":          futures_lots * config.LOT_SIZE,
            "product":           "D",
            "validity":          "DAY",
            "price":             0,
            "instrument_token":  config.INSTRUMENT_NIFTY_FUT,
            "order_type":        "MARKET",
            "transaction_type":  action,
            "disclosed_quantity": 0,
            "trigger_price":     0,
            "is_amo":            False
        }

        try:
            await self.dm._api_post(config.EP_ORDER_PLACE, payload)
            logger.info(
                f"Delta hedge placed: {action} {futures_lots} lots "
                f"excess_delta={excess:.3f}"
            )
        except Exception as e:
            logger.error(f"Delta hedge failed: {e}")

    async def _reduce_position_50pct(self, position: Position) -> None:
        """Reduce all short legs of position by 50%."""
        for leg in position.legs:
            if leg.action == "SELL":
                reduce_qty = math.floor(leg.qty * 0.50)
                if reduce_qty < 1:
                    continue
                close_leg = Leg(
                    instrument_key=leg.instrument_key,
                    option_type=leg.option_type,
                    action="BUY",
                    strike=leg.strike,
                    expiry=leg.expiry,
                    qty=reduce_qty
                )
                success, _ = await self._place_single_leg(
                    close_leg, use_market=False
                )
                if success:
                    leg.qty -= reduce_qty
                    logger.info(
                        f"Reduced {leg.option_type} strike={leg.strike} "
                        f"by {reduce_qty} lots"
                    )
                await asyncio.sleep(config.ORDER_BETWEEN_LEGS_DELAY_SEC)
        self._move_stop_to_breakeven(position)

    async def _reduce_position_pct(
        self, position: Position, pct: float
    ) -> None:
        """Reduce all short legs of position by given percentage."""
        for leg in position.legs:
            if leg.action == "SELL":
                reduce_qty = math.floor(leg.qty * pct)
                if reduce_qty < 1:
                    continue
                close_leg = Leg(
                    instrument_key=leg.instrument_key,
                    option_type=leg.option_type,
                    action="BUY",
                    strike=leg.strike,
                    expiry=leg.expiry,
                    qty=reduce_qty
                )
                success, _ = await self._place_single_leg(
                    close_leg, use_market=False
                )
                if success:
                    leg.qty -= reduce_qty
                    logger.info(
                        f"Reduced {leg.option_type} strike={leg.strike} "
                        f"by {reduce_qty} lots ({pct * 100:.0f}%)"
                    )
                await asyncio.sleep(config.ORDER_BETWEEN_LEGS_DELAY_SEC)

    async def _convert_shorts_to_spreads(self, position: Position) -> None:
        """Add hedge legs to convert naked shorts into spreads."""
        chain = self.dm.option_chain
        for leg in position.legs:
            if leg.action == "SELL":
                if leg.option_type == "put":
                    hedge_strike = leg.strike - config.CONDOR_WING_WIDTH // 3
                else:
                    hedge_strike = leg.strike + config.CONDOR_WING_WIDTH // 3

                hedge_strike = (
                    round(hedge_strike / config.NIFTY_STRIKE_STEP) *
                    config.NIFTY_STRIKE_STEP
                )

                if hedge_strike not in chain:
                    logger.warning(
                        f"Hedge strike {hedge_strike} not found in chain "
                        f"for {leg.option_type} short at {leg.strike}"
                    )
                    continue

                hedge_leg = Leg(
                    instrument_key=chain[hedge_strike][
                        leg.option_type
                    ]["instrument_key"],
                    option_type=leg.option_type,
                    action="BUY",
                    strike=hedge_strike,
                    expiry=leg.expiry,
                    qty=leg.qty,
                    delta=chain[hedge_strike][leg.option_type]["delta"],
                    gamma=chain[hedge_strike][leg.option_type]["gamma"],
                    vega=chain[hedge_strike][leg.option_type]["vega"],
                    theta=chain[hedge_strike][leg.option_type]["theta"]
                )
                success, order_id = await self._place_single_leg(
                    hedge_leg, use_market=False
                )
                if success:
                    position.legs.append(hedge_leg)
                    logger.info(
                        f"Converted short {leg.strike} to spread "
                        f"with hedge at {hedge_strike}"
                    )
                await asyncio.sleep(config.ORDER_BETWEEN_LEGS_DELAY_SEC)

    def _move_stop_to_breakeven(self, position: Position) -> None:
        """Move stop loss to breakeven for position."""
        if position.strategy_name == config.STRAT_SHORT_STRADDLE:
            position.stop_loss = position.entry_spot
            logger.info(
                f"Stop moved to breakeven: {position.entry_spot:.2f} "
                f"for {position.trade_id[:8]}"
            )
        elif position.strategy_name in [
            config.STRAT_IRON_CONDOR,
            config.STRAT_CREDIT_SPREADS
        ]:
            position.stop_loss = 0.0
            logger.info(
                f"Stop moved to breakeven (credit recovered) "
                f"for {position.trade_id[:8]}"
            )

    async def _emergency_flatten_all(self) -> None:
        """Emergency close all positions at market."""
        logger.critical("EMERGENCY: Flattening all positions at market")
        for position in list(self.open_positions):
            await self._close_position(
                position,
                config.EXIT_REASONS["CIRCUIT_BREAK"],
                use_market=True
            )
        logger.critical("All positions flattened")

    async def _reduce_all_positions_50pct(self) -> None:
        """Reduce all open positions by 50% (CB Level 3)."""
        logger.warning("CB L3: Reducing all positions by 50%")
        for position in list(self.open_positions):
            await self._reduce_position_50pct(position)

    async def _execute_reductions(
        self, reduction_legs: List[Dict]
    ) -> bool:
        """Execute short leg reductions for defensive hedge."""
        for item in reduction_legs:
            leg = item["leg"]
            reduce_qty = item["reduce_qty"]
            if reduce_qty < 1:
                continue

            close_leg = Leg(
                instrument_key=leg.instrument_key,
                option_type=leg.option_type,
                action="BUY",
                strike=leg.strike,
                expiry=leg.expiry,
                qty=reduce_qty
            )
            success, _ = await self._place_single_leg(
                close_leg, use_market=True
            )
            if not success:
                logger.warning(
                    f"Reduction failed for strike={leg.strike}"
                )
                return False
            leg.qty -= reduce_qty
            await asyncio.sleep(config.ORDER_BETWEEN_LEGS_DELAY_SEC)
        return True

    async def _monitor_condor_adjustment(
        self, position: Position
    ) -> None:
        """Monitor and adjust iron condor when tested side approaches."""
        if position.strategy_name != config.STRAT_IRON_CONDOR:
            return

        spot = self.dm.spot
        if spot is None:
            return

        meta = position.meta
        short_call = meta.get("short_call", 0)
        short_put = meta.get("short_put", 0)

        if short_call == 0 or short_put == 0:
            return

        buffer = config.CONDOR_TESTED_SIDE_BUFFER

        if spot >= short_call - buffer:
            logger.info(
                f"Condor call side tested: spot={spot:.2f} "
                f"short_call={short_call:.2f} buffer={buffer}"
            )
            await self._close_one_side(
                position, "put", config.EXIT_REASONS["PROFIT_TARGET"]
            )
            await self._roll_condor_side(position, "call")

        elif spot <= short_put + buffer:
            logger.info(
                f"Condor put side tested: spot={spot:.2f} "
                f"short_put={short_put:.2f} buffer={buffer}"
            )
            await self._close_one_side(
                position, "call", config.EXIT_REASONS["PROFIT_TARGET"]
            )
            await self._roll_condor_side(position, "put")

    async def _roll_condor_side(
        self, position: Position, side: str
    ) -> None:
        """Roll tested side of condor to new OTM strikes."""
        chain = self.dm.option_chain

        existing_legs = [
            l for l in position.legs if l.option_type == side
        ]

        for leg in existing_legs:
            close_action = "BUY" if leg.action == "SELL" else "SELL"
            close_leg = Leg(
                instrument_key=leg.instrument_key,
                option_type=side,
                action=close_action,
                strike=leg.strike,
                expiry=leg.expiry,
                qty=leg.qty
            )
            await self._place_single_leg(close_leg, use_market=True)
            await asyncio.sleep(config.ORDER_BETWEEN_LEGS_DELAY_SEC)

        if side == "call":
            new_short = self.dm.get_strike_by_delta(
                "call", config.CONDOR_ADJUSTMENT_DELTA
            )
            if new_short is None:
                return
            new_long = new_short + config.CONDOR_WING_WIDTH
        else:
            new_short = self.dm.get_strike_by_delta(
                "put", config.CONDOR_ADJUSTMENT_DELTA
            )
            if new_short is None:
                return
            new_long = new_short - config.CONDOR_WING_WIDTH

        if new_short not in chain or new_long not in chain:
            logger.warning(
                f"Roll {side}: new strikes not in chain "
                f"short={new_short} long={new_long}"
            )
            return

        qty = existing_legs[0].qty if existing_legs else 1

        new_long_leg = Leg(
            instrument_key=chain[new_long][side]["instrument_key"],
            option_type=side, action="BUY",
            strike=new_long, expiry=position.expiry_date, qty=qty,
            delta=chain[new_long][side]["delta"],
            gamma=chain[new_long][side]["gamma"],
            vega=chain[new_long][side]["vega"],
            theta=chain[new_long][side]["theta"]
        )
        new_short_leg = Leg(
            instrument_key=chain[new_short][side]["instrument_key"],
            option_type=side, action="SELL",
            strike=new_short, expiry=position.expiry_date, qty=qty,
            delta=chain[new_short][side]["delta"],
            gamma=chain[new_short][side]["gamma"],
            vega=chain[new_short][side]["vega"],
            theta=chain[new_short][side]["theta"]
        )

        await self._place_single_leg(new_long_leg, use_market=False)
        await asyncio.sleep(config.ORDER_BETWEEN_LEGS_DELAY_SEC)
        await self._place_single_leg(new_short_leg, use_market=False)

        position.legs = [
            l for l in position.legs if l.option_type != side
        ]
        position.legs.extend([new_long_leg, new_short_leg])

        if side == "call":
            position.meta["short_call"] = new_short
            position.meta["long_call"] = new_long
        else:
            position.meta["short_put"] = new_short
            position.meta["long_put"] = new_long

        logger.info(
            f"Condor {side} side rolled: "
            f"short={new_short} long={new_long}"
        )

    async def _monitor_spread_delta(self, position: Position) -> None:
        """Monitor credit spread delta and roll if trigger breached."""
        if position.strategy_name != config.STRAT_CREDIT_SPREADS:
            return

        chain = self.dm.option_chain

        for side in ["call", "put"]:
            short_strike = self._get_short_strike(position, side)
            if short_strike and short_strike in chain:
                delta = abs(chain[short_strike][side]["delta"])
                if delta >= config.SPREAD_ROLL_DELTA_TRIGGER:
                    logger.info(
                        f"Credit spread {side} delta={delta:.2f} >= "
                        f"trigger={config.SPREAD_ROLL_DELTA_TRIGGER}"
                    )
                    await self._roll_spread_side(position, side)

    async def _roll_spread_side(
        self, position: Position, side: str
    ) -> None:
        """Roll one side of credit spread to new delta strikes."""
        chain = self.dm.option_chain

        if side == "call":
            new_short = self.dm.get_strike_by_delta(
                "call", config.SPREAD_DELTA_SHORT
            )
            new_long = self.dm.get_strike_by_delta(
                "call", config.SPREAD_DELTA_LONG
            )
        else:
            new_short = self.dm.get_strike_by_delta(
                "put", config.SPREAD_DELTA_SHORT
            )
            new_long = self.dm.get_strike_by_delta(
                "put", config.SPREAD_DELTA_LONG
            )

        if new_short is None or new_long is None:
            logger.warning(f"Roll spread {side}: cannot find new strikes")
            return

        existing = [
            l for l in position.legs if l.option_type == side
        ]
        for leg in existing:
            close_action = "BUY" if leg.action == "SELL" else "SELL"
            close_leg = Leg(
                instrument_key=leg.instrument_key,
                option_type=side, action=close_action,
                strike=leg.strike, expiry=leg.expiry, qty=leg.qty
            )
            await self._place_single_leg(close_leg, use_market=True)
            await asyncio.sleep(config.ORDER_BETWEEN_LEGS_DELAY_SEC)

        if new_short not in chain or new_long not in chain:
            logger.warning(
                f"Roll spread {side}: new strikes not in chain"
            )
            return

        qty = existing[0].qty if existing else 1

        new_long_leg = Leg(
            instrument_key=chain[new_long][side]["instrument_key"],
            option_type=side, action="BUY",
            strike=new_long, expiry=position.expiry_date, qty=qty,
            delta=chain[new_long][side]["delta"],
            gamma=chain[new_long][side]["gamma"],
            vega=chain[new_long][side]["vega"],
            theta=chain[new_long][side]["theta"]
        )
        new_short_leg = Leg(
            instrument_key=chain[new_short][side]["instrument_key"],
            option_type=side, action="SELL",
            strike=new_short, expiry=position.expiry_date, qty=qty,
            delta=chain[new_short][side]["delta"],
            gamma=chain[new_short][side]["gamma"],
            vega=chain[new_short][side]["vega"],
            theta=chain[new_short][side]["theta"]
        )

        await self._place_single_leg(new_long_leg, use_market=False)
        await asyncio.sleep(config.ORDER_BETWEEN_LEGS_DELAY_SEC)
        await self._place_single_leg(new_short_leg, use_market=False)

        position.legs = [
            l for l in position.legs if l.option_type != side
        ]
        position.legs.extend([new_long_leg, new_short_leg])

        logger.info(
            f"Spread {side} rolled: short={new_short} long={new_long}"
        )

    async def _monitor_ratio_delta(self, position: Position) -> None:
        """Monitor ratio spread delta and close side if trigger breached."""
        if position.strategy_name != config.STRAT_RATIO_SPREAD:
            return

        chain = self.dm.option_chain
        IST = pytz.timezone(config.TZ)

        for side in ["call", "put"]:
            short_strike = self._get_short_strike(position, side)
            if short_strike and short_strike in chain:
                delta = abs(chain[short_strike][side]["delta"])
                if delta >= config.RATIO_DELTA_EXIT_TRIGGER:
                    logger.info(
                        f"Ratio spread {side} delta={delta:.2f} >= "
                        f"trigger={config.RATIO_DELTA_EXIT_TRIGGER} "
                        f"— closing {side} side"
                    )
                    side_legs = [
                        l for l in position.legs if l.option_type == side
                    ]
                    for leg in side_legs:
                        close_action = (
                            "BUY" if leg.action == "SELL" else "SELL"
                        )
                        close_leg = Leg(
                            instrument_key=leg.instrument_key,
                            option_type=side,
                            action=close_action,
                            strike=leg.strike,
                            expiry=leg.expiry,
                            qty=leg.qty
                        )
                        await self._place_single_leg(
                            close_leg, use_market=True
                        )
                        await asyncio.sleep(config.ORDER_BETWEEN_LEGS_DELAY_SEC)

                    position.legs = [
                        l for l in position.legs if l.option_type != side
                    ]
                    logger.info(f"Ratio spread {side} side closed")

        # If all legs closed: mark position closed
        if not position.legs:
            position.status = "CLOSED"
            position.exit_reason = config.EXIT_REASONS["STOP_LOSS"]
            position.exit_timestamp = datetime.now(IST).isoformat()
            position.exit_spot = self.dm.spot or 0.0
            position.exit_vix = self.dm.vix or 0.0
            position.realized_pnl = self._calculate_final_pnl(position)
            position.realized_pnl_percent = (
                (position.realized_pnl / config.TOTAL_CAPITAL) * 100
                if config.TOTAL_CAPITAL > 0 else 0.0
            )
            if position in self.open_positions:
                self.open_positions.remove(position)
            self.closed_positions.append(position)
            self.dm.close_position(
                position.trade_id,
                self._position_to_dict(position)
            )
            logger.info(
                f"Ratio spread fully closed: {position.trade_id[:8]}"
            )

    async def _monitor_backspread_adjustment(
        self, position: Position
    ) -> None:
        """Monitor backspread and roll short if breached."""
        if position.strategy_name != config.STRAT_BACKSPREAD:
            return

        spot = self.dm.spot
        if spot is None:
            return

        trend = position.trend_direction

        if trend >= 0:
            short_call = self._get_short_strike(position, "call")
            if short_call and spot > short_call:
                logger.info(
                    f"Backspread short call breached: "
                    f"spot={spot:.2f} short_call={short_call:.2f}"
                )
                await self._roll_backspread_short(position, "call")
        else:
            short_put = self._get_short_strike(position, "put")
            if short_put and spot < short_put:
                logger.info(
                    f"Backspread short put breached: "
                    f"spot={spot:.2f} short_put={short_put:.2f}"
                )
                await self._roll_backspread_short(position, "put")

    async def _roll_backspread_short(
        self, position: Position, side: str
    ) -> None:
        """Buy back short leg and sell new further OTM short."""
        chain = self.dm.option_chain

        short_leg = next(
            (l for l in position.legs
             if l.option_type == side and l.action == "SELL"),
            None
        )
        if short_leg is None:
            return

        buyback_leg = Leg(
            instrument_key=short_leg.instrument_key,
            option_type=side, action="BUY",
            strike=short_leg.strike,
            expiry=short_leg.expiry,
            qty=short_leg.qty
        )
        success, _ = await self._place_single_leg(
            buyback_leg, use_market=True
        )
        if not success:
            logger.warning(
                f"Backspread roll: failed to buy back short "
                f"{side} strike={short_leg.strike}"
            )
            return

        await asyncio.sleep(config.ORDER_BETWEEN_LEGS_DELAY_SEC)

        new_short = self.dm.get_strike_by_delta(
            side, config.BACKSPREAD_SHORT_DELTA
        )
        if new_short is None or new_short not in chain:
            logger.warning(
                f"Backspread roll: new short strike not found for {side}"
            )
            return

        new_short_leg = Leg(
            instrument_key=chain[new_short][side]["instrument_key"],
            option_type=side, action="SELL",
            strike=new_short,
            expiry=short_leg.expiry,
            qty=short_leg.qty,
            delta=chain[new_short][side]["delta"],
            gamma=chain[new_short][side]["gamma"],
            vega=chain[new_short][side]["vega"],
            theta=chain[new_short][side]["theta"]
        )

        success, _ = await self._place_single_leg(
            new_short_leg, use_market=False
        )
        if success:
            position.legs = [
                l for l in position.legs
                if not (l.option_type == side and l.action == "SELL")
            ]
            position.legs.append(new_short_leg)
            logger.info(
                f"Backspread short rolled: "
                f"{short_leg.strike} -> {new_short}"
            )

    async def _close_one_side(
        self,
        position: Position,
        option_type: str,
        exit_reason: str
    ) -> None:
        """Close only one side (call or put) of a multi-leg position."""
        side_legs = [
            l for l in position.legs if l.option_type == option_type
        ]
        for leg in side_legs:
            close_action = "BUY" if leg.action == "SELL" else "SELL"
            close_leg = Leg(
                instrument_key=leg.instrument_key,
                option_type=leg.option_type,
                action=close_action,
                strike=leg.strike,
                expiry=leg.expiry,
                qty=leg.qty
            )
            asyncio.create_task(
                self._place_single_leg(close_leg, use_market=True)
            )
        logger.info(
            f"Closed {option_type} side of {position.trade_id[:8]} "
            f"reason={exit_reason}"
        )

    def _create_position_record(
        self,
        strategy_name: str,
        legs: List[Leg],
        meta: Dict
    ) -> Position:
        """Create a Position dataclass from strategy legs and metadata."""
        trade_id = str(uuid.uuid4())
        IST = pytz.timezone(config.TZ)
        now = datetime.now(IST).isoformat()

        total_credit = sum(
            l.entry_price * l.qty
            for l in legs if l.action == "SELL"
        )
        total_debit = sum(
            l.entry_price * l.qty
            for l in legs if l.action == "BUY"
        )
        net_premium = total_credit - total_debit

        expiry_date = legs[0].expiry if legs else ""
        dte = 0
        if expiry_date:
            try:
                dte = (
                    datetime.strptime(expiry_date, "%Y-%m-%d").date() -
                    date.today()
                ).days
            except ValueError:
                dte = 0

        position = Position(
            trade_id=trade_id,
            strategy_name=strategy_name,
            regime_at_entry=self.re.confirmed_regime,
            entry_timestamp=now,
            entry_spot=self.dm.spot or 0.0,
            entry_vix=self.dm.vix or 0.0,
            legs=legs,
            stop_loss=meta.get("stop_loss", 0.0),
            profit_target=meta.get("profit_target", 0.0),
            exit_dte=meta.get("exit_dte"),
            max_hold_date=meta.get("max_hold_date"),
            composite_at_entry=self.re.raw_composite,
            vol_score=self.re.confirmed_vol,
            edge_score=self.re.confirmed_edge,
            trend_score=self.re.confirmed_trend,
            flow_score=self.re.confirmed_flow,
            days_to_expiry=dte,
            expiry_date=expiry_date,
            total_credit=total_credit,
            total_debit=total_debit,
            net_premium=net_premium,
            max_risk=meta.get("max_risk", 0.0),
            paper_trade=config.PAPER_TRADING_MODE,
            trend_direction=meta.get("trend_direction", 0.0),
            meta=meta
        )
        return position

    def _position_to_dict(self, position: Position) -> Dict:
        """Convert Position to dictionary for SQLite/CSV storage."""
        IST = pytz.timezone(config.TZ)

        holding_days = 0
        if position.exit_timestamp and position.entry_timestamp:
            try:
                entry_dt = datetime.fromisoformat(position.entry_timestamp)
                exit_dt = datetime.fromisoformat(position.exit_timestamp)
                holding_days = (exit_dt - entry_dt).days
            except (ValueError, TypeError):
                holding_days = 0

        slippage_total = sum(l.slippage_pts for l in position.legs)

        return {
            "trade_id":                  position.trade_id,
            "strategy_name":             position.strategy_name,
            "regime_at_entry":           position.regime_at_entry,
            "regime_at_exit":            position.re.confirmed_regime if hasattr(position, 're') else position.regime_at_entry,
            "entry_timestamp":           position.entry_timestamp,
            "exit_timestamp":            position.exit_timestamp,
            "holding_days":              holding_days,
            "entry_spot":                position.entry_spot,
            "exit_spot":                 position.exit_spot,
            "entry_vix":                 position.entry_vix,
            "exit_vix":                  position.exit_vix,
            "legs_summary":              json.dumps([{
                "instrument_key": l.instrument_key,
                "side":           l.action,
                "qty":            l.qty,
                "entry_price":    l.entry_price,
                "exit_price":     l.exit_price,
                "option_type":    l.option_type,
                "strike":         l.strike
            } for l in position.legs]),
            "total_credit_received":     position.total_credit,
            "total_debit_paid":          position.total_debit,
            "net_premium":               position.net_premium,
            "max_risk":                  position.max_risk,
            "realized_pnl":              position.realized_pnl,
            "realized_pnl_percent":      position.realized_pnl_percent,
            "exit_reason":               position.exit_reason,
            "slippage_total_points":     slippage_total,
            "transaction_costs":         self._estimate_costs(position),
            "composite_score_at_entry":  position.composite_at_entry,
            "vol_score":                 position.vol_score,
            "edge_score":                position.edge_score,
            "trend_score":               position.trend_score,
            "flow_score":                position.flow_score,
            "days_to_expiry_at_entry":   position.days_to_expiry,
            "expiry_date":               position.expiry_date,
            "paper_trade":               position.paper_trade,
            "stop_loss":                 position.stop_loss,
            "profit_target":             position.profit_target,
            "exit_dte":                  position.exit_dte,
            "max_hold_date":             position.max_hold_date
        }

    def _estimate_costs(self, position: Position) -> float:
        """Estimate transaction costs (brokerage + STT + exchange + GST)."""
        total_turnover = sum(
            l.entry_price * l.qty * config.LOT_SIZE
            for l in position.legs
        )
        brokerage = min(20.0, total_turnover * 0.0003)
        stt = total_turnover * 0.0005
        exchange_fee = total_turnover * 0.00053
        gst = (brokerage + exchange_fee) * 0.18
        return round(brokerage + stt + exchange_fee + gst, 2)

    def _get_short_strike(
        self, position: Position, option_type: str
    ) -> Optional[float]:
        """Get strike of short leg for given option type."""
        for leg in position.legs:
            if leg.action == "SELL" and leg.option_type == option_type:
                return leg.strike
        return None

    def _get_upper_wing_strike(self, position: Position) -> Optional[float]:
        """Get highest BUY put strike (upper wing of butterfly)."""
        put_strikes = [
            l.strike for l in position.legs
            if l.option_type == "put" and l.action == "BUY"
        ]
        return max(put_strikes) if put_strikes else None

    def _get_lower_wing_strike(self, position: Position) -> Optional[float]:
        """Get lowest BUY put strike (lower wing of butterfly)."""
        put_strikes = [
            l.strike for l in position.legs
            if l.option_type == "put" and l.action == "BUY"
        ]
        return min(put_strikes) if put_strikes else None

    def _has_short_positions(self) -> bool:
        """Check if any open position has short legs."""
        return any(
            leg.action == "SELL"
            for pos in self.open_positions
            for leg in pos.legs
        )

    def _gamma_above_50pct_limit(self) -> bool:
        """Check if portfolio gamma exceeds 50% of regime minimum."""
        regime = self.re.confirmed_regime
        limits = config.GREEKS_LIMITS.get(regime, {})
        gamma_min = limits.get("gamma_min", -99)
        if gamma_min is None:
            return False
        greeks = self._get_portfolio_greeks()
        threshold = gamma_min * 0.50
        return greeks["gamma"] < threshold

    def _get_25d_put_iv(self) -> float:
        """Get implied volatility of 25-delta put."""
        strike = self.dm.get_strike_by_delta("put", 0.25)
        if strike is None:
            return 0.0
        return float(
            self.dm.option_chain.get(strike, {})
            .get("put", {}).get("iv", 0.0)
        )

    def _get_25d_call_iv(self) -> float:
        """Get implied volatility of 25-delta call."""
        strike = self.dm.get_strike_by_delta("call", 0.25)
        if strike is None:
            return 0.0
        return float(
            self.dm.option_chain.get(strike, {})
            .get("call", {}).get("iv", 0.0)
        )

    def _get_otm_bid_ask(self, option_type: str) -> float:
        """Get bid-ask spread for event strangle delta strike."""
        strike = self.dm.get_strike_by_delta(
            option_type, config.EVENT_STRANGLE_DELTA
        )
        if strike is None:
            return 99.0
        opt = (
            self.dm.option_chain.get(strike, {})
            .get(option_type, {})
        )
        return float(opt.get("ask", 99) - opt.get("bid", 0))

    def _get_ema_200(self) -> float:
        """Compute EMA(200) from candle closes."""
        if len(self.dm.candles_15m) < 200:
            return self.dm.spot or 0.0
        closes = [c["close"] for c in self.dm.candles_15m]
        ema = pd.Series(closes).ewm(span=200, adjust=False).mean()
        return float(ema.iloc[-1])

    def _compute_ema_n(self, period: int) -> float:
        """Compute EMA of given period from candle closes."""
        if len(self.dm.candles_15m) < period:
            return self.dm.spot or 0.0
        closes = [c["close"] for c in self.dm.candles_15m]
        ema = pd.Series(closes).ewm(span=period, adjust=False).mean()
        return float(ema.iloc[-1])

    def _get_dte_for_target(
        self, min_dte: int, max_dte: int
    ) -> Optional[int]:
        """Get DTE for target expiry range."""
        expiry = self.dm.get_expiry_by_dte(
            (min_dte + max_dte) // 2,
            tolerance=(max_dte - min_dte) // 2
        )
        if expiry is None:
            return None
        try:
            return (
                datetime.strptime(expiry, "%Y-%m-%d").date() - date.today()
            ).days
        except ValueError:
            return None

    def _save_all_positions_to_sqlite(self) -> None:
        """Persist all open positions to SQLite."""
        for position in self.open_positions:
            self.dm.save_position(self._position_to_dict(position))

    def _log_portfolio_summary(self) -> None:
        """Log formatted portfolio summary."""
        greeks = self._get_portfolio_greeks()
        logger.info(
            f"\n{'=' * 60}\n"
            f"PORTFOLIO SUMMARY\n"
            f"Open Positions : {len(self.open_positions)}\n"
            f"Daily P&L      : ₹{self.daily_pnl:,.2f}\n"
            f"Weekly P&L     : ₹{self.weekly_pnl:,.2f}\n"
            f"Capital        : ₹{self.current_capital:,.2f}\n"
            f"Peak Capital   : ₹{self.peak_capital:,.2f}\n"
            f"Delta          : {greeks['delta']:.3f}\n"
            f"Gamma          : {greeks['gamma']:.5f}\n"
            f"Vega           : ₹{greeks['vega']:,.0f}\n"
            f"Theta          : ₹{greeks['theta']:,.0f}/day\n"
            f"CB L2 Active   : {self.cb_level_2_active}\n"
            f"Kill Switch    : {self.kill_switch_active}\n"
            f"{'=' * 60}"
        )

    def _log_circuit_breaker(
        self, level: int, trigger: str, action: str
    ) -> None:
        """Log circuit breaker event to SQLite."""
        try:
            IST = pytz.timezone(config.TZ)
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO circuit_breaker_log (
                    timestamp, level, trigger,
                    action, daily_pnl, drawdown, regime
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(IST).isoformat(),
                level,
                trigger,
                action,
                self.daily_pnl,
                self.peak_capital - self.current_capital,
                self.re.confirmed_regime
            ))
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"_log_circuit_breaker SQLite error: {e}")

    def _log_order(
        self,
        trade_id: str,
        order_id: str,
        leg: Leg,
        status: str,
        fill_price: float = 0.0
    ) -> None:
        """Log order details to SQLite order_log table."""
        try:
            IST = pytz.timezone(config.TZ)
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO order_log (
                    timestamp, trade_id, order_id,
                    instrument_key, action,
                    option_type, strike, expiry,
                    qty, order_type, price,
                    fill_price, status, slippage,
                    paper_trade
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(IST).isoformat(),
                trade_id,
                order_id,
                leg.instrument_key,
                leg.action,
                leg.option_type,
                leg.strike,
                leg.expiry,
                leg.qty * config.LOT_SIZE,
                "MARKET" if fill_price == 0 else "LIMIT",
                leg.entry_price,
                fill_price,
                status,
                leg.slippage_pts,
                1 if config.PAPER_TRADING_MODE else 0
            ))
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"_log_order SQLite error: {e}")

    def _load_positions_from_sqlite(self) -> None:
        """Restore open positions from SQLite on startup."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM open_positions WHERE status = 'OPEN'"
            )
            rows = cursor.fetchall()
            col_names = [desc[0] for desc in cursor.description]
            conn.close()

            if not rows:
                logger.info("No open positions to restore from SQLite")
                return

            for row in rows:
                row_dict = dict(zip(col_names, row))

                legs_json = row_dict.get("legs_json", "[]")
                try:
                    legs_data = json.loads(legs_json)
                except Exception:
                    legs_data = []

                legs = []
                for l in legs_data:
                    leg = Leg(
                        instrument_key=l.get("instrument_key", ""),
                        option_type=l.get("option_type", "call"),
                        action=l.get("side", "BUY"),
                        strike=float(l.get("strike", 0)),
                        expiry=row_dict.get("expiry_date", ""),
                        qty=int(l.get("qty", 1)),
                        entry_price=float(l.get("entry_price", 0)),
                        exit_price=float(l.get("exit_price", 0))
                    )
                    legs.append(leg)

                position = Position(
                    trade_id=row_dict["trade_id"],
                    strategy_name=row_dict["strategy_name"],
                    regime_at_entry=row_dict.get("regime_at_entry", ""),
                    entry_timestamp=row_dict.get("entry_timestamp", ""),
                    entry_spot=float(row_dict.get("entry_spot", 0)),
                    entry_vix=float(row_dict.get("entry_vix", 0)),
                    legs=legs,
                    stop_loss=float(row_dict.get("stop_loss", 0)),
                    profit_target=float(row_dict.get("profit_target", 0)),
                    exit_dte=row_dict.get("exit_dte"),
                    max_hold_date=row_dict.get("max_hold_date"),
                    composite_at_entry=float(
                        row_dict.get("composite_at_entry", 0)
                    ),
                    vol_score=float(row_dict.get("vol_score", 0)),
                    edge_score=float(row_dict.get("edge_score", 0)),
                    trend_score=float(row_dict.get("trend_score", 0)),
                    flow_score=float(row_dict.get("flow_score", 0)),
                    days_to_expiry=int(row_dict.get("days_to_expiry", 0)),
                    expiry_date=row_dict.get("expiry_date", ""),
                    total_credit=float(row_dict.get("total_credit", 0)),
                    total_debit=float(row_dict.get("total_debit", 0)),
                    net_premium=float(row_dict.get("net_premium", 0)),
                    max_risk=float(row_dict.get("max_risk", 0)),
                    paper_trade=bool(row_dict.get("paper_trade", 1)),
                    status="OPEN"
                )
                self.open_positions.append(position)
                logger.info(
                    f"Restored position: {position.strategy_name} "
                    f"id={position.trade_id[:8]}"
                )

            logger.info(
                f"Restored {len(rows)} open positions from SQLite"
            )

        except sqlite3.OperationalError:
            logger.info("No state.db found — fresh start")
        except Exception as e:
            logger.warning(f"_load_positions_from_sqlite error: {e}")

    async def _reconcile_with_broker(self) -> None:
        """Reconcile local positions with broker positions on startup."""
        if config.PAPER_TRADING_MODE:
            logger.info("Paper mode: skipping broker reconciliation")
            return

        try:
            broker_positions = await self.dm._api_get(
                config.EP_POSITIONS, {}
            )

            if not broker_positions:
                logger.info("No broker positions found")
                return

            broker_map: Dict[str, int] = {}
            pos_list = (
                broker_positions
                if isinstance(broker_positions, list)
                else broker_positions.get("data", [])
            )
            for pos in pos_list:
                key = pos.get("instrument_token", "")
                qty = int(pos.get("quantity", 0))
                if key and qty != 0:
                    broker_map[key] = qty

            for position in self.open_positions:
                for leg in position.legs:
                    broker_qty = broker_map.get(leg.instrument_key, 0)
                    local_qty = leg.qty * config.LOT_SIZE

                    if broker_qty == 0 and local_qty != 0:
                        logger.warning(
                            f"Position mismatch: local has {local_qty} units "
                            f"but broker has 0 for {leg.instrument_key} — "
                            f"marking leg as closed externally"
                        )
                        leg.qty = 0
                        leg.fill_status = "CLOSED_EXTERNALLY"

                    elif broker_qty != 0 and abs(broker_qty) != local_qty:
                        logger.warning(
                            f"Qty mismatch: local={local_qty} "
                            f"broker={abs(broker_qty)} for "
                            f"{leg.instrument_key} — broker wins"
                        )
                        leg.qty = abs(broker_qty) // config.LOT_SIZE

            local_keys = {
                leg.instrument_key
                for pos in self.open_positions
                for leg in pos.legs
            }
            for key, qty in broker_map.items():
                if key not in local_keys and qty != 0:
                    logger.warning(
                        f"Unknown broker position: {key} qty={qty} — "
                        f"manual review required"
                    )

            logger.info("Broker reconciliation complete")

        except Exception as e:
            logger.error(f"Broker reconciliation error: {e}")

    def reset_daily_state(self) -> None:
        """Reset daily P&L and circuit breaker state at start of new day."""
        self.daily_pnl = 0.0
        self.daily_trading_halted = False
        self.cb_level_2_active = False
        self.cb_level_1_count = 0
        logger.info("Daily state reset complete")

    def reset_weekly_state(self) -> None:
        """Reset weekly P&L and circuit breaker state at start of new week."""
        self.weekly_pnl = 0.0
        self.cb_level_3_active = False
        logger.info("Weekly state reset complete")
