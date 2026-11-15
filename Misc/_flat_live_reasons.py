"""Run one-day BT in-process and print primary NO_TRADE heads."""
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from core import load_config  # noqa: E402
from backtest_engine import (  # noqa: E402
    BacktestRunner,
    FillModel,
    _default_replay_sources,
    _open_store,
    _resolve_db_paths,
)


def analyze(day: str) -> None:
    config = load_config()
    import dataclasses
    config = dataclasses.replace(config, force_lots=None)
    paths = _resolve_db_paths(_default_replay_sources(config))
    # Prefer the day's shard only for speed.
    day_shard = [
        p for p in paths
        if f"nifty_algo_{day}.db" in str(p).replace("\\", "/")
    ]
    store = _open_store(day_shard or paths)
    fills = FillModel(mode="live")
    eng = BacktestRunner(
        store=store,
        config=config,
        fills=fills,
        verbose=False,
        trade_report="off",
        cycle_reports=False,
    )
    eng.run([day])
    res = eng.results
    print("=" * 60, day, "pnl", round(res.daily_pnl.get(day, 0), 2))
    prim = Counter()
    mom_body = Counter()
    for d, bucket, reason in res.reason_log:
        if d != day:
            continue
        head = reason.split("|")[0][:110]
        prim[f"{bucket} :: {head}"] += 1
        for part in reason.split("|"):
            if "momentum" in part.lower():
                mom_body[part[:100]] += 1
                break
    print("  primary (top 25):")
    for k, v in prim.most_common(25):
        print(f"  {v:5d}  {k}")
    print("  momentum bodies:")
    for k, v in mom_body.most_common(15):
        print(f"  {v:5d}  {k}")


if __name__ == "__main__":
    days = sys.argv[1:] or [
        "2026-10-19", "2026-10-22", "2026-11-04", "2026-11-12",
    ]
    for d in days:
        analyze(d)
