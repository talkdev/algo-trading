#!/usr/bin/env python3
# ============================================================================
#  split_db_per_day.py
#  Split nifty_algo_v3.db into one self-contained SQLite file per session.
# ============================================================================
#
#  WHY THIS EXISTS
#  ---------------
#  The live database is large (mostly option_chain_snapshot). The backtest
#  harness can point at a directory of per-day files and replay each session
#  in parallel. Those shards must contain EVERYTHING HistoricalStore / the
#  replay client need so a per-day run matches a run against the live DB:
#
#    * option_chain_snapshot for the session (all expiries, market hours)
#    * intraday_candles for the session (1-minute bars)
#    * the previous session's LAST 1-minute close (gap detection)
#    * session_state.prev_day_vix_close (Gate 1 + momentum_vix_gap)
#    * session_state and the other date-scoped tables for forensics / parity
#
#  After every split each day is printed FIDELITY PASS / WARN / FAIL.
#  Missing yesterday's VIX is FAIL and the process exits 2 by default.
#  python split_db_per_day.py --allow-unfaithful   # only to ignore that
#
#  IDEMPOTENCE
#  -----------
#  An existing per-day file is NOT deleted when its content fingerprint
#  already matches what a fresh split would write (row counts + checksums of
#  the BT-critical tables, including the previous-session context candle).
#  Use --force to rebuild anyway.
#
#  USAGE
#  -----
#     python split_db_per_day.py
#     python split_db_per_day.py --src data/nifty_algo_v3.db --out data/per_day
#     python split_db_per_day.py --dates 2026-09-21 2026-09-22
#     python split_db_per_day.py --force          # rebuild even if unchanged
#     python split_db_per_day.py --verify-only    # fingerprint compare, no write
#     python split_db_per_day.py --workers 8      # parallel day rebuilds (default 8)
#     python split_db_per_day.py --vacuum         # optional VACUUM on new shards
#
#     python backtest_engine.py --db data/per_day --from 2026-09-08 --to 2026-09-22
#
#  LIVE-PRIMARY SAFETY
#  -------------------
#  Sequence (do not invert):
#    1. main.py ends the session, WAL-checkpoints RESTART, closes, drops
#       the writer lock.
#    2. This script refuses to start while nifty_algo_v3.db.writer.lock
#       is held (--allow-live to snapshot a running engine anyway).
#    3. A read-only work copy is used for all reads:
#         • large WAL or --allow-live → backup() snapshot (one copy, then
#           fast indexed reads; workers never share a live writer)
#         • small WAL + lock free → read primary in place (no multi-GB copy)
#       Source opens are query_only + NO_CKPT_ON_CLOSE; ATTACH uses mode=ro.
#       journal_mode=OFF applies only to the per-day temp shard. The live
#       file is never written.
#
#  Hook it after main.py in Misc/run_weekday_open.cmd, not inside the
#  trading process and not as a 15:40 Scheduled Task that can overlap EOD.
#
# ============================================================================

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from core import live_writer_lock_held, open_sqlite_readonly, protect_sqlite_connection

# Concurrent rebuilds of independent day shards (one process per day).
# Source is a read-only snapshot; each worker writes only its own temp→final
# path via atomic replace after quick_check / integrity_check.
DEFAULT_SPLIT_WORKERS = 8

# Above this many bytes of -wal, prefer backup() snapshot over in-place
# reads (N workers thrashing a multi-GB WAL is slower than one copy).
SNAPSHOT_WAL_THRESHOLD_BYTES = 256 * 1024 * 1024


# GitHub rejects individual files at 100,000,000 bytes (100 MB decimal).
# Large per-day shards are kept locally but automatically added to .gitignore.
GIT_IGNORE_SIZE_LIMIT_BYTES = 100_000_000
GIT_IGNORE_SECTION = "# Per-day shards currently over 100 MB"


def _find_repo_root(start: Path) -> Path:
    """Find the Git repository root, falling back to the script directory."""
    start = start.resolve()
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    for candidate in (start, *start.parents):
        if (candidate / ".gitignore").is_file():
            return candidate
    return start


def update_gitignore_for_large_shards(out_dir: Path) -> List[Tuple[str, int]]:
    """Add output .db shards >=100 MB to the repository .gitignore.

    Entries are root-anchored paths, for example:
        /data/per_day/nifty_algo_2026-10-21.db

    Existing entries are preserved and never duplicated. Files are not deleted
    or modified. Only .db files directly inside ``out_dir`` are considered.
    """
    repo_root = _find_repo_root(Path(__file__).resolve().parent)
    gitignore_path = repo_root / ".gitignore"
    out_dir = Path(out_dir).resolve()
    large_entries: List[Tuple[str, int]] = []

    if not out_dir.is_dir():
        return large_entries

    for shard in sorted(out_dir.glob("*.db")):
        try:
            size_bytes = shard.stat().st_size
        except OSError as exc:
            print(f"  .gitignore: could not inspect {shard.name}: {exc}", file=sys.stderr)
            continue

        if size_bytes < GIT_IGNORE_SIZE_LIMIT_BYTES:
            continue

        try:
            relative_path = shard.resolve().relative_to(repo_root.resolve())
        except ValueError:
            print(
                f"  .gitignore: skipped {shard} because it is outside the repository root",
                file=sys.stderr,
            )
            continue

        entry = "/" + relative_path.as_posix().lstrip("/")
        large_entries.append((entry, size_bytes))

    if not large_entries:
        return []

    try:
        existing_text = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
    except OSError as exc:
        print(f"  .gitignore: could not read {gitignore_path}: {exc}", file=sys.stderr)
        return []

    existing_lines = existing_text.splitlines()
    existing_entries = {line.strip() for line in existing_lines}
    missing = [(entry, size) for entry, size in large_entries if entry not in existing_entries]
    if not missing:
        return []

    # Append the section only if it is not already present. A trailing newline
    # is ensured so existing .gitignore content is not merged with a new entry.
    output = existing_text
    if output and not output.endswith(("\n", "\r")):
        output += "\n"
    if GIT_IGNORE_SECTION not in existing_lines:
        if output and output.strip():
            output += "\n"
        output += GIT_IGNORE_SECTION + "\n"

    for entry, _size in missing:
        output += entry + "\n"

    try:
        gitignore_path.write_text(output, encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"  .gitignore: could not update {gitignore_path}: {exc}", file=sys.stderr)
        return []

    return missing

