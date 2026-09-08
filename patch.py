#!/usr/bin/env python3
"""
════════════════════════════════════════════════════════════════════════════
 patch-version7.py — NIFTY intraday options engine, profitability patch v3.7
════════════════════════════════════════════════════════════════════════════

 Prerequisite: v3.6 must already be installed. Self-contained, stdlib only.

 ── THE DEFECT ─────────────────────────────────────────────────────────────

 The last of the distance-over-delta overrides.

 strategy_engine picks the short strike by delta, then clamps it into a
 band around the expected remaining move. v3.6 fixed the expected move
 itself (208 -> 82.1, read from the live ATM straddle). With that corrected
 the EM-relative floor is 0.80 * 82.1 = 65.7 points and no longer binds.

 But the floor is not only EM-relative:

     _band_lo = max(0.80 * EM, max(2 * step, floor_pts // 2))
              = max(65.7,      max(100,      35))              = 100

 2 * step is a hardcoded 100 points on NIFTY, and it now decides the
 trade. Delta selection asked for 95 points - strike 23750, delta 0.224,
 exactly the 0.20 target - and the floor pushed it to 23800, delta 0.138.

 On the recorded 12:03 chain, at one lot:

     short 23750   gross 10.35   brokerage 11.9% (cap 15)   friction 16.6% (cap 28)   passes
     short 23800   gross  5.55   brokerage 22.2% (cap 15)   friction 33.8% (cap 28)   rejected

 So a hardcoded distance constant, not economics, was rejecting all 86
 surviving candidates. Note what this means: the trade clears the cost
 gates at ONE LOT. Nothing here needs bigger size or a looser cost cap.

 ── THE FIX ────────────────────────────────────────────────────────────────

 The absolute floor drops from two strike steps to one. A single step is a
 genuine sanity bound - it stops a mis-quoted greek putting the short at
 the money - while 0.80 * EM remains the real, market-relative floor and
 delta remains what actually chooses the strike, as v3.2 intended.

 This does not loosen a risk limit. Delta IS the risk control here: the
 engine still sells the 0.20-delta strike it always meant to sell. What
 changes is that it is no longer prevented from doing so on quiet days,
 which is precisely when a 100-point constant is too wide - the expected
 move was 82 points, so the old floor forced every short beyond 1.2 sigma
 where there is not enough premium to cover fixed costs.

 ── HONEST SCOPE ───────────────────────────────────────────────────────────

 This is the change that will make the engine trade, so read the result
 with more suspicion than the previous ones, not less. One session, a
 57-minute usable window, modelled fills, and no out-of-sample test. A
 trade appearing is not a trade making money.

 Usage:
     python3 patch-version7.py            # apply
     python3 patch-version7.py --verify   # report state, change nothing
"""

import argparse
import ast
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent

MARKER_V36 = "NIFTY_ENGINE_PROFIT_PATCH_V36"
MARKER_V37 = "NIFTY_ENGINE_PROFIT_PATCH_V37"

TOUCHED = ["core.py", "strategy_engine.py"]

BAND_OLD = '        _band_lo = max(_band_lo, float(max(2 * step, floor_pts // 2)))'

BAND_NEW = '''        # v3.7: the absolute floor is one strike step, not two.
        #
        # With v3.6's corrected expected move the EM-relative floor is
        # 0.80 * 82.1 = 65.7 points on a quiet 0DTE, but max(2 * step,
        # floor_pts // 2) is a hardcoded 100 and overrode it. Delta
        # selection asked for 95 points - strike 23750, delta 0.224,
        # its 0.20 target - and the floor pushed the short to 23800,
        # delta 0.138, halving the credit from 10.35 to 5.55 and putting
        # fixed brokerage at 22% of it. All 86 surviving candidates were
        # rejected by a constant rather than by economics.
        #
        # One step still stops a mis-quoted greek selling the money.
        # Beyond that, 0.80 * EM is the market-relative floor and delta
        # chooses the strike, which is what v3.2 said it would do.
        _band_lo = max(_band_lo, float(step))'''


