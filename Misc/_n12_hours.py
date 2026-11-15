import sqlite3
from pathlib import Path

db = Path("data/per_day/nifty_algo_2026-11-12.db")
con = sqlite3.connect(db)
tables = [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table'"
).fetchall()]
print("tables:", tables)
for t in tables:
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})").fetchall()]
    time_cols = [c for c in cols if "time" in c.lower() or "ts" in c.lower()
                 or "stamp" in c.lower() or c.lower() in ("ts_ist", "datetime")]
    if not time_cols:
        continue
    tc = time_cols[0]
    try:
        rows = con.execute(
            f"SELECT substr(CAST({tc} AS TEXT),1,13) h, COUNT(*) "
            f"FROM {t} GROUP BY 1 ORDER BY 1"
        ).fetchall()
        if rows:
            print(f"\n{t}.{tc}:")
            for h, n in rows[-20:]:
                print(f"  {h} {n}")
    except Exception as e:
        print(t, e)
