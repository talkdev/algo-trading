import sqlite3
con = sqlite3.connect("data/nifty_algo_v3.db")
con.row_factory = sqlite3.Row
day = "2026-09-22"

print("=== full reasons after 10:30 ===")
for r in con.execute(
    """
    SELECT decision_time, strategy_name, substr(reason,1,180) r
    FROM strategy_decisions
    WHERE trading_date=? AND decision_time >= '2026-09-22T10:30:00'
    ORDER BY decision_time
    """,
    (day,),
):
    print(dict(r))

print("\n=== iv / straddle / max_pain from cycle if cols exist ===")
cols = [d[1] for d in con.execute("PRAGMA table_info(cycle_log)")]
want = [c for c in cols if any(x in c.lower() for x in (
    "straddle", "iv_", "max_pain", "atm"
))]
print("cols", want)
sel = "cycle_time, spot, adx_15, iv_behavior, action_taken, substr(coalesce(no_trade_reason,''),1,120) ntr"
extra = [c for c in ("atm_straddle_price", "opening_straddle_pts", "max_pain", "atm_iv_pct", "iv_change_pct_from_open") if c in cols]
if extra:
    sel = "cycle_time, spot, " + ", ".join(extra) + ", iv_behavior, substr(coalesce(no_trade_reason,''),1,100) ntr"
for r in con.execute(
    f"""
    SELECT {sel} FROM cycle_log
    WHERE trading_date=? AND cycle_time>='2026-09-22T10:30:00'
    ORDER BY cycle_time
    """,
    (day,),
):
    print(dict(r))
