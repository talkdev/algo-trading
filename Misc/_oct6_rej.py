"""Dump Oct-6 rejection detail from a single-day replay."""
import contextlib
import dataclasses
import io
import logging
import warnings
from pathlib import Path

from backtest_engine import BacktestRunner, _open_store, make_fill_model
from core import load_config

logging.disable(logging.CRITICAL)
warnings.filterwarnings("ignore")

day = "2026-10-06"
cfg = load_config()
shard = Path(f"data/per_day/nifty_algo_{day}.db")
paths = [str(shard.resolve())] if shard.exists() else [str(cfg.db_path)]
store = _open_store(paths)
sink = io.StringIO()
with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
    runner = BacktestRunner(
        store, cfg, make_fill_model(cfg, edge=0.0, stress_exit=0.5, mode="live"),
        verbose=False, trade_report="off", cycle_reports=False,
        live_db_path=str(cfg.db_path), mirror_live_aborts=False,
    )
    res = runner.run([day])

print("pnl", res.daily_pnl.get(day, 0.0), "trades", len(res.trades))
print("top buckets:")
for k, v in res.rejections.most_common(10):
    print(f"  {v:5d}  {k}")
# Sample mid/late session reasons from reason_log
from collections import Counter
mid = Counter()
for _d, _bucket, reason in res.reason_log:
    r = str(reason)
    if "BEFORE_09" in r or "OR_NOT" in r or "OBSERVING" in r:
        continue
    # collapse digits
    import re
    mid[re.sub(r"[\d.]+", "N", r)[:120]] += 1
print("mid+ session reasons:")
for k, v in mid.most_common(15):
    print(f"  {v:5d}  {k}")
