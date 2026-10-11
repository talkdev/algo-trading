"""News filters, release surprise, signals, and decision engine."""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import pandas as pd

from config import CONFIG, EngineConfig
from feeds import (
    EconomicEvent,
    atr,
    classify_direction,
    dxy_change_pct,
    events_known_as_of,
    load_calendar,
    percent_change,
    validate_candle_frame,
    value_at_or_before,
    yield_change_bps,
    yield_change_over_window,
)

UTC = timezone.utc
log = logging.getLogger("gold_news.strategy")


# --- news_filter.py ---




def is_relevant_event(event: EconomicEvent | dict,
                      importance_required: str = "high") -> bool:
    if isinstance(event, EconomicEvent):
        currency = event.currency
        importance = event.importance
        status = event.status
    else:
        currency = str(event.get("currency", ""))
        importance = str(event.get("importance", ""))
        status = str(event.get("status", "scheduled"))
    if currency.upper() != "USD":
        return False
    if importance.lower() != importance_required.lower():
        return False
    # Delayed / uncertain high-impact releases keep the block active once known.
    if status.lower() in ("cancelled",):
        return False
    return True


def event_block_window(event: EconomicEvent | dict,
                       cfg: Optional[EngineConfig | dict] = None
                       ) -> Optional[tuple[datetime, datetime]]:
    cfg = cfg or CONFIG
    before = int(_get(cfg, "news_block_before_minutes", 15))
    after = int(_get(cfg, "news_block_after_minutes", 15))
    if isinstance(event, EconomicEvent):
        event_time = event.scheduled_time_utc
        status = event.status
    else:
        event_time = event.get("scheduled_time_utc")
        status = str(event.get("status", "scheduled"))
    if event_time is None:
        return None
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=UTC)
    else:
        event_time = event_time.astimezone(UTC)
    # Delayed / uncertain: extend the after-window until status is verified.
    if status.lower() in ("delayed", "uncertain"):
        after = max(after, 24 * 60)
    from datetime import timedelta
    return event_time - timedelta(minutes=before), event_time + timedelta(minutes=after)


def is_inside_news_block(now_utc: datetime,
                         events: Iterable[EconomicEvent | dict],
                         cfg: Optional[EngineConfig | dict] = None) -> bool:
    """
    True when `now_utc` falls inside any relevant event's blocked window.

    minutes_from_event < 0  -> release is still ahead
    minutes_from_event == 0 -> scheduled release time
    minutes_from_event > 0  -> minutes since scheduled time

    Unknown event times are NOT treated as safely outside the news window — such
    events are ignored for the timed block (cannot compute a window) but callers
    should treat delayed/uncertain statuses via event_block_window.
    """
    cfg = cfg or CONFIG
    importance = str(_get(cfg, "news_importance_required", "high"))
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    else:
        now_utc = now_utc.astimezone(UTC)

    before = float(_get(cfg, "news_block_before_minutes", 15))
    after = float(_get(cfg, "news_block_after_minutes", 15))

    for event in events:
        if not is_relevant_event(event, importance):
            continue
        if isinstance(event, EconomicEvent):
            event_time = event.scheduled_time_utc
            status = event.status
        else:
            event_time = event.get("scheduled_time_utc")
            status = str(event.get("status", "scheduled"))
        if event_time is None:
            # Spec: if time unknown, do not treat as safely outside — block.
            log.warning(
                "High-impact USD event missing scheduled time — blocking entries (%s)",
                (event.event_name if isinstance(event, EconomicEvent)
                 else event.get("event_name")),
            )
            return True
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=UTC)
        else:
            event_time = event_time.astimezone(UTC)
        minutes_from_event = (now_utc - event_time).total_seconds() / 60.0
        local_after = after
        if status.lower() in ("delayed", "uncertain"):
            # Block new entries until status is verified.
            if minutes_from_event >= -before:
                return True
            continue
        if -before <= minutes_from_event <= local_after:
            return True
    return False


def nearest_relevant_event(now_utc: datetime,
                           events: Iterable[EconomicEvent | dict],
                           cfg: Optional[EngineConfig | dict] = None
                           ) -> Optional[EconomicEvent | dict]:
    cfg = cfg or CONFIG
    importance = str(_get(cfg, "news_importance_required", "high"))
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    best = None
    best_abs = None
    for event in events:
        if not is_relevant_event(event, importance):
            continue
        event_time = (
            event.scheduled_time_utc if isinstance(event, EconomicEvent)
            else event.get("scheduled_time_utc")
        )
        if event_time is None:
            continue
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=UTC)
        delta = abs((now_utc.astimezone(UTC) - event_time.astimezone(UTC)).total_seconds())
        if best_abs is None or delta < best_abs:
            best_abs = delta
            best = event
    return best


def _get(cfg: EngineConfig | dict, key: str, default):
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default)

# --- release_analysis.py ---



