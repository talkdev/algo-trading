"""
════════════════════════════════════════════════════════════════════════════
NIFTY INTRADAY OPTIONS ALGO TRADING ENGINE — 2026 PRODUCTION BUILD
FILE 5 of 5 : MAIN ENGINE (orchestration, hard-exit scheduling, EOD reporting)
════════════════════════════════════════════════════════════════════════════

Save as: main.py  (same directory as the other 4 files)

This is the actual entry point you run: `python main.py`

RESPONSIBILITY:
    - Wires together MarketDataEngine, StrategyEngine, ExecutionEngine
    - Runs the 5-minute cycle loop from market open to close
    - Session-level hard-exit sweeps (13:30 Tuesday / 14:45 general) as a
      defense-in-depth backstop on top of each position's own exit checks
    - Session-level daily-loss halt check using REALIZED + UNREALIZED P&L
      (Module 10/11 only checked realized P&L on close events; this adds an
      unrealized-inclusive check every cycle as well)
    - End-of-day: closes any remaining positions, computes and persists a
      full daily_summary row, prints a complete EOD report
    - Graceful shutdown on SIGINT/SIGTERM: closes open positions, saves state

DEPLOYMENT MODEL: one trading day per process run. Schedule it with your OS:

    Linux (cron), start ~5 min before market open, Mon-Fri:
        10 9 * * 1-5  cd /path/to/engine && /usr/bin/python3 main.py >> logs/stdout.log 2>&1

    Or a systemd timer / Docker container with a daily restart policy.

The engine will safely no-op (just wait, logging periodically) if started
before 09:15 and will exit cleanly after EOD tasks once past 15:30.
"""

from __future__ import annotations

import json
import signal
import time as time_module
import traceback
from datetime import datetime, time as dtime

