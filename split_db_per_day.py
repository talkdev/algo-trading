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
#    * session_state and the other date-scoped tables for forensics / parity
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
#
#     python backtest_engine.py --db data/per_day --from 2026-09-08 --to 2026-09-22
#
# ============================================================================

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# ─────────────────────────────────────────────────────────────────────────────
#  TABLE MAP
# ─────────────────────────────────────────────────────────────────────────────
#  BY_DATE  — filter WHERE substr(date_col,1,10) = trading_date
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


def open_db(path: str | Path, readonly: bool = False) -> sqlite3.Connection:
    path = str(path)
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=60000")
    return con


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def table_columns(con: sqlite3.Connection, table: str) -> List[str]:
    return [c[1] for c in con.execute(f'PRAGMA table_info("{table}")').fetchall()]


def date_predicate(col: str, alias: str = "") -> str:
    """Match YYYY-MM-DD whether the column is a bare date or a timestamp."""
    prefix = f'{alias}.' if alias else ""
    return f'substr({prefix}"{col}", 1, 10) = ?'


def discover_dates(src: sqlite3.Connection) -> List[str]:
    dates: Set[str] = set()
    for table, col in BY_DATE:
        if not table_exists(src, table):
            continue
        try:
            rows = src.execute(
                f'SELECT DISTINCT substr("{col}", 1, 10) AS d FROM "{table}" '
                f'WHERE "{col}" IS NOT NULL'
            ).fetchall()
        except sqlite3.Error:
            continue
        for r in rows:
            d = str(r["d"] or "")
            if len(d) >= 10:
                dates.add(d[:10])
    return sorted(dates)


def schema_sql(
    src: sqlite3.Connection,
    *,
    include_global: bool,
) -> List[str]:
    """CREATE TABLE / INDEX for tables we will populate."""
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
    return tables + indexes


# ── fingerprints ────────────────────────────────────────────────────────────

