"""
Market Signal Engine — 15m EMA trend + 5m breakout + ATR regime.

Does not place orders. Signal id is the completed 5m candle timestamp.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

from feeds import atr, validate_candle_frame

UTC = timezone.utc
log = logging.getLogger("gold_news.market_signals")


@dataclass
class MarketSignal:
    direction: str = "neutral"       # bullish | bearish | neutral
    trend: str = "unknown"           # bullish | bearish | flat | unknown
    breakout: str = "none"           # up | down | none
    regime: str = "unknown"          # trending | range | abnormal | unreliable
    atr: Optional[float] = None
    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    breakout_level: Optional[float] = None
    signal_id: str = ""
    candle_utc: str = ""
    data_valid: bool = False
    valid: bool = False
    reasons: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ema_series(close: pd.Series, span: int) -> pd.Series:
    return close.astype(float).ewm(span=int(span), adjust=False).mean()


def classify_trend_15m(
    candles_15m: pd.DataFrame,
    *,
    ema_fast: int = 20,
    ema_slow: int = 50,
) -> tuple[str, Optional[float], Optional[float], str]:
    """Return (trend, ema_fast_val, ema_slow_val, reason)."""
    if candles_15m is None or candles_15m.empty:
        return "unknown", None, None, "no_15m"
    if not validate_candle_frame(candles_15m):
        return "unknown", None, None, "invalid_15m"
    need = int(ema_slow) + 1
    if len(candles_15m) < need:
        return "unknown", None, None, f"warmup_15m need>={need}"
    close = candles_15m["close"].astype(float)
    ef = ema_series(close, ema_fast)
    es = ema_series(close, ema_slow)
    fv, sv = float(ef.iloc[-1]), float(es.iloc[-1])
    if fv > sv:
        return "bullish", fv, sv, "ema_fast_above_slow"
    if fv < sv:
        return "bearish", fv, sv, "ema_fast_below_slow"
    return "flat", fv, sv, "ema_equal"


def classify_breakout_5m(
    candles_5m: pd.DataFrame,
    *,
    lookback: int = 20,
) -> tuple[str, Optional[float], str]:
    """
    Breakout on the last *completed* 5m bar vs prior ``lookback`` bars.

    The comparison window excludes the evaluation candle (no self-inclusion).
    """
    if candles_5m is None or candles_5m.empty:
        return "none", None, "no_5m"
    if not validate_candle_frame(candles_5m):
        return "none", None, "invalid_5m"
    lb = int(lookback)
    if len(candles_5m) < lb + 1:
        return "none", None, f"warmup_5m need>={lb + 1}"
    # Prior window: bars [-(lb+1) : -1]  → excludes last row
    prior = candles_5m.iloc[-(lb + 1):-1]
    last = candles_5m.iloc[-1]
    hh = float(prior["high"].max())
    ll = float(prior["low"].min())
    close = float(last["close"])
    if close > hh:
        return "up", hh, "close_above_prior_high"
    if close < ll:
        return "down", ll, "close_below_prior_low"
    return "none", None, "no_breakout"


def classify_regime(
    candles_5m: pd.DataFrame,
    atr_value: Optional[float],
    trend: str,
    *,
    atr_period: int = 14,
) -> str:
    if candles_5m is None or not validate_candle_frame(candles_5m):
        return "unreliable"
    if atr_value is None or atr_value <= 0:
        return "unreliable"
    # Abnormal: ATR large vs recent median true range proxy (close range)
    recent = candles_5m.iloc[-max(atr_period, 10):]
    ranges = (recent["high"] - recent["low"]).astype(float)
    med = float(ranges.median()) if len(ranges) else 0.0
    if med > 0 and atr_value > med * 3.0:
        return "abnormal"
    if trend in ("bullish", "bearish"):
        return "trending"
    return "range"


def evaluate_market_signal(
    candles_5m: pd.DataFrame,
    candles_15m: pd.DataFrame,
    *,
    lookback: int = 20,
    atr_period: int = 14,
    ema_fast: int = 20,
    ema_slow: int = 50,
    data_valid: bool = True,
) -> MarketSignal:
    sig = MarketSignal(data_valid=bool(data_valid))
    if not data_valid:
        sig.reasons.append("data_invalid")
        sig.regime = "unreliable"
        return sig

    trend, ef, es, t_reason = classify_trend_15m(
        candles_15m, ema_fast=ema_fast, ema_slow=ema_slow,
    )
    brk, level, b_reason = classify_breakout_5m(candles_5m, lookback=lookback)
    atr_v = atr(candles_5m, atr_period)
    regime = classify_regime(candles_5m, atr_v, trend, atr_period=atr_period)

    sig.trend = trend
    sig.breakout = brk
    sig.ema_fast = ef
    sig.ema_slow = es
    sig.breakout_level = level
    sig.atr = atr_v
    sig.regime = regime
    sig.reasons.extend([t_reason, b_reason, f"regime_{regime}"])

    if candles_5m is not None and not candles_5m.empty:
        ts = candles_5m.index[-1]
        sig.candle_utc = str(ts)
        sig.signal_id = f"{brk}:{pd.Timestamp(ts).isoformat()}"

    # Direction requires trend + matching breakout in a usable regime
    if regime in ("unreliable", "abnormal"):
        sig.direction = "neutral"
        sig.reasons.append("regime_blocks_signal")
        return sig
    if trend == "bullish" and brk == "up":
        sig.direction = "bullish"
        sig.valid = True
        sig.reasons.append("long_setup")
    elif trend == "bearish" and brk == "down":
        sig.direction = "bearish"
        sig.valid = True
        sig.reasons.append("short_setup")
    else:
        sig.direction = "neutral"
        sig.reasons.append("trend_breakout_disagree_or_flat")
    return sig
