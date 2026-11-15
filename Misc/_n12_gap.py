import sqlite3
from collections import Counter

con = sqlite3.connect(r"data/per_day/nifty_algo_2026-11-12.db")
rows = con.execute(
    "SELECT substr(timestamp,12,5) FROM market_snapshots "
    "WHERE substr(timestamp,12,2) IN ('12','13','14','15') "
    "ORDER BY timestamp"
).fetchall()
mins = Counter(r[0] for r in rows)
# show empty minutes in 12:00-15:05
for hh in (12, 13, 14, 15):
    missing = []
    for mm in range(60):
        if hh == 15 and mm > 10:
            break
        key = f"{hh:02d}:{mm:02d}"
        if mins.get(key, 0) == 0:
            missing.append(key)
    print(f"hour {hh} missing mins ({len(missing)}):", missing[:40])
print("sample 12:15-13:05 counts:")
for k in sorted(mins):
    if "12:1" <= k <= "13:1" or k.startswith("14:5") or k.startswith("15:0"):
        print(f"  {k} {mins[k]}")
