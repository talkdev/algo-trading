#!/usr/bin/env python3
# ============================================================================
#  patch-version3.py
#  v3.2 -> v3.3   SINGLE-DEFECT REGRESSION FIX
# ============================================================================
#
#  WHAT THIS FIXES, AND HOW IT WAS FOUND
#  -------------------------------------
#  v3.2 changed day_move_used_pct from "realised range as a percentage of the
#  WHOLE-day opening straddle" to "realised range against the range priced for
#  the ELAPSED part of the session", and moved the block threshold 60 -> 125.
#
#  The normalisation was the right idea. The calibration that went with it was
#  wrong, and wrong in a way that silently disabled the entire engine.
#
#  This was not found by reading the code. It was found by measurement:
#  backtest_engine.py replayed a recorded session (2026-09-08) against both
#  versions and the entry funnel changed like this:
#
#      v3.1 baseline    regime verdict blocked 678/787   ->  109 reached sizing
#      v3.2 patched     regime verdict blocked 789/789   ->    0 reached sizing
#
#  Every one of the 109 cycles that used to reach strike selection was now
#  classified VOL_NEUTRAL. RANGE_UNCLEAR and VOL_BUY_OPTIONS disappeared from
#  the census entirely, because the volatility branch short-circuits before
#  the positioning branch is ever consulted.
#
#  THE DEFECT
#  ----------
#  data_engine._compute_day_move_used divides a RANGE by a STRADDLE:
#
#      numerator    day_high - day_low            <- a high-low RANGE
#      denominator  opening_straddle * sqrt(t)    <- prices |displacement|
#
#  Those two quantities are not on the same scale. For a driftless diffusion
#
#      E[range]          = sqrt(8T/pi) * sigma
#      E[|displacement|] = sqrt(2T/pi) * sigma
#      ratio             = 2.0   exactly, in continuous time
#
#  and an ATM straddle is priced at very nearly E[|displacement|]
#  (0.7979 * sigma * sqrt(T) * S under Black-Scholes).
#
#  Monte Carlo over 40,000 paths of 375 one-minute steps - i.e. sampled the
#  way a real session is actually observed - gives 1.933 rather than the
#  continuous-time 2.0, because discrete monitoring cannot see the true
#  extremum between bars. 1.933 is therefore the honest factor for this
#  engine, which reads 1-minute bars.
#
#  So a session running EXACTLY as the market priced it scores ~193, not 100.
#  The v3.2 comment claiming "100 now means the day is running exactly as
#  priced" was simply false, and the 125 threshold it justified sits BELOW an
#  ordinary day. The gate fired on essentially every cycle of every session.
#
#  THE FIX
#  -------
#  Divide by the range-equivalent of the priced move rather than by the priced
#  displacement, so that the documented semantics become true:
#
#      denominator = opening_straddle * sqrt(elapsed_frac) * DAY_MOVE_RANGE_FACTOR
#
#  With DAY_MOVE_RANGE_FACTOR = 1.93, 100 genuinely means "running as priced"
#  and the existing 125 threshold recovers its intended meaning: block when
#  the session is running about a quarter hotter than the market paid for.
#  The factor is env-driven so it can be re-fitted from recorded sessions
#  without a code change.
#
#  SCOPE
#  -----
#  ONE defect. Nothing else is touched. That is deliberate: the whole point of
#  the measurement loop is that a change can be attributed, and a 53-edit
#  patch cannot be. Re-run the backtest after this and the funnel should show
#  the regime layer passing traffic again - at which point the credit_risk
#  and EV gates become measurable, which they currently are not.
#
#  WHAT THIS DOES NOT CLAIM
#  ------------------------
#  This does not make the engine profitable and is not evidence that it is.
#  It removes a gate that was blocking on a unit error. What the engine then
#  does with the traffic is an open question that needs 20+ recorded sessions
#  to answer.
#
#  SAFETY
#  ------
#    * refuses to run unless the v3.2 marker is present
#    * exits 0 if the v3.3 marker is already present (idempotent)
#    * backs every touched file up first; ANY failure restores all of them
#    * AST-parses every touched file, then runs semantic assertions
#
#  USAGE
#  -----
#      python patch-version3.py            apply
#      python patch-version3.py --verify   check an already-patched tree
#
# ============================================================================

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent

