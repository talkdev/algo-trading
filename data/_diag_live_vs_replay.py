"""Live vs replay divergence diagnostics for 2026-09-11 and 2026-09-21."""
from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path

LIVE = Path("data/nifty_algo_v3.db")


def connect(path: Path):
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    return con


def day_decision_summary(con, day: str, limit: int = 40):
    cur = con.cursor()
    print(f"\n===== {day} LIVE decision action census =====")
    for r in cur.execute(
        """
        SELECT action, COUNT(*) c
        FROM strategy_decisions WHERE trading_date=?
        GROUP BY 1 ORDER BY c DESC
        """,
        (day,),
    ):
        print(dict(r))

    print(f"\n===== {day} top NO_TRADE reasons =====")
    for r in cur.execute(
        """
        SELECT substr(reason,1,100) reason, COUNT(*) c
        FROM strategy_decisions
        WHERE trading_date=? AND action='NO_TRADE'
        GROUP BY 1 ORDER BY c DESC LIMIT ?
        """,
        (day, limit),
    ):
        print(dict(r))

    print(f"\n===== {day} SELECT/ENTER =====")
    for r in cur.execute(
        """
        SELECT decision_time, action, strategy_name, substr(reason,1,160) reason
        FROM strategy_decisions
        WHERE trading_date=? AND action IN ('ENTER','STRATEGY_SELECTED')
        ORDER BY decision_time
        """,
        (day,),
    ):
        print(dict(r))


def snapshot_regime_nulls(con, day: str):
    cur = con.cursor()
    print(f"\n===== {day} snapshot regime null rates =====")
    row = cur.execute(
        """
        SELECT COUNT(*) n,
               SUM(CASE WHEN price_regime IS NULL THEN 1 ELSE 0 END) pr_null,
               SUM(CASE WHEN final_regime IS NULL THEN 1 ELSE 0 END) fr_null,
               SUM(CASE WHEN vol_regime IS NULL THEN 1 ELSE 0 END) vr_null,
               SUM(CASE WHEN adx_15 IS NULL OR adx_15=0 THEN 1 ELSE 0 END) adx0
        FROM market_snapshots WHERE date=?
        """,
        (day,),
    ).fetchone()
    print(dict(row))
    # sample non-null
    print("first non-null final_regime rows:")
    for r in cur.execute(
        """
        SELECT time, spot, adx_15, price_regime, final_regime, confidence_level
        FROM market_snapshots
        WHERE date=? AND final_regime IS NOT NULL
        ORDER BY time LIMIT 8
        """,
        (day,),
    ):
        print(dict(r))


def cycle_log_probe(con, day: str):
    cur = con.cursor()
    tabs = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "cycle_log" not in tabs:
        print("no cycle_log")
        return
    cols = [d[1] for d in cur.execute("PRAGMA table_info(cycle_log)")]
    print(f"\n===== {day} cycle_log cols =====", cols[:25])
    # try common shapes
    for q in (
        f"SELECT COUNT(*) c FROM cycle_log WHERE trading_date='{day}'",
        f"SELECT COUNT(*) c FROM cycle_log WHERE date='{day}'",
    ):
        try:
            print(q, dict(cur.execute(q).fetchone()))
        except Exception as e:
            print(q, type(e).__name__, e)


def sep11_momentum_window(con):
    cur = con.cursor()
    print("\n===== 2026-09-11 decisions mentioning MOMENTUM/LONG/EVENT =====")
    for r in cur.execute(
        """
        SELECT decision_time, action, strategy_name, substr(reason,1,160) reason
        FROM strategy_decisions
        WHERE trading_date='2026-09-11'
          AND (
            reason LIKE '%MOMENTUM%' OR reason LIKE '%LONG_%'
            OR reason LIKE '%EVENT%' OR strategy_name LIKE 'LONG%'
            OR reason LIKE '%confidence%'
          )
        ORDER BY decision_time
        LIMIT 60
        """
    ):
        print(dict(r))


def sep21_afternoon(con):
    cur = con.cursor()
    print("\n===== 2026-09-21 12:50-14:00 decisions =====")
    for r in cur.execute(
        """
        SELECT decision_time, action, strategy_name, substr(reason,1,160) reason
        FROM strategy_decisions
        WHERE trading_date='2026-09-21'
          AND decision_time >= '2026-09-21T12:50:00'
          AND decision_time <= '2026-09-21T14:00:00'
        ORDER BY decision_time
        LIMIT 80
        """
    ):
        print(dict(r))


def main():
    con = connect(LIVE)
    for day in ("2026-09-11", "2026-09-21"):
        day_decision_summary(con, day)
        snapshot_regime_nulls(con, day)
        cycle_log_probe(con, day)
    sep11_momentum_window(con)
    sep21_afternoon(con)


if __name__ == "__main__":
    main()
