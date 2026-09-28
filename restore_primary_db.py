#!/usr/bin/env python3
# ============================================================================
#  restore_primary_db.py
#  Recover the live primary. Per-day shards are the last resort.
# ============================================================================
#
#  ORDER
#  -----
#  1. If main.py still has the file open — refuse. Never write the live file
#     while a writer exists.
#  2. Current nifty_algo_v3.db + -wal / -shm (SQLite applies WAL on open).
#     If integrity_check passes, this IS the live session. Do nothing.
#  3. Table-by-table salvage of that live file into a new DB (readable
#     pages survive a torn btree). If the salvage verifies, install it.
#  4. Newest data/nifty_algo_v3.db.corrupt.* quarantine (+ its WAL/SHM).
#  5. Only then merge data/per_day/*.db, overlaying any salvaged *today*
#     rows on top so the live session is not replaced by a stale split.
#
#  USAGE
#  -----
#     python restore_primary_db.py
#     python restore_primary_db.py --dry-run
#     python restore_primary_db.py --force-shards   # skip live/WAL/corrupt
#
# ============================================================================

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

BASE = Path(__file__).resolve().parent
DEFAULT_SHARDS = BASE / "data" / "per_day"
DEFAULT_DST = BASE / "data" / "nifty_algo_v3.db"
IST = timezone(timedelta(hours=5, minutes=30))

# Skip sqlite internals and noisy logs that are live-only / rebuildable.
SKIP_TABLES: Set[str] = {
    "sqlite_sequence",
    "api_call_log",
    "audit_log",
}

DATE_TABLES: List[Tuple[str, str]] = [
    ("session_state", "trading_date"),
    ("positions", "trading_date"),
    ("intraday_candles", "trading_date"),
    ("option_chain_snapshot", "trading_date"),
    ("cycle_log", "trading_date"),
    ("trade_entries", "trading_date"),
    ("daily_summary", "trading_date"),
    ("strategy_decisions", "trading_date"),
    ("phantom_trades", "trading_date"),
    ("regime_accuracy_scores", "trading_date"),
    ("exit_quality_log", "trading_date"),
    ("risk_halt", "trading_date"),
    ("options_chain", "date"),
    ("vix_history", "date"),
    ("market_snapshots", "date"),
    ("regime_decisions", "date"),
]

FK_TABLES: List[Tuple[str, str]] = [
    ("position_legs", "position_id"),
    ("trade_exits", "position_id"),
    ("order_dispatch", "position_id"),
]


def _ro_uri(path: Path) -> str:
    """URI open that works on Windows (file:///C:/... not file:C:\\...)."""
    return f"{Path(path).resolve().as_uri()}?mode=ro"


def open_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(_ro_uri(Path(path)), uri=True, timeout=30)
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


def today_ist_str() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def remove_db_trio(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix) if suffix else path
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


def copy_db_trio(src: Path, dst: Path) -> None:
    """Copy db + wal + shm together. Drop dest sidecars if source has none."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        s = Path(str(src) + suffix) if suffix else src
        d = Path(str(dst) + suffix) if suffix else dst
        if s.exists():
            shutil.copy2(str(s), str(d))
        elif d.exists():
            try:
                d.unlink()
            except OSError:
                pass


def live_engine_running() -> bool:
    """True if a python process is running this repo's main.py."""
    try:
        import psutil  # type: ignore
    except ImportError:
        return False
    base = str(BASE.resolve()).replace("\\", "/").lower()
    for proc in psutil.process_iter(["cmdline", "cwd"]):
        try:
            cmd = " ".join(proc.info.get("cmdline") or []).replace("\\", "/").lower()
            cwd = str(proc.info.get("cwd") or "").replace("\\", "/").lower()
        except Exception:
            continue
        if "main.py" not in cmd:
            continue
        if base in cmd or base in cwd:
            return True
    return False


def is_locked_by_writer(path: Path) -> bool:
    """True when another process holds a SQLite write lock on the primary."""
    if not path.exists():
        return False
    try:
        con = sqlite3.connect(str(path), timeout=0.25)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.rollback()
        finally:
            con.close()
        return False
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        return "locked" in msg or "busy" in msg
    except sqlite3.Error:
        return False


