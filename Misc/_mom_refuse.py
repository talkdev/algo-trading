"""Top momentum refuse suffixes for remaining flat days."""
from __future__ import annotations

import sqlite3
from collections import Counter

DAYS = [
    "2026-09-29", "2026-10-19", "2026-10-22",
    "2026-11-04", "2026-11-09", "2026-11-12",
]


def main() -> None:
    for d in DAYS:
        con = sqlite3.connect(f"data/per_day/nifty_algo_{d}.db")
        cur = con.cursor()
        tabs = [
            r[0]
            for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
        if "strategy_decisions" not in tabs:
            print(d, "no strategy_decisions", tabs[:8])
            con.close()
            continue
        rows = cur.execute(
            "SELECT reason FROM strategy_decisions WHERE trading_date=?",
            (d,),
        ).fetchall()
        c = Counter()
        for (reason,) in rows:
            r = str(reason or "")
            if "momentum_refused:" in r:
                suf = r.split("momentum_refused:", 1)[1][:90]
                c[suf] += 1
            elif r.startswith("momentum_") or "momentum_" in r.lower():
                c[r[:90]] += 1
        print(f"\n=== {d} decisions={len(rows)} mom_buckets={sum(c.values())} ===")
        for k, v in c.most_common(8):
            print(f"  {v:5d}  {k}")
        con.close()


if __name__ == "__main__":
    main()
