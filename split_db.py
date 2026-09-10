#!/usr/bin/env python3
# ============================================================================
#  split_db.py
#  Split nifty_algo_v3.db into one small, self-contained SQLite file per date.
# ============================================================================
#
#  WHY THIS EXISTS
#  ---------------
#  data/nifty_algo_v3.db is ~117 MB and stored via git-lfs, which makes it
#  hard to move around (and the LFS pointer alone is useless). Most of that
#  bulk is the option_chain_snapshot table: a full option chain written every
#  ~45 seconds across many sessions. A single session is only a few MB.
#
#  This script walks every table that carries a trading date, and for each
#  distinct date found in the source database it writes a fresh, fully-schema'd
#  SQLite file (data/per_day/nifty_algo_<date>.db) containing ONLY that day's
#  rows. Each output file is a complete database - the backtest harness and
#  the live engine can open it directly - but it is small enough to commit as
#  an ordinary (non-LFS) file.
#
#  USAGE
#  -----
#     python split_db.py                          # shard the default source DB
#     python split_db.py --src /path/to/big.db --out data/per_day
#     python split_db.py --dates 2026-09-08 2026-09-09   # only these days
#
#  Then run the backtester against one day:
#     python backtest_engine.py --db data/per_day/nifty_algo_2026-09-08.db \
#          --from 2026-09-08 --to 2026-09-08
#
# ============================================================================

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Set

# ─────────────────────────────────────────────────────────────────────────────
#  TABLE MAP
# ─────────────────────────────────────────────────────────────────────────────
#  Every table is placed in one of three buckets:
#
#    BY_DATE[date_col]  - rows are filtered by that column = the target date.
#    BY_FK              - rows carry no date but reference a parent table that
#                         does (position_legs -> positions, trade_exits ->
#                         trade_entries). Copied via the parent's keys.
#    GLOBAL             - small tables that are not date-scoped and are copied
#                         whole (calibration_state, expiry_results).
#
#  api_call_log and audit_log are deliberately NOT copied: they are pure
#  diagnostics, can be large, and the backtester/engine never read them.
# ─────────────────────────────────────────────────────────────────────────────

# (table, date_column)
BY_DATE: List[tuple] = [
    ("session_state",          "trading_date"),
    ("positions",              "trading_date"),
    ("intraday_candles",       "trading_date"),
    ("option_chain_snapshot",  "trading_date"),
    ("cycle_log",              "trading_date"),
    ("trade_entries",          "trading_date"),
    ("daily_summary",          "trading_date"),
    ("strategy_decisions",     "trading_date"),
    ("phantom_trades",         "trading_date"),
    ("regime_accuracy_scores", "trading_date"),
    ("exit_quality_log",       "trading_date"),
    # these tables use a plain `date` column instead of `trading_date`
    ("options_chain",          "date"),
    ("vix_history",            "date"),
    ("market_snapshots",       "date"),
    ("regime_decisions",       "date"),
]

# (table, key_column, parent_table, parent_key_column)
BY_FK: List[tuple] = [
    ("position_legs", "position_id", "positions",     "position_id"),
    ("trade_exits",   "position_id", "positions",     "position_id"),
]

# tables copied whole (small bookkeeping tables)
GLOBAL: List[str] = [
    "calibration_state",
    "expiry_results",
]

SKIP: List[str] = [
    "api_call_log",
    "audit_log",
]