from nifty_algo_core import (
    Database, RateLimiter, UpstoxClient, IST,
    now_ist, today_ist, print_section, print_kv_table,
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
        self.client = UpstoxClient(self.config, self.rate_limiter, self.db, self.logger)

        self.market_engine = MarketDataEngine(self.config, self.db, self.client, self.rate_limiter, self.logger)
        self.strategy_engine = StrategyEngine(self.config, self.db, self.market_engine, self.logger)
        self.execution_engine = ExecutionEngine(self.config, self.db, self.market_engine, self.client, self.logger)

        self.loop_count = 0
        self.running = True

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        self.logger.info(f"Received signal {signum} — will shut down gracefully.")
        self.running = False
        raise KeyboardInterrupt()

    # ─────────────────────────────────────────────────────────────────
    # STARTUP
    # ─────────────────────────────────────────────────────────────────
    def _print_startup_banner(self) -> None:
        print_section("NIFTY INTRADAY OPTIONS ALGO TRADING ENGINE", char="#")
        print_kv_table({
            "Mode": "PAPER TRADE" if self.config.paper_trade_mode else "*** LIVE TRADING ***",
            "Starting Capital (config default)": self.config.starting_capital,
            "Max Daily Loss": f"{self.config.max_daily_loss_pct*100:.1f}%",
            "Max Risk Per Trade": f"{self.config.max_risk_per_trade_pct*100:.2f}%",
            "Trading Window": f"{self.config.trading_window_start} - {self.config.trading_window_last_entry} "
                               f"(hard exit {self.config.hard_exit_time})",
            "Lot Size": self.config.lot_size, "DB Path": str(self.config.db_path),
        }, title="STARTUP CONFIGURATION")
        if not self.config.paper_trade_mode:
            print("\n  " + "!" * 70)
            print("  !!! WARNING: LIVE TRADING MODE ENABLED — REAL ORDERS WILL BE PLACED !!!")
            print("  " + "!" * 70 + "\n")
        print()

    def _verify_lot_size(self) -> None:
        # NOTE: a fully automated check against the broker's live instrument
        # master would require an instrument-lookup endpoint whose exact
        # schema I could not verify with confidence (see File 1's notice).
        # This is therefore a manual reminder, not an automated gate.
        self.logger.info(f"Lot size configured as {self.config.lot_size} shares/lot. "
                          f"MANUALLY VERIFY this matches the current NSE NIFTY contract "
                          f"specification and your broker's instrument master before trading "
                          f"live — lot sizes have changed before and can change again.")

    def _get_capital_at_day_start(self) -> float:
        """
        Restart-safe: if any trade already happened today, the capital
        recorded at that FIRST entry reflects true start-of-day capital
        (captured before today's P&L accrued). If no trades yet, current
        capital IS still start-of-day capital since nothing has changed it.
        """
        today_str = today_ist().isoformat()
        earliest = self.db.query_one(
            "SELECT capital_at_entry FROM trade_entries WHERE trading_date=? "
            "ORDER BY entry_time ASC LIMIT 1",
            (today_str,),
        )
        if earliest and earliest.get("capital_at_entry") is not None:
            return earliest["capital_at_entry"]
        return self.market_engine.state.get("current_capital", self.config.starting_capital)

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
                self.execution_engine.execute_close(
                    pos, "STALE_PRIOR_DAY_CLOSE"
                )
            else:
                self.logger.info(
                    f"Resuming today's open position: "
                    f"{pos['strategy_name']} {pos['position_id'][:16]}..."
                )

    def _carry_forward_capital(self) -> None:
        """
        File 2 initializes a brand-new day's session_state row with
        current_capital = config.starting_capital (a flat default). For a
        genuine multi-day deployment, today's starting capital should be
        yesterday's ending capital. This corrects that ONLY on a genuinely
        fresh day (entry_count == 0, i.e. nothing has happened today yet) —
        a mid-day restart already has the correct capital from today's own
        persisted state and must NOT be touched here.
        """
        state = self.market_engine.state
        if state.get("entry_count", 0) != 0:
            return  # mid-day restart with today's activity already recorded

        today_str = today_ist().isoformat()
        last_summary = self.db.query_one(
            "SELECT capital_end FROM daily_summary WHERE trading_date < ? "
            "ORDER BY trading_date DESC LIMIT 1",
            (today_str,),
        )
        if last_summary and last_summary.get("capital_end") is not None:
            prior_capital = last_summary["capital_end"]
            current = state.get("current_capital", 0)
            if abs(prior_capital - current) > 0.01:
                state["current_capital"] = prior_capital
                self.market_engine._save_session_state()
                self.logger.info(f"Capital carried forward from previous trading day: "
                                  f"Rs{prior_capital:,.2f} (was Rs{current:,.2f})")

    # ─────────────────────────────────────────────────────────────────
    # P&L / RISK HELPERS
    # ─────────────────────────────────────────────────────────────────
    def compute_unrealized_pnl(self) -> float:
        C02 = self.config.lot_size
        unrealized = 0.0
        for pos in self.execution_engine._get_open_positions():
            current_prem = pos.get("last_known_premium")
            if current_prem is None:
                continue
            if pos["strategy_type"] == "SELL":
                unrealized += ((pos.get("entry_credit") or 0.0) - current_prem) * pos["final_lots"] * C02
            else:
                unrealized += (current_prem - (pos.get("entry_debit") or 0.0)) * pos["final_lots"] * C02
        return unrealized

    def compute_total_daily_pnl(self) -> float:
        realized = self.market_engine.state.get("daily_pnl", 0.0)
        return realized + self.compute_unrealized_pnl()

    def check_daily_loss_halt(self) -> None:
        state = self.market_engine.state
        current_cap = state.get("current_capital", self.config.starting_capital)
        if not current_cap or current_cap <= 0:
            return
        total_pnl = self.compute_total_daily_pnl()
        realized_pnl = state.get("daily_pnl", 0.0)
        day_start_cap = current_cap - realized_pnl
        if day_start_cap <= 0:
            day_start_cap = current_cap
        loss_pct = max(0.0, -total_pnl) / day_start_cap
        if loss_pct >= self.config.max_daily_loss_pct and not state.get("daily_halted"):
            state["daily_halted"] = True
            self.logger.warning(f"DAILY LOSS LIMIT (incl. unrealized): {loss_pct*100:.2f}% "
                                 f">= {self.config.max_daily_loss_pct*100:.1f}% — halting trading for the day")
            self.market_engine._save_session_state()

    # ─────────────────────────────────────────────────────────────────
    # HARD EXIT SWEEP (defense in depth beyond each position's own check)
    # ─────────────────────────────────────────────────────────────────
    def perform_hard_exit_sweep(self) -> None:
        current_time = now_ist().time()
        day_label = self.market_engine.state.get("day_label")

        if current_time >= dtime(15, 0):
            open_positions = self.execution_engine._get_open_positions()
            if open_positions:
                self.logger.info(f"HARD EXIT SWEEP @ 15:00 — closing {len(open_positions)} position(s)")
                self.execution_engine.close_all_positions("HARD_EXIT_15:00")

    # ─────────────────────────────────────────────────────────────────
    # ONE CYCLE
    # ─────────────────────────────────────────────────────────────────
    def _print_cycle_footer(self) -> None:
        total_pnl = self.compute_total_daily_pnl()
        open_positions = self.execution_engine._get_open_positions()
        print_section("CYCLE SUMMARY")
        print_kv_table({
            "Cycle #": self.loop_count,
            "Open Positions": len(open_positions),
            "Realized P&L Today (Rs)": self.market_engine.state.get("daily_pnl", 0.0),
            "Unrealized P&L (Rs)": self.compute_unrealized_pnl(),
            "Total P&L Today (Rs)": total_pnl,
            "Current Capital (Rs)": self.market_engine.state.get("current_capital"),
            "Daily Halted": bool(self.market_engine.state.get("daily_halted")),
            "Entries Today": self.market_engine.state.get("entry_count", 0),
            "Consecutive Stops": self.market_engine.state.get("consecutive_stops", 0),
        })
        print()

    def run_one_cycle(self) -> None:
        current_time = now_ist().time()

        self.market_engine.reset_if_new_day()

        signals = self.market_engine.run_cycle()

        self.execution_engine.monitor_all_positions(signals)

        self.perform_hard_exit_sweep()

        self.check_daily_loss_halt()

        entry_possible = (
            current_time <= dtime(15, 0)  # outer sanity bound; real window enforced in Files 3/4
            and not self.market_engine.state.get("daily_halted")
        )

        if entry_possible:
            decision = self.strategy_engine.decide(signals)
            if decision.get("action") == "ENTER":
                self.execution_engine.process_entry_decision(decision, signals)

        _total_pnl_cycle = self.compute_total_daily_pnl()
        finalize_cycle_with_total_pnl = True
        latest_cycle = self.db.query_one(
            "SELECT cycle_id FROM cycle_log WHERE trading_date=? ORDER BY cycle_id DESC LIMIT 1",
            (today_ist().isoformat(),),
        )
        if latest_cycle:
            self.db.update("cycle_log", {"daily_pnl_net": _total_pnl_cycle},
                           {"cycle_id": latest_cycle["cycle_id"]})
        self._print_cycle_footer()
        self.loop_count += 1

    # ─────────────────────────────────────────────────────────────────
    # END OF DAY
    # ─────────────────────────────────────────────────────────────────
    def _compute_max_drawdown(self, pnl_series: list) -> float:
        """Peak-to-trough decline over the day's realized-P&L series
        (as recorded in cycle_log). Returns a positive Rupee figure."""
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

        trades = self.db.query("SELECT * FROM trade_entries WHERE trading_date=?", (trading_date,))
        exits = self.db.query(
            """SELECT te.* FROM trade_exits te
               JOIN positions p ON te.position_id = p.position_id
               WHERE p.trading_date=?""",
            (trading_date,),
        )
        cycle_rows = self.db.query(
            "SELECT * FROM cycle_log WHERE trading_date=? ORDER BY cycle_id", (trading_date,)
        )
        decisions = self.db.query("SELECT * FROM strategy_decisions WHERE trading_date=?", (trading_date,))

        trades_attempted = len(decisions)
        trades_executed = len(trades)
        trades_won = sum(1 for e in exits if e["result"] == "WIN")
        trades_lost = sum(1 for e in exits if e["result"] == "LOSS")
        win_rate_pct = (trades_won / len(exits) * 100.0) if exits else 0.0

        gross_pnl_rupees = sum(e["gross_pnl_rupees"] or 0.0 for e in exits)
        total_costs_rupees = sum(e["total_costs_rupees"] or 0.0 for e in exits)
        net_pnl_rupees = sum(e["net_pnl_rupees"] or 0.0 for e in exits)

        capital_start = self._get_capital_at_day_start()
        capital_end = state.get("current_capital", self.config.starting_capital)
        net_pnl_pct_capital = (net_pnl_rupees / capital_start * 100.0) if capital_start else 0.0

        spots = [c["spot"] for c in cycle_rows if c["spot"] is not None]
        vixs = [c["vix"] for c in cycle_rows if c["vix"] is not None]
        vrps = [c["vrp"] for c in cycle_rows if c["vrp"] is not None]
        pnl_series = [c["daily_pnl_net"] for c in cycle_rows if c["daily_pnl_net"] is not None]

        strategies_used: dict = {}
        for t in trades:
            strategies_used[t["strategy_name"]] = strategies_used.get(t["strategy_name"], 0) + 1

        no_trade_reasons: dict = {}
        for d in decisions:
            if d["action"] == "NO_TRADE":
                r = d["reason"] or "unknown"
                no_trade_reasons[r] = no_trade_reasons.get(r, 0) + 1

        avg_hold_minutes = (sum(e["hold_minutes"] or 0.0 for e in exits) / len(exits)) if exits else 0.0
        avg_credit_pts = (
            sum((t["entry_credit"] or t["entry_debit"] or 0.0) for t in trades) / len(trades)
        ) if trades else 0.0
        avg_vrp_at_entry = (sum(t["entry_vrp"] or 0.0 for t in trades) / len(trades)) if trades else 0.0

        gross_wins = sum(e["net_pnl_rupees"] for e in exits if (e["net_pnl_rupees"] or 0) > 0)
        gross_losses = abs(sum(e["net_pnl_rupees"] for e in exits if (e["net_pnl_rupees"] or 0) < 0))
        if gross_losses > 0:
            profit_factor = gross_wins / gross_losses
        elif gross_wins > 0:
            profit_factor = None  # undefined (no losses to divide by)
        else:
            profit_factor = 0.0

        max_concurrent = max((c["open_positions"] or 0) for c in cycle_rows) if cycle_rows else 0
        stops_fired = sum(1 for e in exits if e["exit_reason"] == "CLOSE_STOP")
        max_drawdown = self._compute_max_drawdown(pnl_series)

        summary = {
            "trading_date": trading_date, "day_label": state.get("day_label"),
            "trades_attempted": trades_attempted, "trades_executed": trades_executed,
            "trades_won": trades_won, "trades_lost": trades_lost, "win_rate_pct": win_rate_pct,
            "gross_pnl_rupees": gross_pnl_rupees, "total_costs_rupees": total_costs_rupees,
            "net_pnl_rupees": net_pnl_rupees, "net_pnl_pct_capital": net_pnl_pct_capital,
            "max_intraday_drawdown": max_drawdown, "max_concurrent_positions": max_concurrent,
            "stops_fired": stops_fired, "daily_halt_triggered": 1 if state.get("daily_halted") else 0,
            "vix_open": vixs[0] if vixs else None, "vix_close": vixs[-1] if vixs else None,
            "vix_low": min(vixs) if vixs else None, "vix_high": max(vixs) if vixs else None,
            "nifty_open": spots[0] if spots else None, "nifty_close": spots[-1] if spots else None,
            "nifty_low": min(spots) if spots else None, "nifty_high": max(spots) if spots else None,
            "or_width": state.get("or_width"), "or_condition": state.get("or_condition"),
            "vrp_mean": (sum(vrps) / len(vrps)) if vrps else None,
            "strategies_used_json": json.dumps(strategies_used),
            "no_trade_reasons_json": json.dumps(no_trade_reasons),
            "avg_hold_minutes": avg_hold_minutes, "avg_credit_pts": avg_credit_pts,
            "avg_vrp_at_entry": avg_vrp_at_entry, "profit_factor": profit_factor,
            "capital_start": capital_start, "capital_end": capital_end,
            "capital_change_pct": ((capital_end - capital_start) / capital_start * 100.0) if capital_start else 0.0,
            "created_at": now_ist().isoformat(),
        }

        self.db.upsert("daily_summary", {"trading_date": trading_date},
                        {k: v for k, v in summary.items() if k != "trading_date"})

        self._print_daily_summary(summary, no_trade_reasons, strategies_used)
        return summary

    def _print_daily_summary(self, summary: dict, no_trade_reasons: dict, strategies_used: dict) -> None:
        print_section(f"END OF DAY SUMMARY — {summary['trading_date']} ({summary['day_label']})", char="#")
        pf_display = summary["profit_factor"] if summary["profit_factor"] is not None else "N/A (no losses)"
        print_kv_table({
            "Trades Attempted": summary["trades_attempted"], "Trades Executed": summary["trades_executed"],
            "Won / Lost": f"{summary['trades_won']} / {summary['trades_lost']}",
            "Win Rate": f"{summary['win_rate_pct']:.1f}%",
            "Gross P&L (Rs)": summary["gross_pnl_rupees"], "Total Costs (Rs)": summary["total_costs_rupees"],
            "Net P&L (Rs)": summary["net_pnl_rupees"],
            "Net P&L (% of day-start capital)": f"{summary['net_pnl_pct_capital']:.3f}%",
            "Profit Factor": pf_display, "Stops Fired": summary["stops_fired"],
            "Max Concurrent Positions": summary["max_concurrent_positions"],
            "Max Intraday Drawdown (Rs)": summary["max_intraday_drawdown"],
            "Daily Halt Triggered": bool(summary["daily_halt_triggered"]),
            "NIFTY Open/Close": f"{summary['nifty_open']} / {summary['nifty_close']}",
            "NIFTY Range": f"{summary['nifty_low']} - {summary['nifty_high']}",
            "VIX Open/Close": f"{summary['vix_open']} / {summary['vix_close']}",
            "OR Condition/Width": f"{summary['or_condition']} / {summary['or_width']}",
            "Mean VRP Today (pp)": summary["vrp_mean"],
            "Avg Hold Time (min)": f"{summary['avg_hold_minutes']:.1f}",
            "Capital Start -> End": f"{summary['capital_start']:.0f} -> {summary['capital_end']:.0f} "
                                     f"({summary['capital_change_pct']:.3f}%)",
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
        self.logger.info(f"EOD SUMMARY: net_pnl=Rs{summary['net_pnl_rupees']:.2f} "
                          f"win_rate={summary['win_rate_pct']:.1f}% trades={summary['trades_executed']}")

    def perform_end_of_day_tasks(self) -> None:
        self.logger.info("Performing end-of-day tasks")
        open_positions = self.execution_engine._get_open_positions()
        if open_positions:
            self.logger.info(f"EOD: closing {len(open_positions)} remaining position(s)")
            self.execution_engine.close_all_positions("EOD_CLOSE")
        self.generate_daily_summary()

    def perform_graceful_shutdown(self) -> None:
        self.logger.info("Graceful shutdown initiated")
        open_positions = self.execution_engine._get_open_positions()
        if open_positions:
            self.logger.info(
                f"Shutdown: {len(open_positions)} open position(s) will remain open. "
                f"Engine will resume monitoring them on next start."
            )
        self.market_engine._save_session_state()
        self.logger.info(
            f"Session state saved. entry_count={self.market_engine.state.get('entry_count', 0)}, "
            f"daily_pnl={self.market_engine.state.get('daily_pnl', 0)}, "
            f"open_positions={len(open_positions) if open_positions else 0}. "
            f"Restart engine to resume."
        )
        self.logger.info("Shutdown complete.")
        self.db.close()

    # ─────────────────────────────────────────────────────────────────
    # SLEEP HELPERS
    # ─────────────────────────────────────────────────────────────────
    def _sleep(self, seconds: float) -> None:
        time_module.sleep(max(0.0, seconds))

    def _sleep_until(self, target_time: dtime) -> None:
        now = now_ist()
        target_dt = datetime.combine(now.date(), target_time, tzinfo=IST)
        wait_seconds = (target_dt - now).total_seconds()
        if wait_seconds > 0:
            # sleep in short increments so Ctrl+C / SIGTERM is still responsive
            self._sleep(min(wait_seconds, 60))

    # ─────────────────────────────────────────────────────────────────
    # MAIN LOOP
    # ─────────────────────────────────────────────────────────────────
    def run(self) -> None:
        self._print_startup_banner()

        if not self.config.upstox_access_token:
            self.logger.error("FATAL: UPSTOX_ACCESS_TOKEN not set in env.txt. Cannot start.")
            self.db.close()
            return
        if not self.client.validate_token():
            self.logger.error("FATAL: Upstox access token is invalid/expired. "
                               "Regenerate it and update env.txt.")
            self.db.close()
            return

        self._verify_lot_size()
        self._reconcile_open_positions_on_startup()
        self._carry_forward_capital()

        while self.running:
            loop_start = now_ist()
            current_time = loop_start.time()

            try:
                if current_time < dtime(9, 15):
                    self.logger.info(f"Pre-market ({current_time}) — waiting for 09:15")
                    self._sleep_until(dtime(9, 15))
                    continue

                if current_time > dtime(15, 40):
                    self.logger.info("Post-market — performing EOD tasks and stopping.")
                    self.perform_end_of_day_tasks()
                    break

                self.run_one_cycle()

                loop_duration = (now_ist() - loop_start).total_seconds()
                if loop_duration > 60:
                    self.logger.warning(f"Cycle took {loop_duration:.0f}s (longer than the 300s target)")
                self._sleep(max(5, 300 - loop_duration))

            except KeyboardInterrupt:
                self.logger.info("Shutdown signal received — shutting down gracefully.")
                self.perform_graceful_shutdown()
                self.running = False
                return

            except Exception as e:
                self.logger.error(f"UNHANDLED ERROR in main loop: {e}")
                self.logger.error(traceback.format_exc())
                self._sleep(30)
                continue

        self.db.close()


def main() -> None:
    engine = MainEngine()
    engine.run()


if __name__ == "__main__":
    main()