# ─────────────────────────────────────────────────────────────────────────────
#  TABLE MAP
# ─────────────────────────────────────────────────────────────────────────────
#  BY_DATE  — filter WHERE date_col >= day AND date_col < next_day
#  BY_FK    — copy rows whose parent key landed in the day shard
#  GLOBAL   — optional whole-table copy (--include-global); not required for BT
#  SKIP     — never copied (diagnostics / auto)
#
#  Backtest behavioural parity only needs option_chain_snapshot +
#  intraday_candles (+ prev-session context). Everything else is forensics /
#  live-engine completeness so a shard is a faithful day slice of the live DB.
# ─────────────────────────────────────────────────────────────────────────────

BY_DATE: List[Tuple[str, str]] = [
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
    ("risk_halt",              "trading_date"),
    ("options_chain",          "date"),
    ("vix_history",            "date"),
    ("market_snapshots",       "date"),
    ("regime_decisions",       "date"),
]

BY_FK: List[Tuple[str, str, str, str]] = [
    ("position_legs",  "position_id", "positions", "position_id"),
    ("trade_exits",    "position_id", "positions", "position_id"),
    ("order_dispatch", "position_id", "positions", "position_id"),
]

GLOBAL: List[str] = [
    "calibration_state",
    "expiry_results",
]

SKIP: Set[str] = {
    "api_call_log",
    "audit_log",
    "sqlite_sequence",
}

# Tables whose content fingerprint must match for a skip decision.
# Counts alone are not enough: a same-count rewrite with different LTPs would
# change replay fills.
CRITICAL_FINGERPRINT = (
    "option_chain_snapshot",
    "intraday_candles",
)


def writer_lock_held(db_path: Path) -> bool:
    return live_writer_lock_held(db_path)


def _as_ro_uri(path: str | Path) -> str:
    """SQLite URI for a read-only attach/open. Forward slashes on Windows."""
    return f"file:{Path(path).resolve().as_posix()}?mode=ro"


def open_db(path: str | Path, readonly: bool = False) -> sqlite3.Connection:
    path = str(path)
    if not readonly and Path(path).name == "nifty_algo_v3.db":
        raise RuntimeError(
            "split_db_per_day refuses to write the live primary. "
            "It snapshots with a read-only backup and writes shards only."
        )
    if readonly:
        return open_sqlite_readonly(path, timeout=60)
    con = sqlite3.connect(path, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=60000")
    protect_sqlite_connection(con)
    return con


def remove_db_trio(path: Path) -> None:
    """Delete path and its -wal/-shm companions if present."""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix) if suffix else path
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


def snapshot_readonly(src_path: Path) -> Path:
    """Consistent copy of a (possibly live WAL) DB. Never writes the source."""
    src_path = Path(src_path).resolve()
    snap = src_path.with_name(f"{src_path.name}.split_snap.{os.getpid()}")
    remove_db_trio(snap)
    src = open_sqlite_readonly(src_path, timeout=60)
    try:
        dst = sqlite3.connect(str(snap), timeout=60)
        try:
            src.backup(dst)
        except BaseException:
            dst.close()
            remove_db_trio(snap)
            raise
        finally:
            try:
                dst.close()
            except Exception:
                pass
    finally:
        src.close()
    return snap


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def table_columns(con: sqlite3.Connection, table: str) -> List[str]:
    return [c[1] for c in con.execute(f'PRAGMA table_info("{table}")').fetchall()]


def date_range_params(date: str) -> Tuple[str, str]:
    """Inclusive start / exclusive next-day bound for date_predicate."""
    start = date[:10]
    nxt = (
        datetime.strptime(start, "%Y-%m-%d") + timedelta(days=1)
    ).strftime("%Y-%m-%d")
    return start, nxt


def date_predicate(col: str, alias: str = "") -> str:
    """Match YYYY-MM-DD (bare or timestamp) using the date index.

    Range bounds beat ``substr(col,1,10)=?``, which cannot use
    ``idx_chain_date`` / ``idx_candles_date`` and full-scans multi-GB
    ``option_chain_snapshot`` tables (minutes per day).
    """
    prefix = f'{alias}.' if alias else ""
    return f'{prefix}"{col}" >= ? AND {prefix}"{col}" < ?'


def discover_dates(src: sqlite3.Connection) -> List[str]:
    """Collect trading dates from small date-keyed tables only.

    Never DISTINCT-scan ``option_chain_snapshot`` — that alone can take
    a minute+ on a large primary and every date is already in
    ``session_state`` / candles when the day traded.
    """
    preferred: Sequence[Tuple[str, str]] = (
        ("session_state", "trading_date"),
        ("daily_summary", "trading_date"),
        ("intraday_candles", "trading_date"),
        ("positions", "trading_date"),
        ("vix_history", "date"),
    )
    dates: Set[str] = set()

    def _harvest(table: str, col: str) -> None:
        if not table_exists(src, table):
            return
        try:
            rows = src.execute(
                f'SELECT DISTINCT substr("{col}", 1, 10) AS d FROM "{table}" '
                f'WHERE "{col}" IS NOT NULL'
            ).fetchall()
        except sqlite3.Error:
            return
        for r in rows:
            d = str(r["d"] or "")
            if len(d) >= 10:
                dates.add(d[:10])

    for table, col in preferred:
        _harvest(table, col)
    if dates:
        return sorted(dates)

    # Fallback if preferred tables are empty/missing (exotic DBs).
    for table, col in BY_DATE:
        if table == "option_chain_snapshot":
            continue
        _harvest(table, col)
    return sorted(dates)


def schema_sql(
    src: sqlite3.Connection,
    *,
    include_global: bool,
) -> Tuple[List[str], List[str]]:
    """Return (CREATE TABLE stmts, CREATE INDEX stmts) for shard tables.

    Callers create tables, bulk-load rows, then create indexes — indexes
    built up-front make every INSERT maintain the btree and dominate
    day-shard build time.
    """
    keep: Set[str] = {t for t, _ in BY_DATE} | {t for t, *_ in BY_FK}
    if include_global:
        keep |= set(GLOBAL)
    keep -= SKIP

    tables: List[str] = []
    indexes: List[str] = []
    for row in src.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND type IN ('table', 'index') "
        "ORDER BY type DESC, name"
    ):
        name = row["name"]
        tbl = row["tbl_name"]
        if row["type"] == "table":
            if name.startswith("sqlite_") or name in SKIP or name not in keep:
                continue
            if not table_exists(src, name):
                continue
            tables.append(row["sql"])
        else:
            if tbl in SKIP or tbl not in keep:
                continue
            indexes.append(row["sql"])
    return tables, indexes


# ── fingerprints ────────────────────────────────────────────────────────────

