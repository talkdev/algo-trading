#!/usr/bin/env python3
"""
patch_round3_fixes.py — D3a (IV rank samples near close instead of
open), D3c (cache the IV-rank DB read), S9 (stop clobbering PARTIAL
fill_status), S8 (use authoritative closed-position P&L fields).

Does NOT apply NEW-3 (re-verified as mathematically incorrect —
see conversation) or the larger deferred items (D1, C3, D4/D5,
TZ-1, M1/M2/D7) pending prioritization.

Usage:
    python patch_round3_fixes.py --dry-run
    python patch_round3_fixes.py
"""

import ast
import os
import sys
import shutil
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DRY_RUN = "--dry-run" in sys.argv
TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

FILES = {
    "data_manager": os.path.join(BASE_DIR, "data_manager.py"),
    "strategy_engine": os.path.join(BASE_DIR, "strategy_engine.py"),
}


class PatchError(Exception):
    pass


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def backup(path):
    bak = f"{path}.bak_{TIMESTAMP}"
    shutil.copy2(path, bak)
    return bak


def apply_text_patch(content, old, new, label):
    count = content.count(old)
    if count == 0:
        if new in content:
            print(f"  [SKIP] {label}: already applied")
            return content, False
        raise PatchError(
            f"Anchor text not found for '{label}'. Aborting this "
            f"specific patch to avoid corrupting the file."
        )
    if count > 1:
        raise PatchError(
            f"Anchor text for '{label}' found {count} times "
            f"(expected exactly 1) — aborting."
        )
    content = content.replace(old, new, 1)
    print(f"  [OK]   {label}")
    return content, True


def verify_syntax(path, content):
    try:
        ast.parse(content, filename=path)
        return True, None
    except SyntaxError as e:
        return False, str(e)


def patch_file(key, patches):
    path = FILES[key]
    if not os.path.exists(path):
        raise PatchError(f"{path} not found")

    print(f"\nPatching {os.path.basename(path)} "
          f"{'(dry-run)' if DRY_RUN else ''} ...")
    content = read(path)
    changed_any = False

    for label, old, new in patches:
        content, changed = apply_text_patch(content, old, new, label)
        changed_any = changed_any or changed

    if not changed_any:
        print(f"  Nothing to do for {os.path.basename(path)}.")
        return

    ok, err = verify_syntax(path, content)
    if not ok:
        raise PatchError(
            f"Syntax error after patching "
            f"{os.path.basename(path)}: {err}"
        )
    print(f"  Syntax check: OK")

    if DRY_RUN:
        print(f"  [DRY-RUN] Would write {os.path.basename(path)} "
              f"(no changes written)")
        return

    bak = backup(path)
    write(path, content)
    print(f"  Backed up original -> {os.path.basename(bak)}")
    print(f"  Wrote patched file  -> {os.path.basename(path)}")


# ════════════════════════════════════════════════════════════════
# DATA_MANAGER.PY — D3a, D3c
# ════════════════════════════════════════════════════════════════

_d3a_old = '''    def _save_daily_iv_close(self) -> None:
        """
        PATCH (D3): persist one IV_ATM close per calendar day so
        compute_iv_rank() can use a genuine multi-week percentile
        instead of the previous intraday-only history / hardcoded
        default.
        """
        if not self.iv_atm or self.iv_atm <= 0:
            return
        today = date.today()
        if self._last_iv_rank_date == today:
            return
        try:'''

_d3a_new = '''    def _save_daily_iv_close(self) -> None:
        """
        PATCH (D3a): previously saved only the FIRST iv_atm
        reading of the day (an opening snapshot, not a genuine
        close) because of the "already saved today" early-return.
        Now only writes/overwrites today's row within the last
        ~45 min of the trading session, so repeated calls in that
        window converge toward the actual closing IV instead of
        locking in an early-morning value.
        """
        if not self.iv_atm or self.iv_atm <= 0:
            return
        now_ist = datetime.now(self._IST)
        if (now_ist.hour, now_ist.minute) < (14, 45):
            return
        today = date.today()
        try:'''

_d3c_old = '''    def _load_iv_rank_history(self) -> List[float]:
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT iv_atm FROM iv_rank_history
                ORDER BY trading_date DESC LIMIT 60
            """)
            rows = cursor.fetchall()
            conn.close()
            return [r[0] for r in rows if r[0] and r[0] > 0]
        except sqlite3.OperationalError:
            return []
        except sqlite3.Error as e:
            logger.warning(f"_load_iv_rank_history error: {e}")
            return []'''

_d3c_new = '''    def _load_iv_rank_history(self) -> List[float]:
        # PATCH (D3c): cache for up to 60s — compute_iv_rank() can
        # run 2-3x per cycle from different callers
        # (_should_enter_new_position, _select_strategy,
        # _build_long_straddle), each previously opening a fresh
        # SQLite connection.
        now = datetime.now(self._IST)
        cached = getattr(self, "_iv_rank_cache", None)
        cached_at = getattr(self, "_iv_rank_cache_time", None)
        if (
            cached is not None
            and cached_at is not None
            and (now - cached_at).total_seconds() < 60
        ):
            return cached
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT iv_atm FROM iv_rank_history
                ORDER BY trading_date DESC LIMIT 60
            """)
            rows = cursor.fetchall()
            conn.close()
            result = [r[0] for r in rows if r[0] and r[0] > 0]
            self._iv_rank_cache = result
            self._iv_rank_cache_time = now
            return result
        except sqlite3.OperationalError:
            return []
        except sqlite3.Error as e:
            logger.warning(f"_load_iv_rank_history error: {e}")
            return []'''

