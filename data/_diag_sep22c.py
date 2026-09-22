import sqlite3
con = sqlite3.connect("data/nifty_algo_v3.db")
con.row_factory = sqlite3.Row
day = "2026-09-22"

print("=== candle intervals today ===")
for r in con.execute(
    """
    SELECT interval_min, COUNT(*) n, MIN(candle_time) a, MAX(candle_time) b
    FROM intraday_candles WHERE trading_date=? GROUP BY 1
    """,
    (day,),
):
    print(dict(r))

print("\n=== 1-min bar count vs clock ===")
for r in con.execute(
    """
    SELECT substr(candle_time,1,5) hhmm, COUNT(*) n
    FROM intraday_candles
    WHERE trading_date=? AND interval_min=1
    GROUP BY 1 ORDER BY 1
    """,
    (day,),
):
    pass
# just totals by half hour
for r in con.execute(
    """
    SELECT
      CASE
        WHEN candle_time < '09:45:00' THEN '09:15-09:45'
        WHEN candle_time < '10:00:00' THEN '09:45-10:00'
        WHEN candle_time < '10:15:00' THEN '10:00-10:15'
        ELSE '10:15+'
      END bucket,
      COUNT(*) n
    FROM intraday_candles
    WHERE trading_date=? AND interval_min=1
    GROUP BY 1 ORDER BY 1
    """,
    (day,),
):
    print(dict(r))

print("\n=== sample bars head/tail ===")
for r in con.execute(
    "SELECT candle_time, open, high, low, close, interval_min FROM intraday_candles WHERE trading_date=? ORDER BY candle_time LIMIT 3",
    (day,),
):
    print(dict(r))
for r in con.execute(
    "SELECT candle_time, open, high, low, close, interval_min FROM intraday_candles WHERE trading_date=? ORDER BY candle_time DESC LIMIT 3",
    (day,),
):
    print(dict(r))
