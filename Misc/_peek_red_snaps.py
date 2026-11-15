#!/usr/bin/env python3
"""Rich snapshot dump for red-day loser entries."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

PER = Path(__file__).resolve().parents[1] / "data" / "per_day"
KEYS = [
    ("2026-10-16", "11:00", "LONG_CALL"),
    ("2026-10-22", "12:12", "BEAR_CALL_SPREAD"),
    ("2026-10-28", "10:07", "BEAR_CALL_SPREAD"),
    ("2026-10-29", "13:06", "BEAR_CALL_SPREAD"),
    ("2026-10-30", "09:49", "BULL_PUT_SPREAD"),
    ("2026-10-30", "11:08", "BEAR_CALL_SPREAD"),
    ("2026-11-02", "12:46", "BULL_PUT_SPREAD"),
    ("2026-11-06", "12:25", "BEAR_CALL_SPREAD"),
    ("2026-11-12", "11:03", "BULL_PUT_SPREAD"),
    ("2026-09-28", "10:19", "LONG_PUT"),
    ("2026-09-18", "10:47", "BULL_PUT_SPREAD"),
]
KEEP = [
    "timestamp", "spot", "final_regime", "price_regime", "vol_regime",
    "confidence_level", "adx_15", "adx_15_mature", "ema_structure",
    "day_move_used_pct", "day_up_used_pct", "day_down_used_pct",
    "iv_behavior", "session_loc", "day_location", "or_width",
    "or_condition", "tape_state", "afternoon_high_fade",
    "afternoon_low_fade", "two_way_auction",
]
SIG_KEEP = [
    "final_regime", "price_regime", "vol_regime", "confidence_level",
    "adx_15", "ema_structure", "day_move_used_pct", "day_up_used_pct",
    "day_down_used_pct", "iv_behavior", "session_loc", "day_location",
    "afternoon_high_fade", "afternoon_low_fade", "or_width",
]


def main() -> int:
    for day, hhmm, sn in KEYS:
        db = PER / f"nifty_algo_{day}.db"
        print(f"\n==== {day} {sn} @{hhmm}")
        if not db.exists():
            print("  NO DB")
            continue
        con = sqlite3.connect(str(db))
        cols = [r[1] for r in con.execute("PRAGMA table_info(market_snapshots)")]
        row = con.execute(
            "SELECT * FROM market_snapshots WHERE time >= ? "
            "ORDER BY timestamp LIMIT 1",
            (hhmm + ":00",),
        ).fetchone()
        if row:
            d = dict(zip(cols, row))
            for k in KEEP:
                if k in d:
                    print(f"  {k}={d[k]}")
        dec = con.execute(
            "SELECT decision_time, action, strategy_name, reason, signals_json "
            "FROM strategy_decisions "
            "WHERE substr(decision_time, 12, 5) >= ? AND action = 'ENTER' "
            "ORDER BY decision_time LIMIT 1",
            (hhmm,),
        ).fetchone()
        if dec:
            try:
                sig = json.loads(dec[4] or "{}")
            except Exception:
                sig = {}
            print(
                f"  LIVE_ENTER {dec[0][11:19]} {dec[2]} "
                f"{(dec[3] or '')[:70]}"
            )
            for k in SIG_KEEP:
                if k in sig:
                    print(f"    sig.{k}={sig.get(k)}")
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
