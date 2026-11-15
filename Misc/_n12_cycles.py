import sqlite3
from collections import Counter

con = sqlite3.connect(r"data/per_day/nifty_algo_2026-11-12.db")
rows = con.execute(
    "SELECT DISTINCT capture_time FROM option_chain_snapshot "
    "WHERE substr(capture_time,12,5) BETWEEN '09:15' AND '15:30' "
    "ORDER BY capture_time"
).fetchall()
cts = [r[0] for r in rows]
print("n_cycles", len(cts))
print("first", cts[0], "last", cts[-1])
hours = Counter(c[11:13] for c in cts)
print("by hour", dict(sorted(hours.items())))
# gaps > 2 min
from datetime import datetime
prev = None
big = []
for c in cts:
    dt = datetime.fromisoformat(c)
    if prev is not None:
        gap = (dt - prev).total_seconds()
        if gap >= 120:
            big.append((prev.isoformat(sep=" ")[11:19], dt.isoformat(sep=" ")[11:19], int(gap)))
    prev = dt
print("gaps>=2min:", len(big))
for a, b, g in big[:25]:
    print(f"  {a} -> {b} ({g}s)")