def _scalar_checksum(
    con: sqlite3.Connection,
    table: str,
    where_sql: str,
    params: tuple,
    *,
    light: bool = False,
) -> Dict[str, Any]:
    """Compact content stamp: count + extrema (+ numeric sums unless light).

    ``light=True`` skips SUM(strike/ltp/…) so skip-if-unchanged can stamp a
    day without reading every chain row. Append-only live sessions change
    n / max_ct when new snapshots arrive; full sums still gate build_day.
    """
    if not table_exists(con, table):
        return {"n": 0}
    cols = set(table_columns(con, table))
    parts = ["COUNT(*) AS n"]
    if "capture_time" in cols:
        parts += [
            "MIN(capture_time) AS min_ct",
            "MAX(capture_time) AS max_ct",
        ]
    if "candle_time" in cols:
        parts += [
            "MIN(candle_time) AS min_ct",
            "MAX(candle_time) AS max_ct",
        ]
    if not light:
        if "strike" in cols:
            parts.append("COALESCE(SUM(CAST(strike AS REAL)), 0) AS sum_strike")
        if "ltp" in cols:
            parts.append("COALESCE(SUM(CAST(ltp AS REAL)), 0) AS sum_ltp")
        if "close" in cols:
            parts.append("COALESCE(SUM(CAST(close AS REAL)), 0) AS sum_close")
        if "bid" in cols:
            parts.append("COALESCE(SUM(CAST(bid AS REAL)), 0) AS sum_bid")
        if "ask" in cols:
            parts.append("COALESCE(SUM(CAST(ask AS REAL)), 0) AS sum_ask")
        if "spot_at_capture" in cols:
            parts.append(
                "COALESCE(SUM(CAST(spot_at_capture AS REAL)), 0) AS sum_spot"
            )
    sql = f'SELECT {", ".join(parts)} FROM "{table}"{where_sql}'
    try:
        row = con.execute(sql, params).fetchone()
    except sqlite3.Error:
        n = con.execute(
            f'SELECT COUNT(*) AS n FROM "{table}"{where_sql}', params
        ).fetchone()["n"]
        return {"n": int(n or 0)}
    out: Dict[str, Any] = {}
    for k in row.keys():
        v = row[k]
        if isinstance(v, float):
            out[k] = round(v, 6)
        else:
            out[k] = v
    out["n"] = int(out.get("n") or 0)
    return out


def _prev_context(con: sqlite3.Connection, date: str) -> Dict[str, Any]:
    """Previous-session last 1m close — required for gap detection in BT."""
    if not table_exists(con, "intraday_candles"):
        return {"ctx_date": None, "ctx_time": None, "ctx_close": None}
    try:
        # Bare inequality uses idx_candles_date; substr(<) cannot.
        row = con.execute(
            'SELECT trading_date, candle_time, close FROM "intraday_candles" '
            "WHERE trading_date < ? AND interval_min = 1 "
            "ORDER BY trading_date DESC, candle_time DESC LIMIT 1",
            (date[:10],),
        ).fetchone()
    except sqlite3.Error:
        return {"ctx_date": None, "ctx_time": None, "ctx_close": None}
    if row is None:
        return {"ctx_date": None, "ctx_time": None, "ctx_close": None}
    close = row["close"]
    try:
        close_f = round(float(close), 6) if close is not None else None
    except (TypeError, ValueError):
        close_f = None
    return {
        "ctx_date": str(row["trading_date"] or "")[:10] or None,
        "ctx_time": str(row["candle_time"] or ""),
        "ctx_close": close_f,
    }


def shard_fidelity(
    path: Path,
    date: str,
    *,
    expect_prev_close: Optional[bool] = None,
) -> Dict[str, Any]:
    """Live-faithful replay needs yesterday's VIX. Previous close is a warning.

    Missing prev_day_vix_close is a FAIL (Gate 1 + momentum_vix_gap go blind).
    Missing previous-session candle is a WARN unless the source DB actually
    had one (then it is a split defect and FAIL).
    """
    out: Dict[str, Any] = {
        "date": date,
        "ok": False,
        "prev_day_vix": None,
        "prev_close": None,
        "prev_close_date": None,
        "why": [],
        "warnings": [],
    }
    if not path.exists():
        out["why"] = ["missing shard"]
        return out
    try:
        con = open_db(path, readonly=True)
    except sqlite3.Error as exc:
        out["why"] = [f"unreadable: {exc}"]
        return out
    try:
        ctx = _prev_context(con, date)
        out["prev_close"] = ctx.get("ctx_close")
        out["prev_close_date"] = ctx.get("ctx_date")
        if out["prev_close"] is None:
            if expect_prev_close is True:
                out["why"].append("no previous-session candle")
            else:
                out["warnings"].append("no previous-session candle")
        prev_vix = None
        if table_exists(con, "session_state"):
            cols = set(table_columns(con, "session_state"))
            if "prev_day_vix_close" in cols:
                sql = (
                    "SELECT prev_day_vix_close FROM session_state "
                    f"WHERE {date_predicate('trading_date')} "
                    "AND prev_day_vix_close IS NOT NULL "
                    "AND prev_day_vix_close > 0"
                )
                if "updated_at" in cols:
                    sql += " ORDER BY updated_at DESC LIMIT 1"
                else:
                    sql += " LIMIT 1"
                row = con.execute(sql, date_range_params(date)).fetchone()
                if row is not None:
                    try:
                        prev_vix = float(row["prev_day_vix_close"])
                    except (TypeError, ValueError, KeyError):
                        prev_vix = None
        out["prev_day_vix"] = prev_vix
        if prev_vix is None:
            out["why"].append("no prev_day_vix_close")
        out["ok"] = not out["why"]
        return out
    except sqlite3.Error as exc:
        out["why"] = [f"query failed: {exc}"]
        return out
    finally:
        con.close()


def format_fidelity(fid: Dict[str, Any]) -> str:
    pv = fid.get("prev_day_vix")
    pc = fid.get("prev_close")
    pvs = str(pv) if pv is not None else "MISSING"
    pcs = str(pc) if pc is not None else "MISSING"
    extra = f" ({fid.get('prev_close_date')})" if fid.get("prev_close_date") else ""
    if fid.get("ok") and not fid.get("warnings"):
        return f"FIDELITY PASS  prev_vix={pvs}  prev_close={pcs}{extra}"
    if fid.get("ok"):
        warn = "; ".join(fid.get("warnings") or [])
        return (
            f"FIDELITY WARN  prev_vix={pvs}  prev_close={pcs}{extra}  "
            f"({warn})"
        )
    why = "; ".join(fid.get("why") or ["unknown"])
    return (
        f"FIDELITY FAIL  prev_vix={pvs}  prev_close={pcs}  "
        f"({why}) — do not use this P&L as go-live"
    )


