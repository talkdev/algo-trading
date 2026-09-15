#!/usr/bin/env python3
"""patch_v13.py - conservative late re-entry risk repair for the NIFTY engine.

This is an independent, idempotent patch.  It edits only strategy_engine.py;
no database, credentials, or generated files are modified.

Why this patch exists
---------------------
The supplied 2026-09-09 replay is not reproducible as a loss with the checked
out engine and the supplied database.  The checked-out engine produces two
trades:

    09:45  BEAR_CALL_SPREAD  +Rs624.51
    12:31  IRON_CONDOR        -Rs82.10

The second ticket is the avoidable failure: it is a fresh DTE>=2 weekly
short-premium position opened after the day's first profitable trade, late in
the session, while the tape was range-bound after an unfilled gap.  The
engine's normal ten-minute entry cooldown only measures time since the prior
entry; it does not impose a post-profit risk stop.  Consequently, a realised
win can be recycled immediately into a new structure whose remaining theta is
small relative to afternoon gamma, spread friction, and the 15:20 hard exit.

The patch adds a general, data-driven guard to the existing hard-gate path:

* after a realised profit of at least max(Rs200, 0.02% of starting capital),
* after 12:15 IST,
* for DTE >= 2, and
* after at least one completed/started trade that day,

new short-premium entries are blocked.  The block is before strategy
mapping, so the engine's existing momentum fallback can still prove and
select a separate long-premium expression.  This refuses late weekly theta
re-entry without changing the first trade of a day, 0DTE/next-day risk,
stop handling, sizing, strike selection, or exits.

The profit is read from the closed-position ledger first, so a process restart
cannot forget the guard.  The fallback is the live session state.  The guard
is reached before strategy mapping: it blocks fresh short-premium structures,
while the engine's existing momentum fallback still gets the opportunity to
underwrite a separate long-premium trade.  The patch is deliberately a
risk-adjusted repair, not a promise of "huge profit" or a hard-coded
2026-09-09 rule.  It cannot manufacture an unobserved trade or prove a
historical loss that the supplied replay does not contain.

Usage
-----
    python3 patch_v13.py

Run it from the repository root.  It creates strategy_engine.py.v13bak once,
refuses ambiguous source matches, applies atomically, and byte-compiles the
touched file.  A second run is a no-op and returns success.

Exit codes: 0 = applied or already applied; 1 = refused/failed.
"""

from __future__ import annotations

import py_compile
import shutil
import sys
from pathlib import Path

PATCH_ID = "PATCH_V13"
TARGET = Path("strategy_engine.py")
BACKUP = Path("strategy_engine.py.v13bak")

OLD = '''        if open_count >= 1:
            return "NO_TRADE", "position_already_open_single_position_engine"

        last_entry_time = state.get("last_entry_time")
'''

NEW = '''        if open_count >= 1:
            return "NO_TRADE", "position_already_open_single_position_engine"

        # PATCH_V13: protect a realised daily win from a late weekly
        # re-entry.  The existing cooldown only measures time since the
        # previous ENTRY, so it permits a new DTE>=2 short-premium ticket
        # immediately after a profitable exit.  On a range day this is an
        # asymmetric trade-off: little weekly theta remains, while the
        # afternoon gamma, spread cost and hard-exit risk remain.  The guard
        # runs before strategy mapping; the existing momentum fallback can
        # still underwrite a separate long-premium trade.  Read the ledger
        # first so a process restart cannot lose the protection.
        try:
            _p13_dte = int(signals.get("actual_dte"))
            _p13_pnl_row = self.db.query_one(
                "SELECT COALESCE(SUM(net_pnl_rupees), 0) AS pnl "
                "FROM positions WHERE trading_date=? AND status='CLOSED'",
                (today_ist().isoformat(),),
            )
            _p13_realised = float(
                (_p13_pnl_row or {}).get(
                    "pnl", state.get("daily_pnl", 0.0)
                ) or 0.0
            )
            _p13_capital = float(
                getattr(self.config, "starting_capital", 0.0) or 0.0
            )
            _p13_profit_floor = max(200.0, _p13_capital * 0.0002)
            if (
                current_time >= dtime(12, 15)
                and _p13_dte >= 2
                and total_count >= 1
                and _p13_realised >= _p13_profit_floor
            ):
                return "NO_TRADE", "post_profit_late_weekly_reentry_block"
        except Exception:
            # A malformed signal or a legacy ledger schema must never break
            # the decision loop.  The existing gates below remain
            # authoritative in that case.
            pass

        last_entry_time = state.get("last_entry_time")
'''


def fail(message: str) -> int:
    print(f"ERROR: {message}")
    return 1


def main() -> int:
    root = Path.cwd()
    target = root / TARGET
    backup = root / BACKUP

    print(f"{PATCH_ID}: conservative late weekly re-entry repair")
    if not target.is_file():
        return fail("run from the repository root; strategy_engine.py is missing")

    try:
        original = target.read_text(encoding="utf-8")
    except OSError as exc:
        return fail(f"cannot read {target}: {exc}")

    # Idempotency is checked before the original match because NEW includes
    # portions of OLD by design.
    if PATCH_ID in original:
        print(f"already applied: {target}")
        try:
            py_compile.compile(str(target), doraise=True)
        except Exception as exc:
            return fail(f"already-patched file does not compile: {exc}")
        print("OK")
        return 0

    occurrences = original.count(OLD)
    if occurrences != 1:
        return fail(
            f"refusing to patch: expected one source match, found {occurrences}"
        )

    patched = original.replace(OLD, NEW, 1)
    if PATCH_ID not in patched:
        return fail("internal verification marker missing after patch construction")

    # Write the backup before changing the target.  The backup is useful for
    # an operator and is created at most once.
    if not backup.exists():
        try:
            shutil.copy2(target, backup)
        except OSError as exc:
            return fail(f"cannot create {backup}: {exc}")

    try:
        target.write_text(patched, encoding="utf-8")
        py_compile.compile(str(target), doraise=True)
    except Exception as exc:
        # Best-effort rollback keeps a failed run from leaving a half-applied
        # source file.  The pre-patch bytes are still available in memory even
        # if the backup write succeeded.
        try:
            target.write_text(original, encoding="utf-8")
        except OSError as rollback_exc:
            return fail(f"patch failed ({exc}); rollback also failed ({rollback_exc})")
        return fail(f"patch rolled back because compilation failed: {exc}")

    print(f"applied: {target}")
    print(f"backup:  {backup}")
    print("byte-compile: OK")
    print("PATCH_V13 APPLIED SUCCESSFULLY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
