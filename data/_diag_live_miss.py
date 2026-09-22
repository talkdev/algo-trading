"""Deeper miss-audit: days with cycle_log but no/few positions vs replay winners."""
import sqlite3
from collections import Counter

con = sqlite3.connect("data/nifty_algo_v3.db")
con.row_factory = sqlite3.Row

for day in ("2026-09-08", "2026-09-09", "2026-09-10", "2026-09-15"):
    print(f"\n======== {day} ========")
    ss = con.execute(
        "SELECT day_mode, day_label, actual_dte, or_condition, or_width, entry_start, entry_end, entry_count FROM session_state WHERE trading_date=?",
        (day,),
    ).fetchone()
    print("session", dict(ss) if ss else None)
    print("positions", con.execute(
        "SELECT COUNT(*) c FROM positions WHERE trading_date=?", (day,)
    ).fetchone()["c"])
    print("SELECT attempts:")
    for r in con.execute(
        """
        SELECT substr(decision_time,12,8) t, action, strategy_name, substr(reason,1,140) r
        FROM strategy_decisions
        WHERE trading_date=? AND (
          action IN ('STRATEGY_SELECTED','ENTER')
          OR reason LIKE 'strategy_rules_failed%'
          OR reason LIKE 'params_invalid%'
          OR reason LIKE '%ev_gate%'
          OR reason LIKE '%LONG_%'
          OR reason LIKE '%momentum%'
        )
        ORDER BY decision_time LIMIT 25
        """,
        (day,),
    ):
        print(dict(r))
    # momentum refuse census
    ctr = Counter()
    for r in con.execute(
        "SELECT reason FROM strategy_decisions WHERE trading_date=?", (day,)
    ):
        reason = r["reason"] or ""
        if "momentum_refused:" in reason:
            m = reason.split("momentum_refused:", 1)[1][:70]
            ctr[m] += 1
        elif "LONG_" in reason:
            ctr["LONG_mention"] += 1
    print("momentum top:")
    for k, v in ctr.most_common(8):
        print(f"  {v:5d}  {k}")

# Sep22 open position status
print("\n======== 2026-09-22 open ========")
for r in con.execute(
    """
    SELECT entry_time, strategy_name, status, net_pnl_rupees, exit_reason,
           substr(selection_reason,1,120) sr
    FROM positions WHERE trading_date='2026-09-22'
    """
):
    print(dict(r))

# Historical max_pain / straddle_expanding counts on other days
print("\n======== max_pain / straddle_expanding historically ========")
for day in ("2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11",
            "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18",
            "2026-09-21", "2026-09-22"):
    mp = con.execute(
        "SELECT COUNT(*) c FROM strategy_decisions WHERE trading_date=? AND reason LIKE '%max_pain%'",
        (day,),
    ).fetchone()["c"]
    se = con.execute(
        "SELECT COUNT(*) c FROM strategy_decisions WHERE trading_date=? AND reason LIKE '%straddle_expanding%'",
        (day,),
    ).fetchone()["c"]
    wi = con.execute(
        "SELECT COUNT(*) c FROM strategy_decisions WHERE trading_date=? AND reason LIKE '%wing_cost%'",
        (day,),
    ).fetchone()["c"]
    print(f"{day}  max_pain={mp:4d}  straddle_exp={se:4d}  wing_cost={wi:4d}")
