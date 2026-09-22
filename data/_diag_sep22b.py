"""Sep22 deeper: ADX health, location, what would fire after 10:30."""
import sqlite3
from collections import Counter

con = sqlite3.connect("data/nifty_algo_v3.db")
con.row_factory = sqlite3.Row
day = "2026-09-22"

print("=== ADX / regime / loc last 20 cycles ===")
cols = [d[1] for d in con.execute("PRAGMA table_info(cycle_log)")]
extra = [c for c in (
    "ema_structure", "vwap_dist_pct", "iv_behavior", "confidence_level",
    "final_regime_notes", "size_multiplier", "choppy_detected"
) if c in cols]
sel = "cycle_time, spot, adx_15, price_regime, final_regime, or_condition, or_width"
if extra:
    sel += ", " + ", ".join(extra)
rows = list(con.execute(
    f"SELECT {sel} FROM cycle_log WHERE trading_date=? ORDER BY cycle_time DESC LIMIT 20",
    (day,),
))
for r in rows:
    print(dict(r))

print("\n=== ADX distribution today ===")
for r in con.execute(
    """
    SELECT
      SUM(CASE WHEN adx_15 IS NULL OR adx_15=0 THEN 1 ELSE 0 END) adx0,
      SUM(CASE WHEN adx_15>0 AND adx_15<12 THEN 1 ELSE 0 END) adx_lt12,
      SUM(CASE WHEN adx_15>=12 AND adx_15<20 THEN 1 ELSE 0 END) adx_12_20,
      SUM(CASE WHEN adx_15>=20 THEN 1 ELSE 0 END) adx_ge20,
      COUNT(*) n,
      MAX(adx_15) max_adx,
      AVG(adx_15) avg_adx
    FROM cycle_log WHERE trading_date=?
      AND cycle_time >= '2026-09-22T09:45'
    """,
    (day,),
):
    print(dict(r))

print("\n=== strategy_decisions last 15 ===")
for r in con.execute(
    """
    SELECT decision_time, substr(reason,1,140) r
    FROM strategy_decisions WHERE trading_date=?
    ORDER BY decision_time DESC LIMIT 15
    """,
    (day,),
):
    print(dict(r))

print("\n=== spot candles available? ===")
tabs = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
for t in ("intraday_candles", "spot_candles", "nifty_candles", "ohlc_bars"):
    if t in tabs:
        print(t, "exists")
        ccols = [d[1] for d in con.execute(f"PRAGMA table_info({t})")]
        print(" cols", ccols[:12])
        try:
            print(dict(con.execute(
                f"SELECT COUNT(*) n, MIN(candle_time) a, MAX(candle_time) b FROM {t} WHERE trading_date=?",
                (day,),
            ).fetchone()))
        except Exception as e:
            try:
                print(dict(con.execute(
                    f"SELECT COUNT(*) n, MIN(timestamp) a, MAX(timestamp) b FROM {t} WHERE date=?",
                    (day,),
                ).fetchone()))
            except Exception as e2:
                print("query fail", e, e2)

# find candle-like
for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%candle%'"
):
    print("candle table:", r[0])