def fingerprint_source(
    src: sqlite3.Connection,
    date: str,
    *,
    light: bool = False,
) -> Dict[str, Any]:
    """What a fresh split of `date` from the live DB would contain."""
    fp: Dict[str, Any] = {"date": date, "tables": {}}
    dparams = date_range_params(date)
    for table, col in BY_DATE:
        if not table_exists(src, table):
            fp["tables"][table] = {"n": 0}
            continue
        where = f" WHERE {date_predicate(col)}"
        if table in CRITICAL_FINGERPRINT:
            fp["tables"][table] = _scalar_checksum(
                src, table, where, dparams, light=light
            )
        else:
            n = src.execute(
                f'SELECT COUNT(*) AS n FROM "{table}"{where}', dparams
            ).fetchone()["n"]
            fp["tables"][table] = {"n": int(n or 0)}

    # FK child counts from this day's positions
    pos_ids: List[str] = []
    if table_exists(src, "positions"):
        pos_ids = [
            str(r[0])
            for r in src.execute(
                f'SELECT position_id FROM "positions" '
                f"WHERE {date_predicate('trading_date')}",
                dparams,
            ).fetchall()
            if r[0]
        ]
    for table, key_col, _parent, _pk in BY_FK:
        if not table_exists(src, table):
            fp["tables"][table] = {"n": 0}
            continue
        if not pos_ids:
            fp["tables"][table] = {"n": 0}
            continue
        n = 0
        for i in range(0, len(pos_ids), 400):
            chunk = pos_ids[i:i + 400]
            q = ",".join("?" for _ in chunk)
            n += int(
                src.execute(
                    f'SELECT COUNT(*) FROM "{table}" '
                    f'WHERE "{key_col}" IN ({q})',
                    tuple(chunk),
                ).fetchone()[0]
                or 0
            )
        fp["tables"][table] = {"n": n}

    fp["context"] = _prev_context(src, date)
    return fp


def fingerprint_shard(
    path: Path,
    date: str,
    *,
    light: bool = False,
) -> Optional[Dict[str, Any]]:
    """Fingerprint of an existing per-day file (same shape as source)."""
    if not path.exists():
        return None
    try:
        con = open_db(path, readonly=True)
    except sqlite3.Error:
        return None
    try:
        fp: Dict[str, Any] = {"date": date, "tables": {}}
        dparams = date_range_params(date)
        for table, col in BY_DATE:
            if not table_exists(con, table):
                fp["tables"][table] = {"n": 0}
                continue
            # Day rows only (exclude the previous-session context candle).
            where = f" WHERE {date_predicate(col)}"
            if table in CRITICAL_FINGERPRINT:
                fp["tables"][table] = _scalar_checksum(
                    con, table, where, dparams, light=light
                )
            else:
                n = con.execute(
                    f'SELECT COUNT(*) AS n FROM "{table}"{where}', dparams
                ).fetchone()["n"]
                fp["tables"][table] = {"n": int(n or 0)}

        pos_ids: List[str] = []
        if table_exists(con, "positions"):
            pos_ids = [
                str(r[0])
                for r in con.execute(
                    f'SELECT position_id FROM "positions" '
                    f"WHERE {date_predicate('trading_date')}",
                    dparams,
                ).fetchall()
                if r[0]
            ]
        for table, key_col, _p, _pk in BY_FK:
            if not table_exists(con, table):
                fp["tables"][table] = {"n": 0}
                continue
            if not pos_ids:
                fp["tables"][table] = {"n": 0}
                continue
            n = 0
            for i in range(0, len(pos_ids), 400):
                chunk = pos_ids[i:i + 400]
                q = ",".join("?" for _ in chunk)
                n += int(
                    con.execute(
                        f'SELECT COUNT(*) FROM "{table}" '
                        f'WHERE "{key_col}" IN ({q})',
                        tuple(chunk),
                    ).fetchone()[0]
                    or 0
                )
            fp["tables"][table] = {"n": n}

        # Context candle is stored with its ORIGINAL trading_date (< date).
        fp["context"] = _prev_context(con, date)
        return fp
    except sqlite3.Error:
        return None
    finally:
        con.close()


