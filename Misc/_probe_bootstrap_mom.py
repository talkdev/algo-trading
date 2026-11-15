"""One-shot: tape at unbanked LONG entries that won vs stopped."""
from __future__ import annotations
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARDS = ROOT / "data" / "per_day"

# From latest bank-first BT blotter
CASES = [
    ("2026-10-16", "11:00", "STOP"),
    ("2026-10-30", "12:13", "STOP"),
    ("2026-09-11", "11:33", "WIN"),
    ("2026-10-15", "10:56", "WIN"),
    ("2026-11-05", "12:11", "WIN"),
    ("2026-10-08", "10:22", "WIN"),
    ("2026-10-12", "10:12", "WIN"),
    ("2026-10-23", "10:06", "WIN"),
]


def shard_db(day: str) -> Path | None:
    for p in SHARDS.rglob("*.db"):
        if day in p.as_posix():
            return p
    # flat layout: data/per_day/YYYY-MM-DD.db or folder
    for cand in (
        SHARDS / f"{day}.db",
        SHARDS / day / "nifty_algo_v3.db",
        SHARDS / day / f"{day}.db",
    ):
        if cand.exists():
            return cand
    return None


def main() -> None:
    print("shards root", SHARDS, "exists", SHARDS.exists())
    sample = list(SHARDS.rglob("*.db"))[:5]
    print("sample", sample)
    for day, hhmm, tag in CASES:
        db = shard_db(day)
        if not db:
            print(day, "NO_DB")
            continue
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        tabs = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        # Prefer market_snapshots / cycle_snapshots style
        table = None
        for t in ("market_snapshots", "cycle_snapshots", "signal_snapshots", "cycles"):
            if t in tabs:
                table = t
                break
        if not table:
            print(day, "tabs", sorted(tabs)[:20])
            continue
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
        want = [
            c for c in (
                "ts", "timestamp", "cycle_time", "adx_15", "adx",
                "price_regime", "confidence_level", "day_move_pct",
                "day_up_pct", "or_location", "spot", "iv_behavior",
                "trading_date", "captured_at",
            ) if c in cols
        ]
        tcol = next(
            (c for c in ("captured_at", "ts", "timestamp", "cycle_time") if c in cols),
            None,
        )
        if not tcol:
            print(day, "no time col", cols[:15])
            continue
        rows = con.execute(
            f"SELECT {', '.join(want)} FROM {table} "
            f"WHERE substr({tcol},12,5)=? OR {tcol} LIKE ? "
            f"ORDER BY {tcol} LIMIT 3",
            (hhmm, f"%{hhmm}%"),
        ).fetchall()
        print(f"\n=== {day} {hhmm} {tag} db={db.name} table={table}")
        for r in rows:
            print(dict(r))


if __name__ == "__main__":
    main()
