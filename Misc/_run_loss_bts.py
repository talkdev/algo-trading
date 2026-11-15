#!/usr/bin/env python3
"""Run quiet per-day backtests for the loss cluster and print blotter summaries."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

DAYS = [
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
]
BASE = Path(__file__).resolve().parents[1]
OUT = BASE / "Misc" / "bt_loss_cluster"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for d in DAYS:
        csv = OUT / f"{d}.csv"
        cmd = [
            sys.executable,
            str(BASE / "backtest_engine.py"),
            "--from",
            d,
            "--to",
            d,
            "--force-lots",
            "0",
            "--trade-report",
            "off",
            "--allow-unfaithful",
            "--csv",
            str(csv),
            "--workers",
            "1",
        ]
        print(f"\n===== {d} =====", flush=True)
        r = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True)
        tail = (r.stdout or "").strip().splitlines()[-25:]
        for line in tail:
            print(line)
        if r.returncode not in (0,):
            print("STDERR tail:", file=sys.stderr)
            print("\n".join((r.stderr or "").splitlines()[-20:]), file=sys.stderr)
        if csv.exists():
            import csv as csvmod

            rows = list(csvmod.DictReader(csv.open(encoding="utf-8")))
            pnl = 0.0
            for row in rows:
                try:
                    pnl += float(row.get("pnl_rs") or row.get("net_pnl_rupees") or 0)
                except ValueError:
                    pass
            print(f"CSV rows={len(rows)} pnl≈{pnl:.0f}")
            for row in rows[:12]:
                keys = list(row.keys())
                et = row.get("entry_time") or row.get("entry") or ""
                xt = row.get("exit_time") or row.get("exit") or ""
                sn = row.get("strategy_name") or row.get("strategy") or ""
                pr = row.get("pnl_rs") or row.get("net_pnl_rupees") or ""
                xr = row.get("exit_reason") or ""
                sr = row.get("selection_reason") or row.get("reason") or ""
                print(f"  {et[11:16]}->{xt[11:16]} {sn} {pr} {xr[:40]} | {sr[:55]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
