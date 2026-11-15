#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
from pathlib import Path

DAYS = [
    "2026-09-18",
    "2026-09-23",
    "2026-09-28",
    "2026-10-16",
    "2026-10-22",
    "2026-10-28",
    "2026-10-29",
    "2026-10-30",
    "2026-11-02",
    "2026-11-06",
]
BASE = Path(__file__).resolve().parents[1] / "data" / "per_day"


def main() -> int:
    for d in DAYS:
        p = BASE / f"nifty_algo_{d}.db"
        if not p.exists():
            print(d, "NO_SHARD")
            continue
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT entry_time, exit_time, strategy_name, status, "
                "net_pnl_rupees, exit_reason, selection_reason, entry_spot, "
                "final_lots FROM positions WHERE trading_date=? "
                "ORDER BY entry_time",
                (d,),
            ).fetchall()
        except Exception as e:
            print(d, "ERR", e)
            con.close()
            continue
        total = round(sum(float(r["net_pnl_rupees"] or 0) for r in rows), 0)
        print(f"==== {d} n={len(rows)} sum={total}")
        for r in rows:
            print(
                " ",
                str(r["entry_time"] or "")[11:16],
                str(r["exit_time"] or "")[11:16],
                r["strategy_name"],
                r["status"],
                round(float(r["net_pnl_rupees"] or 0), 0),
                (r["exit_reason"] or "")[:45],
                (r["selection_reason"] or "")[:55],
            )
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
