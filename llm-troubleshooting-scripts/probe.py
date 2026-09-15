#!/usr/bin/env python3
"""Replay a session through the REAL engines and dump every cycle's signals,
decide() verdict and monitor_position() verdict to CSV.

usage: probe.py <db> <date> <out.csv> [--open-only]
"""
from __future__ import annotations
import sys, csv, json, io, contextlib
from pathlib import Path

BASE = Path('/home/user/algo-trading')
sys.path.insert(0, str(BASE))

import backtest_engine as bt
from core import load_config


def main() -> int:
    db, date, out = sys.argv[1], sys.argv[2], sys.argv[3]
    cfg = load_config()
    store = bt.HistoricalStore(db)
    runner = bt.BacktestRunner(store, cfg, bt.FillModel(0.25, 0.5), False, trade_report='off')

    rows = []
    sig_rows = []

    # wrap decide()
    def wrap_decide(se):
        orig = se.decide

        def decide(signals, *a, **k):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                d = orig(signals, *a, **k)
            rows.append({
                'time': runner.clock.now().strftime('%H:%M:%S'),
                'kind': 'DECIDE',
                'action': d.get('action'),
                'reason': str(d.get('reason'))[:400],
                'params': json.dumps({k2: v for k2, v in (d.get('params') or {}).items()
                                      if k2 in ('strategy_name', 'final_lots', 'net_credit',
                                                'stop_premium', 'target_premium', 'legs',
                                                'wing_width', 'max_loss_per_lot', 'total_max_risk',
                                                'selection_reason')}, default=str)[:900],
            })
            return d
        se.decide = decide

    def wrap_monitor(xe):
        orig = xe.monitor_position

        def monitor_position(position, signals, *a, **k):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                r = orig(position, signals, *a, **k)
            action, prio, ctx = r
            rows.append({
                'time': runner.clock.now().strftime('%H:%M:%S'),
                'kind': 'MONITOR',
                'action': action,
                'reason': f"prio={prio} " + str(ctx.get('reason_detail') or ctx.get('reason') or '')[:300],
                'params': json.dumps({k2: v for k2, v in (ctx or {}).items()
                                      if not isinstance(v, (list, dict))}, default=str)[:600],
            })
            return r
        xe.monitor_position = monitor_position

    KEYS = ['spot', 'vix', 'atm_strike', 'actual_dte', 'actual_expiry', 'vol_regime', 'price_regime',
            'positioning_regime', 'final_regime', 'confidence_level', 'confidence_score', 'size_multiplier',
            'block_new_entries', 'adx_15', 'adx_15_mature', 'adx_60', 'ema_structure', 'hh_hl',
            'or_condition', 'or_width', 'or_high', 'or_low', 'choppy_detected', 'vwap', 'vwap_dist_pct',
            'pcr', 'pcr_change', 'skew_ratio', 'skew_otm', 'oi_change_pct', 'resistance_strike',
            'resistance_strength', 'support_strike', 'support_strength', 'max_pain', 'day_high', 'day_low',
            'prev_close', 'gap_direction', 'gap_points', 'gap_fade_opportunity', 'day_move_used_pct',
            'opening_straddle_pts', 'iv_behavior', 'iv_change_pct_from_open', 'atm_iv_pct',
            'parkinson_rv_pct', 'vrp_raw', 'vrp_smoothed', 'event_day', 'event_name', 'day_label',
            'day_move_used_pct_directional', 'day_move_directional_pct', 'straddle_ratio',
            'momentum_adx_fast', 'adx_fast', 'trend_strength']

    def wrap_classify(r):
        orig = r._classify

        def _classify(signals, *a, **k):
            s = orig(signals, *a, **k)
            rec = {'time': r.clock.now().strftime('%H:%M:%S')}
            for k2 in KEYS:
                if k2 in s:
                    rec[k2] = s[k2]
            extra = {k2: v for k2, v in s.items()
                     if k2 not in KEYS and isinstance(v, (int, float, str, bool, type(None)))}
            rec['_extra_keys'] = ','.join(sorted(extra.keys()))
            sig_rows.append(rec)
            return s
        r._classify = _classify

    runner._build()
    wrap_decide(runner.se)
    wrap_monitor(runner.xe)
    wrap_classify(runner)
    with contextlib.redirect_stdout(io.StringIO()):
        runner.run_day(date)
    runner._teardown()

    with open(out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['time', 'kind', 'action', 'reason', 'params'])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    with open(out.replace('.csv', '_signals.csv'), 'w', newline='') as f:
        keys = ['time'] + KEYS + ['_extra_keys']
        w = csv.DictWriter(f, fieldnames=keys, extrasaction='ignore')
        w.writeheader()
        for r in sig_rows:
            w.writerow(r)
    print(f"wrote {len(rows)} decision rows, {len(sig_rows)} signal rows -> {out}")
    return 0


if __name__ == '__main__':
    sys.exit(main())