import sqlite3, json
con = sqlite3.connect("data/nifty_algo_v3.db")
con.row_factory = sqlite3.Row
r = dict(con.execute(
    "SELECT * FROM session_state WHERE trading_date='2026-09-22'"
).fetchone())
keys = [
    "or_high", "or_low", "or_width", "or_condition", "opening_straddle_pts",
    "actual_dte", "entry_start", "entry_end", "day_label", "day_mode",
]
print("session", {k: r.get(k) for k in keys})
aux = r.get("aux_json")
if aux:
    a = json.loads(aux)
    for k in sorted(a):
        if any(x in k.lower() for x in ("high", "low", "loc", "range", "day_", "open")):
            print("aux", k, "=", a[k])

spot = con.execute(
    "SELECT spot FROM cycle_log WHERE trading_date='2026-09-22' ORDER BY cycle_time DESC LIMIT 1"
).fetchone()["spot"]
oh, ol = float(r["or_high"] or 0), float(r["or_low"] or 0)
# day range from 1m candles
dh = con.execute(
    "SELECT MAX(high) h, MIN(low) l FROM intraday_candles WHERE trading_date='2026-09-22' AND interval_min=1"
).fetchone()
print("spot", spot, "or", oh, ol, "dayHL", dict(dh))
if dh["h"] and dh["l"] and dh["h"] > dh["l"]:
    loc = (spot - dh["l"]) / (dh["h"] - dh["l"])
    print("session_loc", round(loc, 3), "range_pts", round(dh["h"] - dh["l"], 1))
print("or/straddle", round(float(r["or_width"]) / float(r["opening_straddle_pts"]), 3))

# how many 5m bars equivalent
n1 = con.execute(
    "SELECT COUNT(*) n FROM intraday_candles WHERE trading_date='2026-09-22' AND interval_min=1"
).fetchone()["n"]
print("1m bars", n1, "approx 5m bars", n1 // 5, "ADX5 needs 21 (~10:55)")
