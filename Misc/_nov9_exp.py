import sqlite3
from collections import Counter

con = sqlite3.connect("data/per_day/nifty_algo_2026-11-09.db")
tabs = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("tabs", tabs[:30])
for t in tabs:
    if "chain" in t.lower() or "option" in t.lower() or "contract" in t.lower():
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
        print(t, cols[:15])
        if any("expir" in c.lower() for c in cols):
            ec = [c for c in cols if "expir" in c.lower()][0]
            rows = con.execute(
                f"SELECT {ec}, COUNT(*) FROM {t} GROUP BY 1 ORDER BY 1"
            ).fetchall()
            print(" expiries", rows[:20])
con.close()
