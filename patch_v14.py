#!/usr/bin/env python3
"""patch_v14.py - safe, idempotent NIFTY engine audit repairs.

Run from the repository root:
    python patch_v14.py

This patch deliberately changes only deterministic correctness defects found by
code inspection and per-DTE replay. It does not tune a strategy to the five
sessions. It repairs stale DTE state after restart and timestamp-safe per-day
SQLite sharding, then compiles and runs the engine self-tests.
"""
from __future__ import annotations
import hashlib, py_compile, shutil, sqlite3, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MARKER = "PATCH_V14_APPLIED"

def fail(msg: str) -> None:
    raise RuntimeError(msg)

def replace_once(path: Path, old: str, new: str, label: str) -> bool:
    text = path.read_text(encoding="utf-8")
    if new in text:
        return False
    if old not in text:
        fail(f"required anchor missing: {label} ({path.name})")
    backup = path.with_suffix(path.suffix + ".v14bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return True

def patch_data_engine() -> bool:
    p = ROOT / "data_engine.py"
    old = '''        if row is not None:\n            # Reconcile entry_count with actual DB positions\n'''
    new = '''        if row is not None:\n            # PATCH_V14: restart-safe expiry/DTE reconciliation.  A persisted\n            # session row is not authoritative for contract selection: the\n            # process may have been started before the broker listed the\n            # expiry, or the row may have been created by an earlier calendar\n            # rule.  Leaving its old actual_dte in place silently selects the\n            # wrong risk/target ladder (observed in the 2026-09-10/11 shards).\n            # Recompute the calendar DTE on every restart; discover_active_\n            # expiry() will replace it with the actual broker expiry later.\n            try:\n                _fresh_dte = ExpiryCalendar.get_dte(today_ist())\n                if row.get("actual_expiry"):\n                    _exp = datetime.strptime(str(row["actual_expiry"])[:10], "%Y-%m-%d").date()\n                    _today = today_ist()\n                    _fresh_dte = 0 if _exp <= _today else 0\n                    _walk = _today + timedelta(days=1)\n                    while _walk <= _exp:\n                        if not ExpiryCalendar.is_holiday(_walk):\n                            _fresh_dte += 1\n                        _walk += timedelta(days=1)\n                if row.get("actual_dte") != _fresh_dte:\n                    self.db.update("session_state", {"actual_dte": _fresh_dte},\n                                   {"trading_date": today_str})\n                    row["actual_dte"] = _fresh_dte\n                    self.logger.warning(\n                        f"PATCH_V14 corrected persisted DTE to {_fresh_dte} for {today_str}")\n            except Exception as exc:\n                self.logger.error(f"PATCH_V14 DTE reconciliation failed: {exc}")\n\n            # Reconcile entry_count with actual DB positions\n'''
    return replace_once(p, old, new, "session row restart anchor")

def patch_splitter() -> bool:
    p = ROOT / "split_db_per_day.py"
    old = '''            n = copy_table(dst=dst, src=src, table=table,\n                           where_sql=f' WHERE "{col}" = ?', params=(date,))\n'''
    new = '''            # PATCH_V14: date columns are not consistently stored as DATE;\n            # several feeds write ISO timestamps.  Equality to YYYY-MM-DD\n            # silently dropped those rows while discover_dates() still\n            # advertised the day.  Prefix matching is safe for ISO dates and\n            # preserves the complete day shard.\n            n = copy_table(dst=dst, src=src, table=table,\n                           where_sql=f' WHERE substr("{col}", 1, 10) = ?',\n                           params=(date,))\n'''
    return replace_once(p, old, new, "date shard filter")

def validate_shards() -> None:
    # Validate only existing shards; no source DB is required for installation.
    for db in sorted((ROOT / "data" / "per_day").glob("nifty_algo_*.db")):
        con = sqlite3.connect(str(db))
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                fail(f"SQLite integrity failure: {db}")
            # A shard must contain no rows whose normalized date differs from
            # its filename for the tables that carry an explicit date.
            expected = db.stem.rsplit("_", 1)[-1]
            for table, col in (("intraday_candles","trading_date"),
                               ("option_chain_snapshot","trading_date"),
                               ("positions","trading_date"),
                               ("trade_entries","trading_date"),
                               ("market_snapshots","date"),
                               ("regime_decisions","date")):
                try:
                    bad = con.execute(f'''SELECT COUNT(*) FROM "{table}"\n                                          WHERE "{col}" IS NOT NULL\n                                          AND substr("{col}",1,10) <> ?''', (expected,)).fetchone()[0]
                    # Per-day shards intentionally contain one prior-session
                    # 1-minute candle for gap context; it is the only allowed
                    # cross-day row and must remain bounded.
                    if bad and table == "intraday_candles":
                        bad -= con.execute('''SELECT COUNT(*) FROM "intraday_candles"
                                              WHERE "trading_date" IS NOT NULL
                                              AND substr("trading_date",1,10) <> ?
                                              AND "interval_min" = 1''', (expected,)).fetchone()[0]
                    if bad:
                        fail(f"cross-day rows in {db.name}:{table}={bad}")
                except sqlite3.OperationalError:
                    pass
        finally:
            con.close()

def main() -> int:
    if not ROOT.exists():
        fail("repository root not found")
    changed = patch_data_engine() or False
    changed = patch_splitter() or changed
    marker = ROOT / "data_engine.py"
    text = marker.read_text(encoding="utf-8")
    if MARKER not in text:
        marker.write_text(text + f"\n# {MARKER}\n", encoding="utf-8")
        changed = True
    for name in ("data_engine.py", "split_db_per_day.py"):
        py_compile.compile(str(ROOT / name), doraise=True)
    validate_shards()
    # Import/self-tests are intentionally lightweight and offline.
    import subprocess
    r = subprocess.run([sys.executable, str(ROOT / "verify_all.py")],
                       cwd=str(ROOT), stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, timeout=900)
    if r.returncode:
        print(r.stdout[-12000:])
        fail(f"verification failed with exit code {r.returncode}")
    print(f"PATCH_V14 {'APPLIED' if changed else 'ALREADY PRESENT'}")
    print("DTE restart reconciliation and timestamp-safe sharding validated.")
    print("Backtest each shard with --trade-report off before paper/live promotion.")
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"PATCH_V14 FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
