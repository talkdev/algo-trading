# tape_state_engine.py
# Additive WHEN-layer: classifies tape timing while RegimeEngine remains WHICH-side.
# Fail-open: unclassified / disabled → NEUTRAL (existing engine path unchanged).

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from core import now_ist


# Classification labels (first-match order in classify()).
TURN_STARTING = "TURN_STARTING"
TREND_EXHAUSTED = "TREND_EXHAUSTED"
BREAK_FROM_COIL = "BREAK_FROM_COIL"
COIL = "COIL"
TREND_STRENGTHENING = "TREND_STRENGTHENING"
TREND_ON = "TREND_ON"
NEUTRAL = "NEUTRAL"

_TREND_ALLOW_ENTRY = frozenset({TREND_ON, TREND_STRENGTHENING, BREAK_FROM_COIL, NEUTRAL})
_BULL_PX = frozenset({"UPTREND", "STRONG_UPTREND"})
_BEAR_PX = frozenset({"DOWNTREND", "STRONG_DOWNTREND"})


def regime_side(final_regime: Optional[str]) -> int:
    """+1 bull sell / -1 bear sell / 0 other."""
    fr = str(final_regime or "")
    if fr == "PREMIUM_SELL_BULL":
        return 1
    if fr == "PREMIUM_SELL_BEAR":
        return -1
    return 0


def price_side(price_regime: Optional[str]) -> int:
    px = str(price_regime or "")
    if px in _BULL_PX:
        return 1
    if px in _BEAR_PX:
        return -1
    return 0


def impulse_limit(signals: dict, config: Any) -> float:
    try:
        spot = float(signals.get("spot") or 0.0)
        vix = float(signals.get("vix") or 12.0)
        pct = float(getattr(config, "spot_velocity_pct", 0.0014) or 0.0014)
        if spot > 0:
            return max(spot * pct * (1.0 + max(0.0, (vix - 12.0)) / 24.0), 25.0)
    except (TypeError, ValueError):
        pass
    return 25.0


