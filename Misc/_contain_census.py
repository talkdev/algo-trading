"""One-shot: why WIDE containment did not convert flat days."""
from __future__ import annotations

import os
import sqlite3
from collections import Counter

ZEROS = [
    "2026-09-28", "2026-09-29", "2026-10-12", "2026-10-19",
    "2026-10-22", "2026-11-04", "2026-11-09", "2026-11-12",
]


def main() -> None:
    for d in ZEROS:
        path = f"data/per_day/nifty_algo_{d}.db"
        if not os.path.exists(path):
            print(d, "NO_DB")
            continue
        con = sqlite3.connect(path)
        cur = con.cursor()
        tabs = [
            r[0]
            for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        # Prefer richest decision table
        pick = None
        for t in (
            "cycle_log",
            "decision_log",
            "strategy_decisions",
            "engine_cycle_log",
            "decisions",
        ):
            if t in tabs:
                pick = t
                break
        if not pick:
            print(d, "tabs", tabs[:20])
            con.close()
            continue
        cols = [r[1] for r in cur.execute(f"PRAGMA table_info({pick})").fetchall()]
        print(f"\n=== {d} table={pick} n={cur.execute(f'SELECT COUNT(*) FROM {pick}').fetchone()[0]} ===")
        print("cols:", cols)
        # Sample afternoon NO_TRADE reasons
        reason_cols = [c for c in cols if "reason" in c.lower() or "note" in c.lower()]
        fr_cols = [c for c in cols if "regime" in c.lower() or "final" in c.lower()]
        or_cols = [c for c in cols if c.lower() in ("or_condition", "or_width", "adx_15", "vol_regime")]
        time_cols = [c for c in cols if "time" in c.lower() or c.lower() in ("ts", "bar_ts", "cycle_ts")]
        print("reason_cols", reason_cols, "fr", fr_cols, "or", or_cols, "time", time_cols)
        # Dump distinct action/reason after 12:45 if possible
        if reason_cols:
            rc = reason_cols[0]
            q = f"SELECT {rc}, COUNT(*) FROM {pick} GROUP BY 1 ORDER BY 2 DESC LIMIT 15"
            try:
                rows = cur.execute(q).fetchall()
                print("top reasons:")
                for r in rows:
                    print(" ", r)
            except Exception as e:
                print("reason query fail", e)
        con.close()


if __name__ == "__main__":
    main()
