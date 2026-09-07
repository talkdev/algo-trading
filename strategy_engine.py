# verify_all.py
# Run all self-tests and report results

import subprocess
import sys
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

print("=" * 70)
print("NIFTY OPTIONS ALGO ENGINE v3.0 - VERIFICATION")
print("=" * 70)

results = {}
for name, cmd in tests:
    print(f"\nRunning: {name}")
    print("-" * 40)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(BASE),
        )
        output = result.stdout + result.stderr
        passed = result.returncode == 0 and "AssertionError" not in output
        failed_asserts = [
            line for line in output.splitlines()
            if "AssertionError" in line or "Error" in line or "Traceback" in line
        ]
        if passed:
            passed_lines = [
                line for line in output.splitlines()
                if line.strip().startswith("[OK]")
            ]
            for line in passed_lines:
                print(f"  {line.strip()}")
            print(f"  RESULT: PASS (returncode={result.returncode})")
        else:
            for line in output.splitlines()[-30:]:
                print(f"  {line}")
            print(f"  RESULT: FAIL (returncode={result.returncode})")
        results[name] = passed
    except subprocess.TimeoutExpired:
        print(f"  RESULT: TIMEOUT (>60s)")
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
print()
for name, passed in results.items():
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {name}")

print()
if passed_count == total:
    print("ALL TESTS PASSED - Engine ready for trading")
    print()
    print("Start trading engine:")
    print("  python main.py")
else:
    print(f"FAILURES: {total - passed_count} tests failed")
    print("Review output above for details")
