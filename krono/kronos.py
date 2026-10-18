"""
Kronos Nifty 15-Minute Intraday Signal Generator
================================================
Modes:
  python kronos.py                                   → backtest
  python kronos.py --live                            → live signal generator
  python kronos.py --test-bar "2026-10-16 14:30"     → process one historical bar
                                                        as if live (no state writes)
  python kronos.py --show-state
  python kronos.py --report-trade PnL=+1500
  python kronos.py --reset-day

DISCLAIMER: Signal generator only. You place every order. Paper-trade 20–30
sessions per PROJECT.txt §15 before any real capital.
"""

import os
import sys
import time
import json
import copy
import subprocess
import gzip
import argparse
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, time as dtime
from typing import Optional, List, Dict

import numpy as np
import pandas as pd
import pytz
import requests


# ===========================================================================
# Bootstrap
# ===========================================================================
def _module_exists(name):
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def _ensure_dependencies():
    if os.environ.get("KRONOS_AUTO_INSTALL", "1") == "0":
        return
    required = {"torch": "torch", "huggingface_hub": "huggingface_hub",
                "safetensors": "safetensors", "einops": "einops"}
    missing = [pkg for mod, pkg in required.items() if not _module_exists(mod)]
    if not missing:
        return
    print(f"[bootstrap] Installing: {missing}")
    subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", *missing],
                   check=True)


_ensure_dependencies()

KRONOS_REPO = "https://github.com/shiyu-coder/Kronos.git"
KRONOS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Kronos")

try:
    from model import Kronos, KronosTokenizer, KronosPredictor
except ImportError:
    if not os.path.isdir(KRONOS_DIR):
        print(f"[bootstrap] Cloning Kronos into {KRONOS_DIR} …")
        subprocess.run(["git", "clone", "--depth", "1", KRONOS_REPO, KRONOS_DIR],
                       check=True)
    for p in (KRONOS_DIR, os.path.join(KRONOS_DIR, "model")):
        if p not in sys.path:
            sys.path.insert(0, p)
    from model import Kronos, KronosTokenizer, KronosPredictor
    print("[bootstrap] Kronos imported via path injection.")

TZ = pytz.timezone("Asia/Kolkata")
LOG = logging.getLogger("kronos_signals")


# ===========================================================================
# Config
# ===========================================================================
@dataclass
class Config:
    instrument: str = "NIFTY_FUT"
    timezone: str = "Asia/Kolkata"
    lot_size: int = 75
    tick_size: float = 0.05

    model_id: str = "NeoQuasar/Kronos-small"
    tokenizer_id: str = "NeoQuasar/Kronos-Tokenizer-base"
    max_context: int = 512
    lookback: int = 256
    pred_len: int = 1
    temperature: float = 1.0
    top_p: float = 0.9
    sample_count: int = 1

    atr_period: int = 14
    ema_fast: int = 20
    ema_slow: int = 50
    vol_lookback_sessions: int = 60
    vol_percentile: float = 95.0

    base_threshold: float = 0.0010
    cost_threshold_multiplier: float = 2.0
    adverse_move_atr_mult: float = 0.25

    entry_block_first_minutes: int = 30
    stop_atr_mult: float = 1.0
    target_atr_mult: float = 1.5
    max_holding_bars: int = 4
    final_exit_time: dtime = dtime(15, 15)

    risk_per_trade: float = 0.0025
    daily_loss_limit: float = 0.0050
    max_entries_per_day: int = 2
    max_consecutive_losses: int = 2

    brokerage_per_order: float = 20.0
    exchange_txn_rate: float = 0.0000173
    gst_rate: float = 0.18
    stamp_duty_rate: float = 0.00002
    sebi_rate: float = 0.000001
    slippage_ticks: int = 1

    initial_equity: float = 2_000_000.0
    history_days: int = 30
    progress_every: int = 25
    max_ohlc_repair_pct: float = 0.002
    run_all_baselines: bool = False

    # Live mode
    live_equity: float = 200_000.0
    live_fetch_days: int = 90
    option_chain_underlying: str = "NSE_INDEX|Nifty 50"
    option_spread_max_pct: float = 0.02
    option_exchange_txn_rate: float = 0.00035
    option_stt_rate: float = 0.000625
    option_stamp_rate: float = 0.00003
    state_file: str = "live_state.json"
    signal_log_file: str = "signals.jsonl"
    candle_fetch_retry: int = 3
    candle_fetch_retry_delay_sec: float = 2.0

    # Session times
    session_open: dtime = dtime(9, 15)
    session_close: dtime = dtime(15, 30)
    # How long after close to keep polling for the last bar
    session_close_buffer_sec: int = 300
    bar_close_grace_sec: int = 5


INSTRUMENT_MASTER_URL = (
    "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
)


# ===========================================================================
# Env
# ===========================================================================
def load_env_file(path="env.txt") -> Dict[str, str]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path} not found.")
    env = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def get_access_token(env) -> str:
    token = env.get("UPSTOX_ACCESS_TOKEN") or os.getenv("UPSTOX_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN missing in env.txt.")
    return token


