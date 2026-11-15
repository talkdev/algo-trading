"""One-shot census: expiry DTE + OR shape for flat-target days."""
import os
import sqlite3
from datetime import date, datetime

DAYS = ["2026-10-19", "2026-10-22", "2026-11-04", "2026-11-12", "2026-11-09"]
BASE = os.path.join("data", "per_day")


def main() -> None:
    for d in DAYS:
        path = os.path.join(BASE, f"nifty_algo_{d}.db")
        print("====", d, "exists", os.path.exists(path))
        if not os.path.exists(path):
            continue
        con = sqlite3.connect(path)
        cur = con.cursor()
        tabs = [
            r[0]
            for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        print("tables", tabs)
        for t in tabs:
            cols = [r[1] for r in cur.execute(f"PRAGMA table_info({t})").fetchall()]
            n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t}: {n} rows; cols={cols[:30]}")
            # peek expiries / spot if present
            low = {c.lower(): c for c in cols}
            for key in ("expiry", "Expiry", "option_expiry", "dte"):
                if key.lower() in low:
                    cname = low[key.lower()]
                    rows = cur.execute(
                        f"SELECT {cname}, COUNT(*) FROM {t} "
                        f"GROUP BY {cname} ORDER BY COUNT(*) DESC LIMIT 8"
                    ).fetchall()
                    print(f"    by {cname}:", rows)
            for key in ("spot", "nifty_spot", "underlying"):
                if key.lower() in low:
                    cname = low[key.lower()]
                    row = cur.execute(
                        f"SELECT MIN({cname}), MAX({cname}), AVG({cname}) FROM {t}"
                    ).fetchone()
                    print(f"    {cname} min/max/avg:", row)
        day = date.fromisoformat(d)
        # weekday
        print("  weekday", day.strftime("%A"))
        con.close()


if __name__ == "__main__":
    main()
