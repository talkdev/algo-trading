import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def load_env_simple(path):
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


_ENV = load_env_simple(BASE_DIR / "env.txt")
DB_PATH = Path(_ENV.get("DB_PATH", "data/nifty_algo.db"))
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH


def add_column_if_missing(conn, table, column, coltype):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        print(f"  [OK] Added column {column} ({coltype}) to {table}")
    else:
        print(f"  [SKIP] Column {column} already exists in {table}")


def patch_schema():
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(str(DB_PATH))

    add_column_if_missing(conn, "session_state", "last_entry_time", "TEXT")
    add_column_if_missing(conn, "session_state", "last_stop_signal_combo", "TEXT")
    add_column_if_missing(conn, "session_state", "gap_fade_opportunity", "INTEGER DEFAULT 0")
    add_column_if_missing(conn, "session_state", "stop_at_breakeven", "INTEGER DEFAULT 0")
    add_column_if_missing(conn, "session_state", "stop_moved_to_25pct", "INTEGER DEFAULT 0")

    conn.commit()
    conn.close()
    print("Schema patched successfully.")


def patch_market_data_engine_defaults():
    path = BASE_DIR / "market_data_engine.py"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    old = (
        "            \"last_stop_time\": None, \"last_stop_reason\": None, \"last_entry_time\": None,"
    )
    if old in content:
        print("  [SKIP] last_entry_time already in session_state defaults")
        return

    old2 = (
        "            \"last_stop_time\": None, \"last_stop_reason\": None,"
    )
    new2 = (
        "            \"last_stop_time\": None, \"last_stop_reason\": None, \"last_entry_time\": None,"
    )
    if old2 in content:
        content = content.replace(old2, new2)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print("  [OK] Added last_entry_time to session_state defaults in market_data_engine.py")
    else:
        print("  [SKIP] Could not find last_stop_reason line in session_state defaults")


def patch_schema_sql():
    path = BASE_DIR / "nifty_algo_core.py"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    old = (
        "    last_stop_time           TEXT,\n"
        "    last_stop_reason         TEXT,"
    )
    new = (
        "    last_stop_time           TEXT,\n"
        "    last_stop_reason         TEXT,\n"
        "    last_entry_time          TEXT,"
    )
    if "last_entry_time" in content:
        print("  [SKIP] last_entry_time already in SCHEMA_SQL")
        return

    if old in content:
        content = content.replace(old, new)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print("  [OK] Added last_entry_time to SCHEMA_SQL in nifty_algo_core.py")
    else:
        print("  [SKIP] Could not find insertion point in SCHEMA_SQL")


def main():
    print("Patching database schema and code for last_entry_time column...")
    print()
    print("--- SQLite database ---")
    patch_schema()
    print()
    print("--- market_data_engine.py defaults ---")
    patch_market_data_engine_defaults()
    print()
    print("--- nifty_algo_core.py SCHEMA_SQL ---")
    patch_schema_sql()
    print()
    print("Done. Run main.py now.")


if __name__ == "__main__":
    main()