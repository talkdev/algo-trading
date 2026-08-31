#!/usr/bin/env python3
"""
patch_main_fix2.py — WS reconnect storm fix, tomorrow_is_expiry
scoping, NTP-in-paper-mode, midnight log rotation, and shutdown
timeout guards for main.py.

Rebuilt fresh and verified line-for-line against the confirmed
current state of main.py (as directly pasted in this
conversation) — the earlier patch_main_fix.py attempt assumed a
different state and was apparently never actually run/applied.

Does not touch or overlap with the EOD status-print patch
(different, non-overlapping sections of the file) — safe to run
before or after that one, in either order.

WHAT THIS FIXES
----------------
  1. Insert _guarded_ws_reconnect() helper (after
     _ensure_term_structure_expiry(), before _is_expiry_day()).
  2. Fix WS reconnect storm: market-hours check, concurrency
     guard, 15-min backoff after 3 consecutive full failures.
  3. Fix _end_of_day()'s tomorrow_is_expiry — was checking "is
     tomorrow ANY known expiry" instead of "is tomorrow THIS
     position's expiry".
  4. Run the NTP clock-sync check even in paper mode.
  5. Rotate the log file at midnight (TimedRotatingFileHandler)
     instead of one static file per process start.
  6. Timeout guards on the graceful-shutdown cancel-sweep and
     each live-mode position close.

Usage:
    python patch_main_fix2.py --dry-run
    python patch_main_fix2.py
"""

import ast
import os
import sys
import shutil
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DRY_RUN = "--dry-run" in sys.argv
TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
TARGET = os.path.join(BASE_DIR, "main.py")


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


# ════════════════════════════════════════════════════════════════
# Fix 1/2: insert _guarded_ws_reconnect() + fix the storm itself
# ════════════════════════════════════════════════════════════════

_func_insert_old = '''        logger.warning(
            "Term-structure expiry coverage: no expiry found "
            f"in {target_dte_low}-{target_dte_high} DTE window"
        )
    except Exception as e:
        logger.error(
            f"_ensure_term_structure_expiry error: {e}"
        )


def _is_expiry_day(
    dm: Optional[DataManager] = None,
) -> bool:'''

_func_insert_new = '''        logger.warning(
            "Term-structure expiry coverage: no expiry found "
            f"in {target_dte_low}-{target_dte_high} DTE window"
        )
    except Exception as e:
        logger.error(
            f"_ensure_term_structure_expiry error: {e}"
        )


async def _guarded_ws_reconnect(dm, ist_tz) -> None:
    """
    PATCH: prevents overlapping WS reconnect attempts and adds a
    15-min backoff after 3 consecutive full-failure cycles, so a
    persistent broker-side rejection (e.g. HTTP 403) doesn't turn
    into an unbounded retry storm.
    """
    try:
        await dm._reconnect_websocket()
    finally:
        dm._ws_reconnect_in_progress = False
        if dm.ws_connected:
            dm._ws_reconnect_fail_count = 0
        else:
            fail_count = getattr(
                dm, "_ws_reconnect_fail_count", 0
            ) + 1
            dm._ws_reconnect_fail_count = fail_count
            if fail_count >= 3:
                dm._ws_reconnect_backoff_until = (
                    datetime.now(ist_tz) + timedelta(minutes=15)
                )
                logger.warning(
                    f"WS: {fail_count} consecutive full "
                    f"failures — backing off reconnect "
                    f"attempts for 15 min"
                )


def _is_expiry_day(
    dm: Optional[DataManager] = None,
) -> bool:'''

_ws_storm_old = '''            if dm.kill_switch_triggered:
                logger.warning(
                    "WS disconnected — engine continues "
                    "with REST data, attempting reconnect"
                )
                dm.kill_switch_triggered = False
                asyncio.create_task(
                    dm._reconnect_websocket()
                )'''