def _f(signals: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(signals.get(key) if signals.get(key) is not None else default)
    except (TypeError, ValueError):
        return default


class TapeStateEngine:
    """Deterministic WHEN classifier; writes additive fields onto signals."""

    def __init__(self, config: Any, logger: Any = None):
        self.config = config
        self.logger = logger

    def update(self, signals: dict, state: Optional[dict] = None) -> dict:
        """Classify tape state into signals. Never raises into the cycle."""
        try:
            return self._update_inner(signals, state if state is not None else {})
        except Exception as exc:
            if self.logger is not None:
                try:
                    self.logger.debug(f"TapeState update skipped: {exc}")
                except Exception:
                    pass
            return self._write_neutral(signals, reason=f"tape_error:{type(exc).__name__}")

    def _write_neutral(self, signals: dict, reason: str = "disabled_or_fail_open") -> dict:
        signals["tape_state"] = NEUTRAL
        signals["tape_state_reason"] = reason
        signals["tape_side"] = 0
        signals["tape_dwell_min"] = 0.0
        signals["tape_allow_entry"] = True
        signals["tape_force_flat"] = False
        return signals

    def _update_inner(self, signals: dict, state: dict) -> dict:
        if not bool(getattr(self.config, "tape_state_enabled", False)):
            return self._write_neutral(signals, reason="tape_state_disabled")

        now = now_ist()
        cfg = self.config
        dwell_need = float(getattr(cfg, "regime_entry_dwell_min", 5.0) or 5.0)
        adx_trend = float(getattr(cfg, "adx_trend_threshold", 20.0) or 20.0)
        exhausted_pct = float(getattr(cfg, "exhausted_move_pct", 100.0) or 100.0)
        exh_adx_d0 = float(getattr(cfg, "exhausted_move_adx_floor_dte0", 50.0) or 50.0)
        exh_pct_d1 = float(getattr(cfg, "exhausted_move_pct_dte1plus", 125.0) or 125.0)
        exh_adx_d1 = float(getattr(cfg, "exhausted_move_adx_floor_dte1plus", 45.0) or 45.0)

        final_regime = signals.get("final_regime")
        px = str(signals.get("price_regime") or "")
        side = regime_side(final_regime)
        px_side = price_side(px)
        adx = _f(signals, "adx_15", 0.0)
        day_move = _f(signals, "day_move_used_pct", 0.0)
        day_up = _f(signals, "day_up_used_pct", day_move)
        day_dn = _f(signals, "day_down_used_pct", day_move)
        # Exhaustion is directional: a morning dump does not exhaust a
        # fresh afternoon bull (Sep29), and vice versa.
        if side < 0:
            side_move = day_dn
        elif side > 0:
            side_move = day_up
        else:
            side_move = day_move
        ema = str(signals.get("ema_structure") or "")
        or_cond = str(signals.get("or_condition") or "")
        choppy = bool(signals.get("choppy_detected"))
        conf = str(signals.get("confidence_level") or "")
        try:
            dte = signals.get("actual_dte")
            if dte is None:
                dte = signals.get("expiry_dte")
            dte = int(dte if dte is not None else -1)
        except (TypeError, ValueError):
            dte = -1

        imp_up = _f(signals, "spot_impulse_up_pts", 0.0)
        imp_dn = _f(signals, "spot_impulse_down_pts", 0.0)
        lim = impulse_limit(signals, cfg)

        # ── Dwell: how long final_regime side (else price side) has been stable
        track_side = side if side != 0 else px_side
        prev_side = state.get("tape_track_side")
        since_iso = state.get("tape_track_since")
        if track_side == 0:
            dwell_min = 0.0
            state["tape_track_side"] = 0
            state["tape_track_since"] = None
        elif prev_side != track_side or not since_iso:
            state["tape_track_side"] = track_side
            state["tape_track_since"] = now.isoformat()
            dwell_min = 0.0
        else:
            try:
                since = datetime.fromisoformat(str(since_iso))
                dwell_min = max(0.0, (now - since).total_seconds() / 60.0)
            except Exception:
                state["tape_track_since"] = now.isoformat()
                dwell_min = 0.0

        # ── ΔADX from short hist in state
        hist = state.get("tape_adx_hist")
        if not isinstance(hist, list):
            hist = []
        hist = list(hist)[-8:] + [round(adx, 2)]
        state["tape_adx_hist"] = hist[-12:]
        adx_delta = 0.0
        if len(hist) >= 3:
            adx_delta = hist[-1] - hist[0]

        # Price-regime flip inside dwell window (for TURN)
        px_hist = state.get("tape_px_hist")
        if not isinstance(px_hist, list):
            px_hist = []
        px_hist = [p for p in px_hist if isinstance(p, (list, tuple)) and len(p) == 2]
        px_hist.append([now.isoformat(), px])
        # keep ~dwell_need minutes
        trimmed = []
        for ts_s, pxv in px_hist[-40:]:
            try:
                ts = datetime.fromisoformat(str(ts_s))
                if (now - ts).total_seconds() / 60.0 <= max(dwell_need, 8.0):
                    trimmed.append([ts_s, pxv])
            except Exception:
                continue
        state["tape_px_hist"] = trimmed
        px_flipped = False
        sides_seen = {price_side(p) for _, p in trimmed if price_side(p) != 0}
        if len(sides_seen) >= 2:
            px_flipped = True

        coil_latched = bool(state.get("tape_coil_latched"))

        # ── Classification (first match wins)
        label = NEUTRAL
        reason = "fail_open_default"
        force_flat = False
        allow_entry = True

        adverse = False
        with_impulse = False
        if side < 0:
            adverse = imp_up > lim
            with_impulse = imp_dn > lim * 0.75
        elif side > 0:
            adverse = imp_dn > lim
            with_impulse = imp_up > lim * 0.75
        else:
            # No regime side: treat large two-way impulse as adverse turn risk
            adverse = (imp_up > lim and px_side < 0) or (imp_dn > lim and px_side > 0)
            with_impulse = (imp_up > lim) or (imp_dn > lim)

        # 1) TURN_STARTING
        if adverse and (px_flipped or (side != 0 and (
                (side < 0 and (px_side > 0 or imp_up > lim))
                or (side > 0 and (px_side < 0 or imp_dn > lim))
        ))):
            label = TURN_STARTING
            reason = (
                f"adverse_impulse_up={imp_up:.0f}_dn={imp_dn:.0f}_lim={lim:.0f}"
                f"{'_px_flip' if px_flipped else '_vs_regime'}"
            )
            force_flat = True
            allow_entry = False

        # 2) TREND_EXHAUSTED — only this side's directional spend
        elif self._is_exhausted(
            dte, side_move, adx, conf,
            exhausted_pct, exh_adx_d0, exh_pct_d1, exh_adx_d1,
        ):
            label = TREND_EXHAUSTED
            reason = (
                f"side_move_{side_move:.0f}_up={day_up:.0f}_dn={day_dn:.0f}"
                f"_adx_{adx:.0f}_dte_{dte}"
            )
            allow_entry = False

        # 3) BREAK_FROM_COIL
        elif coil_latched and with_impulse and side != 0 and (
            (side > 0 and (px_side > 0 or ema == "BULLISH"))
            or (side < 0 and (px_side < 0 or ema == "BEARISH"))
        ):
            label = BREAK_FROM_COIL
            reason = f"coil_break_impulse_side_{side}"
            state["tape_coil_latched"] = False
            allow_entry = True

        # 4) COIL
        elif (
            or_cond in ("VERY_NARROW", "NARROW") or choppy
        ) and adx < adx_trend and abs(_f(signals, "vwap_dist_pct", 0.0)) < 0.12:
            label = COIL
            reason = f"or_{or_cond}_choppy_{int(choppy)}_adx_{adx:.0f}"
            state["tape_coil_latched"] = True
            # Range / non-directional still allowed; directional blocked downstream
            allow_entry = True

        # 5) TREND_STRENGTHENING
        elif (
            side != 0
            and dwell_min >= dwell_need
            and adx_delta > 0.5
            and adx >= adx_trend
            and side_move < (exhausted_pct if dte == 0 else exh_pct_d1)
            and (
                (side > 0 and ema in ("BULLISH", "TRANSITIONAL") and px_side >= 0)
                or (side < 0 and ema in ("BEARISH", "TRANSITIONAL") and px_side <= 0)
            )
        ):
            label = TREND_STRENGTHENING
            reason = f"adx_rising_{adx_delta:.1f}_dwell_{dwell_min:.1f}"
            allow_entry = True

        # 6) TREND_ON
        elif (
            side != 0
            and dwell_min >= dwell_need
            and not self._is_exhausted(
                dte, side_move, adx, conf,
                exhausted_pct, exh_adx_d0, exh_pct_d1, exh_adx_d1,
            )
            and (
                (side > 0 and px_side >= 0)
                or (side < 0 and px_side <= 0)
            )
        ):
            label = TREND_ON
            reason = f"with_regime_dwell_{dwell_min:.1f}_adx_{adx:.0f}"
            allow_entry = True

        else:
            label = NEUTRAL
            reason = "fail_open_default"
            allow_entry = True

        # Directional allow helper for consumers
        if label == COIL:
            # still allow RANGE / fades; mark allow_entry True but consumers
            # that need directional check tape_state == COIL
            allow_entry = True
        if label in (TURN_STARTING, TREND_EXHAUSTED):
            allow_entry = False
        if label in _TREND_ALLOW_ENTRY and label != NEUTRAL:
            allow_entry = True

        signals["tape_state"] = label
        signals["tape_state_reason"] = reason
        signals["tape_side"] = int(side if side != 0 else px_side)
        signals["tape_dwell_min"] = round(dwell_min, 2)
        signals["tape_allow_entry"] = bool(allow_entry)
        signals["tape_force_flat"] = bool(force_flat)
        return signals

    @staticmethod
    def _is_exhausted(
        dte: int,
        day_move: float,
        adx: float,
        conf: str,
        exhausted_pct: float,
        exh_adx_d0: float,
        exh_pct_d1: float,
        exh_adx_d1: float,
    ) -> bool:
        """Spent day-move without crash-grade ADX → exhausted chase risk."""
        if dte == 0:
            if day_move >= exhausted_pct and adx < exh_adx_d0:
                return True
            return False
        # DTE1+: higher day-move floor; HIGH conf escapes
        if day_move >= exh_pct_d1 and adx < exh_adx_d1 and conf != "HIGH":
            return True
        return False


def entry_when_block_reason(signals: dict, final_regime: Optional[str], config: Any) -> Optional[str]:
    """
    Additive entry WHEN gate. Returns a NO_TRADE reason string, or None to allow.
    Fail-open when flag off or tape_state missing/NEUTRAL.
    """
    if not bool(getattr(config, "tape_state_entry_gate", False)):
        return None
    if not bool(getattr(config, "tape_state_enabled", False)):
        return None

    # True two-way extreme fades: never kill auction harvest
    if bool(signals.get("two_way_auction")) and (
        signals.get("afternoon_high_fade") or signals.get("afternoon_low_fade")
    ):
        return None

    state = str(signals.get("tape_state") or NEUTRAL)
    if state == NEUTRAL:
        return None

    detail = str(signals.get("tape_state_reason") or "")
    fr = str(final_regime or signals.get("final_regime") or "")
    side = regime_side(fr)
    directional = fr in ("PREMIUM_SELL_BEAR", "PREMIUM_SELL_BULL")

    # TURN_STARTING / force_flat: block OLD side only (regime side being
    # attacked). Opposite credit after the flip remains allowed.
    if bool(signals.get("tape_force_flat")) or state == TURN_STARTING:
        if not directional:
            return None
        stale = int(signals.get("tape_side") or side or 0)
        if stale < 0 and fr == "PREMIUM_SELL_BEAR":
            return f"tape_when_{state}_{detail or 'old_side_bear'}"
        if stale > 0 and fr == "PREMIUM_SELL_BULL":
            return f"tape_when_{state}_{detail or 'old_side_bull'}"
        return None

    # EXHAUSTED: block only when THIS side's directional spend is exhausted.
    # Total day_move from a morning dump must not ban a fresh bull (Sep29).
    if state == TREND_EXHAUSTED:
        try:
            exh = float(getattr(config, "exhausted_move_pct", 100.0) or 100.0)
        except (TypeError, ValueError):
            exh = 100.0
        day_move = _f(signals, "day_move_used_pct", 0.0)
        day_up = _f(signals, "day_up_used_pct", day_move)
        day_dn = _f(signals, "day_down_used_pct", day_move)
        px_s = price_side(signals.get("price_regime"))
        if fr == "PREMIUM_SELL_BEAR" and px_s <= 0 and day_dn >= exh:
            return f"tape_when_{state}_{detail or 'exhausted_bear_chase'}"
        if fr == "PREMIUM_SELL_BULL" and px_s >= 0 and day_up >= exh:
            return f"tape_when_{state}_{detail or 'exhausted_bull_chase'}"
        return None

    if state == COIL and directional:
        return f"tape_when_{state}_{detail or 'coil_no_directional'}"

    return None


def size_nudge_multiplier(signals: dict, config: Any) -> float:
    """STRENGTHENING-only size nudge; 1.0 otherwise. Capped at 1.15."""
    if not bool(getattr(config, "tape_state_size_nudge", False)):
        return 1.0
    if str(signals.get("tape_state") or "") != TREND_STRENGTHENING:
        return 1.0
    try:
        boost = float(getattr(config, "tape_state_size_boost", 1.10) or 1.10)
    except (TypeError, ValueError):
        boost = 1.10
    return max(1.0, min(boost, 1.15))


# ── Module self-tests ───────────────────────────────────────────────────────
def _self_test() -> None:
    class _Cfg:
        tape_state_enabled = True
        tape_state_entry_gate = True
        tape_state_size_nudge = True
        tape_state_size_boost = 1.10
        regime_entry_dwell_min = 5.0
        adx_trend_threshold = 20.0
        exhausted_move_pct = 100.0
        exhausted_move_adx_floor_dte0 = 50.0
        exhausted_move_pct_dte1plus = 125.0
        exhausted_move_adx_floor_dte1plus = 45.0
        spot_velocity_pct = 0.0014

    eng = TapeStateEngine(_Cfg())
    st: dict = {}

    # Sep29-like bounce into BEAR → TURN_STARTING
    s1 = {
        "spot": 22800.0, "vix": 13.0,
        "final_regime": "PREMIUM_SELL_BEAR",
        "price_regime": "DOWNTREND",
        "spot_impulse_up_pts": 40.0, "spot_impulse_down_pts": 0.0,
        "adx_15": 22.0, "day_move_used_pct": 110.0,
        "ema_structure": "BEARISH", "or_condition": "MODERATE",
        "choppy_detected": False, "vwap_dist_pct": 0.05,
        "actual_dte": 0, "confidence_level": "MEDIUM",
    }
    # Seed dwell so we are not COIL
    st["tape_track_side"] = -1
    st["tape_track_since"] = (now_ist()).isoformat()
    out1 = eng.update(dict(s1), st)
    assert out1["tape_state"] == TURN_STARTING, out1
    assert out1["tape_force_flat"] is True
    br = entry_when_block_reason(out1, "PREMIUM_SELL_BEAR", _Cfg())
    assert br and "TURN_STARTING" in br, br

    # Sep15 crash ADX87 + spent day-move → not EXHAUSTED (crash-grade ADX)
    st2: dict = {
        "tape_track_side": -1,
        "tape_track_since": (now_ist()).replace(year=2020).isoformat(),
        "tape_adx_hist": [70.0, 75.0, 80.0],
    }
    s2 = {
        "spot": 25000.0, "vix": 18.0,
        "final_regime": "PREMIUM_SELL_BEAR",
        "price_regime": "STRONG_DOWNTREND",
        "spot_impulse_up_pts": 0.0, "spot_impulse_down_pts": 10.0,
        "adx_15": 87.0, "day_move_used_pct": 200.0,
        "ema_structure": "BEARISH", "or_condition": "WIDE",
        "choppy_detected": False, "vwap_dist_pct": 0.20,
        "actual_dte": 0, "confidence_level": "HIGH",
    }
    out2 = eng.update(dict(s2), st2)
    assert out2["tape_state"] in (TREND_ON, TREND_STRENGTHENING), out2
    assert entry_when_block_reason(out2, "PREMIUM_SELL_BEAR", _Cfg()) is None

    # Coil day → COIL blocks directional
    st3: dict = {}
    s3 = {
        "spot": 25000.0, "vix": 12.0,
        "final_regime": "PREMIUM_SELL_BEAR",
        "price_regime": "RANGE",
        "spot_impulse_up_pts": 0.0, "spot_impulse_down_pts": 0.0,
        "adx_15": 12.0, "day_move_used_pct": 20.0,
        "ema_structure": "NEUTRAL", "or_condition": "VERY_NARROW",
        "choppy_detected": True, "vwap_dist_pct": 0.02,
        "actual_dte": 2, "confidence_level": "MEDIUM",
    }
    out3 = eng.update(dict(s3), st3)
    assert out3["tape_state"] == COIL, out3
    br3 = entry_when_block_reason(out3, "PREMIUM_SELL_BEAR", _Cfg())
    assert br3 and "COIL" in br3, br3
    # RANGE still allowed through entry_when (non-directional)
    assert entry_when_block_reason(out3, "PREMIUM_SELL_RANGE", _Cfg()) is None

    # Sep29 reverse-bull: total day_move spent by morning dump, but day_up
    # fresh — BULL must NOT classify as TREND_EXHAUSTED.
    st4: dict = {
        "tape_track_side": 1,
        "tape_track_since": (now_ist()).replace(year=2020).isoformat(),
        "tape_adx_hist": [35.0, 38.0, 42.0],
    }
    s4 = {
        "spot": 22720.0, "vix": 13.0,
        "final_regime": "PREMIUM_SELL_BULL",
        "price_regime": "UPTREND",
        "spot_impulse_up_pts": 5.0, "spot_impulse_down_pts": 0.0,
        "adx_15": 43.0, "day_move_used_pct": 130.0,
        "day_up_used_pct": 55.0, "day_down_used_pct": 120.0,
        "ema_structure": "BULLISH", "or_condition": "WIDE",
        "choppy_detected": False, "vwap_dist_pct": 0.08,
        "actual_dte": 0, "confidence_level": "HIGH",
    }
    out4 = eng.update(dict(s4), st4)
    assert out4["tape_state"] in (TREND_ON, TREND_STRENGTHENING), out4
    assert entry_when_block_reason(out4, "PREMIUM_SELL_BULL", _Cfg()) is None

    assert size_nudge_multiplier({"tape_state": TREND_STRENGTHENING}, _Cfg()) == 1.10
    assert size_nudge_multiplier({"tape_state": TREND_ON}, _Cfg()) == 1.0
    print("tape_state_engine self-test OK")


if __name__ == "__main__":
    _self_test()
