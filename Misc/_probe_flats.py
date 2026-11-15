import sqlite3
from pathlib import Path

SHARDS = Path(__file__).resolve().parents[1] / "data" / "per_day"
FLATS = [
    "2026-09-18", "2026-09-21", "2026-09-23", "2026-09-28", "2026-09-29",
    "2026-10-06", "2026-10-13", "2026-10-22", "2026-10-26", "2026-10-29",
    "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-06", "2026-11-09",
    "2026-11-12",
]

SQL = """
SELECT COUNT(*) AS n,
  SUM(CASE WHEN adx_15>0 AND adx_15<20 AND price_regime IN ('RANGE','CHOPPY')
       AND CAST(substr(timestamp,12,2) AS INT)<13
       AND IFNULL(day_move_used_pct,0)<85 THEN 1 ELSE 0 END) AS soft_v,
  SUM(CASE WHEN adx_15>=20 AND IFNULL(iv_behavior,'')!='CRUSHING'
       AND CAST(substr(timestamp,12,2) AS INT)<15 THEN 1 ELSE 0 END) AS mom,
  SUM(CASE WHEN (price_regime IS NULL OR price_regime='')
       AND adx_15>0 AND adx_15<20
       AND CAST(substr(timestamp,12,2) AS INT)<13 THEN 1 ELSE 0 END) AS soft_null_reg,
  SUM(CASE WHEN adx_15>0 AND adx_15<20
       AND CAST(substr(timestamp,12,2) AS INT)<13
       AND IFNULL(day_move_used_pct,0)<85 THEN 1 ELSE 0 END) AS soft_any_reg
FROM market_snapshots
WHERE substr(timestamp,12,5)>='09:30' AND substr(timestamp,12,5)<='15:00'
"""

for day in FLATS:
    db = SHARDS / f"nifty_algo_{day}.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    r = con.execute(SQL).fetchone()
    print(
        f"{day} soft_v={r[1]} mom={r[2]} soft_null_reg={r[3]} "
        f"soft_any_reg={r[4]} n={r[0]}"
    )
