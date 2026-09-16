import subprocess
import sys
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
PYTHON = sys.executable

# PATCH_V14: core.py runs its own self-test against a scratch database in
# /tmp and was the one engine module this script never executed - the module
# that owns the config loader, the expiry calendar, the schema and every
# migration. main.py is deliberately NOT in this list: it has no test mode and
# importing it is not the problem, RUNNING it starts the live loop. It is
# covered by the static pre-flight below instead, which compiles it and checks
# the config it would trade on.
tests = [
    ("core.py",               [PYTHON, str(BASE / "core.py")]),
    ("data_engine.py",        [PYTHON, str(BASE / "data_engine.py")]),
    ("regime_engine.py",      [PYTHON, str(BASE / "regime_engine.py")]),
    ("execution_engine.py",   [PYTHON, str(BASE / "execution_engine.py")]),
    ("strategy_engine.py",    [PYTHON, str(BASE / "strategy_engine.py")]),
    ("calibration_engine.py", [PYTHON, str(BASE / "calibration_engine.py")]),
    ("backtest_engine.py",    [PYTHON, str(BASE / "backtest_engine.py"), "--test"]),
    ("telegram_reporter.py",  [PYTHON, str(BASE / "telegram_reporter.py"), "--test"]),
]

env = os.environ.copy()
env["PYTHONIOENCODING"] = "utf-8"

print("=" * 70)
print("NIFTY OPTIONS ALGO ENGINE v3.0 - VERIFICATION")
print("=" * 70)


# ═══════════════════════════════════════════════════════════════════════════
#  PATCH_V14: STATIC PRE-FLIGHT
# ═══════════════════════════════════════════════════════════════════════════
# The module self-tests below each prove that one module still runs. None of
# them proves that the TREE is coherent: a patch applied to five files and not
# the sixth, a config whose square-off ordering is impossible, a holiday file
# that does not cover the year being traded. Those failures do not raise in a
# self-test - they raise on the first live cycle, or worse, they do not raise
# at all and quietly re-bucket every DTE. This stage checks them before a
# single engine module is executed, and it is the stage that would have caught
# the two the audit found: HARD_EXIT_TIME later than SQUARE_OFF_DEADLINE, and
# a config field (MAX_DTE_TRADEABLE) that nothing read.

MODULES = [
    "core.py", "data_engine.py", "regime_engine.py", "strategy_engine.py",
    "execution_engine.py", "calibration_engine.py", "backtest_engine.py",
    "telegram_reporter.py", "bot_controller.py", "split_db_per_day.py",
    "clean-db.py", "main.py",
]


def preflight() -> dict:
    """Compile the tree and check the invariants a live session depends on."""
    out = {"checks": [], "failed": 0}

    def check(name, ok, detail=""):
        out["checks"].append((name, bool(ok), detail))
        if not ok:
            out["failed"] += 1
        print(f"  [{'OK ' if ok else 'FAIL'}] {name}"
              + (f"   {detail}" if detail else ""))

    import py_compile as _pc
    for mod in MODULES:
        path = BASE / mod
        if not path.exists():
            check(f"compile {mod}", False, "file missing")
            continue
        try:
            _pc.compile(str(path), doraise=True)
            check(f"compile {mod}", True)
        except Exception as e:
            check(f"compile {mod}", False, str(e).splitlines()[-1][:70])

    sys.path.insert(0, str(BASE))
    try:
        import core as _core
        cfg = _core.load_config()
    except Exception as e:
        check("load_config()", False, str(e)[:70])
        return out
    check("load_config()", True)

    from datetime import time as _t

    def _hm(x):
        return x.strftime("%H:%M") if hasattr(x, "strftime") else str(x)

    # The square-off ordering is a safety property, not a preference:
    # positions must be flat before the watchdog, and the watchdog must fire
    # before the broker's own RMS square-off of an intraday (product="I")
    # order at 15:20 - after which the broker, not the engine, picks the
    # price.
    hx = getattr(cfg, "hard_exit_time", None)
    so = getattr(cfg, "square_off_deadline", None)
    check("hard_exit_time set", hx is not None, _hm(hx) if hx else "")
    check("square_off_deadline set", so is not None, _hm(so) if so else "")
    if hx and so:
        check("hard exit <= watchdog", hx <= so,
              f"{_hm(hx)} vs {_hm(so)}")
        check("watchdog before the broker's 15:20 RMS sweep", so < _t(15, 20),
              _hm(so))
    check("lot_size positive", int(getattr(cfg, "lot_size", 0) or 0) > 0,
          str(getattr(cfg, "lot_size", None)))
    check("starting_capital positive",
          float(getattr(cfg, "starting_capital", 0) or 0) > 0,
          f"{getattr(cfg, 'starting_capital', 0):,.0f}")
    check("max_daily_loss_pct in (0, 1]",
          0 < float(getattr(cfg, "max_daily_loss_pct", 0) or 0) <= 1.0,
          str(getattr(cfg, "max_daily_loss_pct", None)))

    # Dead-config detection: a Config field nothing reads is a knob that
    # silently does nothing. MAX_DTE_TRADEABLE was exactly that until v14.
    import re as _re
    src = ""
    for mod in ("strategy_engine.py", "regime_engine.py", "execution_engine.py",
                "data_engine.py"):
        try:
            src += (BASE / mod).read_text(encoding="utf-8")
        except Exception:
            pass
    for field in ("max_dte_tradeable",):
        check(f"config.{field} is read by the engines",
              bool(_re.search(rf"\b{field}\b", src)),
              "no reference found - the knob does nothing"
              if not _re.search(rf"\b{field}\b", src) else "")

    # The two hand-edited calendars the DTE and event logic depend on.
    import json as _json
    for fname, label in (("nse_holidays.json", "holidays"),
                         ("high_impact_events.json", "events")):
        fpath = BASE / fname
        if not fpath.exists():
            check(f"{label} calendar present", False, fname)
            continue
        try:
            data = _json.loads(fpath.read_text(encoding="utf-8"))
            years = {str(k)[:4] for k in data}
            from datetime import date as _d
            check(f"{label} calendar covers {_d.today().year}",
                  str(_d.today().year) in years,
                  f"{len(data)} entries, years {min(years)}-{max(years)}")
        except Exception as e:
            check(f"{label} calendar parses", False, str(e)[:70])

    # A half-applied patch tree: every version marker that must be present.
    for marker in ("PATCH_V12", "PATCH_V13", "PATCH_V14"):
        hits = 0
        for mod in MODULES:
            try:
                if marker in (BASE / mod).read_text(encoding="utf-8"):
                    hits += 1
            except Exception:
                continue
        check(f"{marker} markers present", hits > 0, f"{hits} module(s)")

    return out


