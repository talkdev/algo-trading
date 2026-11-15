import sqlite3
from pathlib import Path

db = Path(__file__).resolve().parents[1] / "data" / "per_day" / "nifty_algo_2026-09-23.db"
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
sql = """
SELECT final_regime, price_regime, COUNT(*) n FROM market_snapshots
WHERE adx_15>0 AND adx_15<20
  AND IFNULL(price_regime,'') IN ('RANGE','CHOPPY')
  AND CAST(substr(timestamp,12,2) AS INT)<13
  AND IFNULL(day_move_used_pct,0)<85
GROUP BY 1,2 ORDER BY n DESC LIMIT 15
"""
for row in c.execute(sql):
    print(row)