# ===========================================================================
# Upstox client
# ===========================================================================
class UpstoxClient:
    def __init__(self, access_token):
        self.token = access_token
        self.headers = {"Accept": "application/json",
                        "Authorization": f"Bearer {access_token}"}
        self.lot_size = None

    def _get(self, url, params=None, timeout=60):
        r = requests.get(url, headers=self.headers, params=params, timeout=timeout)
        if r.status_code == 401:
            raise PermissionError(
                "Upstox 401: access token expired. Refresh UPSTOX_ACCESS_TOKEN "
                "in env.txt and restart.")
        if r.status_code != 200:
            raise RuntimeError(f"Upstox {r.status_code}: {r.text}")
        return r.json()

    def get_current_nifty_futures_key(self) -> str:
        LOG.info("Downloading instrument master …")
        r = requests.get(INSTRUMENT_MASTER_URL, timeout=120)
        r.raise_for_status()
        instruments = json.loads(gzip.decompress(r.content))

        candidates = []
        for ins in instruments:
            if ins.get("segment") != "NSE_FO": continue
            if ins.get("instrument_type") != "FUT": continue
            name = (ins.get("name") or "").upper().strip()
            symbol = (ins.get("trading_symbol") or "").upper().strip()
            if name == "NIFTY":
                pass
            elif symbol.startswith("NIFTY") and not any(
                    tag in symbol for tag in
                    ("BANK", "FINNIFTY", "MIDCP", "NXT50", "NEXT50")):
                pass
            else:
                continue
            expiry = ins.get("expiry")
            if not expiry: continue
            candidates.append((expiry, ins["instrument_key"], ins.get("lot_size")))

        if not candidates:
            raise RuntimeError("No NIFTY futures found.")
        candidates.sort(key=lambda x: x[0])
        expiry, key, lot = candidates[0]
        self.lot_size = int(lot) if lot else 75
        LOG.info(f"Selected {key} expiry={pd.to_datetime(expiry, unit='ms').date()} "
                 f"lot={self.lot_size}")
        return key

    def fetch_candles(self, instrument_key, from_date, to_date,
                      unit="minutes", interval="15") -> pd.DataFrame:
        start = datetime.strptime(from_date, "%Y-%m-%d")
        end = datetime.strptime(to_date, "%Y-%m-%d")
        max_days = 30
        rows = []
        cur = start
        while cur <= end:
            ce = min(cur + timedelta(days=max_days - 1), end)
            cf, ct = cur.strftime("%Y-%m-%d"), ce.strftime("%Y-%m-%d")
            url = (f"https://api.upstox.com/v3/historical-candle/"
                   f"{requests.utils.quote(instrument_key, safe='')}/"
                   f"{unit}/{interval}/{ct}/{cf}")
            LOG.info(f"Fetching candles {cf} → {ct}")
            data = self._get(url).get("data", {}).get("candles", [])
            for c in reversed(data):
                ts, o, h, l, cl, vol, oi = c[:7]
                rows.append(dict(
                    timestamp=pd.to_datetime(ts),
                    open=float(o), high=float(h), low=float(l), close=float(cl),
                    volume=float(vol) if vol is not None else 0.0,
                ))
            cur = ce + timedelta(days=1)
        if not rows:
            raise RuntimeError("No candles returned.")
        df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
        df["timestamp"] = df["timestamp"].dt.tz_convert(TZ)
        return df.drop_duplicates(subset="timestamp").reset_index(drop=True)

    def fetch_option_expiries(self, underlying_key) -> List[str]:
        data = self._get("https://api.upstox.com/v2/option/contract",
                         params={"instrument_key": underlying_key})
        return sorted({row["expiry"] for row in data.get("data", [])
                       if row.get("expiry")})

    def fetch_option_chain(self, underlying_key, expiry_date) -> List[Dict]:
        data = self._get("https://api.upstox.com/v2/option/chain",
                         params={"instrument_key": underlying_key,
                                 "expiry_date": expiry_date})
        return data.get("data", [])


