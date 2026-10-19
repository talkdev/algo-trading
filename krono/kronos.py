"""
Kronos Nifty 15-Minute Multi-Candle Forecast
=============================================
Predicts the next 3 candles after every completed bar. Prints OHLC, direction,
live LTP, spot-vs-futures basis, and last-5-closed-bar diagnostic.
No filtering. No sizing. You decide what to trade.

Modes:
  python kronos.py --live                          → live, refresh every bar
  python kronos.py --test-bar "YYYY-MM-DD HH:MM"   → forecast one historical bar
  python kronos.py --accuracy                      → direction accuracy
  python kronos.py --forecast-audit "YYYY-MM-DD"   → every bar on one day
"""

import os
import sys
import time
import subprocess
import gzip
import json
import argparse
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime
from typing import Optional

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
    required = {
        "torch": "torch",
        "huggingface_hub": "huggingface_hub",
        "safetensors": "safetensors",
        "einops": "einops",
    }
    missing = [pkg for pkg, mod in ((m, m) for m in required) if not _module_exists(mod)]
    missing = [pkg for mod, pkg in required.items() if not _module_exists(mod)]
    if missing:
        print(f"[bootstrap] Installing: {missing}")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", *missing],
            check=True,
        )


_ensure_dependencies()

KRONOS_REPO = "https://github.com/shiyu-coder/Kronos.git"
KRONOS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Kronos")

try:
    from model import Kronos, KronosTokenizer, KronosPredictor
except ImportError:
    if not os.path.isdir(KRONOS_DIR):
        print(f"[bootstrap] Cloning Kronos into {KRONOS_DIR} …")
        subprocess.run(
            ["git", "clone", "--depth", "1", KRONOS_REPO, KRONOS_DIR],
            check=True,
        )
    for p in (KRONOS_DIR, os.path.join(KRONOS_DIR, "model")):
        if p not in sys.path:
            sys.path.insert(0, p)
    from model import Kronos, KronosTokenizer, KronosPredictor
    print("[bootstrap] Kronos imported via path injection.")

TZ = pytz.timezone("Asia/Kolkata")
LOG = logging.getLogger("kronos_fc")

SPOT_INSTRUMENT = "NSE_INDEX|Nifty 50"


# ===========================================================================
# Config
# ===========================================================================
@dataclass
class Config:
    model_id: str = "NeoQuasar/Kronos-small"
    tokenizer_id: str = "NeoQuasar/Kronos-Tokenizer-base"
    max_context: int = 512
    lookback: int = 256

    pred_len: int = 3

    # Sampling. Kronos' README uses T=1.0 / top_p=0.9. At T=0.1 every sample is
    # ~identical, so "averaging" them adds nothing and hides all uncertainty.
    temperature: float = 1.0
    top_p: float = 0.9
    n_paths: int = 20              # independent sampled futures per forecast
    batch_chunk: int = 10          # paths per predict_batch call (memory)
    use_batch: bool = True
    max_forecast_sec: int = 150    # hard time budget for sampling
    device: str = "auto"           # auto = cuda if available else cpu

    # Trade-plan rules (manual trading helper, not advice)
    min_conf: float = 0.65         # share of paths agreeing on direction (mean over bars)
    min_move_rng: float = 0.30     # model target must be >= this x avg bar range from entry
    min_rr: float = 1.0            # reward:risk to T1 required for a call
    min_exp_r: float = 0.15        # path-simulated expectancy (in R) required for a call
    cost_pts: float = 1.5          # round-trip slippage+costs assumed per trade (points)
    # Candidate stops as multiples of the avg 15m bar range. Floor is 0.75x: a tighter
    # stop is inside normal bar noise and the 3-bar path model can't capture that.
    stop_grid: tuple = (0.75, 1.0, 1.25, 1.5)
    max_p_stop: float = 0.40       # reject stops hit in more than this share of paths
    risk_rs: float = 0.0           # your max loss per trade in Rs (0 = don't size)
    scorecard_n: int = 30

    # Data integrity: every futures bar is cross-checked against the Nifty SPOT index
    # candles (independent feed). Upstox intraday candles are provisional and can carry
    # bad prints; bars that don't fit spot + basis are rebuilt from spot + basis.
    # Candle source. "1m" = fetch 1-minute candles and build the 15m bars locally from
    # completed minutes 09:15-15:29 only (no pre-open prints, no provisional bar).
    # "15m" = trust Upstox's own 15-minute candles (old behaviour).
    candle_source: str = "1m"
    xcheck_enabled: bool = True
    xcheck_min_pts: float = 15.0   # min tolerance (points) for fut-vs-(spot+basis)
    require_xcheck: bool = True    # no trade calls if recent bars can't be verified

    atr_period: int = 14
    ema_fast: int = 20
    ema_slow: int = 50

    history_days: int = 30
    live_fetch_days: int = 90

    session_open: dtime = dtime(9, 15)
    session_close: dtime = dtime(15, 30)
    session_close_buffer_sec: int = 300
    bar_close_grace_sec: int = 5

    bar_publish_timeout_sec: int = 30
    bar_publish_poll_sec: float = 2.0

    instrument: str = "NIFTY_FUT"

    clear_screen: bool = True
    show_live_ltp: bool = True
    show_spot_basis: bool = True
    show_last_n_bars: int = 5
    prediction_log_file: str = "predictions.jsonl"


INSTRUMENT_MASTER_URL = (
    "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
)


# ===========================================================================
# Env + client
# ===========================================================================
def load_env_file(path="env.txt"):
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


def get_access_token(env):
    token = env.get("UPSTOX_ACCESS_TOKEN") or os.getenv("UPSTOX_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN missing in env.txt.")
    return token


def _parse_candles_response(data):
    rows = []
    for c in reversed(data):
        c = list(c) + [0] * (7 - len(c))
        ts, o, h, l, cl, vol, oi = c[:7]
        rows.append(
            dict(
                timestamp=pd.to_datetime(ts),
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(cl),
                volume=float(vol) if vol is not None else 0.0,
            )
        )
    return rows


