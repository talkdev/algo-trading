"""Afternoon snapshot for zero days: action, gates, ADX, vol."""
from __future__ import annotations

import sqlite3
from collections import Counter

ZEROS = [
    "2026-09-28", "2026-09-29", "2026-10-12", "2026-10-19",
    "2026-10-22", "2026-11-04", "2026-11-09", "2026-11-12",
]


def main() -> None:
    for d in ZEROS:
        path = f"data/per_day/nifty_algo_{d}.db"
        con = sqlite3.connect(path)
        cur = con.cursor()
        sample = cur.execute(
            "SELECT cycle_time FROM cycle_log ORDER BY cycle_id LIMIT 2"
        ).fetchall()
        rows = cur.execute(
            """
            SELECT cycle_time, final_regime, final_regime_notes, vol_regime,
                   price_regime, or_condition, adx_15, day_move_used_pct,
                   action_taken, no_trade_reason
            FROM cycle_log
            WHERE substr(cycle_time, -8) >= '12:45:00'
              AND substr(cycle_time, -8) < '14:50:00'
            ORDER BY cycle_id
            """
        ).fetchall()
        if not rows:
            rows = cur.execute(
                """
                SELECT cycle_time, final_regime, final_regime_notes, vol_regime,
                       price_regime, or_condition, adx_15, day_move_used_pct,
                       action_taken, no_trade_reason
                FROM cycle_log
                WHERE cycle_time LIKE '%T13:%'
                   OR cycle_time LIKE '% 13:%'
                   OR cycle_time LIKE '%T14:%'
                   OR cycle_time LIKE '% 14:%'
                ORDER BY cycle_id
                LIMIT 200
                """
            ).fetchall()
        print(f"\n=== {d} sample_ts={sample} aft_n={len(rows)} ===")
        acts = Counter(r[8] or "?" for r in rows)
        ntr = Counter(str(r[9] or "")[:90] for r in rows if r[9])
        frn = Counter(str(r[2] or "")[:80] for r in rows)
        print("actions", acts.most_common(6))
        print("no_trade", ntr.most_common(6))
        print("notes", frn.most_common(6))
        if rows:
            r = rows[len(rows) // 2]
            print(
                f"  mid {r[0]} fr={r[1]} vol={r[3]} px={r[4]} or={r[5]} "
                f"adx={r[6]} dm={r[7]} act={r[8]} ntr={str(r[9])[:70]}"
            )
        con.close()


if __name__ == "__main__":
    main()
