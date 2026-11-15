import sqlite3
from pathlib import Path

days = [
    "2026-09-16",
    "2026-09-29",
    "2026-10-13",
    "2026-10-22",
    "2026-10-26",
    "2026-11-04",
    "2026-11-05",
    "2026-11-09",
    "2026-11-12",
    "2026-11-13",
]
per = Path("data/per_day")
main = Path("data/nifty_algo_v3.db")


def tables(con):
    return [
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1"
        )
    ]


def dump_day(label, path, day):
    print("=" * 70)
    print(label, path, "EXISTS" if path.exists() else "MISSING")
    if not path.exists():
        return
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    ts = tables(con)
    print("tables:", ", ".join(ts))
    for t in ts:
        n = con.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
        print(f"  {t}: {n}")
    if "daily_summary" in ts:
        cols = [r[1] for r in con.execute("PRAGMA table_info(daily_summary)")]
        print("daily_summary cols:", cols)
        q = "SELECT * FROM daily_summary"
        if "trading_date" in cols or "date" in cols:
            dc = "trading_date" if "trading_date" in cols else "date"
            q += f" WHERE {dc}=?"
            rows = con.execute(q, (day,)).fetchall()
        else:
            rows = con.execute(q).fetchall()
        for row in rows[:5]:
            print("  DS:", dict(row))
    if "session_state" in ts:
        cols = [r[1] for r in con.execute("PRAGMA table_info(session_state)")]
        rows = con.execute(
            "SELECT * FROM session_state WHERE trading_date=?", (day,)
        ).fetchall()
        if not rows:
            rows = con.execute("SELECT * FROM session_state LIMIT 1").fetchall()
        for row in rows[:1]:
            d = dict(row)
            keys = [
                k
                for k in d
                if any(
                    x in k.lower()
                    for x in (
                        "pnl",
                        "regime",
                        "trade",
                        "status",
                        "vix",
                        "reason",
                        "block",
                        "flat",
                        "day_",
                        "realized",
                        "open",
                        "close",
                        "spot",
                        "entries",
                        "halt",
                    )
                )
            ]
            print("session_state keys:", keys)
            for k in keys:
                v = d[k]
                if isinstance(v, str) and len(v) > 120:
                    v = v[:120] + "..."
                print(f"  {k}={v!r}")
    # trade-ish tables
    for t in ts:
        low = t.lower()
        if not any(
            x in low
            for x in (
                "trade",
                "position",
                "fill",
                "order",
                "pnl",
                "reject",
                "decision",
                "ledger",
                "cycle",
            )
        ):
            continue
        cols = [r[1] for r in con.execute(f"PRAGMA table_info([{t}])")]
        n = con.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
        print(f"** {t} n={n} cols={cols}")
        if n == 0:
            continue
        # filter by day if possible
        day_cols = [c for c in cols if c in ("trading_date", "date", "day")]
        where = ""
        params = ()
        if day_cols:
            where = f" WHERE {day_cols[0]}=?"
            params = (day,)
            n2 = con.execute(
                f"SELECT COUNT(*) FROM [{t}]{where}", params
            ).fetchone()[0]
            print(f"   filtered by {day_cols[0]}: {n2}")
            n = n2
        # pnl summary
        for c in cols:
            cl = c.lower()
            if cl in (
                "realized_pnl",
                "pnl",
                "net_pnl",
                "total_pnl",
                "exit_reason",
                "status",
                "strategy",
                "strategy_name",
                "side",
            ):
                try:
                    rows = con.execute(
                        f"SELECT [{c}], COUNT(*) FROM [{t}]{where} "
                        f"GROUP BY 1 ORDER BY 2 DESC LIMIT 12",
                        params,
                    ).fetchall()
                    print(f"   group {c}:", rows)
                except Exception as e:
                    print(f"   group {c} err: {e}")
        # sample rows
        sample_cols = [
            c
            for c in cols
            if any(
                x in c.lower()
                for x in (
                    "time",
                    "strateg",
                    "pnl",
                    "status",
                    "reason",
                    "entry",
                    "exit",
                    "name",
                    "side",
                )
            )
        ][:10]
        if not sample_cols:
            sample_cols = cols[:8]
        sel = ", ".join(f"[{c}]" for c in sample_cols)
        try:
            for row in con.execute(
                f"SELECT {sel} FROM [{t}]{where} LIMIT 8", params
            ):
                print("   sample:", dict(row))
        except Exception as e:
            print("   sample err:", e)
    con.close()


for d in days:
    dump_day(f"SHARD {d}", per / f"nifty_algo_{d}.db", d)

print("\n\n### MAIN DB daily_summary / trades for problem days ###")
if main.exists():
    con = sqlite3.connect(f"file:{main.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    ts = tables(con)
    print("main tables:", ", ".join(ts))
    for t in ("daily_summary", "trades", "closed_trades", "positions", "session_state"):
        if t not in ts:
            continue
        cols = [r[1] for r in con.execute(f"PRAGMA table_info([{t}])")]
        print(f"MAIN {t} cols:", cols)
        day_col = None
        for c in ("trading_date", "date", "day"):
            if c in cols:
                day_col = c
                break
        if day_col:
            q = f"SELECT * FROM [{t}] WHERE {day_col} IN ({','.join('?'*len(days))})"
            rows = con.execute(q, days).fetchall()
            print(f"  rows matching days: {len(rows)}")
            for row in rows:
                d = dict(row)
                # compact
                slim = {
                    k: (
                        (v[:100] + "...")
                        if isinstance(v, str) and len(v) > 100
                        else v
                    )
                    for k, v in d.items()
                    if v is not None
                    and k
                    in cols[:40]
                    or any(
                        x in k.lower()
                        for x in ("pnl", "date", "strateg", "reason", "status", "vix")
                    )
                }
                print(" ", slim)
        else:
            n = con.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
            print(f"  no day col, n={n}")
    con.close()
else:
    print("main missing")