def fingerprints_equal(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    if not a or not b:
        return False
    if a.get("date") != b.get("date"):
        return False
    if a.get("context") != b.get("context"):
        return False
    ta, tb = a.get("tables") or {}, b.get("tables") or {}
    keys = set(ta) | set(tb)
    for k in keys:
        if (ta.get(k) or {"n": 0}) != (tb.get(k) or {"n": 0}):
            return False
    return True


# ── build ───────────────────────────────────────────────────────────────────

def _copy_via_attach(
    dst: sqlite3.Connection,
    _src_path: Path,
    table: str,
    where_sql: str,
    params: tuple,
) -> int:
    """INSERT…SELECT across an ATTACH — avoids pulling large chains into RAM."""
    if not table_exists(dst, table):
        return 0
    cols = table_columns(dst, table)
    if not cols:
        return 0
    colq = ", ".join(f'"{c}"' for c in cols)
    dst.execute(
        f'INSERT INTO "{table}" ({colq}) '
        f'SELECT {colq} FROM src."{table}"{where_sql}',
        params,
    )
    return int(dst.execute("SELECT changes()").fetchone()[0] or 0)


def build_day(
    src_path: Path,
    out_path: Path,
    date: str,
    *,
    include_global: bool = False,
    vacuum: bool = False,
    full_integrity: bool = False,
) -> Dict[str, Any]:
    """
    Atomically write one per-day database.

    Writes to a temp file first; only replaces `out_path` after quick_check
    (or integrity_check) + fingerprint match against the source. Never
    deletes an existing shard until the replacement is ready. Optional
    VACUUM (off by default) shrinks the file but is not required for
    correctness.
    """
    # Unique temp name so parallel workers never collide on the same path.
    tmp_path = out_path.parent / (
        f"{out_path.stem}.{date.replace('-', '')}.{os.getpid()}.tmp"
    )
    if tmp_path.exists():
        remove_db_trio(tmp_path)

    counts: Dict[str, int] = {}
    integrity = "unknown"
    check_pragma = (
        "PRAGMA integrity_check" if full_integrity else "PRAGMA quick_check"
    )

    # Pre-read fingerprint / schema / context on a single RO handle, then
    # CLOSE it before ATTACH. On Windows, EXCLUSIVE (or even a second open)
    # against the same snapshot while src_ro is live → "database is locked"
    # (Oct28 rebuild failure with only bot_controller running).
    src_ro = open_db(src_path, readonly=True)
    try:
        try:
            src_ro.execute("PRAGMA cache_size=-65536")
            src_ro.execute("PRAGMA mmap_size=268435456")
        except sqlite3.Error:
            pass
        src_fp = fingerprint_source(src_ro, date)
        # Context is already inside src_fp; reuse to avoid a second scan.
        ctx = src_fp.get("context") or _prev_context(src_ro, date)
        table_stmts, index_stmts = schema_sql(
            src_ro, include_global=include_global
        )
        src_tables = {
            t for t, _ in BY_DATE if table_exists(src_ro, t)
        } | {
            t for t, *_ in BY_FK if table_exists(src_ro, t)
        } | {
            t for t in GLOBAL if table_exists(src_ro, t)
        }
    finally:
        src_ro.close()

    # Open the temp shard with uri=True so ATTACH can use mode=ro on the
    # source (live primary or snapshot). Never open the primary writable.
    tmp_uri = f"file:{tmp_path.resolve().as_posix()}?mode=rwc"
    dst = sqlite3.connect(tmp_uri, uri=True, timeout=60)
    # journal_mode=OFF / synchronous=OFF apply ONLY to this temp shard.
    # The live primary is never opened writable from this script.
    dst.execute("PRAGMA journal_mode=OFF")
    dst.execute("PRAGMA synchronous=OFF")
    dst.execute("PRAGMA temp_store=MEMORY")
    dst.execute("PRAGMA cache_size=-131072")  # ~128 MB page cache
    dst.execute("PRAGMA busy_timeout=60000")
    # Do NOT set locking_mode=EXCLUSIVE before ATTACH — that tries to
    # exclusively lock the attached source and deadlocks on Windows.
    dparams = date_range_params(date)
    try:
        with dst:
            for stmt in table_stmts:
                try:
                    dst.execute(stmt)
                except sqlite3.Error as exc:
                    if "already exists" not in str(exc).lower():
                        raise

            # ATTACH source read-only. mode=ro is required when work_src is
            # the live primary (no snapshot); also prevents accidental
            # writes / close-checkpoint against a disposable snapshot.
            dst.execute(
                "ATTACH DATABASE ? AS src",
                (_as_ro_uri(src_path),),
            )
            try:
                for table, col in BY_DATE:
                    if table not in src_tables:
                        counts[table] = 0
                        continue
                    where = f" WHERE {date_predicate(col)}"
                    counts[table] = _copy_via_attach(
                        dst, src_path, table, where, dparams
                    )

                pos_ids = (
                    [
                        str(r[0])
                        for r in dst.execute(
                            'SELECT position_id FROM "positions"'
                        ).fetchall()
                        if r[0]
                    ]
                    if table_exists(dst, "positions")
                    else []
                )

                for table, key_col, _parent, _pk in BY_FK:
                    if table not in src_tables:
                        counts[table] = 0
                        continue
                    if not pos_ids:
                        counts[table] = 0
                        continue
                    n = 0
                    for i in range(0, len(pos_ids), 400):
                        chunk = pos_ids[i:i + 400]
                        q = ",".join("?" for _ in chunk)
                        n += _copy_via_attach(
                            dst,
                            src_path,
                            table,
                            f' WHERE "{key_col}" IN ({q})',
                            tuple(chunk),
                        )
                    counts[table] = n

                if include_global:
                    for table in GLOBAL:
                        if table not in src_tables:
                            counts[table] = 0
                            continue
                        counts[table] = _copy_via_attach(
                            dst, src_path, table, "", ()
                        )

                # Previous-session last 1m candle (gap detection).
                # Kept under its ORIGINAL trading_date so
                # HistoricalStore.load_day's `trading_date < ?` finds it.
                counts["intraday_candles:context"] = 0
                if ctx.get("ctx_date") and table_exists(
                    dst, "intraday_candles"
                ):
                    cols = table_columns(dst, "intraday_candles")
                    colq = ", ".join(f'"{c}"' for c in cols)
                    ctx_lo, ctx_hi = date_range_params(str(ctx["ctx_date"]))
                    dst.execute(
                        f'INSERT INTO "intraday_candles" ({colq}) '
                        f'SELECT {colq} FROM src."intraday_candles" '
                        f"WHERE trading_date >= ? AND trading_date < ? "
                        f"AND interval_min = 1 AND candle_time = ? "
                        f"LIMIT 1",
                        (ctx_lo, ctx_hi, ctx["ctx_time"]),
                    )
                    counts["intraday_candles:context"] = int(
                        dst.execute("SELECT changes()").fetchone()[0] or 0
                    )
                    if counts["intraday_candles:context"] == 0:
                        row = dst.execute(
                            'SELECT * FROM src."intraday_candles" '
                            "WHERE trading_date < ? "
                            "AND interval_min = 1 "
                            "ORDER BY trading_date DESC, candle_time DESC "
                            "LIMIT 1",
                            (date[:10],),
                        ).fetchone()
                        if row is not None:
                            ph = ", ".join("?" for _ in cols)
                            dst.execute(
                                f'INSERT INTO "intraday_candles" '
                                f"({colq}) VALUES ({ph})",
                                tuple(row[i] for i in range(len(cols))),
                            )
                            counts["intraday_candles:context"] = 1
            finally:
                try:
                    dst.execute("DETACH DATABASE src")
                except sqlite3.Error:
                    pass

            # Build indexes after bulk load (much faster than insert-with-index).
            for stmt in index_stmts:
                try:
                    dst.execute(stmt)
                except sqlite3.Error as exc:
                    if "already exists" not in str(exc).lower():
                        raise

            # Exclusive only on the temp shard after the source is detached.
            try:
                dst.execute("PRAGMA locking_mode=EXCLUSIVE")
            except sqlite3.Error:
                pass

        # quick_check validates pages without the full index↔table cross
        # walk. Content correctness is gated by the fingerprint match below;
        # atomic replace still keeps any existing shard intact on failure.
        # Use --full-integrity for PRAGMA integrity_check (much slower).
        integrity = dst.execute(check_pragma).fetchone()[0]
        # VACUUM is optional compaction — integrity already gates replace.
        if integrity == "ok" and vacuum:
            dst.execute("VACUUM")
    finally:
        dst.close()

    if integrity != "ok":
        remove_db_trio(tmp_path)
        raise RuntimeError(
            f"{date}: {check_pragma} failed ({integrity}); shard not replaced"
        )

    new_fp = fingerprint_shard(tmp_path, date)
    if not fingerprints_equal(src_fp, new_fp or {}):
        if not _critical_match(src_fp, new_fp or {}):
            hint = _diff_hint(src_fp, new_fp)
            remove_db_trio(tmp_path)
            raise RuntimeError(
                f"{date}: new shard fingerprint mismatch vs source "
                f"({hint}); not replacing {out_path.name}"
            )

    # Atomic replace: existing shard stays intact until this succeeds.
    os.replace(str(tmp_path), str(out_path))
    return {
        "counts": counts,
        "integrity": integrity,
        "fingerprint": new_fp,
    }


def _rebuild_day_worker(job: Dict[str, Any]) -> Dict[str, Any]:
    """Child-process entry: rebuild one day shard from the shared snapshot."""
    date = str(job["date"])
    out_path = Path(job["out_path"])
    try:
        result = build_day(
            Path(job["src_path"]),
            out_path,
            date,
            include_global=bool(job.get("include_global")),
            vacuum=bool(job.get("vacuum")),
            full_integrity=bool(job.get("full_integrity")),
        )
        fid = shard_fidelity(
            out_path,
            date,
            expect_prev_close=bool(job.get("expect_prev_close")),
        )
        size = out_path.stat().st_size if out_path.is_file() else 0
        rows = sum(int(v or 0) for v in (result.get("counts") or {}).values())
        return {
            "date": date,
            "ok": True,
            "action": str(job.get("action") or "CREATED"),
            "rows": rows,
            "size": size,
            "fidelity_ok": bool(fid.get("ok")),
            "fidelity": format_fidelity(fid),
            "had_existing": bool(job.get("had_existing")),
        }
    except Exception as exc:
        return {
            "date": date,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "fidelity_ok": False,
            "fidelity": "",
            "size": 0,
            "rows": 0,
            "action": "FAIL",
            "had_existing": bool(job.get("had_existing")),
        }


def _critical_match(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    if a.get("context") != b.get("context"):
        return False
    ta, tb = a.get("tables") or {}, b.get("tables") or {}
    for name in CRITICAL_FINGERPRINT:
        if (ta.get(name) or {"n": 0}) != (tb.get(name) or {"n": 0}):
            return False
    return True


def _trading_dates(
    dates: Sequence[str],
    holiday_dates: Set[str],
) -> List[str]:
    out: List[str] = []
    for d in dates:
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            continue
        if dt.weekday() >= 5:
            continue
        if d in holiday_dates:
            continue
        out.append(d)
    return out


def _weekday_span(
    start: str,
    end: str,
    holiday_dates: Set[str],
) -> List[str]:
    """Every NSE weekday from start..end inclusive (holidays excluded).

    Used so gaps in the live DB (e.g. 2026-10-09..10-14 wiped from
    primary while shards remain) still appear in the split report instead
    of being silently omitted between discovered dates.
    """
    try:
        d0 = datetime.strptime(start[:10], "%Y-%m-%d")
        d1 = datetime.strptime(end[:10], "%Y-%m-%d")
    except ValueError:
        return []
    if d1 < d0:
        d0, d1 = d1, d0
    out: List[str] = []
    cur = d0
    while cur <= d1:
        ds = cur.strftime("%Y-%m-%d")
        if cur.weekday() < 5 and ds not in holiday_dates:
            out.append(ds)
        cur += timedelta(days=1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Split the live nifty_algo DB into per-day shards that the "
            "backtest harness can replay with live-faithful behaviour."
        )
    )
    ap.add_argument(
        "--src",
        default="data/nifty_algo_v3.db",
        help="source database (default: data/nifty_algo_v3.db)",
    )
    ap.add_argument(
        "--out",
        default="data/per_day",
        help="output directory (default: data/per_day)",
    )
    ap.add_argument(
        "--dates",
        nargs="*",
        default=None,
        help="optional YYYY-MM-DD list (default: every date found)",
    )
    ap.add_argument(
        "--holidays",
        default="nse_holidays.json",
        help="JSON list of holiday dates (default: nse_holidays.json)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="rebuild even when an existing shard already matches",
    )
    ap.add_argument(
        "--verify-only",
        action="store_true",
        help="compare fingerprints only; do not write or delete anything",
    )
    ap.add_argument(
        "--check-fidelity",
        action="store_true",
        help="on UNCHANGED days, open each shard for prev_vix/prev_close "
             "fidelity (slow on large libraries). Default skips that I/O.",
    )
    ap.add_argument(
        "--full-fingerprint",
        action="store_true",
        help="skip decision uses full SUM checksums (slow). Default uses "
             "light stamps (n + min/max capture_time) for skip-if-unchanged.",
    )
    ap.add_argument(
        "--include-global",
        action="store_true",
        help="also copy calibration_state / expiry_results (not needed for BT)",
    )
    ap.add_argument(
        "--allow-unfaithful",
        action="store_true",
        help="exit 0 even if a shard is missing yesterday's VIX (do not use for go-live)",
    )
    ap.add_argument(
        "--allow-live",
        action="store_true",
        help="allow a backup() snapshot while the writer lock is held "
             "(not recommended; wait for main.py to exit). Also forces "
             "the snapshot path even if the lock is free.",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_SPLIT_WORKERS,
        help=(
            f"max concurrent day-shard rebuild processes "
            f"(default {DEFAULT_SPLIT_WORKERS}). Fingerprint skip/verify "
            f"stays in the parent; only rebuilds run in parallel."
        ),
    )
    ap.add_argument(
        "--vacuum",
        action="store_true",
        help="VACUUM each new shard after integrity check (slower, smaller files). "
             "Default skips VACUUM — correctness uses quick_check + fingerprint "
             "+ atomic replace.",
    )
    ap.add_argument(
        "--full-integrity",
        action="store_true",
        help="use PRAGMA integrity_check instead of quick_check (slower). "
             "Default quick_check + fingerprint match is sufficient.",
    )
    args = ap.parse_args()
    args.workers = max(1, int(args.workers or DEFAULT_SPLIT_WORKERS))

    src_path = Path(args.src)
    if not src_path.exists():
        print(f"\n  No source database at {src_path}\n", file=sys.stderr)
        return 1
    if not args.allow_live and writer_lock_held(src_path):
        print(
            f"\n  Refusing to split: {src_path.name}.writer.lock is held.\n"
            f"  Wait for main.py to finish the session (WAL checkpoint + "
            f"close) so the live btree is not snapshotted mid-write.\n"
            f"  Pass --allow-live only if you accept a backup() of a live WAL.\n",
            file=sys.stderr,
        )
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    holiday_dates: Set[str] = set()
    holidays_path = Path(args.holidays)
    if holidays_path.exists():
        with open(holidays_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            holiday_dates = {str(x)[:10] for x in raw}
        elif isinstance(raw, dict):
            holiday_dates = {str(x)[:10] for x in raw.keys()}
    else:
        print(
            f"\n  Warning: holidays file '{holidays_path}' not found; "
            f"weekends only filtered.\n"
        )

    live_src = src_path
    snap_path: Optional[Path] = None
    src: Optional[sqlite3.Connection] = None
    try:
        # Prefer a backup() snapshot when the WAL is large or --allow-live:
        # one sequential copy beats N workers random-reading a multi-GB WAL.
        # With a small WAL and a free writer lock, read the primary in place.
        wal_path = Path(str(live_src) + "-wal")
        try:
            wal_bytes = wal_path.stat().st_size if wal_path.is_file() else 0
        except OSError:
            wal_bytes = 0
        need_snapshot = bool(args.allow_live) or (
            wal_bytes >= SNAPSHOT_WAL_THRESHOLD_BYTES
        )
        if need_snapshot:
            why = (
                "--allow-live"
                if args.allow_live
                else f"WAL {wal_bytes / (1024 * 1024):.0f} MB >= "
                     f"{SNAPSHOT_WAL_THRESHOLD_BYTES // (1024 * 1024)} MB"
            )
            print(
                f"\n  Snapshotting with read-only backup() ({why}; "
                f"live file is not written) ..."
            )
            t_snap = time.time()
            snap_path = snapshot_readonly(live_src)
            work_src = snap_path
            print(
                f"  Snapshot : {work_src.name}  "
                f"({time.time() - t_snap:.1f}s)"
            )
        else:
            work_src = live_src
            print(
                "\n  Reading quiescent primary in place "
                f"(WAL {wal_bytes / (1024 * 1024):.1f} MB; "
                f"query_only + ATTACH mode=ro; no snapshot copy)"
            )
        src = open_db(work_src, readonly=True)
        try:
            src.execute("PRAGMA cache_size=-65536")
            src.execute("PRAGMA mmap_size=268435456")
        except sqlite3.Error:
            pass
        discovered = discover_dates(src)
        if args.dates:
            wanted = {d[:10] for d in args.dates}
            discovered = [d for d in discovered if d in wanted]
            missing_wanted = sorted(wanted - set(discovered))
            if missing_wanted:
                print(
                    f"  Warning: no source rows for: {', '.join(missing_wanted)}"
                )
        discovered = _trading_dates(discovered, holiday_dates)
        if not discovered and not args.dates:
            print("\n  No valid trading dates found to process.\n")
            return 1

        # Fill weekday gaps between first and last discovered session so
        # wiped/missing source days still appear in the report (MISSING /
        # ORPHAN) instead of vanishing between e.g. 10-08 and 10-15.
        if args.dates:
            dates = _trading_dates(
                sorted({d[:10] for d in args.dates}), holiday_dates
            )
        elif discovered:
            dates = _weekday_span(discovered[0], discovered[-1], holiday_dates)
        else:
            dates = []
        if not dates:
            print("\n  No valid trading dates found to process.\n")
            return 1

        gap_days = [d for d in dates if d not in set(discovered)]
        print(f"\n  Source : {live_src}  (work copy {work_src.name})")
        print(f"  Output : {out_dir}/")
        print(
            f"  Dates  : {len(dates)} weekdays  ({dates[0]} .. {dates[-1]})  "
            f"source_has_rows={len(discovered)}  "
            f"calendar_gaps={len(gap_days)}"
        )
        if gap_days:
            print(f"  Gaps   : {', '.join(gap_days)}")
        print(
            f"  Mode   : "
            f"{'verify-only' if args.verify_only else ('force-rebuild' if args.force else 'skip-if-unchanged')}"
            f"{' +global' if args.include_global else ''}"
            f"{' +vacuum' if args.vacuum else ''}"
            f"{' +full-integrity' if args.full_integrity else ''}"
            f"  workers={args.workers}\n"
        )

        unchanged = empty_source = orphan_kept = missing_source = 0
        rebuilt = failed = verified_ok = verified_bad = 0
        fidelity_bad = 0
        total_bytes = 0
        t0 = time.time()
        rebuild_jobs: List[Dict[str, Any]] = []

        use_light = not bool(args.full_fingerprint)
        for d in dates:
            out_path = out_dir / f"nifty_algo_{d}.db"
            src_fp = fingerprint_source(src, d, light=use_light)
            chain_n = (src_fp.get("tables") or {}).get(
                "option_chain_snapshot", {}
            ).get("n", 0)
            if int(chain_n or 0) == 0:
                # No chain in primary: cannot rebuild. Keep any existing
                # shard (orphan from a prior split) and report clearly.
                if out_path.is_file():
                    size_kb = out_path.stat().st_size / 1024
                    total_bytes += out_path.stat().st_size
                    print(
                        f"  {d}: ORPHAN   {size_kb:>10,.1f} KB  "
                        f"(no source chain — keeping existing shard)"
                    )
                    orphan_kept += 1
                else:
                    print(
                        f"  {d}: MISSING  (no option_chain_snapshot in source, "
                        f"no shard on disk)"
                    )
                    missing_source += 1
                empty_source += 1
                continue

            existing_fp = fingerprint_shard(out_path, d, light=use_light)
            same = fingerprints_equal(src_fp, existing_fp or {})
            expect_prev = (
                (src_fp.get("context") or {}).get("ctx_close") is not None
            )

            if args.verify_only:
                fid = shard_fidelity(
                    out_path, d, expect_prev_close=expect_prev
                )
                if not fid.get("ok"):
                    fidelity_bad += 1
                if same:
                    print(
                        f"  {d}: MATCH  ({out_path.name})  "
                        f"{format_fidelity(fid)}"
                    )
                    verified_ok += 1
                else:
                    why = _diff_hint(src_fp, existing_fp)
                    print(f"  {d}: DRIFT  {why}  {format_fidelity(fid)}")
                    verified_bad += 1
                continue

            if same and not args.force:
                size_kb = out_path.stat().st_size / 1024
                total_bytes += out_path.stat().st_size
                if args.check_fidelity:
                    fid = shard_fidelity(
                        out_path, d, expect_prev_close=expect_prev
                    )
                    if not fid.get("ok"):
                        fidelity_bad += 1
                    fid_s = format_fidelity(fid)
                else:
                    fid_s = "FIDELITY skipped (pass --check-fidelity)"
                print(
                    f"  {d}: UNCHANGED  {size_kb:>10,.1f} KB  "
                    f"(fingerprint match — kept)  {fid_s}"
                )
                unchanged += 1
                continue

            rebuild_jobs.append(
                {
                    "date": d,
                    "src_path": str(work_src),
                    "out_path": str(out_path),
                    "include_global": bool(args.include_global),
                    "vacuum": bool(args.vacuum),
                    "full_integrity": bool(args.full_integrity),
                    "expect_prev_close": expect_prev,
                    "had_existing": existing_fp is not None,
                    "action": "REPLACED" if existing_fp is not None else "CREATED",
                }
            )

        # Rebuilds only — fingerprint decisions already made above. Each
        # worker writes a unique temp file then atomically replaces its
        # own out_path; the shared source is attached read-only.
        if rebuild_jobs and not args.verify_only:
            # Re-check writer lock before mutating shards when reading the
            # live primary in place (no isolating snapshot).
            if snap_path is None and not args.allow_live and writer_lock_held(
                live_src
            ):
                print(
                    f"\n  Aborting rebuilds: {live_src.name}.writer.lock "
                    f"appeared after fingerprinting.\n"
                    f"  Existing shards were not replaced.\n",
                    file=sys.stderr,
                )
                return 1
            # Drop the parent's RO handle before workers ATTACH the same
            # file (Windows lock class as src_ro+ATTACH in-process).
            if src is not None:
                try:
                    src.close()
                except Exception:
                    pass
                src = None
            workers = max(1, min(int(args.workers), len(rebuild_jobs)))
            print(
                f"  Rebuilding {len(rebuild_jobs)} shard(s) with "
                f"{workers} process(es) ..."
            )
            if workers == 1:
                results = [_rebuild_day_worker(j) for j in rebuild_jobs]
            else:
                ctx = mp.get_context("spawn")
                results = []
                with ProcessPoolExecutor(
                    max_workers=workers, mp_context=ctx
                ) as pool:
                    futs = {
                        pool.submit(_rebuild_day_worker, j): j["date"]
                        for j in rebuild_jobs
                    }
                    for fut in as_completed(futs):
                        try:
                            results.append(fut.result())
                        except Exception as exc:
                            results.append(
                                {
                                    "date": futs[fut],
                                    "ok": False,
                                    "error": f"{type(exc).__name__}: {exc}",
                                    "fidelity_ok": False,
                                    "size": 0,
                                    "rows": 0,
                                    "action": "FAIL",
                                }
                            )
            for row in sorted(results, key=lambda r: str(r.get("date") or "")):
                d = str(row.get("date") or "")
                if not row.get("ok"):
                    print(f"  {d}: FAIL  {row.get('error')}")
                    failed += 1
                    continue
                total_bytes += int(row.get("size") or 0)
                size_kb = int(row.get("size") or 0) / 1024
                if not row.get("fidelity_ok"):
                    fidelity_bad += 1
                print(
                    f"  {d}: {str(row.get('action') or 'CREATED'):8s}  "
                    f"{int(row.get('rows') or 0):>10,} rows  "
                    f"{size_kb:>10,.1f} KB  {row.get('fidelity') or ''}"
                )
                rebuilt += 1

        # Keep .gitignore synchronized after splitting. In verify-only mode we
        # intentionally do not modify any files.
        if not args.verify_only:
            ignored_shards = update_gitignore_for_large_shards(out_dir)
            if ignored_shards:
                print("  .gitignore: added oversized shard(s):")
                for entry, size_bytes in ignored_shards:
                    print(f"    {entry} ({size_bytes / 1_000_000:.2f} MB)")
            else:
                print("  .gitignore: no new oversized shard entries needed")

        elapsed = time.time() - t0
        print()
        if args.verify_only:
            print(
                f"  Verify: {verified_ok} match, {verified_bad} drift, "
                f"empty_source={empty_source} (orphan={orphan_kept} "
                f"missing={missing_source}), fidelity_fail={fidelity_bad}  "
                f"({elapsed:.1f}s)\n"
            )
            if verified_bad:
                return 2
            if fidelity_bad and not args.allow_unfaithful:
                return 2
            return 0

        print(
            f"  Done. rebuilt={rebuilt} unchanged={unchanged} failed={failed}  "
            f"orphan_kept={orphan_kept} missing={missing_source}  "
            f"fidelity_fail={fidelity_bad}  "
            f"{total_bytes / (1024 * 1024):,.1f} MB on disk  ({elapsed:.1f}s)\n"
        )
        if missing_source:
            print(
                "  Note: MISSING days have no chain in the live DB and no "
                "shard — the engine never recorded those sessions (or they "
                "were purged). Split cannot invent them.\n"
            )
        if orphan_kept:
            print(
                "  Note: ORPHAN shards are kept on disk for backtest but "
                "cannot be refreshed until the live DB has those days again.\n"
            )
        if failed:
            return 1
        if fidelity_bad and not args.allow_unfaithful:
            print(
                "  One or more shards missing prev_day_vix_close. "
                "Do not treat those P&L numbers as live-faithful. "
                "(pass --allow-unfaithful only to ignore this.)"
            )
            return 2
        return 0
    finally:
        if src is not None:
            try:
                src.close()
            except Exception:
                pass
        if snap_path is not None:
            remove_db_trio(snap_path)


def _diff_hint(
    src_fp: Dict[str, Any],
    existing_fp: Optional[Dict[str, Any]],
) -> str:
    if existing_fp is None:
        return "(missing shard)"
    bits: List[str] = []
    if src_fp.get("context") != existing_fp.get("context"):
        bits.append(
            f"context src={src_fp.get('context')} dst={existing_fp.get('context')}"
        )
    ta = src_fp.get("tables") or {}
    tb = existing_fp.get("tables") or {}
    for name in CRITICAL_FINGERPRINT:
        if ta.get(name) != tb.get(name):
            bits.append(
                f"{name} src_n={(ta.get(name) or {}).get('n')} "
                f"dst_n={(tb.get(name) or {}).get('n')}"
            )
    for name in sorted(set(ta) | set(tb)):
        if name in CRITICAL_FINGERPRINT:
            continue
        sn = (ta.get(name) or {}).get("n")
        dn = (tb.get(name) or {}).get("n")
        if sn != dn:
            bits.append(f"{name} {sn}->{dn}")
            if len(bits) >= 6:
                break
    return "; ".join(bits) if bits else "(fingerprint mismatch)"


if __name__ == "__main__":
    sys.exit(main())
