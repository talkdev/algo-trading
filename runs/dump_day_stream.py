#!/usr/bin/env python3
"""Dump full-day signal + decision stream for swing/chop diagnosis."""
from __future__ import annotations

import csv
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from backtest_engine import (  # noqa: E402
    BacktestRunner, FillModel, HistoricalStore, load_config,
)


def dump_day(day: str, out: Path) -> None:
    db = ROOT / "data" / "per_day" / f"nifty_algo_{day}.db"
    config = load_config()
    store = HistoricalStore(str(db))
    runner = BacktestRunner(
        store, config, FillModel(0.25, 0.50), verbose=False, trade_report="off",
    )
    rows = []
    orig_build = runner._build

    def _row(signals, action="", strat="", reason="", in_trade=""):
        t = runner.clock.now().strftime("%H:%M:%S")
        day_h = signals.get("day_high_so_far") or signals.get("day_high")
        day_l = signals.get("day_low_so_far") or signals.get("day_low")
        spot = signals.get("spot")
        loc = ""
        try:
            dh, dl, sp = float(day_h), float(day_l), float(spot)
            if dh > dl:
                loc = f"{(sp - dl) / (dh - dl):.3f}"
        except (TypeError, ValueError):
            pass
        return {
            "time": t,
            "spot": spot,
            "or_h": signals.get("or_high"),
            "or_l": signals.get("or_low"),
            "or_cond": signals.get("or_condition"),
            "day_h": day_h,
            "day_l": day_l,
            "loc": loc,
            "px": signals.get("price_regime"),
            "vol": signals.get("vol_regime"),
            "pos": signals.get("positioning_regime"),
            "final": signals.get("final_regime"),
            "notes": (signals.get("final_regime_notes") or "")[:140],
            "adx": signals.get("adx_15"),
            "adx_m": int(bool(signals.get("adx_15_mature"))),
            "dmu": signals.get("day_move_used_pct"),
            "conf": signals.get("confidence_level"),
            "dte": signals.get("actual_dte"),
            "choppy": int(bool(signals.get("choppy_detected"))),
            "fade_hi": int(bool(signals.get("afternoon_high_fade"))),
            "fade_lo": int(bool(signals.get("afternoon_low_fade"))),
            "fb_lo": int(bool(signals.get("failed_break_low"))),
            "fb_hi": int(bool(signals.get("failed_break_high"))),
            "action": action,
            "strat": strat or "",
            "reason": (reason or "")[:200],
            "in_trade": in_trade,
        }

    def build():
        orig_build()
        orig_dec = runner.se.decide
        orig_cls = runner._classify

        def classify(signals):
            s = orig_cls(signals)
            rows.append(_row(s))
            return s

        def decide(signals):
            d = orig_dec(signals)
            if rows:
                rows[-1]["action"] = d.get("action")
                rows[-1]["strat"] = d.get("strategy_name") or ""
                rows[-1]["reason"] = (d.get("reason") or "")[:200]
                rows[-1]["fade_hi"] = int(bool(signals.get("afternoon_high_fade")))
                rows[-1]["fade_lo"] = int(bool(signals.get("afternoon_low_fade")))
                rows[-1]["final"] = signals.get("final_regime")
                rows[-1]["notes"] = (signals.get("final_regime_notes") or rows[-1]["notes"])[:140]
            return d

        runner._classify = classify
        runner.se.decide = decide

    runner._build = build
    runner.run([day])

    out.parent.mkdir(parents=True, exist_ok=True)
    keys = list(rows[0].keys()) if rows else []
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)

    print(f"[{day}] wrote {len(rows)} cycles -> {out}")
    print("  actions:", Counter(r.get("action") for r in rows))
    print("  px:", Counter(r.get("px") for r in rows).most_common(8))
    print("  final:", Counter(r.get("final") for r in rows).most_common(8))
    print("  ENTERS:")
    for r in rows:
        if r.get("action") == "ENTER":
            print(
                f"    {r['time']} {r['strat']} spot={r['spot']} loc={r['loc']} "
                f"final={r['final']} fade_hi={r['fade_hi']} fade_lo={r['fade_lo']} "
                f"{(r['reason'] or '')[:100]}"
            )
    print("  top reasons:")
    for k, v in Counter((r.get("reason") or "")[:90] for r in rows if r.get("reason")).most_common(12):
        print(f"    {v:4d}  {k}")
    # extreme windows where engine stood aside
    print("  extreme stand-asides (loc>=0.85 or <=0.15, NO_TRADE, 10:45-14:00):")
    n = 0
    for r in rows:
        try:
            loc = float(r["loc"]) if r.get("loc") not in (None, "") else None
        except ValueError:
            loc = None
        if loc is None:
            continue
        t = r["time"]
        if t < "10:45:00" or t > "14:00:00":
            continue
        if r.get("action") not in ("NO_TRADE", "", None):
            continue
        if loc >= 0.85 or loc <= 0.15:
            if n < 25:
                print(
                    f"    {t} loc={loc:.3f} spot={r['spot']} px={r['px']} "
                    f"final={r['final']} chop={r['choppy']} "
                    f"{(r.get('reason') or r.get('notes') or '')[:90]}"
                )
            n += 1
    print(f"  extreme stand-aside count: {n}")


if __name__ == "__main__":
    days = sys.argv[1:] or [
        "2026-09-10", "2026-09-16", "2026-09-17",
    ]
    for d in days:
        dump_day(d, ROOT / "runs" / "streams" / f"{d}.csv")
