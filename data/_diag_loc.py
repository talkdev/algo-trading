import sqlite3
con = sqlite3.connect("data/nifty_algo_v3.db")
con.row_factory = sqlite3.Row
print("ADX/price after IC stop 11:58-12:30")
for r in con.execute(
    """
    SELECT cycle_time, adx_15, price_regime, final_regime, open_positions
    FROM cycle_log WHERE trading_date='2026-09-21'
      AND cycle_time>='2026-09-21T11:55' AND cycle_time<='2026-09-21T12:30'
    ORDER BY cycle_time LIMIT 25
    """
):
    print(dict(r))
