import sqlite3
for d in ("2026-10-19", "2026-10-22", "2026-11-04", "2026-11-09", "2026-11-12"):
    con = sqlite3.connect(f"data/per_day/nifty_algo_{d}.db")
    rows = con.execute(
        "SELECT expiry, COUNT(*) FROM option_chain_snapshot GROUP BY 1 ORDER BY 1"
    ).fetchall()
    print(d, rows)
    con.close()
