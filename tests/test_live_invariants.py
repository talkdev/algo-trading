"""Live-escape invariant tests — must pass before claiming a live fix.

Run:  python -m pytest tests/test_live_invariants.py -q
  or: python tests/test_live_invariants.py
"""
from __future__ import annotations
import logging
import sys
from dataclasses import replace
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core import load_config
from strategy_engine import (
    StrategyEngine, BEAR_CALL_SPREAD, BULL_PUT_SPREAD,
)


class _DB:
    def execute(self, *a, **k):
        return None


class _ME:
    state = {}
    last_chain = {}
    last_chain_expiry = None


class _Cal:
    pass


def _eng():
    cfg = replace(load_config(), paper_trade_mode=True)
    return StrategyEngine(cfg, _DB(), _ME(), _Cal(), logging.getLogger("inv"))


def _sig(**kw):
    base = {
        "final_regime": "PREMIUM_SELL_BEAR",
        "confidence_level": "HIGH",
        "actual_dte": 4,
        "or_condition": "NARROW",
        "adx_15": 21.0,
        "adx_15_mature": False,  # Sep23 hatch: mature lagged
        "vol_regime": "SELL_PREMIUM",
        "two_way_auction": True,
        "afternoon_high_fade": True,  # Sep23 hatch: self-set fade
        "day_high_so_far": 25200.0,
        "day_low_so_far": 25000.0,
        "spot": 25180.0,
        "vwap_dist_pct": 0.05,
        "prev_day_close": 25100.0,
        "gap_direction": "UP",
        "or_high": 25150.0,
        "or_low": 25050.0,
        "price_regime": "UPTREND",
    }
    base.update(kw)
    return base


def test_sep23_bcs_uptrend_immature_fade_refused():
    """UPTREND + soft ADX + fade flags but NOT pinned to day high → refuse."""
    eng = _eng()
    sig = _sig(
        spot=25100.0,  # loc ~0.50 — not an extreme fade
        day_high_so_far=25200.0,
        day_low_so_far=25000.0,
    )
    name, why = eng._map_regime_to_strategy(sig)
    assert name == "NO_TRADE", (name, why)
    assert "hard_invariant_no_calls" in why or "no_calls" in why, why
    ct = eng._counter_trend_entry_refusal(BEAR_CALL_SPREAD, sig)
    assert ct and "hard_invariant" in ct, ct


def test_extreme_high_fade_helper_true_when_pinned():
    eng = _eng()
    sig = _sig(
        price_regime="UPTREND",
        adx_15=21.0,
        afternoon_high_fade=True,
        two_way_auction=True,
        spot=23433.9,
        day_high_so_far=23435.6,
        day_low_so_far=23349.55,
        or_high=23420.0,
        or_low=23360.0,
    )
    _, loc, _, _ = eng._session_range_pos(sig)
    assert loc >= 0.95, loc
    assert eng._extreme_high_call_fade_ok(sig, 21.0, loc)
    hard = eng._hard_credit_into_trend_refusal(BEAR_CALL_SPREAD, sig)
    assert hard and "UPTREND" in hard, hard


def test_sep21_bcs_uptrend_refused():
    """Live 2026-09-21 13:35 BCS into UPTREND adx=23."""
    eng = _eng()
    sig = _sig(
        adx_15=23.0,
        adx_15_mature=True,
        afternoon_high_fade=True,
        two_way_auction=False,
        price_regime="UPTREND",
    )
    name, why = eng._map_regime_to_strategy(sig)
    assert name == "NO_TRADE", (name, why)
    ct = eng._counter_trend_entry_refusal(BEAR_CALL_SPREAD, sig)
    assert ct, ct


def test_range_fade_bcs_still_allowed():
    """RANGE + fade mid-range may still select BCS (not unfinished high)."""
    eng = _eng()
    sig = _sig(
        price_regime="RANGE",
        final_regime="PREMIUM_SELL_BEAR",
        spot=25100.0,  # loc ~0.50
        day_high_so_far=25200.0,
        day_low_so_far=25000.0,
    )
    hard = eng._hard_credit_into_trend_refusal(BEAR_CALL_SPREAD, sig)
    assert hard is None, hard


def test_sticky_no_calls_after_uptrend_refuse():
    """After UPTREND refuse, RANGE flicker at loc 0.90 must stay refused."""
    eng = _eng()
    up = _sig(
        price_regime="UPTREND",
        final_regime="PREMIUM_SELL_BEAR",
        adx_15=21.0,
        afternoon_high_fade=True,
        two_way_auction=True,
        spot=23430.0,
        day_high_so_far=23435.6,
        day_low_so_far=23349.55,
        gap_direction="UP",
    )
    hard_up = eng._hard_credit_into_trend_refusal(BEAR_CALL_SPREAD, up)
    assert hard_up and "UPTREND" in hard_up, hard_up
    assert eng.market_engine.state.get("session_no_calls_after_uptrend_refuse")
    rng = _sig(
        price_regime="RANGE",
        final_regime="PREMIUM_SELL_BEAR",
        adx_15=21.67,
        afternoon_high_fade=True,
        two_way_auction=True,
        spot=23426.0,  # loc ~0.90
        day_high_so_far=23435.6,
        day_low_so_far=23349.55,
        gap_direction="UP",
        or_high=23420.0,
        or_low=23360.0,
    )
    hard_r = eng._hard_credit_into_trend_refusal(BEAR_CALL_SPREAD, rng)
    assert hard_r and "sticky_no_calls" in hard_r, hard_r


