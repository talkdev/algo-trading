"""Afternoon regime snaps for remaining flat days."""
import sqlite3
import os

DAYS = ["2026-10-19", "2026-10-22", "2026-11-04", "2026-11-12"]
TIMES = ("11:00", "12:00", "13:00", "13:30", "14:00", "14:30")


def main() -> None:
    for d in DAYS:
        path = os.path.join("data", "per_day", f"nifty_algo_{d}.db")
        con = sqlite3.connect(path)
        print("=" * 60, d)
        for t in TIMES:
            row = con.execute(
                "SELECT substr(cycle_time,12,8), spot, vol_regime, "
                "price_regime, adx_15, or_condition, day_move_used_pct "
                "FROM cycle_log WHERE substr(cycle_time,12,5)=? "
                "ORDER BY cycle_time LIMIT 1",
                (t,),
            ).fetchone()
            print(" ", row)
        # session OR
        ss = con.execute(
            "SELECT or_high, or_low, or_width, actual_dte FROM session_state"
        ).fetchone()
        print("  OR", ss)
        con.close()


if __name__ == "__main__":
    main()
