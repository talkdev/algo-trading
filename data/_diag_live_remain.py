import sqlite3
con = sqlite3.connect("data/nifty_algo_v3.db")
con.row_factory = sqlite3.Row

print("=== Sep18 BCS entry context (cycle nearest 12:38) ===")
for r in con.execute(
    """
    SELECT cycle_time, adx_15, price_regime, final_regime,
           or_condition, vwap_dist_pct, open_positions, action_taken
    FROM cycle_log
    WHERE trading_date='2026-09-18'
      AND cycle_time>='2026-09-18T12:35' AND cycle_time<='2026-09-18T12:45'
    ORDER BY cycle_time LIMIT 15
    """
):
    print(dict(r))

print("\n=== Sep18 SELECT reasons ===")
for r in con.execute(
    """
    SELECT decision_time, strategy_name, substr(reason,1,160) r
    FROM strategy_decisions
    WHERE trading_date='2026-09-18' AND action='STRATEGY_SELECTED'
    ORDER BY decision_time
    """
):
    print(dict(r))

print("\n=== Sep21 BCS entry context 13:35 ===")
for r in con.execute(
    """
    SELECT cycle_time, adx_15, price_regime, final_regime,
           or_condition, vwap_dist_pct, action_taken, no_trade_reason
    FROM cycle_log
    WHERE trading_date='2026-09-21'
      AND cycle_time>='2026-09-21T13:30' AND cycle_time<='2026-09-21T13:40'
    ORDER BY cycle_time LIMIT 12
    """
):
    print(dict(r))
