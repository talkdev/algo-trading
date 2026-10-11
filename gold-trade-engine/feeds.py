"""Market data, macros, calendar feeds, and indicators."""

from __future__ import annotations

from config import MCX_VALUE_PER_PRICE_UNIT, SAMPLE_DATA_DIR

# --- calendar ---
import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo


UTC = timezone.utc


@dataclass
class EconomicEvent:
    event_id: str
    event_name: str
    currency: str
    importance: str
    scheduled_time_utc: Optional[datetime] = None
    forecast: Optional[float] = None
    previous: Optional[float] = None
    actual: Optional[float] = None
    status: str = "scheduled"  # scheduled | released | delayed | cancelled | revised
    source: str = ""
    retrieved_at_utc: Optional[datetime] = None
    actual_release_time_utc: Optional[datetime] = None  # when actual first became known

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for key in ("scheduled_time_utc", "retrieved_at_utc", "actual_release_time_utc"):
            if d[key] is not None:
                d[key] = _iso(d[key])
        return d


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def parse_utc(value: Any, default_tz: str = "America/New_York") -> Optional[datetime]:
    """Parse a timestamp into timezone-aware UTC. Naive values assume default_tz."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=ZoneInfo(default_tz)).astimezone(UTC)
        return value.astimezone(UTC)
    text = str(value).strip()
    if not text:
        return None
    # Support trailing Z and offset forms.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                dt = None  # type: ignore[assignment]
        if dt is None:
            raise ValueError(f"Unrecognised datetime: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(default_tz))
    return dt.astimezone(UTC)


def parse_numeric(value: Any) -> Optional[float]:
    """Parse numbers with %, K/M/B suffixes, commas, and currency noise."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text.lower() in ("null", "none", "nan", "-", "n/a"):
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    text = text.replace(",", "")
    mult = 1.0
    if text and text[-1].upper() in ("K", "M", "B"):
        mult = {"K": 1e3, "M": 1e6, "B": 1e9}[text[-1].upper()]
        text = text[:-1].strip()
    for ch in ("%", "$", "€", "£", "¥", " "):
        text = text.replace(ch, "")
    try:
        val = float(text) * mult
        return -val if negative else val
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    return parse_numeric(value)


def normalize_event(raw: dict[str, Any], *, source: str = "",
                    retrieved_at: Optional[datetime] = None) -> EconomicEvent:
    """Normalize heterogeneous calendar rows into EconomicEvent."""
    eid = str(raw.get("event_id") or raw.get("id") or "").strip()
    name = str(raw.get("event_name") or raw.get("name") or raw.get("title") or "").strip()
    if not eid:
        sched = raw.get("scheduled_time_utc") or raw.get("datetime") or raw.get("date") or ""
        eid = f"{name}|{sched}|{raw.get('currency', 'USD')}"
    importance = str(raw.get("importance") or raw.get("impact") or "low").strip().lower()
    # Common provider aliases
    importance = {"red": "high", "orange": "medium", "yellow": "low",
                  "3": "high", "2": "medium", "1": "low"}.get(importance, importance)
    status = str(raw.get("status") or "scheduled").strip().lower()
    # Never invent actual from forecast.
    actual = _to_float(raw.get("actual"))
    forecast = _to_float(raw.get("forecast"))
    previous = _to_float(raw.get("previous"))
    scheduled = parse_utc(
        raw.get("scheduled_time_utc") or raw.get("datetime") or raw.get("date"),
        default_tz=str(raw.get("timezone") or "America/New_York"),
    )
    actual_release = parse_utc(raw.get("actual_release_time_utc"))
    if actual is not None and status == "scheduled":
        status = "released"
    currency = str(raw.get("currency") or "USD").upper()
    if scheduled is None and importance == "high" and currency == "USD" and name:
        import logging
        logging.getLogger("gold_news.feeds").warning(
            "High-impact USD calendar row missing scheduled time — will block entries (%s)",
            name,
        )
    return EconomicEvent(
        event_id=eid,
        event_name=name,
        currency=currency,
        importance=importance,
        scheduled_time_utc=scheduled,
        forecast=forecast,
        previous=previous,
        actual=actual,
        status=status,
        source=source or str(raw.get("source") or ""),
        # Prefer explicit timestamps. Do NOT stamp datetime.now() for file/history
        # rows — that hides events in point-in-time backtests. Missing → None
        # (events_known_as_of treats unknown retrieval as visible; actuals still gated).
        retrieved_at_utc=parse_utc(raw.get("retrieved_at_utc")) or retrieved_at,
        actual_release_time_utc=actual_release,
    )


def load_calendar_csv(path: str | Path) -> list[EconomicEvent]:
    path = Path(path)
    if not path.exists():
        return []
    events: list[EconomicEvent] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            events.append(normalize_event(row, source=str(path)))
    return sort_events(events)


def load_calendar_json(path: str | Path) -> list[EconomicEvent]:
    path = Path(path)
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload if isinstance(payload, list) else payload.get("events") or []
    return sort_events([
        normalize_event(r, source=str(path)) for r in rows
    ])


def load_calendar(path: str | Path) -> list[EconomicEvent]:
    path = Path(path)
    if path.suffix.lower() == ".json":
        return load_calendar_json(path)
    return load_calendar_csv(path)


def sort_events(events: list[EconomicEvent]) -> list[EconomicEvent]:
    return sorted(
        events,
        key=lambda e: e.scheduled_time_utc or datetime.min.replace(tzinfo=UTC),
    )


def merge_event_updates(existing: list[EconomicEvent],
                        updates: list[EconomicEvent]) -> list[EconomicEvent]:
    """Upsert by event_id so scheduled → released → revised stay on one ID."""
    by_id = {e.event_id: e for e in existing}
    for upd in updates:
        prev = by_id.get(upd.event_id)
        if prev is None:
            by_id[upd.event_id] = upd
            continue
        # Preserve earlier actual_release_time if already known.
        if prev.actual_release_time_utc and not upd.actual_release_time_utc:
            upd.actual_release_time_utc = prev.actual_release_time_utc
        # Never overwrite a known actual with None, and never fill actual from forecast.
        if upd.actual is None and prev.actual is not None:
            upd.actual = prev.actual
        by_id[upd.event_id] = upd
    return sort_events(list(by_id.values()))


def events_known_as_of(
    events: list[EconomicEvent],
    as_of_utc: datetime,
    *,
    strict_retrieval: bool = False,
) -> list[EconomicEvent]:
    """
    Point-in-time calendar view for backtests.

    - An event is visible if retrieved_at_utc <= as_of.
    - When ``strict_retrieval`` is True (research-grade historical backtests),
      events without ``retrieved_at_utc`` are excluded (unknown schedule
      knowledge is not point-in-time evidence). Actuals require a known
      ``actual_release_time_utc`` <= as_of — scheduled time is NOT a release proxy.
    - When ``strict_retrieval`` is False (live / wiring tests), unknown
      retrieval is treated as visible; actuals gated by release time or
      scheduled time for released status.
    """
    as_of = as_of_utc if as_of_utc.tzinfo else as_of_utc.replace(tzinfo=UTC)
    as_of = as_of.astimezone(UTC)
    out: list[EconomicEvent] = []
    for e in events:
        if strict_retrieval and e.retrieved_at_utc is None:
            # Research PIT: no schedule-publication timestamp → not usable
            continue
        if e.retrieved_at_utc and e.retrieved_at_utc.astimezone(UTC) > as_of:
            continue
        clone = EconomicEvent(**asdict(e))
        if strict_retrieval:
            # Only demonstrable release timestamps unlock actuals.
            release_gate = e.actual_release_time_utc
        else:
            release_gate = e.actual_release_time_utc or (
                e.scheduled_time_utc if e.status in ("released", "revised") else None
            )
        if clone.actual is not None:
            if release_gate is None or release_gate.astimezone(UTC) > as_of:
                clone.actual = None
                if clone.status in ("released", "revised"):
                    clone.status = "scheduled"
        out.append(clone)
    return out