# ===========================================================================
# Validation + indicators
# ===========================================================================
def validate_candles(df):
    if df.empty: raise ValueError("Empty DataFrame.")
    req = {"open", "high", "low", "close", "timestamp"}
    if not req.issubset(df.columns):
        raise ValueError(f"Missing: {req - set(df.columns)}")
    if not df["timestamp"].is_monotonic_increasing:
        raise ValueError("Timestamps not ordered.")
    if df["timestamp"].duplicated().any():
        raise ValueError("Duplicate timestamps.")
    if (df[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("Non-positive OHLC.")
    if (df["high"] < df["open"]).any() or (df["high"] < df["close"]).any():
        raise ValueError("High below open or close.")
    if (df["high"] < df["low"]).any(): raise ValueError("High below low.")
    if (df["low"] > df["open"]).any() or (df["low"] > df["close"]).any():
        raise ValueError("Low above open or close.")


def calculate_atr(df, period=14):
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev).abs(),
                    (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def add_indicators(df, cfg):
    out = df.copy()
    out["atr14"] = calculate_atr(out, cfg.atr_period)
    out["ema20"] = out["close"].ewm(span=cfg.ema_fast, adjust=False).mean()
    out["ema50"] = out["close"].ewm(span=cfg.ema_slow, adjust=False).mean()
    out["session"] = out["timestamp"].dt.date
    session_atr = out.groupby("session")["atr14"].last().sort_index()
    rolling = session_atr.shift(1).rolling(
        window=cfg.vol_lookback_sessions, min_periods=10
    ).quantile(cfg.vol_percentile / 100.0)
    out["atr_p95_cutoff"] = out["session"].map(rolling.to_dict())
    return out


# ===========================================================================
# Forecaster
# ===========================================================================
class KronosForecaster:
    def __init__(self, cfg):
        LOG.info("Loading Kronos …")
        self.tokenizer = KronosTokenizer.from_pretrained(cfg.tokenizer_id)
        self.model = Kronos.from_pretrained(cfg.model_id)
        try:
            self.predictor = KronosPredictor(
                self.model, self.tokenizer, device="cpu", max_context=cfg.max_context)
        except TypeError:
            self.predictor = KronosPredictor(self.model, self.tokenizer, device="cpu")
        self.cfg = cfg
        self.repair_count = 0

    def _repair(self, f):
        r = f.iloc[0]
        o, h, l, c = (float(r["open"]), float(r["high"]),
                      float(r["low"]), float(r["close"]))
        th, tl = max(o, c, h), min(o, c, l)
        repair = max(abs(h - th), abs(l - tl))
        if c <= 0: return None
        pct = repair / c
        if pct > self.cfg.max_ohlc_repair_pct:
            LOG.warning(f"OHLC repair {pct*100:.3f}% > tolerance; rejecting.")
            return None
        if repair > 0:
            self.repair_count += 1
            fixed = f.copy()
            fixed.at[f.index[0], "high"] = th
            fixed.at[f.index[0], "low"] = tl
            return fixed
        return f

    def forecast_next(self, window_df):
        if len(window_df) != self.cfg.lookback:
            raise ValueError(f"Expected {self.cfg.lookback}, got {len(window_df)}")
        check = window_df.reset_index()
        if "timestamp" not in check.columns:
            check = check.rename(columns={check.columns[0]: "timestamp"})
        validate_candles(check)

        x = window_df.copy()
        if "amount" not in x.columns:
            x["amount"] = x["close"] * x["volume"]

        next_ts = window_df.index[-1] + timedelta(minutes=15)
        x_ts = pd.Series(pd.DatetimeIndex(window_df.index))
        y_ts = pd.Series([next_ts])
        f = self.predictor.predict(
            df=x[["open", "high", "low", "close", "volume", "amount"]],
            x_timestamp=x_ts, y_timestamp=y_ts, pred_len=self.cfg.pred_len,
            T=self.cfg.temperature, top_p=self.cfg.top_p,
            sample_count=self.cfg.sample_count, verbose=False,
        )
        if len(f) == len(y_ts):
            f.index = pd.DatetimeIndex(y_ts)
        return self._repair(f)


# ===========================================================================
# Costs + signal
# ===========================================================================
def estimate_round_trip_cost_fraction(price, cfg, lot_size):
    turnover = price * lot_size
    brokerage = 2 * cfg.brokerage_per_order
    exchange = turnover * cfg.exchange_txn_rate * 2
    gst = (brokerage + exchange) * cfg.gst_rate
    stamp = turnover * cfg.stamp_duty_rate
    sebi = turnover * cfg.sebi_rate * 2
    slippage = 2 * cfg.slippage_ticks * cfg.tick_size * lot_size
    return (brokerage + exchange + gst + stamp + sebi + slippage) / turnover


def generate_signal(row, forecast_return, cfg, lot_size,
                    use_ema=True, use_atr=True, use_cost=True):
    atr14 = float(row["atr14"]); ema20 = float(row["ema20"]); ema50 = float(row["ema50"])
    cutoff = row.get("atr_p95_cutoff")
    reasons = []
    if use_atr and pd.notna(cutoff) and atr14 > float(cutoff):
        return "NO TRADE", dict(
            forecast_return=forecast_return, atr14=atr14, ema20=ema20, ema50=ema50,
            threshold=None, round_trip_cost=None,
            reasons=[f"ATR14={atr14:.1f} > p95={cutoff:.1f}"])
    rt = estimate_round_trip_cost_fraction(row["close"], cfg, lot_size)
    threshold = (max(cfg.base_threshold, cfg.cost_threshold_multiplier * rt)
                 if use_cost else cfg.base_threshold)
    bullish = ema20 > ema50; bearish = ema20 < ema50
    details = dict(forecast_return=forecast_return, atr14=atr14, ema20=ema20,
                   ema50=ema50, threshold=threshold, round_trip_cost=rt,
                   reasons=reasons)
    if forecast_return > threshold and (not use_ema or bullish):
        return "LONG CANDIDATE", details
    if forecast_return < -threshold and (not use_ema or bearish):
        return "SHORT CANDIDATE", details
    return "NO TRADE", details


def calculate_futures_position_size(equity, entry, atr, cfg, lot_size):
    budget = equity * cfg.risk_per_trade
    stop_dist = cfg.stop_atr_mult * atr
    rt_cost = estimate_round_trip_cost_fraction(entry, cfg, lot_size) * entry * lot_size
    loss_per_lot = stop_dist * lot_size + rt_cost
    lots = max(int(budget // loss_per_lot), 0) if loss_per_lot > 0 else 0
    return lots, budget, loss_per_lot


def simulate_fill(side, signal_close, next_open, atr, cfg):
    limit = cfg.adverse_move_atr_mult * atr
    if side == "LONG" and next_open > signal_close + limit: return None
    if side == "SHORT" and next_open < signal_close - limit: return None
    return next_open


def estimate_option_round_trip_cost(premium, lot_size, cfg):
    turnover = premium * lot_size
    brokerage = 2 * cfg.brokerage_per_order
    exchange = turnover * cfg.option_exchange_txn_rate * 2
    stt = turnover * cfg.option_stt_rate
    gst = (brokerage + exchange) * cfg.gst_rate
    sebi = turnover * cfg.sebi_rate * 2
    stamp = turnover * cfg.option_stamp_rate
    slippage = 2 * cfg.slippage_ticks * cfg.tick_size * lot_size
    return brokerage + exchange + stt + gst + sebi + stamp + slippage


def select_option_strike(chain, spot, side, cfg):
    leg_key = "call_options" if side == "LONG" else "put_options"
    best, best_dist = None, float("inf")
    for row in chain:
        leg = row.get(leg_key) or {}
        md = leg.get("market_data") or {}
        greeks = leg.get("option_greeks") or {}
        bid = md.get("bid_price"); ask = md.get("ask_price"); ltp = md.get("ltp")
        if not (bid and ask and ltp and ltp > 0): continue
        spread_pct = (ask - bid) / ltp
        if spread_pct > cfg.option_spread_max_pct: continue
        dist = abs(row["strike_price"] - spot)
        if dist < best_dist:
            best_dist = dist
            best = dict(instrument_key=leg.get("instrument_key"),
                        strike=row["strike_price"], premium=ltp,
                        bid=bid, ask=ask, spread_pct=spread_pct,
                        delta=greeks.get("delta"), iv=greeks.get("iv"),
                        expiry=row.get("expiry"))
    return best


def size_option_position(equity, delta, atr, premium, lot_size, cfg):
    budget = equity * cfg.risk_per_trade
    if delta is not None and delta != 0:
        premium_loss = abs(delta) * (cfg.stop_atr_mult * atr) * lot_size
    else:
        premium_loss = premium * lot_size
    costs = estimate_option_round_trip_cost(premium, lot_size, cfg)
    loss_per_lot = premium_loss + costs
    if loss_per_lot <= 0:
        return 0, budget, loss_per_lot
    return max(int(budget // loss_per_lot), 0), budget, loss_per_lot


# ===========================================================================
# Live state
# ===========================================================================
@dataclass
class LiveState:
    date: str = ""
    entries_today: int = 0
    consecutive_losses: int = 0
    daily_pnl: float = 0.0
    last_processed_bar_ts: Optional[str] = None
    last_signal_ts: Optional[str] = None
    last_decision: Optional[str] = None
    history: List[Dict] = None

    def __post_init__(self):
        if self.history is None:
            self.history = []

    @classmethod
    def load(cls, path):
        if not os.path.exists(path):
            return cls(date=str(datetime.now(TZ).date()))
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        s = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        s.roll_if_new_day()
        return s

    def save(self, path):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2, default=str)
        os.replace(tmp, path)

    def roll_if_new_day(self):
        today = str(datetime.now(TZ).date())
        if self.date != today:
            if self.date:
                self.history.append(dict(
                    date=self.date, entries=self.entries_today,
                    consecutive_losses=self.consecutive_losses,
                    daily_pnl=self.daily_pnl))
            self.date = today
            self.entries_today = 0
            self.consecutive_losses = 0
            self.daily_pnl = 0.0

    def blocks_entry(self, cfg):
        if self.entries_today >= cfg.max_entries_per_day:
            return f"max entries/day ({cfg.max_entries_per_day})"
        if self.consecutive_losses >= cfg.max_consecutive_losses:
            return f"{cfg.max_consecutive_losses} consecutive losses"
        if self.daily_pnl <= -cfg.live_equity * cfg.daily_loss_limit:
            return "daily loss limit"
        return None

    def record_trade(self, pnl):
        # entries_today is incremented when the signal is emitted, not here.
        self.daily_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0


# ===========================================================================
# Bar scheduling helpers
# ===========================================================================
def is_weekend(now):
    return now.weekday() >= 5


def is_session_active(now, cfg):
    if is_weekend(now):
        return False
    return cfg.session_open <= now.time() < cfg.session_close


def is_session_extended(now, cfg):
    """Extend a few minutes past close so the final bar can be processed."""
    if is_weekend(now):
        return False
    if now.time() < cfg.session_open:
        return False
    buffer_time = (
        datetime.combine(now.date(), cfg.session_close) +
        timedelta(seconds=cfg.session_close_buffer_sec)
    ).time()
    return now.time() < buffer_time


def latest_closed_bar(now, cfg):
    """
    Return start-timestamp of the most recent bar whose END has passed by
    at least cfg.bar_close_grace_sec. None if no session bar has closed yet.
    """
    mins = now.minute
    last_boundary_min = (mins // 15) * 15
    last_boundary = now.replace(minute=last_boundary_min, second=0, microsecond=0)
    elapsed = (now - last_boundary).total_seconds()

    if elapsed >= cfg.bar_close_grace_sec:
        candidate = last_boundary - timedelta(minutes=15)
    else:
        candidate = last_boundary - timedelta(minutes=30)

    session_start = now.replace(hour=cfg.session_open.hour,
                                minute=cfg.session_open.minute,
                                second=0, microsecond=0)
    if candidate < session_start:
        return None
    return candidate


def next_bar_ready_at(now, cfg):
    """Wall-clock time when the NEXT 15-min bar will be ready to process."""
    mins = now.minute
    next_boundary_min = ((mins // 15) + 1) * 15
    base = now.replace(minute=0, second=0, microsecond=0)
    target = base + timedelta(minutes=next_boundary_min,
                              seconds=cfg.bar_close_grace_sec)
    if target <= now:
        target += timedelta(minutes=15)
    return target


# ===========================================================================
# Live signal runner
# ===========================================================================
class LiveSignalRunner:
    def __init__(self, cfg, client, forecaster, instrument_key, test_mode=False):
        self.cfg = cfg
        self.client = client
        self.forecaster = forecaster
        self.instrument_key = instrument_key
        self.test_mode = test_mode
        self.state = LiveState.load(cfg.state_file)

    # ---- entry points ---------------------------------------------------
    def run_test_bar(self, bar_start_iso: str):
        bar_start = pd.Timestamp(bar_start_iso)
        if bar_start.tz is None:
            bar_start = bar_start.tz_localize(TZ)
        else:
            bar_start = bar_start.tz_convert(TZ)
        LOG.info(f"[TEST] Processing bar {bar_start} (state not persisted)")
        saved = copy.deepcopy(self.state)
        try:
            self._process_bar(bar_start, test_mode=True)
        finally:
            self.state = saved
            saved.save(self.cfg.state_file)

    def run(self):
        now = datetime.now(TZ)
        if is_weekend(now):
            LOG.error("Weekend — no session today.")
            return
        if now.time() < self.cfg.session_open:
            target = now.replace(hour=self.cfg.session_open.hour,
                                 minute=self.cfg.session_open.minute,
                                 second=self.cfg.bar_close_grace_sec,
                                 microsecond=0)
            sleep_for = (target - now).total_seconds()
            LOG.info(f"Session opens at {self.cfg.session_open:%H:%M} IST. "
                     f"Sleeping {sleep_for/3600:.2f}h until then …")
            time.sleep(sleep_for)
        elif now.time() >= self.cfg.session_close:
            LOG.error("Session already closed for today. Run tomorrow.")
            return

        LOG.info("Live signal loop started. Ctrl+C to stop.")
        last_handled_bar = None

        while True:
            now = datetime.now(TZ)

            if not is_session_extended(now, self.cfg):
                LOG.info("Session ended. Exiting loop.")
                break

            closed_bar_start = latest_closed_bar(now, self.cfg)

            # No bar has closed yet (e.g., just after 09:15)
            if closed_bar_start is None:
                next_ready = next_bar_ready_at(now, self.cfg)
                sleep_for = (next_ready - now).total_seconds()
                if sleep_for > 0:
                    LOG.info(f"No bar yet. Sleeping {sleep_for:.0f}s "
                             f"until {next_ready:%H:%M:%S}.")
                    time.sleep(sleep_for)
                continue

            # Already processed this bar?
            if (closed_bar_start == last_handled_bar
                    or (self.state.last_processed_bar_ts
                        and str(closed_bar_start) <= self.state.last_processed_bar_ts)):
                next_ready = next_bar_ready_at(now, self.cfg)
                sleep_for = (next_ready - now).total_seconds()
                if sleep_for > 0:
                    LOG.info(f"Bar {closed_bar_start:%H:%M} already processed. "
                             f"Sleeping {sleep_for:.0f}s until "
                             f"{next_ready:%H:%M:%S}.")
                    time.sleep(sleep_for)
                continue

            # Process
            try:
                self._process_bar(closed_bar_start, test_mode=False)
                last_handled_bar = closed_bar_start
            except PermissionError as e:
                LOG.error(f"AUTH FAILURE: {e}")
                LOG.error("Halting. Do not place manual trades.")
                break
            except Exception as e:
                LOG.error(f"Bar {closed_bar_start} error: {e}")
                last_handled_bar = closed_bar_start  # don't retry endlessly

    # ---- per-bar --------------------------------------------------------
    def _process_bar(self, bar_start, test_mode=False):
        if bar_start.tz is None:
            bar_start = bar_start.tz_localize(TZ)
        else:
            bar_start = bar_start.tz_convert(TZ)

        if not test_mode and self.state.last_processed_bar_ts:
            prev = pd.Timestamp(self.state.last_processed_bar_ts)
            if bar_start <= prev:
                LOG.info(f"Bar {bar_start} already processed; skipping.")
                return

        # Fetch
        to_date = datetime.now(TZ).strftime("%Y-%m-%d")
        from_date = (datetime.now(TZ) -
                     timedelta(days=self.cfg.live_fetch_days)).strftime("%Y-%m-%d")
        df = self._fetch_with_retry(from_date, to_date)

        # Confirm bar present
        if df[df["timestamp"] == bar_start].empty:
            LOG.warning(f"Bar {bar_start} not yet available.")
            return

        # Indicators + window
        df = add_indicators(df, self.cfg)
        window = df[df["timestamp"] <= bar_start].tail(self.cfg.lookback)
        if len(window) < self.cfg.lookback:
            LOG.warning(f"Only {len(window)} bars available for window; skip.")
            return
        row = window.iloc[-1]
        ts = pd.Timestamp(bar_start)

        # Mark processed before emitting, so a crash doesn't re-fire
        if not test_mode:
            self.state.last_processed_bar_ts = str(ts)
            self.state.roll_if_new_day()
            self.state.save(self.cfg.state_file)

        # Session gates
        session_start = ts.replace(hour=self.cfg.session_open.hour,
                                   minute=self.cfg.session_open.minute,
                                   second=0, microsecond=0)
        minutes_in = (ts - session_start).total_seconds() / 60
        if minutes_in < self.cfg.entry_block_first_minutes:
            LOG.info(f"[{ts:%H:%M}] first-{self.cfg.entry_block_first_minutes}-min block.")
            return

        # Last entry must leave room for a 4-bar hold before EOD exit
        last_allowed = ts.replace(hour=14, minute=0, second=0, microsecond=0)
        if ts > last_allowed:
            LOG.info(f"[{ts:%H:%M}] after 14:00 — no new signal (EOD truncation).")
            return

        # Risk gates
        block = self.state.blocks_entry(self.cfg)
        if block:
            LOG.info(f"[{ts:%H:%M}] blocked by risk: {block}")
            return

        # Forecast
        window_idx = window.set_index("timestamp")
        try:
            forecast = self.forecaster.forecast_next(window_idx)
        except Exception as e:
            LOG.error(f"[{ts:%H:%M}] forecast failed: {e}")
            return
        if forecast is None:
            LOG.warning(f"[{ts:%H:%M}] forecast rejected (OHLC repair).")
            return

        pred_close = float(forecast["close"].iloc[0])
        fc_ret = (pred_close / float(row["close"])) - 1.0
        decision, details = generate_signal(
            row, fc_ret, self.cfg, self.cfg.lot_size)

        LOG.info(f"[{ts:%H:%M}] {decision}  fc_ret={fc_ret:+.4%}  "
                 f"ATR={details['atr14']:.1f}  thr={details['threshold']:.4%}")

        if decision in ("LONG CANDIDATE", "SHORT CANDIDATE"):
            self._emit_signal(ts, decision, details, row, forecast,
                              test_mode=test_mode)
        else:
            if not test_mode:
                self.state.last_decision = decision
                self.state.save(self.cfg.state_file)

    def _fetch_with_retry(self, from_date, to_date):
        last_err = None
        for attempt in range(self.cfg.candle_fetch_retry):
            try:
                return self.client.fetch_candles(
                    self.instrument_key, from_date, to_date)
            except Exception as e:
                last_err = e
                if attempt < self.cfg.candle_fetch_retry - 1:
                    LOG.warning(f"Fetch attempt {attempt+1} failed: {e}. Retrying …")
                    time.sleep(self.cfg.candle_fetch_retry_delay_sec)
        raise last_err if last_err else RuntimeError("fetch failed")

    def _emit_signal(self, ts, decision, details, row, forecast, test_mode=False):
        side = "LONG" if decision == "LONG CANDIDATE" else "SHORT"
        spot = float(row["close"]); atr = float(row["atr14"])

        if side == "LONG":
            fut_stop = spot - atr * self.cfg.stop_atr_mult
            fut_target = spot + atr * self.cfg.target_atr_mult
            adverse_cancel = spot + atr * self.cfg.adverse_move_atr_mult
        else:
            fut_stop = spot + atr * self.cfg.stop_atr_mult
            fut_target = spot - atr * self.cfg.target_atr_mult
            adverse_cancel = spot - atr * self.cfg.adverse_move_atr_mult

        opt, opt_note = None, None
        try:
            expiries = self.client.fetch_option_expiries(
                self.cfg.option_chain_underlying)
            today = str(datetime.now(TZ).date())
            future = [e for e in expiries if e >= today]
            if not future:
                opt_note = "no future expiry"
            else:
                expiry = future[0]
                chain = self.client.fetch_option_chain(
                    self.cfg.option_chain_underlying, expiry)
                opt = select_option_strike(chain, spot, side, self.cfg)
                if opt is None:
                    opt_note = "no liquid strike within spread limit"
        except Exception as e:
            opt_note = f"chain fetch failed: {e}"

        max_hold_end = ts + timedelta(minutes=15 * self.cfg.max_holding_bars)
        line = "═" * 68
        print(f"\n{line}")
        print(f"  SIGNAL  {ts:%Y-%m-%d %H:%M} IST    [{decision}]"
              + ("  (TEST)" if test_mode else ""))
        print(line)
        print(f"  Forecast return : {details['forecast_return']:+.4%}   "
              f"(threshold {details['threshold']:.4%})")
        print(f"  Underlying close: {spot:,.2f}   ATR14 {atr:.2f}   "
              f"EMA20 {details['ema20']:,.1f} / EMA50 {details['ema50']:,.1f}")

        if opt:
            print(f"\n  ▸ ACTION  BUY NIFTY {opt['expiry']} {int(opt['strike'])} "
                  f"{'CE' if side == 'LONG' else 'PE'}")
            print(f"  ▸ Premium ≈ ₹{opt['premium']:.2f}   "
                  f"bid {opt['bid']:.2f} / ask {opt['ask']:.2f}  "
                  f"spread {opt['spread_pct']*100:.2f}%")
            if opt["delta"] is not None:
                print(f"  ▸ Delta {opt['delta']:.2f}"
                      + (f"   IV {opt['iv']:.1f}%" if opt['iv'] else ""))
            lots, budget, loss_per_lot = size_option_position(
                self.cfg.live_equity, opt["delta"], atr,
                opt["premium"], self.cfg.lot_size, self.cfg)
            if lots >= 1:
                print(f"  ▸ Suggested qty: {lots} lot(s) "
                      f"({lots * self.cfg.lot_size} units)")
                print(f"  ▸ Risk/lot: ₹{loss_per_lot:,.0f}  "
                      f"(budget ₹{budget:,.0f})")
            else:
                print(f"  ▸ ⚠ Sizing yields 0 lots (risk/lot ₹{loss_per_lot:,.0f} "
                      f"> budget ₹{budget:,.0f}).")
                print(f"    Per PROJECT.txt §10: NO TRADE. Do not override.")
        else:
            print(f"\n  ▸ Option recommendation unavailable ({opt_note}).")
            print(f"    Pick ATM {'CE' if side == 'LONG' else 'PE'} manually.")

        print(f"\n  FUTURES STOP    ≤ {fut_stop:,.2f}   ← exit when NIFTY fut hits")
        print(f"  FUTURES TARGET  ≥ {fut_target:,.2f}   ← exit when NIFTY fut hits")
        print(f"  MAX HOLD        {self.cfg.max_holding_bars} bars → close by "
              f"{max_hold_end:%H:%M} IST")
        print(f"  EOD EXIT        {self.cfg.final_exit_time:%H:%M} IST")
        print(f"  ADVERSE CANCEL  skip if futures "
              f"{'≥' if side == 'LONG' else '≤'} {adverse_cancel:,.2f} before entry")
        print(line)
        if not test_mode:
            print(f"  After exit:  python kronos.py --report-trade PnL=<actual>")
        print(line + "\n")

        if not test_mode:
            self.state.entries_today += 1
            self.state.last_signal_ts = str(ts)
            self.state.last_decision = decision
            self.state.save(self.cfg.state_file)

        record = dict(ts=str(ts), decision=decision,
                      forecast_return=details["forecast_return"],
                      threshold=details["threshold"],
                      underlying_close=spot, atr14=atr,
                      ema20=details["ema20"], ema50=details["ema50"],
                      fut_stop=fut_stop, fut_target=fut_target,
                      adverse_cancel=adverse_cancel,
                      max_hold_end=str(max_hold_end),
                      option=opt, option_note=opt_note,
                      live_equity=self.cfg.live_equity,
                      test_mode=test_mode)
        with open(self.cfg.signal_log_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")


# ===========================================================================
# Backtest (kept for continuity)
# ===========================================================================
@dataclass
class Trade:
    trade_id: int
    side: str
    entry_ts: datetime
    entry_price: float
    quantity: int
    stop: float
    target: float
    exit_ts: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: str = ""
    gross_pnl: float = 0.0
    costs: float = 0.0
    net_pnl: float = 0.0


class BacktestEngine:
    def __init__(self, cfg, forecaster, lot_size, label="backtest"):
        self.cfg = cfg; self.forecaster = forecaster; self.lot_size = lot_size
        self.equity = cfg.initial_equity
        self.trades: List[Trade] = []
        self.forecast_log = []
        self.daily_pnl = {}
        self.consecutive_losses = 0
        self.counter = 0
        self.skip = dict(filters=0, sizing=0, daily_limits=0,
                         gap_not_15m=0, overnight=0, first_30_min=0,
                         adverse_move=0, forecast_fail=0)
        self.label = label

    def run(self, df):
        validate_candles(df.reset_index(drop=True))
        pos = None; entries_today = 0; cur_day = None; day_start_eq = self.equity
        total = len(df) - 1 - self.cfg.lookback
        for idx, i in enumerate(range(self.cfg.lookback, len(df) - 1)):
            row, nxt = df.iloc[i], df.iloc[i + 1]; ts = row["timestamp"]
            if idx % self.cfg.progress_every == 0:
                LOG.info(f"[{self.label}] {idx}/{total} ({ts}) trades={self.counter} "
                         f"equity={self.equity:,.0f}")
            if cur_day != ts.date():
                cur_day = ts.date(); entries_today = 0
                day_start_eq = self.equity; self.consecutive_losses = 0
            s_start = ts.replace(hour=9, minute=15, second=0, microsecond=0)
            if (ts - s_start) < timedelta(minutes=self.cfg.entry_block_first_minutes):
                continue
            window = df.iloc[i - self.cfg.lookback + 1 : i + 1].set_index("timestamp")
            try:
                fc = self.forecaster.forecast_next(window)
            except Exception as e:
                LOG.warning(f"forecast failed {ts}: {e}")
                self.skip["forecast_fail"] += 1; continue
            if fc is None:
                self.skip["forecast_fail"] += 1; continue
            fc_ret = (float(fc["close"].iloc[0]) / float(row["close"])) - 1
            decision, details = generate_signal(row, fc_ret, self.cfg, self.lot_size)
            self.forecast_log.append(dict(signal_ts=ts, decision=decision, **details))
            if pos is not None:
                pos = self._exit(pos, nxt)
            if decision not in ("LONG CANDIDATE", "SHORT CANDIDATE"):
                self.skip["filters"] += 1; continue
            if pos is not None: continue
            if (nxt["timestamp"] - ts) != timedelta(minutes=15):
                self.skip["gap_not_15m"] += 1; continue
            if nxt["timestamp"].date() != ts.date():
                self.skip["overnight"] += 1; continue
            e_start = nxt["timestamp"].replace(hour=9, minute=15, second=0, microsecond=0)
            if (nxt["timestamp"] - e_start) < timedelta(minutes=self.cfg.entry_block_first_minutes):
                self.skip["first_30_min"] += 1; continue
            if nxt["timestamp"].time() >= self.cfg.final_exit_time:
                self.skip["daily_limits"] += 1; continue
            if entries_today >= self.cfg.max_entries_per_day:
                self.skip["daily_limits"] += 1; continue
            if self.consecutive_losses >= self.cfg.max_consecutive_losses:
                self.skip["daily_limits"] += 1; continue
            if self.daily_pnl.get(cur_day, 0) <= -day_start_eq * self.cfg.daily_loss_limit:
                self.skip["daily_limits"] += 1; continue
            side = "LONG" if decision == "LONG CANDIDATE" else "SHORT"
            entry = simulate_fill(side, float(row["close"]),
                                  float(nxt["open"]), float(row["atr14"]), self.cfg)
            if entry is None:
                self.skip["adverse_move"] += 1; continue
            lots, _, _ = calculate_futures_position_size(
                self.equity, entry, float(row["atr14"]), self.cfg, self.lot_size)
            if lots < 1:
                self.skip["sizing"] += 1; continue
            atr = float(row["atr14"])
            stop = entry - atr * self.cfg.stop_atr_mult if side == "LONG" else entry + atr * self.cfg.stop_atr_mult
            target = entry + atr * self.cfg.target_atr_mult if side == "LONG" else entry - atr * self.cfg.target_atr_mult
            self.counter += 1
            pos = Trade(trade_id=self.counter, side=side, entry_ts=nxt["timestamp"],
                        entry_price=entry, quantity=lots * self.lot_size,
                        stop=stop, target=target)
            entries_today += 1
            pos = self._exit(pos, nxt)
        if pos is not None:
            self._force_close(pos, df.iloc[-1])
        return pd.DataFrame(self.forecast_log)

    def _force_close(self, t, candle):
        c = candle["close"]
        t.exit_ts = candle["timestamp"]; t.exit_price = c; t.exit_reason = "end_of_data"
        t.gross_pnl = ((c - t.entry_price) if t.side == "LONG"
                       else (t.entry_price - c)) * t.quantity
        t.costs = estimate_round_trip_cost_fraction(
            t.entry_price, self.cfg, self.lot_size) * t.entry_price * t.quantity
        t.net_pnl = t.gross_pnl - t.costs
        self.equity += t.net_pnl; self.trades.append(t)

    def _exit(self, t, candle):
        h, l, c = candle["high"], candle["low"], candle["close"]
        px, reason = None, ""
        if t.side == "LONG":
            if l <= t.stop: px, reason = t.stop, "stop"
            elif h >= t.target: px, reason = t.target, "target"
        else:
            if h >= t.stop: px, reason = t.stop, "stop"
            elif l <= t.target: px, reason = t.target, "target"
        if px is None:
            held = (candle["timestamp"] - t.entry_ts) / pd.Timedelta(minutes=15)
            if held >= self.cfg.max_holding_bars:
                px, reason = c, "max_holding"
        if px is None and candle["timestamp"].time() >= self.cfg.final_exit_time:
            px, reason = c, "eod"
        if px is None: return t
        t.gross_pnl = ((px - t.entry_price) if t.side == "LONG"
                       else (t.entry_price - px)) * t.quantity
        t.costs = estimate_round_trip_cost_fraction(
            t.entry_price, self.cfg, self.lot_size) * t.entry_price * t.quantity
        t.net_pnl = t.gross_pnl - t.costs
        t.exit_ts = candle["timestamp"]; t.exit_price = px; t.exit_reason = reason
        self.equity += t.net_pnl
        day = candle["timestamp"].date()
        self.daily_pnl[day] = self.daily_pnl.get(day, 0.0) + t.net_pnl
        self.consecutive_losses = self.consecutive_losses + 1 if t.net_pnl < 0 else 0
        self.trades.append(t); return None


# ===========================================================================
# Entry point
# ===========================================================================
def run_backtest(cfg, client, instrument_key):
    to_date = datetime.now(TZ).strftime("%Y-%m-%d")
    from_date = (datetime.now(TZ) - timedelta(days=cfg.history_days)).strftime("%Y-%m-%d")
    raw = client.fetch_candles(instrument_key, from_date, to_date)
    validate_candles(raw)
    df = add_indicators(raw, cfg).dropna(
        subset=["atr14", "ema20", "ema50"]).reset_index(drop=True)
    if len(df) < cfg.lookback + 2:
        raise RuntimeError(f"Need ≥{cfg.lookback + 2} candles, got {len(df)}.")
    forecaster = KronosForecaster(cfg)
    engine = BacktestEngine(cfg, forecaster, cfg.lot_size, label="backtest")
    engine.run(df)
    if engine.trades:
        tdf = pd.DataFrame([t.__dict__ for t in engine.trades])
        print(f"\n--- Backtest summary ---")
        print(f"  Trades:     {len(tdf)}")
        print(f"  Net P&L:    ₹{tdf['net_pnl'].sum():,.2f}")
        print(f"  Gross P&L:  ₹{tdf['gross_pnl'].sum():,.2f}")
        print(f"  Costs:      ₹{tdf['costs'].sum():,.2f}")
        print(f"  Win rate:   {(tdf['net_pnl'] > 0).mean() * 100:.1f}%")
    else:
        print("No trades.")
    print("\nSkip counts:")
    for k, v in engine.skip.items():
        print(f"  {k}: {v}")
    print(f"  repairs: {forecaster.repair_count}")


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--test-bar", type=str, default=None,
                        help='Process one historical bar as if live, e.g. '
                             '"2026-10-16 14:30" or "2026-10-16T14:30:00+05:30"')
    parser.add_argument("--show-state", action="store_true")
    parser.add_argument("--reset-day", action="store_true")
    parser.add_argument("--report-trade", type=str, default=None)
    args = parser.parse_args()

    cfg = Config()

    if args.show_state:
        print(json.dumps(asdict(LiveState.load(cfg.state_file)),
                         indent=2, default=str))
        return
    if args.reset_day:
        s = LiveState.load(cfg.state_file)
        s.date = str(datetime.now(TZ).date())
        s.entries_today = 0; s.consecutive_losses = 0; s.daily_pnl = 0.0
        s.save(cfg.state_file)
        print("Daily counters reset."); return
    if args.report_trade:
        s = LiveState.load(cfg.state_file)
        if "=" not in args.report_trade:
            print("Format: --report-trade PnL=+1500"); return
        pnl = float(args.report_trade.split("=", 1)[1])
        s.record_trade(pnl)
        s.save(cfg.state_file)
        print(f"Recorded PnL={pnl:+.2f}. Daily P&L={s.daily_pnl:+.2f}, "
              f"consecutive_losses={s.consecutive_losses}, "
              f"entries_today={s.entries_today}")
        return

    env = load_env_file("env.txt")
    token = get_access_token(env)
    client = UpstoxClient(token)
    instrument_key = client.get_current_nifty_futures_key()
    cfg.lot_size = client.lot_size
    LOG.info(f"lot_size={cfg.lot_size}")

    if args.test_bar:
        forecaster = KronosForecaster(cfg)
        runner = LiveSignalRunner(cfg, client, forecaster, instrument_key,
                                  test_mode=True)
        runner.run_test_bar(args.test_bar)
        return

    if args.live:
        print("\n" + "═" * 68)
        print("  LIVE SIGNAL GENERATOR — manual execution")
        print("═" * 68)
        print(f"  Live equity used for sizing: ₹{cfg.live_equity:,.0f}")
        print("  PROJECT.txt §15: paper-trade 20–30 sessions before real capital.")
        print("═" * 68 + "\n")
        forecaster = KronosForecaster(cfg)
        runner = LiveSignalRunner(cfg, client, forecaster, instrument_key)
        try:
            runner.run()
        except KeyboardInterrupt:
            print("\nInterrupted. State saved.")
        return

    run_backtest(cfg, client, instrument_key)


if __name__ == "__main__":
    main()