# Stronger reading = hawkish (gold bearish on beat)
_HAWKISH_ON_BEAT = (
    "cpi", "ppi", "pce", "inflation", "core cpi", "core pce", "core ppi",
    "nonfarm", "non-farm", "nfp", "payroll", "employment", "average hourly",
    "hourly earnings", "wage", "adp", "jolts", "job openings",
    "gdp", "retail sales", "durable", "ism", "pmi", "industrial production",
    "capacity utilization", "factory orders", "consumer confidence",
    "michigan", "housing starts", "building permits", "existing home",
    "new home", "fomc", "fed rate", "interest rate", "federal funds",
    "rate decision", "dot plot",
)

# Higher reading = dovish (gold bullish on beat) — labor slack / claims
_DOVISH_ON_BEAT = (
    "unemployment", "jobless", "initial claims", "continuing claims",
    "claimant",
)


@dataclass
class SurpriseResult:
    event_id: str = ""
    event_name: str = ""
    actual: Optional[float] = None
    forecast: Optional[float] = None
    previous: Optional[float] = None
    delta: Optional[float] = None          # actual - forecast
    surprise: str = "unknown"              # beat | miss | inline | unknown
    polarity: str = "unknown"              # hawkish_on_beat | dovish_on_beat | unknown
    gold_bias: str = "unknown"             # bullish | bearish | neutral | unknown
    minutes_since_release: Optional[float] = None
    reason: str = ""


def event_polarity(event_name: str) -> str:
    name = (event_name or "").strip().lower()
    if not name:
        return "unknown"
    for key in _DOVISH_ON_BEAT:
        if key in name:
            return "dovish_on_beat"
    for key in _HAWKISH_ON_BEAT:
        if key in name:
            return "hawkish_on_beat"
    return "unknown"


def classify_surprise(
    actual: Optional[float],
    forecast: Optional[float],
    *,
    epsilon: float = 0.0,
) -> str:
    """Return beat | miss | inline | unknown (never invents actual from forecast)."""
    if actual is None or forecast is None:
        return "unknown"
    delta = float(actual) - float(forecast)
    if abs(delta) <= float(epsilon):
        return "inline"
    return "beat" if delta > 0 else "miss"


def surprise_to_gold_bias(
    event_name: str,
    surprise: str,
    *,
    polarity: Optional[str] = None,
) -> str:
    """
    Map release surprise → gold bias.

    hawkish_on_beat: beat→bearish, miss→bullish
    dovish_on_beat:  beat→bullish, miss→bearish
    """
    if surprise in ("unknown",):
        return "unknown"
    if surprise == "inline":
        return "neutral"
    pol = polarity or event_polarity(event_name)
    if pol == "unknown":
        return "unknown"
    if pol == "hawkish_on_beat":
        return "bearish" if surprise == "beat" else "bullish"
    if pol == "dovish_on_beat":
        return "bullish" if surprise == "beat" else "bearish"
    return "unknown"


def analyze_release(
    event: EconomicEvent | dict,
    *,
    now_utc: Optional[datetime] = None,
    epsilon: float = 0.0,
) -> SurpriseResult:
    if isinstance(event, EconomicEvent):
        eid = event.event_id
        name = event.event_name
        actual = event.actual
        forecast = event.forecast
        previous = event.previous
        release_t = event.actual_release_time_utc or event.scheduled_time_utc
    else:
        eid = str(event.get("event_id", ""))
        name = str(event.get("event_name", ""))
        actual = event.get("actual")
        forecast = event.get("forecast")
        previous = event.get("previous")
        release_t = event.get("actual_release_time_utc") or event.get("scheduled_time_utc")

    pol = event_polarity(name)
    surprise = classify_surprise(actual, forecast, epsilon=epsilon)
    bias = surprise_to_gold_bias(name, surprise, polarity=pol)
    delta = None
    if actual is not None and forecast is not None:
        delta = float(actual) - float(forecast)

    mins = None
    if now_utc is not None and release_t is not None:
        nt = now_utc if now_utc.tzinfo else now_utc.replace(tzinfo=UTC)
        rt = release_t if getattr(release_t, "tzinfo", None) else release_t.replace(tzinfo=UTC)
        if getattr(rt, "tzinfo", None):
            rt = rt.astimezone(UTC)
        mins = (nt.astimezone(UTC) - rt).total_seconds() / 60.0

    reason = "ok"
    if surprise == "unknown":
        reason = "missing_actual_or_forecast"
    elif surprise == "inline":
        reason = "inline_no_edge"
    elif bias == "unknown":
        reason = "unknown_event_polarity"

    return SurpriseResult(
        event_id=eid,
        event_name=name,
        actual=None if actual is None else float(actual),
        forecast=None if forecast is None else float(forecast),
        previous=None if previous is None else float(previous),
        delta=delta,
        surprise=surprise,
        polarity=pol,
        gold_bias=bias,
        minutes_since_release=mins,
        reason=reason,
    )


