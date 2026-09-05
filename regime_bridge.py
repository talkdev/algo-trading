from __future__ import annotations

from datetime import datetime
from typing import Optional, Dict, Any

from nifty_algo_core import (
    now_ist, today_ist,
    load_config, setup_logging, Database, RateLimiter, UpstoxClient,
)


FINAL_REGIME_TO_STRATEGY_NAME: Dict[str, Optional[str]] = {
    "PREMIUM_SELL_RANGE":   None,
    "PREMIUM_SELL_BULL":    "BULL_PUT_SPREAD",
    "PREMIUM_SELL_BEAR":    "BEAR_CALL_SPREAD",
    "BUY_STRADDLE":         "LONG_STRADDLE",
    "BUY_DIRECTIONAL_BULL": "BULL_CALL_SPREAD",
    "BUY_DIRECTIONAL_BEAR": "BEAR_PUT_SPREAD",
    "EXPIRY_MAX_PAIN":      None,
    "NO_TRADE":             None,
    "EMERGENCY_EXIT":       None,
}

VOLATILITY_REGIME_TO_CONDITION: Dict[str, str] = {
    "STRONG_SELL_PREMIUM": "VERY_RICH",
    "SELL_PREMIUM":        "RICH",
    "NEUTRAL":             "FAIR",
    "BUY_OPTIONS":         "CHEAP",
    "HIGH_VOL_CAUTION":    "ELEVATED",
    "ABORT":               "ABORT",
}

PRICE_REGIME_TO_TREND: Dict[str, str] = {
    "STRONG_UPTREND":   "STRONG_TREND",
    "UPTREND":          "TRENDING",
    "RANGE":            "RANGE_BOUND",
    "DOWNTREND":        "TRENDING",
    "STRONG_DOWNTREND": "STRONG_TREND",
    "CHOPPY":           "CHOPPY",
    "OBSERVING":        "OR_PENDING",
}

PRICE_REGIME_TO_DIRECTION: Dict[str, str] = {
    "STRONG_UPTREND":   "BULLISH",
    "UPTREND":          "MILD_BULLISH",
    "RANGE":            "NEUTRAL",
    "DOWNTREND":        "MILD_BEARISH",
    "STRONG_DOWNTREND": "BEARISH",
    "CHOPPY":           "NEUTRAL",
    "OBSERVING":        "NEUTRAL",
}

POSITIONING_REGIME_TO_SIGNALS: Dict[str, Dict[str, Any]] = {
    "STRONG_RANGE": {"pcr_signal": "STABLE",    "skew_signal": "BALANCED", "preferred_sell_side": "BOTH"},
    "RANGE":        {"pcr_signal": "STABLE",    "skew_signal": "NORMAL",   "preferred_sell_side": "BOTH"},
    "BULLISH":      {"pcr_signal": "GREED_RISING", "skew_signal": "COMPLACENT", "preferred_sell_side": "PUTS"},
    "BEARISH":      {"pcr_signal": "FEAR_RISING",  "skew_signal": "FEAR",       "preferred_sell_side": "CALLS"},
    "UNCLEAR":      {"pcr_signal": "UNKNOWN",   "skew_signal": "UNKNOWN",  "preferred_sell_side": "BOTH"},
}

CONFIDENCE_TO_SIZE_FACTOR: Dict[str, float] = {
    "HIGH":   1.00,
    "MEDIUM": 0.50,
    "LOW":    0.25,
    "NONE":   0.00,
}


def final_regime_to_strategy_name(
    final_regime: str,
    signals: dict,
) -> str:
    if final_regime in ("NO_TRADE", "EMERGENCY_EXIT"):
        return "NO_TRADE"

    if final_regime == "PREMIUM_SELL_RANGE":
        or_condition = signals.get("or_condition", "MODERATE")
        adx_15 = signals.get("adx_15") or 0.0
        if or_condition in ("VERY_NARROW", "NARROW") and adx_15 < 20:
            return "IRON_BUTTERFLY"
        return "IRON_CONDOR"

    if final_regime == "EXPIRY_MAX_PAIN":
        direction = signals.get("direction", "NEUTRAL")
        preferred = signals.get("preferred_sell_side", "BOTH")
        if direction in ("BULLISH", "MILD_BULLISH") or preferred == "PUTS":
            return "BULL_PUT_SPREAD"
        if direction in ("BEARISH", "MILD_BEARISH") or preferred == "CALLS":
            return "BEAR_CALL_SPREAD"
        return "BULL_PUT_SPREAD"

    mapped = FINAL_REGIME_TO_STRATEGY_NAME.get(final_regime)
    if mapped is not None:
        return mapped

    return "NO_TRADE"