MARKER_V32 = "NIFTY_ENGINE_PROFIT_PATCH_V32"
MARKER_V33 = "NIFTY_ENGINE_PROFIT_PATCH_V33"

TOUCHED = ["core.py", "data_engine.py"]

RANGE_FACTOR = 1.93


# ═══════════════════════════════════════════════════════════════════════════
#  patcher
# ═══════════════════════════════════════════════════════════════════════════

class PatchError(RuntimeError):
    pass


class FilePatcher:
    """Exact-match, apply-once text substitution with a full audit trail."""

    def __init__(self, name: str):
        self.name = name
        self.path = BASE / name
        if not self.path.exists():
            raise PatchError(f"{name} not found in {BASE}")
        self.src = self.path.read_text(encoding="utf-8")
        self.original = self.src
        self.log: list[str] = []

    def sub(self, label: str, old: str, new: str) -> None:
        n = self.src.count(old)
        if n == 0:
            raise PatchError(
                f"[{self.name}] anchor not found for '{label}'.\n"
                f"  looked for:\n    {old[:160]!r}"
            )
        if n > 1:
            raise PatchError(
                f"[{self.name}] anchor for '{label}' matched {n} times; "
                f"it must be unique"
            )
        self.src = self.src.replace(old, new, 1)
        self.log.append(label)

    def contains(self, needle: str) -> bool:
        return needle in self.src

    def write(self) -> None:
        self.path.write_text(self.src, encoding="utf-8")


def backup_all(stamp: str) -> Path:
    d = BASE / f"patch_v33_backup_{stamp}"
    d.mkdir(exist_ok=True)
    for name in TOUCHED + ["env.txt"]:
        p = BASE / name
        if p.exists():
            shutil.copy2(p, d / name)
    return d


def restore_all(d: Path) -> None:
    for f in d.iterdir():
        shutil.copy2(f, BASE / f.name)


# ═══════════════════════════════════════════════════════════════════════════
#  edits
# ═══════════════════════════════════════════════════════════════════════════

def patch_core(p: FilePatcher) -> None:
    p.sub(
        "core/version-marker",
        f'{MARKER_V32} = "3.2"',
        f'{MARKER_V32} = "3.2"\n{MARKER_V33} = "3.3"',
    )

    p.sub(
        "core/config-field",
        "    day_move_used_block_pct:   float",
        "    day_move_used_block_pct:   float\n"
        "    # v3.3: day_move_used_pct divides a high-low RANGE by a straddle,\n"
        "    # and a straddle prices |displacement|, not range. For a driftless\n"
        "    # diffusion E[range]/E[|displacement|] = 2.0 in continuous time and\n"
        "    # 1.933 when sampled at 1-minute bars, which is how this engine\n"
        "    # observes the session. Without this factor a perfectly ordinary\n"
        "    # day scores ~193 against a 125 threshold and the volatility gate\n"
        "    # returns NEUTRAL on every cycle. Env-driven so it can be re-fitted.\n"
        "    day_move_range_factor:     float",
    )

    p.sub(
        "core/config-load",
        '        day_move_used_block_pct=_get_float(env, "DAY_MOVE_USED_BLOCK_PCT", 125.0),',
        '        day_move_used_block_pct=_get_float(env, "DAY_MOVE_USED_BLOCK_PCT", 125.0),\n'
        '        day_move_range_factor=min(max(\n'
        f'            _get_float(env, "DAY_MOVE_RANGE_FACTOR", {RANGE_FACTOR}), 1.0), 2.5),',
    )