def _release_time(event: EconomicEvent | dict) -> Optional[datetime]:
    if isinstance(event, EconomicEvent):
        t = event.actual_release_time_utc or event.scheduled_time_utc
    else:
        t = event.get("actual_release_time_utc") or event.get("scheduled_time_utc")
    if t is None:
        return None
    if t.tzinfo is None:
        return t.replace(tzinfo=UTC)
    return t.astimezone(UTC)


def _has_actual(event: EconomicEvent | dict) -> bool:
    if isinstance(event, EconomicEvent):
        return event.actual is not None
    return event.get("actual") is not None and event.get("actual") != ""


def find_recent_release(
    now_utc: datetime,
    events: Iterable[EconomicEvent | dict],
    *,
    importance_required: str = "high",
    reaction_minutes: float = 180.0,
    block_after_minutes: float = 15.0,
) -> Optional[EconomicEvent | dict]:
    """
    Most recent USD high-impact event with an actual, still inside the
    post-release reaction window (after the news block has cleared).
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    else:
        now_utc = now_utc.astimezone(UTC)

    best = None
    best_age = None
    for ev in events:
        if not is_relevant_event(ev, importance_required):
            continue
        if not _has_actual(ev):
            continue
        rt = _release_time(ev)
        if rt is None:
            continue
        age_min = (now_utc - rt).total_seconds() / 60.0
        # Still inside block window → not yet a tradeable reaction
        if age_min < block_after_minutes:
            continue
        if age_min > reaction_minutes:
            continue
        if best_age is None or age_min < best_age:
            best = ev
            best_age = age_min
    return best


def find_pending_release_in_window(
    now_utc: datetime,
    events: Iterable[EconomicEvent | dict],
    *,
    importance_required: str = "high",
    reaction_minutes: float = 180.0,
    block_after_minutes: float = 15.0,
) -> Optional[EconomicEvent | dict]:
    """
    High-impact scheduled release whose reaction window is open but actual
    (or forecast) is still missing — must block surprise_required entries.
    Exempt verified cancellations (status/cancelled flags).
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    else:
        now_utc = now_utc.astimezone(UTC)

    best = None
    best_age = None
    for ev in events:
        if not is_relevant_event(ev, importance_required):
            continue
        status = ""
        if isinstance(ev, EconomicEvent):
            status = str(getattr(ev, "status", "") or "").lower()
        else:
            status = str(ev.get("status") or ev.get("event_status") or "").lower()
        if status in ("cancelled", "canceled", "postponed", "withdrawn"):
            continue
        rt = _release_time(ev)
        if rt is None:
            continue
        age_min = (now_utc - rt).total_seconds() / 60.0
        if age_min < block_after_minutes:
            continue
        if age_min > reaction_minutes:
            continue
        # Missing actual OR missing forecast → cannot classify surprise
        actual_ok = _has_actual(ev)
        if isinstance(ev, EconomicEvent):
            forecast_ok = ev.forecast is not None
        else:
            forecast_ok = ev.get("forecast") is not None and ev.get("forecast") != ""
        if actual_ok and forecast_ok:
            continue
        if best_age is None or age_min < best_age:
            best = ev
            best_age = age_min
    return best


def combine_bias(
    macro_bias: str,
    surprise_bias: str,
    *,
    require_agreement: bool = True,
) -> tuple[str, str]:
    """
    Merge market (DXY/yield) bias with release-surprise bias.

    Returns (combined_bias, reason).
    """
    if surprise_bias in ("unknown",):
        return "neutral", "surprise_unknown"
    if surprise_bias == "neutral":
        return "neutral", "surprise_inline"
    if surprise_bias not in ("bullish", "bearish"):
        return "neutral", "surprise_invalid"

    if not require_agreement:
        return surprise_bias, "surprise_primary"

    if macro_bias == surprise_bias:
        return surprise_bias, "surprise_macro_agree"
    if macro_bias in ("neutral", "mixed", "unknown"):
        return "neutral", f"macro_{macro_bias}_vs_surprise_{surprise_bias}"
    return "mixed", f"surprise_{surprise_bias}_macro_{macro_bias}_disagree"

# --- signal_engine.py ---


@dataclass
class SignalResult:
    decision: str                          # BUY | SELL | NO_TRADE
    bias: str = "neutral"                  # bullish | bearish | neutral | mixed
    dxy_direction: str = "unknown"
    yield_direction: str = "unknown"
    real_yield_direction: str = "unknown"
    dxy_change_pct: Optional[float] = None
    yield_change_bps: Optional[float] = None
    real_yield_change_bps: Optional[float] = None
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def get_gold_bias(dxy_direction: str, yield_direction: str,
                  real_yield_direction: str = "unknown",
                  use_real_yield: bool = False) -> str:
    """
    Require dollar and nominal yield to agree (conservative).

    Real yields: falling supports bullish, rising supports bearish.
    When ``use_real_yield`` is True, unknown/flat real yields block (mixed)
    — the filter must not be a no-op (audit P0 #20).
    """
    if dxy_direction == "down" and yield_direction == "down":
        bias = "bullish"
    elif dxy_direction == "up" and yield_direction == "up":
        bias = "bearish"
    else:
        return "neutral"

    if not use_real_yield:
        return bias
    if real_yield_direction in ("unknown", "flat", ""):
        return "mixed"  # required input missing / unclassifiable
    if bias == "bullish" and real_yield_direction == "up":
        return "mixed"
    if bias == "bearish" and real_yield_direction == "down":
        return "mixed"
    return bias


