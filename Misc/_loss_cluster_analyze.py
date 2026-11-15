#!/usr/bin/env python3
"""Summarize natural-sizing blotter for loss days + share structure tags."""
from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path

CSV = Path(__file__).resolve().parents[1] / "Misc" / "bt_natural_lots.csv"
LOSS = {
    "2026-09-18",
    "2026-09-23",
    "2026-09-28",
    "2026-10-16",
    "2026-10-22",
    "2026-10-28",
    "2026-10-29",
    "2026-10-30",
    "2026-11-02",
    "2026-11-06",
}


def main() -> int:
    rows = list(csv.DictReader(CSV.open(encoding="utf-8")))
    by = defaultdict(list)
    for r in rows:
        by[r["trading_date"]].append(r)

    print("LOSS DAYS")
    for d in sorted(LOSS):
        day = by.get(d, [])
        pnl = sum(float(x["pnl_rs"]) for x in day)
        print(f"\n==== {d} day_pnl={pnl:.0f}")
        for x in day:
            print(
                f"  {x['entry_time'][:5]}->{x['exit_time'][:5]} "
                f"lots={x['lots']} {x['strategy']:16s} "
                f"pnl={float(x['pnl_rs']):8.0f} {x['exit_reason']:16s} "
                f"held={x['held_min']}m regime={(x.get('regime') or '')[:40]} "
                f"sel={(x.get('selection_reason') or x.get('reason') or '')[:55]}"
            )

    # losers overall
    losers = [r for r in rows if float(r["pnl_rs"]) < 0]
    print("\n\nALL LOSERS BY STRATEGY")
    c = Counter(r["strategy"] for r in losers)
    for k, n in c.most_common():
        s = sum(float(r["pnl_rs"]) for r in losers if r["strategy"] == k)
        print(f"  {k:16s} n={n} sum={s:.0f}")

    print("\nLOSS-DAY LOSERS BY EXIT")
    c2 = Counter(
        (r["strategy"], r["exit_reason"])
        for d, day in by.items()
        if d in LOSS
        for r in day
        if float(r["pnl_rs"]) < 0
    )
    for (s, e), n in c2.most_common():
        print(f"  {n}  {s} / {e}")

    # quick same-side reentry after stop on same day
    print("\nSAME-SIDE REENTRY AFTER STOP (all days)")
    for d, day in sorted(by.items()):
        for i, a in enumerate(day):
            if a["exit_reason"] != "CLOSE_STOP":
                continue
            for b in day[i + 1 :]:
                if b["strategy"] != a["strategy"]:
                    continue
                if b["entry_time"] <= a["exit_time"]:
                    continue
                print(
                    f"  {d} {a['strategy']} stop {a['exit_time'][:5]} "
                    f"pnl={float(a['pnl_rs']):.0f} -> reenter {b['entry_time'][:5]} "
                    f"pnl={float(b['pnl_rs']):.0f} {b['exit_reason']}"
                )
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