# --- market data ---
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

UTC = timezone.utc


@dataclass
class MarketObservation:
    timestamp_utc: datetime
    gold_bid: Optional[float] = None
    gold_ask: Optional[float] = None
    dxy: Optional[float] = None
    us10y_yield_pct: Optional[float] = None
    us10y_real_yield_pct: Optional[float] = None
    usd_inr: Optional[float] = None
    gold_mid: Optional[float] = None
    source: str = ""

    def __post_init__(self) -> None:
        if self.timestamp_utc.tzinfo is None:
            self.timestamp_utc = self.timestamp_utc.replace(tzinfo=UTC)
        else:
            self.timestamp_utc = self.timestamp_utc.astimezone(UTC)
        if self.gold_mid is None and self.gold_bid is not None and self.gold_ask is not None:
            self.gold_mid = (self.gold_bid + self.gold_ask) / 2.0
        elif self.gold_mid is None and self.gold_bid is not None:
            self.gold_mid = self.gold_bid
        elif self.gold_mid is None and self.gold_ask is not None:
            self.gold_mid = self.gold_ask


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def validate_observation(obs: MarketObservation) -> bool:
    """Reject observations without a valid timestamp."""
    try:
        ensure_utc(obs.timestamp_utc)
    except Exception:
        return False
    return True


def sort_observations(rows: list[MarketObservation]) -> list[MarketObservation]:
    valid = [r for r in rows if validate_observation(r)]
    return sorted(valid, key=lambda r: r.timestamp_utc)