def get_price_confirmation(gold_candles: pd.DataFrame,
                           gold_bias: str,
                           lookback: int = 6) -> str:
    """
    Confirm bias with a completed-candle breakout.

    The last row must be the newly completed candle. Earlier rows are the
    lookback range and must not include the current candle.
    """
    if gold_candles is None or len(gold_candles) < lookback + 1:
        return "no_trade"
    if not validate_candle_frame(gold_candles):
        return "no_trade"

    current = gold_candles.iloc[-1]
    previous = gold_candles.iloc[-lookback - 1:-1]
    if len(previous) < lookback:
        return "no_trade"

    previous_high = float(previous["high"].max())
    previous_low = float(previous["low"].min())
    close = float(current["close"])

    # Exact touch does not qualify.
    if gold_bias == "bullish" and close > previous_high:
        return "buy"
    if gold_bias == "bearish" and close < previous_low:
        return "sell"
    return "no_trade"


def compute_market_directions(
    now_utc: datetime,
    dxy: pd.Series,
    yields: pd.Series,
    real_yields: Optional[pd.Series] = None,
    cfg: Optional[EngineConfig | dict] = None,
) -> dict[str, Any]:
    cfg = cfg or CONFIG
    window = int(_get(cfg, "reaction_window_minutes", 15))
    stale = int(_get(cfg, "stale_data_minutes", 5))
    dxy_ch = dxy_change_pct(dxy, now_utc, window, stale)
    y_ch = yield_change_over_window(yields, now_utc, window, stale)
    ry_ch = None
    if real_yields is not None and not real_yields.empty:
        ry_ch = yield_change_over_window(real_yields, now_utc, window, stale)

    return {
        "dxy_change_pct": dxy_ch,
        "yield_change_bps": y_ch,
        "real_yield_change_bps": ry_ch,
        "dxy_direction": classify_direction(
            dxy_ch, float(_get(cfg, "minimum_dxy_change_pct", 0.03))),
        "yield_direction": classify_direction(
            y_ch, float(_get(cfg, "minimum_yield_change_bps", 2.0))),
        "real_yield_direction": classify_direction(
            ry_ch, float(_get(cfg, "minimum_real_yield_change_bps", 2.0))),
    }


def generate_signal(
    now_utc: datetime,
    events,
    dxy_direction: str,
    yield_direction: str,
    gold_candles: pd.DataFrame,
    data_is_valid: bool,
    daily_loss_limit_reached: bool,
    has_open_position: bool,
    *,
    real_yield_direction: str = "unknown",
    cfg: Optional[EngineConfig | dict] = None,
    last_breakout_key: Optional[str] = None,
) -> SignalResult:
    """
    Compose all entry gates into BUY / SELL / NO_TRADE.

    This function generates the signal only. Order management still calculates
    size, verifies margin/spread, and confirms fills.
    """
    cfg = cfg or CONFIG
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    else:
        now_utc = now_utc.astimezone(UTC)

    result = SignalResult(
        decision="NO_TRADE",
        dxy_direction=dxy_direction,
        yield_direction=yield_direction,
        real_yield_direction=real_yield_direction,
    )

    # Rule 1: Data must be valid.
    if not data_is_valid:
        result.reason = "data_invalid"
        return result

    # Rule 2: Stop trading after the daily loss limit.
    if daily_loss_limit_reached:
        result.reason = "daily_loss_limit"
        return result

    # Rule 3: Do not open another position.
    max_open = int(_get(cfg, "maximum_open_positions", 1))
    if has_open_position and max_open <= 1:
        result.reason = "has_open_position"
        return result

    # Rule 4: Avoid scheduled high-impact US news (Version A / filtered).
    news_mode = str(_get(cfg, "news_mode", "filtered")).lower()
    inside = is_inside_news_block(now_utc, events, cfg)
    result.meta["inside_news_block"] = inside
    if news_mode == "filtered" and inside:
        result.reason = "news_block"
        return result

    # Rule 5: Dollar / yield bias.
    bias = get_gold_bias(
        dxy_direction,
        yield_direction,
        real_yield_direction=real_yield_direction,
        use_real_yield=bool(_get(cfg, "use_real_yield", True)),
    )
    result.bias = bias
    if bias in ("neutral", "mixed"):
        result.reason = f"bias_{bias}"
        return result

    # Rule 6: Gold breakout confirmation on completed candles.
    lookback = int(_get(cfg, "gold_breakout_lookback_bars", 6))
    confirmation = get_price_confirmation(gold_candles, bias, lookback=lookback)
    result.meta["confirmation"] = confirmation
    if confirmation == "no_trade":
        result.reason = "no_breakout"
        return result

    # Fresh breakout only — do not re-enter on the same breakout key.
    if gold_candles is not None and len(gold_candles) > 0:
        bar_ts = gold_candles.index[-1]
        breakout_key = f"{confirmation}:{bar_ts}"
        result.meta["breakout_key"] = breakout_key
        if last_breakout_key and breakout_key == last_breakout_key:
            result.reason = "stale_breakout"
            return result

    if confirmation == "buy" and bias == "bullish":
        result.decision = "BUY"
        result.reason = "bullish_breakout"
        if news_mode == "aware":
            result.meta["news_day"] = inside
        return result
    if confirmation == "sell" and bias == "bearish":
        result.decision = "SELL"
        result.reason = "bearish_breakout"
        if news_mode == "aware":
            result.meta["news_day"] = inside
        return result

    result.reason = "confirmation_bias_mismatch"
    return result


