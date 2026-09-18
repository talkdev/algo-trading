import os, sys, dataclasses
os.chdir(r"C:\Users\Administrator\Desktop\algo-trading")
sys.path.insert(0, os.getcwd())
day = sys.argv[1]
outdir = sys.argv[2]
import core
_orig = core.load_config
def load_config():
    c = _orig()
    return dataclasses.replace(c, force_lots=None)
core.load_config = load_config
import backtest_engine as be
be.load_config = load_config
sys.argv = ["backtest_engine.py", "--db", f"data/per_day/nifty_algo_{day}.db",
            "--from", day, "--to", day, "--csv", f"{outdir}/{day}.csv"]
raise SystemExit(be.main())
