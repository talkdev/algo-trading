"""Same-process ENTER snapshot for listed days."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import strategy_engine as se

_orig_log = se.StrategyEngine._log_decision
_orig_persist = se.StrategyEngine._persist_decision


_OUT = open(ROOT / "Misc" / "_enter_snap_out.txt", "w", encoding="utf-8")


def _snap(self, signals, strategy_name, reason, tag=""):
    try:
        from core import now_ist
        t = now_ist().strftime("%H:%M:%S")
    except Exception:
        t = "?"
    try:
        _, loc, _, _ = self._session_range_pos(signals)
    except Exception:
        loc = "?"
    line = (
        f"{tag} {t} {strategy_name} "
        f"reason={(reason or '')[:120]} "
        f"fr={signals.get('final_regime')} "
        f"notes={str(signals.get('final_regime_notes') or '')[:90]} "
        f"px={signals.get('price_regime')} or={signals.get('or_condition')} "
        f"vol={signals.get('vol_regime')} adx={signals.get('adx_15')} "
        f"dte={signals.get('actual_dte')} loc={loc} "
        f"up={signals.get('day_up_used_pct')} dn={signals.get('day_down_used_pct')} "
        f"dm={signals.get('day_move_used_pct')} tape={signals.get('tape_state')} "
        f"fade_hi={signals.get('afternoon_high_fade')} "
        f"fade_lo={signals.get('afternoon_low_fade')} "
        f"contain={signals.get('wide_or_containment')} "
        f"floor={signals.get('_session_floor')} "
        f"spot={signals.get('spot')} "
        f"oh={signals.get('or_high')} ol={signals.get('or_low')}\n"
    )
    _OUT.write(line)
    _OUT.flush()


def _wrapped_log(self, signals, event, reason, name=None, params=None, *a, **k):
    if str(event) == "STRATEGY_SELECTED":
        _snap(self, signals, name, reason, tag="LOG")
    return _orig_log(self, signals, event, reason, name, params, *a, **k)


def _wrapped_persist(self, signals, strategy_name, reason, params, action):
    if strategy_name and str(strategy_name) not in ("NO_TRADE", "None"):
        _snap(self, signals, strategy_name, reason, tag=f"PERSIST:{action}")
    return _orig_persist(self, signals, strategy_name, reason, params, action)


se.StrategyEngine._log_decision = _wrapped_log
se.StrategyEngine._persist_decision = _wrapped_persist

from backtest_engine import _parallel_day_worker

days = sys.argv[1:] or ["2026-11-09"]
for d in days:
    db = ROOT / "data" / "per_day" / f"nifty_algo_{d}.db"
    _OUT.write(f"{'='*70} {d}\n")
    _OUT.flush()
    print("=" * 70, d, flush=True)
    out = _parallel_day_worker({
        "day": d,
        "db_paths": [str(db)],
        "force_lots": 0,
        "trade_report": "off",
        "fill_mode": "live",
        "fill_edge": 0.0,
        "stress_exit": 0.5,
        "mirror_live_aborts": False,
        "cycle_stride": 1,
        "calibration_resolved": False,
    })
    print("pnl", out.get("pnl"), "ok", out.get("ok"), flush=True)
    for t in out.get("trades") or []:
        print("TRADE", t, flush=True)
_OUT.close()
print("--- snap file ---")
print((ROOT / "Misc" / "_enter_snap_out.txt").read_text(encoding="utf-8"))