def candles_to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Build an ascending OHLCV frame indexed by UTC timestamps."""
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce")
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["timestamp_utc", "open", "high", "low", "close"])
    df = df.set_index("timestamp_utc").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df[["open", "high", "low", "close"] + (["volume"] if "volume" in df.columns else [])]


def load_ohlcv_csv(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.read_csv(path)
    # Accept common column aliases
    rename = {}
    for src, dst in (
        ("timestamp", "timestamp_utc"), ("time", "timestamp_utc"),
        ("datetime", "timestamp_utc"), ("date", "timestamp_utc"),
        ("ts", "timestamp_utc"),
    ):
        if src in df.columns and "timestamp_utc" not in df.columns:
            rename[src] = dst
    df = df.rename(columns=rename)
    rows = df.to_dict(orient="records")
    return candles_to_frame(rows)


def load_series_csv(path: str | Path, value_col: str) -> pd.Series:
    """Load a timestamped single-value series as UTC-indexed floats."""
    path = Path(path)
    if not path.exists():
        return pd.Series(dtype=float)
    df = pd.read_csv(path)
    ts_col = next(
        (c for c in ("timestamp_utc", "timestamp", "time", "datetime", "date", "ts")
         if c in df.columns),
        None,
    )
    if ts_col is None or value_col not in df.columns:
        raise ValueError(f"{path}: need timestamp + {value_col} columns")
    idx = pd.to_datetime(df[ts_col], utc=True, errors="coerce")
    vals = pd.to_numeric(df[value_col], errors="coerce")
    s = pd.Series(vals.values, index=idx).dropna().sort_index()
    return s[~s.index.duplicated(keep="last")]


def resample_ohlcv(df: pd.DataFrame, minutes: int = 5) -> pd.DataFrame:
    if df.empty:
        return df
    rule = f"{int(minutes)}min"
    out = df.resample(rule, label="left", closed="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        **({"volume": "sum"} if "volume" in df.columns else {}),
    }).dropna(subset=["open", "high", "low", "close"])
    return out


def value_at_or_before(series: pd.Series, when: datetime,
                       max_age: Optional[timedelta] = None
                       ) -> tuple[Optional[float], Optional[datetime]]:
    """
    Latest observation at or before `when`. Never uses future data.

    Returns (value, observation_time). If missing or too stale -> (None, None).
    """
    if series is None or series.empty:
        return None, None
    when = ensure_utc(when)
    # Convert to pandas Timestamp for comparison
    ts = pd.Timestamp(when)
    idx = series.index[series.index <= ts]
    if len(idx) == 0:
        return None, None
    obs_time = idx[-1]
    if max_age is not None and (ts - obs_time) > pd.Timedelta(max_age):
        return None, None
    return float(series.loc[obs_time]), obs_time.to_pydatetime()


def candles_completed_as_of(candles: pd.DataFrame, when: datetime,
                            timeframe_minutes: int = 5) -> pd.DataFrame:
    """
    Return only candles whose period has fully closed by `when`.

    A 5-minute bar labelled 10:00 covers [10:00, 10:05). It is complete at 10:05.
    """
    if candles is None or candles.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    when = ensure_utc(when)
    cutoff = pd.Timestamp(when) - pd.Timedelta(minutes=timeframe_minutes)
    return candles.loc[candles.index <= cutoff].copy()


def validate_candle_frame(candles: pd.DataFrame) -> bool:
    """
    Reject missing/non-finite OHLC, impossible relationships, duplicate or
    non-monotonic timestamps.
    """
    if candles is None or candles.empty:
        return False
    need = {"open", "high", "low", "close"}
    if not need.issubset(set(candles.columns)):
        return False
    frame = candles[["open", "high", "low", "close"]]
    if frame.isna().any().any():
        return False
    try:
        import numpy as np
        if not np.isfinite(frame.to_numpy(dtype=float)).all():
            return False
    except (TypeError, ValueError):
        return False
    bad = (
        (candles["high"] < candles["low"])
        | (candles["high"] < candles["open"])
        | (candles["high"] < candles["close"])
        | (candles["low"] > candles["open"])
        | (candles["low"] > candles["close"])
    )
    if bool(bad.any()):
        return False
    # Index integrity
    if not isinstance(candles.index, pd.DatetimeIndex):
        return False
    if candles.index.has_duplicates:
        return False
    if not candles.index.is_monotonic_increasing:
        return False
    # Reject non-positive prices
    if (frame <= 0).any().any():
        return False
    # Intra-session gap detection: missing bars within a continuous cluster
    # (gaps >= 4h treated as session breaks — MCX overnight — and ignored).
    if len(candles) >= 4:
        try:
            deltas = candles.index.to_series().diff().dropna().dt.total_seconds()
            med = float(deltas.median())
            if med > 0:
                # Flag only mid-session holes (e.g. 2–4 missing 5m bars), not overnight
                bad_gaps = deltas[(deltas > med * 3.0) & (deltas < 4 * 3600.0)]
                if len(bad_gaps) > 0:
                    return False
        except Exception:  # noqa: BLE001
            return False
    return True


def build_observation_frame(
    gold: pd.DataFrame,
    dxy: pd.Series,
    yields: pd.Series,
    real_yields: Optional[pd.Series] = None,
    usdinr: Optional[pd.Series] = None,
    *,
    max_age_minutes: float = 60.0,
) -> pd.DataFrame:
    """
    Align series onto gold candle timestamps (as-of join, no look-ahead).

    Macro / FX values older than ``max_age_minutes`` relative to the gold bar
    are dropped (None) so stale prints are not silently carried forward.
    """
    if gold is None or gold.empty:
        return pd.DataFrame()
    max_age = timedelta(minutes=float(max_age_minutes))
    rows = []
    for ts, row in gold.iterrows():
        when = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        dxy_v, _ = value_at_or_before(dxy, when, max_age=max_age)
        y_v, _ = value_at_or_before(yields, when, max_age=max_age)
        ry_v, _ = (
            value_at_or_before(real_yields, when, max_age=max_age)
            if real_yields is not None else (None, None)
        )
        inr_v, _ = (
            value_at_or_before(usdinr, when, max_age=max_age)
            if usdinr is not None else (None, None)
        )
        mid = float(row["close"])
        rows.append({
            "timestamp_utc": ensure_utc(when),
            "gold_bid": mid,
            "gold_ask": mid,
            "gold_mid": mid,
            "dxy": dxy_v,
            "us10y_yield_pct": y_v,
            "us10y_real_yield_pct": ry_v,
            "usd_inr": inr_v,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": mid,
            "volume": float(row["volume"]) if "volume" in gold.columns else 0.0,
        })
    return pd.DataFrame(rows).set_index("timestamp_utc").sort_index()


def load_observations_csv(path: str | Path) -> list[MarketObservation]:
    path = Path(path)
    if not path.exists():
        return []
    out: list[MarketObservation] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            ts = row.get("timestamp_utc") or row.get("timestamp") or row.get("time")
            if not ts:
                continue
            out.append(MarketObservation(
                timestamp_utc=pd.Timestamp(ts, tz="UTC").to_pydatetime(),
                gold_bid=_f(row.get("gold_bid")),
                gold_ask=_f(row.get("gold_ask")),
                dxy=_f(row.get("dxy")),
                us10y_yield_pct=_f(row.get("us10y_yield_pct")),
                us10y_real_yield_pct=_f(row.get("us10y_real_yield_pct")),
                usd_inr=_f(row.get("usd_inr")),
                gold_mid=_f(row.get("gold_mid") or row.get("gold") or row.get("close")),
                source=str(path),
            ))
    return sort_observations(out)


def _obs_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

# --- indicators ---
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd



def percent_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous is None:
        return None
    if previous <= 0:
        return None
    return 100.0 * (current / previous - 1.0)


def yield_change_bps(current_pct: Optional[float],
                     previous_pct: Optional[float]) -> Optional[float]:
    if current_pct is None or previous_pct is None:
        return None
    return 100.0 * (current_pct - previous_pct)


def classify_direction(change: Optional[float], threshold: float) -> str:
    if change is None:
        return "unknown"
    if change >= threshold:
        return "up"
    if change <= -threshold:
        return "down"
    return "flat"


def true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(candles: pd.DataFrame, period: int = 14) -> Optional[float]:
    """
    Simple rolling average of the last `period` true-range values on completed candles.
    Returns None if insufficient data.
    """
    if candles is None or len(candles) < period + 1:
        return None
    trs: list[float] = []
    closes = candles["close"].astype(float).tolist()
    highs = candles["high"].astype(float).tolist()
    lows = candles["low"].astype(float).tolist()
    for i in range(1, len(candles)):
        trs.append(true_range(highs[i], lows[i], closes[i - 1]))
    if len(trs) < period:
        return None
    window = trs[-period:]
    value = sum(window) / period
    if value <= 0:
        return None
    return float(value)


def atr_series(candles: pd.DataFrame, period: int = 14) -> pd.Series:
    if candles is None or candles.empty or len(candles) < period + 1:
        return pd.Series(dtype=float)
    tr = pd.Series(index=candles.index, dtype=float)
    prev_close = candles["close"].shift(1)
    tr = pd.concat([
        candles["high"] - candles["low"],
        (candles["high"] - prev_close).abs(),
        (candles["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def series_change_over_window(
    series: pd.Series,
    now_utc: datetime,
    window_minutes: int,
    max_age_minutes: int = 5,
    change_fn=percent_change,
) -> Optional[float]:
    """
    Compare value now with value ~window_minutes earlier.

    Both observations must be within max_age_minutes of their intended timestamps.
    """
    now_utc = ensure_utc(now_utc)
    max_age = timedelta(minutes=max_age_minutes)
    current, cur_t = value_at_or_before(series, now_utc, max_age=max_age)
    earlier_target = now_utc - timedelta(minutes=window_minutes)
    previous, prev_t = value_at_or_before(series, earlier_target, max_age=max_age)
    if current is None or previous is None:
        return None
    # Also reject if the gap between the two samples is wildly off the window.
    if cur_t is not None and prev_t is not None:
        span = (ensure_utc(cur_t) - ensure_utc(prev_t)).total_seconds() / 60.0
        if span < window_minutes * 0.5 or span > window_minutes * 1.5:
            return None
    return change_fn(current, previous)


def dxy_change_pct(series: pd.Series, now_utc: datetime,
                   window_minutes: int = 15, stale_minutes: int = 5) -> Optional[float]:
    return series_change_over_window(
        series, now_utc, window_minutes, stale_minutes, change_fn=percent_change,
    )


def yield_change_over_window(series: pd.Series, now_utc: datetime,
                             window_minutes: int = 15,
                             stale_minutes: int = 5) -> Optional[float]:
    return series_change_over_window(
        series, now_utc, window_minutes, stale_minutes, change_fn=yield_change_bps,
    )


def news_reaction_change(
    series: pd.Series,
    pre_release_utc: datetime,
    now_utc: datetime,
    stale_minutes: int = 5,
    change_fn=percent_change,
) -> Optional[float]:
    """Measure from last valid pre-release observation to latest valid observation."""
    max_age = timedelta(minutes=stale_minutes)
    previous, _ = value_at_or_before(series, ensure_utc(pre_release_utc), max_age=max_age)
    current, _ = value_at_or_before(series, ensure_utc(now_utc), max_age=max_age)
    return change_fn(current, previous)

# --- external macros ---
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from config import SAMPLE_DATA_DIR

log = logging.getLogger("gold_news.external")
UTC = timezone.utc

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
DEFAULT_DXY_CACHE = SAMPLE_DATA_DIR / "dxy_yahoo.csv"
DEFAULT_YIELD_CACHE = SAMPLE_DATA_DIR / "us10y_yahoo.csv"


def load_dxy(path: str = "") -> pd.Series:
    if path:
        for col in ("dxy", "close", "value", "adj_close"):
            try:
                return load_series_csv(path, col)
            except ValueError:
                continue
        raise ValueError(f"Could not find a DXY value column in {path}")
    return pd.Series(dtype=float)


def load_yield(path: str = "", column: str = "us10y_yield_pct") -> pd.Series:
    if path:
        for col in (column, "yield", "close", "value", "adj_close"):
            try:
                return load_series_csv(path, col)
            except ValueError:
                continue
        raise ValueError(f"Could not find a yield column in {path}")
    return pd.Series(dtype=float)


def fetch_yahoo_intraday(symbol: str, range_: str = "5d",
                         interval: str = "5m") -> pd.Series:
    """Best-effort Yahoo chart pull. Returns empty series on failure."""
    url = YAHOO_CHART.format(symbol=symbol)
    params = {"range": range_, "interval": interval, "includePrePost": "false"}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; gold-trade-engine/1.0)"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=20)
        resp.raise_for_status()
        payload = resp.json()
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            return pd.Series(dtype=float)
        block = result[0]
        timestamps = block.get("timestamp") or []
        closes = (((block.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
        rows = []
        for ts, px in zip(timestamps, closes):
            if px is None:
                continue
            rows.append((datetime.fromtimestamp(int(ts), tz=UTC), float(px)))
        if not rows:
            return pd.Series(dtype=float)
        idx, vals = zip(*rows)
        s = pd.Series(vals, index=pd.DatetimeIndex(idx, tz="UTC")).sort_index()
        return s[~s.index.duplicated(keep="last")]
    except Exception as exc:  # noqa: BLE001
        log.warning("Yahoo fetch failed for %s: %s", symbol, exc)
        return pd.Series(dtype=float)


def fetch_dxy_yahoo(range_: str = "5d") -> pd.Series:
    return fetch_yahoo_intraday("DX-Y.NYB", range_=range_, interval="5m")


def normalize_tnx_yield(series: pd.Series) -> pd.Series:
    """
    Yahoo ^TNX is usually a percent yield (e.g. 4.25). Older feeds sometimes
    quote tenths (42.5). Auto-detect: median > 20 → divide by 10.
    """
    if series is None or series.empty:
        return series
    med = float(series.median())
    if med > 20.0:
        log.info("TNX median=%.3f looks like tenths-scale — converting to percent", med)
        return series / 10.0
    return series


def fetch_us10y_yahoo(range_: str = "5d") -> pd.Series:
    return normalize_tnx_yield(fetch_yahoo_intraday("^TNX", range_=range_, interval="5m"))


def _save_series_csv(series: pd.Series, path: Path, value_col: str) -> None:
    if series is None or series.empty:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame({
        "timestamp_utc": series.index.tz_convert("UTC").astype(str),
        value_col: series.values,
    })
    df.to_csv(path, index=False)


def load_macros(
    dxy_csv: str = "",
    yield_csv: str = "",
    *,
    allow_yahoo: bool = True,
    persist_cache: bool = True,
) -> tuple[pd.Series, pd.Series]:
    """
    Load DXY + US10Y for the decision engine.

    Order: explicit CSV → on-disk Yahoo cache → live Yahoo fetch (if allowed).
    """
    dxy = load_dxy(dxy_csv) if dxy_csv else pd.Series(dtype=float)
    yld = load_yield(yield_csv) if yield_csv else pd.Series(dtype=float)

    if dxy.empty and DEFAULT_DXY_CACHE.exists():
        try:
            dxy = load_dxy(str(DEFAULT_DXY_CACHE))
            log.info("Loaded DXY cache (%d pts) from %s", len(dxy), DEFAULT_DXY_CACHE)
        except Exception as exc:  # noqa: BLE001
            log.warning("DXY cache unreadable: %s", exc)

    if yld.empty and DEFAULT_YIELD_CACHE.exists():
        try:
            yld = normalize_tnx_yield(load_yield(str(DEFAULT_YIELD_CACHE)))
            log.info("Loaded US10Y cache (%d pts) from %s", len(yld), DEFAULT_YIELD_CACHE)
        except Exception as exc:  # noqa: BLE001
            log.warning("US10Y cache unreadable: %s", exc)

    if allow_yahoo:
        if dxy.empty:
            log.info("Fetching DXY from Yahoo…")
            dxy = fetch_dxy_yahoo()
            if persist_cache and not dxy.empty:
                _save_series_csv(dxy, DEFAULT_DXY_CACHE, "dxy")
                log.info("Cached DXY → %s (%d pts)", DEFAULT_DXY_CACHE, len(dxy))
        if yld.empty:
            log.info("Fetching US10Y from Yahoo…")
            yld = fetch_us10y_yahoo()
            if persist_cache and not yld.empty:
                _save_series_csv(yld, DEFAULT_YIELD_CACHE, "us10y_yield_pct")
                log.info("Cached US10Y → %s (%d pts)", DEFAULT_YIELD_CACHE, len(yld))

    if not dxy.empty and not yld.empty:
        mark_macros_fresh()
    return dxy, yld


_LAST_MACRO_FETCH_UTC: Optional[datetime] = None


def mark_macros_fresh(when: Optional[datetime] = None) -> None:
    global _LAST_MACRO_FETCH_UTC
    _LAST_MACRO_FETCH_UTC = when or datetime.now(tz=UTC)


def refresh_macros(
    dxy: pd.Series,
    yields: pd.Series,
    *,
    allow_yahoo: bool = True,
    max_age_minutes: float = 30.0,
    prefer_csv: bool = False,
) -> tuple[pd.Series, pd.Series]:
    """
    Refresh Yahoo macros when observations (not just fetch attempts) are stale.

    ``prefer_csv=True`` never replaces configured CSV series with Yahoo (P0 #23).
    Freshness is based on the last observation timestamp in each series.
    """
    global _LAST_MACRO_FETCH_UTC
    if not allow_yahoo or prefer_csv:
        return dxy, yields
    now = datetime.now(tz=UTC)

    def _obs_age_min(series: pd.Series) -> Optional[float]:
        if series is None or series.empty:
            return None
        try:
            t = pd.Timestamp(series.index[-1]).to_pydatetime()
            if t.tzinfo is None:
                t = t.replace(tzinfo=UTC)
            else:
                t = t.astimezone(UTC)
            return (now - t).total_seconds() / 60.0
        except Exception:  # noqa: BLE001
            return None

    dxy_age = _obs_age_min(dxy)
    yld_age = _obs_age_min(yields)
    # Skip fetch if both observations are still within max_age
    if (
        dxy_age is not None and yld_age is not None
        and dxy_age >= -2 and yld_age >= -2
        and dxy_age < max_age_minutes and yld_age < max_age_minutes
    ):
        return dxy, yields
    # Also respect a minimum wall-clock gap between Yahoo calls
    if _LAST_MACRO_FETCH_UTC is not None:
        since = (now - _LAST_MACRO_FETCH_UTC).total_seconds() / 60.0
        if since < min(5.0, max_age_minutes):
            return dxy, yields

    fresh_dxy = fetch_dxy_yahoo()
    fresh_yld = fetch_us10y_yahoo()
    _LAST_MACRO_FETCH_UTC = now
    if not fresh_dxy.empty:
        dxy = fresh_dxy
        _save_series_csv(dxy, DEFAULT_DXY_CACHE, "dxy")
        log.info("Refreshed DXY (%d pts, last=%s)", len(dxy), dxy.index[-1])
    if not fresh_yld.empty:
        yields = fresh_yld
        _save_series_csv(yields, DEFAULT_YIELD_CACHE, "us10y_yield_pct")
        log.info("Refreshed US10Y (%d pts, last=%s)", len(yields), yields.index[-1])
    return dxy, yields


# Back-compat alias used by older call sites.
def maybe_fetch_macros(dxy_csv: str = "", yield_csv: str = "",
                       allow_yahoo: bool = False
                       ) -> tuple[pd.Series, pd.Series]:
    return load_macros(dxy_csv, yield_csv, allow_yahoo=allow_yahoo)

# --- MCX ---
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd



@dataclass
class McxContext:
    symbol: str
    instrument_key: str
    trading_symbol: str
    expiry: Optional[datetime]
    lot_size: int
    tick_size: float
    value_per_price_unit: float
    contract_multiplier_note: str


def mcx_context_from_instrument(row: dict, symbol: str) -> McxContext:
    sym = symbol.upper().strip()
    if sym not in MCX_VALUE_PER_PRICE_UNIT:
        raise ValueError(
            f"Unsupported / unverified gold symbol '{symbol}'. "
            f"Verified: {sorted(MCX_VALUE_PER_PRICE_UNIT)}"
        )
    key = str(row.get("instrument_key", "") or "")
    if not key.startswith("MCX_FO|"):
        raise ValueError(f"Instrument key not MCX_FO: {key!r}")
    if str(row.get("exchange") or "MCX").upper() not in ("MCX", ""):
        raise ValueError(f"Exchange not MCX: {row.get('exchange')}")
    exp = None
    if row.get("expiry"):
        from datetime import timezone
        exp = datetime.fromtimestamp(row["expiry"] / 1000, tz=timezone.utc)
    lot = int(row.get("lot_size") or 0)
    tick = float(row.get("tick_size") or 0)
    if lot <= 0 or tick <= 0:
        raise ValueError(f"Invalid lot_size/tick_size for {key}: lot={lot} tick={tick}")
    return McxContext(
        symbol=sym,
        instrument_key=key,
        trading_symbol=str(row.get("trading_symbol", "")),
        expiry=exp,
        lot_size=lot,
        tick_size=tick,
        value_per_price_unit=float(MCX_VALUE_PER_PRICE_UNIT[sym]),
        contract_multiplier_note=(
            "MCX gold is quoted in INR per 10 grams. "
            "Broker commodity quantity = number of lots (not lot_size units). "
            "value_per_price_unit is INR P&L per 1 point per lot."
        ),
    )


def implied_inr_gold_per_10g(xauusd: float, usdinr: float) -> float:
    """
    Rough international parity estimate only (not a tradeable MCX price).

    XAUUSD is USD/oz; 1 oz ≈ 31.1034768 g → per 10g ≈ oz_price / 3.11034768 * USDINR.
    """
    if xauusd is None or usdinr is None or xauusd <= 0 or usdinr <= 0:
        raise ValueError("xauusd and usdinr must be positive")
    return (xauusd / 3.11034768) * usdinr


def mcx_vs_parity_basis(
    mcx_price: float,
    xauusd: Optional[float],
    usdinr: Optional[float],
) -> Optional[float]:
    """MCX premium/discount versus rough parity (MCX - implied), or None if unavailable."""
    if xauusd is None or usdinr is None:
        return None
    try:
        implied = implied_inr_gold_per_10g(xauusd, usdinr)
    except ValueError:
        return None
    return mcx_price - implied


def confirm_mcx_breakout(
    mcx_candles: pd.DataFrame,
    international_bias: str,
    lookback: int = 6,
) -> str:
    """
    Confirm the international bias on MCX candles themselves.

    The international gold signal is an input; the MCX trade requires MCX confirmation.
    """
    from strategy import get_price_confirmation
    return get_price_confirmation(mcx_candles, international_bias, lookback=lookback)


def aligned_usdinr_at(usdinr: pd.Series, when: datetime,
                      stale_minutes: int = 5) -> Optional[float]:
    from datetime import timedelta
    value, _ = value_at_or_before(usdinr, when, max_age=timedelta(minutes=stale_minutes))
    return value

# --- calendar_feeds/official.py ---
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class OfficialSource:
    name: str
    what: str
    url: str


OFFICIAL_SOURCES: list[OfficialSource] = [
    OfficialSource(
        name="BLS",
        what="CPI, employment, unemployment, PPI release schedules and reports",
        url="https://www.bls.gov/schedule/news_release/",
    ),
    OfficialSource(
        name="BEA",
        what="PCE inflation, personal income and GDP",
        url="https://www.bea.gov/news/schedule",
    ),
    OfficialSource(
        name="Federal Reserve",
        what="FOMC meetings, policy announcements",
        url="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    ),
    OfficialSource(
        name="Federal Reserve JSON calendar",
        what="Machine-readable Fed event calendar (verification aid)",
        url="https://www.federalreserve.gov/json/calendar.json",
    ),
    OfficialSource(
        name="US Census Bureau",
        what="Retail sales, durable goods and other indicators",
        url="https://www.census.gov/economic-indicators/",
    ),
]


# Human-readable reference only — do NOT scrape without permission.
REFERENCE_ONLY = [
    OfficialSource(
        name="Forex Factory",
        what="Manual viewing of event times / impact (scraping prohibited without permission)",
        url="https://www.forexfactory.com/calendar",
    ),
    OfficialSource(
        name="Investing.com",
        what="Manual viewing of scheduled releases (prefer licensed API)",
        url="https://www.investing.com/economic-calendar/",
    ),
]


def official_links_markdown() -> str:
    lines = ["## Official verification sources", ""]
    for s in OFFICIAL_SOURCES:
        lines.append(f"- **{s.name}**: {s.what} — {s.url}")
    lines += ["", "## Reference only (no automated scrape)", ""]
    for s in REFERENCE_ONLY:
        lines.append(f"- **{s.name}**: {s.what} — {s.url}")
    return "\n".join(lines)


def find_source(name: str) -> Optional[OfficialSource]:
    key = name.lower()
    for s in OFFICIAL_SOURCES + REFERENCE_ONLY:
        if s.name.lower() == key:
            return s
    return None

# --- calendar_feeds/latency.py ---
import csv
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

UTC = timezone.utc


def event_log_template() -> dict[str, Any]:
    return {
        "event_id": None,
        "event_name": None,
        "provider": None,
        "scheduled_time_utc": None,
        "official_publication_time_utc": None,
        "provider_received_time_utc": None,
        "python_received_time_utc": None,
        "signal_generated_time_utc": None,
        "provider_delivery_delay_sec": None,
        "processing_delay_sec": None,
        "total_decision_delay_sec": None,
        "raw_message": None,
    }


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _sec(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
    if a is None or b is None:
        return None
    return (a.astimezone(UTC) - b.astimezone(UTC)).total_seconds()


@dataclass
class EventLatencyLog:
    """Append-only latency log under gold-trade-engine/sample_data/."""

    path: Path
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, row: dict[str, Any]) -> dict[str, Any]:
        out = event_log_template()
        out.update(row)
        now = datetime.now(tz=UTC)

        if out.get("python_received_time_utc") is None:
            out["python_received_time_utc"] = now.isoformat()

        official = _parse_dt(out.get("official_publication_time_utc"))
        provider_rx = _parse_dt(out.get("provider_received_time_utc"))
        python_rx = _parse_dt(out.get("python_received_time_utc"))
        signal_t = _parse_dt(out.get("signal_generated_time_utc"))

        # Prefer official publication; fall back to scheduled time for delay maths.
        pub = official or _parse_dt(out.get("scheduled_time_utc"))
        out["provider_delivery_delay_sec"] = _sec(python_rx, pub)
        out["processing_delay_sec"] = _sec(signal_t, python_rx)
        out["total_decision_delay_sec"] = _sec(signal_t, pub)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            write_header = not self.path.exists()
            with self.path.open("a", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(event_log_template().keys()))
                if write_header:
                    writer.writeheader()
                # Keep raw_message compact for CSV
                row_out = dict(out)
                raw = row_out.get("raw_message")
                if isinstance(raw, (dict, list)):
                    row_out["raw_message"] = json.dumps(raw, default=str)[:2000]
                writer.writerow(row_out)
        return out

# --- trading economics ---
import json
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import quote

import requests


log = logging.getLogger("gold_news.calendar.te")
UTC = timezone.utc

TE_REST = "https://api.tradingeconomics.com"
TE_STREAM = "wss://stream.tradingeconomics.com/"



def _importance(value: Any) -> str:
    if value is None or value == "":
        return "low"
    if isinstance(value, (int, float)):
        return {1: "low", 2: "medium", 3: "high"}.get(int(value), "low")
    text = str(value).strip().lower()
    return {"1": "low", "2": "medium", "3": "high",
            "low": "low", "medium": "medium", "high": "high"}.get(text, "low")


def map_te_row(raw: dict[str, Any], *, retrieved_at: Optional[datetime] = None,
               stream: bool = False) -> EconomicEvent:
    """Map TE REST/stream payload → EconomicEvent (never fill actual from forecast)."""
    retrieved_at = retrieved_at or datetime.now(tz=UTC)
    calendar_id = str(raw.get("CalendarId") or raw.get("calendarId")
                      or raw.get("CalendarID") or "").strip()
    name = str(raw.get("Event") or raw.get("event") or raw.get("Category")
               or raw.get("category") or "").strip()
    country = str(raw.get("Country") or raw.get("country") or "").strip()
    currency = "USD" if country.lower() in ("united states", "united states of america", "us", "usa") else ""
    # TE sometimes ships ISO currency; prefer explicit currency when present and USD-related
    cur_field = str(raw.get("Currency") or raw.get("currency") or "").upper()
    if cur_field in ("USD", "US$"):
        currency = "USD"
    if not currency and country:
        currency = country[:3].upper() if len(country) == 3 else "USD"

    scheduled = parse_utc(
        raw.get("Date") or raw.get("date") or raw.get("DateUtc"),
        default_tz="UTC",
    )
    actual = parse_numeric(raw.get("Actual") if "Actual" in raw else raw.get("actual"))
    forecast = parse_numeric(
        raw.get("Forecast") if "Forecast" in raw else raw.get("forecast")
        or raw.get("forecast ")  # TE stream example had a trailing space key
    )
    previous = parse_numeric(raw.get("Previous") if "Previous" in raw else raw.get("previous"))
    eid = calendar_id or f"TE|{name}|{scheduled}|{country}"

    status = "scheduled"
    actual_release = None
    if actual is not None:
        status = "released"
        # On stream, receipt ≈ first knowledge of actual
        actual_release = retrieved_at if stream else (scheduled or retrieved_at)
    revised = raw.get("Revised") if "Revised" in raw else raw.get("revised")
    if revised not in (None, "", "null", "None") and actual is not None:
        status = "revised"

    ev = normalize_event({
        "event_id": eid,
        "event_name": name,
        "currency": currency or "USD",
        "importance": _importance(raw.get("Importance") if "Importance" in raw else raw.get("importance")),
        "scheduled_time_utc": scheduled,
        "forecast": forecast,
        "previous": previous,
        "actual": actual,
        "status": status,
        "actual_release_time_utc": actual_release,
        "source": "trading_economics",
        "retrieved_at_utc": retrieved_at,
    }, source="trading_economics", retrieved_at=retrieved_at)
    return ev


class TradingEconomicsClient:
    """REST snapshot + optional WebSocket calendar stream."""

    def __init__(self, key: str, secret: str = "", *, timeout: float = 20.0):
        self.key = (key or "").strip()
        self.secret = (secret or "").strip()
        self.timeout = timeout
        self._ws_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        if ":" in self.key and not self.secret:
            self.key, self.secret = self.key.split(":", 1)

    @property
    def client_param(self) -> str:
        if self.secret:
            return f"{self.key}:{self.secret}"
        return self.key

    def configured(self) -> bool:
        return bool(self.key)

    def fetch_calendar(self, start: date, end: date,
                       country: str = "united states") -> list[EconomicEvent]:
        if not self.configured():
            return []
        path = (
            f"/calendar/country/{quote(country)}"
            f"/{start.isoformat()}/{end.isoformat()}"
        )
        url = f"{TE_REST}{path}"
        params = {"c": self.client_param}
        retrieved = datetime.now(tz=UTC)
        try:
            resp = requests.get(url, params=params, timeout=self.timeout,
                                headers={"Accept": "application/json"})
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("TE REST calendar failed: %s", exc)
            return []
        rows = payload if isinstance(payload, list) else []
        events = [map_te_row(r, retrieved_at=retrieved) for r in rows if isinstance(r, dict)]
        # Keep US / USD high-signal set; TE uses country name
        out = [e for e in events if e.currency == "USD"
               or "united states" in str(e.event_name).lower()]
        # Prefer currency filter; if TE left currency blank, keep United States rows via source map
        if not out:
            out = events
        log.info("TE REST returned %d events (%s → %s)", len(out), start, end)
        return out

    def start_stream(self, on_event: Callable[[EconomicEvent, dict], None],
                     *, reconnect: bool = True) -> bool:
        """
        Start background WebSocket calendar stream.

        Requires optional dependency: websocket-client
            pip install websocket-client
        """
        if not self.configured():
            log.warning("TE stream not started — missing credentials")
            return False
        try:
            import websocket  # type: ignore
        except ImportError:
            log.warning(
                "websocket-client not installed — TE stream disabled. "
                "Install with: pip install websocket-client"
            )
            return False

        if self._ws_thread and self._ws_thread.is_alive():
            return True

        self._stop.clear()
        client = self.client_param

        def _run() -> None:
            url = f"{TE_STREAM}?client={client}"
            while not self._stop.is_set():
                try:
                    ws = websocket.WebSocketApp(
                        url,
                        on_open=lambda w: w.send(
                            json.dumps({"topic": "subscribe", "to": "calendar"})
                        ),
                        on_message=lambda w, msg: self._handle_message(msg, on_event),
                        on_error=lambda w, err: log.warning("TE WS error: %s", err),
                        on_close=lambda w, *a: log.info("TE WS closed"),
                    )
                    ws.run_forever(ping_interval=20, ping_timeout=10)
                except Exception as exc:  # noqa: BLE001
                    log.warning("TE WS loop error: %s", exc)
                if not reconnect or self._stop.is_set():
                    break
                time.sleep(5)

        self._ws_thread = threading.Thread(target=_run, name="te-calendar-ws", daemon=True)
        self._ws_thread.start()
        log.info("TE calendar WebSocket stream starting")
        return True

    def stop_stream(self) -> None:
        self._stop.set()

    def _handle_message(self, message: str,
                        on_event: Callable[[EconomicEvent, dict], None]) -> None:
        received = datetime.now(tz=UTC)
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        rows = payload if isinstance(payload, list) else [payload]
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("topic", "calendar")).lower() not in ("calendar", ""):
                # still accept calendar-shaped messages without topic
                if "event" not in raw and "Event" not in raw:
                    continue
            country = str(raw.get("country") or raw.get("Country") or "").lower()
            if country and country not in ("united states", "united states of america", "us", "usa"):
                continue
            ev = map_te_row(raw, retrieved_at=received, stream=True)
            try:
                on_event(ev, raw)
            except Exception as exc:  # noqa: BLE001
                log.exception("TE stream callback failed: %s", exc)

# --- fmp ---
import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

import requests


log = logging.getLogger("gold_news.calendar.fmp")
UTC = timezone.utc

# Prefer stable endpoint; fall back to legacy v3 path if needed.
FMP_STABLE = "https://financialmodelingprep.com/stable/economic-calendar"
FMP_V3 = "https://financialmodelingprep.com/api/v3/economic_calendar"


def _impact(value: Any) -> str:
    text = str(value or "").strip().lower()
    return {
        "high": "high", "medium": "medium", "low": "low",
        "3": "high", "2": "medium", "1": "low",
    }.get(text, text if text in ("high", "medium", "low") else "low")


def map_fmp_row(raw: dict[str, Any], *, retrieved_at: Optional[datetime] = None
                ) -> EconomicEvent:
    retrieved_at = retrieved_at or datetime.now(tz=UTC)
    name = str(raw.get("event") or raw.get("eventName") or "").strip()
    currency = str(raw.get("currency") or "").upper() or "USD"
    country = str(raw.get("country") or "").upper()
    if country in ("US", "USA") and not currency:
        currency = "USD"
    scheduled = parse_utc(raw.get("date") or raw.get("datetime"), default_tz="UTC")
    actual = parse_numeric(raw.get("actual"))
    forecast = parse_numeric(
        raw.get("estimate") if raw.get("estimate") is not None else raw.get("forecast"),
    )
    previous = parse_numeric(raw.get("previous"))
    eid = str(raw.get("event_id") or f"FMP|{name}|{scheduled}|{currency}")
    status = "scheduled"
    actual_release = None
    if actual is not None and actual != "":
        status = "released"
        actual_release = scheduled or retrieved_at
    return normalize_event({
        "event_id": eid,
        "event_name": name,
        "currency": currency,
        "importance": _impact(raw.get("impact") or raw.get("importance")),
        "scheduled_time_utc": scheduled,
        "forecast": forecast,
        "previous": previous,
        "actual": actual,
        "status": status,
        "actual_release_time_utc": actual_release,
        "source": "fmp",
        "retrieved_at_utc": retrieved_at,
    }, source="fmp", retrieved_at=retrieved_at)


class FMPCalendarClient:
    def __init__(self, api_key: str, *, timeout: float = 20.0):
        self.api_key = (api_key or "").strip()
        self.timeout = timeout

    def configured(self) -> bool:
        return bool(self.api_key)

    def fetch_calendar(self, start: date, end: date) -> list[EconomicEvent]:
        if not self.configured():
            return []
        retrieved = datetime.now(tz=UTC)
        params = {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "apikey": self.api_key,
        }
        payload = None
        last_err: Optional[Exception] = None
        for url in (FMP_STABLE, FMP_V3):
            try:
                resp = requests.get(url, params=params, timeout=self.timeout,
                                    headers={"Accept": "application/json"})
                resp.raise_for_status()
                payload = resp.json()
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                continue
        if payload is None:
            log.warning("FMP calendar failed: %s", last_err)
            return []
        rows = payload if isinstance(payload, list) else payload.get("economicCalendar") or []
        events = [
            map_fmp_row(r, retrieved_at=retrieved)
            for r in rows if isinstance(r, dict)
        ]
        usd = [e for e in events if e.currency == "USD"]
        log.info("FMP REST returned %d USD events (%s → %s)", len(usd), start, end)
        return usd

# --- calendar feed manager ---
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from config import ENV_FILE, SAMPLE_DATA_DIR
from upstox_client import load_env_file

log = logging.getLogger("gold_news.calendar.manager")
UTC = timezone.utc

LIVE_CALENDAR_PATH = SAMPLE_DATA_DIR / "calendar_live.json"
LATENCY_LOG_PATH = SAMPLE_DATA_DIR / "event_latency.csv"


def _env_creds() -> dict[str, str]:
    """Read calendar API keys from repo-root env.txt (+ process env fallback)."""
    file_vals = load_env_file(str(ENV_FILE))
    merged = dict(os.environ)
    merged.update({k: v for k, v in file_vals.items() if v})
    return {
        "te_key": (
            merged.get("TRADING_ECONOMICS_KEY", "")
            or merged.get("TE_API_KEY", "")
            or ""
        ).strip(),
        "te_secret": (
            merged.get("TRADING_ECONOMICS_SECRET", "")
            or merged.get("TE_API_SECRET", "")
            or ""
        ).strip(),
        "te_client": (
            merged.get("TRADING_ECONOMICS_CLIENT", "")
            or merged.get("TE_CLIENT", "")
            or ""
        ).strip(),
        "fmp_key": (
            merged.get("FMP_API_KEY", "")
            or merged.get("FINANCIAL_MODELING_PREP_API_KEY", "")
            or ""
        ).strip(),
    }


class CalendarFeedManager:
    """
    Startup:
        load_calendar()  → CSV/JSON baseline + licensed API snapshot
        connect_calendar_stream() when TE credentials present

    Continuously (called from the main loop):
        process_new_calendar_events()
        refresh schedule on the 6–12h cadence
        near-release poll when a high-impact USD event is close
        record latency when actuals arrive
    """

    def __init__(
        self,
        *,
        local_calendar_path: str = "",
        primary: str = "auto",              # auto | trading_economics | fmp | csv
        secondary: str = "fmp",
        enable_stream: bool = True,
        schedule_refresh_hours: float = 6.0,
        near_release_window_minutes: float = 30.0,
        near_release_poll_seconds: float = 45.0,
        horizon_days: int = 21,
        importance_required: str = "high",
    ):
        self.local_calendar_path = local_calendar_path
        self.primary = (primary or "auto").lower()
        self.secondary = (secondary or "fmp").lower()
        self.enable_stream = enable_stream
        self.schedule_refresh_hours = max(1.0, float(schedule_refresh_hours))
        self.near_release_window_minutes = float(near_release_window_minutes)
        self.near_release_poll_seconds = max(5.0, float(near_release_poll_seconds))
        self.horizon_days = int(horizon_days)
        self.importance_required = (importance_required or "high").lower()

        creds = _env_creds()
        te_key = creds["te_client"] or creds["te_key"]
        self.te = TradingEconomicsClient(te_key, creds["te_secret"])
        self.fmp = FMPCalendarClient(creds["fmp_key"])
        self.latency = EventLatencyLog(LATENCY_LOG_PATH)

        self.events: list[EconomicEvent] = []
        self._lock = threading.RLock()
        self._last_schedule_refresh: Optional[datetime] = None
        self.last_successful_refresh: Optional[datetime] = None
        self._last_near_poll: Optional[datetime] = None
        self._new_releases: list[EconomicEvent] = []
        self._stream_started = False
        self.stats: dict[str, Any] = {
            "primary": None,
            "secondary": None,
            "stream": False,
            "schedule_refreshes": 0,
            "near_polls": 0,
            "stream_updates": 0,
            "last_refresh_healthy": False,
            "official_sources": [s.url for s in OFFICIAL_SOURCES],
        }

    # ------------------------------------------------------------------ startup

    def start(self) -> list[EconomicEvent]:
        """Load baseline + API snapshot; start stream if available."""
        self.load_calendar()
        self.refresh_schedule(force=True)
        if self.enable_stream:
            self.connect_calendar_stream()
        self.persist()
        log.info(
            "CalendarFeedManager ready: %d events | primary=%s stream=%s | %s",
            len(self.events),
            self.stats.get("primary"),
            self._stream_started,
            "TE+FMP keys from env.txt" if (self.te.configured() or self.fmp.configured())
            else "no TE/FMP keys — using local CSV only",
        )
        return list(self.events)

    def load_calendar(self) -> int:
        """Startup local load (CSV/JSON sample or prior live cache)."""
        paths = []
        if self.local_calendar_path:
            paths.append(Path(self.local_calendar_path))
        else:
            # Always fall back to bundled sample calendar when path unset/empty.
            from config import DEFAULT_CALENDAR
            paths.append(Path(DEFAULT_CALENDAR))
        if LIVE_CALENDAR_PATH.exists():
            paths.append(LIVE_CALENDAR_PATH)
        loaded: list[EconomicEvent] = []
        for path in paths:
            if path.exists():
                chunk = load_calendar(path)
                loaded = merge_event_updates(loaded, chunk)
                log.info("Loaded %d events from %s", len(chunk), path)
        with self._lock:
            self.events = sort_events(loaded)
        return len(self.events)

    def connect_calendar_stream(self) -> bool:
        if not self.te.configured():
            log.info("TE stream skipped — set TRADING_ECONOMICS_KEY/SECRET in env.txt")
            return False
        ok = self.te.start_stream(self._on_stream_event, reconnect=True)
        self._stream_started = ok
        self.stats["stream"] = ok
        return ok

    def stop(self) -> None:
        self.te.stop_stream()
        self.persist()

    # -------------------------------------------------------------- continuous

    def tick(self, now_utc: Optional[datetime] = None) -> dict[str, Any]:
        """
        Call from the main loop every cycle.

        Returns a small status dict for logging / gates.
        """
        now_utc = now_utc or datetime.now(tz=UTC)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=UTC)

        schedule_refreshed = False
        near_polled = False

        # 1) Schedule refresh every 6–12 hours
        if self._needs_schedule_refresh(now_utc):
            self.refresh_schedule(force=True)
            schedule_refreshed = True

        # 2) Near high-impact release → tighter poll (stream preferred)
        if self.high_impact_release_is_near(now_utc):
            if self._needs_near_poll(now_utc):
                self.poll_near_release(now_utc)
                near_polled = True

        new_releases = self.drain_new_releases()
        return {
            "event_count": len(self.events),
            "schedule_refreshed": schedule_refreshed,
            "near_polled": near_polled,
            "near_release": self.high_impact_release_is_near(now_utc),
            "new_releases": len(new_releases),
            "stream": self._stream_started,
            "releases": [
                {"id": e.event_id, "name": e.event_name, "actual": e.actual}
                for e in new_releases
            ],
        }

    def process_new_calendar_events(self) -> list[EconomicEvent]:
        return self.drain_new_releases()

    def high_impact_release_is_near(self, now_utc: Optional[datetime] = None) -> bool:
        now_utc = now_utc or datetime.now(tz=UTC)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=UTC)
        window = self.near_release_window_minutes
        with self._lock:
            events = list(self.events)
        for ev in events:
            if ev.currency.upper() != "USD":
                continue
            if (ev.importance or "").lower() != self.importance_required:
                continue
            if ev.scheduled_time_utc is None:
                continue
            mins = (now_utc - ev.scheduled_time_utc.astimezone(UTC)).total_seconds() / 60.0
            # Near means within ±window of scheduled time
            if -window <= mins <= window:
                return True
        return False

    def new_release_received(self) -> bool:
        with self._lock:
            return bool(self._new_releases)

    def drain_new_releases(self) -> list[EconomicEvent]:
        with self._lock:
            out = list(self._new_releases)
            self._new_releases.clear()
        return out

    def snapshot(self) -> list[EconomicEvent]:
        with self._lock:
            return list(self.events)

    # ----------------------------------------------------------------- refresh

    def refresh_schedule(self, force: bool = False) -> int:
        now = datetime.now(tz=UTC)
        if not force and not self._needs_schedule_refresh(now):
            return 0

        start = date.today() - timedelta(days=2)
        end = date.today() + timedelta(days=self.horizon_days)
        primary_events: list[EconomicEvent] = []
        secondary_events: list[EconomicEvent] = []

        provider_order = self._provider_order()
        primary_ok = False
        primary_name = ""
        for name in provider_order:
            if name == "trading_economics" and self.te.configured():
                primary_events = self.te.fetch_calendar(start, end)
                primary_name = "trading_economics"
                primary_ok = bool(primary_events)
                if primary_ok:
                    self.stats["primary"] = primary_name
                    break
                log.warning("TE calendar fetch returned 0 events — trying next provider")
                continue
            if name == "fmp" and self.fmp.configured():
                primary_events = self.fmp.fetch_calendar(start, end)
                primary_name = "fmp"
                primary_ok = bool(primary_events)
                if primary_ok:
                    self.stats["primary"] = primary_name
                    break
                log.warning("FMP calendar fetch returned 0 events — trying next provider")
                continue
            if name == "csv":
                # CSV is local seed; treat as ok only if we already have / load events
                primary_name = "csv"
                self.stats["primary"] = "csv"
                primary_ok = True  # local file path — events may already be loaded
                break

        # Secondary for comparison / fill gaps
        if self.secondary == "fmp" and self.fmp.configured() and self.stats.get("primary") != "fmp":
            secondary_events = self.fmp.fetch_calendar(start, end)
            self.stats["secondary"] = "fmp"
        elif self.secondary == "trading_economics" and self.te.configured() and self.stats.get("primary") != "trading_economics":
            secondary_events = self.te.fetch_calendar(start, end)
            self.stats["secondary"] = "trading_economics"

        with self._lock:
            if primary_events:
                merged = merge_event_updates(self.events, primary_events)
            else:
                merged = list(self.events)
            if secondary_events:
                merged = merge_event_updates(merged, secondary_events)
            self.events = merged
            count = len(self.events)

        # P0 #16: only mark successful refresh when provider returned events
        # (or CSV primary with non-empty in-memory schedule). Empty/failed
        # remote fetch must NOT reset freshness clock.
        fetch_healthy = primary_ok and (count > 0) and (
            bool(primary_events) or primary_name == "csv"
        )
        self.stats["last_refresh_healthy"] = fetch_healthy
        self.stats["last_refresh_primary"] = primary_name
        self.stats["last_refresh_event_count"] = count
        self.stats["schedule_refreshes"] = int(self.stats.get("schedule_refreshes", 0)) + 1
        if fetch_healthy:
            self._last_schedule_refresh = now
            self.last_successful_refresh = now
        else:
            log.warning(
                "Schedule refresh unhealthy: primary=%s fetched=%d total=%d — "
                "freshness clock NOT updated",
                primary_name, len(primary_events), count,
            )
        self.persist()
        log.info(
            "Schedule refresh: %d events (primary=%s secondary=%s healthy=%s)",
            count, self.stats.get("primary"), self.stats.get("secondary"), fetch_healthy,
        )
        return count

    def poll_near_release(self, now_utc: Optional[datetime] = None) -> int:
        """
        Tighter REST poll around a high-impact window when stream is unavailable
        or as a backup. Respects a minimum interval (default 45s).
        """
        now_utc = now_utc or datetime.now(tz=UTC)
        start = date.today() - timedelta(days=1)
        end = date.today() + timedelta(days=2)
        updates: list[EconomicEvent] = []
        if self.te.configured():
            updates = self.te.fetch_calendar(start, end)
        elif self.fmp.configured():
            updates = self.fmp.fetch_calendar(start, end)
        if not updates:
            self._last_near_poll = now_utc
            return 0

        before = {e.event_id: e.actual for e in self.snapshot()}
        with self._lock:
            self.events = merge_event_updates(self.events, updates)
            for ev in updates:
                prev_actual = before.get(ev.event_id)
                if ev.actual is not None and prev_actual is None:
                    self._new_releases.append(ev)
                    self._log_release(ev, provider=ev.source or "rest_poll")
        self._last_near_poll = now_utc
        self.stats["near_polls"] = int(self.stats.get("near_polls", 0)) + 1
        self.persist()
        return len(updates)

    # ----------------------------------------------------------------- helpers

    def _provider_order(self) -> list[str]:
        if self.primary == "auto":
            order = []
            if self.te.configured():
                order.append("trading_economics")
            if self.fmp.configured():
                order.append("fmp")
            order.append("csv")
            return order
        return [self.primary, "csv"]

    def _needs_schedule_refresh(self, now: datetime) -> bool:
        if self._last_schedule_refresh is None:
            return True
        hours = (now - self._last_schedule_refresh).total_seconds() / 3600.0
        return hours >= self.schedule_refresh_hours

    def _needs_near_poll(self, now: datetime) -> bool:
        # If stream is healthy, near-poll is a slow backup (3x interval).
        min_gap = self.near_release_poll_seconds
        if self._stream_started:
            min_gap = max(min_gap, 60.0)
        if self._last_near_poll is None:
            return True
        return (now - self._last_near_poll).total_seconds() >= min_gap

    def _on_stream_event(self, ev: EconomicEvent, raw: dict) -> None:
        python_rx = datetime.now(tz=UTC)
        with self._lock:
            before = next((e for e in self.events if e.event_id == ev.event_id), None)
            prev_actual = before.actual if before else None
            self.events = merge_event_updates(self.events, [ev])
            is_new_actual = ev.actual is not None and prev_actual is None
            if is_new_actual:
                self._new_releases.append(ev)
        self.stats["stream_updates"] = int(self.stats.get("stream_updates", 0)) + 1
        if is_new_actual:
            self._log_release(
                ev,
                provider="trading_economics_stream",
                python_received=python_rx,
                raw=raw,
            )
            self.persist()
            log.info(
                "STREAM release %s actual=%s forecast=%s previous=%s",
                ev.event_name, ev.actual, ev.forecast, ev.previous,
            )

    def _log_release(
        self,
        ev: EconomicEvent,
        *,
        provider: str,
        python_received: Optional[datetime] = None,
        raw: Any = None,
    ) -> None:
        self.latency.record({
            "event_id": ev.event_id,
            "event_name": ev.event_name,
            "provider": provider,
            "scheduled_time_utc": (
                ev.scheduled_time_utc.isoformat() if ev.scheduled_time_utc else None
            ),
            "official_publication_time_utc": (
                ev.actual_release_time_utc.isoformat()
                if ev.actual_release_time_utc else None
            ),
            "provider_received_time_utc": (
                ev.retrieved_at_utc.isoformat() if ev.retrieved_at_utc else None
            ),
            "python_received_time_utc": (
                (python_received or datetime.now(tz=UTC)).isoformat()
            ),
            "signal_generated_time_utc": None,
            "raw_message": raw,
        })

    def mark_signal_time(self, event_id: str, when: Optional[datetime] = None) -> None:
        """Stamp signal_generated_time for latency maths after a decision uses a release."""
        when = when or datetime.now(tz=UTC)
        self.latency.record({
            "event_id": event_id,
            "event_name": "",
            "provider": "signal_engine",
            "signal_generated_time_utc": when.isoformat(),
            "python_received_time_utc": when.isoformat(),
        })

    def persist(self) -> None:
        with self._lock:
            rows = [e.to_dict() for e in self.events]
        LIVE_CALENDAR_PATH.parent.mkdir(parents=True, exist_ok=True)
        LIVE_CALENDAR_PATH.write_text(
            json.dumps({"events": rows, "updated_utc": datetime.now(tz=UTC).isoformat()},
                       indent=2),
            encoding="utf-8",
        )

    def describe_sources(self) -> str:
        return official_links_markdown()
