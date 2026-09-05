from __future__ import annotations

import os
import sys
import json
import time
import sqlite3
import logging
import logging.handlers
import threading
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, date, time as dtime, timedelta
from typing import Optional, Dict, List, Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    try:
        import pytz
        IST = pytz.timezone("Asia/Kolkata")
    except Exception:
        IST = None

def now_ist() -> datetime:
    if IST is not None:
        return datetime.now(IST)
    from datetime import timezone
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)

def today_ist() -> date:
    return now_ist().date()

def parse_ist_timestamp(ts) -> Optional[datetime]:
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            if IST is not None:
                return datetime.fromtimestamp(ts, tz=IST)
            from datetime import timezone
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
        if isinstance(ts, str):
            s = ts.strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                if IST is not None:
                    dt = dt.replace(tzinfo=IST)
            else:
                if IST is not None:
                    dt = dt.astimezone(IST)
            return dt
    except Exception:
        return None
    return None

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / "env.txt"
DEFAULT_DB_PATH = BASE_DIR / "data" / "nifty_algo.db"
DEFAULT_LOG_DIR = BASE_DIR / "logs"
DEFAULT_EVENTS_FILE = BASE_DIR / "high_impact_events.json"
DEFAULT_HOLIDAYS_FILE = BASE_DIR / "nse_holidays.json"

INSTRUMENT_KEY_NIFTY_SPOT = "NSE_INDEX|Nifty 50"
INSTRUMENT_KEY_INDIA_VIX = "NSE_INDEX|India VIX"
UPSTOX_BASE_URL = "https://api.upstox.com/v2"

API_ENDPOINTS = {
    "profile":           "/user/profile",
    "ltp":               "/market-quote/ltp",
    "quotes":            "/market-quote/quotes",
    "ohlc":              "/market-quote/ohlc",
    "historical_candle": "/historical-candle/{instrument_key}/{interval}/{to_date}/{from_date}",
    "intraday_candle":   "/historical-candle/intraday/{instrument_key}/{interval}",
    "option_contracts":  "/option/contract",
    "option_chain":      "/option/chain",
    "place_order":       "/order/place",
    "cancel_order":      "/order/cancel",
    "order_details":     "/order/details",
    "positions":         "/portfolio/short-term-positions",
    "funds_margin":      "/user/get-funds-and-margin",
}

ENV_TEMPLATE = """\
UPSTOX_API_KEY=
UPSTOX_API_SECRET=
UPSTOX_REDIRECT_URI=
UPSTOX_ACCESS_TOKEN=
PAPER_TRADE_MODE=true
LIVE_RATES_VERIFIED=false
STARTING_CAPITAL=1000000
MAX_DAILY_LOSS_PCT=0.02
MAX_RISK_PER_TRADE_PCT=0.006
NIFTY_LOT_SIZE=65
NIFTY_STRIKE_STEP=50
STT_RATE=0.000625
STT_OPTIONS_SELL=0.000625
STT_OPTIONS_EXERCISE=0.0015
BROKERAGE_PER_ORDER=20.0
EXCHANGE_TXN_RATE=0.0003552
SEBI_RATE=0.000001
STAMP_DUTY_BUY_OPTIONS=0.00003
TRADING_WINDOW_START=09:45
TRADING_WINDOW_LAST_ENTRY=14:00
HARD_EXIT_TIME=15:25
MAX_CONCURRENT_POSITIONS=2
MAX_ENTRIES_PER_DAY=3
DB_PATH=data/nifty_algo.db
LOG_DIR=logs
LOG_LEVEL=INFO
REQUEST_TIMEOUT_SECONDS=10
MAX_RETRIES=3
ADX_PERIOD=14
ADX_TREND_THRESHOLD=25.0
ADX_STRONG_THRESHOLD=35.0
EMA_FAST=9
EMA_SLOW=21
MTF_RESAMPLE_15=900s
MTF_RESAMPLE_60=3600s
MIN_BARS_FOR_ADX=20
MIN_BARS_FOR_EMA_SLOW=25
SKEW_BEARISH_THRESHOLD=3.0
SKEW_BULLISH_THRESHOLD=-1.5
OI_CHANGE_LOOKBACK_MIN=30
OI_BUILDUP_THRESHOLD=0.08
OI_UNWIND_THRESHOLD=-0.08
VIX_FAIL_LIMIT=5
REGIME_CONFIRM_CYCLES=2
STRADDLE_EXPLOSION_PCT=18.0
ORB_VOLUME_MULTIPLIER=1.4
VIX_ROC_LOOKBACK_MIN=30
VIX_ROC_EMERGENCY_PCT=7.5
IVR_SELL=50.0
IVR_BUY=20.0
IVR_NEUTRAL_LOW=35.0
IV_HV_SELL=1.12
IV_HV_NEUTRAL=0.92
IV_HV_BUY=0.78
STRADDLE_RATIO_SELL=1.18
STRADDLE_RATIO_NEUTRAL_H=1.06
STRADDLE_RATIO_NEUTRAL_L=0.88
OI_WALL_STRONG=2.8
OI_WALL_MODERATE=1.7
OI_WALL_WEAK=1.1
PCR_BULLISH_THRESHOLD=0.72
PCR_BEARISH_THRESHOLD=1.28
PCR_EXTREME_BULL=0.60
PCR_EXTREME_BEAR=1.55
REGIME_CALC_INTERVAL_SEC=45
SPOT_BAR_INTERVAL_SEC=60
CALIBRATION_INTERVAL_SEC=3600
MIN_TRADING_DAYS_FOR_CALIBRATION=20
EVENT_SIZE_MULTIPLIER=0.25
TUESDAY_EARLY_EXIT_ENABLED=true
GIFT_NIFTY_INSTRUMENT_KEY=
VIX_LOW=12.5
VIX_NORMAL=16.0
VIX_HIGH=22.0
VIX_EXTREME_HIGH=28.0
DEFINED_RISK_ONLY_ON_EVENT=true
HV_LOOKBACK_DAYS=20
STRADDLE_ROC_WINDOW_MIN=15
STRADDLE_ROC_ALERT_PCT=12.0
VIX_LOW=12.5
VIX_NORMAL=16.0
VIX_HIGH=22.0
VIX_EXTREME_HIGH=28.0
DEFINED_RISK_ONLY_ON_EVENT=true
HV_LOOKBACK_DAYS=20
STRADDLE_ROC_WINDOW_MIN=15
STRADDLE_ROC_ALERT_PCT=12.0
"""


def ensure_env_file(path: Path) -> None:
    if not path.exists():
        path.write_text(ENV_TEMPLATE, encoding="utf-8")
        print(f"[SETUP] Created env.txt template at {path}. Fill in credentials before running.")


