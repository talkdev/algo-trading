# main.py
# NIFTY Intraday Options Engine v3.0
# Main orchestration loop: startup, intraday cycle, EOD tasks, shutdown.
# Regime-based architecture — clean separation of concerns.

from __future__ import annotations

import json
import signal
import time as time_module
import traceback
from datetime import datetime, date, time as dtime, timedelta
from pathlib import Path

from core import (
    Config, Database, RateLimiter, UpstoxClient,
    ExpiryCalendar, now_ist, today_ist,
    print_section, print_kv_table,
    load_config, setup_logging,
)
from data_engine import MarketDataEngine
from regime_engine import RegimeEngine, merge_regime_into_signals
from calibration_engine import CalibrationEngine
from strategy_engine import StrategyEngine
from execution_engine import ExecutionEngine


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class MainEngine:
    """
    Main orchestration engine for NIFTY intraday options trading.

    Architecture:
    ┌─────────────────────────────────────────────────────────┐
    │  MainEngine                                             │
    │  ├── MarketDataEngine   (spot, VIX, chain, signals)     │
    │  ├── CalibrationEngine  (self-calibrating thresholds)   │
    │  ├── RegimeEngine       (4-dimensional regime class.)   │
    │  ├── StrategyEngine     (strategy selection + params)   │
    │  └── ExecutionEngine    (order execution + monitoring)  │
    └─────────────────────────────────────────────────────────┘

    Main loop (every regime_calc_interval_sec = 45s):
    1. Reset if new day
    2. Run market data cycle (fetch spot, VIX, chain, compute signals)
    3. Run regime engine (classify vol/price/positioning, compute size)
    4. Merge regime into signals
    5. Monitor open positions (7-priority exit system)
    6. Perform hard exit sweep at 15:00
    7. Check daily loss halt
    8. If entry possible: run strategy engine → execute entry
    9. Update cycle log with P&L
    10. Print cycle summary

    Separate timers:
    - Spot bar collection: every spot_bar_interval_sec (60s)
    - Calibration: every calibration_interval_sec (3600s) + at startup + EOD
    """

    def __init__(self):
        # ── Load configuration ────────────────────────────────────────────
        self.config = load_config()

        # ── Initialise database ───────────────────────────────────────────
        self.db = Database(self.config.db_path)

        # ── Initialise logging ────────────────────────────────────────────
        import logging
        log_level = getattr(logging, self.config.log_level.upper(), logging.INFO)
        self.logger = setup_logging(self.db, self.config.log_dir, level=log_level)

        # ── Initialise API client ─────────────────────────────────────────
        self.rate_limiter = RateLimiter(self.config.rate_limits)
        self.client = UpstoxClient(
            self.config, self.rate_limiter, self.db, self.logger
        )

        # ── Initialise sub-engines ────────────────────────────────────────
        self.market_engine = MarketDataEngine(
            self.config, self.db, self.client, self.rate_limiter, self.logger
        )
        self.cal_engine = CalibrationEngine(
            self.db, self.config, self.logger
        )
        self.regime_engine = RegimeEngine(
            self.config, self.db, self.market_engine, self.logger
        )
        self.strategy_engine = StrategyEngine(
            self.config, self.db, self.market_engine, self.cal_engine, self.logger
        )
        self.execution_engine = ExecutionEngine(
            self.config, self.db, self.market_engine, self.cal_engine,
            self.client, self.logger
        )

        # ── Loop state ────────────────────────────────────────────────────
        self.loop_count              = 0
        self.running                 = True
        self._last_cycle_time        = 0.0
        self._last_spot_time         = 0.0
        self._last_calibration_time  = 0.0
        self._last_status_print_time = 0.0
        self._eod_done               = False

        # ── Signal handlers ───────────────────────────────────────────────
        signal.signal(signal.SIGINT,  self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    # ─────────────────────────────────────────────────────────────────────
    # SIGNAL HANDLING
    # ─────────────────────────────────────────────────────────────────────

    def _handle_signal(self, signum, frame) -> None:
        """Handle OS signals (Ctrl+C, SIGTERM) for graceful shutdown."""
        self.logger.info(f"Received signal {signum} — initiating graceful shutdown.")
        self.running = False
        raise KeyboardInterrupt()

    # ─────────────────────────────────────────────────────────────────────
    # STARTUP
    # ─────────────────────────────────────────────────────────────────────

    def _print_startup_banner(self) -> None:
        """Print startup configuration banner."""
        print_section("NIFTY INTRADAY OPTIONS ALGO TRADING ENGINE v3.0", char="#")
        print_kv_table({
            "Mode":                  "PAPER TRADE" if self.config.paper_trade_mode
                                     else "*** LIVE TRADING ***",
            "Starting Capital":      f"Rs{self.config.starting_capital:,.0f}",
            "Max Daily Loss":        f"{self.config.max_daily_loss_pct*100:.1f}%",
            "Max Risk Per Trade":    f"{self.config.max_risk_per_trade_pct*100:.3f}%",
            "Trading Window":        (
                f"{self.config.trading_window_start} - "
                f"{self.config.trading_window_last_entry} "
                f"(hard exit {self.config.hard_exit_time})"
            ),
            "Tuesday Window":        (
                f"10:30 - {self.config.tuesday_last_entry} "
                f"(hard exit {self.config.tuesday_hard_exit})"
            ),
            "Lot Size":              self.config.lot_size,
            "Strike Step":           self.config.nifty_strike_step,
            "ADX Trend Threshold":   self.config.adx_trend_threshold,
            "ADX Strong Threshold":  self.config.adx_strong_threshold,
            "VRP Sell Threshold":    f"{self.config.vrp_sell_threshold_default:.2f}pp (default)",
            "ABORT VIX Spike":       f"{self.config.abort_vix_spike_pct:.0f}% from prev close",
            "ABORT VIX Absolute":    f"{self.config.abort_vix_absolute:.0f}",
            "Day Move Block":        f"{self.config.day_move_used_block_pct:.0f}%",
            "Delta Close Threshold": self.config.delta_close_threshold,
            "Spot Proximity":        f"{self.config.spot_proximity_pts}pts",
            "Price Stop Mult":       f"{self.config.price_stop_straddle_mult:.2f}× straddle",
            "Profit Lock DTE0":      f"{self.config.profit_lock_pct_dte0*100:.0f}%",
            "Profit Lock DTE1+":     f"{self.config.profit_lock_pct_dte1plus*100:.0f}%",
            "Cheap Buyback":         f"≤{self.config.cheap_buyback_pts}pts after "
                                     f"{self.config.cheap_buyback_after_time}",
            "Phantom Tracking":      self.config.phantom_trade_tracking,
            "Calibration Min Days":  self.config.min_trading_days_for_calibration,
            "DB Path":               str(self.config.db_path),
            "Log Dir":               str(self.config.log_dir),
        }, title="STARTUP CONFIGURATION")

        if not self.config.paper_trade_mode:
            print("\n  " + "!" * 70)
            print("  !!! WARNING: LIVE TRADING MODE — REAL ORDERS WILL BE PLACED !!!")
            print("  " + "!" * 70 + "\n")
        print()

    def _verify_lot_size(self) -> None:
        """Log lot size verification reminder."""
        self.logger.info(
            f"Lot size configured as {self.config.lot_size} units/lot. "
            f"MANUALLY VERIFY against current NSE NIFTY contract spec "
            f"and broker instrument master before live trading. "
            f"As of 2026, NIFTY lot size = 75 units (verify current spec)."
        )

    def _validate_session_state_integrity(self) -> None:
        today_str = today_ist().isoformat()
        state = self.market_engine.state
        actual_stops = self.db.query(
            "SELECT COUNT(*) as cnt FROM trade_exits "
            "WHERE trade_id IN ("
            "SELECT position_id FROM positions WHERE trading_date=?"
            ") AND exit_reason='CLOSE_STOP'",
            (today_str,),
        )
        real_stops = actual_stops[0]["cnt"] if actual_stops else 0
        stored_stops = int(state.get("consecutive_stops", 0) or 0)
        if stored_stops != real_stops:
            self.logger.warning(
                f"Session state integrity: consecutive_stops={stored_stops} "
                f"but actual stop exits today={real_stops}. Correcting."
            )
            state["consecutive_stops"] = real_stops
        actual_pnl_row = self.db.query_one(
            "SELECT COALESCE(SUM(net_pnl_rupees),0) as total "
            "FROM trade_exits WHERE trade_id IN ("
            "SELECT position_id FROM positions WHERE trading_date=?)",
            (today_str,),
        )
        actual_pnl = float(actual_pnl_row["total"] if actual_pnl_row else 0)
        if actual_pnl > -self.config.starting_capital * self.config.max_daily_loss_pct:
            if state.get("daily_halted") and real_stops < 2:
                self.logger.warning(
                    "Session state integrity: daily_halted=True but losses within limit. "
                    "Clearing halt flag."
                )
                state["daily_halted"] = False
        state["daily_pnl"] = actual_pnl
        self.market_engine._save_session_state()
        self.logger.info(
            f"Session state validated: stops={real_stops} "
            f"halted={state.get('daily_halted')} "
            f"pnl=Rs{actual_pnl:.0f}"
        )

    def _carry_forward_capital(self) -> None:
        """
        Carry forward capital from the previous trading session.
        Called at startup to ensure capital is correctly initialised.
        """
        state     = self.market_engine.state
        today_str = today_ist().isoformat()

        # Only carry forward if no trades have been taken today yet
        if state.get("entry_count", 0) != 0:
            return

        last_summary = self.db.query_one(
            "SELECT capital_end, net_pnl_rupees, trading_date "
            "FROM daily_summary "
            "WHERE trading_date < ? AND capital_end IS NOT NULL "
            "ORDER BY trading_date DESC LIMIT 1",
            (today_str,),
        )

        if last_summary and last_summary.get("capital_end") is not None:
            prior_capital = float(last_summary["capital_end"])
            prior_pnl     = float(last_summary.get("net_pnl_rupees", 0) or 0)
            prior_date    = last_summary.get("trading_date", "unknown")
            current       = float(state.get("current_capital", 0) or 0)

            if abs(prior_capital - current) > 0.01:
                state["current_capital"] = prior_capital
                self.market_engine._save_session_state()
                self.logger.info(
                    f"Capital carried forward from {prior_date}: "
                    f"Rs{prior_capital:,.2f} "
                    f"(session had Rs{current:,.2f}). "
                    f"Prior session P&L: Rs{prior_pnl:,.2f}"
                )
            else:
                self.logger.info(
                    f"Capital reconciled: Rs{prior_capital:,.2f} from {prior_date}. "
                    f"Prior session P&L: Rs{prior_pnl:,.2f}"
                )

    def _reconcile_open_positions_on_startup(self) -> None:
        """
        Reconcile open positions on startup.
        - Close stale prior-day positions
        - Resume monitoring of today's open positions
        """
        today_str      = today_ist().isoformat()
        open_positions = self.execution_engine._get_open_positions()

        if not open_positions:
            self.logger.info("Startup reconciliation: no open positions found.")
            return

        self.logger.info(
            f"Startup reconciliation: {len(open_positions)} open position(s) found."
        )

        for pos in open_positions:
            if pos["trading_date"] != today_str:
                self.logger.warning(
                    f"Closing stale prior-day position: "
                    f"{pos['strategy_name']} from {pos['trading_date']}"
                )
                self.execution_engine.execute_close(
                    pos, "STALE_PRIOR_DAY_CLOSE", 0, {}
                )
            else:
                self.logger.info(
                    f"Resuming today's open position: "
                    f"{pos['strategy_name']} "
                    f"{pos['position_id'][:16]}..."
                )

    # ─────────────────────────────────────────────────────────────────────
    # INTRADAY HELPERS
    # ─────────────────────────────────────────────────────────────────────

    def _reset_daily_state_if_new_day(self) -> None:
        today_str = today_ist().isoformat()
        state = self.market_engine.state
        if state.get("trading_date") == today_str:
            return
        state["consecutive_stops"] = 0
        state["daily_halted"] = False
        state["daily_pnl"] = 0.0
        state["entry_count"] = 0
        state["last_stop_time"] = None
        state["last_stop_reason"] = ""
        state["last_stop_signal_combo"] = ""
        self.market_engine._save_session_state()
        self.logger.info(f"Daily state reset for new day: {today_str}")

    def _market_open(self) -> bool:
        if ExpiryCalendar.is_holiday(today_ist()):
            return False
        now = now_ist().time()
        return dtime(9, 15) <= now <= dtime(15, 30)

    def compute_unrealized_pnl(self) -> float:
        """
        Total unrealised P&L across all open positions.

        v3.1: this used the MID mark and ignored transaction costs. For a
        short-premium book the mid is always the flattering side (shorts are
        bought back at the ask), and the entry charges have already left the
        account. The result was an unrealised number that was systematically
        too good, feeding the daily-loss halt — the engine's last line of
        defence — so the halt fired late, and only once the real drawdown was
        already larger than the configured limit.

        Now marked at liquidation value where available, net of the entry
        costs already paid and an estimate of the cost still to be paid to
        close the position.
        """
        C02        = self.config.lot_size
        unrealized = 0.0

        for pos in self.execution_engine._get_open_positions():
            current_prem = pos.get("last_liquidation_premium")
            if current_prem is None:
                current_prem = pos.get("last_known_premium")
            if current_prem is None:
                continue
            entry_credit = float(pos.get("entry_credit") or 0)
            lots         = int(pos.get("final_lots", 1) or 1)
            gross = (entry_credit - float(current_prem)) * C02 * lots

            entry_costs = float(pos.get("entry_costs_rupees") or 0.0)
            exit_costs  = entry_costs * 0.95
            unrealized += gross - entry_costs - exit_costs

        return unrealized

    def compute_total_daily_pnl(self) -> float:
        """Return realized + unrealized P&L for today."""
        realized = float(self.market_engine.state.get("daily_pnl", 0.0) or 0.0)
        return realized + self.compute_unrealized_pnl()

    def check_daily_loss_halt(self) -> None:
        """
        Check if total daily P&L (realized + unrealized) exceeds daily loss limit.
        Halts trading if limit is exceeded.
        """
        state       = self.market_engine.state
        current_cap = float(state.get("current_capital", self.config.starting_capital) or 0)

        if not current_cap or current_cap <= 0:
            return

        realized_pnl = float(state.get("daily_pnl", 0.0) or 0.0)
        day_start_cap = current_cap - realized_pnl
        if day_start_cap <= 0:
            day_start_cap = current_cap

        total_pnl = self.compute_total_daily_pnl()
        loss_pct  = max(0.0, -total_pnl) / day_start_cap

        if loss_pct >= self.config.max_daily_loss_pct and not state.get("daily_halted"):
            state["daily_halted"] = True
            self.logger.warning(
                f"DAILY LOSS LIMIT (incl. unrealized): {loss_pct*100:.2f}% "
                f">= {self.config.max_daily_loss_pct*100:.1f}% — halting trading"
            )
            self.market_engine._save_session_state()

    def _get_capital_at_day_start(self) -> float:
        """Return capital at the start of today's session."""
        today_str = today_ist().isoformat()
        earliest  = self.db.query_one(
            "SELECT capital_at_entry FROM trade_entries "
            "WHERE trading_date=? ORDER BY entry_time ASC LIMIT 1",
            (today_str,),
        )
        if earliest and earliest.get("capital_at_entry") is not None:
            return float(earliest["capital_at_entry"])
        return float(self.market_engine.state.get(
            "current_capital", self.config.starting_capital
        ) or self.config.starting_capital)

    # ─────────────────────────────────────────────────────────────────────
    # SPOT BAR COLLECTION
    # ─────────────────────────────────────────────────────────────────────

    def _run_spot_cycle(self) -> None:
        """
        Fetch and store today's 1-minute NIFTY candles.
        Called every spot_bar_interval_sec (60s) independently of the main cycle.
        """
        if not self._market_open():
            return
        try:
            candles = self.client.get_intraday_candles(
                "NSE_INDEX|Nifty 50", "1minute"
            )
            if not candles:
                return

            trading_date = today_ist().isoformat()
            rows = []

            for c in candles:
                if len(c) < 5:
                    continue
                try:
                    from core import parse_ist_timestamp
                    ts_raw = (
                        str(c[0])
                        .replace("Z", "")
                        .replace("+05:30", "")
                        .replace("+0530", "")
                    )
                    ts = datetime.fromisoformat(ts_raw)
                    if not (dtime(9, 15) <= ts.time() <= dtime(15, 29)):
                        continue
                    ts_clean = ts.strftime("%H:%M:%S")
                    rows.append((
                        trading_date, ts_clean, 1,
                        float(c[1]), float(c[2]),
                        float(c[3]), float(c[4]),
                        int(c[5]) if len(c) > 5 else 0,
                        "upstox_intraday",
                    ))
                except Exception as _te:
                    self.logger.debug(f"Spot bar parse error: {_te} raw={c[0]}")

            if rows:
                try:
                    self.db.executemany(
                        "INSERT OR REPLACE INTO intraday_candles "
                        "(trading_date, candle_time, interval_min, "
                        "open, high, low, close, volume, source) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        rows,
                    )
                    self.logger.debug(
                        f"Spot cycle stored {len(rows)} bars for {trading_date}"
                    )
                except Exception as e:
                    self.logger.warning(f"Spot bar insert error: {e}")

        except Exception as e:
            self.logger.warning(f"Spot cycle error: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # CALIBRATION
    # ─────────────────────────────────────────────────────────────────────

    def _run_calibration_cycle(
        self, force: bool = False, schedule: str = "daily"
    ) -> None:
        """
        Run calibration cycle.
        force=True: run regardless of market hours (startup, EOD).
        """
        if not force and not self._market_open():
            return
        try:
            cal = self.cal_engine.run(schedule=schedule)
            if cal:
                # Update regime engine with new calibration
                self.regime_engine.calibrator._state = cal
                self.regime_engine.classifier.cal    = cal
                self.logger.info(
                    f"Calibration updated: tier={cal.calibration_tier} "
                    f"vrp_sell={cal.vrp_sell_threshold:.2f}pp"
                )
        except Exception as e:
            self.logger.error(f"Calibration cycle error: {e}", exc_info=True)

    # ─────────────────────────────────────────────────────────────────────
    # MAIN CYCLE
    # ─────────────────────────────────────────────────────────────────────

    def run_one_cycle(self) -> None:
        """
        Execute one complete trading cycle.
        Called every regime_calc_interval_sec (default 45s).
        """
        current_time = now_ist().time()

        # ── Step 1: Reset if new day ──────────────────────────────────────
        self._reset_daily_state_if_new_day()
        self.market_engine.reset_if_new_day()

        # ── Step 2: Market data cycle ─────────────────────────────────────
        signals = self.market_engine.run_cycle()

        # ── Step 3: Regime classification ────────────────────────────────
        try:
            regime_snapshot = self.regime_engine.process_signals(signals)
            signals = merge_regime_into_signals(signals, regime_snapshot)
        except Exception as e:
            self.logger.error(f"Regime engine error: {e}", exc_info=True)
            # Continue without regime — signals will have None for regime fields

        # ── Step 4: Update cycle log with regime outputs ──────────────────
        try:
            latest_cycle = self.db.query_one(
                "SELECT cycle_id FROM cycle_log "
                "WHERE trading_date=? ORDER BY cycle_id DESC LIMIT 1",
                (today_ist().isoformat(),),
            )
            if latest_cycle and signals.get("final_regime"):
                self.db.update(
                    "cycle_log",
                    {
                        "vol_regime":        signals.get("vol_regime"),
                        "price_regime":      signals.get("price_regime"),
                        "positioning_regime":signals.get("positioning_regime"),
                        "confidence_level":  signals.get("confidence_level"),
                        "confidence_score":  signals.get("confidence_score"),
                        "final_regime":      signals.get("final_regime"),
                        "final_regime_notes":signals.get("final_regime_notes"),
                        "size_multiplier":   signals.get("size_multiplier"),
                        "block_new_entries": int(bool(signals.get("block_new_entries", False))),
                        "no_trade_reason":   (
                            signals.get("final_regime_notes")
                            if signals.get("final_regime") in ("NO_TRADE", "ABORT")
                            else None
                        ),
                    },
                    {"cycle_id": latest_cycle["cycle_id"]},
                )
        except Exception as _cle:
            self.logger.debug(f"Cycle log regime update error: {_cle}")

        # ── Step 5: Monitor open positions ────────────────────────────────
        # IMPORTANT: positions are ALWAYS monitored regardless of regime
        # ABORT only blocks new entries — never closes existing positions
        self.execution_engine.monitor_all_positions(signals)

        # ── Step 6: Hard exit sweep ───────────────────────────────────────
        self.execution_engine.perform_hard_exit_sweep()

        # ── Step 7: Daily loss halt check ─────────────────────────────────
        self.check_daily_loss_halt()

        # ── Step 8: Strategy decision and entry ───────────────────────────
        entry_possible = (
            current_time >= dtime(9, 30) and
            current_time <= dtime(14, 30) and
            not self.market_engine.state.get("daily_halted") and
            not signals.get("block_new_entries") and
            bool(signals.get("or_computed", False)) and
            signals.get("final_regime") not in ("NO_TRADE", "ABORT", None)
        )

        if entry_possible:
            try:
                decision = self.strategy_engine.decide(signals)
                if decision.get("action") == "ENTER":
                    self.execution_engine.process_entry_decision(decision, signals)
            except Exception as e:
                self.logger.error(f"Strategy/entry error: {e}", exc_info=True)

        # ── Step 9: Update cycle log with P&L ────────────────────────────
        total_pnl = self.compute_total_daily_pnl()
        try:
            latest_cycle = self.db.query_one(
                "SELECT cycle_id FROM cycle_log "
                "WHERE trading_date=? ORDER BY cycle_id DESC LIMIT 1",
                (today_ist().isoformat(),),
            )
            if latest_cycle:
                self.db.update(
                    "cycle_log",
                    {"daily_pnl_net": total_pnl},
                    {"cycle_id": latest_cycle["cycle_id"]},
                )
        except Exception as _ple:
            self.logger.debug(f"Cycle log P&L update error: {_ple}")

        # ── Step 10: Print cycle footer ───────────────────────────────────
        _atm_iv_none = self.market_engine.state.get("_atm_iv_none_cycles", 0)
        _vrp_none = self.market_engine.state.get("_vrp_none_cycles", 0)
        if _atm_iv_none >= 3:
            self.logger.critical(
                f"SIGNAL HEALTH: ATM IV None for {_atm_iv_none} cycles — "
                f"VRP computation degraded"
            )
        if _vrp_none >= 3:
            self.logger.critical(
                f"SIGNAL HEALTH: VRP None for {_vrp_none} cycles — "
                f"vol regime defaulting to NEUTRAL"
            )
        self._print_cycle_footer(signals, total_pnl)
        self.loop_count += 1

    def _print_cycle_footer(self, signals: dict, total_pnl: float) -> None:
        """Print concise cycle summary to console."""
        open_positions = self.execution_engine._get_open_positions()
        print_section("CYCLE SUMMARY")
        print_kv_table({
            "Cycle #":            self.loop_count,
            "Time":               now_ist().strftime("%H:%M:%S"),
            "Open Positions":     len(open_positions),
            "Realized P&L (Rs)":  f"{self.market_engine.state.get('daily_pnl', 0):,.0f}",
            "Unrealized P&L (Rs)":f"{self.compute_unrealized_pnl():,.0f}",
            "Total P&L (Rs)":     f"{total_pnl:,.0f}",
            "Capital (Rs)":       f"{self.market_engine.state.get('current_capital', 0):,.0f}",
            "Daily Halted":       bool(self.market_engine.state.get("daily_halted")),
            "Block New Entries":  bool(signals.get("block_new_entries")),
            "Entries Today":      self.market_engine.state.get("entry_count", 0),
            "Consec. Stops":      self.market_engine.state.get("consecutive_stops", 0),
            "Vol Regime":         signals.get("vol_regime", "N/A"),
            "Price Regime":       signals.get("price_regime", "N/A"),
            "Final Regime":       signals.get("final_regime", "N/A"),
            "Confidence":         signals.get("confidence_level", "N/A"),
            "Size Multiplier":    signals.get("size_multiplier", 0),
            "VIX":                signals.get("vix", "N/A"),
            "VRP Smoothed":       f"{signals.get('vrp_smoothed', 0):.2f}pp"
                                  if signals.get("vrp_smoothed") else "N/A",
            "IV Behavior":        signals.get("iv_behavior", "N/A"),
            "Day Move Used":      f"{signals.get('day_move_used_pct', 0):.1f}%",
            "Cal Tier":           signals.get("calibration_tier", 0),
        })
        print()

    # ─────────────────────────────────────────────────────────────────────
    # DAILY SUMMARY
    # ─────────────────────────────────────────────────────────────────────

    def generate_daily_summary(self) -> dict:
        """
        Generate and persist the end-of-day summary.
        Called after market close.
        """
        trading_date = today_ist().isoformat()
        state        = self.market_engine.state

        trades = self.db.query(
            "SELECT * FROM trade_entries WHERE trading_date=?",
            (trading_date,),
        )
        exits = self.db.query(
            "SELECT te.* FROM trade_exits te "
            "JOIN positions p ON te.position_id=p.position_id "
            "WHERE p.trading_date=?",
            (trading_date,),
        )
        cycle_rows = self.db.query(
            "SELECT * FROM cycle_log WHERE trading_date=? ORDER BY cycle_id",
            (trading_date,),
        )
        decisions = self.db.query(
            "SELECT * FROM strategy_decisions WHERE trading_date=?",
            (trading_date,),
        )

        # Performance metrics
        trades_attempted = len(decisions)
        trades_executed  = len(trades)
        trades_won       = sum(1 for e in exits if e.get("result") == "WIN")
        trades_lost      = sum(1 for e in exits if e.get("result") == "LOSS")
        win_rate_pct     = (trades_won / len(exits) * 100.0) if exits else 0.0

        gross_pnl_rs  = sum(float(e.get("gross_pnl_rupees") or 0) for e in exits)
        total_costs_rs = sum(float(e.get("total_costs_rupees") or 0) for e in exits)
        net_pnl_rs    = sum(float(e.get("net_pnl_rupees") or 0) for e in exits)

        capital_start = self._get_capital_at_day_start()
        capital_end   = float(state.get("current_capital", self.config.starting_capital) or 0)
        net_pnl_pct   = (net_pnl_rs / capital_start * 100.0) if capital_start else 0.0

        # Market data from cycle log
        spots = [c["spot"] for c in cycle_rows if c.get("spot") is not None]
        vixs  = [c["vix"]  for c in cycle_rows if c.get("vix")  is not None]
        vrps  = [c.get("vrp_smoothed") or c.get("vrp_raw")
                 for c in cycle_rows
                 if (c.get("vrp_smoothed") or c.get("vrp_raw")) is not None]
        pnl_series = [c["daily_pnl_net"] for c in cycle_rows
                      if c.get("daily_pnl_net") is not None]

        # Strategies and no-trade reasons
        strategies_used: dict = {}
        for t in trades:
            strategies_used[t["strategy_name"]] = \
                strategies_used.get(t["strategy_name"], 0) + 1

        no_trade_reasons: dict = {}
        for d in decisions:
            if d.get("action") == "NO_TRADE":
                r = d.get("reason") or "unknown"
                no_trade_reasons[r] = no_trade_reasons.get(r, 0) + 1

        # Performance metrics
        avg_hold = (
            sum(float(e.get("hold_minutes") or 0) for e in exits) / len(exits)
        ) if exits else 0.0
        avg_credit = (
            sum(float(t.get("entry_credit") or 0) for t in trades) / len(trades)
        ) if trades else 0.0
        avg_vrp_entry = (
            sum(float(t.get("entry_vrp_smoothed") or t.get("entry_vrp") or 0)
                for t in trades) / len(trades)
        ) if trades else 0.0

        gross_wins   = sum(float(e.get("net_pnl_rupees") or 0)
                           for e in exits if (e.get("net_pnl_rupees") or 0) > 0)
        gross_losses = abs(sum(float(e.get("net_pnl_rupees") or 0)
                               for e in exits if (e.get("net_pnl_rupees") or 0) < 0))
        profit_factor = (
            round(gross_wins / gross_losses, 3) if gross_losses > 0
            else (None if gross_wins > 0 else 0.0)
        )

        max_concurrent = max(
            (int(c.get("open_positions") or 0) for c in cycle_rows), default=0
        )
        stops_fired = sum(
            1 for e in exits if e.get("exit_reason") == "CLOSE_STOP"
        )

        # Max drawdown from P&L series
        max_drawdown = 0.0
        if len(pnl_series) >= 2:
            peak = pnl_series[0]
            for val in pnl_series:
                if val > peak:
                    peak = val
                dd = peak - val
                if dd > max_drawdown:
                    max_drawdown = dd

        # Dominant regime
        dominant_regime = self._get_dominant_regime(cycle_rows)

        # Dominant vol/price regimes
        vol_counts: dict   = {}
        price_counts: dict = {}
        for c in cycle_rows:
            vr = c.get("vol_regime")
            pr = c.get("price_regime")
            if vr:
                vol_counts[vr]   = vol_counts.get(vr, 0) + 1
            if pr:
                price_counts[pr] = price_counts.get(pr, 0) + 1

        dominant_vol_regime   = max(vol_counts,   key=lambda k: vol_counts[k])   if vol_counts   else None
        dominant_price_regime = max(price_counts, key=lambda k: price_counts[k]) if price_counts else None

        # Phantom trade stats
        phantom_count_row = self.db.query_one(
            "SELECT COUNT(*) as total, "
            "SUM(would_have_been_profitable) as would_have_won "
            "FROM phantom_trades WHERE trading_date=?",
            (trading_date,),
        )
        phantom_blocked   = int(phantom_count_row["total"] or 0) if phantom_count_row else 0
        phantom_would_win = int(phantom_count_row["would_have_won"] or 0) if phantom_count_row else 0

        # Regime accuracy
        acc_row = self.db.query_one(
            "SELECT AVG(score_value) as avg_score "
            "FROM regime_accuracy_scores WHERE trading_date=?",
            (trading_date,),
        )
        regime_accuracy = float(acc_row["avg_score"] or 0) if acc_row else None

        # Straddle ratio
        opening_straddle = float(state.get("_straddle_open_for_summary") or 0)
        realized_move    = 0.0
        straddle_ratio   = 0.0
        if spots and len(spots) >= 2:
            realized_move = abs(spots[-1] - spots[0])
            if opening_straddle > 0:
                straddle_ratio = round(opening_straddle / realized_move, 3) \
                    if realized_move > 0 else 0.0

        summary = {
            "trading_date":           trading_date,
            "day_label":              state.get("day_label"),
            "trades_attempted":       trades_attempted,
            "trades_executed":        trades_executed,
            "trades_won":             trades_won,
            "trades_lost":            trades_lost,
            "win_rate_pct":           round(win_rate_pct, 1),
            "gross_pnl_rupees":       round(gross_pnl_rs, 2),
            "total_costs_rupees":     round(total_costs_rs, 2),
            "net_pnl_rupees":         round(net_pnl_rs, 2),
            "net_pnl_pct_capital":    round(net_pnl_pct, 3),
            "max_intraday_drawdown":  round(max_drawdown, 2),
            "max_concurrent_positions": max_concurrent,
            "stops_fired":            stops_fired,
            "daily_halt_triggered":   1 if state.get("daily_halted") else 0,
            "vix_open":               vixs[0]  if vixs  else None,
            "vix_close":              vixs[-1] if vixs  else None,
            "vix_low":                min(vixs) if vixs else None,
            "vix_high":               max(vixs) if vixs else None,
            "nifty_open":             spots[0]  if spots else None,
            "nifty_close":            spots[-1] if spots else None,
            "nifty_low":              min(spots) if spots else None,
            "nifty_high":             max(spots) if spots else None,
            "or_width":               state.get("or_width"),
            "or_condition":           state.get("or_condition"),
            "vrp_mean":               round(sum(vrps) / len(vrps), 3) if vrps else None,
            "vrp_smoothed_mean":      round(sum(vrps) / len(vrps), 3) if vrps else None,
            "dominant_vol_regime":    dominant_vol_regime,
            "dominant_price_regime":  dominant_price_regime,
            "dominant_final_regime":  dominant_regime,
            "strategies_used_json":   json.dumps(strategies_used),
            "no_trade_reasons_json":  json.dumps(no_trade_reasons),
            "avg_hold_minutes":       round(avg_hold, 1),
            "avg_credit_pts":         round(avg_credit, 3),
            "avg_vrp_at_entry":       round(avg_vrp_entry, 3),
            "profit_factor":          profit_factor,
            "capital_start":          round(capital_start, 2),
            "capital_end":            round(capital_end, 2),
            "capital_change_pct":     round(
                (capital_end - capital_start) / capital_start * 100.0, 3
            ) if capital_start else 0.0,
            "event_day":              int(bool(ExpiryCalendar.is_event_day(today_ist()))),
            "event_name":             ExpiryCalendar.is_event_day(today_ist()),
            "opening_spot":           spots[0]  if spots else None,
            "closing_spot":           spots[-1] if spots else None,
            "day_range_points":       round(max(spots) - min(spots), 2) if spots else 0,
            "day_range_pct":          round(
                (max(spots) - min(spots)) / spots[0] * 100, 3
            ) if spots and spots[0] else 0,
            "opening_straddle":       opening_straddle,
            "realized_move":          round(realized_move, 2),
            "straddle_ratio":         straddle_ratio,
            "phantom_trades_blocked": phantom_blocked,
            "phantom_would_have_won": phantom_would_win,
            "regime_accuracy_score":  regime_accuracy,
            "created_at":             now_ist().isoformat(),
        }

        self.db.upsert(
            "daily_summary",
            {"trading_date": trading_date},
            {k: v for k, v in summary.items() if k != "trading_date"},
        )

        self._print_daily_summary(summary, no_trade_reasons, strategies_used)
        return summary

    def _get_dominant_regime(self, cycle_rows: list) -> str:
        """Return the most frequent final regime from today's cycle log."""
        if not cycle_rows:
            return "UNKNOWN"
        counts: dict = {}
        for c in cycle_rows:
            r = c.get("final_regime") or c.get("action_taken") or "UNKNOWN"
            if r not in ("SIGNAL_ONLY", "NO_TRADE", None):
                counts[r] = counts.get(r, 0) + 1
        if not counts:
            return "NO_TRADE"
        return max(counts, key=lambda k: counts[k])

    def _print_daily_summary(
        self,
        summary:          dict,
        no_trade_reasons: dict,
        strategies_used:  dict,
    ) -> None:
        """Print end-of-day summary to console."""
        print_section(
            f"END OF DAY SUMMARY — {summary['trading_date']} "
            f"({summary.get('day_label', '')})",
            char="#",
        )
        pf = summary.get("profit_factor")
        pf_display = f"{pf:.3f}" if pf is not None else "N/A"

        print_kv_table({
            "Trades Attempted":    summary["trades_attempted"],
            "Trades Executed":     summary["trades_executed"],
            "Won / Lost":          f"{summary['trades_won']} / {summary['trades_lost']}",
            "Win Rate":            f"{summary['win_rate_pct']:.1f}%",
            "Gross P&L (Rs)":      f"{summary['gross_pnl_rupees']:,.2f}",
            "Total Costs (Rs)":    f"{summary['total_costs_rupees']:,.2f}",
            "Net P&L (Rs)":        f"{summary['net_pnl_rupees']:,.2f}",
            "Net P&L (% capital)": f"{summary['net_pnl_pct_capital']:.3f}%",
            "Profit Factor":       pf_display,
            "Stops Fired":         summary["stops_fired"],
            "Max Concurrent":      summary["max_concurrent_positions"],
            "Max Drawdown (Rs)":   f"{summary['max_intraday_drawdown']:,.2f}",
            "Daily Halt":          bool(summary["daily_halt_triggered"]),
            "NIFTY Open/Close":    f"{summary.get('nifty_open')} / {summary.get('nifty_close')}",
            "NIFTY Range":         f"{summary.get('nifty_low')} - {summary.get('nifty_high')}",
            "VIX Open/Close":      f"{summary.get('vix_open')} / {summary.get('vix_close')}",
            "OR Condition/Width":  f"{summary.get('or_condition')} / {summary.get('or_width')}",
            "Mean VRP (pp)":       summary.get("vrp_mean"),
            "Avg Hold (min)":      f"{summary['avg_hold_minutes']:.1f}",
            "Opening Straddle":    f"{summary.get('opening_straddle', 0):.0f}pts",
            "Realized Move":       f"{summary.get('realized_move', 0):.0f}pts",
            "Straddle Ratio":      summary.get("straddle_ratio"),
            "Dominant Vol Regime": summary.get("dominant_vol_regime"),
            "Dominant Price Regime":summary.get("dominant_price_regime"),
            "Dominant Regime":     summary.get("dominant_final_regime"),
            "Phantom Blocked":     summary.get("phantom_trades_blocked", 0),
            "Phantom Would Win":   summary.get("phantom_would_have_won", 0),
            "Regime Accuracy":     summary.get("regime_accuracy_score"),
            "Capital Start → End": (
                f"Rs{summary['capital_start']:,.0f} → "
                f"Rs{summary['capital_end']:,.0f} "
                f"({summary['capital_change_pct']:.3f}%)"
            ),
            "Event Day":           f"{summary['event_day']} {summary.get('event_name', '')}",
        }, title="PERFORMANCE")

        if strategies_used:
            print("\n  Strategies used:")
            for k, v in strategies_used.items():
                print(f"    {k}: {v}")

        if no_trade_reasons:
            print("\n  Top no-trade reasons:")
            for k, v in sorted(no_trade_reasons.items(), key=lambda x: -x[1])[:10]:
                print(f"    [{v}x] {k}")

        print()
        self.logger.info(
            f"EOD SUMMARY: net_pnl=Rs{summary['net_pnl_rupees']:.2f} "
            f"win_rate={summary['win_rate_pct']:.1f}% "
            f"trades={summary['trades_executed']}"
        )

    # ─────────────────────────────────────────────────────────────────────
    # END OF DAY TASKS
    # ─────────────────────────────────────────────────────────────────────

    def perform_end_of_day_tasks(self) -> None:
        """
        Perform all end-of-day tasks after market close.
        Called once per day after 15:35.
        """
        if self._eod_done:
            return

        self.logger.info("Performing end-of-day tasks")
        trading_date = today_ist().isoformat()

        # ── Close any remaining open positions ────────────────────────────
        open_positions = self.execution_engine._get_open_positions()
        if open_positions:
            self.logger.info(
                f"EOD: closing {len(open_positions)} remaining position(s)"
            )
            self.execution_engine.close_all_positions("EOD_CLOSE")

        # ── Run EOD calibration tasks ─────────────────────────────────────
        try:
            self.cal_engine.run_eod_tasks(trading_date)
        except Exception as e:
            self.logger.error(f"EOD calibration tasks error: {e}", exc_info=True)

        # ── Run weekly calibration on Sundays ─────────────────────────────
        if today_ist().weekday() == 6:  # Sunday
            try:
                self._run_calibration_cycle(force=True, schedule="weekly")
            except Exception as e:
                self.logger.error(f"Weekly calibration error: {e}", exc_info=True)

        # ── Generate daily summary ────────────────────────────────────────
        try:
            self.generate_daily_summary()
        except Exception as e:
            self.logger.error(f"Daily summary error (non-fatal): {e}", exc_info=True)

        self._eod_done = True
        self.logger.info("End-of-day tasks complete.")

    # ─────────────────────────────────────────────────────────────────────
    # GRACEFUL SHUTDOWN
    # ─────────────────────────────────────────────────────────────────────

    def perform_graceful_shutdown(self) -> None:
        """
        Perform graceful shutdown.
        Saves session state. Does NOT close open positions
        (they will be resumed on next startup).
        """
        self.logger.info("Graceful shutdown initiated.")

        open_positions = self.execution_engine._get_open_positions()
        if open_positions:
            self.logger.info(
                f"Shutdown: {len(open_positions)} open position(s) will remain open. "
                f"Engine will resume monitoring on next start."
            )

        self.market_engine._save_session_state()
        self.logger.info(
            f"Session state saved. "
            f"entry_count={self.market_engine.state.get('entry_count', 0)}, "
            f"daily_pnl={self.market_engine.state.get('daily_pnl', 0):.2f}, "
            f"open_positions={len(open_positions) if open_positions else 0}. "
            f"Restart engine to resume."
        )
        self.logger.info("Shutdown complete.")

        try:
            self.db.close()
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────
    # SLEEP HELPER
    # ─────────────────────────────────────────────────────────────────────

    def _sleep(self, seconds: float) -> None:
        """Sleep for the given number of seconds."""
        time_module.sleep(max(0.0, seconds))

    # ─────────────────────────────────────────────────────────────────────
    # MAIN RUN LOOP
    # ─────────────────────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Main run loop.

        Flow:
        1. Print startup banner
        2. Validate Upstox token
        3. Verify lot size
        4. Reconcile open positions
        5. Carry forward capital
        6. Run startup calibration
        7. Enter main loop:
           - Skip holidays
           - Wait for market open
           - Run spot bar cycle (every 60s)
           - Run main trading cycle (every 45s)
           - Run calibration cycle (every 3600s)
           - Perform EOD tasks after 15:35
        8. Graceful shutdown on exit
        """
        self._print_startup_banner()

        # ── Token validation ──────────────────────────────────────────────
        if not self.config.upstox_access_token:
            self.logger.error(
                "FATAL: UPSTOX_ACCESS_TOKEN not set in env.txt. Cannot start."
            )
            self.db.close()
            return

        if not self.client.validate_token():
            self.logger.error(
                "FATAL: Upstox access token is invalid/expired. "
                "Regenerate and update env.txt."
            )
            self.db.close()
            return

        # ── Startup tasks ─────────────────────────────────────────────────
        self._verify_lot_size()
        self._reconcile_open_positions_on_startup()
        self._validate_session_state_integrity()
        self._carry_forward_capital()

        # Startup calibration
        self._run_calibration_cycle(force=True, schedule="startup")
        self._last_calibration_time = time_module.monotonic()

        # Check for event day
        today_event = ExpiryCalendar.is_event_day(today_ist())
        if today_event:
            self.logger.warning(
                f"EVENT DAY: {today_event} | "
                f"Size reduced {int((1-self.config.event_size_multiplier)*100)}% | "
                f"Defined risk only"
            )

        now_t = now_ist().time()
        if dtime(9, 0) <= now_t <= dtime(9, 15):
            self.logger.info("Pre-market validation starting...")
            _retries = 0
            while _retries < 5:
                try:
                    _spot, _vix = self.market_engine.fetch_spot_and_vix()
                    if _spot and _spot > 10000 and _vix and _vix > 5:
                        self.logger.info(
                            f"Pre-market validation passed: "
                            f"spot={_spot:.0f} vix={_vix:.2f}"
                        )
                        break
                    else:
                        self.logger.warning(
                            f"Pre-market validation attempt {_retries+1}: "
                            f"spot={_spot} vix={_vix} — retrying"
                        )
                except Exception as _pme:
                    self.logger.warning(f"Pre-market validation error: {_pme}")
                _retries += 1
                time_module.sleep(30)
        elif now_t < dtime(9, 15):
            self.logger.info(
                "Pre-market: Engine ready. Market opens at 09:15. Waiting."
            )
        elif now_t > dtime(15, 30):
            self.logger.info(
                "Post-market: Engine ready. Next session starts at 09:15."
            )
        self.logger.info("Press Ctrl+C to stop.")

        # ── Main loop ─────────────────────────────────────────────────────
        try:
            while self.running:
                loop_start   = now_ist()
                current_time = loop_start.time()
                now_mono     = time_module.monotonic()

                # ── Holiday check ─────────────────────────────────────────
                if ExpiryCalendar.is_holiday(today_ist()):
                    next_day = ExpiryCalendar.get_next_trading_day(today_ist())
                    self.logger.info(
                        f"Non-trading day ({today_ist().strftime('%A %Y-%m-%d')}). "
                        f"Next trading day: {next_day}. Sleeping 300s."
                    )
                    self._sleep(300)
                    continue

                # ── Pre-market wait ───────────────────────────────────────
                if current_time < dtime(9, 15):
                    self._sleep(30)
                    continue

                # ── Post-market EOD ───────────────────────────────────────
                if current_time > dtime(15, 35):
                    self.logger.info(
                        "Post-market — performing EOD tasks and stopping."
                    )
                    self.perform_end_of_day_tasks()
                    break

                # ── Spot bar cycle (every 60s) ────────────────────────────
                if (now_mono - self._last_spot_time) >= self.config.spot_bar_interval_sec:
                    self._last_spot_time = now_mono
                    self._run_spot_cycle()

                # ── Main trading cycle (every 45s) ────────────────────────
                if (now_mono - self._last_cycle_time) >= self.config.regime_calc_interval_sec:
                    self._last_cycle_time = now_mono
                    try:
                        self.run_one_cycle()
                    except Exception as e:
                        self.logger.error(
                            f"UNHANDLED ERROR in run_one_cycle: {e}"
                        )
                        self.logger.error(traceback.format_exc())
                        self._sleep(30)
                        continue

                # ── Calibration cycle (every 3600s) ───────────────────────
                if (now_mono - self._last_calibration_time) >= self.config.calibration_interval_sec:
                    self._last_calibration_time = now_mono
                    self._run_calibration_cycle(force=False, schedule="daily")

                # ── Loop timing ───────────────────────────────────────────
                loop_duration = (now_ist() - loop_start).total_seconds()
                if loop_duration > 60:
                    self.logger.warning(
                        f"Main loop iteration took {loop_duration:.0f}s"
                    )

                self._sleep(max(0.2, 1.0 - loop_duration))

        except KeyboardInterrupt:
            self.logger.info("Shutdown signal received.")
            self.perform_graceful_shutdown()
            self.running = False
            return

        except Exception as e:
            self.logger.error(f"FATAL ERROR in main loop: {e}")
            self.logger.error(traceback.format_exc())
            self.perform_graceful_shutdown()
            return

        self.db.close()


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    """Main entry point."""
    engine = MainEngine()
    engine.run()


if __name__ == "__main__":
    main()