def merge_regime_into_signals(
    signals: dict,
    market_engine_or_regime_snapshot=None,
) -> dict:
    if market_engine_or_regime_snapshot is None:
        return signals

    try:
        from regime_engine import RegimeEngine, RegimeSnapshot
        if isinstance(market_engine_or_regime_snapshot, RegimeSnapshot):
            return _merge_from_snapshot(signals, market_engine_or_regime_snapshot)
        if hasattr(market_engine_or_regime_snapshot, "get_current_regime"):
            regime_engine = market_engine_or_regime_snapshot
            snap = regime_engine.get_current_regime()
            if snap is not None:
                return _merge_from_snapshot(signals, snap)
    except ImportError:
        pass
    except Exception:
        pass

    if hasattr(market_engine_or_regime_snapshot, "state"):
        market_engine = market_engine_or_regime_snapshot
        try:
            from regime_engine import RegimeEngine
            if hasattr(market_engine, "_regime_engine"):
                snap = market_engine._regime_engine.get_current_regime()
                if snap is not None:
                    return _merge_from_snapshot(signals, snap)
        except (ImportError, AttributeError):
            pass

    return signals


def _merge_from_snapshot(signals: dict, snap) -> dict:
    enriched = dict(signals)

    enriched["final_regime"]         = snap.final_regime
    enriched["confidence"]           = snap.confidence
    enriched["size_multiplier"]      = snap.size_multiplier
    enriched["raw_size_multiplier"]  = snap.raw_size_multiplier
    enriched["defined_risk_only"]    = snap.defined_risk_only
    enriched["event_day"]            = snap.event_day
    enriched["event_name"]           = snap.event_name
    enriched["is_calibrated"]        = snap.is_calibrated
    enriched["calibration_tier"]     = snap.calibration_tier
    enriched["notes"]                = snap.notes
    enriched["volatility_regime"]    = snap.volatility_regime
    enriched["price_regime_15"]      = snap.price_regime_15
    enriched["price_regime_60"]      = snap.price_regime_60
    enriched["mtf_aligned"]          = snap.mtf_aligned
    enriched["positioning_regime"]   = snap.positioning_regime
    enriched["regime_adx_15"]        = snap.adx_15
    enriched["regime_adx_60"]        = snap.adx_60
    enriched["regime_ema_structure"] = snap.ema_structure
    enriched["regime_vix_roc"]       = snap.vix_roc
    enriched["regime_ivr"]           = snap.ivr
    enriched["regime_iv_hv_ratio"]   = snap.iv_hv_ratio
    enriched["regime_straddle_ratio"]= snap.straddle_ratio
    enriched["regime_oi_wall_strength"] = snap.oi_wall_strength

    vol_cond = VOLATILITY_REGIME_TO_CONDITION.get(snap.volatility_regime, "UNKNOWN")
    if not enriched.get("volatility_condition") or enriched.get("volatility_condition") == "UNKNOWN":
        enriched["volatility_condition"] = vol_cond

    trend_cond = PRICE_REGIME_TO_TREND.get(snap.price_regime_15, "UNCERTAIN")
    if not enriched.get("trend_condition") or enriched.get("trend_condition") in ("OR_PENDING", "UNKNOWN"):
        enriched["trend_condition"] = trend_cond

    direction_from_regime = PRICE_REGIME_TO_DIRECTION.get(snap.price_regime_15, "NEUTRAL")
    if not enriched.get("direction") or enriched.get("direction") == "NEUTRAL":
        if snap.positioning_regime == "BULLISH":
            enriched["direction"] = "BULLISH"
        elif snap.positioning_regime == "BEARISH":
            enriched["direction"] = "BEARISH"
        else:
            enriched["direction"] = direction_from_regime

    pos_signals = POSITIONING_REGIME_TO_SIGNALS.get(
        snap.positioning_regime,
        {"pcr_signal": "UNKNOWN", "skew_signal": "UNKNOWN", "preferred_sell_side": "BOTH"}
    )
    if not enriched.get("pcr_signal") or enriched.get("pcr_signal") == "UNKNOWN":
        enriched["pcr_signal"] = pos_signals["pcr_signal"]
    if not enriched.get("skew_signal") or enriched.get("skew_signal") == "UNKNOWN":
        enriched["skew_signal"] = pos_signals["skew_signal"]
    if not enriched.get("preferred_sell_side") or enriched.get("preferred_sell_side") == "BOTH":
        enriched["preferred_sell_side"] = pos_signals["preferred_sell_side"]

    if snap.volatility_regime in ("STRONG_SELL_PREMIUM", "SELL_PREMIUM"):
        enriched["sell_ok"] = True
        enriched["buy_ok"]  = False
    elif snap.volatility_regime == "BUY_OPTIONS":
        enriched["sell_ok"] = False
        enriched["buy_ok"]  = True
    elif snap.volatility_regime == "ABORT":
        enriched["sell_ok"] = False
        enriched["buy_ok"]  = False

    if snap.adx_15 > 0 and not enriched.get("adx_15"):
        enriched["adx_15"] = snap.adx_15
    if snap.adx_60 > 0 and not enriched.get("adx_60"):
        enriched["adx_60"] = snap.adx_60
    if snap.ema_structure != "INSUFFICIENT_DATA" and not enriched.get("ema_structure"):
        enriched["ema_structure"] = snap.ema_structure

    if snap.adx_15 > 0:
        from nifty_algo_core import load_config as _lc
        try:
            _cfg = _lc()
            adx_strong = _cfg.adx_strong_threshold
            adx_trend  = _cfg.adx_trend_threshold
        except Exception:
            adx_strong = 35.0
            adx_trend  = 25.0
        if snap.adx_15 >= adx_strong:
            enriched["adx_condition"] = "STRONG"
        elif snap.adx_15 >= adx_trend:
            enriched["adx_condition"] = "MODERATE"
        elif snap.adx_15 >= 20:
            enriched["adx_condition"] = "WEAK"
        else:
            enriched["adx_condition"] = "FLAT"

    if snap.final_regime in ("NO_TRADE", "EMERGENCY_EXIT"):
        enriched["iv_behavior"] = enriched.get("iv_behavior", "UNKNOWN")
    else:
        if snap.volatility_regime == "ABORT" and not enriched.get("iv_behavior"):
            enriched["iv_behavior"] = "SPIKING"

    enriched["high_quality_sell_day"] = (
        snap.volatility_regime in ("STRONG_SELL_PREMIUM", "SELL_PREMIUM") and
        snap.positioning_regime in ("STRONG_RANGE", "RANGE") and
        snap.confidence in ("HIGH", "MEDIUM") and
        not snap.event_day
    )

    enriched["sell_size_reduction"] = 1.0
    if snap.volatility_regime == "SELL_PREMIUM":
        enriched["sell_size_reduction"] = 0.75
    elif snap.volatility_regime == "HIGH_VOL_CAUTION":
        enriched["sell_size_reduction"] = 0.50

    return enriched


