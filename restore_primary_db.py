#!/usr/bin/env python3
# ============================================================================
#  restore_primary_db.py
#  Rebuild data/nifty_algo_v3.db from data/per_day/*.db shards.
# ============================================================================
#
#  WHY
#  ---
#  The live primary can become SQLite-corrupt (disk full, hard kill mid-WAL
#  write). Per-day shards from split_db_per_day.py remain intact. This tool
#  quarantines the broken primary and merges every usable shard into a fresh
#  file, then atomically replaces the primary after integrity_check passes.
#
#  USAGE
#  -----
#     python restore_primary_db.py
#     python restore_primary_db.py --shards data/per_day --dst data/nifty_algo_v3.db
#     python restore_primary_db.py --dry-run
#
# ============================================================================

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set, Tuple

BASE = Path(__file__).resolve().parent
DEFAULT_SHARDS = BASE / "data" / "per_day"
DEFAULT_DST = BASE / "data" / "nifty_algo_v3.db"

# Skip sqlite internals and noisy logs that are live-only / rebuildable.
SKIP_TABLES: Set[str] = {
    "sqlite_sequence",
    "api_call_log",
    "audit_log",
}


def open_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=60000")
    return con


def open_rw(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.execute("PRAGMA foreign_keys=OFF")  # merge order is not FK-strict
    return con


def table_list(con: sqlite3.Connection) -> List[str]:
    return [
        str(r[0])
        for r in con.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]


def columns(con: sqlite3.Connection, table: str) -> List[Tuple[str, str, int]]:
    """Return (name, type, pk) for each column."""
    return [
        (str(r[1]), str(r[2] or ""), int(r[5] or 0))
        for r in con.execute(f'PRAGMA table_info("{table}")')
    ]


def is_usable_shard(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    try:
        con = open_ro(path)
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM option_chain_snapshot"
            ).fetchone()
            return row is not None and int(row[0] or 0) > 0
        finally:
            con.close()
    except sqlite3.Error as exc:
        print(f"  skip {path.name}: {exc}")
        return False


def copy_schema(src: sqlite3.Connection, dst: sqlite3.Connection) -> List[str]:
    """Replay CREATE TABLE / INDEX from src into dst. Returns table names."""
    tables: List[str] = []
    indexes: List[str] = []
    for row in src.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND type IN ('table', 'index') "
        "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name"
    ):
        name = str(row["name"] or "")
        tbl = str(row["tbl_name"] or "")
        sql = str(row["sql"] or "")
        if name.startswith("sqlite_") or name in SKIP_TABLES or tbl in SKIP_TABLES:
            continue
        if row["type"] == "table":
            tables.append(name)
            dst.execute(sql)
        else:
            indexes.append(sql)
    for sql in indexes:
        try:
            dst.execute(sql)
        except sqlite3.Error:
            pass
    dst.commit()
    return tables


def insert_table(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    table: str,
) -> int:
    """Copy all rows; drop INTEGER PRIMARY KEY AUTOINCREMENT ids so they renumber."""
    src_cols = columns(src, table)
    dst_cols = {c[0] for c in columns(dst, table)}
    if not src_cols or not dst_cols:
        return 0

    # Omit single-column INTEGER PRIMARY KEY so AUTOINCREMENT reassigns.
    # TEXT PKs (e.g. positions.position_id) must be preserved.
    omit: Set[str] = set()
    pk_cols = [c for c in src_cols if c[2]]
    if len(pk_cols) == 1:
        name, ctype, _ = pk_cols[0]
        if "INT" in (ctype or "").upper():
            omit.add(name)

    use = [c[0] for c in src_cols if c[0] in dst_cols and c[0] not in omit]
    if not use:
        return 0

    col_sql = ", ".join(f'"{c}"' for c in use)
    placeholders = ", ".join("?" for _ in use)
    select_sql = f'SELECT {col_sql} FROM "{table}"'
    insert_sql = (
        f'INSERT OR IGNORE INTO "{table}" ({col_sql}) VALUES ({placeholders})'
    )

    cur = src.execute(select_sql)
    batch: List[tuple] = []
    n = 0
    while True:
        rows = cur.fetchmany(2000)
        if not rows:
            break
        for r in rows:
            batch.append(tuple(r[c] for c in use))
        if len(batch) >= 2000:
            dst.executemany(insert_sql, batch)
            n += len(batch)
            batch.clear()
    if batch:
        dst.executemany(insert_sql, batch)
        n += len(batch)
    dst.commit()
    return n


def integrity_ok(path: Path) -> Tuple[bool, str]:
    try:
        con = open_ro(path)
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
            msg = str(row[0] if row else "unknown")
            if msg != "ok":
                return False, msg[:500]
            n = con.execute(
                "SELECT COUNT(*) FROM option_chain_snapshot"
            ).fetchone()[0]
            days = con.execute(
                "SELECT COUNT(DISTINCT trading_date) FROM option_chain_snapshot"
            ).fetchone()[0]
            return True, f"ok chain_rows={n} days={days}"
        finally:
            con.close()
    except sqlite3.Error as exc:
        return False, str(exc)


