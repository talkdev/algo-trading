"""Tape at soft-credit wins vs stops (from pre-bank-first blotter)."""
from __future__ import annotations
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARDS = ROOT / "data" / "per_day"

# Earlier envelope BT credit blotter
CASES = [
    ("2026-09-09", "10:00", "WIN", "approx"),  # need real times from log if any
    ("2026-09-18", "10:47", "STOP", "BPS"),
    ("2026-10-07", "11:24", "WIN", "BCS"),
    ("2026-10-14", "11:35", "WIN", "BPS"),
    ("2026-10-27", "12:42", "WIN", "BCS"),
    ("2026-10-28", "10:27", "STOP", "BCS"),
    ("2026-10-29", "13:06", "STOP", "BCS"),
    ("2026-10-30", "09:49", "STOP", "BPS"),
    ("2026-11-04", "13:06", "WIN", "BPS"),
    ("2026-11-05", "10:34", "WIN", "BPS"),
    ("2026-11-06", "12:25", "STOP", "BCS"),
    ("2026-11-11", "09:47", "WIN", "BCS"),
    ("2026-11-12", "11:03", "STOP", "BPS"),
    ("2026-09-10", "10:30", "WIN", "guess"),
    ("2026-09-16", "11:00", "WIN", "guess"),
    ("2026-09-17", "11:00", "WIN", "guess"),
    ("2026-09-25", "11:00", "WIN", "guess"),
    ("2026-09-28", "11:00", "WIN", "guess"),
    ("2026-09-30", "11:00", "WIN", "guess"),
]


def db_for(day: str) -> Path | None:
    p = SHARDS / f"nifty_algo_{day}.db"
    return p if p.exists() else None


def row_at(day: str, hhmm: str):
    db = db_for(day)
    if not db:
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(market_snapshots)")}
    want = [c for c in (
        "timestamp", "adx_15", "price_regime", "confidence_level",
        "iv_behavior", "spot", "day_move_pct", "or_location",
    ) if c in cols]
    r = con.execute(
        f"SELECT {', '.join(want)} FROM market_snapshots "
        f"WHERE substr(timestamp,12,5)=? ORDER BY timestamp LIMIT 1",
        (hhmm,),
    ).fetchone()
    return dict(r) if r else None


def main():
    for day, hhmm, tag, note in CASES:
        r = row_at(day, hhmm)
        if not r:
            # nearest minute search
            db = db_for(day)
            if not db:
                print(day, "NO_DB")
                continue
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            r = con.execute(
                "SELECT timestamp, adx_15, price_regime, confidence_level, "
                "iv_behavior, spot FROM market_snapshots "
                "WHERE substr(timestamp,12,5) LIKE ? "
                "ORDER BY timestamp LIMIT 1",
                (hhmm[:4] + "%",),
            ).fetchone()
            r = dict(r) if r else None
        print(f"{tag:4} {day} {hhmm} {note}: {r}")


if __name__ == "__main__":
    main()