print()
print("-" * 70)
print("STAGE 1 - STATIC PRE-FLIGHT  (tree, config, calendars)")
print("-" * 70)
_pre = preflight()
print()
print("-" * 70)
print("STAGE 2 - MODULE SELF-TESTS")
print("-" * 70)

results = {}
for name, cmd in tests:
    timeout = 300 if name == "strategy_engine.py" else 120
    print(f"\nRunning: {name} (timeout={timeout}s)")
    print("-" * 40)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(BASE),
            env=env,
        )
        output = result.stdout + result.stderr
        passed = result.returncode == 0
        lines = output.splitlines()
        if passed:
            show = [l for l in lines if "[OK]" in l or "passed" in l.lower()]
            for line in show[-15:]:
                print(f"  {line.strip()}")
            print(f"  RESULT: PASS")
        else:
            tb_start = 0
            for i, line in enumerate(lines):
                if "Traceback" in line:
                    tb_start = max(0, i - 1)
                    break
            for line in lines[tb_start:tb_start + 30]:
                print(f"  {line}")
            print(f"  RESULT: FAIL (returncode={result.returncode})")
        results[name] = passed
    except subprocess.TimeoutExpired:
        print(f"  RESULT: TIMEOUT (>{timeout}s)")
        results[name] = False
    except Exception as e:
        print(f"  RESULT: ERROR ({e})")
        results[name] = False

print("\n" + "=" * 70)
print("VERIFICATION SUMMARY")
print("=" * 70)
passed_count = sum(1 for v in results.values() if v)
total = len(results)
print(f"Passed: {passed_count}/{total}")
# PATCH_V14: the pre-flight is part of the verdict, not a preamble to it.
_pf_checks = len(_pre["checks"])
_pf_failed = _pre["failed"]
print(f"Pre-flight: {_pf_checks - _pf_failed}/{_pf_checks} checks passed")
print()
for name, passed in results.items():
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {name}")
if _pf_failed:
    print()
    print("  Pre-flight failures:")
    for _n, _ok, _d in _pre["checks"]:
        if not _ok:
            print(f"    [FAIL] {_n}" + (f"   {_d}" if _d else ""))
print()
# PATCH_V14: a non-zero exit code. This script always returned 0, so a
# wrapper, a cron job or a CI step invoking it before a live start got
# "success" from a tree that had just failed its own tests - the one signal
# that was supposed to stop the start could not stop anything.
if passed_count == total and _pf_failed == 0:
    print("ALL TESTS PASSED")
    print()
    print("  That is a statement about this tree compiling, its self-tests")
    print("  passing and its config being internally consistent. It is NOT a")
    print("  statement that the strategy has an edge: seven module self-tests")
    print("  and a pre-flight cannot produce one, and the backtest sample is")
    print("  far too small to. Go live through paper mode, then one lot.")
    print()
    print("Start engine:")
    print("  python main.py")
    sys.exit(0)
else:
    print(f"FAILURES: {total - passed_count} module test(s), "
          f"{_pf_failed} pre-flight check(s) failed")
    sys.exit(1)