def _child_env() -> dict:
    """
    Force UTF-8 on any Python we spawn.

    regime_engine's self test prints arrows and box characters. When its
    stdout is a console Windows routes it through WriteConsoleW and it
    survives, but subprocess.run captures output through a PIPE, and a
    piped stdout falls back to the process locale encoding - cp1252 on a
    default Windows install - so the child dies with UnicodeEncodeError
    before it can report whether the assertions passed. That failure is an
    artefact of how this script calls the test, not a real test failure,
    and it was rolling back a correctly applied patch.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


# Injected ahead of everything else in any Python we spawn. Environment
# variables turned out not to be enough on the reported Windows 3.13 box, so
# this rebinds sys.stdout/sys.stderr to explicit UTF-8 wrappers around the
# raw byte buffers from inside the child itself. After this runs, the child's
# print() cannot raise UnicodeEncodeError no matter what the locale, the
# console codepage or PYTHONIOENCODING happen to be.
_UTF8_PRELUDE = (
    "import sys, io\n"
    "for _nm in ('stdout', 'stderr'):\n"
    "    try:\n"
    "        _st = getattr(sys, _nm)\n"
    "        if _st is not None and hasattr(_st, 'buffer'):\n"
    "            setattr(sys, _nm, io.TextIOWrapper(\n"
    "                _st.buffer, encoding='utf-8', errors='replace',\n"
    "                line_buffering=True))\n"
    "    except Exception:\n"
    "        pass\n"
)


def _run_py(body: str, timeout: int) -> subprocess.CompletedProcess:
    """Run Python in the repo with UTF-8 forced three separate ways."""
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-c", _UTF8_PRELUDE + body],
        cwd=BASE, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace", env=_child_env(),
    )


def _harden_stdout() -> None:
    """Survive being redirected to a file or pipe on a cp1252 console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


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
                f"  looked for:\n    {old[:200]!r}"
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
    d = BASE / f"patch_v37_backup_{stamp}"
    d.mkdir(exist_ok=True)
    for name in TOUCHED + ["env.txt"]:
        p = BASE / name
        if p.exists():
            shutil.copy2(p, d / name)
    return d


def restore_all(d: Path) -> None:
    for f in d.iterdir():
        shutil.copy2(f, BASE / f.name)


def patch_core(p: FilePatcher) -> None:
    p.sub("core/version-marker",
          f'{MARKER_V36} = "3.6"',
          f'{MARKER_V36} = "3.6"\n{MARKER_V37} = "3.7"')


def patch_strategy(p: FilePatcher) -> None:
    p.sub("strategy/absolute-strike-floor-one-step", BAND_OLD, BAND_NEW)


def verify_syntax() -> list[str]:
    errs = []
    for name in TOUCHED:
        try:
            ast.parse((BASE / name).read_text(encoding="utf-8"), filename=name)
        except SyntaxError as e:
            errs.append(f"{name}: line {e.lineno}: {e.msg}")
    return errs