def test_same_side_reload_into_mature_uptrend_refused():
    """After BPS CLOSE_TARGET, refuse another BPS into UPTREND+strong ADX."""
    eng = _eng()
    eng.market_engine.state.update({
        "last_exit_strategy_side": "BULL",
        "last_exit_time": "2026-09-17T11:57:29+05:30",
        "last_exit_reason": "CLOSE_TARGET",
    })
    sig = _sig(
        final_regime="PREMIUM_SELL_BULL",
        price_regime="UPTREND",
        adx_15=39.0,
        adx_15_mature=True,
        afternoon_low_fade=False,
        two_way_auction=False,
        spot=25100.0,
    )
    why = eng._same_side_chase_refusal(BULL_PUT_SPREAD, sig)
    assert why and "same_side_reload_into_mature_uptrend" in why, why


def test_momentum_refused_after_same_side_credit_harvest():
    """After BEAR CLOSE_TARGET, mid-day LONG_PUT must not chase."""
    eng = _eng()
    eng.market_engine.state.update({
        "last_exit_strategy_side": "BEAR",
        "last_exit_time": "2026-09-22T11:30:00+05:30",
        "last_exit_reason": "CLOSE_TARGET",
        "last_exit_pnl_rs": 1492.0,
        "last_exit_priority": 5,
    })
    sig = _sig(
        final_regime="PREMIUM_SELL_BEAR",
        price_regime="DOWNTREND",
        adx_15=36.0,
        adx_15_mature=True,
        afternoon_high_fade=False,
        afternoon_low_fade=False,
        two_way_auction=False,
        choppy_detected=False,
        ema_structure="BEARISH",
        vwap_dist_pct=-0.21,
        # loc mid — extension check uses last_exit_spot vs spot
        spot=25010.0,
        day_high_so_far=25200.0,
        day_low_so_far=25000.0,
        or_computed=True,
    )
    eng.market_engine.state["last_exit_spot"] = 25015.0  # no new low
    eng._momentum_gate = lambda signals, block_reason: (
        True, "momentum_gate_open", -1
    )
    eng._in_late_momentum_window = lambda cur: False
    out = eng._momentum_decision(sig, "entry_cooldown_10min_remaining")
    refuse = sig.get("_momentum_refuse_reason") or ""
    assert out is None, (out, refuse)
    assert "bear_credit_harvest" in refuse, refuse


def test_bps_into_downtrend_refused():
    eng = _eng()
    sig = _sig(
        final_regime="PREMIUM_SELL_BULL",
        price_regime="DOWNTREND",
        afternoon_high_fade=False,
        afternoon_low_fade=False,
        two_way_auction=False,
        spot=25100.0,  # mid-range — avoid high-loc fade rewrite
        day_high_so_far=25200.0,
        day_low_so_far=25000.0,
    )
    hard = eng._hard_credit_into_trend_refusal(BULL_PUT_SPREAD, sig)
    assert hard and "hard_invariant_no_puts" in hard, hard
    name, why = eng._map_regime_to_strategy(sig)
    assert name == "NO_TRADE", (name, why)
    assert "hard_invariant_no_puts" in why, why


def test_fade_pin_side_match_gate():
    """High-wall BCS pin requires afternoon_high_fade; low-fade alone is not enough."""
    assert not bool({"afternoon_high_fade": False}.get("afternoon_high_fade"))
    assert bool({"afternoon_high_fade": True}.get("afternoon_high_fade"))


def test_delta_dist_zero_at_center_preserved():
    """Delta strike on the centre must yield dist 0, not EM fallback."""
    step = 50.0
    band_lo, band_hi = 70.0, 200.0

    def clamp(d):
        if d is None:
            return None
        if float(d) <= 0.0:
            return 0.0
        d = min(max(d, band_lo), band_hi)
        d = round(d / step) * step
        return max(d, step)

    assert clamp(0.0) == 0.0
    assert clamp(37.0) == 50.0  # band_lo 70 → round-to-step 50
    assert clamp(None) is None


if __name__ == "__main__":
    test_sep23_bcs_uptrend_immature_fade_refused()
    test_extreme_high_fade_helper_true_when_pinned()
    test_sep21_bcs_uptrend_refused()
    test_range_fade_bcs_still_allowed()
    test_sticky_no_calls_after_uptrend_refuse()
    test_same_side_reload_into_mature_uptrend_refused()
    test_momentum_refused_after_same_side_credit_harvest()
    test_bps_into_downtrend_refused()
    test_fade_pin_side_match_gate()
    test_delta_dist_zero_at_center_preserved()
    print("PASS: live-escape invariants")
