"""Pre-10:30 audit: current loc + likely post-window blockers."""
import sqlite3, json
con = sqlite3.connect("data/nifty_algo_v3.db")
con.row_factory = sqlite3.Row
day = "2026-09-22"

spot = con.execute(
    "SELECT spot FROM cycle_log WHERE trading_date=? ORDER BY cycle_time DESC LIMIT 1",
    (day,),
).fetchone()["spot"]
dh = con.execute(
    """
    SELECT MAX(high) h, MIN(low) l FROM intraday_candles
    WHERE trading_date=? AND interval_min=1
    """,
    (day,),
).fetchone()
ss = dict(con.execute(
    "SELECT * FROM session_state WHERE trading_date=?", (day,)
).fetchone())
loc = (spot - dh["l"]) / (dh["h"] - dh["l"]) if dh["h"] > dh["l"] else None
print("spot", spot, "dayHL", dict(dh), "loc", round(loc, 3) if loc else None,
      "range", round(dh["h"] - dh["l"], 1))
print("OR", ss.get("or_high"), ss.get("or_low"), ss.get("or_condition"),
      "straddle", ss.get("opening_straddle_pts"))
print("warmup lean would fire BPS?", loc is not None and loc >= 0.62)
print("warmup lean would fire BCS?", loc is not None and loc <= 0.38)
print("mid wait?", loc is not None and 0.38 < loc < 0.62)

# Sep8 0DTE live/replay style rejection patterns from prior day if any
print("\n=== Sep8 0DTE decision SELECT (historical live if any) ===")
for r in con.execute(
    """
    SELECT decision_time, strategy_name, substr(reason,1,120) r
    FROM strategy_decisions
    WHERE trading_date='2026-09-08' AND action='STRATEGY_SELECTED'
    ORDER BY decision_time LIMIT 10
    """
):
    print(dict(r))

# Check hard-gate related config in session
print("\nwing", ss.get("wing_width"), "size", ss.get("size_multiplier"),
      "halted", ss.get("daily_halted"))
