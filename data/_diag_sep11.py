import sqlite3
con = sqlite3.connect("data/nifty_algo_v3.db")
con.row_factory = sqlite3.Row
cur = con.cursor()

print("=== Sep11 cycle_log action around 10:20-11:20 ===")
for r in cur.execute(
    """
    SELECT cycle_time, action_taken, no_trade_reason, final_regime,
           price_regime, open_positions, adx_15, ema_structure
    FROM cycle_log
    WHERE trading_date='2026-09-11'
      AND cycle_time >= '2026-09-11T10:20:00'
      AND cycle_time <= '2026-09-11T11:20:00'
    ORDER BY cycle_time
    LIMIT 30
    """
):
    print(dict(r))

print("\n=== Sep11 EVENT mentions in cycle notes ===")
for r in cur.execute(
    """
    SELECT cycle_time, final_regime, final_regime_notes, action_taken, no_trade_reason
    FROM cycle_log
    WHERE trading_date='2026-09-11'
      AND (final_regime_notes LIKE '%EVENT%' OR no_trade_reason LIKE '%EVENT%'
           OR final_regime_notes LIKE '%event%')
    ORDER BY cycle_time LIMIT 20
    """
):
    print(dict(r))

print("\n=== config max concurrent historically? session_state ===")
cols = [d[1] for d in cur.execute("PRAGMA table_info(session_state)")]
print(cols)
for r in cur.execute("SELECT * FROM session_state ORDER BY trading_date"):
    d = dict(r)
    print({k: d.get(k) for k in list(d)[:15]})
