"""Replay classify_price/vol/final on shards for flat+red days — current code."""
from __future__ import annotations

import contextlib
import io
import logging
import warnings
from collections import Counter, defaultdict
from pathlib import Path

from backtest_engine import BacktestRunner, _open_store, make_fill_model
from core import load_config

logging.disable(logging.CRITICAL)
warnings.filterwarnings("ignore")

DAYS = [
    "2026-09-29", "2026-10-06", "2026-10-13", "2026-10-22", "2026-10-26",
    "2026-10-28", "2026-10-29", "2026-11-02", "2026-11-03", "2026-11-04",
    "2026-11-09", "2026-11-12",
]

cfg = load_config()
for day in DAYS:
    shard = Path(f"data/per_day/nifty_algo_{day}.db")
    paths = [str(shard.resolve())] if shard.exists() else [str(cfg.db_path)]
    store = _open_store(paths)
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        runner = BacktestRunner(
            store, cfg, make_fill_model(cfg, edge=0.0, stress_exit=0.5, mode="live"),
            verbose=False, trade_report="off", cycle_reports=True,
            live_db_path=str(cfg.db_path), mirror_live_aborts=False,
        )
        res = runner.run([day])
    pnl = float(res.daily_pnl.get(day, 0.0) or 0.0)
    # Pull regime samples from reason_log / trades; also scan runner if attrs
    px_c: Counter = Counter()
    fr_c: Counter = Counter()
    or_c: Counter = Counter()
    # cycle reports may be on runner
    cycles = getattr(res, "cycle_reports", None) or getattr(runner, "cycle_reports", None) or []
    inside_wide = 0
    n = 0
    for c in cycles:
        if not isinstance(c, dict):
            continue
        t = str(c.get("cycle_time") or c.get("time") or "")
        if len(t) >= 5:
            hhmm = t[-8:-3] if "T" in t or " " in t else t[:5]
        else:
            hhmm = ""
        if hhmm and hhmm < "10:00":
            continue
        n += 1
        px = str(c.get("price_regime") or "")
        fr = str(c.get("final_regime") or "")
        oc = str(c.get("or_condition") or "")
        px_c[px] += 1
        fr_c[fr] += 1
        or_c[oc] += 1
        try:
            sp = float(c.get("spot") or 0)
            oh = float(c.get("or_high") or 0)
            ol = float(c.get("or_low") or 0)
        except (TypeError, ValueError):
            sp = oh = ol = 0.0
        if oc in ("WIDE", "VERY_WIDE") and px == "RANGE" and oh > ol > 0 and ol <= sp <= oh:
            inside_wide += 1
    print(
        f"{day} pnl={pnl:.0f} trades={len(res.trades)} "
        f"cycles_ge10={n} inside_wide_RANGE={inside_wide}"
    )
    print(f"  or={or_c.most_common(4)}")
    print(f"  px={px_c.most_common(5)}")
    print(f"  fr={fr_c.most_common(5)}")
    if res.trades:
        t0 = res.trades[0]
        print(
            "  first_trade",
            getattr(t0, "entry_time", None),
            getattr(t0, "strategy", None),
            getattr(t0, "regime", None),
            round(float(getattr(t0, "pnl_rs", 0) or 0), 1),
        )
