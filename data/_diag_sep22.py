"""Diagnose 2026-09-22 flat live session."""
import sqlite3
from collections import Counter
from pathlib import Path

DB = Path("data/nifty_algo_v3.db")
con = sqlite3.connect(str(DB))
con.row_factory = sqlite3.Row
day = "2026-09-22"

tabs = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
print("tables present:", sorted(t for t in tabs if t in (
    "positions", "strategy_decisions", "cycle_log", "session_state", "market_snapshots"
)))

print("\n=== positions ===")
for r in con.execute(
    "SELECT * FROM positions WHERE trading_date=? ORDER BY entry_time", (day,)
):
    print(dict(r))
print("count", con.execute(
    "SELECT COUNT(*) c FROM positions WHERE trading_date=?", (day,)
).fetchone()["c"])

print("\n=== session_state ===")
for r in con.execute("SELECT * FROM session_state WHERE trading_date=?", (day,)):
    d = dict(r)
    keep = [
        "trading_date", "day_mode", "day_label", "or_width", "or_condition",
        "entry_start", "entry_end", "hard_exit_time", "actual_dte",
        "actual_expiry", "entry_count", "daily_halted", "vix_regime",
        "size_multiplier", "opening_straddle_pts",
    ]
    print({k: d.get(k) for k in keep})

print("\n=== decision action census ===")
for r in con.execute(
    """
    SELECT action, COUNT(*) c FROM strategy_decisions
    WHERE trading_date=? GROUP BY 1 ORDER BY c DESC
    """,
    (day,),
):
    print(dict(r))

print("\n=== top NO_TRADE reason prefixes ===")
ctr = Counter()
for r in con.execute(
    """
    SELECT reason FROM strategy_decisions
    WHERE trading_date=? AND action='NO_TRADE'
    """,
    (day,),
):
    reason = (r["reason"] or "")[:90]
    # bucket by first clause
    bucket = reason.split("|")[0][:70]
    ctr[bucket] += 1
for k, v in ctr.most_common(25):
    print(f"{v:5d}  {k}")

print("\n=== STRATEGY_SELECTED / ENTER ===")
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

print("\n=== sample cycle_log morning / midday ===")
for label, t0, t1 in (
    ("09:45-10:30", "09:45", "10:30"),
    ("10:30-12:00", "10:30", "12:00"),
    ("12:00-14:00", "12:00", "14:00"),
):
    print(f"--- {label} ---")
    rows = list(con.execute(
        f"""
        SELECT cycle_time, adx_15, price_regime, final_regime, or_condition,
               day_mode, open_positions, action_taken,
               substr(coalesce(no_trade_reason,''),1,80) ntr
        FROM cycle_log
        WHERE trading_date=?
          AND cycle_time >= ? AND cycle_time < ?
        ORDER BY cycle_time
        LIMIT 8
        """,
        (day, f"{day}T{t0}", f"{day}T{t1}"),
    ))
    if not rows:
        print("  (no rows)")
    for r in rows[:5]:
        print(dict(r))
    # census action in window
    for r in con.execute(
        f"""
        SELECT action_taken, COUNT(*) c FROM cycle_log
        WHERE trading_date=?
          AND cycle_time >= ? AND cycle_time < ?
        GROUP BY 1 ORDER BY c DESC LIMIT 8
        """,
        (day, f"{day}T{t0}", f"{day}T{t1}"),
    ):
        print("  act", dict(r))

print("\n=== cycle_log count / first / last ===")
print(dict(con.execute(
    """
    SELECT COUNT(*) n, MIN(cycle_time) first_t, MAX(cycle_time) last_t
    FROM cycle_log WHERE trading_date=?
    """,
    (day,),
).fetchone()))

print("\n=== event / dte / spot sample ===")
cols = [d[1] for d in con.execute("PRAGMA table_info(cycle_log)")]
want = [c for c in ("spot", "vix", "day_mode", "actual_dte", "or_width", "confidence_level") if c in cols]
if want:
    q = f"SELECT cycle_time, {', '.join(want)}, final_regime, adx_15 FROM cycle_log WHERE trading_date=? ORDER BY cycle_time LIMIT 3"
    for r in con.execute(q, (day,)):
        print(dict(r))
    q2 = f"SELECT cycle_time, {', '.join(want)}, final_regime, adx_15 FROM cycle_log WHERE trading_date=? ORDER BY cycle_time DESC LIMIT 3"
    for r in con.execute(q2, (day,)):
        print(dict(r))
