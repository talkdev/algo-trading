import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "nifty_algo.db"

TARGET_DATE = "2026-09-04"

if not DB_PATH.exists():
    print(f"Database not found at {DB_PATH}")
else:
    conn = sqlite3.connect(str(DB_PATH))
    
    cur = conn.execute("SELECT COUNT(*) FROM session_state WHERE trading_date=?", (TARGET_DATE,))
    count = cur.fetchone()[0]
    print(f"Found {count} session_state row(s) for {TARGET_DATE}")
    
    conn.execute("DELETE FROM session_state WHERE trading_date=?", (TARGET_DATE,))
    conn.commit()
    print(f"Deleted session_state for {TARGET_DATE}")
    
    cur2 = conn.execute("SELECT COUNT(*) FROM cycle_log WHERE trading_date=?", (TARGET_DATE,))
    cycle_count = cur2.fetchone()[0]
    print(f"Found {cycle_count} cycle_log row(s) for {TARGET_DATE}")
    
    conn.execute("DELETE FROM cycle_log WHERE trading_date=?", (TARGET_DATE,))
    conn.commit()
    print(f"Deleted cycle_log for {TARGET_DATE}")
    
    cur3 = conn.execute("SELECT COUNT(*) FROM strategy_decisions WHERE trading_date=?", (TARGET_DATE,))
    dec_count = cur3.fetchone()[0]
    print(f"Found {dec_count} strategy_decisions row(s) for {TARGET_DATE}")
    
    conn.execute("DELETE FROM strategy_decisions WHERE trading_date=?", (TARGET_DATE,))
    conn.commit()
    print(f"Deleted strategy_decisions for {TARGET_DATE}")
    
    conn.execute("DELETE FROM option_chain_snapshot WHERE trading_date=?", (TARGET_DATE,))
    conn.commit()
    print(f"Deleted option_chain_snapshot for {TARGET_DATE}")
    
    conn.execute("DELETE FROM api_call_log WHERE call_time LIKE ?", (f"{TARGET_DATE}%",))
    conn.commit()
    print(f"Deleted api_call_log for {TARGET_DATE}")
    
    conn.execute("DELETE FROM audit_log WHERE log_time LIKE ?", (f"{TARGET_DATE}%",))
    conn.commit()
    print(f"Deleted audit_log for {TARGET_DATE}")
    
    conn.close()
    print(f"\nAll data for {TARGET_DATE} cleared. Engine will start fresh on next run.")