def quarantine(path: Path) -> Optional[Path]:
    if not path.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = path.with_name(f"{path.name}.corrupt.{ts}")
    # Also move WAL/SHM companions so SQLite cannot reopen a half-state.
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix) if suffix else path
        if not p.exists():
            continue
        target = Path(str(dest) + suffix) if suffix else dest
        _force_replace(p, target)
        print(f"  quarantined {p.name} -> {target.name}")
    return dest


def _force_replace(src: Path, dst: Path, retries: int = 8) -> None:
    """Move/replace a file, retrying through transient Windows locks."""
    last: Optional[BaseException] = None
    for i in range(retries):
        try:
            if dst.exists():
                try:
                    dst.unlink()
                except OSError:
                    pass
            try:
                src.replace(dst)
                return
            except OSError:
                # Cross-volume or locked: copy then delete.
                shutil.copy2(str(src), str(dst))
                try:
                    src.unlink()
                except OSError:
                    # Source still locked — leave the copy as the target of
                    # record; caller may overwrite source on the next step.
                    pass
                return
        except BaseException as exc:
            last = exc
            time.sleep(0.5 * (i + 1))
    raise RuntimeError(f"Could not replace {src} -> {dst}: {last}")


def ensure_live_schema(dst_path: Path) -> None:
    """Create any live-only tables missing from shards via core.SCHEMA_SQL."""
    sys.path.insert(0, str(BASE))
    from core import Database  # noqa: WPS433

    db = Database(dst_path)
    db.close()


def restore(
    shard_dir: Path,
    dst: Path,
    *,
    dry_run: bool = False,
) -> int:
    shards = sorted(
        p for p in shard_dir.glob("*.db") if is_usable_shard(p)
    )
    if not shards:
        print(f"No usable shards under {shard_dir}")
        return 1

    print(f"Found {len(shards)} usable shard(s) in {shard_dir}")
    for p in shards:
        print(f"  + {p.name}")

    if dry_run:
        print("Dry run — no files written.")
        return 0

    building = dst.with_name(dst.name + f".building.{int(time.time())}")
    if building.exists():
        building.unlink()
    for suffix in ("-wal", "-shm"):
        side = Path(str(building) + suffix)
        if side.exists():
            side.unlink()

    print(f"\nBuilding {building.name} ...")
    schema_src = open_ro(shards[-1])  # newest shard usually has latest columns
    dst_con = open_rw(building)
    try:
        tables = copy_schema(schema_src, dst_con)
        print(f"  schema tables: {len(tables)}")
    finally:
        schema_src.close()

    # Prefer column-richest definition: ensure older shards' missing cols
    # still copy into newer schema via intersection in insert_table.
    totals = {t: 0 for t in tables}
    for shard in shards:
        print(f"\n  merging {shard.name} ...")
        src = open_ro(shard)
        try:
            for table in tables:
                if table not in table_list(src):
                    continue
                n = insert_table(src, dst_con, table)
                totals[table] = totals.get(table, 0) + n
                if n and table in (
                    "option_chain_snapshot", "intraday_candles", "positions"
                ):
                    print(f"    {table}: +{n:,}")
        finally:
            src.close()

    dst_con.execute("PRAGMA foreign_keys=ON")
    dst_con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    dst_con.close()

    print("\nVerifying build ...")
    ok, detail = integrity_ok(building)
    print(f"  integrity: {detail}")
    if not ok:
        print("BUILD FAILED integrity_check — leaving .building file for inspection")
        return 2

    print("\nQuarantining old primary (if any) ...")
    quarantine(dst)

    print(f"Installing {building.name} -> {dst.name}")
    _force_replace(building, dst)
    for suffix in ("-wal", "-shm"):
        side = Path(str(building) + suffix)
        if side.exists():
            _force_replace(side, Path(str(dst) + suffix))

    print("Ensuring live schema / migrations ...")
    ensure_live_schema(dst)

    ok2, detail2 = integrity_ok(dst)
    print(f"\nPrimary ready: {dst}")
    print(f"  {detail2}")
    print("  row totals (merged):")
    for t in (
        "option_chain_snapshot", "intraday_candles", "positions",
        "cycle_log", "strategy_decisions",
    ):
        if t in totals:
            print(f"    {t}: {totals[t]:,}")
    return 0 if ok2 else 3


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Rebuild nifty_algo_v3.db from per-day shards."
    )
    ap.add_argument(
        "--shards", type=Path, default=DEFAULT_SHARDS,
        help="directory of per-day .db files",
    )
    ap.add_argument(
        "--dst", type=Path, default=DEFAULT_DST,
        help="primary database path to restore",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="list shards only",
    )
    args = ap.parse_args(argv)
    return restore(args.shards, args.dst, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