def verify_with_wal(path: Path) -> Tuple[bool, str]:
    """Read-only open so SQLite sees WAL without rewriting the main file."""
    if not path.exists():
        return False, "missing"
    try:
        con = sqlite3.connect(_ro_uri(path), uri=True, timeout=30)
        try:
            con.execute("PRAGMA busy_timeout=60000")
            row = con.execute("PRAGMA integrity_check").fetchone()
            msg = str(row[0] if row else "unknown")
            if msg != "ok":
                return False, msg[:500]
            n = 0
            days = 0
            try:
                n = int(con.execute(
                    "SELECT COUNT(*) FROM option_chain_snapshot"
                ).fetchone()[0] or 0)
                days = int(con.execute(
                    "SELECT COUNT(DISTINCT trading_date) "
                    "FROM option_chain_snapshot"
                ).fetchone()[0] or 0)
            except sqlite3.Error:
                pass
            return True, f"ok chain_rows={n} days={days}"
        finally:
            con.close()
    except sqlite3.Error as exc:
        return False, str(exc)


def _chain_rows_from_detail(detail: str) -> int:
    try:
        if "chain_rows=" not in detail:
            return 0
        return int(detail.split("chain_rows=", 1)[1].split()[0].replace(",", ""))
    except (IndexError, ValueError):
        return 0


def salvage_worth_installing(
    verified: bool,
    detail: str,
    today: Dict[str, int],
) -> bool:
    """Do not replace the primary with an empty schema-only salvage."""
    if not verified:
        return False
    if int(today.get("chain") or 0) > 0:
        return True
    if int(today.get("positions") or 0) > 0 and _chain_rows_from_detail(detail) > 0:
        return True
    return _chain_rows_from_detail(detail) >= 1000


def today_stats(path: Path, today: str) -> Dict[str, int]:
    out = {"chain": 0, "candles": 0, "positions": 0, "session": 0}
    if not path.exists():
        return out
    try:
        con = sqlite3.connect(_ro_uri(path), uri=True, timeout=15)
    except sqlite3.Error:
        return out
    try:
        def _n(sql: str) -> int:
            try:
                row = con.execute(sql, (today,)).fetchone()
                return int(row[0] or 0) if row else 0
            except sqlite3.Error:
                return 0

        out["chain"] = _n(
            "SELECT COUNT(*) FROM option_chain_snapshot "
            "WHERE substr(trading_date,1,10)=?"
        )
        out["candles"] = _n(
            "SELECT COUNT(*) FROM intraday_candles "
            "WHERE substr(trading_date,1,10)=?"
        )
        out["positions"] = _n(
            "SELECT COUNT(*) FROM positions "
            "WHERE substr(trading_date,1,10)=?"
        )
        out["session"] = _n(
            "SELECT COUNT(*) FROM session_state "
            "WHERE substr(trading_date,1,10)=?"
        )
        return out
    finally:
        con.close()


def has_live_today(stats: Dict[str, int]) -> bool:
    return int(stats.get("chain") or 0) > 0 or int(stats.get("session") or 0) > 0


def list_quarantine_copies(dst: Path) -> List[Path]:
    found: List[Path] = []
    for p in dst.parent.glob(dst.name + ".corrupt.*"):
        if p.name.endswith("-wal") or p.name.endswith("-shm"):
            continue
        if p.is_file():
            found.append(p)
    found.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return found


def salvage_to(
    src_path: Path,
    out_path: Path,
    schema_fallback: Optional[Path] = None,
) -> Dict[str, int]:
    """Copy every table that still SELECTs into a new file. Never writes src."""
    remove_db_trio(out_path)
    totals: Dict[str, int] = {}
    src = sqlite3.connect(str(src_path), timeout=30)
    src.row_factory = sqlite3.Row
    dst = open_rw(out_path)
    try:
        tables: List[str] = []
        try:
            tables = copy_schema(src, dst)
        except sqlite3.Error as exc:
            print(f"  schema from damaged source failed ({exc})")
            if schema_fallback and schema_fallback.exists():
                sch = open_ro(schema_fallback)
                try:
                    tables = copy_schema(sch, dst)
                finally:
                    sch.close()
        if not tables:
            return totals
        for table in tables:
            try:
                n = insert_table(src, dst, table)
                totals[table] = n
                if n:
                    print(f"    salvage {table}: {n:,}")
            except sqlite3.Error as exc:
                print(f"    salvage {table}: FAILED ({exc})")
                totals[table] = 0
        dst.commit()
        return totals
    finally:
        try:
            src.close()
        except Exception:
            pass
        try:
            dst.close()
        except Exception:
            pass


