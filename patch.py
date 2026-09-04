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

if not DB_PATH.exists():
    print(f"Database not found: {DB_PATH}")
else:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    tables = [
        "session_state",
        "cycle_log",
        "strategy_decisions",
        "option_chain_snapshot",
        "api_call_log",
        "audit_log",
        "intraday_candles",
        "daily_summary",
    ]

    for table in tables:
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            conn.execute(f"DELETE FROM {table}")
            print(f"  Cleared {count:>6} rows from {table}")
        except Exception as e:
            print(f"  Skipped {table}: {e}")

    pos_ids = [r[0] for r in conn.execute("SELECT position_id FROM positions").fetchall()]
    if pos_ids:
        ph = ",".join("?" for _ in pos_ids)
        for t in ("position_legs", "trade_entries", "trade_exits"):
            try:
                count = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE position_id IN ({ph})", pos_ids).fetchone()[0]
                conn.execute(f"DELETE FROM {t} WHERE position_id IN ({ph})", pos_ids)
                print(f"  Cleared {count:>6} rows from {t}")
            except Exception as e:
                print(f"  Skipped {t}: {e}")

    count = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    conn.execute("DELETE FROM positions")
    print(f"  Cleared {count:>6} rows from positions")

    try:
        conn.execute("DELETE FROM sqlite_sequence")
        print(f"  Reset auto-increment counters")
    except Exception:
        pass

    conn.commit()
    conn.close()
    print(f"\nAll test data cleared. Database is clean for production use.")
    print(f"Run: python main.py")