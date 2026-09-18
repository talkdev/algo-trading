import csv
from pathlib import Path

base = {
    "2026-09-08": 2861,
    "2026-09-09": 7783,
    "2026-09-10": 5417,
    "2026-09-11": 17181,
    "2026-09-15": 11809,
    "2026-09-16": 5943,
    "2026-09-17": 5547,
}
days = list(base) + ["2026-09-18"]
print(f"{'Date':12} {'Base':>10} {'V33':>10} {'Delta':>10} {'Trades':>6}")
tot_b = tot_v = 0
for d in days:
    rows = list(csv.DictReader(open(f"runs/v33/{d}.csv")))
    s = sum(float(r["pnl_rs"]) for r in rows)
    b = base.get(d, 0)
    if d in base:
        tot_b += b
        tot_v += s
    ents = ", ".join(
        f"{r['entry_time'][:5]} {r['strategy'][:16]}@{r['lots']}L={float(r['pnl_rs']):+.0f}"
        for r in rows
    )
    print(f"{d:12} {b:10.0f} {s:10.2f} {s - b:+10.2f} {len(rows):6}  {ents}")
print(
    f"{'WEEK7':12} {tot_b:10.0f} {tot_v:10.2f} {tot_v - tot_b:+10.2f}  "
    f"retain={tot_v / tot_b * 100:.1f}%"
)
