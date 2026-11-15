import sqlite3
import sys

day = sys.argv[1] if len(sys.argv) > 1 else "2026-11-12"
c = sqlite3.connect(f"data/per_day/nifty_algo_{day}.db")
tables = [r[0] for r in c.execute(
    "SELECT name FROM sqlite_master WHERE type='table'"
).fetchall()]
print("tables", tables)
for t in tables:
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})").fetchall()]
    tcol = None
    for cand in ("capture_time", "timestamp", "time", "ts"):
        if cand in cols:
            tcol = cand
            break
    if not tcol:
        continue
    sql = (
        f"SELECT substr(CAST({tcol} AS TEXT), 12, 5) AS hh, COUNT(*) "
        f"FROM {t} GROUP BY hh ORDER BY hh"
    )
    try:
        rows = c.execute(sql).fetchall()
    except Exception as e:
        print(t, "err", e)
        continue
    print(t, "n=", sum(r[1] for r in rows), "hours=", rows)