def overlay_today(
    dst_con: sqlite3.Connection,
    salvage_path: Path,
    today: str,
) -> Dict[str, int]:
    """Replace *today's* rows in dst with rows from a salvaged live file."""
    copied: Dict[str, int] = {}
    dst_con.execute(
        "ATTACH DATABASE ? AS sal",
        (str(Path(salvage_path).resolve()),),
    )
    try:
        dst_names = set(table_list(dst_con))
        sal_names = {
            str(r[0])
            for r in dst_con.execute(
                "SELECT name FROM sal.sqlite_master WHERE type='table'"
            )
        }
        pos_ids: List[str] = []
        if "positions" in sal_names:
            try:
                pos_ids = [
                    str(r[0])
                    for r in dst_con.execute(
                        "SELECT position_id FROM sal.positions "
                        "WHERE substr(trading_date,1,10)=?",
                        (today,),
                    )
                    if r[0]
                ]
            except sqlite3.Error:
                pos_ids = []

        dst_pos: List[str] = []
        if "positions" in dst_names:
            try:
                dst_pos = [
                    str(r[0])
                    for r in dst_con.execute(
                        "SELECT position_id FROM positions "
                        "WHERE substr(trading_date,1,10)=?",
                        (today,),
                    )
                    if r[0]
                ]
            except sqlite3.Error:
                dst_pos = []

        drop_ids = list(dict.fromkeys([*pos_ids, *dst_pos]))
        if drop_ids and "positions" in dst_names:
            q = ",".join("?" for _ in drop_ids)
            for table, key_col in FK_TABLES:
                if table not in dst_names:
                    continue
                try:
                    dst_con.execute(
                        f'DELETE FROM "{table}" WHERE "{key_col}" IN ({q})',
                        tuple(drop_ids),
                    )
                except sqlite3.Error:
                    pass

        for table, col in DATE_TABLES:
            if table not in dst_names or table not in sal_names:
                continue
            try:
                dst_cols = {c[0] for c in columns(dst_con, table)}
                sal_cols = [
                    str(r[1])
                    for r in dst_con.execute(f'PRAGMA sal.table_info("{table}")')
                ]
                use = [c for c in sal_cols if c in dst_cols]
                if not use:
                    continue
                colq = ", ".join(f'"{c}"' for c in use)
                dst_con.execute(
                    f'DELETE FROM "{table}" WHERE substr("{col}",1,10)=?',
                    (today,),
                )
                dst_con.execute(
                    f'INSERT OR IGNORE INTO "{table}" ({colq}) '
                    f'SELECT {colq} FROM sal."{table}" '
                    f'WHERE substr("{col}",1,10)=?',
                    (today,),
                )
                copied[table] = int(
                    dst_con.execute("SELECT changes()").fetchone()[0] or 0
                )
            except sqlite3.Error as exc:
                print(f"    overlay {table}: FAILED ({exc})")

        if pos_ids:
            q = ",".join("?" for _ in pos_ids)
            for table, key_col in FK_TABLES:
                if table not in dst_names or table not in sal_names:
                    continue
                try:
                    dst_cols = {c[0] for c in columns(dst_con, table)}
                    sal_cols = [
                        str(r[1])
                        for r in dst_con.execute(
                            f'PRAGMA sal.table_info("{table}")'
                        )
                    ]
                    use = [c for c in sal_cols if c in dst_cols]
                    if not use:
                        continue
                    colq = ", ".join(f'"{c}"' for c in use)
                    dst_con.execute(
                        f'INSERT OR IGNORE INTO "{table}" ({colq}) '
                        f'SELECT {colq} FROM sal."{table}" '
                        f'WHERE "{key_col}" IN ({q})',
                        tuple(pos_ids),
                    )
                    copied[table] = int(
                        dst_con.execute("SELECT changes()").fetchone()[0] or 0
                    )
                except sqlite3.Error as exc:
                    print(f"    overlay {table}: FAILED ({exc})")
        dst_con.commit()
        return copied
    finally:
        try:
            dst_con.execute("DETACH DATABASE sal")
        except sqlite3.Error:
            pass