def patch_data_engine(p: FilePatcher) -> None:
    old = (
        "        _frac_dm = min(max(_elapsed_dm / 375.0, 0.06), 1.0)\n"
        "        _straddle_ref = max(_straddle_ref * _math_dm.sqrt(_frac_dm), 12.0)"
    )
    new = (
        "        _frac_dm = min(max(_elapsed_dm / 375.0, 0.06), 1.0)\n"
        "        # ── v3.3: convert the priced DISPLACEMENT into a priced RANGE ──\n"
        "        # The numerator below is day_high - day_low, a range. A straddle\n"
        "        # prices E[|displacement|], not E[range]. For a driftless\n"
        "        # diffusion those differ by exactly 2.0 in continuous time, and\n"
        "        # by 1.933 when the path is observed at 1-minute bars as it is\n"
        "        # here. v3.2 omitted the conversion and asserted that 100 meant\n"
        "        # 'running exactly as priced'; the true figure was ~193, which\n"
        "        # sits above the 125 block threshold, so classify_volatility\n"
        "        # returned NEUTRAL on effectively every cycle of every session\n"
        "        # and no trade could ever be reached. Measured on a replayed\n"
        "        # session: the regime layer went from passing 109 of 787 cycles\n"
        "        # to passing 0 of 789.\n"
        "        _range_factor_dm = float(\n"
        f"            getattr(self.config, 'day_move_range_factor', {RANGE_FACTOR})\n"
        "        )\n"
        "        _straddle_ref = max(\n"
        "            _straddle_ref * _math_dm.sqrt(_frac_dm) * _range_factor_dm,\n"
        "            12.0,\n"
        "        )"
    )
    p.sub("data_engine/day-move-range-factor", old, new)


ENV_APPEND = f"""
# ── v3.3 ────────────────────────────────────────────────────────────────
# day_move_used_pct divides a high-low RANGE by a straddle, and a straddle
# prices |displacement| rather than range. The two differ by a factor of 2.0
# in continuous time and 1.933 at 1-minute sampling. Without this conversion
# an ordinary session scores ~193 against DAY_MOVE_USED_BLOCK_PCT=125 and the
# volatility gate blocks every cycle. With it, 100 means "running exactly as
# the market priced it" and 125 means "running about a quarter hotter".
DAY_MOVE_RANGE_FACTOR={RANGE_FACTOR}
"""


def migrate_env() -> None:
    env_path = BASE / "env.txt"
    if not env_path.exists():
        print("  env.txt not present - core.ENV_TEMPLATE carries the default")
        return
    text = env_path.read_text(encoding="utf-8")
    if "DAY_MOVE_RANGE_FACTOR" in text:
        print("  env.txt already current")
        return
    if not text.endswith("\n"):
        text += "\n"
    env_path.write_text(text + ENV_APPEND, encoding="utf-8")
    print(f"  env.txt: appended DAY_MOVE_RANGE_FACTOR={RANGE_FACTOR}")


# ═══════════════════════════════════════════════════════════════════════════
#  verification
# ═══════════════════════════════════════════════════════════════════════════

def verify_syntax() -> list[str]:
    errs = []
    for name in TOUCHED:
        p = BASE / name
        try:
            ast.parse(p.read_text(encoding="utf-8"), filename=name)
        except SyntaxError as e:
            errs.append(f"{name}: line {e.lineno}: {e.msg}")
    return errs


def verify_semantics() -> list[str]:
    """Assertions against the patched source and the imported config."""
    errs = []
    core_src = (BASE / "core.py").read_text(encoding="utf-8")
    de_src = (BASE / "data_engine.py").read_text(encoding="utf-8")

    checks = [
        ("core carries the v3.3 marker", MARKER_V33 in core_src),
        ("core keeps the v3.2 marker", MARKER_V32 in core_src),
        ("config declares day_move_range_factor",
         "day_move_range_factor:     float" in core_src),
        ("load_config reads DAY_MOVE_RANGE_FACTOR",
         'DAY_MOVE_RANGE_FACTOR"' in core_src),
        ("data_engine applies the range factor",
         "_range_factor_dm" in de_src),
        ("the factor multiplies the straddle reference",
         "_math_dm.sqrt(_frac_dm) * _range_factor_dm" in de_src),
        ("the bare v3.2 form is gone",
         "max(_straddle_ref * _math_dm.sqrt(_frac_dm), 12.0)" not in de_src),
        ("block threshold left at 125",
         'DAY_MOVE_USED_BLOCK_PCT", 125.0' in core_src),
    ]
    for label, ok in checks:
        if not ok:
            errs.append(label)

    # live config
    try:
        out = subprocess.run(
            [sys.executable, "-c",
             "import core;c=core.load_config();"
             "print(c.day_move_range_factor, c.day_move_used_block_pct)"],
            cwd=str(BASE), capture_output=True, text=True, timeout=120,
        )
        if out.returncode != 0:
            errs.append(f"load_config failed: {out.stderr.strip()[:200]}")
        else:
            f, b = (float(x) for x in out.stdout.split())
            if abs(f - RANGE_FACTOR) > 1e-9:
                errs.append(f"day_move_range_factor is {f}, expected {RANGE_FACTOR}")
            if abs(b - 125.0) > 1e-9:
                errs.append(f"day_move_used_block_pct is {b}, expected 125.0")
    except Exception as e:                                    # pragma: no cover
        errs.append(f"config import error: {e}")

    # arithmetic: an as-priced day must now land near 100, not near 193
    as_priced = 1.933 / RANGE_FACTOR * 100.0
    if not (95.0 <= as_priced <= 105.0):
        errs.append(
            f"an as-priced session would score {as_priced:.0f}, not ~100"
        )
    if as_priced >= 125.0:
        errs.append("an as-priced session would still trip the block threshold")

    return errs


