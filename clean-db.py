#!/usr/bin/env python3
"""
Delete rows recorded outside market hours (09:15-15:30 IST) from the engine
database.

Dry run by default. Nothing is written unless you pass --apply, and --apply
takes a full copy of the file first.

    python3 clean_db_market_hours.py                  # show what would go
    python3 clean_db_market_hours.py --apply          # do it
    python3 clean_db_market_hours.py --apply --include-logs

Two timestamp formats live in this database - ISO like
'2026-09-08T09:15:23.220199+05:30' and bare '09:15:00' - so the hour and
minute are pulled from either. Rows whose timestamp is NULL are always kept:
an unknown time is not evidence of a bad time.
"""

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# recorded market data
MARKET = [
    ("option_chain_snapshot", "capture_time"),
    ("cycle_log",             "cycle_time"),
    ("intraday_candles",      "candle_time"),
    ("market_snapshots",      "timestamp"),
    ("options_chain",         "timestamp"),
    ("regime_decisions",      "timestamp"),
    ("vix_history",           "timestamp"),
    ("strategy_decisions",    "decision_time"),
    ("phantom_trades",        "block_time"),
    ("exit_quality_log",      "exit_time"),
]

# operational logs - your audit trail, so only with --include-logs
LOGS = [
    ("api_call_log", "call_time"),
    ("audit_log",    "log_time"),
]


def hhmm(col: str) -> str:
    """HH:MM out of either an ISO timestamp or a bare clock time."""
    return (f"CASE WHEN instr({col}, 'T') > 0 "
            f"THEN substr({col}, instr({col}, 'T') + 1, 5) "
            f"ELSE substr({col}, 1, 5) END")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/nifty_algo_v3.db")
    ap.add_argument("--open", dest="mkt_open", default="09:15")
    ap.add_argument("--close", dest="mkt_close", default="15:30")
    ap.add_argument("--apply", action="store_true", help="actually delete")
    ap.add_argument("--include-logs", action="store_true",
                    help="also prune api_call_log and audit_log")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"no such database: {db}")
        return 1

    targets = MARKET + (LOGS if args.include_logs else [])
    con = sqlite3.connect(db)
    present = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    print(f"\n  {db}   keeping {args.mkt_open}-{args.mkt_close} IST"
          f"{'' if args.apply else '   (DRY RUN)'}\n")
    print(f"  {'table':24}{'rows':>9}{'outside':>9}{'null ts':>9}   keep")
    print("  " + "-" * 62)

    plan, total_out = [], 0
    for table, col in targets:
        if table not in present:
            continue
        cols = {r[1] for r in con.execute(f"PRAGMA table_info('{table}')")}
        if col not in cols:
            print(f"  {table:24}{'-':>9}{'-':>9}{'-':>9}   no '{col}' column")
            continue
        rows = con.execute(f"SELECT COUNT(*) FROM '{table}'").fetchone()[0]
        where = (f"{col} IS NOT NULL AND "
                 f"{hhmm(col)} NOT BETWEEN ? AND ?")
        out = con.execute(
            f"SELECT COUNT(*) FROM '{table}' WHERE {where}",
            (args.mkt_open, args.mkt_close)).fetchone()[0]
        nulls = con.execute(
            f"SELECT COUNT(*) FROM '{table}' WHERE {col} IS NULL").fetchone()[0]
        print(f"  {table:24}{rows:>9}{out:>9}{nulls:>9}   {rows - out}")
        if out:
            plan.append((table, where, out))
            total_out += out

    print("  " + "-" * 62)
    print(f"  {'total to delete':24}{total_out:>9}\n")

    if not args.include_logs:
        print("  api_call_log and audit_log left alone (--include-logs to prune)\n")

    if not total_out:
        print("  nothing to do.\n")
        return 0

    if not args.apply:
        print("  dry run - nothing written. Re-run with --apply.\n")
        return 0

    if not args.no_backup:
        bak = db.with_name(
            f"{db.stem}.{datetime.now():%Y%m%d_%H%M%S}.bak{db.suffix}")
        shutil.copy2(db, bak)
        print(f"  backup: {bak.name}  ({bak.stat().st_size / 1e6:.1f} MB)")

    before = db.stat().st_size
    with con:
        for table, where, _ in plan:
            con.execute(f"DELETE FROM '{table}' WHERE {where}",
                        (args.mkt_open, args.mkt_close))
    print(f"  deleted {total_out} row(s); vacuuming...")
    con.execute("VACUUM")
    con.close()
    after = db.stat().st_size
    print(f"  {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())