"""One-shot midday tape census for flat vs green sessions (live cycle_log)."""
import sqlite3
from collections import Counter

con = sqlite3.connect(r"C:\Users\Administrator\Desktop\algo-trading\data\nifty_algo_v3.db")
flat = [
    "2026-09-18", "2026-09-23", "2026-09-28", "2026-09-29", "2026-10-06",
    "2026-10-13", "2026-10-22", "2026-10-26", "2026-10-29", "2026-11-02",
    "2026-11-03", "2026-11-04", "2026-11-06", "2026-11-09", "2026-11-12",
]
green = [
    "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-16", "2026-09-21",
    "2026-10-05", "2026-10-08", "2026-10-12", "2026-10-15", "2026-10-21",
    "2026-10-23", "2026-11-11",
]
for label, days in [("FLAT", flat), ("GREEN", green)]:
    ors: Counter = Counter()
    adxs = []
    for d in days:
        rows = con.execute(
            """
            SELECT or_condition, price_regime, iv_behavior, adx_15, or_width,
                   vol_regime, ema_structure
            FROM cycle_log
            WHERE trading_date=? AND cycle_time BETWEEN '11:45:00' AND '12:30:00'
            ORDER BY cycle_time LIMIT 1
            """,
            (d,),
        ).fetchall()
        if not rows:
            n = con.execute(
                "SELECT COUNT(1) FROM cycle_log WHERE trading_date=?", (d,)
            ).fetchone()[0]
            print(label, d, "NO MIDDAY", "rows=", n)
            continue
        o, p, i, a, w, vr, ema = rows[0]
        ors[o or "?"] += 1
        if a is not None:
            adxs.append(float(a))
        print(
            f"{label} {d} OR={o} px={p} iv={i} adx={None if a is None else round(a,1)} "
            f"ow={w} vr={vr} ema={ema}"
        )
    print("---", label, "OR", dict(ors), "adx", [round(x, 1) for x in sorted(adxs)])
