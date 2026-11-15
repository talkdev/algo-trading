import sqlite3
from collections import Counter

for day in ["2026-09-23", "2026-10-06", "2026-11-02", "2026-09-28"]:
    c = sqlite3.connect(f"data/per_day/nifty_algo_{day}.db")
    cols = {r[1] for r in c.execute("PRAGMA table_info(cycle_log)")}
    print("===", day)
    print("price", Counter(r[0] for r in c.execute("select price_regime from cycle_log")).most_common(8))
    print("vol", Counter(r[0] for r in c.execute("select vol_regime from cycle_log")).most_common(6))
    if "ema_structure" in cols:
        print("ema", Counter(r[0] for r in c.execute("select ema_structure from cycle_log")).most_common(6))
    print("iv", Counter(r[0] for r in c.execute("select iv_behavior from cycle_log")).most_common(6))
    print("action", Counter(r[0] for r in c.execute("select action from cycle_log")).most_common(8))
    if "no_trade_reason" in cols:
        print(
            "reason",
            Counter(
                r[0]
                for r in c.execute(
                    "select no_trade_reason from cycle_log "
                    "where no_trade_reason is not null"
                )
            ).most_common(10),
        )
