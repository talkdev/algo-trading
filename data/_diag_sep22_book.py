"""Sep22 open book: BCS + LONG_PUT context."""
import sqlite3
con = sqlite3.connect("data/nifty_algo_v3.db")
con.row_factory = sqlite3.Row
day = "2026-09-22"

print("=== positions ===")
cols = [d[1] for d in con.execute("PRAGMA table_info(positions)")]
print("cols sample", [c for c in cols if c in (
    "strategy_name", "entry_time", "status", "selection_reason",
    "net_pnl_rupees", "gross_pnl_rupees", "entry_premium", "lots"
)][:20])
for r in con.execute(
    """
    SELECT entry_time, strategy_name, status, final_lots,
           entry_spot, price_regime_at_entry, final_regime_at_entry,
           substr(coalesce(selection_reason,''),1,180) sr
    FROM positions WHERE trading_date=? ORDER BY entry_time
    """,
    (day,),
):
    print(dict(r))

print("\n=== SELECT around 10:37 and 10:56 ===")
for r in con.execute(
    """
    SELECT decision_time, action, strategy_name, substr(reason,1,180) r
    FROM strategy_decisions
    WHERE trading_date=?
      AND decision_time >= '2026-09-22T10:35'
      AND decision_time <= '2026-09-22T11:00'
      AND (action IN ('STRATEGY_SELECTED','ENTER')
           OR reason LIKE '%LONG_%'
           OR reason LIKE '%momentum%'
           OR reason LIKE '%BEAR_CALL%'
           OR reason LIKE '%slot%')
    ORDER BY decision_time
    """,
    (day,),
):
    print(dict(r))

print("\n=== cycle at entries ===")
for t0, t1 in (("10:37", "10:39"), ("10:55", "10:58")):
    print(f"--- {t0}-{t1} ---")
    for r in con.execute(
        f"""
        SELECT cycle_time, spot, adx_15, price_regime, final_regime,
               ema_structure, round(vwap_dist_pct,3) vd,
               open_positions, action_taken,
               substr(coalesce(no_trade_reason,''),1,100) ntr
        FROM cycle_log
        WHERE trading_date=?
          AND cycle_time >= ? AND cycle_time < ?
        ORDER BY cycle_time LIMIT 6
        """,
        (day, f"{day}T{t0}", f"{day}T{t1}"),
    ):
        print(dict(r))
