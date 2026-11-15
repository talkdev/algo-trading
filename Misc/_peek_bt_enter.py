#!/usr/bin/env python3
"""Replay one day and print price/final/adx at each ENTER."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# Hook StrategyEngine decision path by wrapping after import.
import strategy_engine as se

_orig = se.StrategyEngine._persist_decision


def _wrapped(self, signals, strategy_name, reason, params, action):
    if action == "ENTER":
        print(
            f"ENTER {strategy_name} reason={(reason or '')[:60]} "
            f"final={signals.get('final_regime')} px={signals.get('price_regime')} "
            f"vol={signals.get('vol_regime')} conf={signals.get('confidence_level')} "
            f"adx={signals.get('adx_15')} dm={signals.get('day_move_used_pct')} "
            f"up={signals.get('day_up_used_pct')} iv={signals.get('iv_behavior')} "
            f"loc={signals.get('session_loc') or signals.get('day_location')} "
            f"fade_hi={signals.get('afternoon_high_fade')} "
            f"fade_lo={signals.get('afternoon_low_fade')} "
            f"ema={signals.get('ema_structure')} "
            f"two={signals.get('two_way_auction')}"
        )
    return _orig(self, signals, strategy_name, reason, params, action)


se.StrategyEngine._persist_decision = _wrapped

day = sys.argv[1] if len(sys.argv) > 1 else "2026-10-29"
db = ROOT / "data" / "per_day" / f"nifty_algo_{day}.db"
from backtest_engine import main as _  # noqa: F401
import backtest_engine as be

# Build argv for backtest
sys.argv = [
    "backtest_engine.py",
    "--db", str(db),
    "--from", day,
    "--to", day,
    "--force-lots", "0",
    "--trade-report", "off",
    "--allow-unfaithful",
    "--workers", "1",
]
be.main()
