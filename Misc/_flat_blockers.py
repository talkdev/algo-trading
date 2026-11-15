"""Why remaining flat days stay NO_TRADE at midday (live cycle_log)."""
import json
import sqlite3

DB = r"data/nifty_algo_v3.db"
ZEROS = [
    "2026-09-28", "2026-09-29", "2026-10-06", "2026-10-13", "2026-10-22",
    "2026-10-26", "2026-10-29", "2026-11-02", "2026-11-03", "2026-11-04",
    "2026-11-09", "2026-11-12",
]
con = sqlite3.connect(DB)
for d in ZEROS:
    r = con.execute(
        """
        SELECT substr(cycle_time,12,8), or_condition, price_regime, vol_regime,
               final_regime, adx_15, iv_behavior, ema_structure,
               day_move_used_pct, no_trade_reason, action_taken, raw_json
        FROM cycle_log
        WHERE trading_date=? AND substr(cycle_time,12,5)>='11:45'
          AND or_condition IS NOT NULL
        ORDER BY cycle_time LIMIT 1
        """,
        (d,),
    ).fetchone()
    if not r:
        print(d, "NO_ROW")
        continue
    t, oc, px, vr, fr, adx, iv, ema, dm, ntr, act, raw = r
    j = json.loads(raw) if raw else {}
    sp = float(j.get("spot") or 0)
    oh = float(j.get("or_high") or 0)
    ol = float(j.get("or_low") or 0)
    op = float(j.get("day_open_spot") or 0)
    through = (oh and sp >= oh + 5) or (ol and sp <= ol - 5)
    drift = abs(sp - op) if sp and op else 0
    print(
        f"{d} {t} OR={oc} px={px} vol={vr} final={fr} "
        f"adx={None if adx is None else round(float(adx),1)} iv={iv} ema={ema} "
        f"dm={None if dm is None else round(float(dm),0)} "
        f"through={through} drift={drift:.0f} act={act}"
    )
    print(f"  reason={(ntr or '')[:140]}")