class UpstoxClient:
    def __init__(self, access_token):
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        }
        self.lot_size = None
        self.instrument_key = None
        self.expiry_date = None
        self.last_dq = None
        self.source = "1m"
        self._hist_cache = {}

    def _get(self, url, params=None, timeout=60):
        r = requests.get(url, headers=self.headers, params=params, timeout=timeout)
        if r.status_code == 401:
            raise PermissionError(
                "Upstox 401: token expired. Refresh UPSTOX_ACCESS_TOKEN."
            )
        if r.status_code != 200:
            raise RuntimeError(f"Upstox {r.status_code}: {r.text}")
        return r.json()

    def get_current_nifty_futures_key(self):
        LOG.info("Downloading instrument master …")
        r = requests.get(INSTRUMENT_MASTER_URL, timeout=120)
        r.raise_for_status()
        instruments = json.loads(gzip.decompress(r.content))
        candidates = []
        for ins in instruments:
            if ins.get("segment") != "NSE_FO":
                continue
            if ins.get("instrument_type") != "FUT":
                continue
            name = (ins.get("name") or "").upper().strip()
            symbol = (ins.get("trading_symbol") or "").upper().strip()
            if name == "NIFTY":
                pass
            elif symbol.startswith("NIFTY") and not any(
                tag in symbol
                for tag in ("BANK", "FINNIFTY", "MIDCP", "NXT50", "NEXT50")
            ):
                pass
            else:
                continue
            expiry = ins.get("expiry")
            if not expiry:
                continue
            try:
                exp_date = pd.to_datetime(expiry, unit="ms").date()
            except Exception:
                continue
            if exp_date < datetime.now(TZ).date():
                continue
            candidates.append((expiry, ins["instrument_key"], ins.get("lot_size")))
        if not candidates:
            raise RuntimeError("No NIFTY futures found.")
        candidates.sort(key=lambda x: x[0])
        expiry, key, lot = candidates[0]
        self.lot_size = int(lot) if lot else 75
        self.instrument_key = key
        self.expiry_date = pd.to_datetime(expiry, unit="ms").date()
        LOG.info(
            f"Using {key} expiry={self.expiry_date} lot={self.lot_size}"
        )
        return key

    def _fetch_historical(self, instrument_key, from_date, to_date, interval=15):
        start = datetime.strptime(from_date, "%Y-%m-%d")
        end = datetime.strptime(to_date, "%Y-%m-%d")
        max_days = 30
        rows = []
        cur = start
        while cur <= end:
            ce = min(cur + timedelta(days=max_days - 1), end)
            cf, ct = cur.strftime("%Y-%m-%d"), ce.strftime("%Y-%m-%d")
            url = (
                f"https://api.upstox.com/v3/historical-candle/"
                f"{requests.utils.quote(instrument_key, safe='')}/"
                f"minutes/{interval}/{ct}/{cf}"
            )
            data = self._get(url).get("data", {}).get("candles", [])
            rows.extend(_parse_candles_response(data))
            cur = ce + timedelta(days=1)
        return rows

    def _fetch_intraday(self, instrument_key, interval=15):
        url = (
            f"https://api.upstox.com/v3/historical-candle/intraday/"
            f"{requests.utils.quote(instrument_key, safe='')}/"
            f"minutes/{interval}"
        )
        data = self._get(url).get("data", {}).get("candles", [])
        return _parse_candles_response(data)

    def fetch_candles(self, instrument_key, from_date, to_date):
        """15m bars. Default: built locally from 1-minute candles (see candle_source)."""
        if self.source == "1m":
            try:
                df = self._fetch_candles_1m(instrument_key, from_date, to_date)
                if len(df):
                    return df
                LOG.warning("1m build returned no bars; falling back to Upstox 15m candles")
            except Exception as e:
                LOG.warning("1m build failed (%s); falling back to Upstox 15m candles", e)
        return self._fetch_candles_15m(instrument_key, from_date, to_date)

    def _hist_rows_cached(self, instrument_key, from_date, to_date, interval):
        k = (instrument_key, from_date, to_date, interval)
        if k not in self._hist_cache:
            self._hist_cache[k] = self._fetch_historical(
                instrument_key, from_date, to_date, interval)
        return self._hist_cache[k]

    def _fetch_candles_1m(self, instrument_key, from_date, to_date):
        """Build 15m bars from 1-minute candles.
        - only session minutes 09:15..15:29 (drops pre-open / post-close prints)
        - drops the still-forming minute and any bucket not fully elapsed
        - O=first, H=max, L=min, C=last, V=sum; logs buckets with missing minutes"""
        hist = self._hist_rows_cached(instrument_key, from_date, to_date, 1)
        intr = []
        try:
            intr = self._fetch_intraday(instrument_key, 1)
        except Exception as e:
            LOG.warning("Intraday 1m fetch failed (%s); using historical only.", e)
        if not hist and not intr:
            raise RuntimeError("No 1m candles returned.")
        parts = []
        if hist:
            h = pd.DataFrame(hist); h["src"] = 0; parts.append(h)
        if intr:
            i = pd.DataFrame(intr); i["src"] = 1; parts.append(i)
        df = pd.concat(parts, ignore_index=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(TZ)
        df = df.sort_values(["timestamp", "src"], kind="mergesort")
        df = df.drop_duplicates(subset="timestamp", keep="last")

        mod = df["timestamp"].dt.hour * 60 + df["timestamp"].dt.minute
        n_out = int((~((mod >= 555) & (mod <= 929))).sum())
        df = df[(mod >= 555) & (mod <= 929)].copy()
        if n_out:
            LOG.info("Dropped %d out-of-session 1m candles (pre-open/post-close)", n_out)

        now_min = datetime.now(TZ).replace(second=0, microsecond=0)
        df = df[df["timestamp"] < now_min]                    # drop forming minute
        if df.empty:
            raise RuntimeError("No completed session minutes.")
        mod = df["timestamp"].dt.hour * 60 + df["timestamp"].dt.minute
        off = 555 + 15 * ((mod - 555) // 15)
        df["bucket"] = df["timestamp"].dt.normalize() + pd.to_timedelta(off, unit="m")
        g = df.groupby("bucket", sort=True)
        out = pd.DataFrame({
            "timestamp": g["timestamp"].first().index,
            "open": g["open"].first().values,
            "high": g["high"].max().values,
            "low": g["low"].min().values,
            "close": g["close"].last().values,
            "volume": g["volume"].sum().values,
            "n_min": g["open"].count().values,
        })
        out = out[out["timestamp"] + pd.Timedelta(minutes=15) <= now_min]   # fully elapsed only
        thin = out[out["n_min"] < 12]
        if len(thin):
            LOG.warning("%d bar(s) built from <12 of 15 minutes (latest %s) — illiquid or "
                        "missing minutes", len(thin), thin["timestamp"].iloc[-1])
        return out.drop(columns="n_min").reset_index(drop=True)

    def _fetch_candles_15m(self, instrument_key, from_date, to_date):
        hist_rows = self._fetch_historical(instrument_key, from_date, to_date)
        intraday_rows = []
        try:
            intraday_rows = self._fetch_intraday(instrument_key)
            if intraday_rows:
                LOG.info(
                    f"Intraday returned {len(intraday_rows)} candles "
                    f"(latest: {intraday_rows[-1]['timestamp']})"
                )
        except Exception as e:
            LOG.warning(f"Intraday fetch failed ({e}); using historical only.")

        if not hist_rows and not intraday_rows:
            raise RuntimeError("No candles returned.")

        hist = pd.DataFrame(hist_rows)
        intr = pd.DataFrame(intraday_rows)
        if not hist.empty:
            hist["src"] = 0
        if not intr.empty:
            intr["src"] = 1          # intraday wins on overlap (fresher)
        df = pd.concat([hist, intr], ignore_index=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(TZ)

        # Stable sort, then keep the intraday copy of any duplicated bar.
        df = df.sort_values(["timestamp", "src"], kind="mergesort")
        df = df.drop_duplicates(subset="timestamp", keep="last")

        # Keep ONLY regular-session 15m bars: starts 09:15 ... 15:15.
        # Anything else (e.g. a stray 15:30 closing-trade bar, pre-open prints)
        # is not on the broker chart and corrupts ATR / model input.
        mins = df["timestamp"].dt.hour * 60 + df["timestamp"].dt.minute
        in_session = (mins >= 9 * 60 + 15) & (mins <= 15 * 60 + 15)
        on_grid = ((mins - (9 * 60 + 15)) % 15 == 0) & (df["timestamp"].dt.second == 0)
        dropped = df[~(in_session & on_grid)]
        if len(dropped):
            LOG.warning(
                "Dropped %d off-session/off-grid bars (e.g. %s)",
                len(dropped), dropped["timestamp"].iloc[-1],
            )
        df = df[in_session & on_grid].drop(columns="src").reset_index(drop=True)
        return df

    def fetch_clean(self, instrument_key, from_date, to_date, cfg):
        """Futures candles, cross-checked against spot and repaired. The report is in
        self.last_dq; per-bar flags are in the 'repaired'/'verified' columns."""
        fut = self.fetch_candles(instrument_key, from_date, to_date)
        if not cfg.xcheck_enabled:
            out = fut.copy()
            for c in ("open", "high", "low", "close"):
                out["raw_" + c] = out[c]
            out["repaired"], out["verified"] = False, True
            self.last_dq = {"available": False, "disabled": True}
            return out
        try:
            spot = self.fetch_candles(SPOT_INSTRUMENT, from_date, to_date)
        except Exception as e:
            LOG.warning("Spot fetch failed (%s); futures candles UNVERIFIED", e)
            spot = None
        out, dq = cross_check_and_repair(fut, spot, cfg)
        n_bad = int(out["repaired"].sum())
        if n_bad:
            LOG.warning("%d futures bar(s) failed the spot cross-check and were rebuilt "
                        "(latest: %s)", n_bad, out.loc[out["repaired"], "timestamp"].iloc[-1])
        self.last_dq = dq      # NOT in df.attrs: a DataFrame there breaks pandas concat
        return out

    def dump_raw(self, instrument_key, n=8):
        """Print the untouched API candles so you can compare with the broker."""
        url = (
            f"https://api.upstox.com/v3/historical-candle/intraday/"
            f"{requests.utils.quote(instrument_key, safe='')}/minutes/15"
        )
        data = self._get(url).get("data", {}).get("candles", [])
        print(f"RAW intraday 15m candles for {instrument_key} (newest first, as sent by Upstox):")
        for c in data[:n]:
            print("  ", c)
        url1 = url.rsplit("/", 1)[0] + "/1"
        d1 = self._get(url1).get("data", {}).get("candles", [])
        print(f"RAW intraday 1m candles (newest first; first one is the forming minute):")
        for c in d1[:n]:
            print("  ", c)

    def get_live_ltp(self, instrument_key):
        try:
            r = requests.get(
                "https://api.upstox.com/v2/market-quote/ltp",
                headers=self.headers,
                params={"instrument_key": instrument_key},
                timeout=5,
            )
            if r.status_code != 200:
                return None
            data = r.json().get("data", {})
            key = instrument_key.replace("|", ":")
            if key in data:
                return float(data[key]["last_price"])
            for v in data.values():
                return float(v["last_price"])
        except Exception:
            pass
        return None


# ===========================================================================
# Validation + indicators
# ===========================================================================
def validate_candles(df):
    if df.empty:
        raise ValueError("Empty DataFrame.")
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
    if (df["high"] < df["low"]).any():
        raise ValueError("High below low.")


def add_context(df, cfg):
    out = df.copy()
    prev = out["close"].shift(1)
    tr = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev).abs(),
            (out["low"] - prev).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["tr"] = tr
    out["atr14"] = tr.ewm(alpha=1.0 / cfg.atr_period, adjust=False).mean()
    # Avg 15m high-low range EXCLUDING the 09:15 bar: no overnight gap, no opening
    # spike. This (not gap-inflated ATR14) sizes stops/targets for a 45-min trade.
    is_open = (out["timestamp"].dt.hour * 60 + out["timestamp"].dt.minute) == (9 * 60 + 15)
    rng_ = (out["high"] - out["low"]).where(~is_open)
    out["rng14"] = rng_.rolling(14, min_periods=5).mean()
    out["ema20"] = out["close"].ewm(span=cfg.ema_fast, adjust=False).mean()
    out["ema50"] = out["close"].ewm(span=cfg.ema_slow, adjust=False).mean()
    return out


# ===========================================================================
# Data integrity: futures vs spot cross-check
# ===========================================================================
def cross_check_and_repair(fut, spot, cfg):
    """Compare each futures bar with the spot-index bar for the same 15m slot.
    futures ≈ spot + basis (basis drifts slowly), so a bar whose O/H/L/C sits far
    outside spot + rolling-basis is a bad print. Such bars are rebuilt from spot +
    basis, keeping the futures volume, and are marked repaired=True."""
    f = fut.copy().reset_index(drop=True)
    for c in ("open", "high", "low", "close"):
        f["raw_" + c] = f[c]
    f["repaired"] = False
    f["verified"] = False
    dq = {"available": False, "n_checked": 0, "basis_now": None, "tol": None, "table": None}
    if spot is None or len(spot) == 0:
        return f, dq
    s = spot[["timestamp", "open", "high", "low", "close"]].rename(
        columns={"open": "s_open", "high": "s_high", "low": "s_low", "close": "s_close"})
    m = f.merge(s, on="timestamp", how="left")
    has = m["s_close"].notna()
    if int(has.sum()) < 10:
        return f, dq

    d_c = m["close"] - m["s_close"]
    is_open = (m["timestamp"].dt.hour * 60 + m["timestamp"].dt.minute) == 9 * 60 + 15
    s_rng = (m["s_high"] - m["s_low"]).fillna(0.0)
    flag = pd.Series(False, index=m.index)
    basis = pd.Series(np.nan, index=m.index)
    tol = cfg.xcheck_min_pts
    for _ in range(4):
        clean = d_c.where(has & ~flag)
        basis = clean.rolling(26, min_periods=5).median().shift(1).ffill().bfill()
        def dev(a, b):
            return (m[a] - m[b] - basis).abs()
        dc, do = dev("close", "s_close"), dev("open", "s_open")
        mad = dc[has & ~flag].median()
        tol = max(cfg.xcheck_min_pts, 4 * 1.4826 * (0.0 if pd.isna(mad) else mad))
        tol_b = pd.Series(np.where(is_open, tol * 1.7, tol), index=m.index)
        tol_e = tol_b + 0.25 * s_rng
        new_flag = has & ((dc > tol_b) | (do > tol_b)
                          | (dev("high", "s_high") > tol_e) | (dev("low", "s_low") > tol_e))
        if new_flag.equals(flag):
            break
        flag = new_flag

    for c in ("open", "high", "low", "close"):
        f.loc[flag.values, c] = (m["s_" + c] + basis)[flag].values
    f["verified"] = has.values
    f["repaired"] = flag.values
    tbl = m[["timestamp", "raw_open", "raw_high", "raw_low", "raw_close",
             "s_open", "s_high", "s_low", "s_close"]].copy() if "raw_open" in m else m.copy()
    tbl["basis"] = basis.values
    tbl["flag"] = flag.values
    dq.update(available=True, n_checked=int(has.sum()), tol=float(tol),
              basis_now=float(basis.iloc[-1]), table=tbl)
    return f, dq


def dq_tail_reason(closed_df, cfg, n=3):
    """Why the most recent bars can't be trusted for a trade call (or None)."""
    if not cfg.xcheck_enabled or "verified" not in closed_df:
        return None
    tail = closed_df.tail(n)
    if tail["repaired"].any():
        ts = ", ".join(f"{t:%H:%M}" for t in tail.loc[tail["repaired"], "timestamp"])
        return f"recent bar(s) {ts} failed the spot cross-check (bad futures prints, rebuilt)"
    if cfg.require_xcheck and not tail["verified"].all():
        return "recent bars could not be verified against the spot index"
    return None


# ===========================================================================
# Forecaster — probabilistic: many sampled paths, median candle + P(up) + bands
# ===========================================================================
def next_session_timestamps(last_ts, n):
    """Next n 15m bar starts, skipping the overnight gap (15:15 -> next 09:15)
    and weekends. Kronos uses time features, so wrong timestamps hurt."""
    out, t = [], last_ts
    for _ in range(n):
        t = t + timedelta(minutes=15)
        if t.hour * 60 + t.minute > 15 * 60 + 15:
            t = (t + timedelta(days=1)).replace(hour=9, minute=15, second=0, microsecond=0)
        while t.weekday() >= 5:
            t = t + timedelta(days=1)
        out.append(t)
    return out


class KronosForecaster:
    COLS6 = ["open", "high", "low", "close", "volume", "amount"]

    def __init__(self, cfg):
        LOG.info("Loading Kronos (%s) …", cfg.model_id)
        device = cfg.device
        if device == "auto":
            try:
                import torch
                device = "cuda:0" if torch.cuda.is_available() else "cpu"
            except Exception:
                device = "cpu"
        self.device = device
        self.tokenizer = KronosTokenizer.from_pretrained(cfg.tokenizer_id)
        self.model = Kronos.from_pretrained(cfg.model_id)
        try:
            self.predictor = KronosPredictor(
                self.model, self.tokenizer,
                device=device, max_context=cfg.max_context,
            )
        except TypeError:
            self.predictor = KronosPredictor(
                self.model, self.tokenizer, device=device
            )
        self.cfg = cfg
        LOG.info("Kronos ready on %s", device)

    def _repair(self, f):
        f = f.copy()
        for idx in f.index:
            o, h, l, c = (
                float(f.at[idx, "open"]), float(f.at[idx, "high"]),
                float(f.at[idx, "low"]), float(f.at[idx, "close"]),
            )
            th, tl = max(o, c, h), min(o, c, l)
            if th > h:
                f.at[idx, "high"] = th
            if tl < l:
                f.at[idx, "low"] = tl
        return f

    def _sample_paths(self, x, x_ts, y_ts, n):
        cfg = self.cfg
        t0 = time.time()
        paths = []
        kw = dict(pred_len=n, T=cfg.temperature, top_p=cfg.top_p,
                  sample_count=1, verbose=False)
        if cfg.use_batch and hasattr(self.predictor, "predict_batch") and cfg.n_paths > 1:
            try:
                remaining = cfg.n_paths
                while remaining > 0 and (time.time() - t0) < cfg.max_forecast_sec:
                    k = min(cfg.batch_chunk, remaining)
                    outs = self.predictor.predict_batch(
                        df_list=[x] * k, x_timestamp_list=[x_ts] * k,
                        y_timestamp_list=[y_ts] * k, **kw,
                    )
                    paths.extend(o for o in outs if o is not None and len(o) >= n)
                    remaining -= k
                if paths:
                    return paths
            except Exception as e:
                LOG.warning("predict_batch failed (%s); falling back to loop", e)
                paths = []
        for _ in range(cfg.n_paths):
            if paths and (time.time() - t0) > cfg.max_forecast_sec:
                break
            try:
                f = self.predictor.predict(df=x, x_timestamp=x_ts, y_timestamp=y_ts, **kw)
                if f is not None and len(f) >= n:
                    paths.append(f)
            except Exception:
                continue
        return paths

    def forecast_next(self, window_df):
        n = self.cfg.pred_len
        if len(window_df) != self.cfg.lookback:
            raise ValueError(f"Expected {self.cfg.lookback}, got {len(window_df)}")

        x = window_df.copy()
        if "amount" not in x.columns:
            x["amount"] = x["close"] * x["volume"]
        x = x[self.COLS6]

        forecast_ts = next_session_timestamps(window_df.index[-1], n)
        x_ts = pd.Series(pd.DatetimeIndex(window_df.index))
        y_ts = pd.Series(forecast_ts)

        paths = self._sample_paths(x, x_ts, y_ts, n)
        if not paths:
            raise RuntimeError("All forecast samples failed.")

        cols = ["open", "high", "low", "close", "volume"]
        S = np.stack([p.iloc[:n][cols].values.astype(np.float64) for p in paths])
        S = S[np.isfinite(S).all(axis=(1, 2))]
        if len(S) == 0:
            raise RuntimeError("Forecast samples were all NaN.")

        # Median candle per step is robust to the odd wild sample (mean is not).
        med = np.median(S, axis=0)
        out = pd.DataFrame(med, columns=cols, index=pd.DatetimeIndex(forecast_ts))
        out = self._repair(out)

        last_close = float(window_df["close"].iloc[-1])
        closes = S[:, :, 3]
        out["p_up"] = (closes > last_close).mean(axis=0)
        out["close_p10"] = np.percentile(closes, 10, axis=0)
        out["close_p90"] = np.percentile(closes, 90, axis=0)
        out["n_paths"] = len(S)
        out.attrs["paths"] = S          # (n_paths, n_bars, [o,h,l,c,v]) for trade simulation
        return out


# ===========================================================================
# Display
# ===========================================================================
def _clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def _bar_time_str(ts):
    end = ts + timedelta(minutes=15)
    return f"{ts:%H:%M}-{end:%H:%M}"


def _ts(x):
    t = pd.Timestamp(x)
    return t.tz_localize(TZ) if t.tzinfo is None else t.tz_convert(TZ)


def day_context(closed_df, ltp=None):
    """Session stats for the manual trader: O/H/L, VWAP, opening range, prev-day levels."""
    last_day = closed_df["timestamp"].iloc[-1].date()
    dates = closed_df["timestamp"].dt.date
    today = closed_df[dates == last_day]
    prev = closed_df[dates < last_day]
    ctx = {"n_bars": len(today), "day_open": float(today["open"].iloc[0]),
           "day_high": float(today["high"].max()), "day_low": float(today["low"].min())}
    if ltp:
        ctx["day_high"] = max(ctx["day_high"], ltp)
        ctx["day_low"] = min(ctx["day_low"], ltp)
    vol = today["volume"].sum()
    if vol > 0:
        tp = (today["high"] + today["low"] + today["close"]) / 3.0
        ctx["vwap"] = float((tp * today["volume"]).sum() / vol)
    first = today.iloc[0]
    ctx["or_high"], ctx["or_low"] = float(first["high"]), float(first["low"])
    if len(prev):
        pday = prev[prev["timestamp"].dt.date == prev["timestamp"].iloc[-1].date()]
        ctx["pdc"] = float(pday["close"].iloc[-1])
        ctx["pdh"] = float(pday["high"].max())
        ctx["pdl"] = float(pday["low"].min())
    return ctx


def simulate_trade(paths, side, entry, stop, t1, cost):
    """Replay every sampled path bar-by-bar. Stop wins if a bar touches both levels
    (conservative). Unresolved paths are closed at the last bar's close."""
    risk, reward = abs(entry - stop), abs(t1 - entry)
    pnl, kinds = [], []
    for p in paths:
        res = None
        for j in range(p.shape[0]):
            hi, lo = p[j, 1], p[j, 2]
            hit_stop = (lo <= stop) if side == 1 else (hi >= stop)
            hit_t1 = (hi >= t1) if side == 1 else (lo <= t1)
            if hit_stop:
                res = (-risk - cost, "stop"); break
            if hit_t1:
                res = (reward - cost, "win"); break
        if res is None:
            res = (side * (p[-1, 3] - entry) - cost, "time")
        pnl.append(res[0]); kinds.append(res[1])
    n = len(pnl)
    return {"p_win": kinds.count("win") / n, "p_stop": kinds.count("stop") / n,
            "p_time": kinds.count("time") / n,
            "exp_pts": float(np.mean(pnl)), "exp_r": float(np.mean(pnl)) / risk if risk else 0.0}


def trust_state(score):
    """How much the live track record justifies trusting the model right now."""
    if not score or score["n"] < 15 or score["dirs"] < 8:
        return "unproven"
    hr = score["hits"] / score["dirs"]
    if hr < 0.5 or score["mae_model"] > score["mae_naive"] * 1.05:
        return "poor"
    return "ok"


def levels_between(ctx, a, b):
    """Named session levels strictly between prices a and b, nearest to a first."""
    if not ctx:
        return []
    names = [("vwap", "VWAP"), ("pdh", "PDH"), ("pdl", "PDL"), ("pdc", "PDC"),
             ("day_high", "DayH"), ("day_low", "DayL"), ("or_high", "OR-H"), ("or_low", "OR-L")]
    lo, hi = sorted([a, b])
    found = [(n, ctx[k]) for k, n in names if k in ctx and lo < ctx[k] < hi]
    return sorted(found, key=lambda x: abs(x[1] - a))


def build_trade_plan(cfg, fc, ref, rng, trend, signal_close, signal_ts,
                     ctx=None, score=None, lot=None, dq_reason=None):
    """Turn the sampled forecast into a tradeable plan. Every number comes from the
    model's own paths (T1 = median final close, stop chosen by simulated expectancy);
    nothing is padded. If any gate fails, the answer is NO TRADE with the reasons."""
    p_up = fc["p_up"].values
    closes = fc["close"].values
    p_avg = float(np.mean(p_up))
    side = 1 if p_avg >= 0.5 else -1
    p_dir = p_avg if side == 1 else 1 - p_avg
    t1 = float(closes[-1])
    reward = side * (t1 - ref)
    trust = trust_state(score)

    plan = {"side": side, "trade": False, "p_avg": p_avg, "p_dir": p_dir, "entry": ref,
            "t1": t1, "fails": [], "notes": [], "trust": trust, "sim": None}
    fails = plan["fails"]

    if dq_reason:
        fails.append(f"DATA: {dq_reason}")
    if p_dir < cfg.min_conf:
        fails.append(f"paths agree only {p_dir*100:.0f}% (need {cfg.min_conf*100:.0f}%)")
    if abs(ref - signal_close) > 0.5 * rng:
        fails.append(f"price is {ref - signal_close:+.1f} pts from the signal close "
                     f"(> {0.5*rng:.0f}) — forecast stale, wait for next bar")
    if reward < cfg.min_move_rng * rng:
        fails.append(f"model target is {reward:+.1f} pts from entry "
                     f"(need ≥ {cfg.min_move_rng*rng:.1f})")

    paths = fc.attrs.get("paths")
    if reward > 0 and paths is not None:
        cands = []
        for k in cfg.stop_grid:
            risk = k * rng
            stop = ref - side * risk
            cands.append((stop, risk, simulate_trade(paths, side, ref, stop, t1, cfg.cost_pts)))
        ok = [c for c in cands if c[2]["p_stop"] <= cfg.max_p_stop]
        # best expectancy among sane stops; if every stop gets hit too often, use the widest
        stop, risk, sim = max(ok, key=lambda c: c[2]["exp_r"]) if ok else cands[-1]
        rr = reward / risk
        ext = float(fc["close_p90"].iloc[-1] if side == 1 else fc["close_p10"].iloc[-1])
        t2 = ext if side * (ext - t1) > 0 else t1 + side * 0.5 * rng
        plan.update(stop=stop, risk=risk, reward=reward, rr=rr, sim=sim, t2=t2)
        if rr < cfg.min_rr:
            fails.append(f"R:R to T1 is {rr:.2f} (need ≥ {cfg.min_rr:.1f})")
        if sim["exp_r"] < cfg.min_exp_r:
            fails.append(f"simulated expectancy {sim['exp_r']:+.2f}R (need ≥ {cfg.min_exp_r:+.2f}R)")

    if fails:
        return plan

    plan["trade"] = True
    sim = plan["sim"]
    lvl = 2 if (sim["exp_r"] >= 0.5 and sim["p_win"] >= 0.55) else (1 if sim["exp_r"] >= 0.3 else 0)
    if (side == 1 and trend == "bearish") or (side == -1 and trend == "bullish"):
        lvl -= 1
        plan["notes"].append(f"counter-trend vs EMA20/50 ({trend}) — conviction lowered")
    last_bar = fc.index[-1]
    if signal_ts.hour * 60 + signal_ts.minute >= 14 * 60 + 45:
        lvl -= 1
        plan["notes"].append("late session — little time left; conviction lowered")
    if trust == "poor":
        lvl = 0
        plan["notes"].append("live track record is POOR — conviction forced to LOW")
    elif trust == "unproven":
        lvl = min(lvl, 1)
        plan["notes"].append("track record unproven (<15 scored forecasts) — conviction capped at MEDIUM")
    plan["conf"] = ["LOW", "MEDIUM", "HIGH"][max(lvl, 0)]

    if last_bar.date() != signal_ts.date():
        exit_by = signal_ts.replace(hour=15, minute=15, second=0, microsecond=0)
        plan["notes"].append("forecast horizon crosses the close — exit intraday, don't hold overnight")
    else:
        exit_by = last_bar + timedelta(minutes=15)
    plan["exit_by"] = exit_by
    plan["in_path"] = levels_between(ctx, ref, t1)
    plan["cushion"] = levels_between(ctx, ref, plan["stop"])
    if cfg.risk_rs > 0 and lot:
        plan["lots"] = int(cfg.risk_rs // (plan["risk"] * lot))
    return plan


def load_scorecard(cfg, closed_df):
    """Score past logged forecasts against what actually printed (+1 bar)."""
    path = cfg.prediction_log_file
    if not os.path.exists(path):
        return None
    recs = []
    with open(path, "r", encoding="utf-8") as fh:
        for ln in fh.readlines()[-600:]:
            try:
                recs.append(json.loads(ln))
            except Exception:
                pass
    closes = closed_df.set_index("timestamp")["close"]
    seen, rows = set(), []
    for r in reversed(recs):
        try:
            sb = _ts(r["signal_bar"])
            if sb in seen:
                continue
            seen.add(sb)
            p0 = r["predictions"][0]
            pt = _ts(p0["ts"])
            if sb not in closes.index or pt not in closes.index:
                continue
            sc = float(r["signal_close"])
            if abs(float(closes[sb]) - sc) > 0.5:      # different contract/data
                continue
            ac, pc = float(closes[pt]), float(p0["close"])
            rows.append({"sc": sc, "pc": pc, "ac": ac, "p_up": p0.get("p_up")})
        except Exception:
            continue
        if len(rows) >= cfg.scorecard_n:
            break
    if not rows:
        return None
    hit = [(r["pc"] - r["sc"]) * (r["ac"] - r["sc"]) > 0 for r in rows
           if abs(r["pc"] - r["sc"]) > 0.05 and abs(r["ac"] - r["sc"]) > 0.05]
    conv = [(r["p_up"] - 0.5) * (r["ac"] - r["sc"]) > 0 for r in rows
            if r["p_up"] is not None and (r["p_up"] >= 0.7 or r["p_up"] <= 0.3)
            and abs(r["ac"] - r["sc"]) > 0.05]
    return {
        "n": len(rows), "hits": sum(hit), "dirs": len(hit),
        "conv_hits": sum(conv), "conv_n": len(conv),
        "mae_model": float(np.mean([abs(r["pc"] - r["ac"]) for r in rows])),
        "mae_naive": float(np.mean([abs(r["sc"] - r["ac"]) for r in rows])),
    }


def print_full_display(
    cfg, candle_row, forecast_df, closed_df, instrument_key, expiry_date,
    live_ltp, spot_ltp, last_check, now, next_update_at,
    plan=None, ctx=None, score=None, lot_size=None, rng=None, dq=None,
):
    signal_close = float(candle_row["close"])
    atr = float(candle_row["atr14"])
    ema20, ema50 = float(candle_row["ema20"]), float(candle_row["ema50"])
    trend = "bullish" if ema20 > ema50 else ("bearish" if ema20 < ema50 else "neutral")
    bar_ts = candle_row["timestamp"]
    ref = live_ltp or signal_close
    width = 96
    line, thin = "═" * width, "─" * width
    secs = max(0, int((next_update_at - now).total_seconds()))
    dte = (expiry_date - now.date()).days if expiry_date else None
    npaths = int(forecast_df["n_paths"].iloc[0]) if "n_paths" in forecast_df else 0
    lot = lot_size or 0

    print(f"\n{line}")
    print(f"  KRONOS  |  NIFTY FUT {expiry_date} ({dte}d)  |  {now:%Y-%m-%d %H:%M:%S} IST  |  "
          f"next update {next_update_at:%H:%M:%S} (in {secs//60}m{secs%60:02d}s)")
    print(f"  {instrument_key}   lot {lot or '?'}   {npaths} paths   T={cfg.temperature}   "
          f"model {cfg.model_id.split('/')[-1]}")
    print(line)

    # ================= ACTION (what to do right now) =================
    if plan and plan["trade"]:
        s, sim = plan["side"], plan["sim"]
        name = "BUY / LONG  ▲" if s == 1 else "SELL / SHORT  ▼"
        print(f"  ACTION:  {name}      conviction {plan['conf']}")
        print(f"    Win {sim['p_win']*100:.0f}%  |  Stop-out {sim['p_stop']*100:.0f}%  |  "
              f"No result by exit {sim['p_time']*100:.0f}%   "
              f"(of {npaths} simulated paths, after {cfg.cost_pts:.1f} pt costs)")
        print(f"    Expectancy {sim['exp_r']:+.2f}R  ({sim['exp_pts']:+.1f} pts per trade)   "
              f"R:R to T1 {plan['rr']:.2f}")
        print(thin)
        print(f"    Entry     {plan['entry']:>10,.2f}   (current LTP)")
        rs = f"   = ₹{plan['risk']*lot:,.0f}/lot" if lot else ""
        print(f"    Stop      {plan['stop']:>10,.2f}   risk {plan['risk']:.1f} pts{rs}")
        rw = f"   = ₹{plan['reward']*lot:,.0f}/lot" if lot else ""
        print(f"    Target 1  {plan['t1']:>10,.2f}   +{plan['reward']:.1f} pts{rw}   "
              f"(model median close, {forecast_df.index[-1]:%H:%M} bar)")
        print(f"    Target 2  {plan['t2']:>10,.2f}   stretch (10/90th pct of final close)")
        if "lots" in plan:
            print(f"    Size      {plan['lots']} lot(s) for ₹{cfg.risk_rs:,.0f} max loss"
                  f"{'   (stop too wide for this budget)' if plan['lots'] == 0 else ''}")
        print(f"    Exit by   {plan['exit_by']:%H:%M} or at T1/stop, whichever first; "
              f"a close beyond the stop = out.")
        if plan["in_path"]:
            print("    In the way: " + ", ".join(f"{n} {p:,.1f}" for n, p in plan["in_path"]))
        if plan["cushion"]:
            print("    Behind stop: " + ", ".join(f"{n} {p:,.1f}" for n, p in plan["cushion"]))
        for n_ in plan["notes"]:
            print(f"    ⚠ {n_}")
    else:
        lean = "SHORT ▼" if (plan and plan["side"] == -1) else "LONG ▲"
        print(f"  ACTION:  ◼ NO TRADE")
        if plan:
            print(f"    Model leans {lean} (paths agree {plan['p_dir']*100:.0f}%) but it is blocked:")
            for f_ in plan["fails"]:
                print(f"      ✗ {f_}")
            if plan["sim"]:
                print(f"    (if forced: win {plan['sim']['p_win']*100:.0f}%, "
                      f"expectancy {plan['sim']['exp_r']:+.2f}R)")

    # ================= data check (futures vs spot) =================
    if cfg.xcheck_enabled:
        if not dq or not dq.get("available"):
            print("  ⚠ DATA: spot cross-check unavailable — futures candles are UNVERIFIED")
        else:
            rec = closed_df.tail(60)
            bad = rec[rec["repaired"]] if "repaired" in rec else rec.iloc[0:0]
            if len(bad):
                print(f"  ⚠ DATA: {len(bad)} of last {len(rec)} futures bars failed the spot "
                      f"cross-check → rebuilt from spot + basis ({dq['basis_now']:+.1f}):")
                for _, r in bad.tail(4).iterrows():
                    print(f"      ~{r['timestamp']:%d %H:%M}  raw L {r['raw_low']:,.1f} / "
                          f"H {r['raw_high']:,.1f}   →   L {r['low']:,.1f} / H {r['high']:,.1f}")
            else:
                print(f"  DATA ✓ {dq['n_checked']} bars verified vs spot index "
                      f"(basis {dq['basis_now']:+.1f}, tol ±{dq['tol']:.0f})")

    # ================= data-quality warnings =================
    warns = []
    if not live_ltp:
        warns.append("Live LTP unavailable — entry uses signal-bar close; don't trade off this screen")
    age = (now - (bar_ts + timedelta(minutes=15))).total_seconds()
    if age > 300:
        warns.append(f"Forecast is {age/60:.0f} min old")
    if dte is not None and dte <= 1:
        warns.append("Expiry day/eve — erratic moves; consider the next series")
    if npaths and npaths < 8:
        warns.append(f"Only {npaths} sampled paths — probabilities are noisy")
    for w in warns:
        print(f"  ⚠ {w}")

    # ================= market =================
    print(f"\n{thin}")
    if live_ltp:
        chg = ""
        if ctx and "pdc" in ctx:
            chg = f"   {live_ltp - ctx['pdc']:+.2f} ({(live_ltp/ctx['pdc']-1)*100:+.2f}%) vs prev close"
        sp = f"   |   spot {spot_ltp:,.2f}  basis {live_ltp - spot_ltp:+.2f}" if spot_ltp else ""
        print(f"  FUT {live_ltp:,.2f}{chg}{sp}")
    if ctx:
        v = f"VWAP {ctx['vwap']:,.2f} (LTP {ref - ctx['vwap']:+.1f})" if "vwap" in ctx else "VWAP n/a"
        print(f"  TODAY  O {ctx['day_open']:,.2f}  H {ctx['day_high']:,.2f}  L {ctx['day_low']:,.2f}   {v}")
        if "pdh" in ctx:
            print(f"  PREV   H {ctx['pdh']:,.2f}  L {ctx['pdl']:,.2f}  C {ctx['pdc']:,.2f}   "
                  f"|  OR(09:15) H {ctx['or_high']:,.2f} L {ctx['or_low']:,.2f}")
    rg = f"{rng:.1f}" if rng else "n/a"
    print(f"  TREND  EMA20 {ema20:,.2f} / EMA50 {ema50:,.2f} ({trend})   |   "
          f"avg 15m range {rg} pts (ex-open bar)   ATR14 {atr:.1f} (incl. gaps)")
    print(f"  SIGNAL BAR {_bar_time_str(bar_ts)}  O {candle_row['open']:,.2f}  "
          f"H {candle_row['high']:,.2f}  L {candle_row['low']:,.2f}  C {signal_close:,.2f}")

    # ================= forecast =================
    print(f"\n{thin}")
    print(f"  FORECAST — median of {npaths} sampled paths   (vsLTP = distance from {ref:,.2f})")
    print(thin)
    print(f"  {'#':<3} {'Window':<12} {'Open':>9} {'High':>9} {'Low':>9} {'Close':>9} "
          f"{'Move':>7} {'vsLTP':>7} {'P(up)':>6}  {'Close 10–90%':<19}")
    prev_close = signal_close
    crossed = False
    for i in range(len(forecast_df)):
        r = forecast_df.iloc[i]
        c = float(r["close"])
        mv = c - prev_close
        arrow = "▲" if mv > 0.05 else ("▼" if mv < -0.05 else "—")
        band = f"{r['close_p10']:,.1f}–{r['close_p90']:,.1f}" if "close_p10" in r else ""
        nd = forecast_df.index[i].date() != bar_ts.date()
        crossed = crossed or nd
        print(f"  +{i+1:<2} {_bar_time_str(forecast_df.index[i]):<12} {r['open']:>9,.2f} "
              f"{r['high']:>9,.2f} {r['low']:>9,.2f} {c:>9,.2f} {mv:>+6.1f}{arrow} "
              f"{c - ref:>+7.1f} {r['p_up']*100:>5.0f}%  {band:<19}{' *' if nd else ''}")
        prev_close = c
    if crossed:
        print("  * next trading session (overnight gap not modelled)")

    # ================= track record =================
    print(f"\n{thin}")
    if score and score["dirs"] >= 1:
        hr = score["hits"] / score["dirs"] * 100
        txt = (f"  TRACK RECORD (last {score['n']} forecasts, +1 bar): "
               f"direction {score['hits']}/{score['dirs']} = {hr:.0f}%")
        if score["conv_n"]:
            txt += f"   |   strong-lean {score['conv_hits']}/{score['conv_n']}"
        print(txt)
        better = score["mae_model"] < score["mae_naive"]
        print(f"    close error: model {score['mae_model']:.1f} pts vs 'no change' {score['mae_naive']:.1f} pts  "
              f"→ {'beating baseline' if better else 'NOT beating baseline'}   "
              f"[trust: {trust_state(score).upper()}]")
    else:
        print("  TRACK RECORD: building — [trust: UNPROVEN]")
    if last_check:
        fc_ts, fc_close, actual_close = last_check
        print(f"  Last bar [{fc_ts:%H:%M}]: predicted {fc_close:,.2f}  actual {actual_close:,.2f}  "
              f"err {fc_close - actual_close:+.1f}")

    # ================= last bars =================
    print(f"\n{thin}")
    print(f"  LAST {cfg.show_last_n_bars} CLOSED BARS (verify vs broker NIFTY FUT {expiry_date})")
    print(f"  {'Time':<6} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10} {'Vol':>10} {'Range':>7}")
    n_bars = min(cfg.show_last_n_bars, len(closed_df))
    for i in range(len(closed_df) - n_bars, len(closed_df)):
        r = closed_df.iloc[i]
        mk = "~" if ("repaired" in r.index and bool(r["repaired"])) else " "
        print(f" {mk}{r['timestamp']:%H:%M}  {r['open']:>10,.2f} {r['high']:>10,.2f} "
              f"{r['low']:>10,.2f} {r['close']:>10,.2f} {r['volume']:>10,.0f} "
              f"{r['high'] - r['low']:>7.1f}")
    print(f"{line}\n")


def print_test_bar_display(cfg, candle_row, forecast_df, actual_bars=None,
                           instrument_key="", expiry_date=None):
    signal_close = float(candle_row["close"])
    atr = float(candle_row["atr14"])
    ema20 = float(candle_row["ema20"])
    ema50 = float(candle_row["ema50"])
    trend = "bullish" if ema20 > ema50 else ("bearish" if ema20 < ema50 else "neutral")
    bar_ts = candle_row["timestamp"]
    width = 88
    line = "═" * width

    print(f"\n{line}")
    print(f"  KRONOS {len(forecast_df)}-CANDLE FORECAST (test bar)")
    print(f"  Instrument: {instrument_key}  (expiry {expiry_date})")
    print(line)
    print(f"  Signal bar        {bar_ts:%Y-%m-%d %H:%M}  "
          f"({_bar_time_str(bar_ts)})")
    print(f"    Open   {candle_row['open']:>10,.2f}    "
          f"High {candle_row['high']:>10,.2f}")
    print(f"    Low    {candle_row['low']:>10,.2f}    "
          f"Close {candle_row['close']:>10,.2f}")
    print(f"    ATR14  {atr:>10,.2f}    "
          f"EMA20/50 {ema20:,.2f} / {ema50:,.2f} ({trend})")
    print("─" * width)
    print(f"  {'#':<3} {'Time window':<13} {'Open':>10} {'High':>10} "
          f"{'Low':>10} {'Close':>10} {'Move':>9} {'Dir':<4} {'Range':>7}")
    print("─" * width)
    prev = signal_close
    for i in range(len(forecast_df)):
        r = forecast_df.iloc[i]
        o, h, l, c = (
            float(r["open"]), float(r["high"]),
            float(r["low"]), float(r["close"]),
        )
        move = c - prev
        rng = h - l
        direction = "▲" if move > 0.05 else ("▼" if move < -0.05 else "—")
        bar_start = forecast_df.index[i]
        print(
            f"  +{i+1:<2} {_bar_time_str(bar_start):<13} "
            f"{o:>10,.2f} {h:>10,.2f} {l:>10,.2f} {c:>10,.2f} "
            f"{move:>+8.2f} {direction:<4} {rng:>6.2f}"
        )
        prev = c
    print(line)

    if actual_bars is not None and not actual_bars.empty:
        print(f"\n  ACTUAL subsequent bars (for reference):")
        print(f"  {'#':<3} {'Time window':<13} {'Open':>10} {'High':>10} "
              f"{'Low':>10} {'Close':>10} {'Move':>9}")
        prev = signal_close
        for i, (_, r) in enumerate(actual_bars.iterrows()):
            move = float(r["close"]) - prev
            bar_start = r["timestamp"]
            print(
                f"  +{i+1:<2} {_bar_time_str(bar_start):<13} "
                f"{r['open']:>10,.2f} {r['high']:>10,.2f} "
                f"{r['low']:>10,.2f} {r['close']:>10,.2f} "
                f"{move:>+8.2f}"
            )
            prev = float(r["close"])
        print(line + "\n")


# ===========================================================================
# Bar scheduling
# ===========================================================================
def is_weekend(now):
    return now.weekday() >= 5


def is_session_extended(now, cfg):
    if is_weekend(now):
        return False
    if now.time() < cfg.session_open:
        return False
    buffer_time = (
        datetime.combine(now.date(), cfg.session_close)
        + timedelta(seconds=cfg.session_close_buffer_sec)
    ).time()
    return now.time() < buffer_time


def latest_closed_bar(now, cfg):
    mins = now.minute
    last_boundary_min = (mins // 15) * 15
    last_boundary = now.replace(minute=last_boundary_min, second=0, microsecond=0)
    elapsed = (now - last_boundary).total_seconds()
    if elapsed >= cfg.bar_close_grace_sec:
        candidate = last_boundary - timedelta(minutes=15)
    else:
        candidate = last_boundary - timedelta(minutes=30)
    session_start = now.replace(
        hour=cfg.session_open.hour,
        minute=cfg.session_open.minute,
        second=0, microsecond=0,
    )
    if candidate < session_start:
        return None
    return candidate


def next_bar_ready_at(now, cfg):
    mins = now.minute
    next_boundary_min = ((mins // 15) + 1) * 15
    base = now.replace(minute=0, second=0, microsecond=0)
    target = base + timedelta(
        minutes=next_boundary_min, seconds=cfg.bar_close_grace_sec
    )
    if target <= now:
        target += timedelta(minutes=15)
    return target


# ===========================================================================
# Live runner
# ===========================================================================
class LiveRunner:
    def __init__(self, cfg, client, forecaster, instrument_key):
        self.cfg = cfg
        self.client = client
        self.forecaster = forecaster
        self.instrument_key = instrument_key
        self.last_bar_ts = None
        self.pending_forecast = None

    def run(self):
        now = datetime.now(TZ)
        if is_weekend(now):
            LOG.error("Weekend — no session today.")
            return
        if now.time() < self.cfg.session_open:
            target = now.replace(
                hour=self.cfg.session_open.hour,
                minute=self.cfg.session_open.minute,
                second=self.cfg.bar_close_grace_sec,
                microsecond=0,
            )
            print(f"\nSession opens at {self.cfg.session_open:%H:%M} IST. "
                  f"Sleeping {((target - now).total_seconds()/60):.1f} min …")
            time.sleep((target - now).total_seconds())
        elif now.time() >= self.cfg.session_close:
            LOG.error("Session already closed.")
            return

        print("\nLive forecast loop started. Ctrl+C to stop.")
        time.sleep(1)

        while True:
            now = datetime.now(TZ)
            if not is_session_extended(now, self.cfg):
                print("\nSession ended. Exiting loop.")
                break

            closed = latest_closed_bar(now, self.cfg)
            if closed is None:
                nr = next_bar_ready_at(now, self.cfg)
                s = (nr - now).total_seconds()
                if s > 0:
                    print(f"Waiting for first bar. Sleeping {s:.0f}s …")
                    time.sleep(s)
                continue

            if closed == self.last_bar_ts:
                nr = next_bar_ready_at(now, self.cfg)
                s = (nr - now).total_seconds()
                if s > 0:
                    time.sleep(s)
                continue

            try:
                ok = self._handle_bar(closed)
                if ok:
                    self.last_bar_ts = closed
                else:
                    time.sleep(self.cfg.bar_publish_poll_sec)
            except PermissionError as e:
                print(f"AUTH FAILURE: {e}")
                break
            except Exception as e:
                import traceback
                print(f"Bar {closed:%H:%M} error: {e}")
                traceback.print_exc()
                self.last_bar_ts = closed

    def _fetch_until_bar_available(self, bar_start):
        deadline = time.time() + self.cfg.bar_publish_timeout_sec
        last_df = None
        attempts = 0
        while time.time() < deadline:
            attempts += 1
            to_date = datetime.now(TZ).strftime("%Y-%m-%d")
            from_date = (
                datetime.now(TZ) - timedelta(days=self.cfg.live_fetch_days)
            ).strftime("%Y-%m-%d")
            try:
                df = self.client.fetch_clean(
                    self.instrument_key, from_date, to_date, self.cfg
                )
            except Exception as e:
                print(f"  fetch attempt {attempts} failed: {e}")
                time.sleep(self.cfg.bar_publish_poll_sec)
                continue
            last_df = df
            if not df.empty and (df["timestamp"] == bar_start).any():
                return df, True
            if attempts == 1:
                print(f"  Bar {bar_start:%H:%M} not yet in API; "
                      f"polling up to {self.cfg.bar_publish_timeout_sec}s …")
            time.sleep(self.cfg.bar_publish_poll_sec)
        return last_df, False

    def _handle_bar(self, bar_start):
        df, found = self._fetch_until_bar_available(bar_start)
        if df is None or not found:
            latest = df["timestamp"].iloc[-1] if df is not None and not df.empty else "none"
            print(f"[{bar_start:%H:%M}] bar not published after "
                  f"{self.cfg.bar_publish_timeout_sec}s "
                  f"(latest: {latest}); skipping.")
            return False

        # CLOSED bars only, indicators computed after filtering
        dq = self.client.last_dq
        closed_df = df[df["timestamp"] <= bar_start].copy()
        closed_df = add_context(closed_df, self.cfg)

        window = closed_df.tail(self.cfg.lookback)
        if len(window) < self.cfg.lookback:
            print(f"[{bar_start:%H:%M}] only {len(window)} closed bars; skipping.")
            return False

        row = window.iloc[-1]
        if row["timestamp"] != bar_start:
            print(f"[{bar_start:%H:%M}] WARNING: window ends at "
                  f"{row['timestamp']}, not target bar. Skipping.")
            return False

        # Previous check
        last_check = None
        if self.pending_forecast is not None:
            prev_ts, prev_fc_close = self.pending_forecast
            if prev_ts == bar_start:
                actual_row = df[df["timestamp"] == bar_start]
                if not actual_row.empty:
                    last_check = (
                        bar_start,
                        prev_fc_close,
                        float(actual_row.iloc[0]["close"]),
                    )
            self.pending_forecast = None

        t_fc = time.time()
        # Forecast
        window_idx = window.set_index("timestamp")
        try:
            forecast = self.forecaster.forecast_next(window_idx)
        except Exception as e:
            import traceback
            print(f"[{bar_start:%H:%M}] forecast failed: {e}")
            traceback.print_exc()
            return False

        LOG.info("Forecast done in %.0fs", time.time() - t_fc)

        # Live quotes AFTER the forecast so levels reflect the price right now
        live_ltp = spot_ltp = None
        if self.cfg.show_live_ltp:
            live_ltp = self.client.get_live_ltp(self.instrument_key)
        if self.cfg.show_spot_basis:
            spot_ltp = self.client.get_live_ltp(SPOT_INSTRUMENT)

        ref = live_ltp or float(row["close"])
        atr = float(row["atr14"])
        r14 = row.get("rng14")
        rng = float(r14) if r14 is not None and np.isfinite(r14) else 0.6 * atr
        trend = "bullish" if row["ema20"] > row["ema50"] else (
            "bearish" if row["ema20"] < row["ema50"] else "neutral")
        ctx = day_context(closed_df, live_ltp)
        try:
            score = load_scorecard(self.cfg, closed_df)
        except Exception:
            score = None
        plan = build_trade_plan(self.cfg, forecast, ref, rng, trend,
                                float(row["close"]), bar_start,
                                ctx=ctx, score=score, lot=self.client.lot_size,
                                dq_reason=dq_tail_reason(closed_df, self.cfg))

        if self.cfg.clear_screen:
            _clear_screen()
        now = datetime.now(TZ)
        next_ready = next_bar_ready_at(now, self.cfg)
        print_full_display(
            self.cfg, row, forecast, closed_df,
            self.instrument_key, self.client.expiry_date,
            live_ltp, spot_ltp, last_check, now, next_ready,
            plan=plan, ctx=ctx, score=score, lot_size=self.client.lot_size, rng=rng, dq=dq,
        )

        # Persist
        try:
            record = {
                "signal_bar": str(bar_start),
                "signal_close": float(row["close"]),
                "atr14": float(row["atr14"]),
                "side": plan["side"] if plan["trade"] else 0,
                "trade": plan["trade"],
                "exp_r": plan["sim"]["exp_r"] if plan.get("sim") else None,
                "predictions": [
                    {
                        "ts": str(forecast.index[i]),
                        "open": float(forecast.iloc[i]["open"]),
                        "high": float(forecast.iloc[i]["high"]),
                        "low": float(forecast.iloc[i]["low"]),
                        "close": float(forecast.iloc[i]["close"]),
                        "p_up": float(forecast.iloc[i]["p_up"]),
                    }
                    for i in range(len(forecast))
                ],
            }
            with open(self.cfg.prediction_log_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except Exception:
            pass

        first_ts = forecast.index[0]
        first_close = float(forecast.iloc[0]["close"])
        self.pending_forecast = (first_ts, first_close)
        return True


# ===========================================================================
# Test one historical bar
# ===========================================================================
def run_test_bar(cfg, client, forecaster, instrument_key, bar_iso):
    bar_start = pd.Timestamp(bar_iso)
    bar_start = (
        bar_start.tz_localize(TZ)
        if bar_start.tz is None
        else bar_start.tz_convert(TZ)
    )
    print(f"\nTesting forecast for bar closed at {bar_start}")

    to_date = datetime.now(TZ).strftime("%Y-%m-%d")
    from_date = (
        datetime.now(TZ) - timedelta(days=cfg.live_fetch_days)
    ).strftime("%Y-%m-%d")
    df = client.fetch_clean(instrument_key, from_date, to_date, cfg)

    closed_df = df[df["timestamp"] <= bar_start].copy()
    closed_df = add_context(closed_df, cfg)
    window = closed_df.tail(cfg.lookback)
    if len(window) < cfg.lookback:
        print(f"Not enough history: {len(window)} < {cfg.lookback}")
        return
    row = window.iloc[-1]
    if row["timestamp"] != bar_start:
        print(f"WARNING: last closed bar is {row['timestamp']}, not {bar_start}")

    window_idx = window.set_index("timestamp")
    forecast = forecaster.forecast_next(window_idx)

    n = cfg.pred_len
    actual = df[df["timestamp"] > bar_start].head(n).reset_index(drop=True)

    print_test_bar_display(
        cfg, row, forecast, actual,
        instrument_key=instrument_key,
        expiry_date=client.expiry_date,
    )


# ===========================================================================
# Accuracy
# ===========================================================================
def run_accuracy(cfg, client, forecaster, instrument_key):
    print(f"\nRunning accuracy report on {cfg.history_days} days …")
    to_date = datetime.now(TZ).strftime("%Y-%m-%d")
    from_date = (
        datetime.now(TZ) - timedelta(days=cfg.history_days)
    ).strftime("%Y-%m-%d")
    df = client.fetch_clean(instrument_key, from_date, to_date, cfg)
    df = add_context(df, cfg).dropna(subset=["atr14", "ema20", "ema50"]).reset_index(drop=True)

    total = len(df) - 1 - cfg.lookback
    if total <= 0:
        print(f"Not enough data: {len(df)} candles, need ≥{cfg.lookback + 1}.")
        return

    hit = miss = flat = 0
    err_sum = 0.0
    within_range = 0
    processed = 0
    naive_err = 0.0
    conv_n = conv_hit = 0
    t0 = time.time()

    for idx, i in enumerate(range(cfg.lookback, len(df) - 1)):
        row = df.iloc[i]
        nxt = df.iloc[i + 1]
        window = df.iloc[i - cfg.lookback + 1 : i + 1].set_index("timestamp")

        try:
            fc = forecaster.forecast_next(window)
        except Exception:
            continue

        f = fc.iloc[0]
        current_close = float(row["close"])
        fc_close = float(f["close"])
        actual_close = float(nxt["close"])

        if abs(fc_close - current_close) < 0.05:
            flat += 1
        else:
            pred_dir = 1 if fc_close > current_close else -1
            actual_dir = 1 if actual_close > current_close else -1
            if pred_dir == actual_dir:
                hit += 1
            else:
                miss += 1

        err_sum += abs(fc_close - actual_close)
        naive_err += abs(current_close - actual_close)
        pu = float(f["p_up"]) if "p_up" in f.index else None
        if pu is not None and (pu >= 0.7 or pu <= 0.3) and abs(actual_close - current_close) > 0.05:
            conv_n += 1
            conv_hit += int((pu - 0.5) * (actual_close - current_close) > 0)
        if float(f["low"]) <= actual_close <= float(f["high"]):
            within_range += 1
        processed += 1

        if (idx + 1) % 50 == 0:
            elapsed = time.time() - t0
            eta = elapsed / (idx + 1) * (total - idx - 1)
            print(f"  {idx+1}/{total}  "
                  f"hit_rate={hit/max(hit+miss,1)*100:.1f}%  "
                  f"eta {eta/60:.1f} min")

    directional = hit + miss
    if directional == 0:
        print("No directional predictions produced.")
        return

    print(f"\n{'═'*60}")
    print(f"  ACCURACY REPORT  ({processed} bars, {cfg.history_days} days)")
    print(f"{'═'*60}")
    print(f"  Directional predictions: {directional}")
    print(f"    Correct:               {hit}")
    print(f"    Wrong:                 {miss}")
    print(f"    Direction accuracy:    {hit/directional*100:.1f}%")
    print(f"  Flat predictions:        {flat}")
    print(f"  Mean absolute error of predicted close: "
          f"{err_sum/max(processed,1):.2f} pts")
    print(f"  'No change' baseline error:            "
          f"{naive_err/max(processed,1):.2f} pts  (model must beat this)")
    if conv_n:
        print(f"  Strong-lean calls (P(up)>=70% or <=30%): "
              f"{conv_hit}/{conv_n} = {conv_hit/conv_n*100:.1f}%")
    print(f"  Actual close within predicted range:   "
          f"{within_range/max(processed,1)*100:.1f}%")
    print(f"{'═'*60}\n")


# ===========================================================================
# Forecast audit
# ===========================================================================
def run_forecast_audit(cfg, client, forecaster, instrument_key, target_date):
    to_date = datetime.now(TZ).strftime("%Y-%m-%d")
    from_date = (
        datetime.now(TZ) - timedelta(days=cfg.live_fetch_days)
    ).strftime("%Y-%m-%d")
    df = client.fetch_clean(instrument_key, from_date, to_date, cfg)
    df = add_context(df, cfg).dropna(subset=["atr14", "ema20", "ema50"]).reset_index(drop=True)

    day_mask = df["timestamp"].dt.strftime("%Y-%m-%d") == target_date
    day_indices = df.index[day_mask].tolist()
    if not day_indices:
        print(f"No bars found for {target_date}")
        return

    print(f"\nForecast audit for {target_date}")
    print(f"{'time':<6} {'close':>10} {'pred_close':>11} "
          f"{'move%':>8} {'dir':<6} {'actual%':>8} {'hit':<4}")
    print("-" * 60)

    hits = misses = flats = 0
    for i in day_indices:
        if i < cfg.lookback or i + 1 >= len(df):
            continue
        window = df.iloc[i - cfg.lookback + 1 : i + 1].set_index("timestamp")
        try:
            fc = forecaster.forecast_next(window)
        except Exception as e:
            print(f"{df.iloc[i]['timestamp']:%H:%M} ERROR {e}")
            continue

        row = df.iloc[i]
        nxt = df.iloc[i + 1]
        current = float(row["close"])
        pred = float(fc.iloc[0]["close"])
        actual = float(nxt["close"])
        move_pct = (pred / current - 1) * 100
        actual_pct = (actual / current - 1) * 100

        if abs(pred - current) < 0.05:
            dir_str, mark = "flat", "—"
            flats += 1
        elif (pred > current and actual > current) or (pred < current and actual < current):
            dir_str = "up" if pred > current else "down"
            mark = "✓"
            hits += 1
        else:
            dir_str = "up" if pred > current else "down"
            mark = "✗"
            misses += 1

        print(
            f"{row['timestamp']:%H:%M}  {current:>10.2f}  "
            f"{pred:>11.2f}  {move_pct:>+7.3f}%  "
            f"{dir_str:<6}  {actual_pct:>+7.3f}%  {mark}"
        )

    directional = hits + misses
    if directional:
        print(f"\nDay accuracy: {hits}/{directional} = "
              f"{hits/directional*100:.1f}% (flats: {flats})")


# ===========================================================================
# Data check: show futures vs spot bar by bar
# ===========================================================================
def run_check_data(cfg, client, instrument_key):
    """Side by side: bars built from 1m  vs  Upstox's own 15m bars  vs  spot index."""
    to_date = datetime.now(TZ).strftime("%Y-%m-%d")
    from_date = (datetime.now(TZ) - timedelta(days=8)).strftime("%Y-%m-%d")
    client.source = "1m"
    built = client.fetch_candles(instrument_key, from_date, to_date)
    try:
        up = client._fetch_candles_15m(instrument_key, from_date, to_date)
    except Exception as e:
        print("Upstox 15m fetch failed:", e); up = None
    try:
        spot = client.fetch_candles(SPOT_INSTRUMENT, from_date, to_date)
    except Exception:
        spot = None
    m = built.rename(columns={c: "b_" + c for c in ("open", "high", "low", "close", "volume")})
    if up is not None:
        u = up[["timestamp", "open", "high", "low", "close"]].rename(
            columns={c: "u_" + c for c in ("open", "high", "low", "close")})
        m = m.merge(u, on="timestamp", how="outer")
    if spot is not None:
        s = spot[["timestamp", "high", "low", "close"]].rename(
            columns={c: "s_" + c for c in ("high", "low", "close")})
        m = m.merge(s, on="timestamp", how="left")
    m = m.sort_values("timestamp").tail(40)
    print("\nNIFTY FUT bars: built from 1-minute candles (b_) vs Upstox 15m endpoint (u_) vs SPOT (s_)")
    print("Compare b_ and u_ with your broker's NIFTY FUT chart. '≠' = the two Upstox sources disagree.")
    print(f"{'Time':<12} {'b_O':>9} {'b_H':>9} {'b_L':>9} {'b_C':>9} | {'u_H':>9} {'u_L':>9} {'u_C':>9} | "
          f"{'s_C':>9} {'b-s':>6}  note")
    for _, r in m.iterrows():
        g = lambda k: r[k] if k in r.index and pd.notna(r[k]) else np.nan
        bo, bh, bl, bc = g("b_open"), g("b_high"), g("b_low"), g("b_close")
        uh, ul, uc, sc = g("u_high"), g("u_low"), g("u_close"), g("s_close")
        note = ""
        if np.isnan(bc):
            note = "(not built: forming/incomplete)"
        elif not np.isnan(uc) and (abs(bh - uh) > 3 or abs(bl - ul) > 3 or abs(bc - uc) > 3):
            note = "≠ sources disagree"
        f = lambda x, w=9: f"{x:>{w},.1f}" if not np.isnan(x) else " " * (w - 1) + "-"
        bs = f"{bc - sc:>+6.1f}" if not (np.isnan(bc) or np.isnan(sc)) else "     -"
        print(f"{r['timestamp']:%d %H:%M}    {f(bo)} {f(bh)} {f(bl)} {f(bc)} | {f(uh)} {f(ul)} {f(uc)} | "
              f"{f(sc)} {bs}  {note}")
    print()


# ===========================================================================
# Entry
# ===========================================================================
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true")
    p.add_argument("--test-bar", type=str, default=None)
    p.add_argument("--accuracy", action="store_true")
    p.add_argument("--forecast-audit", type=str, default=None)
    p.add_argument("--no-clear", action="store_true")
    p.add_argument("--dump-raw", action="store_true")
    p.add_argument("--check-data", action="store_true",
                   help="print futures vs spot bars and flag the bad ones")
    p.add_argument("--source", choices=["1m", "15m"], default=None,
                   help="build 15m bars from 1m candles (default) or use Upstox 15m candles")
    p.add_argument("--no-xcheck", action="store_true",
                   help="disable the spot cross-check (not recommended)")
    p.add_argument("--paths", type=int, default=None, help="sampled paths per forecast")
    p.add_argument("--temp", type=float, default=None, help="sampling temperature")
    p.add_argument("--model", choices=["small", "base"], default=None,
                   help="Kronos-small (fast) or Kronos-base (4x params, slower)")
    p.add_argument("--device", type=str, default=None, help="auto | cpu | cuda:0")
    p.add_argument("--risk-rs", type=float, default=None,
                   help="max loss per trade in Rs; enables lot sizing")
    args = p.parse_args()

    cfg = Config()
    if args.no_clear:
        cfg.clear_screen = False
    if args.paths:
        cfg.n_paths = args.paths
    if args.temp is not None:
        cfg.temperature = args.temp
    if args.model:
        cfg.model_id = f"NeoQuasar/Kronos-{args.model}"
    if args.device:
        cfg.device = args.device
    if args.risk_rs:
        cfg.risk_rs = args.risk_rs
    if args.no_xcheck:
        cfg.xcheck_enabled = False
    if args.source:
        cfg.candle_source = args.source

    env = load_env_file("env.txt")
    token = get_access_token(env)
    client = UpstoxClient(token)
    client.source = cfg.candle_source
    instrument_key = client.get_current_nifty_futures_key()

    if args.dump_raw:
        client.dump_raw(instrument_key)
        return
    if args.check_data:
        run_check_data(cfg, client, instrument_key)
        return

    forecaster = KronosForecaster(cfg)

    if args.test_bar:
        run_test_bar(cfg, client, forecaster, instrument_key, args.test_bar)
        return
    if args.accuracy:
        run_accuracy(cfg, client, forecaster, instrument_key)
        return
    if args.forecast_audit:
        run_forecast_audit(
            cfg, client, forecaster, instrument_key, args.forecast_audit
        )
        return
    if args.live:
        print("\n" + "═" * 66)
        print(f"  KRONOS {cfg.pred_len}-CANDLE FORECAST — live")
        print("═" * 66)
        print(f"  Temperature: {cfg.temperature}, "
              f"paths: {cfg.n_paths}")
        print(f"  Lookback: {cfg.lookback} candles  |  "
              f"Predicting: {cfg.pred_len} candles ahead")
        print(f"  Data: historical + intraday (current session included)")
        print(f"  Display refresh: every completed bar")
        print("═" * 66)
        runner = LiveRunner(cfg, client, forecaster, instrument_key)
        try:
            runner.run()
        except KeyboardInterrupt:
            print("\nInterrupted.")
        return

    print("Choose: --live, --test-bar, --accuracy, --forecast-audit")


if __name__ == "__main__":
    main()