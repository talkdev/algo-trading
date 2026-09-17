#!/usr/bin/env python3
"""Compare two-way range / ADX context on all session DBs."""
import sqlite3
from pathlib import Path

ROOT = Path(r"C:\Users\Administrator\Desktop\algo-trading\data")
days = [
    ("2026-09-08", ROOT / "per_day/nifty_algo_2026-09-08.db"),
    ("2026-09-09", ROOT / "per_day/nifty_algo_2026-09-09.db"),
    ("2026-09-10", ROOT / "per_day/nifty_algo_2026-09-10.db"),
    ("2026-09-11", ROOT / "per_day/nifty_algo_2026-09-11.db"),
    ("2026-09-15", ROOT / "per_day/nifty_algo_2026-09-15.db"),
    ("2026-09-16", ROOT / "per_day/nifty_algo_2026-09-16.db"),
    ("2026-09-17", ROOT / "per_day/nifty_algo_2026-09-17.db"),
]


def snap(db, d, tp):
    c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    r = c.execute(
        """
        SELECT MIN(spot_at_capture) lo, MAX(spot_at_capture) hi,
               AVG(spot_at_capture) avg
        FROM option_chain_snapshot
        WHERE trading_date=? AND substr(capture_time,12,5)<=?
          AND substr(capture_time,12,5)>='09:15'
        """,
        (d, tp),
    ).fetchone()
    pos = list(c.execute(
        """
        SELECT strategy_name, substr(entry_time,12,8) et, final_lots,
               ROUND(net_pnl_rupees,1) pnl, actual_dte, final_regime_at_entry
        FROM positions WHERE trading_date=?
        """,
        (d,),
    ))
    first = c.execute(
        """
        SELECT spot_at_capture FROM option_chain_snapshot
        WHERE trading_date=? AND substr(capture_time,12,5)>='09:15'
        ORDER BY capture_time LIMIT 1
        """,
        (d,),
    ).fetchone()
    c.close()
    return r, pos, (first[0] if first else None)

for d, p in days:
    if not p.exists():
        print(d, "MISSING", p)
        continue
    print("=" * 72)
    print(d, p.name)
    for tp in ("10:35", "11:30", "12:20"):
        r, pos, op = snap(p, d, tp)
        rng = (r["hi"] or 0) - (r["lo"] or 0)
        print(
            f"  thru {tp}: open={op:.1f} lo={r['lo']:.1f} hi={r['hi']:.1f} "
            f"rng={rng:.0f} avg={r['avg']:.1f}"
        )
    _, pos, _ = snap(p, d, "15:30")
    print("  live positions:", [dict(x) if hasattr(x, "keys") else x for x in pos])
