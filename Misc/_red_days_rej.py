"""Single-day rejection dumps for fix22 reds + Oct6 control."""
import contextlib
import io
import logging
import re
import warnings
from collections import Counter
from pathlib import Path

from backtest_engine import BacktestRunner, _open_store, make_fill_model
from core import load_config

logging.disable(logging.CRITICAL)
warnings.filterwarnings("ignore")

cfg = load_config()
for day in ("2026-09-29", "2026-10-06", "2026-10-28", "2026-11-02"):
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
    pnl = res.daily_pnl.get(day, 0.0)
    print(f"==== {day} pnl={pnl:.2f} trades={len(res.trades)}")
    for t in res.trades:
        print(
            " TRADE", getattr(t, "entry_time", None), getattr(t, "strategy", None),
            getattr(t, "regime", None), "pnl", round(float(getattr(t, "pnl_rs", 0) or 0), 1),
            getattr(t, "exit_reason", None),
        )
    mid = Counter()
    for _d, _bucket, reason in res.reason_log:
        r = str(reason)
        if "BEFORE_09" in r or "OR_NOT" in r or "OBSERVING" in r:
            continue
        mid[re.sub(r"[\d.]+", "N", r)[:130]] += 1
    for k, v in mid.most_common(12):
        print(f"  {v:5d}  {k}")
