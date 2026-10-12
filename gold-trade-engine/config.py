"""Thresholds, risk settings, paths, and Upstox / MCX instrument configuration."""
from __future__ import annotations
import json
import logging
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional
log = logging.getLogger("gold_news.config")

__version__ = "1.0.0"

# gold-trade-engine/
ENGINE_DIR = Path(__file__).resolve().parent

# algo-trading/  (repository root)
ROOT_DIR = ENGINE_DIR.parent

# Sole credential file location — always the repo root.
ENV_FILE = ROOT_DIR / "env.txt"
SAMPLE_DATA_DIR = ENGINE_DIR / "sample_data"
DEFAULT_CALENDAR = SAMPLE_DATA_DIR / "calendar.csv"
DEFAULT_CONFIG_FILE = ENGINE_DIR / "config.json"
INSTRUMENT_CACHE = ENGINE_DIR / "instrument_master_cache.json"
OUTPUT_DIR = ENGINE_DIR
# Operator kill switch: create this empty file to block new orders immediately.
KILL_SWITCH_FILE = ENGINE_DIR / "KILL_SWITCH"

def ensure_engine_dirs() -> None:
    SAMPLE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Spec defaults (research parameters — not proven profitable).

# Overridden by gold-trade-engine/config.json when present.
CONFIG: dict[str, Any] = {
    # Runtime
    "mode": "paper",                 # paper | live
    "sizing_mode": "fixed",          # fixed | risk
    "fixed_lots": 1,                 # used when sizing_mode == fixed
    # Trading instrument
    # GOLDM (100g) fits ₹5L paper equity under 10% margin model; GOLD (1kg) does not.
    "gold_symbol": "GOLDM",
    "timeframe_minutes": 5,
    # News controls
    "news_block_before_minutes": 15,
    "news_block_after_minutes": 15,
    "news_importance_required": "high",
    # Market-reaction measurement
    "reaction_window_minutes": 15,
    "minimum_dxy_change_pct": 0.03,
    "minimum_yield_change_bps": 2.0,
    "minimum_real_yield_change_bps": 2.0,
    # Gold confirmation / market signal (15m trend + 5m breakout)
    "strategy_mode": "trend_breakout",  # trend_breakout | legacy_macro
    "gold_breakout_lookback_bars": 20,
    "trend_timeframe_minutes": 15,
    "trend_ema_fast": 20,
    "trend_ema_slow": 50,
    "require_news_confirmation": True,
    "news_freshness_minutes": 45.0,
    "news_db_path": "",                 # default: engine/gold_signals.db
    "flatten_before_close_minutes": 5,  # mandatory flatten before market_close
    # Embedded producers (monitor-gold + commodity-events inside main.py)
    "run_embedded_producers": True,
    "enable_monitor_producer": True,
    "enable_news_producer": True,
    "monitor_refresh_seconds": 60.0,
    "news_poll_interval_seconds": 60.0,
    "news_producer_no_telegram": False,
    # Risk management
    "risk_per_trade_pct": 0.25,
    "maximum_daily_loss_pct": 1.0,
    "maximum_open_positions": 1,
    "max_trades_per_day": 5,
    "max_consecutive_losses": 3,
    # Execution assumptions
    "maximum_spread_points": 50.0,
    "reward_risk_ratio": 2.0,
    "atr_period": 14,
    "atr_stop_multiplier": 1.5,
    "stale_data_minutes": 5,
    # Account / contract
    "initial_equity": 500_000.0,
    "value_per_price_unit": 10.0,   # GOLDM default; GOLD uses 100
    "estimated_cost_per_unit": 5.0,
    "min_stop_distance": 1.0,
    "min_position_lots": 1,
    "lot_size": 1,
    "margin_pct_of_notional": 0.10,
    # Modes
    "news_mode": "filtered",
    "use_real_yield": False,         # no live real-yield feed wired yet
    "enforce_risk_budget": True,     # fixed lots must still fit risk_per_trade_pct
    "trading_day_tz": "Asia/Kolkata",
    # Yahoo macros are research-oriented; observation timestamps gate entries.
    # Live mode additionally requires allow_yahoo_in_live=true.
    "yahoo_macros": True,
    "macro_stale_minutes": 4320,
    "macro_trade_max_age_minutes": 60,   # new entries require fresher macros
    "gold_entry_max_age_minutes": 30,
    "place_broker_stop": True,
    "require_broker_stop": True,
    "macro_refresh_minutes": 15,
    # Calendar feeds (licensed APIs — intervals per design note)
    "calendar_provider": "auto",           # auto | trading_economics | fmp | csv
    "calendar_secondary": "fmp",
    "calendar_stream": True,               # TE WebSocket for actuals when credentials present
    "calendar_schedule_refresh_hours": 6.0,
    "calendar_near_release_window_minutes": 30.0,
    "calendar_near_release_poll_seconds": 45.0,
    "calendar_horizon_days": 21,
    # Release surprise → gold bias (post news-block reaction window)
    "use_release_surprise": True,
    "surprise_reaction_minutes": 180.0,     # trade window after release (incl. block)
    "surprise_epsilon": 0.0,                # |actual-forecast| <= epsilon → inline
    "require_surprise_macro_agree": True,   # DXY/yield bias must match surprise bias
    "surprise_required_in_window": True,    # inside reaction window, no surprise → no trade
    # Backtest realism (paper path already uses bid/ask + estimated_cost_per_unit)
    "backtest_slippage_points": 1.0,        # adverse points added beyond half-spread
    "backtest_fee_per_lot": 0.0,            # extra fee layered on estimated_cost_per_unit
    # Upstox V3 products: I | D | MTF. MCX overnight/carry → D (legacy "M" rejected).
    "product": "D",
    "validity": "DAY",
    "order_tag": "GOLD_NEWS",
    "market_protection": 0,
    "rest_retry": 4,
    "rest_timeout": 15.0,
    "poll_seconds": 5.0,
    "market_open": "09:00",
    "market_close": "23:30",
    "entry_cutoff": "23:00",
    "enforce_session_hours": True,
    "require_live_calendar": True,
    "calendar_max_age_hours": 12.0,
    "live_armed": False,
    "require_broker_funds": False,
    "allow_yahoo_in_live": False,
    "instrument_master_max_age_hours": 36.0,
    "roll_days_before_expiry": 3,
    "min_days_to_expiry_for_entry": 3,  # aligned with roll_days (calendar days)
}

