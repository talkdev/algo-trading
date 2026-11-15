"""Structure of remaining flat days from cycle_log + session_state."""
import os
import sqlite3
from collections import Counter

DAYS = ["2026-10-19", "2026-10-22", "2026-11-04", "2026-11-12", "2026-11-09"]
BASE = os.path.join("data", "per_day")


def main() -> None:
    for d in DAYS:
        path = os.path.join(BASE, f"nifty_algo_{d}.db")
        if not os.path.exists(path):
            print(d, "MISSING")
            continue
        con = sqlite3.connect(path)
        cur = con.cursor()
        ss = cur.execute(
            "SELECT or_high, or_low, or_width, or_condition, actual_dte, "
            "actual_expiry, opening_straddle_pts, day_label FROM session_state"
        ).fetchone()
        print("=" * 60)
        print(d, "session", ss)
        # OR expiry from chain
        exp = cur.execute(
            "SELECT DISTINCT expiry FROM option_chain_snapshot LIMIT 5"
        ).fetchall()
        print("  chain expiry", exp)
        # dominant regimes afternoon
        rows = cur.execute(
            "SELECT substr(cycle_time,12,5), spot, or_condition, vol_regime, "
            "price_regime, adx_15, day_move_used_pct, vwap_dist_pct "
            "FROM cycle_log WHERE substr(cycle_time,12,5) >= '13:00' "
            "ORDER BY cycle_time"
        ).fetchall()
        vols = Counter(r[3] for r in rows)
        prices = Counter(r[4] for r in rows)
        ors = Counter(r[2] for r in rows)
        print("  aft vol", vols.most_common(5))
        print("  aft price", prices.most_common(5))
        print("  aft or", ors.most_common(3))
        if rows:
            spots = [r[1] for r in rows]
            print(
                "  aft spot",
                round(min(spots), 1),
                round(max(spots), 1),
                "adx sample",
                rows[len(rows) // 2][5],
            )
        # full day range vs OR
        oh, ol = ss[0], ss[1]
        day_hi, day_lo = cur.execute(
            "SELECT MAX(spot), MIN(spot) FROM cycle_log"
        ).fetchone()
        print(
            "  day hi/lo",
            day_hi,
            day_lo,
            "OR break hi",
            day_hi - oh if oh else None,
            "lo",
            ol - day_lo if ol else None,
        )
        # sample mid-afternoon loc
        mid = cur.execute(
            "SELECT spot FROM cycle_log WHERE substr(cycle_time,12,5)='13:45' "
            "LIMIT 1"
        ).fetchone()
        if mid and oh and ol and oh > ol:
            loc = (mid[0] - ol) / (oh - ol)
            print("  13:45 spot", mid[0], "loc", round(loc, 3))
        # strategy decision reason tops (from last BT write if any)
        reasons = cur.execute(
            "SELECT action, substr(reason,1,80), COUNT(*) FROM strategy_decisions "
            "GROUP BY action, substr(reason,1,80) ORDER BY COUNT(*) DESC LIMIT 12"
        ).fetchall()
        print("  decision tops:")
        for r in reasons:
            print("   ", r)
        con.close()


if __name__ == "__main__":
    main()