def get_strategy_name_from_signals(signals: dict) -> str:
    final_regime = signals.get("final_regime")
    if not final_regime:
        return "NO_TRADE"
    return final_regime_to_strategy_name(final_regime, signals)


def get_size_multiplier_from_signals(signals: dict) -> float:
    size = signals.get("size_multiplier")
    if size is not None:
        return float(size)
    raw = signals.get("raw_size_multiplier")
    if raw is not None:
        return float(raw)
    confidence = signals.get("confidence", "NONE")
    return CONFIDENCE_TO_SIZE_FACTOR.get(confidence, 0.0)


def is_defined_risk_only(signals: dict) -> bool:
    return bool(signals.get("defined_risk_only", False))


def is_regime_tradeable(signals: dict) -> bool:
    final_regime = signals.get("final_regime")
    if not final_regime:
        return False
    if final_regime in ("NO_TRADE", "EMERGENCY_EXIT"):
        return False
    if signals.get("confidence") == "NONE":
        return False
    if get_size_multiplier_from_signals(signals) <= 0:
        return False
    return True


def get_regime_summary(signals: dict) -> dict:
    return {
        "final_regime":       signals.get("final_regime"),
        "confidence":         signals.get("confidence"),
        "size_multiplier":    get_size_multiplier_from_signals(signals),
        "strategy_name":      get_strategy_name_from_signals(signals),
        "volatility_regime":  signals.get("volatility_regime"),
        "price_regime_15":    signals.get("price_regime_15"),
        "price_regime_60":    signals.get("price_regime_60"),
        "mtf_aligned":        signals.get("mtf_aligned"),
        "positioning_regime": signals.get("positioning_regime"),
        "event_day":          signals.get("event_day"),
        "event_name":         signals.get("event_name"),
        "defined_risk_only":  is_defined_risk_only(signals),
        "is_tradeable":       is_regime_tradeable(signals),
        "is_calibrated":      signals.get("is_calibrated"),
        "calibration_tier":   signals.get("calibration_tier"),
        "notes":              signals.get("notes"),
    }