def build_signal_from_market(
    now_utc: datetime,
    events,
    dxy: pd.Series,
    yields: pd.Series,
    gold_candles: pd.DataFrame,
    data_is_valid: bool,
    daily_loss_limit_reached: bool,
    has_open_position: bool,
    real_yields: Optional[pd.Series] = None,
    cfg: Optional[EngineConfig | dict] = None,
    last_breakout_key: Optional[str] = None,
) -> SignalResult:
    """Convenience wrapper: compute directions then generate_signal."""
    dirs = compute_market_directions(now_utc, dxy, yields, real_yields, cfg)
    result = generate_signal(
        now_utc=now_utc,
        events=events,
        dxy_direction=dirs["dxy_direction"],
        yield_direction=dirs["yield_direction"],
        gold_candles=gold_candles,
        data_is_valid=data_is_valid,
        daily_loss_limit_reached=daily_loss_limit_reached,
        has_open_position=has_open_position,
        real_yield_direction=dirs["real_yield_direction"],
        cfg=cfg,
        last_breakout_key=last_breakout_key,
    )
    result.dxy_change_pct = dirs["dxy_change_pct"]
    result.yield_change_bps = dirs["yield_change_bps"]
    result.real_yield_change_bps = dirs["real_yield_change_bps"]
    result.meta["atr"] = atr(
        gold_candles, int(_get(cfg or CONFIG, "atr_period", 14))
    )
    return result


def _get(cfg: EngineConfig | dict, key: str, default):
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default)

# --- news_engine.py ---


@dataclass
class NewsContext:
    inside_block: bool = False
    allow_new_entries: bool = True
    nearest_event_id: str = ""
    nearest_event_name: str = ""
    minutes_from_event: Optional[float] = None
    block_window_start_utc: str = ""
    block_window_end_utc: str = ""
    relevant_upcoming: list[dict[str, Any]] = field(default_factory=list)
    surprise: Optional[SurpriseResult] = None
    surprise_active: bool = False
    pending_release_without_actual: bool = False
    pending_release_name: str = ""
    reason: str = ""


@dataclass
class MacroContext:
    data_valid: bool = False
    dxy: Optional[float] = None
    yield_pct: Optional[float] = None
    dxy_change_pct: Optional[float] = None
    yield_change_bps: Optional[float] = None
    dxy_direction: str = "unknown"
    yield_direction: str = "unknown"
    real_yield_direction: str = "unknown"
    dxy_age_minutes: Optional[float] = None
    yield_age_minutes: Optional[float] = None
    effective_as_of_utc: str = ""
    reason: str = ""


@dataclass
class TradeDecision:
    decision: str  # BUY | SELL | NO_TRADE
    reason: str
    bias: str = "neutral"
    news: NewsContext = field(default_factory=NewsContext)
    macros: MacroContext = field(default_factory=MacroContext)
    signal: Optional[SignalResult] = None
    timestamp_utc: str = ""

    def to_log_dict(self) -> dict[str, Any]:
        sur = self.news.surprise
        return {
            "decision": self.decision,
            "reason": self.reason,
            "bias": self.bias,
            "news_block": self.news.inside_block,
            "nearest_event": self.news.nearest_event_name,
            "minutes_from_event": self.news.minutes_from_event,
            "surprise": sur.surprise if sur else None,
            "surprise_bias": sur.gold_bias if sur else None,
            "surprise_delta": sur.delta if sur else None,
            "dxy_dir": self.macros.dxy_direction,
            "yield_dir": self.macros.yield_direction,
            "dxy_chg": self.macros.dxy_change_pct,
            "yield_chg_bps": self.macros.yield_change_bps,
        }