data_manager_patches = [
    ("Sample IV rank near close, not open (D3a)",
     _d3a_old, _d3a_new),
    ("Cache IV rank history lookup (D3c)",
     _d3c_old, _d3c_new),
]


# ════════════════════════════════════════════════════════════════
# STRATEGY_ENGINE.PY — S9, S8
# ════════════════════════════════════════════════════════════════

_s9_long_old = '''            if not success:
                logger.warning(
                    f"Long leg failed: "
                    f"{leg.option_type} {leg.strike}"
                )
                await self._cancel_and_reverse(
                    filled_legs
                )
                return False
            leg.order_id    = order_id
            leg.fill_status = "COMPLETE"
            filled_legs.append(leg)'''

_s9_long_new = '''            if not success:
                logger.warning(
                    f"Long leg failed: "
                    f"{leg.option_type} {leg.strike}"
                )
                await self._cancel_and_reverse(
                    filled_legs
                )
                return False
            leg.order_id    = order_id
            # PATCH (S9): don't clobber the fill_status
            # _resolve_fill_result() already set correctly
            # (COMPLETE or PARTIAL) — this unconditional overwrite
            # was silently destroying the PARTIAL marker that
            # _rebalance_partial_fills() depends on.
            if leg.fill_status != "PARTIAL":
                leg.fill_status = "COMPLETE"
            filled_legs.append(leg)'''

_s9_short_old = '''            if not success:
                logger.warning(
                    f"Short leg failed: "
                    f"{leg.option_type} {leg.strike}"
                )
                await self._cancel_and_reverse(
                    filled_legs
                )
                return False
            leg.order_id    = order_id
            leg.fill_status = "COMPLETE"
            filled_legs.append(leg)'''

_s9_short_new = '''            if not success:
                logger.warning(
                    f"Short leg failed: "
                    f"{leg.option_type} {leg.strike}"
                )
                await self._cancel_and_reverse(
                    filled_legs
                )
                return False
            leg.order_id    = order_id
            # PATCH (S9): same fix as the long-legs loop above.
            if leg.fill_status != "PARTIAL":
                leg.fill_status = "COMPLETE"
            filled_legs.append(leg)'''

_s8_old = '''        slippage_total = sum(
            l.slippage_pts for l in position.legs
        )
        tx_costs  = self._calculate_transaction_costs(
            position
        )
        gross_pnl = position.realized_pnl
        net_pnl   = gross_pnl - tx_costs

        return {'''

_s8_new = '''        slippage_total = sum(
            l.slippage_pts for l in position.legs
        )
        # PATCH (S8): for a CLOSED position, use the authoritative
        # values _close_position() already computed and stored
        # (correctly including banked_pnl/banked_costs from any
        # partial one-sided closes) instead of recomputing
        # tx_costs fresh here, which silently drops banked_costs.
        # Open positions still compute live for real-time
        # unrealized tracking.
        if position.status == "CLOSED":
            gross_pnl = position.realized_pnl
            tx_costs  = position.transaction_costs
            net_pnl   = position.net_pnl
        else:
            tx_costs  = self._calculate_transaction_costs(
                position
            )
            gross_pnl = position.realized_pnl
            net_pnl   = gross_pnl - tx_costs

        return {'''

strategy_engine_patches = [
    ("Preserve PARTIAL fill_status in long-legs loop (S9)",
     _s9_long_old, _s9_long_new),
    ("Preserve PARTIAL fill_status in short-legs loop (S9)",
     _s9_short_old, _s9_short_new),
    ("Use authoritative closed-position P&L fields (S8)",
     _s8_old, _s8_new),
]


def main():
    print("=" * 70)
    print("ROUND-3 CONFIRMED FIXES"
          + (" [DRY RUN]" if DRY_RUN else ""))
    print("=" * 70)

    errors = []

    try:
        patch_file("data_manager", data_manager_patches)
    except PatchError as e:
        print(f"  [ABORTED] data_manager.py: {e}")
        errors.append(("data_manager", str(e)))

    try:
        patch_file("strategy_engine", strategy_engine_patches)
    except PatchError as e:
        print(f"  [ABORTED] strategy_engine.py: {e}")
        errors.append(("strategy_engine", str(e)))

    print("\n" + "=" * 70)
    if errors:
        print(f"COMPLETED WITH {len(errors)} FAILURE(S):")
        for key, err in errors:
            print(f"  - {key}.py: {err}")
        sys.exit(1)

    print("ALL PATCHES " + ("VALIDATED (dry-run)" if DRY_RUN
                             else "APPLIED SUCCESSFULLY"))


if __name__ == "__main__":
    main()