def validate_regime_signals(signals: dict) -> tuple:
    errors = []
    warnings = []

    final_regime = signals.get("final_regime")
    if final_regime is None:
        warnings.append("final_regime not set — regime engine output not merged")

    if signals.get("volatility_condition") == "UNKNOWN":
        warnings.append("volatility_condition is UNKNOWN — VRP data may be unavailable")

    if signals.get("trend_condition") in ("OR_PENDING", "OBSERVING"):
        warnings.append(f"trend_condition={signals.get('trend_condition')} — opening range not yet established")

    if signals.get("size_multiplier", 0) <= 0 and final_regime not in ("NO_TRADE", "EMERGENCY_EXIT", None):
        errors.append(f"size_multiplier={signals.get('size_multiplier')} is zero or negative for tradeable regime")

    if signals.get("event_day") and signals.get("size_multiplier", 1.0) > 0.25:
        errors.append(
            f"event_day=True but size_multiplier={signals.get('size_multiplier')} > 0.25 — "
            f"non-negotiable 75% reduction not applied"
        )

    if final_regime == "EMERGENCY_EXIT" and signals.get("size_multiplier", 0) > 0:
        errors.append("EMERGENCY_EXIT regime must have size_multiplier=0")

    if signals.get("defined_risk_only") and final_regime in ("PREMIUM_SELL_BULL", "PREMIUM_SELL_BEAR"):
        errors.append(
            f"defined_risk_only=True but final_regime={final_regime} — "
            f"naked premium blocked on event days"
        )

    return errors, warnings


def build_regime_context_for_logging(signals: dict) -> str:
    parts = []
    final_regime = signals.get("final_regime", "UNKNOWN")
    confidence   = signals.get("confidence", "NONE")
    size         = get_size_multiplier_from_signals(signals)
    parts.append(f"regime={final_regime}")
    parts.append(f"conf={confidence}")
    parts.append(f"size={size:.2f}")
    if signals.get("event_day"):
        parts.append(f"EVENT:{signals.get('event_name', '')}")
    if signals.get("defined_risk_only"):
        parts.append("DEFINED_RISK_ONLY")
    vol = signals.get("volatility_regime", "")
    if vol:
        parts.append(f"vol={vol}")
    price15 = signals.get("price_regime_15", "")
    if price15:
        parts.append(f"price15={price15}")
    pos = signals.get("positioning_regime", "")
    if pos:
        parts.append(f"pos={pos}")
    adx15 = signals.get("adx_15") or signals.get("regime_adx_15")
    if adx15:
        parts.append(f"adx15={adx15:.1f}")
    tier = signals.get("calibration_tier")
    if tier is not None:
        parts.append(f"tier={tier}")
    return " | ".join(parts)


