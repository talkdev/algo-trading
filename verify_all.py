import subprocess
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
    ("backtest_engine.py",    [PYTHON, str(BASE / "backtest_engine.py"), "--test"]),
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
