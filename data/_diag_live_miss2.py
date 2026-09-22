import sqlite3
from collections import Counter
con = sqlite3.connect("data/nifty_algo_v3.db")
con.row_factory = sqlite3.Row

print("=== Sep15 action census ===")
for r in con.execute(
    """
    SELECT action, COUNT(*) c FROM strategy_decisions
    WHERE trading_date='2026-09-15' GROUP BY 1
    """
):
    print(dict(r))
ctr = Counter()
for r in con.execute(
    "SELECT reason FROM strategy_decisions WHERE trading_date='2026-09-15'"
):
    reason = (r["reason"] or "")
    first = reason.split("|")[0][:70]
    ctr[first] += 1
print("top reasons:")
for k, v in ctr.most_common(15):
    print(f"  {v:5d}  {k}")
print("momentum samples:")
for r in con.execute(
    """
    SELECT substr(decision_time,12,8) t, substr(reason,1,160) r
    FROM strategy_decisions WHERE trading_date='2026-09-15'
      AND reason LIKE '%momentum%'
    ORDER BY decision_time LIMIT 8
    """
):
    print(dict(r))

print("\n=== Sep8 afternoon (replay BCS was 12:15) ===")
for r in con.execute(
    """
    SELECT substr(decision_time,12,8) t, strategy_name, substr(reason,1,140) r
    FROM strategy_decisions
    WHERE trading_date='2026-09-08'
      AND decision_time >= '2026-09-08T12:00'
      AND decision_time < '2026-09-08T13:30'
    ORDER BY decision_time LIMIT 20
    """
):
    print(dict(r))
