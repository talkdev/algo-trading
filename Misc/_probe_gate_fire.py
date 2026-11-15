#!/usr/bin/env python3
"""Unit-check whether current refusal gates fire on red-entry snaps."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from strategy_engine import (
    BEAR_CALL_SPREAD,
    BULL_PUT_SPREAD,
    LONG_CALL,
    LONG_PUT,
    StrategyEngine,
)

IST = ZoneInfo("Asia/Kolkata")
PER = Path(__file__).resolve().parents[1] / "data" / "per_day"

CASES = [
    ("2026-10-16", "11:00", LONG_CALL),
    ("2026-10-22", "12:12", BEAR_CALL_SPREAD),
    ("2026-10-28", "10:07", BEAR_CALL_SPREAD),
    ("2026-10-29", "13:06", BEAR_CALL_SPREAD),
    ("2026-10-30", "09:49", BULL_PUT_SPREAD),
    ("2026-10-30", "11:08", BEAR_CALL_SPREAD),
    ("2026-11-02", "12:46", BULL_PUT_SPREAD),
    ("2026-11-06", "12:25", BEAR_CALL_SPREAD),
    ("2026-11-12", "11:03", BULL_PUT_SPREAD),
    ("2026-09-28", "10:19", LONG_PUT),
]


def snap_signals(day: str, hhmm: str) -> dict:
    con = sqlite3.connect(str(PER / f"nifty_algo_{day}.db"))
    cols = [r[1] for r in con.execute("PRAGMA table_info(market_snapshots)")]
    row = con.execute(
        "SELECT * FROM market_snapshots WHERE time >= ? "
        "ORDER BY timestamp LIMIT 1",
        (hhmm + ":00",),
    ).fetchone()
    con.close()
    d = dict(zip(cols, row)) if row else {}
    out = {k: d.get(k) for k in cols}
    # normalize None → empty for string fields used by gates
    for k in (
        "final_regime", "price_regime", "vol_regime", "confidence_level",
        "ema_structure", "iv_behavior",
    ):
        if out.get(k) is None:
            out[k] = ""
    return out


def main() -> int:
    eng = StrategyEngine.__new__(StrategyEngine)

    class Cfg:
        adx_trend_threshold = 20.0
        adx_strong_threshold = 28.0
        momentum_day_move_max_pct = 90.0
        counter_trend_entry_block = True

    class ME:
        state = {
            "session_credit_target_count": 0,
            "last_exit_reason": "",
            "last_flat_credit_reason": "",
            "last_exit_strategy_side": "",
            "last_flat_credit_side": "",
        }

    eng.config = Cfg()
    eng.market_engine = ME()
    eng.TWO_WAY_FADE_HI = 0.80
    eng.TWO_WAY_FADE_LO = 0.20

    def _session_range_pos(signals):
        return 100.0, 0.55, 0.0, 0.0

    eng._session_range_pos = _session_range_pos
    eng._day_structure_bearish = lambda s: (False, "test")
    eng._extreme_high_call_fade_ok = lambda s, a, l: False
    eng._low_fade_bounce_confirmed = lambda s: False
    eng._open_strategy_names = lambda: []

    # patch now_ist via injecting — gates call now_ist(); use sim via state
    import strategy_engine as se

    for day, hhmm, sn in CASES:
        sig = snap_signals(day, hhmm)
        hh, mm = map(int, hhmm.split(":"))
        fake = datetime(2026, int(day[5:7]), int(day[8:10]), hh, mm, tzinfo=IST)

        def _now(fake=fake):
            return fake

        se.now_ist = _now
        hard = eng._hard_credit_into_trend_refusal(sn, sig)
        aft = eng._afternoon_range_credit_refusal(sn, sig)
        ct = eng._counter_trend_entry_refusal(sn, sig)
        print(
            f"{day} {hhmm} {sn:16s} "
            f"px={sig.get('price_regime')} final={sig.get('final_regime')} "
            f"adx={sig.get('adx_15')} dm={sig.get('day_move_used_pct')} "
            f"| hard={hard} aft={aft} ct={ct}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