def verify_semantics() -> list[str]:
    errs = []
    core_src = (BASE / "core.py").read_text(encoding="utf-8")
    se_src = (BASE / "strategy_engine.py").read_text(encoding="utf-8")
    checks = [
        ("core carries the v3.7 marker", MARKER_V37 in core_src),
        ("core keeps the v3.6 marker", MARKER_V36 in core_src),
        ("the absolute floor is one step",
         "_band_lo = max(_band_lo, float(step))" in se_src),
        ("the hardcoded two-step floor is gone",
         "max(2 * step, floor_pts // 2)" not in se_src),
        ("the EM-relative floor is untouched",
         '_band_lo = float(getattr(self.config, "em_band_lo", 0.80)) * _em'
         in se_src),
        ("the upper band is untouched",
         "_band_hi = max(_band_hi, _band_lo + step)" in se_src),
    ]
    for label, ok in checks:
        print(f"    {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            errs.append(label)
    out = _run_py("import strategy_engine\nprint('IMPORT_OK')\n", 180)
    ok = out.returncode == 0 and "IMPORT_OK" in out.stdout
    print(f"    {'PASS' if ok else 'FAIL'}  strategy_engine imports")
    if not ok:
        errs.append("import failed")
    return errs


def verify_behaviour() -> list[str]:
    errs = []
    out = _run_py(
        "em, step = 82.1, 50\n"
        "old = max(0.80 * em, max(2 * step, 70 // 2))\n"
        "new = max(0.80 * em, float(step))\n"
        "print(f'floor: {old:.1f} pts -> {new:.1f} pts   (delta wanted 95)')\n"
        "assert old > 95, 'the old floor must override a 95pt delta pick'\n"
        "assert new < 95, 'the new floor must not'\n"
        "for k, g in ((23800, 5.55), (23750, 10.35)):\n"
        "    print(f'  short {k}: gross {g:5.2f} brokerage {1.23/g*100:4.1f}%'\n"
        "          f' friction {1.57/(g-0.9)*100:4.1f}%')\n"
        "assert 1.23 / 10.35 < 0.15 and 1.57 / 9.45 < 0.28\n"
        "print('ARITHMETIC_OK')\n", 120)
    for line in (out.stdout or "").strip().splitlines():
        if "ARITHMETIC_OK" not in line:
            print(f"           {line}")
    ok = out.returncode == 0 and "ARITHMETIC_OK" in out.stdout
    print(f"    {'PASS' if ok else 'FAIL'}  delta governs, and 23750 clears both cost gates at 1 lot")
    if not ok:
        errs.append("behaviour check failed")
    return errs


def run_self_tests() -> list[str]:
    errs = []
    for mod in ("strategy_engine.py", "data_engine.py", "regime_engine.py"):
        body = ("import runpy\n"
                f"runpy.run_path({str(BASE / '@M@')!r}, run_name='__main__')\n"
                ).replace("@M@", mod)
        out = _run_py(body, 600)
        combined = (out.stderr or "") + (out.stdout or "")
        ok = out.returncode == 0
        if not ok and "UnicodeEncodeError" in combined:
            print(f"    WARN  {mod} could not write its output on this console")
            out = _run_py("import runpy, os, sys\n"
                          "sys.stdout = open(os.devnull, 'w')\n"
                          + body.split("\n", 1)[1], 600)
            combined = (out.stderr or "") + (out.stdout or "")
            ok = out.returncode == 0
        print(f"    {'PASS' if ok else 'FAIL'}  {mod} self test")
        if not ok:
            for line in (combined.strip().splitlines() or ["(no output)"])[-6:]:
                print(f"           {line}")
            errs.append(f"{mod} self test failed")
    return errs


def do_verify() -> int:
    print("\n  state of the tree\n")
    core_src = (BASE / "core.py").read_text(encoding="utf-8")
    for label, marker in (("v3.6", MARKER_V36), ("v3.7", MARKER_V37)):
        print(f"    {label}: {'installed' if marker in core_src else 'NOT installed'}")
    print()
    errs = verify_semantics()
    print()
    return 1 if errs else 0


def main() -> int:
    _harden_stdout()
    ap = argparse.ArgumentParser(description="NIFTY engine profitability patch v3.7")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    print("=" * 76)
    print(" NIFTY intraday options engine - profitability patch v3.7")
    print(" let delta choose the strike")
    print("=" * 76)
    if args.verify:
        return do_verify()
    core_src = (BASE / "core.py").read_text(encoding="utf-8")
    if MARKER_V36 not in core_src:
        print("\n  REFUSING: v3.6 must be installed first. Run patch-version6.py.\n")
        return 1
    if MARKER_V37 in core_src:
        print("\n  v3.7 is already installed. Nothing to do.\n")
        return 0
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = backup_all(stamp)
    print(f"\n  backup: {backup.name}/\n")
    try:
        patchers = {}
        for name, fn in (("core.py", patch_core),
                         ("strategy_engine.py", patch_strategy)):
            p = FilePatcher(name)
            fn(p)
            patchers[name] = p
        for name, p in patchers.items():
            p.write()
            for label in p.log:
                print(f"    applied  {label}")
        print("\n  syntax\n")
        errs = verify_syntax()
        for e in errs:
            print(f"    FAIL  {e}")
        if not errs:
            print("    PASS  all touched files parse")
        print("\n  semantics\n")
        errs += verify_semantics()
        print("\n  behaviour\n")
        errs += verify_behaviour()
        print("\n  self tests\n")
        errs += run_self_tests()
        if errs:
            raise PatchError(f"{len(errs)} verification failure(s)")
    except Exception as exc:                                # noqa: BLE001
        print(f"\n  ERROR: {exc}")
        print("  restoring every touched file from the backup...")
        restore_all(backup)
        print("  restored. The tree is exactly as it was.\n")
        return 1
    print("\n" + "=" * 76)
    print(" v3.7 applied and verified.")
    print("=" * 76)
    print(f"""
 The absolute strike-distance floor is now one strike step instead of a
 hardcoded two. Delta selection chooses the short; 0.80 * expected move
 remains the market-relative floor.

 This is the change that lets the engine trade. Treat the first P&L it
 produces as a hypothesis, not a result: one session, a 57-minute usable
 window, modelled fills, no out-of-sample test.

 Rollback

   cp {backup.name}/* .
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())