#!/usr/bin/env python3
"""Inspect exact strikes/credits/EV internals the engine builds at a time."""
import argparse, contextlib, io, sys
from pathlib import Path
BASE = Path(__file__).resolve().parent; sys.path.insert(0, str(BASE))
from core import load_config
import backtest_engine as bt

ap = argparse.ArgumentParser()
ap.add_argument("--db", required=True); ap.add_argument("--date", required=True)
ap.add_argument("--time", required=True)
ap.add_argument("--strategy", default="IRON_CONDOR")
args = ap.parse_args()

cfg = load_config()
store = bt.HistoricalStore(args.db)
day = store.load_day(args.date)
runner = bt.BacktestRunner(store, cfg, bt.FillModel(0.25, 0.5), verbose=False)
runner._build()
sink = io.StringIO()
se, me = runner.se, runner.me
try:
    with contextlib.redirect_stdout(sink):
        sig = None
        for ct in day.cycles:
            dt = day.cycle_dt(ct)
            runner.clock.set(dt); runner.client.point(day, ct)
            try:
                sig = me.run_cycle(); sig = runner._classify(sig)
            except Exception:
                continue
            if dt.strftime("%H:%M") >= args.time:
                chosen = ct
                break
        chosen_ct = day.cycle_dt(chosen)
    print(f"cycle {chosen_ct:%H:%M:%S} spot={sig.get('spot'):.0f} "
          f"dte={sig.get('actual_dte')} vix={sig.get('vix'):.2f} "
          f"vrp={sig.get('vrp_smoothed'):.2f} adx={sig.get('adx_15'):.0f} "
          f"or={sig.get('or_condition')} pos={sig.get('positioning_regime')} "
          f"vol={sig.get('vol_regime')} final={sig.get('final_regime')}")
    chain = me.last_chain
    # engine-selected geometry
    legs, err = se._select_strikes(args.strategy, chain, sig["spot"],
                                   sig["actual_dte"], sig)
    if legs is None:
        print("strike selection failed:", err)
    else:
        vlegs, verr = se._build_validated_legs(legs, chain)
        if vlegs is None:
            print("leg validation failed:", verr)
        else:
            print(f"{args.strategy} engine legs (exec prices):")
            shorts = {"call": [0, 0.0], "put": [0, 0.0]}
            longs = {"call": [0, 0.0], "put": [0, 0.0]}
            for l in vlegs:
                q = chain[float(l["strike"])][l["option_type"]]
                mid = (q["bid"] + q["ask"]) / 2
                px = se._get_exec_price(chain, float(l["strike"]), l["option_type"], l["action"])
                d = q.get("delta")
                print(f"  {l['action']:4s} {l['option_type']:4s} {l['strike']:7.0f} "
                      f"bid={q['bid']:7.2f} ask={q['ask']:7.2f} mid={mid:7.2f} "
                      f"fill={px:7.2f} delta={d} oi={q.get('oi')}")
                bucket = shorts if l["action"] == "SELL" else longs
                bucket[l["option_type"]][0] += 1
                bucket[l["option_type"]][1] += px
            for side in ("call", "put"):
                s, b = shorts[side][1], longs[side][1]
                if s and b:
                    print(f"  {side}: short={s:.2f} long={b:.2f} "
                          f"wing/short={b/s*100:.0f}% net_leg={s-b:.2f}")
            # full params incl EV
            with contextlib.redirect_stdout(sink):
                params = se.compute_params(
                    args.strategy, "debug", sig, sig.get("size_multiplier") or 1.0)
            if params.get("valid"):
                print(f"  VALID: net_credit={params['entry_credit']} "
                      f"wing={params['wing_width']} lots={params['final_lots']} "
                      f"stop={params['stop_premium']} tgt={params['target_premium']:.2f} "
                      f"margin/lot~{params['estimated_margin']/max(params['final_lots'],1):,.0f}")
            else:
                print("  INVALID:", params.get("reason"))
    # scan candidate geometries: shorts at fixed offsets, wings at 50/100/150
    print("\ngeometry scan (mid prices):")
    spot = sig["spot"]; atm = round(spot/50)*50
    for n in (100, 150, 200, 250):
        for w in (50, 100, 150):
            try:
                sc, sp, lc, lp = atm+n, atm-n, atm+n+w, atm-n-w
                cs = (chain[float(sc)]["call"]["bid"]+chain[float(sc)]["call"]["ask"])/2
                ps = (chain[float(sp)]["put"]["bid"]+chain[float(sp)]["put"]["ask"])/2
                cl = (chain[float(lc)]["call"]["bid"]+chain[float(lc)]["call"]["ask"])/2
                pl = (chain[float(lp)]["put"]["bid"]+chain[float(lp)]["put"]["ask"])/2
                print(f"  shorts +-{n} w{w}: C {cs:.1f}/{cl:.1f}={cl/cs*100:3.0f}%  "
                      f"P {ps:.1f}/{pl:.1f}={pl/ps*100:3.0f}%  net={cs+ps-cl-pl:6.1f} "
                      f"dC={chain[float(sc)]['call']['delta']} dP={chain[float(sp)]['put']['delta']}")
            except Exception as e:
                print(f"  n{n} w{w}: {e}")
finally:
    runner._teardown()
