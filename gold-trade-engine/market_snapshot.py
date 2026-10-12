"""
Market snapshot adapter — structured MCX trading data + contextual series.

Trading prices must come from MCX Upstox futures. COMEX GC=F / Yahoo proxies
are context only and never executable.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from config import ENGINE_DIR, EngineConfig, MCX_VALUE_PER_PRICE_UNIT
from feeds import (
    candles_completed_as_of,
    resample_ohlcv,
    validate_candle_frame,
)

UTC = timezone.utc
log = logging.getLogger("gold_news.market_snapshot")

SNAPSHOT_PATH = ENGINE_DIR / "market_snapshot.json"

# Contextual names that must never be treated as the tradeable MCX instrument
NON_EXECUTABLE_CONTEXT = frozenset({
    "XAUUSD", "GC=F", "COMEX", "DXY", "US10Y_REAL", "FED_WATCH", "USDINR",
})


@dataclass
class ContractInfo:
    symbol: str
    instrument_key: str
    trading_symbol: str = ""
    expiry_utc: str = ""
    lot_size: int = 1
    tick_size: float = 1.0
    value_per_price_unit: float = 10.0


@dataclass
class QuoteView:
    bid: Optional[float] = None
    ask: Optional[float] = None
    ltp: Optional[float] = None
    spread: Optional[float] = None
    source: str = ""
    age_seconds: Optional[float] = None
    valid: bool = False
    reject_reason: str = ""


@dataclass
class CandleBundle:
    timeframe_minutes: int
    bars: int = 0
    last_bar_utc: str = ""
    age_minutes: Optional[float] = None
    valid: bool = False
    reject_reason: str = ""


@dataclass
class ContextObservation:
    name: str
    value: Optional[float] = None
    source: str = ""
    age_seconds: Optional[float] = None
    valid: bool = False
    executable: bool = False  # always False for context
    note: str = ""


@dataclass
class MarketSnapshot:
    """Published snapshot for decision / signal engines."""
    as_of_utc: str = ""
    contract: Optional[ContractInfo] = None
    quote: QuoteView = field(default_factory=QuoteView)
    candles_5m: CandleBundle = field(default_factory=lambda: CandleBundle(5))
    candles_15m: CandleBundle = field(default_factory=lambda: CandleBundle(15))
    context: list[ContextObservation] = field(default_factory=list)
    mcx_data_valid: bool = False
    collector_healthy: bool = False
    reasons: list[str] = field(default_factory=list)
    # In-memory frames (not serialized to JSON)
    frame_5m: Optional[pd.DataFrame] = field(default=None, repr=False)
    frame_15m: Optional[pd.DataFrame] = field(default=None, repr=False)

    def to_public_dict(self) -> dict[str, Any]:
        d = {
            "as_of_utc": self.as_of_utc,
            "contract": asdict(self.contract) if self.contract else None,
            "quote": asdict(self.quote),
            "candles_5m": asdict(self.candles_5m),
            "candles_15m": asdict(self.candles_15m),
            "context": [asdict(c) for c in self.context],
            "mcx_data_valid": self.mcx_data_valid,
            "collector_healthy": self.collector_healthy,
            "reasons": list(self.reasons),
        }
        return d


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _age_minutes(ts: Any, now: datetime) -> Optional[float]:
    if ts is None:
        return None
    try:
        t = pd.Timestamp(ts)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        else:
            t = t.tz_convert("UTC")
        return (now - t.to_pydatetime()).total_seconds() / 60.0
    except Exception:  # noqa: BLE001
        return None


def contract_from_mcx_context(ctx: Any, cfg: EngineConfig) -> ContractInfo:
    sym = str(getattr(ctx, "symbol", None) or cfg.gold_symbol or "GOLDM").upper()
    exp = getattr(ctx, "expiry", None)
    exp_s = ""
    if exp is not None:
        if getattr(exp, "tzinfo", None) is None:
            exp = exp.replace(tzinfo=UTC)
        exp_s = exp.astimezone(UTC).isoformat()
    return ContractInfo(
        symbol=sym,
        instrument_key=str(getattr(ctx, "instrument_key", "") or ""),
        trading_symbol=str(getattr(ctx, "trading_symbol", "") or ""),
        expiry_utc=exp_s,
        lot_size=int(getattr(ctx, "lot_size", 1) or 1),
        tick_size=float(getattr(ctx, "tick_size", 1.0) or 1.0),
        value_per_price_unit=float(
            getattr(ctx, "value_per_price_unit", None)
            or MCX_VALUE_PER_PRICE_UNIT.get(sym, cfg.value_per_price_unit)
        ),
    )


def build_candle_bundle(
    raw_1m: pd.DataFrame,
    *,
    timeframe_minutes: int,
    now_utc: datetime,
    max_age_minutes: float,
    min_bars: int,
) -> tuple[CandleBundle, pd.DataFrame]:
    bundle = CandleBundle(timeframe_minutes=timeframe_minutes)
    empty = pd.DataFrame(columns=["open", "high", "low", "close"])
    if raw_1m is None or raw_1m.empty:
        bundle.reject_reason = "no_1m_bars"
        return bundle, empty
    framed = resample_ohlcv(raw_1m, timeframe_minutes)
    completed = candles_completed_as_of(framed, now_utc, timeframe_minutes)
    if not validate_candle_frame(completed):
        bundle.reject_reason = "invalid_candle_frame"
        return bundle, empty
    if len(completed) < min_bars:
        bundle.bars = len(completed)
        bundle.reject_reason = f"warmup need>={min_bars} have={len(completed)}"
        return bundle, completed
    last_ts = completed.index[-1]
    age = _age_minutes(last_ts, now_utc)
    bundle.bars = len(completed)
    bundle.last_bar_utc = str(last_ts)
    bundle.age_minutes = age
    if age is None:
        bundle.reject_reason = "bar_age_unknown"
        return bundle, completed
    if age < -2:
        bundle.reject_reason = "future_dated_bar"
        return bundle, completed
    # Completed bar age naturally grows with TF; allow TF + stale buffer
    limit = max(float(max_age_minutes), float(timeframe_minutes) * 2)
    if age > limit:
        bundle.reject_reason = f"stale_bar age={age:.0f}m limit={limit:.0f}m"
        return bundle, completed
    bundle.valid = True
    return bundle, completed


def build_quote_view(
    bid: Optional[float],
    ask: Optional[float],
    ltp: Optional[float],
    *,
    source: str = "upstox",
    max_spread: float = 50.0,
) -> QuoteView:
    q = QuoteView(bid=bid, ask=ask, ltp=ltp, source=source)
    if bid is None or ask is None:
        q.reject_reason = "incomplete_book"
        return q
    try:
        b, a = float(bid), float(ask)
    except (TypeError, ValueError):
        q.reject_reason = "non_numeric_quote"
        return q
    if not (b > 0 and a > 0):
        q.reject_reason = "non_positive_quote"
        return q
    if a < b:
        q.reject_reason = "crossed_book"
        return q
    q.spread = a - b
    if q.spread > float(max_spread):
        q.reject_reason = f"spread_{q.spread:.1f}_gt_max_{max_spread}"
        # Still mark structurally valid for monitoring; entry gate uses spread
        q.valid = True
        return q
    q.valid = True
    return q


def context_from_monitor_snapshot(monitor_snap: Any) -> list[ContextObservation]:
    """Map monitor-gold Snapshot observations → non-executable context."""
    out: list[ContextObservation] = []
    obs_list = getattr(monitor_snap, "observations", None) or []
    for obs in obs_list:
        name = str(getattr(obs, "name", "") or "")
        if name.upper().startswith("MCX_GOLD") or name.upper() == "MCX_GOLD":
            # Tradeable series is handled separately via Upstox instrument_key
            continue
        note = ""
        src = str(getattr(obs, "source", "") or "")
        if "GC=F" in src or "COMEX" in src.upper() or name.upper() in ("XAUUSD",):
            note = "context_only_not_executable_mcx"
        out.append(ContextObservation(
            name=name,
            value=getattr(obs, "numeric", None),
            source=src,
            age_seconds=getattr(obs, "age_seconds", None),
            valid=bool(getattr(obs, "valid", False)),
            executable=False,
            note=note or "context",
        ))
    return out


def build_engine_snapshot(
    *,
    cfg: EngineConfig,
    contract: ContractInfo,
    raw_1m: pd.DataFrame,
    bid: Optional[float],
    ask: Optional[float],
    ltp: Optional[float],
    now_utc: Optional[datetime] = None,
    monitor_snap: Any = None,
    quote_source: str = "upstox",
) -> MarketSnapshot:
    """
    Assemble the engine MarketSnapshot from Upstox candles/quotes.

    ``monitor_snap`` (optional) adds contextual series only.
    """
    now_utc = now_utc or _utc_now()
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    snap = MarketSnapshot(as_of_utc=now_utc.isoformat())
    snap.contract = contract

    if not contract.instrument_key.startswith("MCX_FO|"):
        snap.reasons.append("instrument_not_mcx_fo")
        snap.mcx_data_valid = False
        snap.collector_healthy = False
        return snap

    snap.quote = build_quote_view(
        bid, ask, ltp, source=quote_source,
        max_spread=float(cfg.maximum_spread_points),
    )
    if not snap.quote.valid:
        snap.reasons.append(f"quote:{snap.quote.reject_reason}")

    lookback = int(getattr(cfg, "gold_breakout_lookback_bars", 20) or 20)
    atr_p = int(cfg.atr_period)
    min_5 = max(lookback + 1, atr_p + 2)
    min_15 = max(int(getattr(cfg, "trend_ema_slow", 50) or 50) + 2, 55)

    stale = float(cfg.stale_data_minutes)
    c5, f5 = build_candle_bundle(
        raw_1m, timeframe_minutes=5, now_utc=now_utc,
        max_age_minutes=stale, min_bars=min_5,
    )
    c15, f15 = build_candle_bundle(
        raw_1m, timeframe_minutes=15, now_utc=now_utc,
        max_age_minutes=max(stale * 3, 45), min_bars=min_15,
    )
    snap.candles_5m = c5
    snap.candles_15m = c15
    snap.frame_5m = f5
    snap.frame_15m = f15
    if not c5.valid:
        snap.reasons.append(f"candles_5m:{c5.reject_reason}")
    if not c15.valid:
        snap.reasons.append(f"candles_15m:{c15.reject_reason}")

    if monitor_snap is not None:
        snap.context = context_from_monitor_snapshot(monitor_snap)

    snap.mcx_data_valid = bool(snap.quote.valid and c5.valid and c15.valid)
    snap.collector_healthy = snap.mcx_data_valid
    if snap.mcx_data_valid:
        snap.reasons.append("ok")
    return snap


def persist_snapshot(snap: MarketSnapshot, path: Optional[Path] = None) -> Path:
    path = Path(path) if path else SNAPSHOT_PATH
    path.write_text(json.dumps(snap.to_public_dict(), indent=2), encoding="utf-8")
    return path


def load_persisted_snapshot(path: Optional[Path] = None) -> Optional[dict]:
    path = Path(path) if path else SNAPSHOT_PATH
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("market_snapshot unreadable: %s", exc)
        return None