def open_db(path: str, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def discover_dates(src: sqlite3.Connection) -> List[str]:
    """Union of every distinct date across all BY_DATE tables."""
    dates: Set[str] = set()
    for table, col in BY_DATE:
        try:
            rows = src.execute(
                f'SELECT DISTINCT "{col}" AS d FROM "{table}" '
                f'WHERE "{col}" IS NOT NULL'
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        for r in rows:
            d = str(r["d"])
            # normalise timestamps that may be stored with a time component
            if len(d) >= 10:
                dates.add(d[:10])
    return sorted(dates)


def schema_sql(src: sqlite3.Connection) -> List[str]:
    """All CREATE TABLE / CREATE INDEX statements, in dependency order."""
    out: List[str] = []
    for row in src.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND type IN ('table','index') "
        "ORDER BY (type != 'table'), name"
    ):
        if row["type"] == "table" and row["name"].startswith("sqlite_"):
            continue
        if row["type"] == "table" and row["name"] in SKIP:
            continue
        if row["type"] == "index":
            # skip indexes on tables we are not copying rows for, so the
            # target build never references a missing table
            try:
                tbl = src.execute(
                    "SELECT tbl_name FROM sqlite_master "
                    "WHERE type='index' AND name=?",
                    (row["name"],),
                ).fetchone()["tbl_name"]
            except Exception:
                continue
            if tbl in SKIP:
                continue
        out.append(row["sql"])
    return out


def copy_table(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    table: str,
    where_sql: str = "",
    params: tuple = (),
) -> int:
    """Copy rows of `table` from src to dst, optionally filtered."""
    cols = [c[1] for c in src.execute(f'PRAGMA table_info("{table}")').fetchall()]
    if not cols:
        return 0
    colq = ", ".join(f'"{c}"' for c in cols)
    ph = ", ".join("?" for _ in cols)
    sql = f'SELECT {colq} FROM "{table}"{where_sql}'
    rows = src.execute(sql, params).fetchall()
    if not rows:
        return 0
    dst.executemany(f'INSERT INTO "{table}" ({colq}) VALUES ({ph})',
                    [tuple(r) for r in rows])
    return len(rows)


def build_day(src: sqlite3.Connection, out_path: Path, date: str) -> dict:
    """Create one per-day database and copy that day's rows into it."""
    if out_path.exists():
        out_path.unlink()
    dst = sqlite3.connect(str(out_path))

    with dst:
        for stmt in schema_sql(src):
            dst.execute(stmt)

        counts: Dict[str, int] = {}

        # 1. date-scoped tables
        for table, col in BY_DATE:
            n = copy_table(dst=dst, src=src, table=table,
                           where_sql=f' WHERE "{col}" = ?', params=(date,))
            counts[table] = n

        # 2. foreign-key tables (position_legs, trade_exits) - copy rows whose
        #    parent key belongs to this day
        for table, key_col, parent, parent_key in BY_FK:
            keys = [
                r[0] for r in dst.execute(
                    f'SELECT DISTINCT "{parent_key}" FROM "{parent}"'
                ).fetchall()
            ]
            if not keys:
                counts[table] = 0
                continue
            n = 0
            # chunk to avoid an oversized IN (...) clause
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                q = ",".join("?" for _ in chunk)
                n += copy_table(
                    dst=dst, src=src, table=table,
                    where_sql=f' WHERE "{key_col}" IN ({q})',
                    params=tuple(chunk),
                )
            counts[table] = n

        # 3. global bookkeeping tables (whole)
        for table in GLOBAL:
            counts[table] = copy_table(dst=dst, src=src, table=table)

    dst.close()

    # Compact the file (reclaims any free pages left by the big insert) and
    # sanity-check it opens cleanly.
    check = sqlite3.connect(str(out_path))
    check.execute("VACUUM")
    integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
    check.close()
    counts["__integrity__"] = integrity
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Split nifty_algo_v3.db into one small DB per trading date."
    )
    ap.add_argument("--src", default="data/nifty_algo_v3.db",
                    help="source database (default: data/nifty_algo_v3.db)")
    ap.add_argument("--out", default="data/per_day",
                    help="output directory (default: data/per_day)")
    ap.add_argument("--dates", nargs="*", default=None,
                    help="optional list of YYYY-MM-DD dates to shard "
                         "(default: every date found)")
    args = ap.parse_args()

    src_path = Path(args.src)
    if not src_path.exists():
        print(f"\n  No source database at {src_path}\n", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    src = open_db(str(src_path), readonly=True)
    try:
        dates = discover_dates(src)
        if args.dates:
            dates = [d for d in dates if d in set(args.dates)]
        if not dates:
            print("\n  No dates found in the source database.\n")
            return 1

        print(f"\n  Source : {src_path}")
        print(f"  Output : {out_dir}/")
        print(f"  Dates  : {len(dates)}  ({dates[0]} .. {dates[-1]})\n")

        total = 0
        for d in dates:
            out_path = out_dir / f"nifty_algo_{d}.db"
            counts = build_day(src, out_path, d)
            rows = sum(v for k, v in counts.items() if not k.startswith("__"))
            size_kb = out_path.stat().st_size / 1024
            total += out_path.stat().st_size
            status = "OK" if counts.get("__integrity__") == "ok" else \
                f"INTEGRITY={counts.get('__integrity__')}"
            print(f"  {d}: {rows:>10,} rows  {size_kb:>10,.1f} KB  {status}")

        print(f"\n  Done. {len(dates)} file(s), "
              f"{total / (1024 * 1024):,.1f} MB total.\n")
    finally:
        src.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())