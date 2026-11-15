import sqlite3
con = sqlite3.connect("data/per_day/nifty_algo_2026-10-22.db")
for t in ("13:00", "13:30", "14:00", "14:30"):
    r = con.execute(
        "SELECT substr(cycle_time,12,8), vrp_smoothed, vrp_raw, vol_regime, "
        "adx_15, day_move_used_pct, price_regime FROM cycle_log "
        "WHERE substr(cycle_time,12,5)=? LIMIT 1",
        (t,),
    ).fetchone()
    print(r)
con.close()
