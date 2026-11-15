"""Capture Nov9 STRATEGY_SELECTED reason + tape snapshot at entry."""
from __future__ import annotations

from datetime import date

import strategy_engine as se
from backtest_engine import BacktestRunner

_orig = se.StrategyEngine._log_decision
_hits = []


def _spy(self, signals, event, reason, name=None, params=None, *a, **k):
    if event == "STRATEGY_SELECTED":
        try:
            from core import now_ist
            t = now_ist().strftime("%H:%M")
        except Exception:
            t = "?"
        _hits.append({
            "t": t,
            "name": name,
            "reason": reason,
            "fr": signals.get("final_regime"),
            "notes": str(signals.get("final_regime_notes") or "")[:80],
            "px": signals.get("price_regime"),
            "or": signals.get("or_condition"),
            "vol": signals.get("vol_regime"),
            "adx": signals.get("adx_15"),
            "dte": signals.get("actual_dte"),
            "loc": None,
            "up": signals.get("day_up_used_pct"),
            "dn": signals.get("day_down_used_pct"),
            "tape": signals.get("tape_state"),
            "fade_hi": signals.get("afternoon_high_fade"),
            "fade_lo": signals.get("afternoon_low_fade"),
            "contain": signals.get("wide_or_containment"),
            "floor": signals.get("_session_floor"),
            "spot": signals.get("spot"),
            "dh": signals.get("day_high_so_far") or signals.get("day_high"),
            "dl": signals.get("day_low_so_far") or signals.get("day_low"),
        })
        try:
            _, loc, _, _ = self._session_range_pos(signals)
            _hits[-1]["loc"] = round(float(loc), 3)
        except Exception:
            pass
    return _orig(self, signals, event, reason, name, params, *a, **k)


se.StrategyEngine._log_decision = _spy

runner = BacktestRunner(
    start=date(2026, 11, 9),
    end=date(2026, 11, 9),
    force_lots=0,
    trade_report="off",
)
res = runner.run()
print("RESULT", res)
for h in _hits:
    print(h)
