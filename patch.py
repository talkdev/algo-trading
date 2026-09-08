#!/usr/bin/env python3
"""
════════════════════════════════════════════════════════════════════════════
 patch-version4.py — NIFTY intraday options engine, profitability patch v3.4
════════════════════════════════════════════════════════════════════════════

 Prerequisite: v3.2 and v3.3 must already be installed (this script refuses
 to run otherwise). Self-contained: no imports outside the stdlib, no network.

 ── WHAT THIS FIXES, AND HOW IT WAS FOUND ─────────────────────────────────

 Unlike v3.2 and v3.3, which were derived by reading the source, this patch
 was derived by *measurement*. The engine was replayed cycle-by-cycle over
 the only genuinely live session in the recorded database — 2026-09-08,
 768 market-hours snapshots of the 0DTE expiry series — and it placed zero
 trades. Instrumenting the regime classifier produced this census of
 terminal volatility triggers:

     544  NEUTRAL  <- VRP_DATA_ERROR_NEUTRAL      (70.8% of all cycles)
      15  NEUTRAL  <- IV_SPIKING_HARD_BLOCK
      41  NEUTRAL  <- DAY_MOVE_USED_*             (spread over many values)

 DEFECT 1 — the VRP data-error guard rejects the premium it exists to sell.

   regime_engine.classify_volatility discards a cycle as corrupt when

       vrp_raw > max(8.0, 0.70 * atm_iv)

   On the measured session the broker's ATM IV on the expiry series ran
   27-29%, Parkinson RV ran ~6%, and India VIX sat at 11.2. So vrp_raw was
   about 22pp against a bound of 20.2pp, and 544 cycles were thrown away as
   bad data.

   None of it was bad data. It was verified against the raw stored chain:
   the 28.8% median ATM IV is the broker's own number, and the ~6% realised
   vol is correct for a session whose entire spot range was 90 points. VIX
   prices roughly 30 calendar days; a contract with hours left to live
   prices pin risk and gamma, and printing at two to three times VIX is its
   ordinary state on expiry day. Realised vol coming in at a fifth of
   implied is not a measurement failure — it is the variance risk premium,
   and harvesting it is the whole reason a premium-selling engine exists.
   The guard was standing the engine down precisely on the conditions it
   was built for.

   A genuine Parkinson failure does not present as a low ratio. It presents
   as realised vol collapsing to essentially nothing, because the bar feed
   is empty, flat, or degenerate. The test is therefore split in two:

     * an absolute floor catches the real failure — realised vol at or
       below 0.5% annualised is not a quiet market, it is a missing feed;
     * the ratio bound is kept, but relaxed to 0.92 on the expiry series,
       where a low ratio is the expected reading rather than a suspect one,
       and left at v3.1's 0.70 everywhere else.

   The 8pp absolute floor is retained for the case where ATM IV is
   unavailable, exactly as before.

 DEFECT 2 — the v3.2 threshold migration never actually migrated.

   v3.2 raised the day-move block threshold to 125% and v3.3 rescaled the
   quantity being compared against it. But env.txt overrides code defaults,
   the v3.2 migration only *appended* keys it could not find, and env.txt
   already contained the v3.1 line

       DAY_MOVE_USED_BLOCK_PCT=60.0

   so the append was skipped and the engine kept running the v3.1 threshold
   while its source claimed 125. The measured day_move_used had a median of
   28.1 and a maximum of 153.2, so the stale 60 was binding on the tail of
   the distribution. This patch rewrites the existing key in place rather
   than appending, and reports the old value it replaced.

 ── HONEST SCOPE ───────────────────────────────────────────────────────────

 One live session is one sample. These two edits remove blockers that were
 demonstrably misfiring on real data; they are NOT evidence that the
 resulting trades are profitable, and nothing here has been validated
 against a profitable out-of-sample record. Treat the result as an engine
 that can now express an opinion, not as an engine known to be right.

 ── ALSO CHANGED ───────────────────────────────────────────────────────────

 This patch edits one assertion in regime_engine's built-in self test. The
 fixture s6 (vrp_raw 9.5pp at 12.5% ATM IV, actual_dte 0) asserted that the
 guard fires. Under the corrected contract it must not, so the fixture is
 replaced with three cases that test the new contract directly: a dead bar
 feed still trips the guard, a rich 0DTE premium does not, and an absurd
 ratio away from the expiry series still does. Changing a test alongside
 the behaviour it pins is deliberate and is called out here so it is not
 mistaken for the test being quietly weakened.

 Usage:
     python3 patch-version4.py            # apply
     python3 patch-version4.py --verify   # report state, change nothing
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

MARKER_V32 = "NIFTY_ENGINE_PROFIT_PATCH_V32"
MARKER_V33 = "NIFTY_ENGINE_PROFIT_PATCH_V33"
MARKER_V34 = "NIFTY_ENGINE_PROFIT_PATCH_V34"

TOUCHED = ["core.py", "regime_engine.py"]

DTE0_VRP_FRAC = 0.92
BASE_VRP_FRAC = 0.70
RV_DEAD_PCT = 0.5
BLOCK_PCT = 125.0


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
    d = BASE / f"patch_v34_backup_{stamp}"
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
        f'{MARKER_V33} = "3.3"',
        f'{MARKER_V33} = "3.3"\n{MARKER_V34} = "3.4"',
    )


VRP_OLD = '''        _vrp_limit = max(8.0, 0.70 * _atm_iv_pct) if _atm_iv_pct else 8.0

        if vrp_raw is not None and vrp_raw > _vrp_limit:
            details["trigger"] = "VRP_DATA_ERROR_NEUTRAL"
            self.logger.warning(
                f"VRP={vrp_raw:.2f}pp > limit {_vrp_limit:.2f}pp "
                f"(ATM IV {_atm_iv_pct if _atm_iv_pct else float('nan'):.2f}%) — "
                f"likely Parkinson RV error. Treating as NEUTRAL."
            )
            return VolatilityRegime.NEUTRAL, details'''

VRP_NEW = '''        # ── v3.4 ──────────────────────────────────────────────────────────
        # Measured over the 2026-09-08 0DTE session: this guard was the
        # terminal gate on 544 of 768 market-hours cycles, 70.8% of the day.
        # Broker ATM IV on the expiry series ran 27-29% against a Parkinson
        # RV near 6% and a VIX of 11.2, so vrp_raw sat around 22pp and
        # cleared the 0.70 x IV bound of 20.2pp. Every one of those cycles
        # was discarded as corrupt input.
        #
        # None of it was corrupt. The 28.8% median ATM IV is the broker's
        # own figure in the stored chain and the ~6% realised vol is right
        # for a session with a 90-point total range. VIX prices roughly 30
        # calendar days; a contract with hours left prices pin risk and
        # gamma, and two to three times VIX is its ordinary state on expiry
        # day. Realised vol at a fifth of implied is not a broken feed — it
        # is the variance risk premium this engine exists to sell.
        #
        # A real Parkinson failure does not look like a low ratio. It looks
        # like realised vol collapsing to nothing because the bar feed is
        # empty, flat or degenerate. So the test is split: an absolute floor
        # catches the true failure, and the ratio bound is relaxed on the
        # expiry series where a low ratio is the expected reading.
        _rv_pct = None
        _rv_raw = signals.get("parkinson_rv")
        if _rv_raw is not None:
            try:
                _rv_pct = float(_rv_raw)
                if _rv_pct <= 2.0:          # stored as a decimal, not a pct
                    _rv_pct *= 100.0
            except (TypeError, ValueError):
                _rv_pct = None

        try:
            _dte_vrp = int(dte) if dte is not None else None
        except (TypeError, ValueError):
            _dte_vrp = None

        # 0.92 on the expiry series admits realised vol down to 8% of
        # implied before the reading is called impossible; 0.70 elsewhere,
        # unchanged from v3.1.
        _vrp_frac = VRP_DATA_ERROR_FRAC_DTE0 if _dte_vrp == 0 else VRP_DATA_ERROR_FRAC
        _vrp_limit = max(8.0, _vrp_frac * _atm_iv_pct) if _atm_iv_pct else 8.0

        _rv_dead = _rv_pct is not None and _rv_pct <= VRP_RV_DEAD_PCT
        _vrp_over = vrp_raw is not None and vrp_raw > _vrp_limit

        if _rv_dead or _vrp_over:
            details["trigger"] = "VRP_DATA_ERROR_NEUTRAL"
            details["vrp_limit"] = _vrp_limit
            details["vrp_reason"] = "rv_dead" if _rv_dead else "ratio"
            _why = (
                f"realised vol {_rv_pct:.2f}% at or below the "
                f"{VRP_RV_DEAD_PCT:.2f}% floor — bar feed looks empty"
                if _rv_dead else
                f"VRP={vrp_raw:.2f}pp > limit {_vrp_limit:.2f}pp"
            )
            self.logger.warning(
                f"{_why} (ATM IV "
                f"{_atm_iv_pct if _atm_iv_pct else float('nan'):.2f}%, "
                f"dte={_dte_vrp}) — likely Parkinson RV error. "
                f"Treating as NEUTRAL."
            )
            return VolatilityRegime.NEUTRAL, details'''


CONST_OLD = '''class RegimeClassifier:'''

CONST_NEW = f'''# {MARKER_V34}: bounds for the VRP data-error guard. The expiry series gets a
# looser ratio because a low realised-to-implied ratio is its normal state,
# and an absolute floor carries the burden of catching a genuinely dead feed.
VRP_DATA_ERROR_FRAC = {BASE_VRP_FRAC}
VRP_DATA_ERROR_FRAC_DTE0 = {DTE0_VRP_FRAC}
VRP_RV_DEAD_PCT = {RV_DEAD_PCT}


class RegimeClassifier:'''


TEST_OLD = '''    # VRP data error → NEUTRAL. At the default 12.5% ATM IV the v3.1 bound is
    # max(8, 0.70 x 12.5) = 8.75pp, so 9.5pp is still treated as bad data.
    s6 = make_signals(vrp_raw=9.5, vrp_smoothed=9.5)
    vol6, d6 = classifier.classify_volatility(s6, 0, 11.0)
    print(f"  VRP=9.5pp @ IV 12.5% → {vol6.value} (expect NEUTRAL - bad data)")
    assert vol6 == VolatilityRegime.NEUTRAL, f"Expected NEUTRAL for data error, got {vol6}"'''

TEST_NEW = '''    # v3.4 replaces the old s6 fixture. It asserted that 9.5pp of VRP at
    # 12.5% ATM IV on the expiry series is bad data; under the corrected
    # contract that is an ordinary 0DTE reading and must pass. The guard is
    # now pinned by the three cases that actually define it.

    # (a) a dead bar feed is still a data error, whatever the ratio says
    s6 = make_signals(vrp_raw=9.5, vrp_smoothed=9.5, parkinson_rv=0.0)
    vol6, d6 = classifier.classify_volatility(s6, 0, 11.0)
    print(f"  RV=0% (dead feed)    -> {vol6.value} (expect NEUTRAL - bad data)")
    assert vol6 == VolatilityRegime.NEUTRAL, f"Expected NEUTRAL for dead feed, got {vol6}"
    assert d6.get("trigger") == "VRP_DATA_ERROR_NEUTRAL", (
        f"A dead bar feed must trip the data-error guard, got {d6}"
    )
    assert d6.get("vrp_reason") == "rv_dead", f"Expected the absolute floor to fire, got {d6}"

    # (b) the measured 2026-09-08 condition: 28% ATM IV against 6% realised
    #     on the expiry series is a real variance premium, not a broken feed
    s6c = make_signals(vrp_raw=22.0, vrp_smoothed=22.0, atm_iv=0.28,
                       parkinson_rv=0.06, actual_dte=0)
    vol6c, d6c = classifier.classify_volatility(s6c, 0, 11.0)
    print(f"  VRP=22pp @ IV 28% 0DTE -> {vol6c.value} (expect NOT bad data)")
    assert d6c.get("trigger") != "VRP_DATA_ERROR_NEUTRAL", (
        f"A genuine 0DTE variance premium must not be called a data error, got {d6c}"
    )

    # (c) away from the expiry series the v3.1 ratio still applies
    s6d = make_signals(vrp_raw=11.0, vrp_smoothed=11.0, atm_iv=0.125,
                       parkinson_rv=0.015, actual_dte=2)
    vol6d, d6d = classifier.classify_volatility(s6d, 0, 11.0)
    print(f"  VRP=11pp @ IV 12.5% dte2 -> {vol6d.value} (expect NEUTRAL - bad data)")
    assert d6d.get("trigger") == "VRP_DATA_ERROR_NEUTRAL", (
        f"An impossible ratio off the expiry series is still a data error, got {d6d}"
    )'''


def patch_regime(p: FilePatcher) -> None:
    p.sub("regime/vrp-guard-constants", CONST_OLD, CONST_NEW)
    p.sub("regime/vrp-data-error-guard", VRP_OLD, VRP_NEW)
    p.sub("regime/self-test-vrp-contract", TEST_OLD, TEST_NEW)


def migrate_env() -> None:
    """Rewrite the stale v3.1 threshold in place. v3.2 only appended."""
    env_path = BASE / "env.txt"
    if not env_path.exists():
        print("  env.txt not present - core.load_config carries the 125.0 default")
        return
    text = env_path.read_text(encoding="utf-8")
    pat = re.compile(r"^(\s*DAY_MOVE_USED_BLOCK_PCT\s*=\s*)([0-9.]+)\s*$", re.M)
    m = pat.search(text)
    if not m:
        if not text.endswith("\n"):
            text += "\n"
        text += (
            f"\n# {MARKER_V34}: v3.3 rescaled day_move_used so 100 means "
            f"'the day has spent exactly what it was priced for'.\n"
            f"DAY_MOVE_USED_BLOCK_PCT={BLOCK_PCT}\n"
        )
        env_path.write_text(text, encoding="utf-8")
        print(f"  env.txt: added DAY_MOVE_USED_BLOCK_PCT={BLOCK_PCT}")
        return
    old_val = m.group(2)
    if abs(float(old_val) - BLOCK_PCT) < 1e-9:
        print(f"  env.txt: DAY_MOVE_USED_BLOCK_PCT already {BLOCK_PCT}")
        return
    text = pat.sub(lambda _m: f"{_m.group(1)}{BLOCK_PCT}", text, count=1)
    env_path.write_text(text, encoding="utf-8")
    print(f"  env.txt: DAY_MOVE_USED_BLOCK_PCT {old_val} -> {BLOCK_PCT} "
          f"(stale v3.1 value was overriding the v3.2 code default)")


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
    errs = []
    core_src = (BASE / "core.py").read_text(encoding="utf-8")
    rg_src = (BASE / "regime_engine.py").read_text(encoding="utf-8")
    env_src = ""
    if (BASE / "env.txt").exists():
        env_src = (BASE / "env.txt").read_text(encoding="utf-8")

    checks = [
        ("core carries the v3.4 marker", MARKER_V34 in core_src),
        ("core keeps the v3.3 marker", MARKER_V33 in core_src),
        ("core keeps the v3.2 marker", MARKER_V32 in core_src),
        ("the DTE-aware VRP fractions are declared",
         "VRP_DATA_ERROR_FRAC_DTE0 = " in rg_src),
        ("the dead-feed floor is declared", "VRP_RV_DEAD_PCT = " in rg_src),
        ("the guard selects its fraction by DTE",
         "_vrp_frac = VRP_DATA_ERROR_FRAC_DTE0 if _dte_vrp == 0" in rg_src),
        ("the guard reads realised vol", 'signals.get("parkinson_rv")' in rg_src),
        ("the guard fires on a dead feed", "_rv_dead or _vrp_over" in rg_src),
        ("the reason is recorded for the audit trail",
         '"vrp_reason"' in rg_src),
        ("the flat 0.70 form is gone",
         "max(8.0, 0.70 * _atm_iv_pct)" not in rg_src),
        ("the self test pins the 0DTE contract",
         "A genuine 0DTE variance premium must not be called a data error" in rg_src),
        ("the self test still pins the dead-feed case",
         "A dead bar feed must trip the data-error guard" in rg_src),
    ]
    if env_src:
        checks.append(
            (f"env.txt block threshold is {BLOCK_PCT}",
             re.search(rf"^\s*DAY_MOVE_USED_BLOCK_PCT\s*=\s*{BLOCK_PCT}\s*$",
                       env_src, re.M) is not None))

    for label, ok in checks:
        print(f"    {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            errs.append(label)

    # the classifier must import and the constants must be live
    try:
        out = _run_py(
            "import regime_engine as r\n"
            "print(r.VRP_DATA_ERROR_FRAC_DTE0, r.VRP_DATA_ERROR_FRAC,"
            " r.VRP_RV_DEAD_PCT)\n", 120)
        if out.returncode != 0:
            errs.append("regime_engine does not import: "
                        + (out.stderr.strip().splitlines() or ["?"])[-1])
        else:
            got = out.stdout.strip()
            want = f"{DTE0_VRP_FRAC} {BASE_VRP_FRAC} {RV_DEAD_PCT}"
            ok = got == want
            print(f"    {'PASS' if ok else 'FAIL'}  live constants are {want}"
                  + ("" if ok else f" (got {got})"))
            if not ok:
                errs.append("live constants differ")
    except Exception as e:                                  # noqa: BLE001
        errs.append(f"import check failed: {e}")

    return errs


def run_self_tests() -> list[str]:
    """regime_engine ships its own test block; it must still pass."""
    errs = []
    out = _run_py(
        "import runpy\n"
        f"runpy.run_path({str(BASE / 'regime_engine.py')!r}, run_name='__main__')\n",
        300)
    ok = out.returncode == 0
    combined = (out.stderr or "") + (out.stdout or "")

    # Last line of defence. If the child still died purely because it could
    # not encode a character for its console, that says nothing about whether
    # the assertions hold, and it must not roll back a correct patch.
    if not ok and "UnicodeEncodeError" in combined:
        print("    WARN  regime_engine.py self test could not write its output "
              "on this console")
        print("          (UnicodeEncodeError - a display problem, not a failed "
              "assertion)")
        print("          re-running with all output discarded so the asserts "
              "still count...")
        out = _run_py(
            "import runpy, os, sys\n"
            "sys.stdout = open(os.devnull, 'w')\n"
            f"runpy.run_path({str(BASE / 'regime_engine.py')!r},"
            " run_name='__main__')\n",
            300)
        ok = out.returncode == 0
        combined = (out.stderr or "") + (out.stdout or "")

    print(f"    {'PASS' if ok else 'FAIL'}  regime_engine.py self test")
    if not ok:
        tail = (combined.strip().splitlines() or ["(no stderr)"])[-6:]
        for line in tail:
            print(f"           {line}")
        errs.append("regime_engine self test failed")
    return errs


def do_verify() -> int:
    print("\n  state of the tree\n")
    core_src = (BASE / "core.py").read_text(encoding="utf-8")
    for label, marker in (("v3.2", MARKER_V32), ("v3.3", MARKER_V33),
                          ("v3.4", MARKER_V34)):
        print(f"    {label}: {'installed' if marker in core_src else 'NOT installed'}")
    print()
    errs = verify_semantics()
    print()
    return 1 if errs else 0


# ═══════════════════════════════════════════════════════════════════════════
#  driver
# ═══════════════════════════════════════════════════════════════════════════

def main() -> int:
    _harden_stdout()
    ap = argparse.ArgumentParser(description="NIFTY engine profitability patch v3.4")
    ap.add_argument("--verify", action="store_true",
                    help="report the state of the tree and change nothing")
    args = ap.parse_args()

    print("=" * 76)
    print(" NIFTY intraday options engine — profitability patch v3.4")
    print(" the VRP data-error guard, and the v3.2 threshold that never migrated")
    print("=" * 76)

    if args.verify:
        return do_verify()

    core_src = (BASE / "core.py").read_text(encoding="utf-8")
    if MARKER_V32 not in core_src or MARKER_V33 not in core_src:
        print("\n  REFUSING: v3.2 and v3.3 must be installed first.")
        print("  Run patch-version2.py then patch-version3.py.\n")
        return 1
    if MARKER_V34 in core_src:
        print("\n  v3.4 is already installed. Nothing to do.\n")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = backup_all(stamp)
    print(f"\n  backup: {backup.name}/\n")

    try:
        patchers = {}
        for name, fn in (("core.py", patch_core),
                         ("regime_engine.py", patch_regime)):
            p = FilePatcher(name)
            fn(p)
            patchers[name] = p
        for name, p in patchers.items():
            p.write()
            for label in p.log:
                print(f"    applied  {label}")
        migrate_env()

        print("\n  syntax\n")
        errs = verify_syntax()
        for e in errs:
            print(f"    FAIL  {e}")
        if not errs:
            print("    PASS  all touched files parse")

        print("\n  semantics\n")
        errs += verify_semantics()

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
    print(" v3.4 applied and verified.")
    print("=" * 76)
    print(f"""
 What changed

   1. regime_engine.classify_volatility — the VRP data-error guard is now
      DTE-aware. It admits realised vol down to 8% of implied on the expiry
      series ({DTE0_VRP_FRAC}) and keeps v3.1's {BASE_VRP_FRAC} elsewhere, and it
      carries an absolute floor: realised vol at or below {RV_DEAD_PCT}% is
      treated as a dead bar feed regardless of the ratio. Measured effect on
      2026-09-08: the guard was the terminal gate on 544 of 768 cycles.

   2. env.txt — DAY_MOVE_USED_BLOCK_PCT rewritten in place to {BLOCK_PCT}. The
      v3.2 migration only appended absent keys, so the v3.1 value of 60.0
      had been silently overriding the code default ever since.

 What this does not tell you

   These edits remove two blockers that were demonstrably misfiring on real
   recorded data. They are not evidence that the trades now permitted are
   profitable. One live session is one sample. Re-run the backtester and
   read the funnel before putting size on this.

 Rollback

   cp {backup.name}/* .
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())