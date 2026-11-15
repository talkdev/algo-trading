import json
import sqlite3
from pathlib import Path

PER = Path(__file__).resolve().parents[1] / "data" / "per_day"
CASES = [
    ("2026-11-12", "11:03"),
    ("2026-10-29", "13:06"),
    ("2026-11-02", "12:46"),
    ("2026-10-30", "09:49"),
    ("2026-10-30", "11:08"),
    ("2026-09-18", "10:47"),
    ("2026-10-22", "12:12"),
    ("2026-10-28", "10:07"),
    ("2026-11-06", "12:25"),
    ("2026-09-28", "10:19"),
    ("2026-10-16", "11:00"),
]

for day, hhmm in CASES:
    con = sqlite3.connect(str(PER / f"nifty_algo_{day}.db"))
    h, m = map(int, hhmm.split(":"))
    end = f"{h:02d}:{m+3:02d}"
    rows = con.execute(
        "SELECT decision_time, action, strategy_name, reason, signals_json "
        "FROM strategy_decisions "
        "WHERE substr(decision_time,12,5) BETWEEN ? AND ? "
        "ORDER BY decision_time LIMIT 12",
        (hhmm, end),
    ).fetchall()
    print(f"\n==== {day} {hhmm} rows={len(rows)}")
    for r in rows:
        if r[1] not in ("ENTER", "STRATEGY_SELECTED") and "SELECTED" not in str(r[1]):
            continue
        sig = {}
        try:
            sig = json.loads(r[4] or "{}")
        except Exception:
            pass
        print(
            f"  {r[0][11:19]} {r[1]:20s} {r[2] or '-':16s} "
            f"{(r[3] or '')[:85]}"
        )
        print(
            f"     or={sig.get('or_condition')} loc={sig.get('session_loc')} "
            f"adx={sig.get('adx_15')} px={sig.get('price_regime')} "
            f"final={sig.get('final_regime')} dm={sig.get('day_move_used_pct')} "
            f"hi_fade={sig.get('afternoon_high_fade')} "
            f"lo_fade={sig.get('afternoon_low_fade')}"
        )
    con.close()
