#!/usr/bin/env python3
"""
patch4.py — Two-step BUG-1 fix for strategy_engine.py

Step 1 (--scan): Finds and prints the exact vega_max block
                 so we can see what is actually in the file.

Step 2 (--apply): Applies the fix using the exact text found.

Usage:
    python patch4.py --scan    # find and print the block
    python patch4.py --apply   # apply the fix
    python patch4.py --verify  # check if already applied
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path


BASE_DIR   = Path(__file__).parent.resolve()
BACKUP_DIR = BASE_DIR / "patch_backups"
TIMESTAMP  = datetime.now().strftime("%Y%m%d_%H%M%S")
TARGET     = BASE_DIR / "strategy_engine.py"

VERIFY_MARKER = "_empty_book = abs(_existing_vega) < 100.0"


# ─────────────────────────────────────────────────────────────────────
# File helpers
# ─────────────────────────────────────────────────────────────────────

def _read() -> str:
    return TARGET.read_text(encoding="utf-8")


def _write_atomic(content: str) -> None:
    fd, tmp = tempfile.mkstemp(
        dir=TARGET.parent, suffix=".tmp", prefix=TARGET.name
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        shutil.move(tmp, TARGET)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _backup() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"{TARGET.name}.{TIMESTAMP}.bak"
    shutil.copy2(TARGET, dest)
    return dest


def _syntax_ok(src: str) -> bool:
    try:
        ast.parse(src)
        return True
    except SyntaxError as exc:
        print(f"  SYNTAX ERROR: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────
# Step 1 — scan: find the vega_max block
# ─────────────────────────────────────────────────────────────────────

def scan(src: str) -> None:
    """
    Print every line in _pre_trade_checks that mentions vega_max,
    plus 5 lines of context before and after, so we can see the
    exact text to match.
    """
    lines = src.splitlines()

    # Find _pre_trade_checks
    fn_start = None
    for i, line in enumerate(lines):
        if "async def _pre_trade_checks(" in line:
            fn_start = i
            break

    if fn_start is None:
        print("ERROR: _pre_trade_checks not found in file.")
        return

    print(f"\n_pre_trade_checks starts at line {fn_start + 1}")
    print("=" * 70)

    # Find all lines mentioning vega_max inside the function
    hits = []
    for i in range(fn_start, min(fn_start + 300, len(lines))):
        if "vega_max" in lines[i]:
            hits.append(i)

    if not hits:
        print("No 'vega_max' references found in _pre_trade_checks.")
        return

    print(f"Found {len(hits)} vega_max reference(s).\n")

    # Print context around each hit
    shown = set()
    for hit in hits:
        ctx_start = max(fn_start, hit - 5)
        ctx_end   = min(len(lines), hit + 20)
        for j in range(ctx_start, ctx_end):
            if j not in shown:
                marker = ">>>" if j == hit else "   "
                print(f"{marker} {j+1:5d}: {lines[j]}")
                shown.add(j)
        print()

    print("=" * 70)
    print("\nCopy the exact lines of the if-block shown above.")
    print("Then run:  python patch4.py --apply")


# ─────────────────────────────────────────────────────────────────────
# Step 2 — apply: replace the vega_max block
#
# We use a line-by-line approach:
#   1. Find the line that starts the if-block (contains "vega_max is not None")
#   2. Walk forward to find the matching "return False"
#   3. Replace those lines with the fixed block
#
# This approach is immune to exact whitespace differences because
# we detect indentation from the actual file content.
# ─────────────────────────────────────────────────────────────────────

def _find_vega_block_lines(lines: list[str]) -> tuple[int, int] | None:
    """
    Returns (start_idx, end_idx) — the line indices of the vega_max
    if-block (inclusive).  start_idx is the `if (` line.
    end_idx is the `return False` line.

    We identify the block by finding a run of lines that together
    contain all three of:
      - "vega_max is not None"
      - "port_greeks"  (covers .get() and ["vega"] variants)
      - "post_vega > vega_max"
    within a short window (the if-condition), followed eventually
    by a `return False` at one indent level deeper than the `if`.
    """
    # Find _pre_trade_checks first to limit search scope
    fn_start = 0
    for i, line in enumerate(lines):
        if "async def _pre_trade_checks(" in line:
            fn_start = i
            break

    search_end = min(fn_start + 400, len(lines))

    for i in range(fn_start, search_end):
        line = lines[i].strip()

        # Look for the opening of the vega_max if-block
        # It could be "if (" on its own or "if (vega_max..."
        if not (line == "if (" or line.startswith("if (")):
            continue

        # Collect the next 8 lines to check for our signature
        window_lines = lines[i: min(i + 8, search_end)]
        window_text  = " ".join(l.strip() for l in window_lines)

        if not (
            "vega_max is not None" in window_text
            and "port_greeks" in window_text
            and "post_vega > vega_max" in window_text
        ):
            continue

        # Found the if-block start.  Determine indentation.
        if_line     = lines[i]
        base_indent = len(if_line) - len(if_line.lstrip())
        body_indent = base_indent + 4

        # Walk forward to find `return False` at body_indent
        for j in range(i + 1, min(i + 60, search_end)):
            l       = lines[j]
            stripped = l.strip()
            if not stripped:
                continue
            cur_indent = len(l) - len(l.lstrip())

            if cur_indent == body_indent and stripped == "return False":
                return i, j

            # If we hit something at base_indent or less that is
            # not a comment or blank, the block has ended without
            # finding return False — unusual, keep searching.
            if (
                cur_indent <= base_indent
                and stripped
                and not stripped.startswith("#")
                and stripped != ")"
                and stripped != "):"
            ):
                break

    return None


def _build_fixed_block(base_indent: int) -> list[str]:
    """
    Build the replacement lines for the vega_max if-block.
    base_indent: number of spaces for the `if (` line.

    Returns a list of lines WITHOUT trailing newlines.
    The caller adds `\n` when joining.

    We use a literal em-dash character (U+2014) directly in the
    string — no escape sequences that could confuse re.sub or
    string concatenation.
    """
    b  = " " * base_indent         # 12 spaces (if-line level)
    i1 = " " * (base_indent + 4)   # 16 spaces (if-body)
    i2 = " " * (base_indent + 8)   # 20 spaces (nested)
    i3 = " " * (base_indent + 12)  # 24 spaces (double-nested)

    # em-dash as a literal character — safe in any string context
    emdash = "\u2014"

    return [
        b  + "# BUG-1 FIX: vega_max is a PORTFOLIO cap, not a",
        b  + "# per-trade requirement.  On an empty book the first",
        b  + "# credit spread adds only ~-380 to -550 vega, which is",
        b  + "# above vega_max=-1000 for MILD_SELL_VOL.  The old check",
        b  + "# fired because -450 > -1000 is mathematically True but",
        b  + "# semantically wrong: the book is empty, not over-limit.",
        b  + "# Threshold: abs < 100 covers floating-point residuals",
        b  + "# from recently closed positions.",
        b  + "_existing_vega = float(",
        i1 + "port_greeks.get(\"vega\", 0.0) or 0.0",
        b  + ")",
        b  + "_empty_book = abs(_existing_vega) < 100.0",
        b  + "if (",
        i1 + "vega_max is not None",
        i1 + "and not _empty_book",
        i1 + "and post_vega > vega_max",
        b  + "):",
        i1 + "logger.warning(",
        i2 + "f\"Pre-trade: vega above max: \"",
        i2 + "f\"post={post_vega:.0f} > max={vega_max} \"",
        i2 + "f\"" + emdash + " blocking entry\"",
        i1 + ")",
        i1 + "self._pretrade_fail_reason = (",
        i2 + "f\"VEGA_GATE_MAX\"",
        i2 + "f\":post={post_vega:.0f}\"",
        i2 + "f\":max={vega_max}\"",
        i1 + ")",
        i1 + "logger.info(",
        i2 + "\"VEGA_DIAGNOSTIC | \"",
        i2 + "f\"portfolio={_existing_vega:.0f} | \"",
        i2 + "f\"candidate={new_greeks['vega']:.0f} | \"",
        i2 + "f\"post={post_vega:.0f} | \"",
        i2 + "f\"min={vega_min} | \"",
        i2 + "f\"max={vega_max} | \"",
        i2 + "f\"strategy={strategy_name}\"",
        i1 + ")",
        i1 + "if hasattr(self, \"_set_entry_diagnostic\"):",
        i2 + "self._set_entry_diagnostic(",
        i3 + "\"PRETRADE_FAILED\",",
        i3 + "\"Vega limit rejected candidate\",",
        i3 + "strategy=strategy_name,",
        i3 + "credit=None,",
        i3 + "pretrade=\"FAILED\",",
        i3 + "execution=\"NOT_RUN\",",
        i3 + "vega_portfolio=_existing_vega,",
        i3 + "vega_candidate=new_greeks[\"vega\"],",
        i3 + "vega_post=post_vega,",
        i3 + "vega_min=vega_min,",
        i3 + "vega_max=vega_max,",
        i2 + ")",
        i1 + "return False",
    ]


def apply_fix(src: str) -> str:
    """Apply BUG-1 fix.  Returns patched source."""
    if VERIFY_MARKER in src:
        return src  # already applied — idempotent

    lines = src.splitlines()
    coords = _find_vega_block_lines(lines)

    if coords is None:
        raise ValueError(
            "Could not locate the vega_max if-block.\n"
            "Run  python patch4.py --scan  to inspect the file,\n"
            "then apply the fix manually."
        )

    start_idx, end_idx = coords

    # Determine base indent from the actual if-line in the file
    if_line     = lines[start_idx]
    base_indent = len(if_line) - len(if_line.lstrip())

    replacement_lines = _build_fixed_block(base_indent)

    # Rebuild the file: lines before + replacement + lines after
    new_lines = (
        lines[:start_idx]
        + replacement_lines
        + lines[end_idx + 1:]
    )
    return "\n".join(new_lines) + "\n"


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="patch4.py — BUG-1 vega gate fix"
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Print the vega_max block as it exists in the file",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the BUG-1 fix",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --apply: show result without writing",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Check if BUG-1 fix is already applied",
    )
    args = parser.parse_args()

    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found")
        return 1

    src = _read()

    # ── verify ──────────────────────────────────────────────────────
    if args.verify:
        if VERIFY_MARKER in src:
            print("BUG-1: ALREADY APPLIED")
            return 0
        else:
            print("BUG-1: NOT YET APPLIED")
            return 1

    # ── scan ────────────────────────────────────────────────────────
    if args.scan:
        scan(src)
        return 0

    # ── apply ───────────────────────────────────────────────────────
    if args.apply:
        print(f"\nNIFTY Engine Patch4 — BUG-1 — {TIMESTAMP}")
        print("=" * 60)
        print(f"File: {TARGET.name}")

        if VERIFY_MARKER in src:
            print("Status: ALREADY APPLIED — nothing to do")
            return 0

        try:
            patched = apply_fix(src)
        except ValueError as exc:
            print(f"Status: FAILED\n  {exc}")
            return 1
        except Exception as exc:
            print(f"Status: ERROR — {exc}")
            return 1

        if not (VERIFY_MARKER in patched):
            print("Status: VERIFY FAILED — marker not in patched source")
            return 1

        if not _syntax_ok(patched):
            print("Status: SYNTAX ERROR — NOT written")
            return 1

        if args.dry_run:
            print("Status: OK (dry-run — not written)")
            # Show changed lines
            orig_lines    = src.splitlines()
            patched_lines = patched.splitlines()
            max_len = max(len(orig_lines), len(patched_lines))
            changed = []
            for k in range(min(len(orig_lines), len(patched_lines))):
                if orig_lines[k] != patched_lines[k]:
                    changed.append(k)
            if changed:
                first = max(0, changed[0] - 2)
                last  = min(len(patched_lines), changed[-1] + 3)
                print(f"\nChanged region (lines {first+1}-{last}):")
                for ln in patched_lines[first:last]:
                    print(f"  {ln}")
            return 0

        backup = _backup()
        _write_atomic(patched)
        print("Status: OK")
        print(f"Written: {TARGET.name}")
        print(f"Backup : {backup.name}")
        print("=" * 60)
        print("BUG-1 fix applied successfully.")
        return 0

    # No flag given
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())