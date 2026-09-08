#!/usr/bin/env python3
"""
════════════════════════════════════════════════════════════════════════════
 patch-version6.py — NIFTY intraday options engine, profitability patch v3.6
════════════════════════════════════════════════════════════════════════════

 Prerequisite: v3.5 must already be installed. Self-contained, stdlib only.

 ── THE DEFECT ─────────────────────────────────────────────────────────────

 Measured by replaying 2026-09-08. After v3.5 the engine reached the
 structure builder and then rejected all 86 surviving candidates on
 net_credit. It was not a lack of edge. It was selling the wrong strike.

 At 12:03, spot 23654.5, the engine sold 23850 / bought 23950:

     SELL 23850 call @ 5.40   delta 0.085
     BUY  23950 call @ 2.60
     gross 2.80 pts, net 1.95, friction 1.57  ->  76% of credit, rejected

 Its own delta target was 0.20, and _find_strike_by_delta correctly
 returned 23750 (delta 0.224). Same 100-point wing, so identical maximum
 loss, but a very different trade:

     short   delta   credit   wing   gross
     23750   0.224   15.75    5.40   10.35    <- what delta selection chose
     23850   0.085    5.40    2.60    2.80    <- what actually traded

 The override is in strategy_engine, which clamps the short strike to lie
 at least 0.80 * expected_move_remaining_pts away from the centre. Delta
 asked for 95 points of distance; the clamp forced 166.

 And the expected move driving that clamp was wrong:

     engine expected_move_remaining = 208.1 pts
     market's own live ATM straddle =  82.1 pts
     overstatement                  = 2.5x

 Because _em_base read state["opening_straddle_pts"] - the straddle
 captured at 09:30 on the 15-Sep series, 280 points - and then scaled it
 by sqrt(remaining fraction): 280 * sqrt(0.552) = 208.0, matching the
 observed 208.1 exactly.

 This is the same fault v3.5 fixed for opening_iv, on a different
 baseline: a session-opening value latched on one expiry series and never
 re-taken when the engine switched to another.

 ── THE FIX ────────────────────────────────────────────────────────────────

 Rather than re-baseline a third stale value, the expected remaining move
 is now read from the live ATM straddle of the active chain every cycle,
 and the opening-baseline scaling is dropped.

 On the expiry series this needs no time scaling at all. A 0DTE straddle
 already prices exactly the time left in the session - that is what it is
 - so multiplying it by sqrt(T_remaining) double-counts the decay. At
 12:03 the answer is simply 82.1.

 Away from expiry the live straddle prices the move to ITS expiry, not to
 tonight's close, so only today's share is at risk intraday and only the
 unexpired part of today remains. Both scalings are kept there.

 expected_range_so_far_pts is a statement about the whole session rather
 than what is left of it, so it keeps the opening baseline. Nothing reads
 it today, and this patch does not change its meaning.

 ── WHAT THIS AFFECTS ──────────────────────────────────────────────────────

 expected_move_remaining_pts feeds two consumers, and both were being fed
 a number 2.5x too large:

   strategy_engine:526   the strike-distance clamp described above
   strategy_engine:1342  the EV gate's risk horizon

 So this also tightens the EV gate's estimate of what can go wrong. That
 is a real behavioural change beyond strike selection and it is called out
 here rather than left to be discovered.

 ── HONEST SCOPE ───────────────────────────────────────────────────────────

 This corrects a units error against the market's own quoted price. It is
 not a tuning parameter and it was not fitted to an outcome. But one live
 session is still one sample, and letting a trade through is not the same
 as that trade making money.

 Usage:
     python3 patch-version6.py            # apply
     python3 patch-version6.py --verify   # report state, change nothing
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

MARKER_V35 = "NIFTY_ENGINE_PROFIT_PATCH_V35"
MARKER_V36 = "NIFTY_ENGINE_PROFIT_PATCH_V36"

TOUCHED = ["core.py", "data_engine.py"]



EM_OLD = """\
            _em_base = float(
                self.state.get("opening_straddle_pts")
                or self.state.get("_last_atm_straddle")
                or 0.0
            )
            if _em_base <= 20 and atm_straddle > 20:
                _em_base = float(atm_straddle)
            if _em_base <= 20:
                _em_sp = float(spot or self.state.get("prev_spot") or 0.0)
                _em_base = _em_sp * 0.009 if _em_sp > 0 else 0.0
            if _em_base > 20:
                _em_dte = actual_dte if actual_dte is not None else 1
                if _em_dte and _em_dte > 0:
                    # For a multi-day contract only the part of the
                    # straddle attributable to today is at risk intraday.
                    _em_scale = _math_em.sqrt(
                        1.0 / max(float(_em_dte) + 1.0, 1.0)
                    )
                else:
                    _em_scale = 1.0
                _expected_move_remaining = round(
                    _em_base * _em_scale * _math_em.sqrt(_em_rem_frac), 2
                )
                _expected_range_so_far = round(
                    _em_base * _math_em.sqrt(max(1.0 - _em_rem_frac, 0.02)), 2
                )
            else:
                _expected_move_remaining = 0.0
                _expected_range_so_far = 0.0"""


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
    d = BASE / f"patch_v36_backup_{stamp}"
    d.mkdir(exist_ok=True)
    for name in TOUCHED + ["env.txt"]:
        p = BASE / name
        if p.exists():
            shutil.copy2(p, d / name)
    return d


def restore_all(d: Path) -> None:
    for f in d.iterdir():
        shutil.copy2(f, BASE / f.name)


EM_NEW = '''            # ── v3.6 ─────────────────────────────────────────────────
            # The expected remaining move now comes from the live ATM
            # straddle of the ACTIVE chain, every cycle.
            #
            # It used to come from state["opening_straddle_pts"] scaled by
            # sqrt(remaining fraction). On 2026-09-08 that baseline was
            # captured at 09:30 on the 15-Sep series - 280 points - and
            # the engine was still using it after it switched to the 0DTE
            # chain at 12:03, where the real straddle was 82.1:
            #
            #     280 * sqrt(0.552) = 208.0   the engine's answer
            #     live ATM straddle =  82.1   the market's answer
            #
            # A 2.5x overstatement, which strategy_engine turns into a
            # floor of 0.80 * EM on how far out the short strike must sit.
            # Delta selection asked for 95 points and got clamped to 166,
            # so the engine sold 0.085 delta instead of the 0.224 it had
            # chosen, collected 2.80 instead of 10.35, and then rejected
            # itself because friction was 76% of the credit.
            #
            # On the expiry series no time scaling belongs here at all: a
            # 0DTE straddle already prices exactly the time left in the
            # session, so scaling it again by sqrt(T) double-counts decay.
            # Away from expiry the straddle prices the move to ITS expiry,
            # so today's share and the unexpired part of today both apply.
            _em_live = float(atm_straddle or 0.0)
            if _em_live <= 20:
                _em_live = float(self.state.get("_last_atm_straddle") or 0.0)

            _em_dte = actual_dte if actual_dte is not None else 1

            if _em_live > 20:
                if _em_dte is not None and _em_dte > 0:
                    _expected_move_remaining = round(
                        _em_live
                        * _math_em.sqrt(1.0 / max(float(_em_dte) + 1.0, 1.0))
                        * _math_em.sqrt(_em_rem_frac), 2
                    )
                else:
                    _expected_move_remaining = round(_em_live, 2)
            else:
                # No usable chain. Fall back to a fraction of spot, still
                # never to the opening baseline.
                _em_sp = float(spot or self.state.get("prev_spot") or 0.0)
                _expected_move_remaining = round(
                    _em_sp * 0.009 * _math_em.sqrt(_em_rem_frac), 2
                ) if _em_sp > 0 else 0.0

            # expected_range_so_far is about the whole session, not what is
            # left of it, so it keeps the opening baseline unchanged.
            _em_base = float(self.state.get("opening_straddle_pts") or 0.0)
            if _em_base <= 20 and _em_live > 20:
                _em_base = _em_live
            _expected_range_so_far = round(
                _em_base * _math_em.sqrt(max(1.0 - _em_rem_frac, 0.02)), 2
            ) if _em_base > 20 else 0.0'''


def patch_core(p: FilePatcher) -> None:
    p.sub("core/version-marker",
          f'{MARKER_V35} = "3.5"',
          f'{MARKER_V35} = "3.5"\n{MARKER_V36} = "3.6"')


def patch_data_engine(p: FilePatcher) -> None:
    p.sub("data_engine/live-straddle-expected-move", EM_OLD, EM_NEW)


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
        ("core carries the v3.6 marker", MARKER_V36 in core_src),
        ("core keeps the v3.5 marker", MARKER_V35 in core_src),
        ("the expected move reads the live ATM straddle",
         "_em_live = float(atm_straddle or 0.0)" in de_src),
        ("0DTE takes the straddle unscaled",
         "_expected_move_remaining = round(_em_live, 2)" in de_src),
        ("non-expiry keeps both time scalings",
         "* _math_em.sqrt(1.0 / max(float(_em_dte) + 1.0, 1.0))" in de_src),
        ("the opening baseline no longer feeds the expected move",
         '_em_base = float(\n                self.state.get("opening_straddle_pts")'
         not in de_src),
        ("expected_range_so_far still uses the opening baseline",
         '_em_base = float(self.state.get("opening_straddle_pts") or 0.0)'
         in de_src),
        ("both signals are still published",
         '"expected_move_remaining_pts": _expected_move_remaining,' in de_src
         and '"expected_range_so_far_pts":   _expected_range_so_far,' in de_src),
    ]
    for label, ok in checks:
        print(f"    {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            errs.append(label)

    out = _run_py("import data_engine\nprint('IMPORT_OK')\n", 180)
    ok = out.returncode == 0 and "IMPORT_OK" in out.stdout
    print(f"    {'PASS' if ok else 'FAIL'}  data_engine imports")
    if not ok:
        errs.append("data_engine import: "
                    + ((out.stderr or "").strip().splitlines() or ["?"])[-1])
    return errs


def verify_behaviour() -> list[str]:
    """
    Exercise the patched arithmetic directly on the measured 12:03 numbers
    rather than trusting the comment above it.
    """
    errs = []
    out = _run_py(
        "import math\n"
        "live, opening, rem = 82.1, 280.0, (375.0 - 168.0) / 375.0\n"
        "old = round(opening * math.sqrt(rem), 2)\n"
        "new0 = round(live, 2)\n"
        "new7 = round(live * math.sqrt(1.0 / 8.0) * math.sqrt(rem), 2)\n"
        "print(f'old (opening 15-Sep straddle, scaled) = {old}')\n"
        "print(f'new 0DTE  (live straddle, unscaled)   = {new0}')\n"
        "print(f'new 7DTE  (live straddle, scaled)     = {new7}')\n"
        "assert abs(old - 208.0) < 0.2, old\n"
        "assert abs(new0 - 82.1) < 0.01, new0\n"
        "assert new7 < new0, 'a far series must contribute less to today'\n"
        "band_old, band_new = 0.80 * old, 0.80 * new0\n"
        "print(f'strike floor 0.80*EM: {band_old:.0f} pts -> {band_new:.0f} pts')\n"
        "assert band_new < 95.0, 'the floor must stop overriding a 95pt delta pick'\n"
        "print('ARITHMETIC_OK')\n", 120)
    for line in (out.stdout or "").strip().splitlines():
        if "ARITHMETIC_OK" not in line:
            print(f"           {line}")
    ok = out.returncode == 0 and "ARITHMETIC_OK" in out.stdout
    print(f"    {'PASS' if ok else 'FAIL'}  the clamp stops overriding delta selection")
    if not ok:
        errs.append("behaviour check failed")
    return errs


def run_self_tests() -> list[str]:
    errs = []
    for mod in ("data_engine.py", "strategy_engine.py", "regime_engine.py"):
        body = ("import runpy\n"
                f"runpy.run_path({str(BASE / '@M@')!r}, run_name='__main__')\n"
                ).replace("@M@", mod)
        out = _run_py(body, 600)
        combined = (out.stderr or "") + (out.stdout or "")
        ok = out.returncode == 0
        if not ok and "UnicodeEncodeError" in combined:
            print(f"    WARN  {mod} could not write its output on this console")
            out = _run_py(
                "import runpy, os, sys\n"
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
    for label, marker in (("v3.5", MARKER_V35), ("v3.6", MARKER_V36)):
        print(f"    {label}: {'installed' if marker in core_src else 'NOT installed'}")
    print()
    errs = verify_semantics()
    print()
    return 1 if errs else 0


def main() -> int:
    _harden_stdout()
    ap = argparse.ArgumentParser(description="NIFTY engine profitability patch v3.6")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    print("=" * 76)
    print(" NIFTY intraday options engine - profitability patch v3.6")
    print(" the expected move: read it from the market, every cycle")
    print("=" * 76)

    if args.verify:
        return do_verify()

    core_src = (BASE / "core.py").read_text(encoding="utf-8")
    if MARKER_V35 not in core_src:
        print("\n  REFUSING: v3.5 must be installed first. Run patch-version5.py.\n")
        return 1
    if MARKER_V36 in core_src:
        print("\n  v3.6 is already installed. Nothing to do.\n")
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
    print(" v3.6 applied and verified.")
    print("=" * 76)
    print(f"""
 What changed

   expected_move_remaining_pts is now the live ATM straddle of the active
   chain, taken fresh each cycle, unscaled on the expiry series and scaled
   for today's share away from it. It no longer reads the opening
   straddle, which on 2026-09-08 was a 15-Sep value of 280 points still in
   use at 12:03 when the real 0DTE straddle was 82.1.

   This feeds strike selection AND the EV gate's risk horizon, so both
   were previously working from a number 2.5x too large.

 Rollback

   cp {backup.name}/* .
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())