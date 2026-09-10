#!/usr/bin/env python3
"""Dump current-code regime/decision timeline for a recorded session."""
import argparse, contextlib, io, sys
from pathlib import Path
BASE = Path(__file__).resolve().parent; sys.path.insert(0, str(BASE))
from core import load_config
import backtest_engine as bt

ap = argparse.ArgumentParser()
ap.add_argument("--db", required=True); ap.add_argument("--date", required=True)
ap.add_argument("--every", type=int, default=40)
ap.add_argument("--tradable", action="store_true",
                help="only print cycles where regime allows a trade")
args = ap.parse_args()

cfg = load_config()
store = bt.HistoricalStore(args.db)
day = store.load_day(args.date)
runner = bt.BacktestRunner(store, cfg, bt.FillModel(0.25, 0.5), verbose=False)
runner._build()
sink = io.StringIO()
print(f"{'time':6s} {'spot':>8s} {'vix':>5s} {'vrp':>5s} {'dte':>3s} {'adx':>4s} "
      f"{'vol':20s} {'price':10s} {'pos':12s} {'conf':6s} {'final':22s} notes")
try:
    for i, ct in enumerate(day.cycles):
        dt = day.cycle_dt(ct)
        runner.clock.set(dt); runner.client.point(day, ct)
        try:
            with contextlib.redirect_stdout(sink):
                sig = runner.me.run_cycle()
                sig = runner._classify(sig)
                dec = runner.se.decide(sig)
        except Exception:
            continue
        if i % args.every != 0 and not args.tradable:
            continue
        if dec.get("action") == "ENTER":
            p = dec.get("params", {})
            note = f"ENTER {p.get('strategy_name')} credit={p.get('entry_credit')} lots={p.get('final_lots')}"
        else:
            note = str(dec.get("reason", ""))[:78]
        if args.tradable and dec.get("action") != "ENTER":
            continue
        hm = dt.strftime("%H:%M")
        print(f"{hm:6s} {sig.get('spot',0):>8.0f} {sig.get('vix',0) or 0:>5.2f} "
              f"{sig.get('vrp_smoothed',0) or 0:>5.2f} {str(sig.get('actual_dte')):>3s} "
              f"{sig.get('adx_15',0) or 0:>4.0f} {str(sig.get('vol_regime')):20s} "
              f"{str(sig.get('price_regime')):10.10s} {str(sig.get('positioning_regime')):12.12s} "
              f"{str(sig.get('confidence_level')):6.6s} {str(sig.get('final_regime')):22.22s} {note}")
finally:
    runner._teardown()
