"""Sep22 live status after 10:17 — window / entries / refusals."""
import sqlite3
from collections import Counter
from datetime import datetime

con = sqlite3.connect("data/nifty_algo_v3.db")
con.row_factory = sqlite3.Row
day = "2026-09-22"

print("now_approx_local", datetime.now().isoformat())
print("DB mtime check via last cycle:")
print(dict(con.execute(
    """
    SELECT COUNT(*) n, MIN(cycle_time) first_t, MAX(cycle_time) last_t
    FROM cycle_log WHERE trading_date=?
    """,
    (day,),
).fetchone()))

print("\n=== positions ===")
for r in con.execute(
    """
    SELECT entry_time, strategy_name, exit_time, exit_reason,
           round(net_pnl_rupees,1) pnl
    FROM positions WHERE trading_date=? ORDER BY entry_time
    """,
    (day,),
):
    print(dict(r))
print("pos_count", con.execute(
    "SELECT COUNT(*) c FROM positions WHERE trading_date=?", (day,)
).fetchone()["c"])

print("\n=== session ===")
r = dict(con.execute(
    "SELECT * FROM session_state WHERE trading_date=?", (day,)
).fetchone())
print({k: r.get(k) for k in (
    "entry_start", "entry_end", "actual_dte", "or_condition", "or_width",
    "entry_count", "day_mode", "opening_straddle_pts",
)})

print("\n=== decision census (all day) ===")
for r in con.execute(
    """
    SELECT action, COUNT(*) c FROM strategy_decisions
    WHERE trading_date=? GROUP BY 1 ORDER BY c DESC
    """,
    (day,),
):
    print(dict(r))

print("\n=== top reasons AFTER 10:30 ===")
ctr = Counter()
n_after = 0
for r in con.execute(
    """
    SELECT reason FROM strategy_decisions
    WHERE trading_date=? AND decision_time >= '2026-09-22T10:30:00'
    """,
    (day,),
):
    n_after += 1
    bucket = (r["reason"] or "").split("|")[0][:80]
    ctr[bucket] += 1
print("decisions_after_10:30", n_after)
for k, v in ctr.most_common(20):
    print(f"{v:5d}  {k}")

print("\n=== SELECT/ENTER ===")
for r in con.execute(
    """
    SELECT decision_time, action, strategy_name, substr(reason,1,160) r
    FROM strategy_decisions
    WHERE trading_date=? AND action IN ('ENTER','STRATEGY_SELECTED')
    ORDER BY decision_time
    """,
    (day,),
):
    print(dict(r))

print("\n=== last 8 cycles ===")
for r in con.execute(
    """
    SELECT cycle_time, spot, adx_15, price_regime, final_regime,
           ema_structure, round(vwap_dist_pct,3) vd,
           action_taken, substr(coalesce(no_trade_reason,''),1,100) ntr
    FROM cycle_log WHERE trading_date=?
    ORDER BY cycle_time DESC LIMIT 8
    """,
    (day,),
):
    print(dict(r))

print("\n=== ADX after 10:15 ===")
print(dict(con.execute(
    """
    SELECT COUNT(*) n, MAX(adx_15) mx, AVG(adx_15) av,
           SUM(CASE WHEN adx_15>0 THEN 1 ELSE 0 END) nonzero,
           SUM(CASE WHEN ema_structure='INSUFFICIENT_DATA' THEN 1 ELSE 0 END) ema_insuf
    FROM cycle_log
    WHERE trading_date=? AND cycle_time>='2026-09-22T10:15:00'
    """,
    (day,),
).fetchone()))