def _scalar_checksum(
    con: sqlite3.Connection,
    table: str,
    where_sql: str,
    params: tuple,
) -> Dict[str, Any]:
    """Compact content stamp: count + extrema + numeric sums on known cols."""
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
        row = con.execute(
            'SELECT trading_date, candle_time, close FROM "intraday_candles" '
            'WHERE substr(trading_date, 1, 10) < ? AND interval_min = 1 '
            "ORDER BY trading_date DESC, candle_time DESC LIMIT 1",
            (date,),
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


def fingerprint_source(src: sqlite3.Connection, date: str) -> Dict[str, Any]:
    """What a fresh split of `date` from the live DB would contain."""
    fp: Dict[str, Any] = {"date": date, "tables": {}}
    for table, col in BY_DATE:
        if not table_exists(src, table):
            fp["tables"][table] = {"n": 0}
            continue
        where = f" WHERE {date_predicate(col)}"
        if table in CRITICAL_FINGERPRINT:
            fp["tables"][table] = _scalar_checksum(src, table, where, (date,))
        else:
            n = src.execute(
                f'SELECT COUNT(*) AS n FROM "{table}"{where}', (date,)
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
                (date,),
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


def fingerprint_shard(path: Path, date: str) -> Optional[Dict[str, Any]]:
    """Fingerprint of an existing per-day file (same shape as source)."""
    if not path.exists():
        return None
    try:
        con = open_db(path, readonly=True)
    except sqlite3.Error:
        return None
    try:
        fp: Dict[str, Any] = {"date": date, "tables": {}}
        for table, col in BY_DATE:
            if not table_exists(con, table):
                fp["tables"][table] = {"n": 0}
                continue
            # Day rows only (exclude the previous-session context candle).
            where = f" WHERE {date_predicate(col)}"
            if table in CRITICAL_FINGERPRINT:
                fp["tables"][table] = _scalar_checksum(
                    con, table, where, (date,)
                )
            else:
                n = con.execute(
                    f'SELECT COUNT(*) AS n FROM "{table}"{where}', (date,)
                ).fetchone()["n"]
                fp["tables"][table] = {"n": int(n or 0)}

        pos_ids: List[str] = []
        if table_exists(con, "positions"):
            pos_ids = [
                str(r[0])
                for r in con.execute(
                    f'SELECT position_id FROM "positions" '
                    f"WHERE {date_predicate('trading_date')}",
                    (date,),
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
    src_path: Path,
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
    before = dst.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    dst.execute(
        f'INSERT INTO "{table}" ({colq}) '
        f'SELECT {colq} FROM src."{table}"{where_sql}',
        params,
    )
    after = dst.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    return int(after - before)


def build_day(
    src_path: Path,
    out_path: Path,
    date: str,
    *,
    include_global: bool = False,
) -> Dict[str, Any]:
    """
    Atomically write one per-day database.

    Writes to a temp file first; only replaces `out_path` after integrity
    check + fingerprint match against the source. Never deletes an existing
    shard until the replacement is ready.
    """
    tmp_path = out_path.with_suffix(out_path.suffix + f".{os.getpid()}.tmp")
    if tmp_path.exists():
        tmp_path.unlink()

    counts: Dict[str, int] = {}
    integrity = "unknown"

    src_ro = open_db(src_path, readonly=True)
    try:
        # Freeze the expected fingerprint BEFORE the copy. Re-reading the
        # live DB afterwards races with an in-session writer (WAL growth)
        # and falsely rejects a correct shard.
        src_fp = fingerprint_source(src_ro, date)

        dst = sqlite3.connect(str(tmp_path))
        dst.execute("PRAGMA journal_mode=OFF")
        dst.execute("PRAGMA synchronous=OFF")
        dst.execute("PRAGMA temp_store=MEMORY")
        try:
            with dst:
                for stmt in schema_sql(src_ro, include_global=include_global):
                    try:
                        dst.execute(stmt)
                    except sqlite3.Error as exc:
                        if "already exists" not in str(exc).lower():
                            raise

                dst.execute("ATTACH DATABASE ? AS src", (str(Path(src_path).resolve()),))
                try:
                    for table, col in BY_DATE:
                        if not table_exists(src_ro, table):
                            counts[table] = 0
                            continue
                        where = f" WHERE {date_predicate(col)}"
                        counts[table] = _copy_via_attach(
                            dst, src_path, table, where, (date,)
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
                        if not table_exists(src_ro, table):
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
                            if not table_exists(src_ro, table):
                                counts[table] = 0
                                continue
                            counts[table] = _copy_via_attach(
                                dst, src_path, table, "", ()
                            )

                    # Previous-session last 1m candle (gap detection).
                    # Kept under its ORIGINAL trading_date so
                    # HistoricalStore.load_day's `trading_date < ?` finds it.
                    counts["intraday_candles:context"] = 0
                    ctx = _prev_context(src_ro, date)
                    if ctx.get("ctx_date") and table_exists(
                        dst, "intraday_candles"
                    ):
                        cols = table_columns(dst, "intraday_candles")
                        colq = ", ".join(f'"{c}"' for c in cols)
                        dst.execute(
                            f'INSERT INTO "intraday_candles" ({colq}) '
                            f'SELECT {colq} FROM src."intraday_candles" '
                            f"WHERE substr(trading_date, 1, 10) = ? "
                            f"AND interval_min = 1 AND candle_time = ? "
                            f"LIMIT 1",
                            (ctx["ctx_date"], ctx["ctx_time"]),
                        )
                        counts["intraday_candles:context"] = int(
                            dst.execute("SELECT changes()").fetchone()[0] or 0
                        )
                        if counts["intraday_candles:context"] == 0:
                            row = src_ro.execute(
                                'SELECT * FROM "intraday_candles" '
                                "WHERE substr(trading_date,1,10) < ? "
                                "AND interval_min = 1 "
                                "ORDER BY trading_date DESC, candle_time DESC "
                                "LIMIT 1",
                                (date,),
                            ).fetchone()
                            if row is not None:
                                ph = ", ".join("?" for _ in cols)
                                dst.execute(
                                    f'INSERT INTO "intraday_candles" '
                                    f"({colq}) VALUES ({ph})",
                                    tuple(row[c] for c in cols),
                                )
                                counts["intraday_candles:context"] = 1
                finally:
                    try:
                        dst.execute("DETACH DATABASE src")
                    except sqlite3.Error:
                        pass

            integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity == "ok":
                dst.execute("VACUUM")
        finally:
            dst.close()
    finally:
        src_ro.close()

    if integrity != "ok":
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise RuntimeError(
            f"{date}: integrity_check failed ({integrity}); shard not replaced"
        )

    new_fp = fingerprint_shard(tmp_path, date)
    if not fingerprints_equal(src_fp, new_fp or {}):
        if not _critical_match(src_fp, new_fp or {}):
            hint = _diff_hint(src_fp, new_fp)
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise RuntimeError(
                f"{date}: new shard fingerprint mismatch vs source "
                f"({hint}); not replacing {out_path.name}"
            )

    os.replace(str(tmp_path), str(out_path))
    return {
        "counts": counts,
        "integrity": integrity,
        "fingerprint": new_fp,
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
        "--include-global",
        action="store_true",
        help="also copy calibration_state / expiry_results (not needed for BT)",
    )
    args = ap.parse_args()

    src_path = Path(args.src)
    if not src_path.exists():
        print(f"\n  No source database at {src_path}\n", file=sys.stderr)
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

    src = open_db(src_path, readonly=True)
    try:
        dates = discover_dates(src)
        if args.dates:
            wanted = {d[:10] for d in args.dates}
            dates = [d for d in dates if d in wanted]
            missing = sorted(wanted - set(dates))
            if missing:
                print(
                    f"  Warning: no source rows for: {', '.join(missing)}"
                )
        dates = _trading_dates(dates, holiday_dates)
        if not dates:
            print("\n  No valid trading dates found to process.\n")
            return 1

        print(f"\n  Source : {src_path}")
        print(f"  Output : {out_dir}/")
        print(f"  Dates  : {len(dates)}  ({dates[0]} .. {dates[-1]})")
        print(
            f"  Mode   : "
            f"{'verify-only' if args.verify_only else ('force-rebuild' if args.force else 'skip-if-unchanged')}"
            f"{' +global' if args.include_global else ''}\n"
        )

        skipped = rebuilt = failed = verified_ok = verified_bad = 0
        total_bytes = 0
        t0 = time.time()

        for d in dates:
            out_path = out_dir / f"nifty_algo_{d}.db"
            src_fp = fingerprint_source(src, d)
            chain_n = (src_fp.get("tables") or {}).get(
                "option_chain_snapshot", {}
            ).get("n", 0)
            if int(chain_n or 0) == 0:
                print(f"  {d}: SKIP  (no option_chain_snapshot rows in source)")
                skipped += 1
                continue

            existing_fp = fingerprint_shard(out_path, d)
            same = fingerprints_equal(src_fp, existing_fp or {})

            if args.verify_only:
                if same:
                    print(f"  {d}: MATCH  ({out_path.name})")
                    verified_ok += 1
                else:
                    why = _diff_hint(src_fp, existing_fp)
                    print(f"  {d}: DRIFT  {why}")
                    verified_bad += 1
                continue

            if same and not args.force:
                size_kb = out_path.stat().st_size / 1024
                total_bytes += out_path.stat().st_size
                print(
                    f"  {d}: UNCHANGED  {size_kb:>10,.1f} KB  "
                    f"(fingerprint match — kept)"
                )
                skipped += 1
                continue

            try:
                result = build_day(
                    src_path,
                    out_path,
                    d,
                    include_global=bool(args.include_global),
                )
            except Exception as exc:
                print(f"  {d}: FAIL  {exc}")
                failed += 1
                continue

            size_kb = out_path.stat().st_size / 1024
            total_bytes += out_path.stat().st_size
            rows = sum(
                int(v or 0)
                for k, v in (result.get("counts") or {}).items()
            )
            action = "REPLACED" if existing_fp is not None else "CREATED"
            ctx = (result.get("fingerprint") or {}).get("context") or {}
            ctx_note = (
                f"ctx={ctx.get('ctx_date')}@{ctx.get('ctx_close')}"
                if ctx.get("ctx_close") is not None
                else "ctx=NONE"
            )
            print(
                f"  {d}: {action:8s}  {rows:>10,} rows  "
                f"{size_kb:>10,.1f} KB  {ctx_note}"
            )
            rebuilt += 1

        elapsed = time.time() - t0
        print()
        if args.verify_only:
            print(
                f"  Verify: {verified_ok} match, {verified_bad} drift, "
                f"{skipped} empty-source  ({elapsed:.1f}s)\n"
            )
            return 0 if verified_bad == 0 else 2

        print(
            f"  Done. rebuilt={rebuilt} unchanged={skipped} failed={failed}  "
            f"{total_bytes / (1024 * 1024):,.1f} MB on disk  ({elapsed:.1f}s)\n"
        )
        return 0 if failed == 0 else 1
    finally:
        src.close()


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
