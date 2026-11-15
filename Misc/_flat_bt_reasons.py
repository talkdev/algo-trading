"""Top NO_TRADE reasons from latest backtest writes on flat days."""
import os
import sqlite3
from collections import Counter

DAYS = ["2026-10-19", "2026-10-22", "2026-11-04", "2026-11-12", "2026-11-09"]


def main() -> None:
    for d in DAYS:
        path = os.path.join("data", "per_day", f"nifty_algo_{d}.db")
        con = sqlite3.connect(path)
        print("=" * 60, d)
        rows = con.execute(
            "SELECT action, reason FROM strategy_decisions"
        ).fetchall()
        print("  n=", len(rows))
        acts = Counter(a for a, _ in rows)
        print("  actions", acts)
        # split primary reason (before |)
        prim = Counter()
        for a, r in rows:
            if a != "NO_TRADE":
                prim[f"{a}:{(r or '')[:100]}"] += 1
                continue
            head = (r or "").split("|")[0][:90]
            prim[head] += 1
        for k, v in prim.most_common(15):
            print(f"  {v:5d}  {k}")
        # any ENTER / SELECTED
        sel = [r for a, r in rows if a != "NO_TRADE"]
        for s in sel[:5]:
            print("  SEL", (s or "")[:120])
        # trades
        te = con.execute(
            "SELECT strategy_name, entry_time, net_pnl_rupees FROM trade_entries "
            "LEFT JOIN trade_exits USING(trade_id)"
        ).fetchall()
        # simpler
        te = con.execute(
            "SELECT e.strategy_name, e.entry_time, x.net_pnl_rupees, e.actual_dte "
            "FROM trade_entries e LEFT JOIN trade_exits x ON e.trade_id=x.trade_id"
        ).fetchall()
        print("  trades", te)
        con.close()


if __name__ == "__main__":
    main()
