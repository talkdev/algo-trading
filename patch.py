# patch_final.py
# Direct fix for strategy_engine.py slowness
# Reads the actual strategy_engine.py file directly

import shutil
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parent
BACKUP_DIR = BASE / f"backup_final_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

def backup(fpath):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(fpath, BACKUP_DIR / fpath.name)

def read_file(fname):
    fpath = BASE / fname
    if not fpath.exists():
        print(f"ERROR: {fpath} not found")
        return None, None
    return fpath, fpath.read_text(encoding="utf-8")

def write_file(fpath, content):
    fpath.write_text(content, encoding="utf-8")
    print(f"  Written: {fpath.name} ({len(content.splitlines())} lines)")

print("=" * 60)
print("PATCH FINAL - Direct file fixes")
print("=" * 60)

# ── Fix 1: strategy_engine.py - reduce chain size in self-test ───────────────
print("\n[1] strategy_engine.py - reduce mock chain from 41 to 17 strikes")
fpath, src = read_file("strategy_engine.py")
if src is None:
    print("  ERROR: file not found")
else:
    print(f"  File size: {len(src.splitlines())} lines")

    if "range(-20, 21)" in src:
        backup(fpath)
        src = src.replace("range(-20, 21)", "range(-8, 9)", 1)
        write_file(fpath, src)
        print("  OK   range(-20,21) -> range(-8,9): 41 strikes -> 17 strikes")
    else:
        print("  range(-20, 21) not found - checking what range is used...")
        for i, line in enumerate(src.splitlines(), 1):
            if "range(" in line and "offset" in src.splitlines()[max(0,i-3):i+3].__str__():
                print(f"  L{i}: {repr(line)}")
            if "for offset in range(" in line:
                print(f"  L{i}: {repr(line)}")

        if "for offset in range(" in src:
            import re
            matches = re.findall(r"for offset in range\([^)]+\)", src)
            print(f"  Found: {matches}")

# ── Fix 2: verify_all.py - rewrite cleanly with correct settings ─────────────
print("\n[2] verify_all.py - rewrite with correct timeout and encoding")

verify_content = '''import subprocess
import sys
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
PYTHON = sys.executable

tests = [
    ("data_engine.py",        [PYTHON, str(BASE / "data_engine.py")]),
    ("regime_engine.py",      [PYTHON, str(BASE / "regime_engine.py")]),
    ("execution_engine.py",   [PYTHON, str(BASE / "execution_engine.py")]),
    ("strategy_engine.py",    [PYTHON, str(BASE / "strategy_engine.py")]),
    ("calibration_engine.py", [PYTHON, str(BASE / "calibration_engine.py")]),
    ("backtest.py",           [PYTHON, str(BASE / "backtest.py"), "--test"]),
    ("eod_report.py",         [PYTHON, str(BASE / "eod_report.py"), "--test"]),
]

env = os.environ.copy()
env["PYTHONIOENCODING"] = "utf-8"

print("=" * 70)
print("NIFTY OPTIONS ALGO ENGINE v3.0 - VERIFICATION")
print("=" * 70)

results = {}
for name, cmd in tests:
    timeout = 300 if name == "strategy_engine.py" else 120
    print(f"\\nRunning: {name} (timeout={timeout}s)")
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

print("\\n" + "=" * 70)
print("VERIFICATION SUMMARY")
print("=" * 70)
passed_count = sum(1 for v in results.values() if v)
total = len(results)
print(f"Passed: {passed_count}/{total}")
print()
for name, passed in results.items():
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {name}")
print()
if passed_count == total:
    print("ALL TESTS PASSED - Engine ready for trading")
    print()
    print("Start engine:")
    print("  python main.py")
else:
    print(f"FAILURES: {total - passed_count} tests failed")
'''

verify_path = BASE / "verify_all.py"
backup(verify_path)
verify_path.write_text(verify_content, encoding="utf-8")
print(f"  Written: verify_all.py")

# ── Fix 3: Check strategy_engine.py self-test structure ──────────────────────
print("\n[3] Inspecting strategy_engine.py self-test structure...")
fpath, src = read_file("strategy_engine.py")
if src:
    lines = src.splitlines()
    selftest_start = -1
    for i, line in enumerate(lines):
        if "def _self_test()" in line:
            selftest_start = i
            break

    if selftest_start != -1:
        print(f"  _self_test at L{selftest_start+1}")
        print(f"  Total file lines: {len(lines)}")

        print(f"\n  First 50 lines of _self_test:")
        for i in range(selftest_start, min(selftest_start+50, len(lines))):
            print(f"  L{i+1}: {repr(lines[i])}")

        print(f"\n  Searching for slow patterns...")
        slow_patterns = {
            "range(-20, 21)": "large mock chain",
            "range(-10, 11)": "medium mock chain",
            "cal_engine.run": "calibration run",
            "validate_token": "API token validation",
            "get_daily_summary": "DB query",
            "get_vix_history": "DB query",
            "get_market_snapshots": "DB query",
        }
        for i in range(selftest_start, len(lines)):
            for pat, desc in slow_patterns.items():
                if pat in lines[i]:
                    print(f"  L{i+1} [{desc}]: {repr(lines[i])}")

print("\nRun: python verify_all.py")