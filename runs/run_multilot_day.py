import os, sys
os.chdir(r"C:\Users\Administrator\Desktop\algo-trading")
sys.path.insert(0, os.getcwd())

day = sys.argv[1]
out_dir = sys.argv[2] if len(sys.argv) > 2 else "runs/multilot_baseline"

import core
_orig = core.load_config

def load_config():
    c = _orig()
    object.__setattr__(c, "force_lots", None) if hasattr(c, "__setattr__") else None
    try:
        c.force_lots = None
    except Exception:
        pass
    return c

core.load_config = load_config

import backtest_engine as be
be.load_config = load_config

# dataclass may be frozen? patch after load inside main by monkeypatching Fill path
_old_main = be.main

def main():
    import argparse
    # Call original but intercept config
    from backtest_engine import (
        HistoricalStore, BacktestRunner, FillModel, print_report, Results
    )
    # Re-implement thin CLI from be.main args
    import backtest_engine as m
    parser = argparse.ArgumentParser()
    # steal args by setting sys.argv
    return _old_main()

# Patch load_config used at start of main
sys.argv = [
    "backtest_engine.py",
    "--db", f"data/per_day/nifty_algo_{day}.db",
    "--from", day, "--to", day,
    "--csv", f"{out_dir}/{day}.csv",
]
# Monkeypatch: after config load in main — wrap HistoricalStore path instead
_real_load = be.load_config
def _load():
    c = _orig()
    # Config is a dataclass — force_lots is mutable field
    c.force_lots = None
    return c
be.load_config = _load
core.load_config = _load
raise SystemExit(be.main())
