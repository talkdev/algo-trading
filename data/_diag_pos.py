import sqlite3
from pathlib import Path

con = sqlite3.connect("data/nifty_algo_v3.db")
con.row_factory = sqlite3.Row
cur = con.cursor()

print("=== positions status ===")
cols = [d[1] for d in cur.execute("PRAGMA table_info(positions)")]
print(cols)
for r in cur.execute("SELECT * FROM positions ORDER BY entry_time"):
    d = dict(r)
    keys = [k for k in (
        "position_id", "trading_date", "strategy_name", "status", "state",
        "entry_time", "exit_time", "closed", "is_open", "actual_dte",
        "event_day", "final_lots",
    ) if k in d]
    print({k: d.get(k) for k in keys})

print("\n=== Sep11 cycle_log sample 10:20-11:20 (regimes if present) ===")
cols = [d[1] for d in cur.execute("PRAGMA table_info(cycle_log)")]
print([c for c in cols if "regime" in c or "final" in c or "event" in c or "action" in c])
# print action columns
print("action-ish", [c for c in cols if "act" in c.lower() or "reason" in c.lower() or "decision" in c.lower()])

print("\n=== Sep11 cycle around long-call time ===")
# get all cols matching
for r in cur.execute(
    """
    SELECT cycle_time, spot, adx_15, ema_structure, vwap_dist_pct, or_condition,
           day_move_used_pct
    FROM cycle_log
    WHERE trading_date='2026-09-11'
      AND cycle_time >= '2026-09-11T11:00:00'
      AND cycle_time <= '2026-09-11T11:30:00'
    ORDER BY cycle_time LIMIT 20
    """
):
    print(dict(r))

print("\n=== Sep11 trade entries event flags ===")
for r in cur.execute(
    "SELECT entry_time, strategy_name, event_day, event_name, "
    "price_regime_at_entry, final_regime_at_entry, selection_reason "
    "FROM trade_entries WHERE trading_date='2026-09-11'"
):
    print(dict(r))

print("\n=== phantom_trades ===")
try:
    for r in cur.execute("SELECT * FROM phantom_trades LIMIT 20"):
        print(dict(r))
except Exception as e:
    print(e)

print("\n=== data_engine snapshot insert fields check via source ===")