def _self_test() -> None:
    from nifty_algo_core import print_section, print_kv_table

    print_section("REGIME BRIDGE SELF-TEST")

    mock_signals = {
        "trading_date": today_ist().isoformat(),
        "spot": 24500.0,
        "vix": 14.5,
        "vrp": 3.2,
        "atm_iv": 0.145,
        "atm_straddle_price": 185.0,
        "pcr": 0.95,
        "skew": 2.1,
        "oi_change_pct": 0.05,
        "adx_15": 18.0,
        "adx_60": 15.0,
        "ema_structure": "NEUTRAL",
        "price_regime_15": "RANGE",
        "price_regime_60": "RANGE",
        "mtf_aligned": True,
        "or_condition": "NARROW",
        "or_width": 85.0,
        "volatility_condition": "RICH",
        "trend_condition": "RANGE_BOUND",
        "direction": "NEUTRAL",
        "sell_ok": True,
        "buy_ok": False,
    }

    class MockRegimeSnapshot:
        final_regime         = "PREMIUM_SELL_RANGE"
        confidence           = "HIGH"
        size_multiplier      = 0.75
        raw_size_multiplier  = 0.75
        defined_risk_only    = False
        event_day            = False
        event_name           = ""
        is_calibrated        = True
        calibration_tier     = 2
        notes                = "votes=['SELL', 'RANGE', 'RANGE']"
        volatility_regime    = "SELL_PREMIUM"
        price_regime         = "RANGE"
        price_regime_15      = "RANGE"
        price_regime_60      = "RANGE"
        mtf_aligned          = True
        positioning_regime   = "RANGE"
        adx_15               = 18.0
        adx_60               = 15.0
        ema_structure        = "NEUTRAL"
        vix_roc              = -0.5
        ivr                  = 55.0
        iv_hv_ratio          = 1.08
        straddle_ratio       = 1.12
        oi_wall_strength     = 2.2
        oi_change_pct        = 0.05
        skew                 = 2.1
        max_pain_distance    = 50.0
        pcr                  = 0.95
        timestamp            = datetime.now()
        day_type             = "MID_WEEK"
        dte                  = 3

    snap = MockRegimeSnapshot()
    enriched = _merge_from_snapshot(mock_signals, snap)

    print_kv_table({
        "final_regime":         enriched.get("final_regime"),
        "confidence":           enriched.get("confidence"),
        "size_multiplier":      enriched.get("size_multiplier"),
        "volatility_condition": enriched.get("volatility_condition"),
        "trend_condition":      enriched.get("trend_condition"),
        "direction":            enriched.get("direction"),
        "preferred_sell_side":  enriched.get("preferred_sell_side"),
        "sell_ok":              enriched.get("sell_ok"),
        "adx_condition":        enriched.get("adx_condition"),
        "high_quality_sell_day":enriched.get("high_quality_sell_day"),
        "sell_size_reduction":  enriched.get("sell_size_reduction"),
    }, title="Merged Signals")

    strategy = get_strategy_name_from_signals(enriched)
    print(f"\n  Strategy from final_regime: {strategy}")

    errors, warnings = validate_regime_signals(enriched)
    print(f"\n  Validation errors: {errors}")
    print(f"  Validation warnings: {warnings}")

    context = build_regime_context_for_logging(enriched)
    print(f"\n  Log context: {context}")

    summary = get_regime_summary(enriched)
    print_kv_table(summary, title="Regime Summary")

    print_section("Testing event day enforcement")
    event_snap = MockRegimeSnapshot()
    event_snap.final_regime      = "PREMIUM_SELL_BULL"
    event_snap.event_day         = True
    event_snap.event_name        = "RBI MPC Decision"
    event_snap.defined_risk_only = True
    event_snap.size_multiplier   = 0.1875
    event_snap.raw_size_multiplier = 0.75

    event_signals = dict(mock_signals)
    event_enriched = _merge_from_snapshot(event_signals, event_snap)
    errors2, warnings2 = validate_regime_signals(event_enriched)
    print(f"  Event day errors (expect naked premium error): {errors2}")
    print(f"  Event day warnings: {warnings2}")

    print_section("Testing NO_TRADE regime")
    no_trade_snap = MockRegimeSnapshot()
    no_trade_snap.final_regime    = "NO_TRADE"
    no_trade_snap.confidence      = "NONE"
    no_trade_snap.size_multiplier = 0.0
    no_trade_snap.raw_size_multiplier = 0.0

    nt_signals = dict(mock_signals)
    nt_enriched = _merge_from_snapshot(nt_signals, no_trade_snap)
    print(f"  is_tradeable: {is_regime_tradeable(nt_enriched)}")
    print(f"  strategy_name: {get_strategy_name_from_signals(nt_enriched)}")

    print_section("SELF-TEST COMPLETE")


if __name__ == "__main__":
    _self_test()