class NewsDecisionEngine:
    """Stateful evaluator used by the paper/live loop."""

    def __init__(self, cfg: EngineConfig, events: Optional[list[EconomicEvent]] = None):
        self.cfg = cfg
        self.events = list(events or [])
        if not self.events and cfg.calendar_path:
            self.events = load_calendar(cfg.calendar_path)

    def reload_calendar(self, path: str = "") -> int:
        path = path or self.cfg.calendar_path
        self.events = load_calendar(path)
        return len(self.events)

    def set_events(self, events: list) -> int:
        """Replace the in-memory calendar (used by CalendarFeedManager)."""
        self.events = list(events or [])
        return len(self.events)

    def evaluate_news(self, now_utc: datetime) -> NewsContext:
        now_utc = _utc(now_utc)
        visible = events_known_as_of(self.events, now_utc)
        cfg = self.cfg
        importance = cfg.news_importance_required
        inside = is_inside_news_block(now_utc, visible, cfg)
        nearest = nearest_relevant_event(now_utc, visible, cfg)

        ctx = NewsContext(inside_block=inside)
        news_mode = str(cfg.news_mode or "filtered").lower()
        # filtered: block entries inside window; aware: allow but tag
        ctx.allow_new_entries = (not inside) if news_mode == "filtered" else True
        if inside and news_mode == "filtered":
            ctx.reason = "news_block"
        elif inside:
            ctx.reason = "news_window_tagged"
        else:
            ctx.reason = "clear"

        if nearest is not None:
            if isinstance(nearest, EconomicEvent):
                ctx.nearest_event_id = nearest.event_id
                ctx.nearest_event_name = nearest.event_name
                et = nearest.scheduled_time_utc
            else:
                ctx.nearest_event_id = str(nearest.get("event_id", ""))
                ctx.nearest_event_name = str(nearest.get("event_name", ""))
                et = nearest.get("scheduled_time_utc")
            if et is not None:
                et = _utc(et)
                ctx.minutes_from_event = (now_utc - et).total_seconds() / 60.0
                window = event_block_window(nearest, cfg)
                if window:
                    ctx.block_window_start_utc = window[0].isoformat()
                    ctx.block_window_end_utc = window[1].isoformat()

        # Upcoming relevant USD high-impact in the next 24h (for logging).
        horizon = now_utc + timedelta(hours=24)
        upcoming = []
        for ev in visible:
            if not is_relevant_event(ev, importance):
                continue
            et = ev.scheduled_time_utc if isinstance(ev, EconomicEvent) else ev.get("scheduled_time_utc")
            if et is None:
                continue
            et = _utc(et)
            if now_utc - timedelta(minutes=cfg.news_block_after_minutes) <= et <= horizon:
                upcoming.append({
                    "event_id": ev.event_id if isinstance(ev, EconomicEvent) else ev.get("event_id"),
                    "event_name": ev.event_name if isinstance(ev, EconomicEvent) else ev.get("event_name"),
                    "scheduled_time_utc": et.isoformat(),
                    "minutes_from_now": (et - now_utc).total_seconds() / 60.0,
                })
        ctx.relevant_upcoming = sorted(upcoming, key=lambda r: r["minutes_from_now"])

        # Post-block surprise analysis for the most recent high-impact actual.
        reaction_m = float(getattr(cfg, "surprise_reaction_minutes", 180))
        block_after = float(cfg.news_block_after_minutes)
        if bool(getattr(cfg, "use_release_surprise", True)):
            recent = find_recent_release(
                now_utc, visible,
                importance_required=importance,
                reaction_minutes=reaction_m,
                block_after_minutes=block_after,
            )
            if recent is not None:
                ctx.surprise = analyze_release(
                    recent, now_utc=now_utc,
                    epsilon=float(getattr(cfg, "surprise_epsilon", 0.0)),
                )
                ctx.surprise_active = True
            # P0 #17: scheduled release in window without actual must not
            # fall through to macro-only trading when surprise is required.
            if bool(getattr(cfg, "surprise_required_in_window", True)):
                pending = find_pending_release_in_window(
                    now_utc, visible,
                    importance_required=importance,
                    reaction_minutes=reaction_m,
                    block_after_minutes=block_after,
                )
                if pending is not None:
                    ctx.pending_release_without_actual = True
                    if isinstance(pending, EconomicEvent):
                        ctx.pending_release_name = pending.event_name
                    else:
                        ctx.pending_release_name = str(pending.get("event_name") or "")
                    ctx.allow_new_entries = False
                    ctx.reason = (
                        f"surprise_pending_actual:{ctx.pending_release_name or 'event'}"
                    )
        return ctx

    def evaluate_macros(
        self,
        now_utc: datetime,
        dxy: pd.Series,
        yields: pd.Series,
        real_yields: Optional[pd.Series] = None,
    ) -> MacroContext:
        """
        Classify DXY / yield direction over the reaction window.

        When US cash hours are closed (weekend / overnight), use the latest
        available macro timestamp as the window end — provided it is still
        within ``macro_stale_minutes`` of wall-clock now.
        """
        now_utc = _utc(now_utc)
        cfg = self.cfg
        macro_stale = int(getattr(cfg, "macro_stale_minutes", 4320) or 4320)
        window = int(cfg.reaction_window_minutes)
        # Point lookup tolerance: at least the reaction window (+ buffer) so the
        # "previous" observation at as_of-window can be found on 5m Yahoo bars.
        # Series age for NEW entries is gated separately by macro_trade_max_age_minutes.
        point_stale = int(max(
            int(cfg.stale_data_minutes),
            int(cfg.reaction_window_minutes) + 10,
        ))

        ctx = MacroContext()
        if dxy is None or dxy.empty or yields is None or yields.empty:
            ctx.reason = "macro_series_empty"
            return ctx

        dxy_last_t = pd.Timestamp(dxy.index[-1]).to_pydatetime()
        yld_last_t = pd.Timestamp(yields.index[-1]).to_pydatetime()
        dxy_last_t = _utc(dxy_last_t)
        yld_last_t = _utc(yld_last_t)

        ctx.dxy_age_minutes = (now_utc - dxy_last_t).total_seconds() / 60.0
        ctx.yield_age_minutes = (now_utc - yld_last_t).total_seconds() / 60.0

        # Reject future-dated / non-finite macro observations
        if ctx.dxy_age_minutes < -2 or ctx.yield_age_minutes < -2:
            ctx.reason = "macro_future_timestamp"
            ctx.data_valid = False
            return ctx
        try:
            last_dxy = float(dxy.iloc[-1])
            last_yld = float(yields.iloc[-1])
            if not (math.isfinite(last_dxy) and math.isfinite(last_yld)
                    and last_dxy > 0 and last_yld > 0):
                ctx.reason = "macro_invalid_value"
                ctx.data_valid = False
                return ctx
        except Exception:  # noqa: BLE001
            ctx.reason = "macro_invalid_value"
            ctx.data_valid = False
            return ctx

        if ctx.dxy_age_minutes > macro_stale or ctx.yield_age_minutes > macro_stale:
            ctx.reason = (
                f"macro_too_stale dxy_age={ctx.dxy_age_minutes:.0f}m "
                f"yield_age={ctx.yield_age_minutes:.0f}m limit={macro_stale}m"
            )
            return ctx

        # Stricter freshness for NEW entries (weekend/cache gaps → NO_TRADE).
        trade_max = float(getattr(cfg, "macro_trade_max_age_minutes", 60) or 60)
        if ctx.dxy_age_minutes > trade_max or ctx.yield_age_minutes > trade_max:
            ctx.reason = (
                f"macro_stale_for_entry dxy_age={ctx.dxy_age_minutes:.0f}m "
                f"yield_age={ctx.yield_age_minutes:.0f}m trade_limit={trade_max:.0f}m"
            )
            ctx.data_valid = False
            return ctx

        # Window end = latest shared macro time (handles weekend / US close).
        as_of = min(now_utc, dxy_last_t, yld_last_t)
        ctx.effective_as_of_utc = as_of.isoformat()

        max_age = timedelta(minutes=point_stale)
        dxy_now, _ = value_at_or_before(dxy, as_of, max_age=max_age)
        dxy_prev, _ = value_at_or_before(
            dxy, as_of - timedelta(minutes=window), max_age=max_age,
        )
        y_now, _ = value_at_or_before(yields, as_of, max_age=max_age)
        y_prev, _ = value_at_or_before(
            yields, as_of - timedelta(minutes=window), max_age=max_age,
        )

        ctx.dxy = dxy_now
        ctx.yield_pct = y_now
        ctx.dxy_change_pct = percent_change(dxy_now, dxy_prev)
        ctx.yield_change_bps = yield_change_bps(y_now, y_prev)
        ctx.dxy_direction = classify_direction(
            ctx.dxy_change_pct, float(cfg.minimum_dxy_change_pct),
        )
        ctx.yield_direction = classify_direction(
            ctx.yield_change_bps, float(cfg.minimum_yield_change_bps),
        )

        if real_yields is not None and not real_yields.empty:
            ry_now, _ = value_at_or_before(real_yields, as_of, max_age=max_age)
            ry_prev, _ = value_at_or_before(
                real_yields, as_of - timedelta(minutes=window), max_age=max_age,
            )
            ry_chg = yield_change_bps(ry_now, ry_prev)
            ctx.real_yield_direction = classify_direction(
                ry_chg, float(cfg.minimum_real_yield_change_bps),
            )
        elif bool(getattr(cfg, "use_real_yield", False)):
            # Config asks for real yields but feed not wired → fail closed.
            ctx.real_yield_direction = "unknown"
            ctx.reason = "real_yield_required_but_missing"
            ctx.data_valid = False
            return ctx

        if ctx.dxy_direction == "unknown" or ctx.yield_direction == "unknown":
            ctx.reason = "macro_change_unavailable"
            ctx.data_valid = False
            return ctx

        if bool(getattr(cfg, "use_real_yield", False)) and ctx.real_yield_direction in (
            "unknown", "flat", "",
        ):
            ctx.reason = "real_yield_unclassifiable"
            ctx.data_valid = False
            return ctx

        ctx.data_valid = True
        ctx.reason = "ok"
        return ctx

    def decide(
        self,
        now_utc: datetime,
        dxy: pd.Series,
        yields: pd.Series,
        gold_candles: pd.DataFrame,
        *,
        daily_loss_limit_reached: bool,
        has_open_position: bool,
        real_yields: Optional[pd.Series] = None,
        last_breakout_key: Optional[str] = None,
        gold_data_valid: bool = True,
    ) -> TradeDecision:
        now_utc = _utc(now_utc)
        news = self.evaluate_news(now_utc)
        macros = self.evaluate_macros(now_utc, dxy, yields, real_yields)

        decision = TradeDecision(
            decision="NO_TRADE",
            reason="",
            news=news,
            macros=macros,
            timestamp_utc=now_utc.isoformat(),
        )

        if daily_loss_limit_reached:
            decision.reason = "daily_loss_limit"
            return decision
        if has_open_position:
            decision.reason = "has_open_position"
            return decision
        if not gold_data_valid:
            decision.reason = "gold_data_invalid"
            return decision
        if not macros.data_valid:
            decision.reason = macros.reason or "macros_invalid"
            return decision
        if not news.allow_new_entries:
            decision.reason = news.reason or "news_block"
            decision.bias = get_gold_bias(
                macros.dxy_direction, macros.yield_direction,
                macros.real_yield_direction, self.cfg.use_real_yield,
            )
            return decision

        
        macro_bias = get_gold_bias(
            macros.dxy_direction, macros.yield_direction,
            macros.real_yield_direction, self.cfg.use_real_yield,
        )

        # When a fresh high-impact actual is in the reaction window, require
        # surprise classification (and usually agreement with macro bias).
        bias = macro_bias
        bias_reason = "macro_only"
        if news.surprise_active and news.surprise is not None:
            require_agree = bool(getattr(self.cfg, "require_surprise_macro_agree", True))
            bias, bias_reason = combine_bias(
                macro_bias, news.surprise.gold_bias,
                require_agreement=require_agree,
            )
            if bool(getattr(self.cfg, "surprise_required_in_window", True)):
                if news.surprise.gold_bias in ("unknown", "neutral"):
                    decision.decision = "NO_TRADE"
                    decision.reason = news.surprise.reason or f"surprise_{news.surprise.surprise}"
                    decision.bias = bias
                    return decision
                if bias in ("neutral", "mixed"):
                    decision.decision = "NO_TRADE"
                    decision.reason = bias_reason
                    decision.bias = bias
                    return decision

        signal = SignalResult(
            decision="NO_TRADE",
            bias=bias,
            dxy_direction=macros.dxy_direction,
            yield_direction=macros.yield_direction,
            real_yield_direction=macros.real_yield_direction,
            dxy_change_pct=macros.dxy_change_pct,
            yield_change_bps=macros.yield_change_bps,
        )
        signal.meta["inside_news_block"] = news.inside_block
        signal.meta["macro_bias"] = macro_bias
        signal.meta["bias_reason"] = bias_reason
        signal.meta["news_context"] = asdict(news)
        signal.meta["macro_context"] = asdict(macros)

        if bias in ("neutral", "mixed"):
            decision.decision = "NO_TRADE"
            decision.reason = f"bias_{bias}" if bias_reason == "macro_only" else bias_reason
            decision.bias = bias
            decision.signal = signal
            return decision

        confirmation = get_price_confirmation(
            gold_candles, bias, self.cfg.gold_breakout_lookback_bars,
        )
        if confirmation == "no_trade":
            decision.decision = "NO_TRADE"
            decision.reason = "no_breakout"
            decision.bias = bias
            decision.signal = signal
            return decision

        breakout_key = ""
        if gold_candles is not None and len(gold_candles):
            breakout_key = f"{confirmation}:{gold_candles.index[-1]}"
            signal.meta["breakout_key"] = breakout_key
            if last_breakout_key and breakout_key == last_breakout_key:
                decision.decision = "NO_TRADE"
                decision.reason = "stale_breakout"
                decision.bias = bias
                decision.signal = signal
                return decision

        if confirmation == "buy" and bias == "bullish":
            signal.decision = "BUY"
            signal.reason = (
                "bullish_breakout_surprise" if news.surprise_active else "bullish_breakout"
            )
        elif confirmation == "sell" and bias == "bearish":
            signal.decision = "SELL"
            signal.reason = (
                "bearish_breakout_surprise" if news.surprise_active else "bearish_breakout"
            )
        else:
            signal.decision = "NO_TRADE"
            signal.reason = "confirmation_bias_mismatch"

        decision.decision = signal.decision
        decision.reason = signal.reason
        decision.bias = bias
        decision.signal = signal
        return decision


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)
