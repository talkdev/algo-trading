from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

path = BASE_DIR / "backtest.py"
with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()

new_lines = []
i = 0
fixed = False

while i < len(lines):
    line = lines[i]

    if "session_traded = False" in line and not fixed:
        new_lines.append(line)
        i += 1
        fixed = True
        continue

    if "if session_traded:" in line and not fixed:
        indent = len(line) - len(line.lstrip())
        pad = " " * indent
        new_lines.append(f"{pad}session_traded = False\n")
        new_lines.append(line)
        i += 1
        fixed = True
        print("[OK] Inserted session_traded = False before the if check")
        continue

    new_lines.append(line)
    i += 1

if not fixed:
    print("[SKIP] session_traded not found")
else:
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

import ast
try:
    with open(path, "r", encoding="utf-8") as f:
        ast.parse(f.read())
    print("[OK] Syntax valid")
except SyntaxError as e:
    print(f"[ERROR] Line {e.lineno}: {e.msg}")