# ═══════════════════════════════════════════════════════════════════════════
#  driver
# ═══════════════════════════════════════════════════════════════════════════

def do_verify() -> int:
    print()
    print("=" * 78)
    print("VERIFYING v3.3")
    print("=" * 78)
    errs = verify_syntax() + verify_semantics()
    if errs:
        for e in errs:
            print(f"  FAIL  {e}")
        print(f"\n  {len(errs)} check(s) failed.")
        return 1
    print("  all checks passed.")
    return 0


def main() -> int:
    print()
    print("=" * 78)
    print("PATCH v3.2 -> v3.3   (day-move range/displacement unit fix)")
    print("=" * 78)

    if "--verify" in sys.argv:
        return do_verify()

    core_src = (BASE / "core.py").read_text(encoding="utf-8")
    if MARKER_V33 in core_src:
        print(f"  Already at v3.3 (marker {MARKER_V33} found). Nothing to do.")
        return 0
    if MARKER_V32 not in core_src:
        print("  ERROR: this patch upgrades v3.2 -> v3.3 and the v3.2 marker")
        print(f"         ({MARKER_V32}) is not present in core.py.")
        print("         Apply patch-version2.py first.")
        return 2

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = backup_all(stamp)
    print(f"  backup -> {backup.name}")

    try:
        patchers = {n: FilePatcher(n) for n in TOUCHED}
        patch_core(patchers["core.py"])
        patch_data_engine(patchers["data_engine.py"])
        for p in patchers.values():
            p.write()
            for label in p.log:
                print(f"  applied  {label}")
        migrate_env()

        errs = verify_syntax()
        if errs:
            raise PatchError("syntax: " + "; ".join(errs))
        errs = verify_semantics()
        if errs:
            raise PatchError("semantics: " + "; ".join(errs))

    except Exception as e:
        print()
        print(f"  FAILED: {e}")
        print("  restoring every touched file from backup ...")
        restore_all(backup)
        print("  restored. The tree is exactly as it was.")
        return 1

    print()
    print("-" * 78)
    print("  v3.3 applied and verified.")
    print("-" * 78)
    print(f"""
  WHAT CHANGED
    day_move_used_pct now divides the realised high-low range by the range
    the market priced for the elapsed session, instead of by the priced
    DISPLACEMENT. 100 finally means what v3.2 claimed it meant.

  WHY IT MATTERED
    A perfectly ordinary session scored ~193 against a 125 block threshold,
    so classify_volatility returned NEUTRAL on every cycle. On a replayed
    session the regime layer passed 109 of 787 cycles before v3.2 and 0 of
    789 after it. This restores the traffic.

  WHAT TO DO NEXT
    1. python backtest_engine.py --from <a> --to <b>
       The funnel should show the regime layer passing cycles again, and
       credit_risk_ratio / EV becoming measurable for the first time since
       v3.2 was applied.
    2. Do NOT tune anything else until you have 20+ recorded sessions.
    3. Roll back at any time from {backup.name}

  THIS IS NOT EVIDENCE OF PROFITABILITY. It removes a unit error that was
  blocking the engine. Whether the engine makes money is still unmeasured.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())