# MCX contract value per 1 INR move in the quoted price (INR per 10 grams).

MCX_VALUE_PER_PRICE_UNIT = {
    "GOLD": 100.0,       # 1 kg = 100 x 10g
    "GOLDM": 10.0,       # 100 g = 10 x 10g
    "GOLDPETAL": 1.0,    # 1 g
    "GOLDGUINEA": 8.0,   # 8 g
    "GOLDTEN": 10.0,     # 10 g (verify against current contract sheet)
}

def load_config_file(path: str | Path | None = None) -> dict[str, Any]:
    """Load JSON config from the engine folder (missing file → empty dict)."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_FILE
    if not cfg_path.is_absolute():
        cfg_path = DEFAULT_CONFIG_FILE.parent / cfg_path
    if not cfg_path.exists():
        log.warning("Config file not found: %s — using built-in defaults", cfg_path)
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Failed to read config file {cfg_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"Config file {cfg_path} must contain a JSON object")
    return data

@dataclass

class EngineConfig:
    """Typed view over CONFIG plus Upstox runtime settings."""
    mode: str = "paper"
    sizing_mode: str = "fixed"
    fixed_lots: int = 1
    gold_symbol: str = "GOLDM"
    timeframe_minutes: int = 5
    news_block_before_minutes: int = 15
    news_block_after_minutes: int = 15
    news_importance_required: str = "high"
    reaction_window_minutes: int = 15
    minimum_dxy_change_pct: float = 0.03
    minimum_yield_change_bps: float = 2.0
    minimum_real_yield_change_bps: float = 2.0
    strategy_mode: str = "trend_breakout"
    gold_breakout_lookback_bars: int = 20
    trend_timeframe_minutes: int = 15
    trend_ema_fast: int = 20
    trend_ema_slow: int = 50
    require_news_confirmation: bool = True
    news_freshness_minutes: float = 45.0
    news_db_path: str = ""
    flatten_before_close_minutes: int = 5
    run_embedded_producers: bool = True
    enable_monitor_producer: bool = True
    enable_news_producer: bool = True
    monitor_refresh_seconds: float = 60.0
    news_poll_interval_seconds: float = 60.0
    news_producer_no_telegram: bool = False
    risk_per_trade_pct: float = 0.25
    maximum_daily_loss_pct: float = 1.0
    maximum_open_positions: int = 1
    max_trades_per_day: int = 5
    max_consecutive_losses: int = 3
    maximum_spread_points: float = 50.0
    reward_risk_ratio: float = 2.0
    atr_period: int = 14
    atr_stop_multiplier: float = 1.5
    stale_data_minutes: int = 5
    initial_equity: float = 500_000.0
    value_per_price_unit: float = 10.0
    estimated_cost_per_unit: float = 5.0
    min_stop_distance: float = 1.0
    min_position_lots: int = 1
    lot_size: int = 1
    margin_pct_of_notional: float = 0.10
    news_mode: str = "filtered"
    use_real_yield: bool = False
    enforce_risk_budget: bool = True
    trading_day_tz: str = "Asia/Kolkata"
    yahoo_macros: bool = True
    macro_stale_minutes: int = 4320
    macro_trade_max_age_minutes: float = 60.0
    gold_entry_max_age_minutes: float = 30.0
    place_broker_stop: bool = True
    require_broker_stop: bool = True
    macro_refresh_minutes: int = 15
    calendar_provider: str = "auto"
    calendar_secondary: str = "fmp"
    calendar_stream: bool = True
    calendar_schedule_refresh_hours: float = 6.0
    calendar_near_release_window_minutes: float = 30.0
    calendar_near_release_poll_seconds: float = 45.0
    calendar_horizon_days: int = 21
    use_release_surprise: bool = True
    surprise_reaction_minutes: float = 180.0
    surprise_epsilon: float = 0.0
    require_surprise_macro_agree: bool = True
    surprise_required_in_window: bool = True
    backtest_slippage_points: float = 1.0
    backtest_fee_per_lot: float = 0.0
    # Upstox / IO — env.txt is always the repo-root file.
    env_file: str = str(ENV_FILE)
    instrument_cache: str = str(INSTRUMENT_CACHE)
    calendar_path: str = str(DEFAULT_CALENDAR)
    dxy_csv: str = ""
    yield_csv: str = ""
    gold_csv: str = ""
    usdinr_csv: str = ""
    product: str = "D"                 # Upstox: I | D | MTF (MCX carry → D)
    validity: str = "DAY"
    order_tag: str = "GOLD_NEWS"
    market_protection: float = 0.0
    rest_retry: int = 4
    rest_timeout: float = 15.0
    poll_seconds: float = 5.0
    market_open: str = "09:00"
    market_close: str = "23:30"
    entry_cutoff: str = "23:00"
    enforce_session_hours: bool = True
    require_live_calendar: bool = True
    calendar_max_age_hours: float = 12.0
    live_armed: bool = False
    require_broker_funds: bool = False
    allow_yahoo_in_live: bool = False  # research Yahoo never auto-approved for live
    instrument_master_max_age_hours: float = 36.0
    roll_days_before_expiry: int = 3
    min_days_to_expiry_for_entry: int = 2
    access_token: str = field(default="", repr=False)
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    redirect_uri: str = field(default="", repr=False)
    instrument_key_override: str = ""
    usdinr_instrument_key: str = ""
    @classmethod
    def from_mapping(cls, raw: Optional[dict] = None, **overrides: Any) -> "EngineConfig":
        base = deepcopy(CONFIG)
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        if raw:
            unknown = [k for k in raw.keys() if k not in known and k not in ("",)]
            # Allow a few documentation-only keys without failing
            doc_ok = {"_comment", "comment", "$schema", "notes"}
            unknown = [k for k in unknown if k not in doc_ok]
            if unknown:
                raise SystemExit(
                    "Invalid engine config — unknown keys (typo?): "
                    + ", ".join(sorted(unknown))
                )
            base.update({k: v for k, v in raw.items() if v is not None and v != ""})
        base.update({k: v for k, v in overrides.items() if v is not None})
        cfg = cls(**{k: v for k, v in base.items() if k in known})
        # Empty calendar_path in JSON means "use engine default".
        if not cfg.calendar_path:
            cfg.calendar_path = str(DEFAULT_CALENDAR)
        cfg.env_file = str(ENV_FILE)
        cfg.mode = str(cfg.mode or "paper").lower()
        cfg.sizing_mode = str(cfg.sizing_mode or "fixed").lower()
        cfg.fixed_lots = max(1, int(cfg.fixed_lots or 1))
        # Keep P&L multiplier aligned with verified symbol map unless overridden.
        sym = str(cfg.gold_symbol or "").upper()
        if sym in MCX_VALUE_PER_PRICE_UNIT and (
            raw is None or "value_per_price_unit" not in (raw or {})
        ) and "value_per_price_unit" not in overrides:
            cfg.value_per_price_unit = MCX_VALUE_PER_PRICE_UNIT[sym]
        cfg.validate()
        return cfg

    def validate(self) -> None:
        """Fail closed on invalid / unsupported production settings."""
        import math
        from zoneinfo import ZoneInfo
        errors: list[str] = []

        def _finite(name: str, v: Any, *, lo: Optional[float] = None,
                    hi: Optional[float] = None, positive: bool = False) -> None:
            try:
                f = float(v)
            except (TypeError, ValueError):
                errors.append(f"{name}: not numeric ({v!r})")
                return
            if not math.isfinite(f):
                errors.append(f"{name}: not finite")
                return
            if positive and f <= 0:
                errors.append(f"{name}: must be > 0")
            if lo is not None and f < lo:
                errors.append(f"{name}: {f} < {lo}")
            if hi is not None and f > hi:
                errors.append(f"{name}: {f} > {hi}")

        def _bool(name: str, v: Any) -> None:
            if not isinstance(v, bool):
                errors.append(
                    f"{name}: must be JSON boolean true/false, not {type(v).__name__} ({v!r})"
                )

        def _int_field(name: str, v: Any, *, lo: Optional[int] = None,
                       hi: Optional[int] = None) -> None:
            if isinstance(v, bool) or not isinstance(v, int):
                # Reject bool (subclass of int) and floats masquerading as periods
                try:
                    if isinstance(v, float) and v == int(v):
                        pass  # will coerce below via check
                    elif not isinstance(v, int) or isinstance(v, bool):
                        errors.append(f"{name}: must be integer, got {v!r}")
                        return
                except Exception:  # noqa: BLE001
                    errors.append(f"{name}: must be integer, got {v!r}")
                    return
            try:
                iv = int(v)
            except (TypeError, ValueError):
                errors.append(f"{name}: must be integer, got {v!r}")
                return
            if lo is not None and iv < lo:
                errors.append(f"{name}: {iv} < {lo}")
            if hi is not None and iv > hi:
                errors.append(f"{name}: {iv} > {hi}")

        if self.mode not in ("paper", "live"):
            errors.append(f"mode unsupported: {self.mode}")
        if self.sizing_mode not in ("fixed", "risk"):
            errors.append(f"sizing_mode unsupported: {self.sizing_mode}")
        if self.news_mode not in ("filtered", "aware"):
            errors.append(f"news_mode unsupported: {self.news_mode}")
        if str(self.strategy_mode or "").lower() not in ("trend_breakout", "legacy_macro"):
            errors.append(f"strategy_mode unsupported: {self.strategy_mode}")
        self.strategy_mode = str(self.strategy_mode or "trend_breakout").lower()
        if self.gold_symbol.upper() not in MCX_VALUE_PER_PRICE_UNIT:
            errors.append(
                f"gold_symbol '{self.gold_symbol}' not in verified multipliers "
                f"{sorted(MCX_VALUE_PER_PRICE_UNIT)}"
            )
        prod = str(self.product or "").upper().strip()
        if prod not in ("I", "D", "MTF"):
            errors.append(
                f"product '{self.product}' unsupported — use I|D|MTF "
                "(legacy M/NRML code is not accepted by current Upstox place-order API)"
            )
        self.product = prod
        if str(self.validity or "").upper() not in ("DAY", "IOC"):
            errors.append(f"validity unsupported: {self.validity}")
        cal_p = str(self.calendar_provider or "").lower()
        if cal_p not in ("auto", "trading_economics", "fmp", "csv"):
            errors.append(f"calendar_provider unsupported: {self.calendar_provider}")
        # Single-position engine — config must not imply multi-position support
        if int(self.maximum_open_positions) != 1:
            errors.append(
                "maximum_open_positions must be 1 "
                "(engine maintains a single Position ledger)"
            )
        # Broker stop compatibility
        if bool(self.require_broker_stop) and not bool(self.place_broker_stop):
            errors.append("require_broker_stop=true requires place_broker_stop=true")
        # Live mode hard gates
        if self.mode == "live":
            if not bool(self.require_broker_funds):
                errors.append(
                    "live mode requires require_broker_funds=true "
                    "(actual broker funds/margin check)"
                )
            if bool(self.yahoo_macros) and not bool(self.allow_yahoo_in_live):
                errors.append(
                    "live mode: yahoo_macros=true requires allow_yahoo_in_live=true "
                    "with documented approval (research-only source otherwise)"
                )
            if bool(self.use_real_yield):
                errors.append(
                    "live mode: use_real_yield=true but no production real-yield "
                    "feed is wired — set use_real_yield=false or implement the feed"
                )
            if not bool(self.live_armed):
                # Not an error — keep as soft gate in runner; document only
                pass

        _bool("yahoo_macros", self.yahoo_macros)
        _bool("use_real_yield", self.use_real_yield)
        _bool("enforce_risk_budget", self.enforce_risk_budget)
        _bool("place_broker_stop", self.place_broker_stop)
        _bool("require_broker_stop", self.require_broker_stop)
        _bool("use_release_surprise", self.use_release_surprise)
        _bool("require_surprise_macro_agree", self.require_surprise_macro_agree)
        _bool("surprise_required_in_window", self.surprise_required_in_window)
        _bool("enforce_session_hours", self.enforce_session_hours)
        _bool("require_live_calendar", self.require_live_calendar)
        _bool("live_armed", self.live_armed)
        _bool("require_broker_funds", self.require_broker_funds)
        _bool("allow_yahoo_in_live", self.allow_yahoo_in_live)
        _bool("calendar_stream", self.calendar_stream)
        _bool("require_news_confirmation", self.require_news_confirmation)
        _bool("run_embedded_producers", self.run_embedded_producers)
        _bool("enable_monitor_producer", self.enable_monitor_producer)
        _bool("enable_news_producer", self.enable_news_producer)
        _bool("news_producer_no_telegram", self.news_producer_no_telegram)
        _finite("news_freshness_minutes", self.news_freshness_minutes, positive=True)
        _finite("monitor_refresh_seconds", self.monitor_refresh_seconds, lo=15, hi=600)
        _finite("news_poll_interval_seconds", self.news_poll_interval_seconds, lo=30, hi=900)
        _int_field("max_trades_per_day", self.max_trades_per_day, lo=0, hi=100)
        _int_field("max_consecutive_losses", self.max_consecutive_losses, lo=0, hi=50)
        _int_field("trend_timeframe_minutes", self.trend_timeframe_minutes, lo=5, hi=60)
        _int_field("trend_ema_fast", self.trend_ema_fast, lo=2, hi=200)
        _int_field("trend_ema_slow", self.trend_ema_slow, lo=3, hi=400)
        _int_field("flatten_before_close_minutes", self.flatten_before_close_minutes, lo=0, hi=120)
        if int(self.trend_ema_fast) >= int(self.trend_ema_slow):
            errors.append("trend_ema_fast must be < trend_ema_slow")

        _finite("risk_per_trade_pct", self.risk_per_trade_pct, lo=0.01, hi=100)
        _finite("maximum_daily_loss_pct", self.maximum_daily_loss_pct, lo=0.01, hi=100)
        _finite("initial_equity", self.initial_equity, positive=True)
        _finite("reward_risk_ratio", self.reward_risk_ratio, positive=True)
        _finite("atr_stop_multiplier", self.atr_stop_multiplier, positive=True)
        _finite("min_stop_distance", self.min_stop_distance, positive=True)
        _finite("value_per_price_unit", self.value_per_price_unit, positive=True)
        _finite("maximum_spread_points", self.maximum_spread_points, lo=0)
        _finite("estimated_cost_per_unit", self.estimated_cost_per_unit, lo=0)
        _finite("margin_pct_of_notional", self.margin_pct_of_notional, lo=0.0, hi=1.0)
        _finite("poll_seconds", self.poll_seconds, positive=True)
        _finite("rest_timeout", self.rest_timeout, positive=True)
        _finite("macro_trade_max_age_minutes", self.macro_trade_max_age_minutes, positive=True)
        _finite("gold_entry_max_age_minutes", self.gold_entry_max_age_minutes, positive=True)
        _finite("stale_data_minutes", self.stale_data_minutes, positive=True)
        _finite("calendar_max_age_hours", self.calendar_max_age_hours, positive=True)
        _finite("calendar_schedule_refresh_hours", self.calendar_schedule_refresh_hours, positive=True)
        _finite("calendar_near_release_window_minutes", self.calendar_near_release_window_minutes, positive=True)
        _finite("calendar_near_release_poll_seconds", self.calendar_near_release_poll_seconds, positive=True)
        _finite("surprise_reaction_minutes", self.surprise_reaction_minutes, positive=True)
        _finite("surprise_epsilon", self.surprise_epsilon, lo=0)
        _finite("backtest_slippage_points", self.backtest_slippage_points, lo=0)
        _finite("backtest_fee_per_lot", self.backtest_fee_per_lot, lo=0)
        _finite("instrument_master_max_age_hours", self.instrument_master_max_age_hours, positive=True)
        _finite("market_protection", self.market_protection, lo=0)

        _int_field("atr_period", self.atr_period, lo=1, hi=500)
        _int_field("fixed_lots", self.fixed_lots, lo=1, hi=1000)
        _int_field("lot_size", self.lot_size, lo=1, hi=1000)
        _int_field("min_position_lots", self.min_position_lots, lo=1)
        _int_field("gold_breakout_lookback_bars", self.gold_breakout_lookback_bars, lo=1)
        _int_field("timeframe_minutes", self.timeframe_minutes, lo=1, hi=240)
        _int_field("rest_retry", self.rest_retry, lo=1, hi=20)
        _int_field("news_block_before_minutes", self.news_block_before_minutes, lo=0)
        _int_field("news_block_after_minutes", self.news_block_after_minutes, lo=0)
        _int_field("reaction_window_minutes", self.reaction_window_minutes, lo=1)
        _int_field("macro_stale_minutes", self.macro_stale_minutes, lo=1)
        _int_field("macro_refresh_minutes", self.macro_refresh_minutes, lo=1)
        _int_field("calendar_horizon_days", self.calendar_horizon_days, lo=1, hi=90)
        _int_field("roll_days_before_expiry", self.roll_days_before_expiry, lo=0, hi=30)
        _int_field("min_days_to_expiry_for_entry", self.min_days_to_expiry_for_entry, lo=0, hi=30)
        _int_field("maximum_open_positions", self.maximum_open_positions, lo=1, hi=1)

        try:
            ZoneInfo(self.trading_day_tz or "Asia/Kolkata")
        except Exception:  # noqa: BLE001
            errors.append(f"trading_day_tz invalid: {self.trading_day_tz!r}")

        session_mins: dict[str, int] = {}
        for label, hhmm in (
            ("market_open", self.market_open),
            ("market_close", self.market_close),
            ("entry_cutoff", self.entry_cutoff),
        ):
            try:
                h, m = str(hhmm).split(":")
                hi, mi = int(h), int(m)
                if not (0 <= hi <= 23 and 0 <= mi <= 59):
                    raise ValueError
                session_mins[label] = hi * 60 + mi
            except Exception:  # noqa: BLE001
                errors.append(f"{label} must be HH:MM, got {hhmm!r}")
        if len(session_mins) == 3:
            o, c, cut = (
                session_mins["market_open"],
                session_mins["market_close"],
                session_mins["entry_cutoff"],
            )
            # Same-day session only (overnight not supported)
            if not (o < cut <= c):
                errors.append(
                    f"session order invalid: need market_open < entry_cutoff <= market_close "
                    f"(got open={self.market_open} cutoff={self.entry_cutoff} close={self.market_close})"
                )
            if c <= o:
                errors.append(
                    "overnight sessions are not supported — market_close must be after market_open"
                )
        # Roll policy: entry block must be at least as strict as roll days
        if int(self.min_days_to_expiry_for_entry) < int(self.roll_days_before_expiry):
            # Align entry gate with roll calendar-day policy
            self.min_days_to_expiry_for_entry = int(self.roll_days_before_expiry)

        if errors:
            raise SystemExit("Invalid engine config:\n  - " + "\n  - ".join(errors))
    @classmethod
    def from_file(cls, path: str | Path | None = None, **overrides: Any) -> "EngineConfig":
        return cls.from_mapping(load_config_file(path), **overrides)
    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
    def apply_mcx_symbol(self, symbol: str) -> None:
        """Set gold_symbol and the matching contract value-per-unit default."""
        sym = symbol.upper().strip()
        self.gold_symbol = sym
        if sym in MCX_VALUE_PER_PRICE_UNIT:
            self.value_per_price_unit = MCX_VALUE_PER_PRICE_UNIT[sym]
    @property
    def is_live(self) -> bool:
        return self.mode == "live"