def install_replacement(src: Path, dst: Path, *, reason: str) -> int:
    """Quarantine dst (if any) and move src into place. Src must already verify."""
    if dst.exists() and is_locked_by_writer(dst):
        print(
            "REFUSE: live engine still has the database open. "
            "Stop main.py first."
        )
        return 4
    print(f"\nInstalling {reason}")
    print(f"  from {src.name} -> {dst.name}")
    if dst.exists():
        quarantine(dst)
    _force_replace(src, dst)
    for suffix in ("-wal", "-shm"):
        s = Path(str(src) + suffix)
        d = Path(str(dst) + suffix)
        if s.exists():
            _force_replace(s, d)
        elif d.exists():
            try:
                d.unlink()
            except OSError:
                pass
    print("Ensuring live schema / migrations ...")
    ensure_live_schema(dst)
    ok, detail = verify_with_wal(dst)
    print(f"  {detail}")
    st = today_stats(dst, today_ist_str())
    print(
        f"  today: chain={st['chain']:,} candles={st['candles']:,} "
        f"positions={st['positions']:,} session={st['session']:,}"
    )
    return 0 if ok else 3


def merge_shards_to(building: Path, shards: List[Path]) -> Dict[str, int]:
    remove_db_trio(building)
    print(f"\nBuilding shard merge {building.name} ...")
    schema_src = open_ro(shards[-1])
    dst_con = open_rw(building)
    try:
        tables = copy_schema(schema_src, dst_con)
        print(f"  schema tables: {len(tables)}")
    finally:
        schema_src.close()

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
    dst_con.commit()
    dst_con.close()
    return totals


