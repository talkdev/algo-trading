"""Shape of flat BT sessions from cycle_log after latest code paths.

Uses per-day cycle_log only as market-shape proxy (spot/OR/ADX/vol), not
as decision truth — decision truth comes from rejection tables in BT.
"""
from __future__ import annotations

import sqlite3

DAYS = [
    "2026-09-28", "2026-09-29", "2026-10-12", "2026-10-19",
    "2026-10-22", "2026-11-04", "2026-11-09", "2026-11-12",
]


def main() -> None:
    for d in DAYS:
        con = sqlite3.connect(f"data/per_day/nifty_algo_{d}.db")
        cur = con.cursor()
        rows = cur.execute(
            """
            SELECT substr(cycle_time,12,8), spot, or_high, or_low, or_width,
                   adx_15, vol_regime, price_regime, or_condition,
                   day_move_used_pct, day_high_so_far, day_low_so_far
            FROM cycle_log
            WHERE substr(cycle_time,12,8) BETWEEN '13:00:00' AND '14:30:00'
            ORDER BY cycle_id
            """
        ).fetchall()
        # day_high_so_far may not exist
        if not rows:
            cols = [r[1] for r in cur.execute("PRAGMA table_info(cycle_log)")]
            print(d, "no rows; has", [c for c in cols if "day_" in c or c=="spot"])
            # fallback without day high/low columns
            rows = cur.execute(
                """
                SELECT substr(cycle_time,12,8), spot,
                       (SELECT NULL), (SELECT NULL), or_width,
                       adx_15, vol_regime, price_regime, or_condition,
                       day_move_used_pct
                FROM cycle_log
                WHERE substr(cycle_time,12,8) BETWEEN '13:00:00' AND '14:30:00'
                ORDER BY cycle_id
                LIMIT 5
                """
            ).fetchall()
            for r in rows:
                print(" ", r)
            con.close()
            continue
        # pick 13:00, 13:30, 14:00
        want = ("13:00", "13:30", "14:00")
        print(f"\n=== {d} n={len(rows)} ===")
        for w in want:
            hit = next((r for r in rows if r[0] and r[0].startswith(w)), None)
            if not hit:
                continue
            t, sp, oh, ol, ow, adx, vol, px, oc, dm = hit[:10]
            try:
                spf = float(sp or 0)
                ohf = float(oh or 0) if oh is not None else 0
                olf = float(ol or 0) if ol is not None else 0
            except Exception:
                spf = ohf = olf = 0
            side = "?"
            if ohf and spf >= ohf:
                side = "above_ORH"
            elif olf and spf <= olf:
                side = "below_ORL"
            else:
                side = "inside_OR"
            print(
                f"  {t} sp={spf:.0f} OR=[{olf:.0f},{ohf:.0f}] {side} "
                f"adx={adx} vol={vol} px={px} or={oc} dm={dm}"
            )
        con.close()


if __name__ == "__main__":
    main()