def load_env_file(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _get_bool(env: dict, key: str, default: bool) -> bool:
    val = env.get(key)
    if val is None or val == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_float(env: dict, key: str, default: float) -> float:
    val = env.get(key)
    try:
        return float(val) if val not in (None, "") else default
    except (ValueError, TypeError):
        return default


def _get_int(env: dict, key: str, default: int) -> int:
    val = env.get(key)
    try:
        return int(val) if val not in (None, "") else default
    except (ValueError, TypeError):
        return default


def _get_time(env: dict, key: str, default: dtime) -> dtime:
    val = env.get(key)
    if not val:
        return default
    try:
        h, m = val.strip().split(":")
        return dtime(int(h), int(m))
    except Exception:
        return default


def load_nse_holidays(path: Path = DEFAULT_HOLIDAYS_FILE) -> set:
    if not path.exists():
        path.write_text("[]\n", encoding="utf-8")
        print(f"[SETUP] Created empty {path}. Populate with verified NSE holiday dates.")
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return set(raw)
        if isinstance(raw, dict):
            return set(raw.keys())
        return set()
    except json.JSONDecodeError:
        print(f"[WARNING] {path} is not valid JSON. Treating as empty.")
        return set()


def load_high_impact_events(path: Path = DEFAULT_EVENTS_FILE) -> Dict[date, str]:
    if not path.exists():
        path.write_text("{}\n", encoding="utf-8")
        print(f"[SETUP] Created empty {path}. Populate with verified event dates.")
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[WARNING] {path} is not valid JSON ({e}). Treating as empty.")
        return {}
    if not raw:
        return {}
    flat = {}
    for key, val in raw.items():
        if isinstance(val, dict) and "dates" in val:
            desc = val.get("description", key)
            for d in val.get("dates", []):
                if isinstance(d, str) and len(d) == 10:
                    flat[d] = desc
        elif isinstance(val, list):
            for d in val:
                if isinstance(d, str) and len(d) == 10:
                    flat[d] = key
        elif isinstance(val, str) and len(key) == 10:
            flat[key] = val
    if flat:
        result = {}
        for k, v in flat.items():
            try:
                result[date.fromisoformat(k)] = v
            except ValueError:
                pass
        return result
    result = {}
    for k, v in raw.items():
        if isinstance(k, str) and len(k) == 10:
            try:
                result[date.fromisoformat(k)] = v
            except ValueError:
                pass
    return result


_NSE_HOLIDAYS_CACHE: Optional[set] = None
_HIGH_IMPACT_EVENTS_CACHE: Optional[Dict[date, str]] = None


def get_nse_holidays() -> set:
    global _NSE_HOLIDAYS_CACHE
    if _NSE_HOLIDAYS_CACHE is None:
        _NSE_HOLIDAYS_CACHE = load_nse_holidays()
    return _NSE_HOLIDAYS_CACHE


def get_high_impact_events() -> Dict[date, str]:
    global _HIGH_IMPACT_EVENTS_CACHE
    if _HIGH_IMPACT_EVENTS_CACHE is None:
        _HIGH_IMPACT_EVENTS_CACHE = load_high_impact_events()
    return _HIGH_IMPACT_EVENTS_CACHE


class ExpiryCalendar:

    @classmethod
    def is_holiday(cls, d: date) -> bool:
        holidays = get_nse_holidays()
        return d.isoformat() in holidays or d.weekday() >= 5

    @classmethod
    def is_event_day(cls, d: date = None) -> str:
        if d is None:
            d = today_ist()
        return get_high_impact_events().get(d, "")

    @classmethod
    def get_next_expiry(cls, from_date: date = None, after_close: bool = False) -> date:
        if from_date is None:
            from_date = today_ist()
        if not after_close:
            after_close = now_ist().time() > dtime(15, 30)
        days_ahead = (1 - from_date.weekday()) % 7
        candidate = from_date + timedelta(days=days_ahead)
        if from_date.weekday() == 1:
            candidate = from_date + timedelta(days=7) if after_close else from_date
        while cls.is_holiday(candidate):
            candidate -= timedelta(days=1)
        return candidate

    @classmethod
    def get_dte(cls, from_date: date = None) -> int:
        if from_date is None:
            from_date = today_ist()
        expiry = cls.get_next_expiry(from_date)
        if expiry <= from_date:
            return 0
        count = 0
        d = from_date + timedelta(days=1)
        while d <= expiry:
            if not cls.is_holiday(d):
                count += 1
            d += timedelta(days=1)
        return count

    @classmethod
    def get_day_type(cls, d: date = None) -> str:
        if d is None:
            d = today_ist()
        if cls.is_holiday(d):
            return "NON_TRADING"
        dte = cls.get_dte(d)
        weekday = d.weekday()
        if dte == 0:
            return "EXPIRY_DAY"
        if dte == 1:
            return "PRE_EXPIRY"
        if weekday == 2:
            return "NEW_CYCLE"
        if weekday == 4:
            return "WEEKEND_RISK"
        if weekday == 0:
            return "PRE_EXPIRY"
        return "MID_WEEK"

    @classmethod
    def is_monthly_expiry(cls, d: date = None) -> bool:
        if d is None:
            d = today_ist()
        if d.weekday() != 1:
            return False
        return (d + timedelta(days=7)).month != d.month

    @classmethod
    def get_next_trading_day(cls, from_date: date = None) -> Optional[date]:
        if from_date is None:
            from_date = today_ist()
        d = from_date + timedelta(days=1)
        for _ in range(10):
            if not cls.is_holiday(d):
                return d
            d += timedelta(days=1)
        return None


@dataclass(frozen=True, repr=False)
class Config:
    upstox_api_key: str
    upstox_api_secret: str
    upstox_redirect_uri: str
    upstox_access_token: str
    paper_trade_mode: bool
    starting_capital: float
    max_daily_loss_pct: float
    max_risk_per_trade_pct: float
    lot_size: int
    nifty_strike_step: int
    stt_rate: float
    stt_options_sell: float
    stt_options_exercise: float
    brokerage_per_order: float
    exchange_txn_rate: float
    sebi_rate: float
    stamp_duty_buy_options: float
    trading_window_start: dtime
    trading_window_last_entry: dtime
    hard_exit_time: dtime
    max_concurrent_positions: int
    max_entries_per_day: int
    db_path: Path
    log_dir: Path
    log_level: str
    request_timeout_seconds: float
    max_retries: int
    rate_limits: dict
    adx_period: int
    adx_trend_threshold: float
    adx_strong_threshold: float
    ema_fast: int
    ema_slow: int
    mtf_resample_15: str
    mtf_resample_60: str
    min_bars_for_adx: int
    min_bars_for_ema_slow: int
    skew_bearish_threshold: float
    skew_bullish_threshold: float
    oi_change_lookback_min: int
    oi_buildup_threshold: float
    oi_unwind_threshold: float
    vix_fail_limit: int
    regime_confirm_cycles: int
    straddle_explosion_pct: float
    orb_volume_multiplier: float
    vix_roc_lookback_min: int
    vix_roc_emergency_pct: float
    ivr_sell: float
    ivr_buy: float
    ivr_neutral_low: float
    iv_hv_sell: float
    iv_hv_neutral: float
    iv_hv_buy: float
    straddle_ratio_sell: float
    straddle_ratio_neutral_h: float
    straddle_ratio_neutral_l: float
    oi_wall_strong: float
    oi_wall_moderate: float
    oi_wall_weak: float
    pcr_bullish_threshold: float
    pcr_bearish_threshold: float
    pcr_extreme_bull: float
    pcr_extreme_bear: float
    regime_calc_interval_sec: int
    spot_bar_interval_sec: int
    calibration_interval_sec: int
    min_trading_days_for_calibration: int
    event_size_multiplier: float
    tuesday_early_exit_enabled: bool
    gift_nifty_instrument_key: str
    vix_low: float
    vix_normal: float
    vix_high: float
    vix_extreme_high: float
    defined_risk_only_on_event: bool
    hv_lookback_days: int
    straddle_roc_window_min: int
    straddle_roc_alert_pct: float
    vix_low: float
    vix_normal: float
    vix_high: float
    vix_extreme_high: float
    defined_risk_only_on_event: bool
    hv_lookback_days: int
    straddle_roc_window_min: int
    straddle_roc_alert_pct: float

    def __repr__(self) -> str:
        def mask(s: str) -> str:
            if not s:
                return "<empty>"
            return (s[:4] + "..." + s[-2:]) if len(s) > 8 else "***"
        return (
            f"Config(paper_trade_mode={self.paper_trade_mode}, "
            f"lot_size={self.lot_size}, "
            f"starting_capital={self.starting_capital}, "
            f"upstox_api_key={mask(self.upstox_api_key)}, "
            f"upstox_access_token={mask(self.upstox_access_token)})"
        )


def load_config(env_file: Path = ENV_FILE) -> Config:
    ensure_env_file(env_file)
    file_env = load_env_file(env_file)
    env = {**os.environ, **file_env}

    max_daily_loss_pct = _get_float(env, "MAX_DAILY_LOSS_PCT", 0.02)
    max_risk_per_trade_pct = _get_float(env, "MAX_RISK_PER_TRADE_PCT", 0.006)

    if max_risk_per_trade_pct >= max_daily_loss_pct / 3.0:
        safe_value = round(max_daily_loss_pct / 3.0 - 0.001, 4)
        print(f"[WARNING] MAX_RISK_PER_TRADE_PCT clamped to {safe_value} to satisfy < MAX_DAILY_LOSS_PCT/3")
        max_risk_per_trade_pct = max(safe_value, 0.001)

    rate_limits = {
        "quote":      {"per_second": 8,  "per_minute": 120, "per_30min": 1200},
        "historical": {"per_second": 5,  "per_minute": 60,  "per_30min": 600},
        "chain":      {"per_second": 3,  "per_minute": 30,  "per_30min": 300},
        "order":      {"per_second": 3,  "per_minute": 30,  "per_30min": 200},
        "default":    {"per_second": 5,  "per_minute": 60,  "per_30min": 600},
    }

    db_path = Path(env.get("DB_PATH", str(DEFAULT_DB_PATH)))
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    log_dir = Path(env.get("LOG_DIR", str(DEFAULT_LOG_DIR)))
    if not log_dir.is_absolute():
        log_dir = BASE_DIR / log_dir

    paper_trade_mode = _get_bool(env, "PAPER_TRADE_MODE", True)
    live_rates_verified = _get_bool(env, "LIVE_RATES_VERIFIED", False)
    if not paper_trade_mode and not live_rates_verified:
        print("[SAFETY] LIVE_RATES_VERIFIED not set. Forcing PAPER_TRADE_MODE=true.")
        paper_trade_mode = True

    return Config(
        upstox_api_key=env.get("UPSTOX_API_KEY", ""),
        upstox_api_secret=env.get("UPSTOX_API_SECRET", ""),
        upstox_redirect_uri=env.get("UPSTOX_REDIRECT_URI", ""),
        upstox_access_token=env.get("UPSTOX_ACCESS_TOKEN", ""),
        paper_trade_mode=paper_trade_mode,
        starting_capital=_get_float(env, "STARTING_CAPITAL", 1_000_000.0),
        max_daily_loss_pct=max_daily_loss_pct,
        max_risk_per_trade_pct=max_risk_per_trade_pct,
        lot_size=_get_int(env, "NIFTY_LOT_SIZE", 65),
        nifty_strike_step=_get_int(env, "NIFTY_STRIKE_STEP", 50),
        stt_rate=_get_float(env, "STT_RATE", 0.000625),
        stt_options_sell=_get_float(env, "STT_OPTIONS_SELL", 0.000625),
        stt_options_exercise=_get_float(env, "STT_OPTIONS_EXERCISE", 0.0015),
        brokerage_per_order=_get_float(env, "BROKERAGE_PER_ORDER", 20.0),
        exchange_txn_rate=_get_float(env, "EXCHANGE_TXN_RATE", 0.0003552),
        sebi_rate=_get_float(env, "SEBI_RATE", 0.000001),
        stamp_duty_buy_options=_get_float(env, "STAMP_DUTY_BUY_OPTIONS", 0.00003),
        trading_window_start=_get_time(env, "TRADING_WINDOW_START", dtime(9, 45)),
        trading_window_last_entry=_get_time(env, "TRADING_WINDOW_LAST_ENTRY", dtime(14, 0)),
        hard_exit_time=_get_time(env, "HARD_EXIT_TIME", dtime(15, 25)),
        max_concurrent_positions=_get_int(env, "MAX_CONCURRENT_POSITIONS", 2),
        max_entries_per_day=_get_int(env, "MAX_ENTRIES_PER_DAY", 3),
        db_path=db_path,
        log_dir=log_dir,
        log_level=env.get("LOG_LEVEL", "INFO"),
        request_timeout_seconds=_get_float(env, "REQUEST_TIMEOUT_SECONDS", 10.0),
        max_retries=_get_int(env, "MAX_RETRIES", 3),
        rate_limits=rate_limits,
        adx_period=_get_int(env, "ADX_PERIOD", 14),
        adx_trend_threshold=_get_float(env, "ADX_TREND_THRESHOLD", 25.0),
        adx_strong_threshold=_get_float(env, "ADX_STRONG_THRESHOLD", 35.0),
        ema_fast=_get_int(env, "EMA_FAST", 9),
        ema_slow=_get_int(env, "EMA_SLOW", 21),
        mtf_resample_15=env.get("MTF_RESAMPLE_15", "900s"),
        mtf_resample_60=env.get("MTF_RESAMPLE_60", "3600s"),
        min_bars_for_adx=_get_int(env, "MIN_BARS_FOR_ADX", 20),
        min_bars_for_ema_slow=_get_int(env, "MIN_BARS_FOR_EMA_SLOW", 25),
        skew_bearish_threshold=_get_float(env, "SKEW_BEARISH_THRESHOLD", 3.0),
        skew_bullish_threshold=_get_float(env, "SKEW_BULLISH_THRESHOLD", -1.5),
        oi_change_lookback_min=_get_int(env, "OI_CHANGE_LOOKBACK_MIN", 30),
        oi_buildup_threshold=_get_float(env, "OI_BUILDUP_THRESHOLD", 0.08),
        oi_unwind_threshold=_get_float(env, "OI_UNWIND_THRESHOLD", -0.08),
        vix_fail_limit=_get_int(env, "VIX_FAIL_LIMIT", 5),
        regime_confirm_cycles=_get_int(env, "REGIME_CONFIRM_CYCLES", 2),
        straddle_explosion_pct=_get_float(env, "STRADDLE_EXPLOSION_PCT", 18.0),
        orb_volume_multiplier=_get_float(env, "ORB_VOLUME_MULTIPLIER", 1.4),
        vix_roc_lookback_min=_get_int(env, "VIX_ROC_LOOKBACK_MIN", 30),
        vix_roc_emergency_pct=_get_float(env, "VIX_ROC_EMERGENCY_PCT", 7.5),
        ivr_sell=_get_float(env, "IVR_SELL", 50.0),
        ivr_buy=_get_float(env, "IVR_BUY", 20.0),
        ivr_neutral_low=_get_float(env, "IVR_NEUTRAL_LOW", 35.0),
        iv_hv_sell=_get_float(env, "IV_HV_SELL", 1.12),
        iv_hv_neutral=_get_float(env, "IV_HV_NEUTRAL", 0.92),
        iv_hv_buy=_get_float(env, "IV_HV_BUY", 0.78),
        straddle_ratio_sell=_get_float(env, "STRADDLE_RATIO_SELL", 1.18),
        straddle_ratio_neutral_h=_get_float(env, "STRADDLE_RATIO_NEUTRAL_H", 1.06),
        straddle_ratio_neutral_l=_get_float(env, "STRADDLE_RATIO_NEUTRAL_L", 0.88),
        oi_wall_strong=_get_float(env, "OI_WALL_STRONG", 2.8),
        oi_wall_moderate=_get_float(env, "OI_WALL_MODERATE", 1.7),
        oi_wall_weak=_get_float(env, "OI_WALL_WEAK", 1.1),
        pcr_bullish_threshold=_get_float(env, "PCR_BULLISH_THRESHOLD", 0.72),
        pcr_bearish_threshold=_get_float(env, "PCR_BEARISH_THRESHOLD", 1.28),
        pcr_extreme_bull=_get_float(env, "PCR_EXTREME_BULL", 0.60),
        pcr_extreme_bear=_get_float(env, "PCR_EXTREME_BEAR", 1.55),
        regime_calc_interval_sec=_get_int(env, "REGIME_CALC_INTERVAL_SEC", 45),
        spot_bar_interval_sec=_get_int(env, "SPOT_BAR_INTERVAL_SEC", 60),
        calibration_interval_sec=_get_int(env, "CALIBRATION_INTERVAL_SEC", 3600),
        min_trading_days_for_calibration=_get_int(env, "MIN_TRADING_DAYS_FOR_CALIBRATION", 20),
        event_size_multiplier=_get_float(env, "EVENT_SIZE_MULTIPLIER", 0.25),
        tuesday_early_exit_enabled=_get_bool(env, "TUESDAY_EARLY_EXIT_ENABLED", True),
        gift_nifty_instrument_key=env.get("GIFT_NIFTY_INSTRUMENT_KEY", "").strip(),
        vix_low=_get_float(env, "VIX_LOW", 12.5),
        vix_normal=_get_float(env, "VIX_NORMAL", 16.0),
        vix_high=_get_float(env, "VIX_HIGH", 22.0),
        vix_extreme_high=_get_float(env, "VIX_EXTREME_HIGH", 28.0),
        defined_risk_only_on_event=_get_bool(env, "DEFINED_RISK_ONLY_ON_EVENT", True),
        hv_lookback_days=_get_int(env, "HV_LOOKBACK_DAYS", 20),
        straddle_roc_window_min=_get_int(env, "STRADDLE_ROC_WINDOW_MIN", 15),
        straddle_roc_alert_pct=_get_float(env, "STRADDLE_ROC_ALERT_PCT", 12.0),
    )


class RateLimiter:

    def __init__(self, limits: dict):
        self._limits = limits
        self._calls: dict = {}
        self._lock = threading.Lock()

    def _deque_for(self, category: str) -> list:
        if category not in self._calls:
            self._calls[category] = []
        return self._calls[category]

    def wait_if_needed(self, category: str) -> float:
        cfg = self._limits.get(category, self._limits.get("default", {"per_second": 5, "per_minute": 60, "per_30min": 600}))
        total_wait = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                dq = self._deque_for(category)
                dq[:] = [t for t in dq if now - t <= 1800]
                in_1s = [t for t in dq if now - t <= 1]
                in_60s = [t for t in dq if now - t <= 60]
                in_30m = dq
                sleep_needed = 0.0
                if len(in_1s) >= cfg.get("per_second", 5):
                    sleep_needed = max(sleep_needed, 1.0 - (now - min(in_1s)) + 0.01)
                if len(in_60s) >= cfg.get("per_minute", 60):
                    sleep_needed = max(sleep_needed, 60.0 - (now - min(in_60s)) + 0.01)
                if len(in_30m) >= cfg.get("per_30min", 600):
                    sleep_needed = max(sleep_needed, 1800.0 - (now - in_30m[0]) + 0.01)
                if sleep_needed <= 0:
                    dq.append(now)
                    return total_wait
            time.sleep(sleep_needed)
            total_wait += sleep_needed


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS session_state (
    trading_date TEXT PRIMARY KEY,
    day_mode TEXT, vix_regime TEXT, gap_size TEXT, gap_direction TEXT,
    day_label TEXT, or_high REAL, or_low REAL, or_width REAL, or_condition TEXT,
    entry_start TEXT, entry_end TEXT, hard_exit_time TEXT,
    stop_multiplier REAL, size_multiplier REAL, wing_width INTEGER,
    entry_count INTEGER DEFAULT 0, reentry_count INTEGER DEFAULT 0,
    daily_halted INTEGER DEFAULT 0, consecutive_stops INTEGER DEFAULT 0,
    last_stop_time TEXT, last_stop_reason TEXT, last_entry_time TEXT,
    actual_expiry TEXT, actual_dte INTEGER, opening_iv REAL, opening_pcr REAL,
    current_capital REAL, daily_pnl REAL DEFAULT 0.0,
    circuit_breaker_suspected INTEGER DEFAULT 0,
    vix_spike_detected INTEGER DEFAULT 0, event_announced INTEGER DEFAULT 0,
    paper_trade_mode INTEGER DEFAULT 1,
    or_computed INTEGER DEFAULT 0, session_initialized INTEGER DEFAULT 0,
    vix_regime_last_checked TEXT, prev_spot REAL, prev_vix REAL,
    parkinson_rv_pct REAL, parkinson_rv_computed_date TEXT,
    vwap_valid INTEGER DEFAULT 0, expiry_last_checked TEXT,
    pre_event_spot REAL, pre_event_iv REAL, event_announcement_time TEXT,
    last_stop_signal_combo TEXT, gap_fade_opportunity INTEGER DEFAULT 0,
    created_at TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    trading_date TEXT, strategy_name TEXT, strategy_type TEXT,
    selection_reason TEXT, target_expiry TEXT, actual_dte INTEGER,
    entry_time TEXT, entry_spot REAL, entry_vix REAL, entry_vrp REAL,
    entry_credit REAL, entry_debit REAL, gross_credit REAL,
    total_slippage REAL, entry_costs_rupees REAL,
    stop_premium REAL, target_premium REAL, stop_value REAL, target_value REAL,
    price_stop_pts INTEGER, hard_exit_time TEXT,
    final_lots INTEGER, max_loss_per_lot REAL, total_max_risk REAL,
    estimated_margin REAL, status TEXT DEFAULT 'OPEN',
    exit_time TEXT, exit_reason TEXT, exit_premium REAL,
    gross_pnl_rupees REAL, exit_costs_rupees REAL, net_pnl_rupees REAL,
    last_known_premium REAL, stop_at_breakeven INTEGER DEFAULT 0,
    stop_moved_to_25pct INTEGER DEFAULT 0,
    stop_tightened_for_delta INTEGER DEFAULT 0,
    paper_trade INTEGER DEFAULT 1, raw_params_json TEXT,
    created_at TEXT, updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_date ON positions(trading_date);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

CREATE TABLE IF NOT EXISTS position_legs (
    leg_id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT, strike REAL, option_type TEXT, action TEXT,
    qty INTEGER, entry_price REAL, exit_price REAL,
    entry_bid REAL, entry_ask REAL, entry_delta REAL, entry_gamma REAL,
    entry_vega REAL, entry_theta REAL, entry_iv REAL, entry_oi INTEGER,
    exit_delta REAL, broker_order_id_entry TEXT, broker_order_id_exit TEXT,
    quoted_mid_at_entry REAL, quoted_mid_at_exit REAL,
    leg_status TEXT DEFAULT 'OPEN',
    FOREIGN KEY (position_id) REFERENCES positions(position_id)
);

CREATE INDEX IF NOT EXISTS idx_legs_position ON position_legs(position_id);

CREATE TABLE IF NOT EXISTS intraday_candles (
    candle_id INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_date TEXT, candle_time TEXT, interval_min INTEGER DEFAULT 1,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER, source TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_intraday_candles_unique
    ON intraday_candles(trading_date, candle_time, interval_min);
CREATE INDEX IF NOT EXISTS idx_intraday_candles_date
    ON intraday_candles(trading_date);

CREATE TABLE IF NOT EXISTS option_chain_snapshot (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_time TEXT, trading_date TEXT, expiry TEXT,
    strike REAL, option_type TEXT,
    bid REAL, ask REAL, ltp REAL, oi INTEGER, volume INTEGER,
    iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL,
    data_timestamp TEXT, cycle_id INTEGER,
    spot_at_capture REAL, vix_at_capture REAL, vrp_at_capture REAL,
    adx_at_capture REAL, vwap_dist_at_capture REAL,
    trend_condition_at_capture TEXT,
    volatility_condition_at_capture TEXT,
    direction_at_capture TEXT,
    final_regime_at_capture TEXT,
    confidence_at_capture TEXT
);

CREATE INDEX IF NOT EXISTS idx_chain_snapshot_time
    ON option_chain_snapshot(capture_time);
CREATE INDEX IF NOT EXISTS idx_chain_snapshot_date
    ON option_chain_snapshot(trading_date);
CREATE INDEX IF NOT EXISTS idx_chain_snapshot_expiry
    ON option_chain_snapshot(expiry, strike);

CREATE TABLE IF NOT EXISTS options_chain (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL, date TEXT NOT NULL, time TEXT NOT NULL,
    expiry_date TEXT, strike INTEGER,
    ce_ltp REAL, ce_iv REAL, ce_oi INTEGER, ce_volume INTEGER,
    pe_ltp REAL, pe_iv REAL, pe_oi INTEGER, pe_volume INTEGER,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_oc_ts ON options_chain(timestamp);
CREATE INDEX IF NOT EXISTS idx_oc_strike
    ON options_chain(strike, expiry_date, timestamp);

CREATE TABLE IF NOT EXISTS cycle_log (
    cycle_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_time TEXT, trading_date TEXT,
    spot REAL, vix REAL, vrp REAL, atm_iv_pct REAL, parkinson_rv_pct REAL,
    adx REAL, adx_condition TEXT, vwap REAL, vwap_dist_pct REAL,
    pcr REAL, pcr_change REAL, skew_ratio REAL,
    or_width REAL, or_condition TEXT,
    volatility_condition TEXT, iv_behavior TEXT,
    trend_condition TEXT, direction TEXT, preferred_sell_side TEXT,
    final_regime TEXT, confidence TEXT,
    price_regime_15 TEXT, price_regime_60 TEXT, mtf_aligned INTEGER DEFAULT 0,
    adx_15 REAL, adx_60 REAL, ema_structure TEXT,
    oi_change_pct REAL, skew REAL,
    entry_timing TEXT, action_taken TEXT, no_trade_reason TEXT,
    conditions_met_json TEXT, conditions_not_met_json TEXT,
    open_positions INTEGER, daily_pnl_net REAL,
    vix_regime TEXT, day_mode TEXT, open_position_ids TEXT,
    vrp_percentile REAL, max_pain REAL, atm_straddle_price REAL,
    chain_stale INTEGER DEFAULT 0,
    raw_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_cycle_log_time ON cycle_log(cycle_time);
CREATE INDEX IF NOT EXISTS idx_cycle_log_date ON cycle_log(trading_date);

CREATE TABLE IF NOT EXISTS trade_entries (
    trade_id TEXT PRIMARY KEY, position_id TEXT, strategy_name TEXT,
    entry_time TEXT, trading_date TEXT, day_label TEXT,
    entry_spot REAL, entry_vix REAL, entry_vrp REAL,
    entry_atm_iv REAL, entry_parkinson_rv REAL, entry_adx REAL,
    entry_vwap REAL, entry_vwap_dist REAL, entry_pcr REAL,
    entry_pcr_change REAL, entry_skew_ratio REAL,
    or_width REAL, or_condition TEXT,
    volatility_condition TEXT, iv_behavior TEXT,
    trend_condition TEXT, adx_condition TEXT, direction TEXT,
    vwap_signal TEXT, pcr_signal TEXT, skew_signal TEXT,
    preferred_sell_side TEXT, target_expiry TEXT, actual_dte INTEGER,
    legs_json TEXT, entry_credit REAL, entry_debit REAL, gross_credit REAL,
    total_slippage REAL, entry_costs_pts REAL, entry_costs_rupees REAL,
    stop_premium REAL, target_premium REAL, price_stop_pts INTEGER,
    hard_exit_time TEXT, final_lots INTEGER, max_loss_per_lot REAL,
    total_max_risk REAL, capital_at_entry REAL, daily_pnl_at_entry REAL,
    paper_trade INTEGER DEFAULT 1, selection_reason TEXT,
    final_regime_at_entry TEXT, confidence_at_entry TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS trade_exits (
    exit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT, position_id TEXT, strategy_name TEXT,
    exit_time TEXT, hold_minutes REAL, exit_reason TEXT,
    exit_spot REAL, exit_vix REAL, exit_adx REAL, exit_vwap_dist REAL,
    exit_legs_json TEXT, exit_premium REAL,
    gross_pnl_pts REAL, gross_pnl_rupees REAL,
    exit_slippage REAL, exit_costs_pts REAL, exit_costs_rupees REAL,
    total_costs_rupees REAL, net_pnl_pts REAL, net_pnl_rupees REAL,
    net_pnl_pct REAL, result TEXT, profit_pct_of_credit REAL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS daily_summary (
    trading_date TEXT PRIMARY KEY, day_label TEXT,
    trades_attempted INTEGER, trades_executed INTEGER,
    trades_won INTEGER, trades_lost INTEGER, win_rate_pct REAL,
    gross_pnl_rupees REAL, total_costs_rupees REAL, net_pnl_rupees REAL,
    net_pnl_pct_capital REAL, max_intraday_drawdown REAL,
    max_concurrent_positions INTEGER, stops_fired INTEGER,
    daily_halt_triggered INTEGER,
    vix_open REAL, vix_close REAL, vix_low REAL, vix_high REAL,
    nifty_open REAL, nifty_close REAL, nifty_low REAL, nifty_high REAL,
    or_width REAL, or_condition TEXT, vrp_mean REAL,
    strategies_used_json TEXT, no_trade_reasons_json TEXT,
    avg_hold_minutes REAL, avg_credit_pts REAL, avg_vrp_at_entry REAL,
    profit_factor REAL, capital_start REAL, capital_end REAL,
    capital_change_pct REAL,
    event_day INTEGER DEFAULT 0, event_name TEXT,
    opening_spot REAL, closing_spot REAL, high REAL, low REAL,
    day_range_points REAL, day_range_pct REAL,
    vix_close_val REAL, opening_straddle REAL,
    realized_move REAL, straddle_ratio REAL, dominant_regime TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS expiry_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expiry_date TEXT NOT NULL UNIQUE,
    opening_spot REAL, closing_spot REAL,
    max_pain_at_open INTEGER, max_pain_at_1pm INTEGER,
    final_settlement REAL, error_from_open_mp REAL,
    error_from_1pm_mp REAL, day_range_points REAL,
    opening_straddle REAL, realized_move REAL,
    vix_open REAL, regime_at_open TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS vix_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL, date TEXT NOT NULL, time TEXT NOT NULL,
    weekday INTEGER, vix_value REAL NOT NULL, dte INTEGER,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_vh_ts ON vix_history(timestamp);
CREATE INDEX IF NOT EXISTS idx_vh_date ON vix_history(date);

CREATE TABLE IF NOT EXISTS regime_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL, date TEXT NOT NULL, time TEXT NOT NULL,
    weekday INTEGER, dte INTEGER, day_type TEXT,
    event_day INTEGER DEFAULT 0, event_name TEXT,
    defined_risk_only INTEGER DEFAULT 0,
    volatility_regime TEXT, price_regime TEXT,
    price_regime_15 TEXT, price_regime_60 TEXT,
    mtf_aligned INTEGER DEFAULT 0,
    positioning_regime TEXT, final_regime TEXT,
    confidence TEXT, size_multiplier REAL, raw_size_multiplier REAL,
    vix_level REAL, vix_roc REAL, ivr REAL, iv_hv_ratio REAL,
    straddle_ratio REAL, adx_15 REAL, adx_60 REAL,
    ema_structure TEXT, oi_wall_strength REAL,
    oi_change_pct REAL, skew REAL,
    max_pain_distance REAL, pcr REAL,
    notes TEXT, is_calibrated INTEGER DEFAULT 0,
    calibration_tier INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_rd_ts ON regime_decisions(timestamp);
CREATE INDEX IF NOT EXISTS idx_rd_date ON regime_decisions(date);

CREATE TABLE IF NOT EXISTS calibration_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    calibrated_at TEXT NOT NULL,
    n_tuesday_expiries INTEGER, n_trading_days INTEGER,
    vix_p25 REAL, vix_p50 REAL, vix_p75 REAL, vix_p90 REAL,
    vix_roc_emergency REAL,
    ivr_sell_threshold REAL, ivr_buy_threshold REAL,
    iv_hv_sell_threshold REAL, straddle_ratio_sell REAL,
    oi_wall_strong REAL,
    tuesday_avg_range REAL, monday_avg_range REAL,
    thursday_avg_range REAL, friday_avg_range REAL,
    wednesday_avg_range REAL,
    skew_bearish_threshold REAL DEFAULT 3.0,
    skew_bullish_threshold REAL DEFAULT -1.5,
    oi_buildup_threshold REAL DEFAULT 0.08,
    oi_unwind_threshold REAL DEFAULT -0.08,
    oi_wall_strong_cal REAL DEFAULT 2.8,
    oi_wall_moderate_cal REAL DEFAULT 1.7,
    calibration_tier INTEGER DEFAULT 0,
    is_valid INTEGER DEFAULT 0, notes TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS strategy_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_time TEXT, trading_date TEXT,
    action TEXT, strategy_name TEXT, reason TEXT,
    params_json TEXT, signals_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_strategy_decisions_time
    ON strategy_decisions(decision_time);

CREATE TABLE IF NOT EXISTS audit_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_time TEXT, level TEXT, logger_name TEXT,
    message TEXT, context_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_log_time ON audit_log(log_time);
CREATE INDEX IF NOT EXISTS idx_audit_log_level ON audit_log(level);

CREATE TABLE IF NOT EXISTS api_call_log (
    call_id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_time TEXT, category TEXT, endpoint TEXT, method TEXT,
    status_code INTEGER, response_time_ms REAL,
    rate_limited INTEGER DEFAULT 0, error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_api_call_time ON api_call_log(call_time);

CREATE TABLE IF NOT EXISTS instrument_master (
    instrument_key TEXT PRIMARY KEY,
    trading_symbol TEXT, exchange TEXT, instrument_type TEXT,
    expiry TEXT, strike REAL, option_type TEXT,
    lot_size INTEGER, tick_size REAL, last_updated TEXT
);

CREATE TABLE IF NOT EXISTS backtest_calibration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT NOT NULL,
    cell_key TEXT, n_trades INTEGER,
    win_rate_pct REAL, avg_pnl_rs REAL, sharpe REAL,
    recommended_stop_multiplier REAL,
    recommended_min_credit_pts REAL,
    cost_coverage_floor_pts REAL,
    recommended_size_multiplier REAL,
    kelly_fraction REAL,
    data_quality TEXT, action TEXT, has_edge INTEGER,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_backtest_cal_cell
    ON backtest_calibration(cell_key);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    spot REAL,
    vix REAL,
    atm_iv REAL,
    skew REAL,
    oi_change_pct REAL,
    resistance_oi INTEGER,
    support_oi INTEGER,
    total_ce_oi INTEGER,
    total_pe_oi INTEGER,
    pcr REAL,
    adx_15 REAL,
    vwap_dist_pct REAL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_ms_ts ON market_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_ms_date ON market_snapshots(date);
"""

MIGRATION_SQL = [
    "ALTER TABLE cycle_log ADD COLUMN final_regime TEXT",
    "ALTER TABLE cycle_log ADD COLUMN confidence TEXT",
    "ALTER TABLE cycle_log ADD COLUMN price_regime_15 TEXT",
    "ALTER TABLE cycle_log ADD COLUMN price_regime_60 TEXT",
    "ALTER TABLE cycle_log ADD COLUMN mtf_aligned INTEGER DEFAULT 0",
    "ALTER TABLE cycle_log ADD COLUMN adx_15 REAL",
    "ALTER TABLE cycle_log ADD COLUMN adx_60 REAL",
    "ALTER TABLE cycle_log ADD COLUMN ema_structure TEXT",
    "ALTER TABLE cycle_log ADD COLUMN oi_change_pct REAL",
    "ALTER TABLE cycle_log ADD COLUMN skew REAL",
    "ALTER TABLE trade_entries ADD COLUMN final_regime_at_entry TEXT",
    "ALTER TABLE trade_entries ADD COLUMN confidence_at_entry TEXT",
    "ALTER TABLE option_chain_snapshot ADD COLUMN final_regime_at_capture TEXT",
    "ALTER TABLE option_chain_snapshot ADD COLUMN confidence_at_capture TEXT",
    "ALTER TABLE daily_summary ADD COLUMN event_day INTEGER DEFAULT 0",
    "ALTER TABLE daily_summary ADD COLUMN event_name TEXT",
    "ALTER TABLE daily_summary ADD COLUMN opening_spot REAL",
    "ALTER TABLE daily_summary ADD COLUMN closing_spot REAL",
    "ALTER TABLE daily_summary ADD COLUMN high REAL",
    "ALTER TABLE daily_summary ADD COLUMN low REAL",
    "ALTER TABLE daily_summary ADD COLUMN day_range_points REAL",
    "ALTER TABLE daily_summary ADD COLUMN day_range_pct REAL",
    "ALTER TABLE daily_summary ADD COLUMN vix_close_val REAL",
    "ALTER TABLE daily_summary ADD COLUMN opening_straddle REAL",
    "ALTER TABLE daily_summary ADD COLUMN realized_move REAL",
    "ALTER TABLE daily_summary ADD COLUMN straddle_ratio REAL",
    "ALTER TABLE daily_summary ADD COLUMN dominant_regime TEXT",
    "ALTER TABLE positions ADD COLUMN stop_tightened_for_delta INTEGER DEFAULT 0",
    "ALTER TABLE session_state ADD COLUMN or_computed INTEGER DEFAULT 0",
    "ALTER TABLE session_state ADD COLUMN session_initialized INTEGER DEFAULT 0",
    "ALTER TABLE session_state ADD COLUMN vix_regime_last_checked TEXT",
    "ALTER TABLE session_state ADD COLUMN prev_spot REAL",
    "ALTER TABLE session_state ADD COLUMN prev_vix REAL",
    "ALTER TABLE session_state ADD COLUMN parkinson_rv_pct REAL",
    "ALTER TABLE session_state ADD COLUMN parkinson_rv_computed_date TEXT",
    "ALTER TABLE session_state ADD COLUMN vwap_valid INTEGER DEFAULT 0",
    "ALTER TABLE session_state ADD COLUMN expiry_last_checked TEXT",
    "ALTER TABLE session_state ADD COLUMN pre_event_spot REAL",
    "ALTER TABLE session_state ADD COLUMN pre_event_iv REAL",
    "ALTER TABLE session_state ADD COLUMN event_announcement_time TEXT",
    "ALTER TABLE session_state ADD COLUMN last_stop_signal_combo TEXT",
    "ALTER TABLE session_state ADD COLUMN gap_fade_opportunity INTEGER DEFAULT 0",
    "ALTER TABLE calibration_state ADD COLUMN skew_bearish_threshold REAL DEFAULT 3.0",
    "ALTER TABLE calibration_state ADD COLUMN skew_bullish_threshold REAL DEFAULT -1.5",
    "ALTER TABLE calibration_state ADD COLUMN oi_buildup_threshold REAL DEFAULT 0.08",
    "ALTER TABLE calibration_state ADD COLUMN oi_unwind_threshold REAL DEFAULT -0.08",
    "ALTER TABLE calibration_state ADD COLUMN oi_wall_strong_cal REAL DEFAULT 2.8",
    "ALTER TABLE calibration_state ADD COLUMN oi_wall_moderate_cal REAL DEFAULT 1.7",
    "ALTER TABLE calibration_state ADD COLUMN calibration_tier INTEGER DEFAULT 0",
    "ALTER TABLE regime_decisions ADD COLUMN calibration_tier INTEGER DEFAULT 0",
    "ALTER TABLE market_snapshots ADD COLUMN vrp REAL",
    "ALTER TABLE market_snapshots ADD COLUMN parkinson_rv REAL",
    "ALTER TABLE market_snapshots ADD COLUMN volatility_condition TEXT",
    "ALTER TABLE market_snapshots ADD COLUMN trend_condition TEXT",
    "ALTER TABLE market_snapshots ADD COLUMN direction TEXT",
    "ALTER TABLE cycle_log ADD COLUMN chain_stale INTEGER DEFAULT 0",
    "ALTER TABLE position_legs ADD COLUMN quoted_mid_at_entry REAL",
    "ALTER TABLE position_legs ADD COLUMN quoted_mid_at_exit REAL",
    "ALTER TABLE calibration_state ADD COLUMN vrp_sell_threshold REAL DEFAULT 3.0",
    "ALTER TABLE calibration_state ADD COLUMN vrp_fair_threshold REAL DEFAULT 1.5",
    "ALTER TABLE calibration_state ADD COLUMN day_size_monday REAL DEFAULT 0.75",
    "ALTER TABLE calibration_state ADD COLUMN day_size_tuesday REAL DEFAULT 0.60",
    "ALTER TABLE calibration_state ADD COLUMN day_size_wednesday REAL DEFAULT 0.75",
    "ALTER TABLE calibration_state ADD COLUMN day_size_thursday REAL DEFAULT 0.75",
    "ALTER TABLE calibration_state ADD COLUMN day_size_friday REAL DEFAULT 0.50",
]


class Database:

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._init_schema()
        self._run_migrations()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA_SQL)
            self._conn.commit()

    def _run_migrations(self) -> None:
        with self._lock:
            existing_tables = {
                row[0] for row in
                self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            for sql in MIGRATION_SQL:
                try:
                    table_name = sql.split("ALTER TABLE")[1].split("ADD COLUMN")[0].strip()
                    if table_name not in existing_tables:
                        continue
                    col_name = sql.split("ADD COLUMN")[1].strip().split()[0]
                    existing_cols = {
                        row[1] for row in
                        self._conn.execute(f"PRAGMA table_info({table_name})").fetchall()
                    }
                    if col_name not in existing_cols:
                        self._conn.execute(sql)
                except Exception:
                    pass
            self._conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def executemany(self, sql: str, param_list: list) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.executemany(sql, param_list)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: tuple = ()) -> list:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def query_one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None

    def insert(self, table: str, data: dict) -> int:
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
        with self._lock:
            cur = self._conn.execute(sql, tuple(data.values()))
            self._conn.commit()
            return cur.lastrowid

    def update(self, table: str, data: dict, where: dict) -> int:
        set_clause = ", ".join(f"{k}=?" for k in data)
        where_clause = " AND ".join(f"{k}=?" for k in where)
        sql = f"UPDATE {table} SET {set_clause} WHERE {where_clause}"
        params = tuple(data.values()) + tuple(where.values())
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount

    def upsert(self, table: str, key: dict, data: dict) -> int:
        where_clause = " AND ".join(f"{k}=?" for k in key)
        existing = self.query_one(
            f"SELECT 1 FROM {table} WHERE {where_clause}",
            tuple(key.values())
        )
        if existing:
            return self.update(table, data, key)
        return self.insert(table, {**key, **data})

    def table_exists(self, name: str) -> bool:
        row = self.query_one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        )
        return row is not None

    def column_exists(self, table: str, column: str) -> bool:
        cols = self.query(f"PRAGMA table_info({table})")
        return any(c["name"] == column for c in cols)

    def ensure_column(self, table: str, column: str, coltype: str) -> None:
        if self.table_exists(table) and not self.column_exists(table, column):
            try:
                self.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            except Exception:
                pass

    def log_audit(self, level: str, logger_name: str, message: str,
                   context: Optional[dict] = None) -> None:
        try:
            self.insert("audit_log", {
                "log_time": now_ist().isoformat(),
                "level": level,
                "logger_name": logger_name,
                "message": message,
                "context_json": json.dumps(context, default=str) if context else None,
            })
        except Exception:
            pass

    def log_api_call(self, category: str, endpoint: str, method: str,
                      status_code: Optional[int], response_time_ms: float,
                      rate_limited: bool = False,
                      error_message: Optional[str] = None) -> None:
        try:
            self.insert("api_call_log", {
                "call_time": now_ist().isoformat(),
                "category": category,
                "endpoint": endpoint,
                "method": method,
                "status_code": status_code,
                "response_time_ms": response_time_ms,
                "rate_limited": 1 if rate_limited else 0,
                "error_message": error_message,
            })
        except Exception:
            pass

    def get_connection(self):
        return self._conn

    def get_latest_calibration(self):
        return self.query_one(
            "SELECT * FROM calibration_state WHERE is_valid=1 ORDER BY calibrated_at DESC LIMIT 1"
        )

    def count_tuesday_expiries(self):
        row = self.query_one(
            "SELECT COUNT(*) as cnt FROM daily_summary WHERE day_label='TUESDAY'"
        )
        return row["cnt"] if row else 0

    def count_trading_days(self):
        row = self.query_one(
            "SELECT COUNT(DISTINCT trading_date) as cnt FROM daily_summary"
        )
        return row["cnt"] if row else 0

    def get_vix_history(self, days=365, from_date=None):
        try:
            import pandas as pd
            from datetime import date, timedelta
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            if from_date and from_date > cutoff:
                cutoff = from_date
            rows = self.query(
                "SELECT * FROM vix_history WHERE date >= ? ORDER BY timestamp",
                (cutoff,),
            )
            return pd.DataFrame(rows) if rows else pd.DataFrame()
        except Exception:
            import pandas as pd
            return pd.DataFrame()

    def get_daily_summary(self, days=365):
        try:
            import pandas as pd
            from datetime import date, timedelta
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            rows = self.query(
                "SELECT *, strftime('%w', trading_date) as weekday "
                "FROM daily_summary WHERE trading_date >= ? ORDER BY trading_date",
                (cutoff,),
            )
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows)
            df["weekday"] = pd.to_numeric(df["weekday"], errors="coerce")
            return df
        except Exception:
            import pandas as pd
            return pd.DataFrame()

    def get_spot_history(self, days=30):
        try:
            import pandas as pd
            from datetime import date, timedelta
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            rows = self.query(
                "SELECT trading_date as date, candle_time as time, "
                "open, high, low, close, volume "
                "FROM intraday_candles WHERE trading_date >= ? "
                "ORDER BY trading_date, candle_time",
                (cutoff,),
            )
            return pd.DataFrame(rows) if rows else pd.DataFrame()
        except Exception:
            import pandas as pd
            return pd.DataFrame()

    def get_connection(self):
        return self._conn

    def get_latest_calibration(self):
        return self.query_one(
            "SELECT * FROM calibration_state WHERE is_valid=1 ORDER BY calibrated_at DESC LIMIT 1"
        )

    def count_tuesday_expiries(self):
        row = self.query_one(
            "SELECT COUNT(*) as cnt FROM daily_summary WHERE day_label='TUESDAY'"
        )
        return row["cnt"] if row else 0

    def count_trading_days(self):
        row = self.query_one(
            "SELECT COUNT(DISTINCT trading_date) as cnt FROM daily_summary"
        )
        return row["cnt"] if row else 0

    def get_vix_history(self, days=365, from_date=None):
        try:
            import pandas as pd
            from datetime import date, timedelta
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            if from_date and from_date > cutoff:
                cutoff = from_date
            rows = self.query(
                "SELECT * FROM vix_history WHERE date >= ? ORDER BY timestamp",
                (cutoff,),
            )
            return pd.DataFrame(rows) if rows else pd.DataFrame()
        except Exception:
            import pandas as pd
            return pd.DataFrame()

    def get_daily_summary(self, days=365):
        try:
            import pandas as pd
            from datetime import date, timedelta
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            rows = self.query(
                "SELECT *, CAST(strftime('%w', trading_date) AS INTEGER) as weekday_sql "
                "FROM daily_summary WHERE trading_date >= ? ORDER BY trading_date",
                (cutoff,),
            )
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows)
            df["weekday_sql"] = pd.to_numeric(df["weekday_sql"], errors="coerce")
            df["weekday"] = df["weekday_sql"].apply(
                lambda w: (int(w) - 1) % 7 if pd.notna(w) else float("nan")
            )
            return df
        except Exception:
            import pandas as pd
            return pd.DataFrame()

    def get_spot_history(self, days=30):
        try:
            import pandas as pd
            from datetime import date, timedelta
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            rows = self.query(
                "SELECT trading_date as date, candle_time as time, "
                "open, high, low, close, volume "
                "FROM intraday_candles WHERE trading_date >= ? "
                "ORDER BY trading_date, candle_time",
                (cutoff,),
            )
            return pd.DataFrame(rows) if rows else pd.DataFrame()
        except Exception:
            import pandas as pd
            return pd.DataFrame()

    def get_market_snapshots(self, days=365):
        try:
            import pandas as pd
            from datetime import date, timedelta
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            rows = self.query(
                "SELECT * FROM market_snapshots WHERE date >= ? ORDER BY timestamp",
                (cutoff,),
            )
            return pd.DataFrame(rows) if rows else pd.DataFrame()
        except Exception:
            import pandas as pd
            return pd.DataFrame()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


class SQLiteAuditHandler(logging.Handler):

    def __init__(self, db: Database):
        super().__init__()
        self.db = db

    def emit(self, record: logging.LogRecord) -> None:
        try:
            context = getattr(record, "context", None)
            self.db.log_audit(
                level=record.levelname,
                logger_name=record.name,
                message=record.getMessage(),
                context=context,
            )
        except Exception:
            self.handleError(record)


def setup_logging(db: Database, log_dir: Path,
                   level: int = logging.INFO) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("nifty_algo")
    logger.setLevel(level)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-24s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(level)
    logger.addHandler(console)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_dir / "nifty_algo_audit.log",
        when="midnight", backupCount=90, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    sqlite_handler = SQLiteAuditHandler(db)
    sqlite_handler.setFormatter(fmt)
    sqlite_handler.setLevel(logging.DEBUG)
    logger.addHandler(sqlite_handler)

    logger.propagate = False
    return logger


class UpstoxAPIError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None,
                 response_body: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class UpstoxClient:

    def __init__(self, config: Config, rate_limiter: RateLimiter,
                 db: Database, logger: logging.Logger):
        self.config = config
        self.rate_limiter = rate_limiter
        self.db = db
        self.logger = logger

        self.session = requests.Session()
        try:
            retry_strategy = Retry(
                total=config.max_retries, backoff_factor=0.5,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["GET", "POST", "DELETE"],
                respect_retry_after_header=True,
            )
        except TypeError:
            retry_strategy = Retry(
                total=config.max_retries, backoff_factor=0.5,
                status_forcelist=[429, 500, 502, 503, 504],
                method_whitelist=["GET", "POST", "DELETE"],
            )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.headers.update({
            "Accept": "application/json",
            "Authorization": f"Bearer {config.upstox_access_token}",
        })

    def _request(self, method: str, endpoint_key: str, category: str = "default",
                  path_params: Optional[dict] = None, params: Optional[dict] = None,
                  json_body: Optional[dict] = None) -> dict:
        path = API_ENDPOINTS[endpoint_key]
        if path_params:
            path = path.format(**path_params)
        url = f"{UPSTOX_BASE_URL}{path}"

        wait_time = self.rate_limiter.wait_if_needed(category)
        if wait_time > 0:
            self.logger.debug(f"Rate limiter delayed {endpoint_key} by {wait_time:.2f}s")

        start = time.monotonic()
        status_code = None
        try:
            resp = self.session.request(
                method, url, params=params, json=json_body,
                timeout=self.config.request_timeout_seconds,
            )
            status_code = resp.status_code
            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code == 429:
                msg = "Rate limited by Upstox (HTTP 429)"
                self.logger.warning(f"{endpoint_key}: {msg}")
                self.db.log_api_call(category, endpoint_key, method, status_code,
                                      elapsed_ms, rate_limited=True, error_message=msg)
                raise UpstoxAPIError(msg, status_code=429, response_body=resp.text)

            resp.raise_for_status()
            data = resp.json()
            self.db.log_api_call(category, endpoint_key, method, status_code, elapsed_ms)
            return data

        except requests.exceptions.RequestException as e:
            elapsed_ms = (time.monotonic() - start) * 1000
            error_message = str(e)
            self.logger.error(f"API call failed: {endpoint_key} — {error_message}")
            self.db.log_api_call(category, endpoint_key, method, status_code,
                                  elapsed_ms, error_message=error_message)
            raise UpstoxAPIError(error_message, status_code=status_code) from e

    def validate_token(self) -> bool:
        try:
            data = self._request("GET", "profile", category="default")
            user_name = data.get("data", {}).get("user_name", "unknown")
            self.logger.info(f"Upstox token validated. User: {user_name}")
            return True
        except UpstoxAPIError as e:
            self.logger.error(f"Upstox token validation FAILED: {e}")
            return False

    def get_ltp(self, instrument_keys) -> dict:
        if isinstance(instrument_keys, str):
            instrument_keys = [instrument_keys]
        params = {"instrument_key": ",".join(instrument_keys)}
        data = self._request("GET", "ltp", category="quote", params=params)
        return data.get("data", {})

    def get_full_quote(self, instrument_keys: list) -> dict:
        params = {"instrument_key": ",".join(instrument_keys)}
        data = self._request("GET", "quotes", category="quote", params=params)
        return data.get("data", {})

    def get_historical_candles(self, instrument_key: str, interval: str,
                                from_date: str, to_date: str) -> list:
        path_params = {
            "instrument_key": instrument_key, "interval": interval,
            "to_date": to_date, "from_date": from_date,
        }
        data = self._request("GET", "historical_candle", category="historical",
                              path_params=path_params)
        return data.get("data", {}).get("candles", [])

    def get_intraday_candles(self, instrument_key: str, interval: str) -> list:
        path_params = {"instrument_key": instrument_key, "interval": interval}
        data = self._request("GET", "intraday_candle", category="historical",
                              path_params=path_params)
        return data.get("data", {}).get("candles", [])

    def get_option_contracts(self, instrument_key: str,
                              expiry_date: Optional[str] = None) -> list:
        params = {"instrument_key": instrument_key}
        if expiry_date:
            params["expiry_date"] = expiry_date
        data = self._request("GET", "option_contracts", category="chain", params=params)
        return data.get("data", [])

    def get_option_chain(self, instrument_key: str, expiry_date: str) -> list:
        params = {"instrument_key": instrument_key, "expiry_date": expiry_date}
        data = self._request("GET", "option_chain", category="chain", params=params)
        return data.get("data", [])

    def get_positions(self) -> list:
        data = self._request("GET", "positions", category="default")
        return data.get("data", [])

    def get_funds_and_margin(self) -> dict:
        data = self._request("GET", "funds_margin", category="default")
        return data.get("data", {})

    def get_order_details(self, order_id: str) -> dict:
        params = {"order_id": order_id}
        data = self._request("GET", "order_details", category="order", params=params)
        return data.get("data", {})

    def place_order(self, instrument_token: str, quantity: int,
                     transaction_type: str, order_type: str = "MARKET",
                     product: str = "I", price: float = 0,
                     trigger_price: float = 0, validity: str = "DAY",
                     tag: str = "nifty_algo") -> dict:
        if self.config.paper_trade_mode:
            raise RuntimeError(
                "place_order() called while PAPER_TRADE_MODE=True. "
                "Refusing to place a real order."
            )
        body = {
            "quantity": quantity, "product": product, "validity": validity,
            "price": price, "instrument_token": instrument_token,
            "order_type": order_type, "transaction_type": transaction_type,
            "trigger_price": trigger_price, "is_amo": False, "tag": tag,
        }
        data = self._request("POST", "place_order", category="order", json_body=body)
        return data.get("data", {})

    def cancel_order(self, order_id: str) -> dict:
        if self.config.paper_trade_mode:
            raise RuntimeError(
                "cancel_order() called while PAPER_TRADE_MODE=True. Refusing."
            )
        params = {"order_id": order_id}
        data = self._request("DELETE", "cancel_order", category="order", params=params)
        return data.get("data", {})


def print_section(title: str, char: str = "=", width: int = 78) -> None:
    print(char * width)
    print(title.center(width))
    print(char * width)


def print_kv_table(data: dict, title: Optional[str] = None, width: int = 78) -> None:
    if title:
        print(f"\n--- {title} ---")
    for k, v in data.items():
        if isinstance(v, float):
            v_str = f"{v:,.4f}"
        else:
            v_str = str(v)
        print(f"  {k:<32}: {v_str}")


def _self_test() -> None:
    print_section("NIFTY ALGO — CORE INFRASTRUCTURE SELF-TEST")

    config = load_config()
    print(repr(config))
    print_kv_table({
        "paper_trade_mode": config.paper_trade_mode,
        "starting_capital": config.starting_capital,
        "lot_size": config.lot_size,
        "nifty_strike_step": config.nifty_strike_step,
        "max_risk_per_trade_pct": config.max_risk_per_trade_pct,
        "max_daily_loss_pct": config.max_daily_loss_pct,
        "trading_window_start": config.trading_window_start,
        "trading_window_last_entry": config.trading_window_last_entry,
        "hard_exit_time": config.hard_exit_time,
        "adx_period": config.adx_period,
        "adx_trend_threshold": config.adx_trend_threshold,
        "adx_strong_threshold": config.adx_strong_threshold,
        "ema_fast": config.ema_fast,
        "ema_slow": config.ema_slow,
        "mtf_resample_15": config.mtf_resample_15,
        "mtf_resample_60": config.mtf_resample_60,
        "skew_bearish_threshold": config.skew_bearish_threshold,
        "oi_change_lookback_min": config.oi_change_lookback_min,
        "regime_confirm_cycles": config.regime_confirm_cycles,
        "event_size_multiplier": config.event_size_multiplier,
        "tuesday_early_exit_enabled": config.tuesday_early_exit_enabled,
        "db_path": str(config.db_path),
        "log_dir": str(config.log_dir),
    }, title="Configuration")

    print_section("EXPIRY CALENDAR TEST")
    today = today_ist()
    print_kv_table({
        "today": today,
        "is_holiday": ExpiryCalendar.is_holiday(today),
        "is_event_day": ExpiryCalendar.is_event_day(today),
        "next_expiry": ExpiryCalendar.get_next_expiry(today),
        "dte": ExpiryCalendar.get_dte(today),
        "day_type": ExpiryCalendar.get_day_type(today),
        "is_monthly_expiry": ExpiryCalendar.is_monthly_expiry(today),
        "nse_holidays_loaded": len(get_nse_holidays()),
        "events_loaded": len(get_high_impact_events()),
    })

    db = Database(config.db_path)
    logger = setup_logging(
        db, config.log_dir,
        level=getattr(logging, config.log_level.upper(), logging.INFO)
    )
    logger.info("Core infrastructure self-test starting")

    print_section("DATABASE SCHEMA CHECK")
    tables = db.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    for t in tables:
        print(f"  - {t['name']}")
    logger.info(f"Database schema initialized with {len(tables)} tables at {config.db_path}")

    if not config.upstox_access_token:
        logger.warning("UPSTOX_ACCESS_TOKEN not set — skipping live API validation.")
        print_section("UPSTOX TOKEN: NOT CONFIGURED")
    else:
        rate_limiter = RateLimiter(config.rate_limits)
        client = UpstoxClient(config, rate_limiter, db, logger)
        ok = client.validate_token()
        print_section("UPSTOX TOKEN: VALID" if ok else "UPSTOX TOKEN: INVALID / EXPIRED")
        if not ok:
            logger.warning("Upstox access token is invalid or expired. Regenerate and update env.txt.")

    logger.info("Core infrastructure self-test complete.")
    print_section("SELF-TEST COMPLETE")
    db.close()


if __name__ == "__main__":
    _self_test()