"""Diagnose why containment IC floor returns None on a flat day."""
import dataclasses
import sys
from collections import Counter
from datetime import time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import load_config, now_ist  # noqa: E402
from backtest_engine import (  # noqa: E402
    BacktestRunner,
    FillModel,
    _default_replay_sources,
    _open_store,
    _resolve_db_paths,
)
import strategy_engine as se  # noqa: E402

why: Counter = Counter()
samples: list = []
_orig = se.StrategyEngine._try_credit_floor_ic


def wrapped(self, signals, prior_reason):
    _or = str(signals.get("or_condition") or "")
    _notes = str(signals.get("final_regime_notes") or "")
    _contain = bool(signals.get("wide_or_containment")) or (
        "CONTAINMENT_PIN" in _notes
    )
    if _or not in ("NARROW", "VERY_NARROW", "MODERATE") and not (
        _or in ("WIDE", "VERY_WIDE") and _contain
    ):
        why["or_gate"] += 1
        return None
    if self._slot_conflict(se.IRON_CONDOR, signals):
        why["slot"] += 1
        return None
    try:
        _adx = float(signals.get("adx_15") or 0)
    except (TypeError, ValueError):
        _adx = 0.0
    _strong = float(getattr(self.config, "adx_strong_threshold", 28) or 28)
    _adx_cap = _strong
    if _contain:
        try:
            _iv_t = now_ist().time()
        except Exception:
            _iv_t = dtime(13, 0)
        _adx_cap = 40.0 if _iv_t >= dtime(13, 30) else 32.0
        try:
            _rng, _loc, _, _ = self._session_range_pos(signals)
        except Exception:
            _loc, _rng = 0.5, 0.0
        if not (0.20 <= _loc <= 0.80):
            why[f"loc_{_loc:.2f}"] += 1
            return None
        if _rng < 45:
            why[f"rng_{_rng:.0f}"] += 1
            return None
    if _adx >= _adx_cap:
        why[f"adx_{_adx:.0f}_ge_{_adx_cap}"] += 1
        return None
    _iv = str(signals.get("iv_behavior") or "").upper()
    try:
        _iv_t = now_ist().time()
    except Exception:
        _iv_t = dtime(12, 0)
    _iv_ok = (
        _iv in ("CRUSHING", "DECLINING")
        or (_iv in ("STABLE", "") and _iv_t >= dtime(12, 30))
        or (_contain and _iv_t >= dtime(13, 0))
    )
    if not _iv_ok and not _contain:
        why["iv"] += 1
        return None
    if _contain and not _iv_ok and _iv_t < dtime(13, 0):
        why["iv_early"] += 1
        return None
    _size = float(signals.get("size_multiplier") or 1.0)
    signals["_session_floor"] = True
    try:
        _params = self.compute_params(
            se.IRON_CONDOR, "dbg_floor", signals, min(_size, 0.45)
        )
    finally:
        signals.pop("_session_floor", None)
    if not _params.get("valid"):
        why[f"invalid:{str(_params.get('reason'))[:90]}"] += 1
        return None
    try:
        _nc = float(
            _params["net_credit"]
            if "net_credit" in _params and _params["net_credit"] is not None
            else _params.get("credit_pts") or 0
        )
    except (TypeError, ValueError):
        _nc = 0.0
    if _nc <= 0:
        why["nc0"] += 1
        if len(samples) < 3:
            samples.append(
                {
                    "keys": list(_params.keys())[:40],
                    "nc": _params.get("net_credit"),
                    "cp": _params.get("credit_pts"),
                    "valid": _params.get("valid"),
                    "reason": str(_params.get("reason") or "")[:80],
                    "gross": _params.get("gross_credit"),
                }
            )
        return None
    if self._apply_day_pnl_risk_envelope(_params, signals):
        why["envelope"] += 1
        return None
    why["WOULD_ENTER"] += 1
    return _orig(self, signals, prior_reason)


se.StrategyEngine._try_credit_floor_ic = wrapped

day = sys.argv[1] if len(sys.argv) > 1 else "2026-10-22"
config = dataclasses.replace(load_config(), force_lots=None)
paths = _resolve_db_paths(_default_replay_sources(config))
day_shard = [
    p for p in paths if f"nifty_algo_{day}.db" in str(p).replace("\\", "/")
]
store = _open_store(day_shard or paths)
eng = BacktestRunner(
    store=store,
    config=config,
    fills=FillModel(mode="live"),
    verbose=False,
    trade_report="off",
    cycle_reports=False,
)
eng.run([day])
print("pnl", dict(eng.results.daily_pnl))
print("trades", len(eng.results.trades))
print("samples", samples)
for k, v in why.most_common(25):
    print(f"{v:5d}  {k}")
