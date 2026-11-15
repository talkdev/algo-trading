"""Probe live shard snapshots around natural BT credit-stop losers."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PER = ROOT / "data" / "per_day"

STOPS = [
    ("2026-10-16", "11:00", "LONG_CALL"),
    ("2026-10-28", "10:07", "BEAR_CALL_SPREAD"),
    ("2026-10-29", "13:06", "BEAR_CALL_SPREAD"),
    ("2026-10-30", "09:49", "BULL_PUT_SPREAD"),
    ("2026-10-30", "11:08", "BEAR_CALL_SPREAD"),
    ("2026-11-02", "12:46", "BULL_PUT_SPREAD"),
    ("2026-11-06", "12:25", "BEAR_CALL_SPREAD"),
]


def main() -> None:
    for day, hhmm, sn in STOPS:
        db = PER / f"nifty_algo_{day}.db"
        print(f"\n==== {day} {sn} @{hhmm}")
        if not db.exists():
            print("  NO DB")
            continue
        con = sqlite3.connect(str(db))
        row = con.execute(
            "SELECT timestamp, spot, final_regime, price_regime, vol_regime, "
            "confidence_level, adx_15, vix, day_move_used_pct, iv_behavior "
            "FROM market_snapshots "
            "WHERE time >= ? ORDER BY timestamp LIMIT 1",
            (hhmm + ":00",),
        ).fetchone()
        keys = (
            "ts", "spot", "final", "price", "vol", "conf", "adx", "vix",
            "day_move", "iv_beh",
        )
        print("  snap", dict(zip(keys, row)) if row else None)
        rows = con.execute(
            "SELECT decision_time, action, strategy_name, reason, signals_json "
            "FROM strategy_decisions "
            "WHERE strategy_name=? AND action IN ('ENTER','STRATEGY_SELECTED') "
            "ORDER BY decision_time",
            (sn,),
        ).fetchall()
        for r in rows[:4]:
            sig = {}
            try:
                sig = json.loads(r[4] or "{}")
            except Exception:
                pass
            print(
                f"  dec {r[0][11:19]} {r[1]} conf={sig.get('confidence_level')} "
                f"px={sig.get('price_regime')} adx={sig.get('adx_15')} "
                f"or_w={sig.get('or_width')} loc={sig.get('session_loc') or sig.get('day_location')} "
                f"tape={sig.get('tape_state')} reason={(r[3] or '')[:70]}"
            )
        con.close()


if __name__ == "__main__":
    main()
