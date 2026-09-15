#!/bin/bash
# usage: bt.sh <outdir> [extra env assignments...]
# Runs all five recorded sessions with a compact trade report + CSV blotter.
set -u
cd /home/user/algo-trading
OUT="$1"; shift
mkdir -p "$OUT"
export TRADE_REPORT_MODE=off
for d in 2026-09-08 2026-09-09 2026-09-10 2026-09-11 2026-09-15; do
  env "$@" python backtest_engine.py --db data/per_day/nifty_algo_$d.db \
      --from $d --to $d --trade-report off --csv "$OUT/$d.csv" \
      > "$OUT/$d.log" 2>&1
done
python - "$OUT" <<'PY'
import sys, os, csv, glob
out=sys.argv[1]
tot=0.0; n=0; w=0
rows=[]
for f in sorted(glob.glob(os.path.join(out,'*.csv'))):
    day=os.path.basename(f)[:10]
    with open(f) as fh:
        for r in csv.DictReader(fh):
            pnl=float(r['pnl_rs']); n+=1; tot+=pnl
            if pnl>0: w+=1
            rows.append((day, r['strategy'], r['entry_time'][:5], r['exit_time'][:5],
                         r['dte'], int(r['lots']), r['exit_reason'][:40], pnl,
                         r['exit_priority']))
for r in rows:
    print(f"{r[0]} dte={r[4]:>2} {r[1]:<18} {r[2]}->{r[3]} lots={r[5]:>2} P{r[8]} {r[7]:>+10,.0f}  {r[6]}")
print(f"\nTOTAL {tot:>+12,.2f}   trades={n}  wins={w}  winrate={(100.0*w/n if n else 0):.1f}%")
PY