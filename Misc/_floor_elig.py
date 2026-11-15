import json
import sqlite3

con = sqlite3.connect(r"data/nifty_algo_v3.db")
zeros = [
    "2026-09-23", "2026-09-28", "2026-09-29", "2026-10-06", "2026-10-13",
    "2026-10-22", "2026-10-26", "2026-10-29", "2026-11-02", "2026-11-03",
    "2026-11-04", "2026-11-06", "2026-11-09", "2026-11-12",
]
for d in zeros:
    r = con.execute(
        """
        SELECT raw_json FROM cycle_log
        WHERE trading_date=? AND substr(cycle_time,12,5)>='11:45'
          AND or_condition IS NOT NULL
        ORDER BY cycle_time LIMIT 1
        """,
        (d,),
    ).fetchone()
    if not r:
        print(d, "NO")
        continue
    j = json.loads(r[0])
    sp = float(j.get("spot") or 0)
    op = float(j.get("day_open_spot") or 0)
    oh = float(j.get("or_high") or 0)
    ol = float(j.get("or_low") or 0)
    drift = abs(sp - op) if sp and op else 0
    inside = (ol <= sp <= oh) if oh and ol else None
    print(
        f"{d} OR={j.get('or_condition')} px={j.get('price_regime')} "
        f"ema={j.get('ema_structure')} adx={round(float(j.get('adx_15') or 0),1)} "
        f"iv={j.get('iv_behavior')} vol={j.get('vol_regime')} "
        f"drift={drift:.0f} inside={inside}"
    )
