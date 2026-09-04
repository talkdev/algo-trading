from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def fix_ipft():
    path = BASE_DIR / "execution_engine.py"
    content = read_file(path)

    targets = [
        (
            '"stt": round(stt, 2), "exchange": round(exchange, 2), "ipft": round(ipft, 6),\n                "sebi": round(sebi, 4), "stamp": round(stamp, 4), "brokerage": round(brokerage, 2),\n                "gst": round(gst, 2), "total": round(total_costs, 2),',
            '"stt": round(stt, 2), "exchange": round(exchange, 2),\n                "sebi": round(sebi, 4), "stamp": round(stamp, 4), "brokerage": round(brokerage, 2),\n                "gst": round(gst, 2), "total": round(total_costs, 2),'
        ),
    ]

    found = False
    for old, new in targets:
        if old in content:
            content = content.replace(old, new)
            found = True
            print(f"  [OK] Removed ipft from breakdown dict")
            break

    if not found:
        print("  Searching line by line...")
        lines = content.split("\n")
        new_lines = []
        fixed = False
        for line in lines:
            if "ipft" in line and "round(ipft" in line:
                print(f"  [FOUND] Line with ipft: {repr(line)}")
                new_line = line.replace(', "ipft": round(ipft, 6)', "")
                new_line = new_line.replace('"ipft": round(ipft, 6), ', "")
                new_line = new_line.replace('"ipft": round(ipft, 6),', "")
                new_line = new_line.replace('"ipft": round(ipft, 6)', "")
                new_lines.append(new_line)
                fixed = True
                print(f"  [OK] Fixed to: {repr(new_line)}")
            else:
                new_lines.append(line)
        if fixed:
            content = "\n".join(new_lines)
        else:
            print("  [SKIP] ipft not found anywhere in file")

    write_file(path, content)
    print("Patched: execution_engine.py")


def main():
    print("Fixing ipft NameError in execution_engine.py...")
    fix_ipft()
    print("\nDone. Restart main.py — no need to clear session this time.")
    print("The position entry was already attempted, check if a partial")
    print("position was created by running:")
    print("  python clear_session.py")
    print("Then restart:")
    print("  python main.py")


if __name__ == "__main__":
    main()