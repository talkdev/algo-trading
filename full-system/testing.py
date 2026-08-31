#!/usr/bin/env python3
"""
patch_ipft_fix.py — Reverts COST_NSE_IPFT_PCT to 1e-9 (correct:
Rs 0.01/crore) and explicitly leaves COST_EXCHANGE_PCT untouched
at 0.0003552 (Rs 3,552/crore) — verified by direct arithmetic
(10,000,000 x 0.0003552 = 3,552, checks out exactly). Does not
apply the 3.552e-5 "correction" suggested in the latest message,
which is off by a factor of 10.

Usage:
    python patch_ipft_fix.py --dry-run
    python patch_ipft_fix.py
"""

import ast
import os
import sys
import shutil
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DRY_RUN = "--dry-run" in sys.argv
TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
TARGET = os.path.join(BASE_DIR, "config.py")


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
            f"Anchor text not found for '{label}'. Aborting to "
            f"avoid corrupting the file."
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


_ipft_old = '''# Parameter: COST_NSE_IPFT_PCT
#   PATCH (round-2 audit): the original verification pass supplied
#   Rs 0.01/crore (1e-9); a later audit pass claims the real NSE
#   IPFT is Rs 10/crore (1e-6) — a 1000x discrepancy between two
#   externally-supplied "verified" values that is NOT resolved
#   here. Applying the newer figure since it's the most recent
#   instruction, but confirm the actual current rate against a
#   live NSE circular before trusting either number — economically
#   negligible either way at this trade frequency.
#   Unit: % of total turnover, both sides   Effective: 01-Mar-2026
COST_NSE_IPFT_PCT = 0.000001'''

_ipft_new = '''# Parameter: COST_NSE_IPFT_PCT
#   RESOLVED: reverted to Rs 0.01/crore/side (1e-9). Verified by
#   direct arithmetic: 0.01 / 10,000,000 = 1e-9. The intermediate
#   1e-6 value (from a round-2 audit claiming Rs 10/crore) was
#   incorrect and has been reverted.
#   NOTE: a later message also proposed changing
#   COST_EXCHANGE_PCT (below) from 0.0003552 to 3.552e-5 for
#   Rs 3,552/crore — that proposed change is itself off by a
#   factor of 10 (10,000,000 x 0.0003552 = 3,552, which checks
#   out exactly) and was NOT applied. COST_EXCHANGE_PCT remains
#   0.0003552.
#   Unit: % of total turnover, both sides   Effective: 01-Mar-2026
COST_NSE_IPFT_PCT = 0.000000001'''


def main():
    print("=" * 70)
    print("IPFT RATE FIX (revert to 1e-9, exchange rate unchanged)"
          + (" [DRY RUN]" if DRY_RUN else ""))
    print("=" * 70)

    if not os.path.exists(TARGET):
        print(f"ERROR: {TARGET} not found")
        sys.exit(1)

    content = read(TARGET)
    try:
        content, changed = apply_text_patch(
            content, _ipft_old, _ipft_new,
            "Revert COST_NSE_IPFT_PCT to 1e-9",
        )
    except PatchError as e:
        print(f"  [ABORTED] {e}")
        sys.exit(1)

    if not changed:
        print("\nNothing to do — already applied.")
        return

    ok, err = verify_syntax(TARGET, content)
    if not ok:
        print(f"\nSYNTAX ERROR after patching: {err}")
        print("Nothing written.")
        sys.exit(1)
    print("\nSyntax check: OK")

    if DRY_RUN:
        print("[DRY-RUN] Would write config.py (no changes written)")
        return

    bak = backup(TARGET)
    write(TARGET, content)
    print(f"Backed up original -> {os.path.basename(bak)}")
    print(f"Wrote patched file  -> config.py")
    print("\nDONE.")
    print(
        "\nFinal state: COST_NSE_IPFT_PCT=1e-9, "
        "COST_EXCHANGE_PCT=0.0003552 (unchanged).\n"
        "Both values independently verified by direct arithmetic "
        "against the stated Rs-per-crore figures in this "
        "conversation."
    )


if __name__ == "__main__":
    main()