def restore(
    shard_dir: Path,
    dst: Path,
    *,
    dry_run: bool = False,
    force_shards: bool = False,
) -> int:
    dst = Path(dst)
    shard_dir = Path(shard_dir)
    today = today_ist_str()
    shards = sorted(
        p for p in shard_dir.glob("*.db") if is_usable_shard(p)
    )
    quarantines = list_quarantine_copies(dst)

    print(f"Restore target : {dst}")
    print(f"Today (IST)    : {today}")
    print(f"Quarantines    : {len(quarantines)}")
    for p in quarantines[:8]:
        print(f"  + {p.name}")
    print(f"Shards         : {len(shards)} under {shard_dir}")
    for p in shards:
        print(f"  + {p.name}")

    engine_up = live_engine_running()
    if engine_up and not dry_run:
        print(
            "\nREFUSE: main.py is still running. Stop it first. Restore will "
            "not touch the live file while the engine is up."
        )
        return 4
    if engine_up and dry_run:
        print(
            "\nNOTE: main.py is running. Dry-run is read-only; it will not "
            "replace the file. A real restore would refuse until the engine "
            "is stopped."
        )
    if not dry_run and dst.exists() and is_locked_by_writer(dst):
        print(
            "\nREFUSE: another process has a write lock on the live database. "
            "Stop it first."
        )
        return 4

    if dry_run:
        if not force_shards and dst.exists():
            ok, detail = verify_with_wal(dst)
            st = today_stats(dst, today)
            print(f"\nCurrent primary: {detail}  today={st}")
            if ok:
                print("Dry run — would keep the current live session (no write).")
                return 0
        print("Dry run — no files written.")
        return 0

    salvage_overlay: Optional[Path] = None

    if not force_shards and dst.exists():
        ok, detail = verify_with_wal(dst)
        st = today_stats(dst, today)
        print(f"\n[1] Current primary + WAL: {detail}")
        print(f"    today={st}")
        if ok:
            print(
                "Live session is intact (WAL applied, integrity_check ok). "
                "Nothing to replace."
            )
            return 0

        print("\n[2] Salvaging readable tables from the live file ...")
        salvage_path = dst.with_name(f"{dst.name}.salvage.{os.getpid()}")
        schema_fb = shards[-1] if shards else None
        try:
            salvage_to(dst, salvage_path, schema_fb)
            s_ok, s_detail = verify_with_wal(salvage_path)
            s_st = today_stats(salvage_path, today)
            print(f"    salvage verify: {s_detail}  today={s_st}")
            if salvage_worth_installing(s_ok, s_detail, s_st):
                return install_replacement(
                    salvage_path, dst,
                    reason="salvaged live session (WAL applied)",
                )
            if salvage_path.exists():
                salvage_overlay = salvage_path
        except sqlite3.Error as exc:
            print(f"    salvage failed: {exc}")

        print("\n[3] Quarantined live copies (.corrupt.*) ...")
        for cand in quarantines:
            print(f"    trying {cand.name}")
            c_ok, c_detail = verify_with_wal(cand)
            print(f"      {c_detail}")
            if c_ok:
                staged = dst.with_name(
                    f"{dst.name}.from_corrupt.{os.getpid()}"
                )
                copy_db_trio(cand, staged)
                st_ok, _st_detail = verify_with_wal(staged)
                if st_ok:
                    return install_replacement(
                        staged, dst,
                        reason=f"quarantined live copy {cand.name}",
                    )
                remove_db_trio(staged)
            staged_s = dst.with_name(
                f"{dst.name}.salvage_corrupt.{os.getpid()}"
            )
            try:
                salvage_to(cand, staged_s, shards[-1] if shards else None)
                sc_ok, sc_detail = verify_with_wal(staged_s)
                sc_st = today_stats(staged_s, today)
                print(f"      salvage {sc_detail} today={sc_st}")
                if salvage_worth_installing(sc_ok, sc_detail, sc_st):
                    return install_replacement(
                        staged_s, dst,
                        reason=f"salvaged quarantine {cand.name}",
                    )
            except sqlite3.Error as exc:
                print(f"      salvage failed: {exc}")
            remove_db_trio(staged_s)

    print("\n[4] Per-day shards (stale vs live unless overlay succeeds) ...")
    if not shards:
        print("No usable shards. Cannot rebuild history.")
        return 1

    if salvage_overlay is None and not force_shards and dst.exists():
        candidate = dst.with_name(f"{dst.name}.salvage.{os.getpid()}")
        if candidate.exists():
            salvage_overlay = candidate
        else:
            print("  Re-salvaging live file for today's overlay ...")
            try:
                salvage_to(dst, candidate, shards[-1])
                salvage_overlay = candidate
            except sqlite3.Error as exc:
                print(f"  overlay salvage failed: {exc}")

    building = dst.with_name(dst.name + f".building.{int(time.time())}")
    totals = merge_shards_to(building, shards)

    if salvage_overlay and salvage_overlay.exists():
        print(f"\n  Overlaying today's live rows from {salvage_overlay.name} ...")
        dst_con = open_rw(building)
        try:
            copied = overlay_today(dst_con, salvage_overlay, today)
            for k, v in copied.items():
                if v:
                    print(f"    {k}: {v:,} rows")
        finally:
            dst_con.close()

    ok, detail = verify_with_wal(building)
    print(f"\nShard build integrity: {detail}")
    if not ok:
        print("BUILD FAILED integrity_check — leaving .building file")
        return 2

    used_overlay = bool(salvage_overlay and salvage_overlay.exists())
    rc = install_replacement(
        building, dst,
        reason=(
            "per-day shards + today's live overlay"
            if used_overlay
            else "per-day shards (STALE — no live/WAL/corrupt session recovered)"
        ),
    )
    print("  row totals (shard merge):")
    for t in (
        "option_chain_snapshot", "intraday_candles", "positions",
        "cycle_log", "strategy_decisions",
    ):
        if t in totals:
            print(f"    {t}: {totals[t]:,}")
    if salvage_overlay:
        remove_db_trio(salvage_overlay)
    return rc


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Recover nifty_algo_v3.db from live WAL / quarantined copy first; "
            "per-day shards only as last resort."
        )
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
        help="inspect live/WAL/quarantine/shards; write nothing",
    )
    ap.add_argument(
        "--force-shards", action="store_true",
        help="skip live/WAL/corrupt recovery and merge shards only",
    )
    args = ap.parse_args(argv)
    return restore(
        args.shards, args.dst,
        dry_run=args.dry_run,
        force_shards=args.force_shards,
    )


if __name__ == "__main__":
    raise SystemExit(main())