_ws_storm_new = '''            if dm.kill_switch_triggered:
                # PATCH: market-hours check + concurrency guard +
                # backoff, to stop the WS reconnect storm (was
                # firing unconditionally every main-loop
                # iteration with no guard against overlapping
                # attempts).
                _now_ws = datetime.now(IST)
                _now_ws_time = _now_ws.time()
                _today_ws_str = _now_ws.date().strftime(
                    "%Y-%m-%d"
                )
                _is_trading_ws = (
                    _now_ws.date().weekday() < 5
                    and _today_ws_str
                    not in config.NSE_MARKET_HOLIDAYS
                )
                _is_mkt_ws = (
                    config.MARKET_OPEN
                    <= _now_ws_time
                    <= config.MARKET_CLOSE
                )
                _backoff_until = getattr(
                    dm, "_ws_reconnect_backoff_until", None
                )
                _in_progress = getattr(
                    dm, "_ws_reconnect_in_progress", False
                )
                if not (_is_trading_ws and _is_mkt_ws):
                    dm.kill_switch_triggered = False
                elif _in_progress:
                    dm.kill_switch_triggered = False
                elif (
                    _backoff_until
                    and _now_ws < _backoff_until
                ):
                    dm.kill_switch_triggered = False
                else:
                    logger.warning(
                        "WS disconnected — engine continues "
                        "with REST data, attempting reconnect"
                    )
                    dm.kill_switch_triggered = False
                    dm._ws_reconnect_in_progress = True
                    asyncio.create_task(
                        _guarded_ws_reconnect(dm, IST)
                    )'''

# ════════════════════════════════════════════════════════════════
# Fix 3: tomorrow_is_expiry scoping
# ════════════════════════════════════════════════════════════════

_tomorrow_old = '''        tomorrow_str = (
            date.today() + timedelta(days=1)
        ).isoformat()
        tomorrow_is_expiry = (
            tomorrow_str in dm.get_available_expiries()
        )'''

_tomorrow_new = '''        tomorrow_str = (
            date.today() + timedelta(days=1)
        ).isoformat()
        # PATCH: was checking "is tomorrow ANY known expiry" —
        # since NIFTY has a weekly expiry almost every Tuesday,
        # this was True on nearly every Monday regardless of
        # which contract this specific position is in. Now
        # correctly scoped to this position's own expiry.
        tomorrow_is_expiry = (
            tomorrow_str == position.expiry_date
        )'''

# ════════════════════════════════════════════════════════════════
# Fix 4: NTP check in paper mode
# ════════════════════════════════════════════════════════════════

_ntp_old = '''    # CHECK 2 — NTP clock sync (live mode only)
    if not config.PAPER_TRADING_MODE:
        try:
            import ntplib'''

_ntp_new = '''    # CHECK 2 — NTP clock sync
    # PATCH: run this even in paper mode — it validates the
    # system clock, which every time-gated decision in the
    # engine depends on, regardless of trading mode.
    if True:
        try:
            import ntplib'''

# ════════════════════════════════════════════════════════════════
# Fix 5: midnight log rotation
# ════════════════════════════════════════════════════════════════

_logrotate_old = '''    audit_file = os.path.join(
        config.LOG_DIR,
        f"audit_log_{today_str}.log",
    )
    file_handler = logging.FileHandler(
        audit_file, mode="a", encoding="utf-8"
    )'''

_logrotate_new = '''    audit_file = os.path.join(
        config.LOG_DIR,
        f"audit_log_{today_str}.log",
    )
    # PATCH: rotate at midnight so continuous multi-day operation
    # doesn't keep appending every subsequent day's logs into the
    # first day's file.
    from logging.handlers import TimedRotatingFileHandler
    file_handler = TimedRotatingFileHandler(
        audit_file,
        when="midnight",
        backupCount=90,
        encoding="utf-8",
    )
    file_handler.suffix = "%Y-%m-%d"'''

# ════════════════════════════════════════════════════════════════
# Fix 6: shutdown timeout guards
# ════════════════════════════════════════════════════════════════

_shutdown_cancel_old = '''    try:
        cancelled = await se.cancel_all_open_orders(
            context="SHUTDOWN_CANCEL_SWEEP"
        )
        if cancelled > 0:
            logger.warning(
                f"SHUTDOWN: Cancelled {cancelled} "
                f"open orders"
            )
        await asyncio.sleep(1.0)
    except Exception as e:
        logger.error(
            f"SHUTDOWN: Cancel sweep failed: {e}"
        )'''

