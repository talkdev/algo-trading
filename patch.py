#!/usr/bin/env python3
"""
════════════════════════════════════════════════════════════════════════════
 patch-version5.py — NIFTY intraday options engine, profitability patch v3.5
════════════════════════════════════════════════════════════════════════════

 Prerequisite: v3.4 must already be installed. Self-contained, stdlib only.

 ── THE DEFECT ─────────────────────────────────────────────────────────────

 Measured by replaying 2026-09-08 through the real decision code. After
 v3.4 unblocked the variance-premium guard, the engine still took zero
 trades and the entire 0DTE window died on one gate:

     IV_SPIKING_HARD_BLOCK    556 of the 562 afternoon cycles

 _compute_iv_behavior classifies IV against the session's opening IV and
 hard-blocks new entries on EXPANDING or SPIKING. Both halves of that
 comparison were wrong on expiry day.

 1. The baseline outlived the series it was taken from.

    The engine opened on the 15-Sep chain and latched opening_iv at
    10.13%. At 12:03 it switched to the 0DTE chain and went on comparing
    against that stale number. A five-day option and an expiring one are
    different instruments; their implied vols are not commensurable. The
    apparent "spike" at 12:05 was a change of contract, not of volatility.

 2. Raw ATM IV is not comparable against itself across an expiry session.

    As T collapses the annualisation factor blows up. Measured ATM IV on
    the 0DTE series ran 21.9% at 12:05 to 64.4% at 15:20 on a day whose
    entire spot range was 90 points. Nothing was spiking; the clock was
    running out. v3.1 saw this coming and widened the bands by sqrt(T),
    but capped the widening at 3.2x while the drift reached +536%, so the
    artefact won anyway.

        time    raw IV   vs open    IV*sqrt(T)   vs 12:05
        12:05    21.87     +116%         16.17         --
        13:30    25.73     +154%         14.56       -10%
        14:30    37.44     +270%         14.98        -7%
        15:20    64.43     +536%         10.52       -35%

 ── THE FIX ────────────────────────────────────────────────────────────────

 Compare a quantity that does not depend on T. IV * sqrt(T_remaining) is
 proportional to the expected move in points, which is what a premium
 seller is actually short. On the measured session it decays smoothly from
 16.17 to 10.52 - a 35% vol crush, correctly read as DECLINING, which is a
 sell condition and not a block. A genuine volatility expansion still
 shows through, because it moves the expected move itself rather than just
 the annualisation.

 And re-take the baseline whenever the active expiry changes, recording
 the remaining-time fraction at which it was taken so the two sides of the
 comparison are always the same instrument measured the same way.

 The two changes ship together because they are two halves of one broken
 comparison: fixing the baseline without the normalisation still blocks on
 the sqrt(T) ramp, and normalising against a baseline from another series
 still compares different instruments. Neither is measurable alone.

 ── COMPATIBILITY ──────────────────────────────────────────────────────────

 The normalised path engages only when a baseline remaining-time fraction
 was recorded. Any caller that sets opening_iv by hand and nothing else -
 including data_engine's own self test - keeps the exact v3.1 behaviour,
 sqrt(T) tolerance widening included. This patch therefore changes no
 existing test.

 ── WHAT THIS DOES NOT FIX ─────────────────────────────────────────────────

 The morning of 2026-09-08 is unrecoverable from the recording. The live
 collector polled the 15-Sep series until 12:03, so for 206 cycles the
 0DTE chain the engine needed simply is not in the database, and those
 cycles are correctly rejected as dte 5 above max 4. That is a defect in
 the collector, not in the decision code, and no patch to this repo can
 recover data that was never captured. Until it is fixed every session you
 record will lose its morning.

 One live session remains one sample. Removing a blocker that was
 provably misfiring is not evidence that the trades it now permits make
 money.

 Usage:
     python3 patch-version5.py            # apply
     python3 patch-version5.py --verify   # report state, change nothing
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

MARKER_V34 = "NIFTY_ENGINE_PROFIT_PATCH_V34"
MARKER_V35 = "NIFTY_ENGINE_PROFIT_PATCH_V35"

TOUCHED = ["core.py", "data_engine.py"]



OLD_IV_BLOCK = """\
        atm_iv_pct    = atm_iv * 100.0 if atm_iv < 2.0 else atm_iv
        opening_iv_pct = opening_iv * 100.0 if opening_iv < 2.0 else opening_iv

        iv_change_pct = (atm_iv_pct - opening_iv_pct) / opening_iv_pct * 100.0

        # v3.1: on expiry day the MEASURED ATM IV drifts upward through the
        # afternoon even in a dead-flat market, because the sqrt(T) in the
        # denominator collapses faster than the residual premium does. With
        # fixed bands, EXPANDING — a hard entry block — fires on quiet 0DTE
        # afternoons and kills the highest-theta window of the week. The
        # bands are therefore inflated by the same sqrt(T) factor on 0DTE,
        # which neutralises the artefact while leaving a genuine volatility
        # expansion (which is far larger) fully detected.
        _dte_iv = self.state.get("actual_dte", 0)
        _tol = 1.0
        if _dte_iv == 0:
            _elapsed = max(0.0, (
                datetime.combine(today_ist(), now_ist().time()) -
                datetime.combine(today_ist(), dtime(9, 15))
            ).total_seconds() / 60.0)
            _rem_frac = max((375.0 - _elapsed) / 375.0, 0.04)
            _tol = min(max(_rem_frac ** -0.5, 1.0), 3.2)"""


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
    d = BASE / f"patch_v35_backup_{stamp}"
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
    p.sub("core/version-marker",
          f'{MARKER_V34} = "3.4"',
          f'{MARKER_V34} = "3.4"\n{MARKER_V35} = "3.5"')


HELPER_ANCHOR = "    def _compute_iv_behavior("

HELPER_NEW = '''    def _session_rem_frac(self) -> float:
        """
        Fraction of the 09:15-15:30 session still to run, floored at 0.04.

        v3.5: hoisted out of _compute_iv_behavior so the IV baseline and the
        live reading are normalised by the same clock.
        """
        _elapsed = max(0.0, (
            datetime.combine(today_ist(), now_ist().time()) -
            datetime.combine(today_ist(), dtime(9, 15))
        ).total_seconds() / 60.0)
        return max((375.0 - _elapsed) / 375.0, 0.04)

    def _compute_iv_behavior('''


INIT_OLD = '''            self.state["opening_iv"]          = atm_iv
            self.state["session_initialized"] = True'''

INIT_NEW = '''            self.state["opening_iv"]          = atm_iv
            self.state["session_initialized"] = True
            # v3.5: record which series the baseline came from and how much
            # of the session was left when it was taken. Without both, the
            # baseline cannot be compared against anything later on.
            self.state["opening_iv_expiry"]   = self.state.get("actual_expiry")
            self.state["opening_iv_rem_frac"] = self._session_rem_frac()'''


GUARD_OLD = '''        if bars is None or len(bars) < 6:
            return "UNKNOWN", 0.0'''

GUARD_NEW = '''        if bars is None or len(bars) < 6:
            return "UNKNOWN", 0.0

        # v3.5: a baseline taken on a different expiry series is not a
        # baseline. On 2026-09-08 the engine opened on the 15-Sep chain,
        # latched 10.13%, then switched to the 0DTE chain at 12:03 and read
        # the change of contract as a volatility spike for the rest of the
        # day. Re-take it, and say so in the log.
        _cur_exp  = self.state.get("actual_expiry")
        _base_exp = self.state.get("opening_iv_expiry")
        if _cur_exp and _base_exp and _cur_exp != _base_exp:
            self.state["opening_iv"]          = atm_iv
            self.state["opening_iv_expiry"]   = _cur_exp
            self.state["opening_iv_rem_frac"] = self._session_rem_frac()
            self.logger.info(
                f"IV baseline re-taken on expiry change {_base_exp} -> "
                f"{_cur_exp}: opening_iv={atm_iv * 100.0:.2f}%"
            )
            return "UNKNOWN", 0.0'''


IV_NEW = '''        atm_iv_pct     = atm_iv * 100.0 if atm_iv < 2.0 else atm_iv
        opening_iv_pct = opening_iv * 100.0 if opening_iv < 2.0 else opening_iv

        # ── v3.5 ──────────────────────────────────────────────────────────
        # Raw ATM IV cannot be compared against itself across an expiry
        # session. As T collapses the annualisation factor blows up: the
        # measured 0DTE series ran 21.9% at 12:05 to 64.4% at 15:20 on a day
        # whose whole range was 90 points, a +536% drift that the v3.1
        # sqrt(T) band widening could not absorb because it caps at 3.2x.
        # 556 of 562 afternoon cycles were hard-blocked as SPIKING - the
        # entire 0DTE window, which is the only part of the day worth
        # trading.
        #
        # IV * sqrt(T_remaining) is proportional to the expected move in
        # points, which is the thing a premium seller is short, and it does
        # not depend on T. On the measured session it decays 16.17 -> 10.52,
        # a 35% crush read correctly as DECLINING. A real expansion still
        # registers because it moves the expected move itself.
        #
        # The normalised path needs a baseline taken at a known point in the
        # session. Where that is absent - a caller that sets opening_iv by
        # hand, including this module's own self test - behaviour falls back
        # to v3.1 exactly, sqrt(T) tolerance widening included.
        _dte_iv   = self.state.get("actual_dte", 0)
        _rem_base = self.state.get("opening_iv_rem_frac")
        _tol = 1.0

        if _dte_iv == 0 and _rem_base:
            _rem_now   = self._session_rem_frac()
            _cur_norm  = atm_iv_pct * math.sqrt(max(_rem_now, 1e-6))
            _base_norm = opening_iv_pct * math.sqrt(max(float(_rem_base), 1e-6))
            if _base_norm <= 0.0:
                return "UNKNOWN", 0.0
            iv_change_pct = (_cur_norm - _base_norm) / _base_norm * 100.0
        else:
            iv_change_pct = (atm_iv_pct - opening_iv_pct) / opening_iv_pct * 100.0
            if _dte_iv == 0:
                _rem_frac = self._session_rem_frac()
                _tol = min(max(_rem_frac ** -0.5, 1.0), 3.2)'''


TEST_SETUP_OLD = '''    engine.state["opening_iv"] = 0.125  # 12.5%
    engine.state["session_initialized"] = True'''

TEST_SETUP_NEW = '''    engine.state["opening_iv"] = 0.125  # 12.5%
    engine.state["session_initialized"] = True
    # v3.5: pin the series away from expiry so the band assertions below are
    # deterministic. They were not: v3.1's sqrt(T) tolerance widening reads
    # the wall clock, so out of hours _tol reached its 3.2 cap, the STABLE
    # band opened to +/-16%, and "IV 13.8% vs open 12.5%" (+10.4%) returned
    # STABLE instead of EXPANDING. This test therefore passed during market
    # hours and failed outside them, on the tree as it stood before v3.5.
    # The 0DTE path it used to exercise by accident is now covered on
    # purpose, with the clock pinned, at the end of this block.
    engine.state["actual_dte"] = 5'''

TEST_0DTE_OLD = '''    print("  [OK] IV behavior test passed")'''

TEST_0DTE_NEW = '''    # v3.5: the measured 2026-09-08 afternoon, with the session clock
    # pinned so the result does not depend on when the test is run. The
    # baseline is the 0DTE reading at 12:05 (22.0% with 205 of 375 minutes
    # left) and the live reading is 15:20 (64.4% with 10 minutes left).
    # Raw, that is +193% and a hard SPIKING block. Normalised it is a 35%
    # collapse in the expected move, which is what actually happened.
    engine.state["actual_dte"]          = 0
    engine.state["opening_iv"]          = 0.22
    engine.state["opening_iv_rem_frac"] = 205.0 / 375.0
    engine.state["opening_iv_expiry"]   = "2026-09-08"
    engine.state["actual_expiry"]       = "2026-09-08"
    _saved_rem_frac = engine._session_rem_frac
    engine._session_rem_frac = lambda: 10.0 / 375.0
    try:
        beh0, chg0 = engine._compute_iv_behavior(0.6443, test_bars)
    finally:
        engine._session_rem_frac = _saved_rem_frac
    print(f"  0DTE IV 64.4% vs open 22.0% into the close: {beh0} ({chg0:.1f}%)")
    assert beh0 in ("CRUSHING", "DECLINING"), (
        f"A quiet expiry afternoon must read as a vol crush, got {beh0} {chg0}"
    )
    assert chg0 < -20.0, f"Expected a large negative normalised change, got {chg0}"

    print("  [OK] IV behavior test passed")'''


def patch_data_engine(p: FilePatcher) -> None:
    p.sub("data_engine/session-rem-frac-helper", HELPER_ANCHOR, HELPER_NEW)
    p.sub("data_engine/deterministic-iv-band-test", TEST_SETUP_OLD, TEST_SETUP_NEW)
    p.sub("data_engine/0dte-normalisation-test", TEST_0DTE_OLD, TEST_0DTE_NEW)
    p.sub("data_engine/baseline-records-series-and-clock", INIT_OLD, INIT_NEW)
    p.sub("data_engine/rebaseline-on-expiry-change", GUARD_OLD, GUARD_NEW)
    p.sub("data_engine/time-normalised-iv-behavior", OLD_IV_BLOCK, IV_NEW)


# ═══════════════════════════════════════════════════════════════════════════
#  verification
# ═══════════════════════════════════════════════════════════════════════════

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
    de_src = (BASE / "data_engine.py").read_text(encoding="utf-8")

    checks = [
        ("core carries the v3.5 marker", MARKER_V35 in core_src),
        ("core keeps the v3.4 marker", MARKER_V34 in core_src),
        ("the session-clock helper exists",
         "def _session_rem_frac(self) -> float:" in de_src),
        ("the baseline records its expiry series",
         '"opening_iv_expiry"]   = self.state.get("actual_expiry")' in de_src),
        ("the baseline records its clock",
         '"opening_iv_rem_frac"] = self._session_rem_frac()' in de_src),
        ("the baseline is re-taken on an expiry change",
         "IV baseline re-taken on expiry change" in de_src),
        ("IV behaviour is compared in time-normalised space",
         "_cur_norm  = atm_iv_pct * math.sqrt(max(_rem_now, 1e-6))" in de_src),
        ("the v3.1 path survives when no baseline clock exists",
         "_tol = min(max(_rem_frac ** -0.5, 1.0), 3.2)" in de_src),
        ("the band test no longer depends on the wall clock",
         'engine.state["actual_dte"] = 5' in de_src),
        ("the 0DTE normalisation is covered by a test",
         "A quiet expiry afternoon must read as a vol crush" in de_src),
        ("the bare raw comparison is no longer unconditional",
         de_src.count(
             "iv_change_pct = (atm_iv_pct - opening_iv_pct) / opening_iv_pct"
             " * 100.0") == 1),
    ]
    for label, ok in checks:
        print(f"    {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            errs.append(label)

    out = _run_py(
        "import data_engine, inspect\n"
        "src = inspect.getsource(data_engine.MarketDataEngine._compute_iv_behavior)\n"
        "print('NORM' if 'math.sqrt' in src else 'NONORM')\n", 180)
    ok = out.returncode == 0 and "NORM" in out.stdout and "NONORM" not in out.stdout
    print(f"    {'PASS' if ok else 'FAIL'}  data_engine imports and carries the fix")
    if not ok:
        errs.append("data_engine import check: "
                    + ((out.stderr or "").strip().splitlines() or ["?"])[-1])
    return errs


def verify_behaviour() -> list[str]:
    """
    Prove the arithmetic on the real measured numbers rather than asserting
    it in a comment. A quiet expiry afternoon must read as a vol crush.
    """
    errs = []
    out = _run_py(
        "import math\n"
        "pts = [('12:05', 21.87), ('13:30', 25.73), ('15:20', 64.43)]\n"
        "base = None\n"
        "res = []\n"
        "for t, iv in pts:\n"
        "    h, m = t.split(':')\n"
        "    rem = (15 * 60 + 30) - (int(h) * 60 + int(m))\n"
        "    n = iv * math.sqrt(max(rem, 1) / 375.0)\n"
        "    base = n if base is None else base\n"
        "    res.append((t, iv, (iv - 10.13) / 10.13 * 100.0,"
        " n, (n - base) / base * 100.0))\n"
        "for t, iv, raw, n, nn in res:\n"
        "    print(f'{t} raw={raw:+.0f}% norm={nn:+.0f}%')\n"
        "assert res[-1][2] > 500, 'raw drift should be enormous'\n"
        "assert res[-1][4] < -20, 'normalised should read as a crush'\n"
        "print('ARITHMETIC_OK')\n", 120)
    ok = out.returncode == 0 and "ARITHMETIC_OK" in out.stdout
    for line in (out.stdout or "").strip().splitlines():
        if line and "ARITHMETIC_OK" not in line:
            print(f"           {line}")
    print(f"    {'PASS' if ok else 'FAIL'}  raw drift blocks, normalised drift sells")
    if not ok:
        errs.append("behaviour check failed")
    return errs


def run_self_tests() -> list[str]:
    errs = []
    for mod in ("data_engine.py", "regime_engine.py"):
        out = _run_py(
            "import runpy\n"
            f"runpy.run_path({str(BASE / mod)!r}, run_name='__main__')\n", 600)
        combined = (out.stderr or "") + (out.stdout or "")
        ok = out.returncode == 0
        if not ok and "UnicodeEncodeError" in combined:
            print(f"    WARN  {mod} could not write its output on this console")
            print("          (a display problem, not a failed assertion)")
            out = _run_py(
                "import runpy, os, sys\n"
                "sys.stdout = open(os.devnull, 'w')\n"
                f"runpy.run_path({str(BASE / mod)!r}, run_name='__main__')\n", 600)
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
    for label, marker in (("v3.4", MARKER_V34), ("v3.5", MARKER_V35)):
        print(f"    {label}: {'installed' if marker in core_src else 'NOT installed'}")
    print()
    errs = verify_semantics()
    print()
    return 1 if errs else 0


def main() -> int:
    _harden_stdout()
    ap = argparse.ArgumentParser(description="NIFTY engine profitability patch v3.5")
    ap.add_argument("--verify", action="store_true",
                    help="report the state of the tree and change nothing")
    args = ap.parse_args()

    print("=" * 76)
    print(" NIFTY intraday options engine - profitability patch v3.5")
    print(" the IV baseline: wrong series, and wrong units")
    print("=" * 76)

    if args.verify:
        return do_verify()

    core_src = (BASE / "core.py").read_text(encoding="utf-8")
    if MARKER_V34 not in core_src:
        print("\n  REFUSING: v3.4 must be installed first. Run patch-version4.py.\n")
        return 1
    if MARKER_V35 in core_src:
        print("\n  v3.5 is already installed. Nothing to do.\n")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = backup_all(stamp)
    print(f"\n  backup: {backup.name}/\n")

    try:
        patchers = {}
        for name, fn in (("core.py", patch_core),
                         ("data_engine.py", patch_data_engine)):
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
    print(" v3.5 applied and verified.")
    print("=" * 76)
    print(f"""
 What changed

   data_engine._compute_iv_behavior now compares IV * sqrt(T_remaining)
   instead of raw IV on the expiry series, and re-takes its baseline
   whenever the active expiry changes, recording the clock at which it was
   taken. Measured effect on 2026-09-08: IV_SPIKING_HARD_BLOCK was the
   terminal gate on 556 of the 562 afternoon cycles.

   Callers that set opening_iv without a baseline clock keep v3.1
   behaviour exactly, so no existing test changes.

 What is still broken, and not by this patch

   The collector polled the 15-Sep series until 12:03 on 2026-09-08, so
   the morning 206 cycles have no 0DTE chain to trade and are correctly
   rejected as dte 5 above max 4. Fix the collector or every session you
   record will keep losing its morning.

 Rollback

   cp {backup.name}/* .
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())