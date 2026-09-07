# patch_final_v2.py
import ast, shutil
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parent
fpath = BASE / "strategy_engine.py"
lines = fpath.read_text(encoding="utf-8").splitlines()
print(f"Lines: {len(lines)}")

BACKUP = BASE / f"backup_pfv2_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
BACKUP.mkdir(parents=True, exist_ok=True)
shutil.copy2(fpath, BACKUP / fpath.name)

new = list(lines)

# ── Fix 1: _check_hard_gates add _test_time parameter ────────────────────────
for i, line in enumerate(new):
    if "def _check_hard_gates(" in line and "_test_time" not in line:
        new[i] = line.replace(
            "def _check_hard_gates(\n        self,\n        signals: dict,",
            "def _check_hard_gates(\n        self,\n        signals: dict,\n        _test_time=None,"
        )
        if new[i] == line:
            new[i] = line.rstrip()
            if new[i].endswith("signals: dict,"):
                pass
            elif "signals: dict" in new[i] and new[i].endswith(":"):
                new[i] = new[i].replace(
                    "self, signals: dict,",
                    "self, signals: dict, _test_time=None,"
                )
                new[i] = new[i].replace(
                    "self, signals: dict) ->",
                    "self, signals: dict, _test_time=None) ->"
                )
        print(f"Fix1 L{i+1}: {repr(new[i])}")
        break

# ── Fix 2: Inside _check_hard_gates, use _test_time for current_time ─────────
in_check_gates = False
for i, line in enumerate(new):
    if "def _check_hard_gates(" in line:
        in_check_gates = True
    if in_check_gates and "current_time = now_ist().time()" in line and "_test_time" not in line:
        new[i] = line.replace(
            "current_time = now_ist().time()",
            "current_time = _test_time if _test_time is not None else now_ist().time()"
        )
        print(f"Fix2 L{i+1}: {repr(new[i])}")
        break
    if in_check_gates and "def _map_regime_to_strategy" in line:
        break

# ── Fix 3: All gate test calls - pass _test_time=dtime(11, 0) ────────────────
selftest = next(i for i, l in enumerate(new) if "def _self_test()" in l)

gate_lines = []
for i in range(selftest, len(new)):
    if "engine._check_hard_gates(" in new[i]:
        gate_lines.append(i)

print(f"\nFound {len(gate_lines)} _check_hard_gates calls in test")
for i in gate_lines:
    print(f"  L{i+1}: {repr(new[i])}")

for i in gate_lines:
    line = new[i]
    if "_test_time" in line:
        print(f"  L{i+1}: already has _test_time")
        continue
    
    if line.strip().endswith("))"):
        new[i] = line.replace("))", ", _test_time=dtime(11, 0))")
        print(f"  Fixed L{i+1}: {repr(new[i])}")
    elif line.strip().endswith(")"):
        new[i] = line.rstrip()[:-1] + ", _test_time=dtime(11, 0))"
        print(f"  Fixed L{i+1}: {repr(new[i])}")

# ── Fix 4: strat2 test - add _test_time to _map_regime_to_strategy call ──────
for i in range(selftest, len(new)):
    if "strat2, _ = engine._map_regime_to_strategy(make_signals(" in new[i]:
        paren = 0
        end = i
        for j in range(i, min(i+10, len(new))):
            paren += new[j].count("(") - new[j].count(")")
            if paren <= 0 and j > i:
                end = j
                break
        
        already = any("_test_time" in new[k] for k in range(i, end+1))
        if not already:
            if new[end].strip() == "))":
                new[end] = new[end].replace("))", ", _test_time=dtime(10, 0))")
                print(f"Fix4 strat2 L{end+1}: {repr(new[end])}")
        break

# ── Verify syntax ─────────────────────────────────────────────────────────────
src = "\n".join(new) + "\n"
try:
    ast.parse(src)
    print("\nSYNTAX OK")
    fpath.write_text(src, encoding="utf-8")
    print(f"Written: {len(new)} lines")
except SyntaxError as e:
    print(f"\nSYNTAX ERROR L{e.lineno}: {e.msg} | {repr(e.text)}")
    for i in range(max(0,e.lineno-4), min(len(new), e.lineno+4)):
        print(f"  L{i+1}: {repr(new[i])}")
    print("NOT written")

print("\nRun: python verify_all.py")