_shutdown_cancel_new = '''    try:
        # PATCH: timeout guard so a hung network call during
        # shutdown can't stall the whole "graceful" sequence
        # indefinitely.
        cancelled = await asyncio.wait_for(
            se.cancel_all_open_orders(
                context="SHUTDOWN_CANCEL_SWEEP"
            ),
            timeout=30.0,
        )
        if cancelled > 0:
            logger.warning(
                f"SHUTDOWN: Cancelled {cancelled} "
                f"open orders"
            )
        await asyncio.sleep(1.0)
    except asyncio.TimeoutError:
        logger.error(
            "SHUTDOWN: Cancel sweep timed out after 30s "
            "— continuing shutdown anyway"
        )
    except Exception as e:
        logger.error(
            f"SHUTDOWN: Cancel sweep failed: {e}"
        )'''

_shutdown_close_old = '''    if not config.PAPER_TRADING_MODE:
        logger.info("Live mode: squaring off positions")
        for position in list(se.open_positions):
            try:
                await se._close_position(
                    position,
                    config.EXIT_REASONS["MANUAL"],
                    use_market=True,
                )
            except Exception as e:
                logger.error(
                    f"Shutdown close error "
                    f"{position.trade_id[:8]}: {e}"
                )'''

_shutdown_close_new = '''    if not config.PAPER_TRADING_MODE:
        logger.info("Live mode: squaring off positions")
        for position in list(se.open_positions):
            try:
                # PATCH: timeout guard per position close during
                # shutdown, same reasoning as the cancel sweep.
                await asyncio.wait_for(
                    se._close_position(
                        position,
                        config.EXIT_REASONS["MANUAL"],
                        use_market=True,
                    ),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                logger.error(
                    f"Shutdown close timed out "
                    f"{position.trade_id[:8]} — continuing"
                )
            except Exception as e:
                logger.error(
                    f"Shutdown close error "
                    f"{position.trade_id[:8]}: {e}"
                )'''


PATCHES = [
    ("Insert _guarded_ws_reconnect() helper",
     _func_insert_old, _func_insert_new),
    ("Fix WS reconnect storm",
     _ws_storm_old, _ws_storm_new),
    ("Fix tomorrow_is_expiry scoping",
     _tomorrow_old, _tomorrow_new),
    ("Run NTP check even in paper mode",
     _ntp_old, _ntp_new),
    ("Add midnight log rotation",
     _logrotate_old, _logrotate_new),
    ("Timeout guard on shutdown cancel-sweep",
     _shutdown_cancel_old, _shutdown_cancel_new),
    ("Timeout guard on shutdown position closes",
     _shutdown_close_old, _shutdown_close_new),
]


def main():
    print("=" * 70)
    print("MAIN.PY — WS STORM / TOMORROW-EXPIRY / NTP / LOG-ROTATE / "
          "SHUTDOWN FIX SCRIPT" + (" [DRY RUN]" if DRY_RUN else ""))
    print("=" * 70)

    if not os.path.exists(TARGET):
        print(f"ERROR: {TARGET} not found")
        sys.exit(1)

    content = read(TARGET)
    changed_any = False
    errors = []

    for label, old, new in PATCHES:
        try:
            content, changed = apply_text_patch(
                content, old, new, label
            )
            changed_any = changed_any or changed
        except PatchError as e:
            print(f"  [ABORTED] {label}: {e}")
            errors.append((label, str(e)))

    if errors:
        print(f"\n{len(errors)} PATCH(ES) FAILED — nothing written.")
        sys.exit(1)

    if not changed_any:
        print("\nNothing to do — already fully patched.")
        return

    ok, err = verify_syntax(TARGET, content)
    if not ok:
        print(f"\nSYNTAX ERROR after patching: {err}")
        print("Nothing written.")
        sys.exit(1)
    print("\nSyntax check: OK")

    if DRY_RUN:
        print("[DRY-RUN] Would write main.py (no changes written)")
        return

    bak = backup(TARGET)
    write(TARGET, content)
    print(f"Backed up original -> {os.path.basename(bak)}")
    print(f"Wrote patched file  -> main.py")
    print("\nALL PATCHES APPLIED SUCCESSFULLY")


if __name__ == "__main__":
    main()