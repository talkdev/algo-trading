#!/usr/bin/env python3
"""In-process single-day BT with gate traces at credit selection."""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import strategy_engine as se
from backtest_engine import (
    BacktestRunner,
    _best_calibration,
    _calibration_sources,
    _db_paths_for_day,
    _open_store,
    _read_calibration_rows,
    make_fill_model,
)
from core import load_config, now_ist

DAY = sys.argv[1] if len(sys.argv) > 1 else "2026-10-29"

_oct = se.StrategyEngine._counter_trend_entry_refusal


def _ct(self, strategy_name, signals):
    r = _oct(self, strategy_name, signals)
    if strategy_name not in (se.BEAR_CALL_SPREAD, se.BULL_PUT_SPREAD):
        return r
    try:
        hard = self._hard_credit_into_trend_refusal(strategy_name, signals)
        ntr = self._no_trade_range_credit_refusal(strategy_name, signals)
        aft = self._afternoon_range_credit_refusal(strategy_name, signals)
        t = now_ist().strftime("%H:%M")
        print(
            f"CT {t} {strategy_name} r={r} hard={hard} ntr={ntr} aft={aft} "
            f"px={signals.get('price_regime')} final={signals.get('final_regime')} "
            f"adx={signals.get('adx_15')} dm={signals.get('day_move_used_pct')} "
            f"up={signals.get('day_up_used_pct')} "
            f"fade_hi={signals.get('afternoon_high_fade')} "
            f"fade_lo={signals.get('afternoon_low_fade')} "
            f"two={signals.get('two_way_auction')} "
            f"bank={self.market_engine.state.get('session_credit_target_count')} "
            f"ema={signals.get('ema_structure')}",
            flush=True,
        )
    except Exception as exc:
        print(f"CT-ERR {strategy_name}: {exc}", flush=True)
    return r


se.StrategyEngine._counter_trend_entry_refusal = _ct

_mom = se.StrategyEngine._momentum_gate


def _mom_w(self, signals):
    ok, reason, direction = _mom(self, signals)
    t = now_ist().strftime("%H:%M")
    if ok and direction != 0:
        print(
            f"MOM-OPEN {t} dir={direction} {reason} "
            f"px={signals.get('price_regime')} adx={signals.get('adx_15')} "
            f"dm={signals.get('day_move_used_pct')} "
            f"up={signals.get('day_up_used_pct')} iv={signals.get('iv_behavior')}"
        )
    elif (not ok) and (
        "call_day_move" in str(reason) or "day_move_used" in str(reason)
    ):
        print(
            f"MOM-REFUSE {t} {reason} "
            f"px={signals.get('price_regime')} adx={signals.get('adx_15')} "
            f"dm={signals.get('day_move_used_pct')} up={signals.get('day_up_used_pct')}"
        )
    return ok, reason, direction


se.StrategyEngine._momentum_gate = _mom_w


def main() -> int:
    config = load_config()
    config = dataclasses.replace(config, force_lots=None)
    db = ROOT / "data" / "per_day" / f"nifty_algo_{DAY}.db"
    day_paths = [str(db)]
    store = _open_store(day_paths)
    cal_sources = _calibration_sources(day_paths, config)
    cal_rows = _read_calibration_rows(cal_sources)
    cal = _best_calibration(cal_rows, DAY)
    runner = BacktestRunner(
        store,
        config,
        make_fill_model(config, edge=0.0, stress_exit=0.5, mode="live"),
        verbose=False,
        trade_report="off",
        cycle_reports=False,
        live_db_path=str(getattr(config, "db_path", "") or ""),
        mirror_live_aborts=False,
        cycle_stride=1,
        seed_calibration=dict(cal) if cal else None,
        calibration_resolved=True,
    )
    res = runner.run([DAY])
    print(f"\nDAY {DAY} pnl={res.daily_pnl.get(DAY, 0):.0f} trades={len(res.trades)}")
    for t in res.trades:
        row = t.as_row() if hasattr(t, "as_row") else t
        if isinstance(row, dict):
            print(
                f"  {row.get('strategy')} {row.get('entry_time')} "
                f"pnl={row.get('pnl_rs')} {row.get('exit_reason')} "
                f"regime={row.get('regime')}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
