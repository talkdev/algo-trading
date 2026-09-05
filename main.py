from __future__ import annotations

import json
import signal
import time as time_module
import traceback
from datetime import datetime, date, time as dtime, timedelta

from nifty_algo_core import (
    Config, Database, RateLimiter, UpstoxClient,
    ExpiryCalendar, now_ist, today_ist,
    print_section, print_kv_table,
    load_config, setup_logging,
)
from market_data_engine import MarketDataEngine
from strategy_engine import StrategyEngine
from execution_engine import ExecutionEngine


class MainEngine:

    def __init__(self):
        self.config = load_config()
        self.db = Database(self.config.db_path)
        self.logger = setup_logging(self.db, self.config.log_dir)
        self.rate_limiter = RateLimiter(self.config.rate_limits)
        self.client = UpstoxClient(
            self.config, self.rate_limiter, self.db, self.logger
        )
        self.market_engine = MarketDataEngine(
            self.config, self.db, self.client, self.rate_limiter, self.logger
        )
        self.strategy_engine = StrategyEngine(
            self.config, self.db, self.market_engine, self.logger
        )
        self.execution_engine = ExecutionEngine(
            self.config, self.db, self.market_engine, self.client, self.logger
        )

        self.loop_count = 0
        self.running = True
        self._last_status_print_time = 0.0
        self._last_cycle_time = 0.0
        self._last_spot_time = 0.0
        self._last_calibration_time = 0.0
        self._eod_done = False

        signal.signal(signal.SIGINT,  self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        self.logger.info(f"Received signal {signum} — shutting down gracefully.")
        self.running = False
        raise KeyboardInterrupt()

    def _print_startup_banner(self) -> None:
        print_section("NIFTY INTRADAY OPTIONS ALGO TRADING ENGINE v2.0", char="#")
        print_kv_table({
            "Mode": "PAPER TRADE" if self.config.paper_trade_mode else "*** LIVE TRADING ***",
            "Starting Capital": self.config.starting_capital,
            "Max Daily Loss": f"{self.config.max_daily_loss_pct*100:.1f}%",
            "Max Risk Per Trade": f"{self.config.max_risk_per_trade_pct*100:.2f}%",
            "Trading Window": (
                f"{self.config.trading_window_start} - "
                f"{self.config.trading_window_last_entry} "
                f"(hard exit {self.config.hard_exit_time})"
            ),
            "Lot Size": self.config.lot_size,
            "ADX Trend Threshold": self.config.adx_trend_threshold,
            "ADX Strong Threshold": self.config.adx_strong_threshold,
            "EMA Fast / Slow": f"{self.config.ema_fast} / {self.config.ema_slow}",
            "Event Size Multiplier": self.config.event_size_multiplier,
            "Tuesday Early Exit": self.config.tuesday_early_exit_enabled,
            "DB Path": str(self.config.db_path),
        }, title="STARTUP CONFIGURATION")
        if not self.config.paper_trade_mode:
            print("\n  " + "!" * 70)
            print("  !!! WARNING: LIVE TRADING MODE — REAL ORDERS WILL BE PLACED !!!")
            print("  " + "!" * 70 + "\n")
        print()

    def _verify_lot_size(self) -> None:
        self.logger.info(
            f"Lot size configured as {self.config.lot_size} units/lot. "
            f"MANUALLY VERIFY against current NSE NIFTY contract spec "
            f"and broker instrument master before live trading."
        )

    def _carry_forward_capital(self) -> None:
        state = self.market_engine.state
        if state.get("entry_count", 0) != 0:
            return
        today_str = today_ist().isoformat()
        last_summary = self.db.query_one(
            "SELECT capital_end FROM daily_summary "
            "WHERE trading_date < ? AND capital_end IS NOT NULL "
            "ORDER BY trading_date DESC LIMIT 1",
            (today_str,),
        )
        if last_summary and last_summary.get("capital_end") is not None:
            prior_capital = last_summary["capital_end"]
            current = state.get("current_capital", 0)
            if abs(prior_capital - current) > 0.01:
                state["current_capital"] = prior_capital
                self.market_engine._save_session_state()
                self.logger.info(
                    f"Capital carried forward: Rs{prior_capital:,.2f} "
                    f"(was Rs{current:,.2f})"
                )

    def _reconcile_open_positions_on_startup(self) -> None:
        today_str = today_ist().isoformat()
        open_positions = self.execution_engine._get_open_positions()
        if not open_positions:
            return
        self.logger.info(
            f"STARTUP RECONCILIATION: {len(open_positions)} open position(s) found"
        )
        for pos in open_positions:
            if pos["trading_date"] != today_str:
                self.logger.warning(
                    f"Closing stale prior-day position: "
                    f"{pos['strategy_name']} from {pos['trading_date']}"
                )
                self.execution_engine.execute_close(pos, "STALE_PRIOR_DAY_CLOSE")
            else:
                self.logger.info(
                    f"Resuming today's open position: "
                    f"{pos['strategy_name']} {pos['position_id'][:16]}..."
                )

    def _get_capital_at_day_start(self) -> float:
        today_str = today_ist().isoformat()
        earliest = self.db.query_one(
            "SELECT capital_at_entry FROM trade_entries "
            "WHERE trading_date=? ORDER BY entry_time ASC LIMIT 1",
            (today_str,),
        )
        if earliest and earliest.get("capital_at_entry") is not None:
            return earliest["capital_at_entry"]
        return self.market_engine.state.get(
            "current_capital", self.config.starting_capital
        )

    def compute_unrealized_pnl(self) -> float:
        C02 = self.config.lot_size
        unrealized = 0.0
        for pos in self.execution_engine._get_open_positions():
            current_prem = pos.get("last_known_premium")
            if current_prem is None:
                continue
            if pos["strategy_type"] == "SELL":
                unrealized += (
                    (pos.get("entry_credit") or 0.0) - current_prem
                ) * pos["final_lots"] * C02
            else:
                unrealized += (
                    current_prem - (pos.get("entry_debit") or 0.0)
                ) * pos["final_lots"] * C02
        return unrealized

    def compute_total_daily_pnl(self) -> float:
        realized = self.market_engine.state.get("daily_pnl", 0.0)
        return realized + self.compute_unrealized_pnl()

    def check_daily_loss_halt(self) -> None:
        state = self.market_engine.state
        current_cap = state.get("current_capital", self.config.starting_capital)
        if not current_cap or current_cap <= 0:
            return
        realized_pnl = state.get("daily_pnl", 0.0)
        day_start_cap = current_cap - realized_pnl
        if day_start_cap <= 0:
            day_start_cap = current_cap
        total_pnl = self.compute_total_daily_pnl()
        loss_pct = max(0.0, -total_pnl) / day_start_cap
        if loss_pct >= self.config.max_daily_loss_pct and not state.get("daily_halted"):
            state["daily_halted"] = True
            self.logger.warning(
                f"DAILY LOSS LIMIT (incl. unrealized): {loss_pct*100:.2f}% "
                f">= {self.config.max_daily_loss_pct*100:.1f}% — halting trading"
            )
            self.market_engine._save_session_state()

    def perform_hard_exit_sweep(self) -> None:
        current_time = now_ist().time()
        if current_time >= dtime(15, 0):
            open_positions = self.execution_engine._get_open_positions()
            if open_positions:
                self.logger.info(
                    f"HARD EXIT SWEEP @ 15:00 — "
                    f"closing {len(open_positions)} position(s)"
                )
                self.execution_engine.close_all_positions("HARD_EXIT_15:00")

    def _run_spot_cycle(self) -> None:
        if not self._market_open():
            return
        try:
            candles = self.client.get_intraday_candles(
                "NSE_INDEX|Nifty 50", "1minute"
            )
            if candles:
                trading_date = today_ist().isoformat()
                rows = []
                for c in candles[-20:]:
                    if len(c) >= 6:
                        try:
                            from nifty_algo_core import parse_ist_timestamp
                            ts_raw = (str(c[0])
                                      .replace("Z", "")
                                      .replace("+05:30", "")
                                      .replace("+0530", ""))
                            ts = datetime.fromisoformat(ts_raw)
                            rows.append((
                                trading_date,
                                ts.isoformat(),
                                1,
                                float(c[1]), float(c[2]),
                                float(c[3]), float(c[4]),
                                int(c[5]),
                                "upstox_intraday",
                            ))
                        except Exception as _te:
                            self.logger.debug(
                                f"Spot bar parse failed: {_te} raw={c[0]}"
                            )
                if rows:
                    try:
                        self.db.executemany(
                            "INSERT OR IGNORE INTO intraday_candles "
                            "(trading_date, candle_time, interval_min, "
                            "open, high, low, close, volume, source) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            rows,
                        )
                    except Exception as e:
                        self.logger.warning(f"Spot bar insert error: {e}")
        except Exception as e:
            self.logger.warning(f"Spot cycle error: {e}")

    def _run_calibration_cycle(self, force: bool = False) -> None:
        if not force and not self._market_open():
            return
        try:
            from regime_bridge import merge_regime_into_signals
            cal = self._calibration_engine_run()
            if cal:
                self.strategy_engine.market_engine._save_session_state()
        except ImportError:
            pass
        except Exception as e:
            self.logger.error(f"Calibration error: {e}", exc_info=True)

    def _calibration_engine_run(self):
        try:
            import numpy as np
            import pandas as pd
            db = self.db
            today_str = today_ist().isoformat()
            n_days = db.query_one(
                "SELECT COUNT(DISTINCT date) as cnt FROM daily_summary"
            )
            n_days_count = n_days["cnt"] if n_days else 0
            vix_df = pd.read_sql_query(
                "SELECT vix_value FROM vix_history WHERE date >= ? ORDER BY timestamp",
                db._conn(),
                params=((date.today() - timedelta(days=365)).isoformat(),),
            )
            if len(vix_df) >= 50:
                v = vix_df["vix_value"].dropna().values
                p25 = float(np.percentile(v, 25))
                p50 = float(np.percentile(v, 50))
                p75 = float(np.percentile(v, 75))
                p90 = float(np.percentile(v, 90))
                tier1 = len(vix_df) >= 50 and n_days_count >= 5
                tier2 = n_days_count >= self.config.min_trading_days_for_calibration and len(vix_df) >= 50
                tier3 = n_days_count >= 60 and len(vix_df) >= 100
                cal_tier = 3 if tier3 else (2 if tier2 else (1 if tier1 else 0))
                valid = tier2
                if valid:
                    self.logger.info(f"Calibration tier={cal_tier} VALID.")
                else:
                    self.logger.warning(
                        f"Calibration tier={cal_tier}. "
                        f"Need {self.config.min_trading_days_for_calibration} days "
                        f"(have {n_days_count})."
                    )
                return {"tier": cal_tier, "valid": valid,
                        "vix_p25": p25, "vix_p50": p50,
                        "vix_p75": p75, "vix_p90": p90}
        except Exception as e:
            self.logger.debug(f"Calibration run error: {e}")
        return None

    def _market_open(self) -> bool:
        if ExpiryCalendar.is_holiday(today_ist()):
            return False
        now = now_ist().time()
        return dtime(9, 15) <= now <= dtime(15, 30)

    def _print_cycle_footer(self) -> None:
        total_pnl = self.compute_total_daily_pnl()
        open_positions = self.execution_engine._get_open_positions()
        print_section("CYCLE SUMMARY")
        print_kv_table({
            "Cycle #": self.loop_count,
            "Open Positions": len(open_positions),
            "Realized P&L Today (Rs)": round(
                self.market_engine.state.get("daily_pnl", 0.0), 2
            ),
            "Unrealized P&L (Rs)": round(self.compute_unrealized_pnl(), 2),
            "Total P&L Today (Rs)": round(total_pnl, 2),
            "Current Capital (Rs)": self.market_engine.state.get("current_capital"),
            "Daily Halted": bool(self.market_engine.state.get("daily_halted")),
            "Entries Today": self.market_engine.state.get("entry_count", 0),
            "Consecutive Stops": self.market_engine.state.get("consecutive_stops", 0),
            "VIX Regime": self.market_engine.state.get("vix_regime"),
            "Day Label": self.market_engine.state.get("day_label"),
        })
        print()

    def run_one_cycle(self) -> None:
        current_time = now_ist().time()
        self.market_engine.reset_if_new_day()

        signals = self.market_engine.run_cycle()

        try:
            from regime_bridge import merge_regime_into_signals
            signals = merge_regime_into_signals(signals, self.market_engine)
        except ImportError:
            pass
        except Exception as e:
            self.logger.debug(f"Regime bridge error: {e}")

        self.execution_engine.monitor_all_positions(signals)

        self.perform_hard_exit_sweep()

        self.check_daily_loss_halt()

        entry_possible = (
            current_time <= dtime(15, 0) and
            not self.market_engine.state.get("daily_halted")
        )

        if entry_possible:
            decision = self.strategy_engine.decide(signals)
            if decision.get("action") == "ENTER":
                self.execution_engine.process_entry_decision(decision, signals)

        total_pnl = self.compute_total_daily_pnl()
        latest_cycle = self.db.query_one(
            "SELECT cycle_id FROM cycle_log WHERE trading_date=? "
            "ORDER BY cycle_id DESC LIMIT 1",
            (today_ist().isoformat(),),
        )
        if latest_cycle:
            self.db.update(
                "cycle_log",
                {"daily_pnl_net": total_pnl},
                {"cycle_id": latest_cycle["cycle_id"]},
            )

        self._print_cycle_footer()
        self.loop_count += 1

    def _compute_max_drawdown(self, pnl_series: list) -> float:
        if not pnl_series:
            return 0.0
        peak = pnl_series[0]
        max_dd = 0.0
        for val in pnl_series:
            if val > peak:
                peak = val
            dd = peak - val
            if dd > max_dd:
                max_dd = dd
        return max_dd

    def generate_daily_summary(self) -> dict:
        trading_date = today_ist().isoformat()
        state = self.market_engine.state

        trades = self.db.query(
            "SELECT * FROM trade_entries WHERE trading_date=?", (trading_date,)
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
            "SELECT * FROM strategy_decisions WHERE trading_date=?", (trading_date,)
        )

        trades_attempted = len(decisions)
        trades_executed  = len(trades)
        trades_won  = sum(1 for e in exits if e["result"] == "WIN")
        trades_lost = sum(1 for e in exits if e["result"] == "LOSS")
        win_rate_pct = (trades_won / len(exits) * 100.0) if exits else 0.0

        gross_pnl_rs  = sum(e["gross_pnl_rupees"] or 0.0 for e in exits)
        total_costs_rs = sum(e["total_costs_rupees"] or 0.0 for e in exits)
        net_pnl_rs    = sum(e["net_pnl_rupees"] or 0.0 for e in exits)

        capital_start = self._get_capital_at_day_start()
        capital_end   = state.get("current_capital", self.config.starting_capital)
        net_pnl_pct   = (net_pnl_rs / capital_start * 100.0) if capital_start else 0.0

        spots = [c["spot"] for c in cycle_rows if c.get("spot") is not None]
        vixs  = [c["vix"]  for c in cycle_rows if c.get("vix")  is not None]
        vrps  = [c["vrp"]  for c in cycle_rows if c.get("vrp")  is not None]
        pnl_series = [c["daily_pnl_net"] for c in cycle_rows if c.get("daily_pnl_net") is not None]

        strategies_used: dict = {}
        for t in trades:
            strategies_used[t["strategy_name"]] = strategies_used.get(t["strategy_name"], 0) + 1

        no_trade_reasons: dict = {}
        for d in decisions:
            if d["action"] == "NO_TRADE":
                r = d["reason"] or "unknown"
                no_trade_reasons[r] = no_trade_reasons.get(r, 0) + 1

        avg_hold = (sum(e["hold_minutes"] or 0.0 for e in exits) / len(exits)) if exits else 0.0
        avg_credit = (
            sum((t["entry_credit"] or t["entry_debit"] or 0.0) for t in trades) / len(trades)
        ) if trades else 0.0
        avg_vrp_entry = (
            sum(t["entry_vrp"] or 0.0 for t in trades) / len(trades)
        ) if trades else 0.0

        gross_wins  = sum(e["net_pnl_rupees"] for e in exits if (e["net_pnl_rupees"] or 0) > 0)
        gross_losses = abs(sum(e["net_pnl_rupees"] for e in exits if (e["net_pnl_rupees"] or 0) < 0))
        profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else (None if gross_wins > 0 else 0.0)

        max_concurrent = max((c["open_positions"] or 0) for c in cycle_rows) if cycle_rows else 0
        stops_fired    = sum(1 for e in exits if e["exit_reason"] == "CLOSE_STOP")
        max_drawdown   = self._compute_max_drawdown(pnl_series)

        event_day_str = ExpiryCalendar.is_event_day(today_ist())

        bars = self.market_engine.get_today_spot_bars()
        opening_spot = closing_spot = high_val = low_val = None
        day_range = day_range_pct = realized_move = straddle_ratio = 0.0
        if not bars.empty:
            mb = bars[bars["time"] >= "09:15:00"]
            if not mb.empty:
                opening_spot = float(mb["open"].iloc[0])
            closing_spot = float(bars["close"].iloc[-1])
            high_val = float(bars["high"].max())
            low_val  = float(bars["low"].min())
            day_range = high_val - low_val
            day_range_pct = (day_range / opening_spot * 100.0) if opening_spot else 0.0
            if opening_spot:
                realized_move = abs(closing_spot - opening_spot)
            opening_straddle = self.market_engine.state.get("_straddle_open_for_summary", 0)
            straddle_ratio = (opening_straddle / day_range) if (day_range > 0 and opening_straddle > 0) else 0.0

        summary = {
            "trading_date": trading_date,
            "day_label": state.get("day_label"),
            "trades_attempted": trades_attempted,
            "trades_executed": trades_executed,
            "trades_won": trades_won,
            "trades_lost": trades_lost,
            "win_rate_pct": win_rate_pct,
            "gross_pnl_rupees": gross_pnl_rs,
            "total_costs_rupees": total_costs_rs,
            "net_pnl_rupees": net_pnl_rs,
            "net_pnl_pct_capital": net_pnl_pct,
            "max_intraday_drawdown": max_drawdown,
            "max_concurrent_positions": max_concurrent,
            "stops_fired": stops_fired,
            "daily_halt_triggered": 1 if state.get("daily_halted") else 0,
            "vix_open": vixs[0] if vixs else None,
            "vix_close": vixs[-1] if vixs else None,
            "vix_low": min(vixs) if vixs else None,
            "vix_high": max(vixs) if vixs else None,
            "nifty_open": spots[0] if spots else opening_spot,
            "nifty_close": spots[-1] if spots else closing_spot,
            "nifty_low": min(spots) if spots else low_val,
            "nifty_high": max(spots) if spots else high_val,
            "or_width": state.get("or_width"),
            "or_condition": state.get("or_condition"),
            "vrp_mean": (sum(vrps) / len(vrps)) if vrps else None,
            "strategies_used_json": json.dumps(strategies_used),
            "no_trade_reasons_json": json.dumps(no_trade_reasons),
            "avg_hold_minutes": avg_hold,
            "avg_credit_pts": avg_credit,
            "avg_vrp_at_entry": avg_vrp_entry,
            "profit_factor": profit_factor,
            "capital_start": capital_start,
            "capital_end": capital_end,
            "capital_change_pct": (
                (capital_end - capital_start) / capital_start * 100.0
            ) if capital_start else 0.0,
            "event_day": int(bool(event_day_str)),
            "event_name": event_day_str,
            "opening_spot": opening_spot,
            "closing_spot": closing_spot,
            "high": high_val,
            "low": low_val,
            "day_range_points": round(day_range, 2),
            "day_range_pct": round(day_range_pct, 3),
            "vix_close_val": vixs[-1] if vixs else None,
            "opening_straddle": self.market_engine.state.get("_straddle_open_for_summary", 0),
            "realized_move": round(realized_move, 2),
            "straddle_ratio": round(straddle_ratio, 3),
            "dominant_regime": self._get_dominant_regime(cycle_rows),
            "created_at": now_ist().isoformat(),
        }

        self.db.upsert(
            "daily_summary",
            {"trading_date": trading_date},
            {k: v for k, v in summary.items() if k != "trading_date"},
        )

        self._print_daily_summary(summary, no_trade_reasons, strategies_used)
        return summary

    def _get_dominant_regime(self, cycle_rows: list) -> str:
        if not cycle_rows:
            return "UNKNOWN"
        regime_counts: dict = {}
        for c in cycle_rows:
            r = c.get("final_regime") or c.get("action_taken") or "UNKNOWN"
            if r not in ("SIGNAL_ONLY", "NO_TRADE", None):
                regime_counts[r] = regime_counts.get(r, 0) + 1
        if not regime_counts:
            return "NO_TRADE"
        return max(regime_counts, key=lambda k: regime_counts[k])

    def _print_daily_summary(
        self, summary: dict, no_trade_reasons: dict, strategies_used: dict
    ) -> None:
        print_section(
            f"END OF DAY SUMMARY — {summary['trading_date']} ({summary['day_label']})",
            char="#"
        )
        pf = summary["profit_factor"]
        pf_display = f"{pf:.3f}" if pf is not None else "N/A (no losses)"
        print_kv_table({
            "Trades Attempted": summary["trades_attempted"],
            "Trades Executed": summary["trades_executed"],
            "Won / Lost": f"{summary['trades_won']} / {summary['trades_lost']}",
            "Win Rate": f"{summary['win_rate_pct']:.1f}%",
            "Gross P&L (Rs)": summary["gross_pnl_rupees"],
            "Total Costs (Rs)": summary["total_costs_rupees"],
            "Net P&L (Rs)": summary["net_pnl_rupees"],
            "Net P&L (% capital)": f"{summary['net_pnl_pct_capital']:.3f}%",
            "Profit Factor": pf_display,
            "Stops Fired": summary["stops_fired"],
            "Max Concurrent": summary["max_concurrent_positions"],
            "Max Drawdown (Rs)": summary["max_intraday_drawdown"],
            "Daily Halt": bool(summary["daily_halt_triggered"]),
            "NIFTY Open/Close": f"{summary['nifty_open']} / {summary['nifty_close']}",
            "NIFTY Range": f"{summary['nifty_low']} - {summary['nifty_high']}",
            "VIX Open/Close": f"{summary['vix_open']} / {summary['vix_close']}",
            "OR Condition/Width": f"{summary['or_condition']} / {summary['or_width']}",
            "Mean VRP (pp)": summary["vrp_mean"],
            "Avg Hold (min)": f"{summary['avg_hold_minutes']:.1f}",
            "Capital Start -> End": (
                f"{summary['capital_start']:.0f} -> {summary['capital_end']:.0f} "
                f"({summary['capital_change_pct']:.3f}%)"
            ),
            "Event Day": f"{summary['event_day']} {summary['event_name']}",
            "Dominant Regime": summary["dominant_regime"],
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

    def perform_end_of_day_tasks(self) -> None:
        if self._eod_done:
            return
        self.logger.info("Performing end-of-day tasks")
        open_positions = self.execution_engine._get_open_positions()
        if open_positions:
            self.logger.info(f"EOD: closing {len(open_positions)} remaining position(s)")
            self.execution_engine.close_all_positions("EOD_CLOSE")
        self._run_calibration_cycle(force=True)
        self.generate_daily_summary()
        self._eod_done = True

    def perform_graceful_shutdown(self) -> None:
        self.logger.info("Graceful shutdown initiated")
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
            f"daily_pnl={self.market_engine.state.get('daily_pnl', 0)}, "
            f"open_positions={len(open_positions) if open_positions else 0}. "
            f"Restart engine to resume."
        )
        self.logger.info("Shutdown complete.")
        self.db.close()

    def _sleep(self, seconds: float) -> None:
        time_module.sleep(max(0.0, seconds))

    def run(self) -> None:
        self._print_startup_banner()

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

        self._verify_lot_size()
        self._reconcile_open_positions_on_startup()
        self._carry_forward_capital()
        self._run_calibration_cycle(force=True)

        today_event = ExpiryCalendar.is_event_day(today_ist())
        if today_event:
            self.logger.warning(f"EVENT DAY: {today_event}")
            self.logger.warning(
                "NON-NEGOTIABLE: All position sizes reduced by 75%. Defined risk only."
            )

        now_t = now_ist().time()
        if now_t < dtime(9, 15):
            self.logger.info("Pre-market: Engine ready. Market opens at 09:15. Waiting.")
        elif now_t > dtime(15, 30):
            self.logger.info("Post-market: Engine ready. Next session starts at 09:15.")
        self.logger.info("Press Ctrl+C to stop.")

        try:
            while self.running:
                loop_start = now_ist()
                current_time = loop_start.time()

                if current_time < dtime(9, 15):
                    self._sleep(30)
                    continue

                if current_time > dtime(15, 40):
                    self.logger.info("Post-market — performing EOD tasks and stopping.")
                    self.perform_end_of_day_tasks()
                    break

                now_mono = time_module.monotonic()

                if (now_mono - self._last_spot_time) >= self.config.spot_bar_interval_sec:
                    self._last_spot_time = now_mono
                    self._run_spot_cycle()

                if (now_mono - self._last_cycle_time) >= self.config.regime_calc_interval_sec:
                    self._last_cycle_time = now_mono
                    try:
                        self.run_one_cycle()
                    except Exception as e:
                        self.logger.error(f"UNHANDLED ERROR in run_one_cycle: {e}")
                        self.logger.error(traceback.format_exc())
                        self._sleep(30)
                        continue

                if (now_mono - self._last_calibration_time) >= self.config.calibration_interval_sec:
                    self._last_calibration_time = now_mono
                    self._run_calibration_cycle()

                status_interval = self.config.STATUS_PRINT_INTERVAL_MIN * 60 if hasattr(self.config, 'STATUS_PRINT_INTERVAL_MIN') else 300
                if (now_mono - self._last_status_print_time) >= status_interval:
                    self._last_status_print_time = now_mono
                    if self._market_open() and self.market_engine._snap_available():
                        pass

                loop_duration = (now_ist() - loop_start).total_seconds()
                if loop_duration > 60:
                    self.logger.warning(
                        f"Main loop iteration took {loop_duration:.0f}s"
                    )

                self._sleep(max(0.5, 1.0 - loop_duration))

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


def main() -> None:
    engine = MainEngine()
    engine.run()


if __name__ == "__main__":
    main()