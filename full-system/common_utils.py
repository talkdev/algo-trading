# -*- coding: utf-8 -*-
"""
clock.py — time source abstraction.

Real mode uses wall-clock IST (Asia/Kolkata).  Demo mode uses a VirtualClock that
advances in fixed steps (default 5 min per engine cycle) so the whole pipeline —
entry window, regime confirmations, transitions, scheduled exits — can be
exercised offline in seconds.
"""
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
    _HAS_ZONEINFO = True
except Exception:                      # pragma: no cover - py<3.9 fallback
    from datetime import timezone
    IST = timezone(timedelta(hours=5, minutes=30), name="IST")
    _HAS_ZONEINFO = False


def now_ist() -> datetime:
    return datetime.now(IST)


class Clock:
    """Base clock — real wall time in IST."""
    def now(self) -> datetime:
        return now_ist()

    def sleep_seconds(self, s: float):
        import time
        time.sleep(s)


class VirtualClock(Clock):
    """Deterministic clock for offline demo/testing. Starts at the configured
    IST datetime and advances by `step` every `advance()` call."""
    def __init__(self, start: datetime, step: timedelta = timedelta(minutes=5)):
        self._t = start
        self._step = step

    def now(self) -> datetime:
        return self._t

    def advance(self, minutes: int = None) -> datetime:
        self._t += (timedelta(minutes=minutes) if minutes is not None else self._step)
        return self._t

    def set(self, dt: datetime):
        self._t = dt

    def sleep_seconds(self, s: float):
        # in virtual mode wall-clock sleeps are handled by the orchestrator;
        # expose a tiny real sleep so polling loops still yield
        import time
        time.sleep(min(s, 0.05))


# -*- coding: utf-8 -*-
"""
risk_manager.py — position sizing, risk limits, circuit breakers.

* Per-trade risk <= MAX_RISK_PER_TRADE (2% of capital), combined <= 4%.
* Ratio-spread style undefined-risk trades capped at 1% of capital.
* Daily loss circuit breaker at MAX_DAILY_LOSS (-3000 points).
* Stop-loss percentages scaled by VIX (SL_BASE_PERCENT @ SL_REFERENCE_VIX,
  clamped to [SL_MIN_PERCENT, SL_MAX_PERCENT]).
* Feed-staleness kill-switch (30 s) — invoked by main.py.
* Margin pre-check via broker.margin_available() before entries.
"""
import math

import config as C


class RiskManager:
    def __init__(self, broker, state, loggers=None, capital=C.INITIAL_CAPITAL):
        self.broker = broker
        self.state = state
        self.loggers = loggers
        self.capital = capital
        self.day_pnl_points = 0.0
        self.day_key = None

    # ------------------------------------------------------------------ days
    def _roll_day(self, clock):
        day = clock.now().date().isoformat()
        if self.day_key != day:
            self.day_key = day
            self.day_pnl_points = 0.0

    def add_day_pnl(self, points):
        self.day_pnl_points += points

    def daily_loss_breached(self) -> bool:
        return self.day_pnl_points <= C.MAX_DAILY_LOSS

    # ----------------------------------------------------------------- stops
    def stop_pct_for_vix(self, vix):
        """VIX-scaled stop percent on premium: base 30% @ VIX 14, clamp 18-40%."""
        pct = C.SL_BASE_PERCENT * (C.SL_REFERENCE_VIX / max(vix, 5.0))
        return max(C.SL_MIN_PERCENT, min(C.SL_MAX_PERCENT, pct))

    # ---------------------------------------------------------------- sizing
    def suggest_lots(self, plan, existing_risk_rupees=0.0):
        """Lots = floor(2% capital / (max_risk_points_per_lot * lot_size)),
        clamped by the 4% combined-risk cap. Never below 1 if plan is viable.
        `plan` must be built with lots=1 so max_risk_points is per-lot."""
        per_lot_risk_rupees = max(plan.max_risk_points, 1.0) * plan.lot_size
        risk_cap = C.MAX_RISK_PER_TRADE * self.capital
        lots = math.floor(risk_cap / per_lot_risk_rupees)
        # undefined-risk (ratio spread) trades capped at 1%
        if plan.strategy_name in ("RATIO_SPREAD_1x2",):
            lots = min(lots, math.floor(0.01 * self.capital / per_lot_risk_rupees))
        combined_cap = C.MAX_COMBINED_RISK * self.capital
        while lots > 0 and existing_risk_rupees + lots * per_lot_risk_rupees > combined_cap:
            lots -= 1
        return max(lots, 1)

    # ----------------------------------------------------------- margin check
    def margin_ok(self, plan) -> bool:
        # rough premium-notional margin requirement (paper model)
        notional = 0.0
        for leg in plan.legs:
            q = self.broker.quote(leg.instrument_key)
            notional += leg.qty * (q.get("last_price") or 0.0)
        required = max(notional * 0.5, 50_000.0)
        avail = self.broker.margin_available()
        ok = avail is None or required <= avail
        if self.loggers and not ok:
            self.loggers.audit("margin_insufficient", required=required, available=avail,
                               strategy=plan.strategy_name)
        return ok

    # --------------------------------------------------------- gamma exposure
    @staticmethod
    def gamma_ratio(open_shorts, spot, capital=C.INITIAL_CAPITAL):
        """Proxy for gamma exposure vs the combined-risk limit: dollar delta of
        short book / (4% of capital). 1.0 == limit."""
        dollar_delta = sum(abs(s.get("delta", 0.0) or 0.0) * s.get("qty", 0) * spot
                           for s in open_shorts)
        limit = C.MAX_COMBINED_RISK * capital
        return dollar_delta / limit if limit else 0.0

    # ------------------------------------------------------------- kill switch
    def kill_switch(self, reason):
        if self.loggers:
            self.loggers.audit("KILL_SWITCH", reason=reason)


# -*- coding: utf-8 -*-
"""
indicators.py — pure-math helpers shared by the regime detector, strategy
selector and strategy modules: Black-Scholes greeks, EMA, Wilder ADX/ATR,
realised volatility, delta-based strike helpers.
"""
import math
import statistics
from datetime import datetime


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot, strike, T_years, iv_pct, r, is_call):
    """Black-Scholes option price. iv_pct in percent (e.g. 17.5)."""
    if T_years <= 0 or iv_pct <= 0 or spot <= 0 or strike <= 0:
        intrinsic = max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)
        return intrinsic
    sig = iv_pct / 100.0
    sq = math.sqrt(T_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * sig ** 2) * T_years) / (sig * sq)
    d2 = d1 - sig * sq
    df = math.exp(-r * T_years)
    if is_call:
        return spot * norm_cdf(d1) - strike * df * norm_cdf(d2)
    return strike * df * norm_cdf(-d2) - spot * norm_cdf(-d1)


def bs_delta(spot, strike, T_years, iv_pct, r, is_call):
    """Black-Scholes delta. iv_pct in percent (e.g. 17.5)."""
    if T_years <= 0 or iv_pct <= 0 or spot <= 0 or strike <= 0:
        if is_call:
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0
    sq = math.sqrt(T_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv_pct ** 2 / 1e4) * T_years) / (iv_pct / 100.0 * sq)
    return norm_cdf(d1) if is_call else norm_cdf(d1) - 1.0


def ema_series(vals, span):
    if len(vals) < span:
        return []
    k = 2.0 / (span + 1.0)
    e = [sum(vals[:span]) / span]
    for v in vals[span:]:
        e.append(e[-1] + k * (v - e[-1]))
    return e


def _wilder(vals, n):
    if len(vals) < n:
        return []
    out = [sum(vals[:n]) / n]
    for v in vals[n:]:
        out.append((out[-1] * (n - 1) + v) / n)
    return out


def adx14(bars, n=14):
    """Wilder ADX. bars: ascending list of {'h','l','c'}. -> (adx, +di, -di) or None."""
    if len(bars) < 2 * n + 2:
        return None
    trs, pdms, ndms = [], [], []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        up, dn = bars[i]["h"] - bars[i - 1]["h"], bars[i - 1]["l"] - bars[i]["l"]
        pdms.append(up if (up > dn and up > 0) else 0.0)
        ndms.append(dn if (dn > up and dn > 0) else 0.0)
    atr = _wilder(trs, n)
    pdm_s = _wilder(pdms, n)
    ndm_s = _wilder(ndms, n)
    if not atr or len(atr) != len(pdm_s):
        return None
    pdi = [100.0 * p / a if a > 0 else 0.0 for p, a in zip(pdm_s, atr)]
    ndi = [100.0 * d / a if a > 0 else 0.0 for d, a in zip(ndm_s, atr)]
    dx = [abs(a - b) / (a + b) * 100 if (a + b) > 0 else 0.0 for a, b in zip(pdi, ndi)]
    adx_line = _wilder(dx, n)
    if not adx_line:
        return None
    return adx_line[-1], pdi[-1], ndi[-1]


def atr14(bars, n=14):
    """Wilder ATR over ascending bars -> float or None."""
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    w = _wilder(trs, n)
    return w[-1] if w else None


def sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def realised_vol_pct(closes, window=20, annualise=252):
    """Annualised realised vol (%) over the last `window` daily closes."""
    closes = closes[-(window + 1):]
    if len(closes) < window + 1:
        return None, len(closes) - 1
    rets = [math.log(closes[i + 1] / closes[i]) for i in range(len(closes) - 1)]
    return statistics.stdev(rets) * math.sqrt(annualise) * 100.0, len(rets)


def years_to_expiry(expiry_date, now: datetime):
    if expiry_date is None:
        return 1.0 / 365.0
    try:
        dt = datetime.combine(expiry_date, datetime.min.time())
        dt = dt.replace(hour=15, minute=30)
        if dt.tzinfo is None:
            from common_utils import IST
            dt = dt.replace(tzinfo=IST)
        return max((dt - now).total_seconds(), 1.0) / (365.0 * 24 * 3600)
    except Exception:
        return 1.0 / 365.0


def round_to_nearest(spot, step=50.0):
    return round(spot / step) * step


def round_down(spot, step=50.0):
    return math.floor(spot / step) * step


def round_up(spot, step=50.0):
    return math.ceil(spot / step) * step


def expected_move(spot, vix, days_to_expiry, annualise=365.0):
    return spot * (vix / 100.0) * math.sqrt(max(days_to_expiry, 0.001) / annualise)


def days_between(a, b):
    return (b - a).days


def pick_strike_by_delta(chain_legs, target_delta, is_call, spot=None):
    """chain_legs: list of (strike, leg_dict) for one side. Returns the strike
    whose delta is closest to target_delta (target_delta signed for puts)."""
    tgt = abs(target_delta) * (1 if is_call else -1)
    best, best_d = None, None
    for strike, leg in chain_legs:
        d = leg.get("delta")
        if d is None or not (0.01 < abs(d) < 0.99):
            continue
        if (is_call and d <= 0) or (not is_call and d >= 0):
            continue
        diff = abs(d - tgt)
        if best_d is None or diff < best_d:
            best, best_d = strike, diff
    return best


def zscore(value, series):
    if not series:
        return None
    sd = statistics.stdev(series)
    if sd < 1e-12:
        return None
    return (value - statistics.mean(series)) / sd


# -*- coding: utf-8 -*-
"""
logger_utils.py — thread-safe CSV/JSONL writers, console output, audit logs
with daily rotation (gzip compressed).  Spec §12: 'Log Rotation: Audit logs
rotated daily (gzip compressed).'
"""
import csv
import gzip
import json
import os
import shutil
import threading
import time
from datetime import datetime

from config import LOG_DIR, AUDIT_LOG_CSV, REGIME_LOG_CSV, TRADE_ANALYSIS_CSV, DAILY_LOG_MAX_BYTES

_USE_COLOR = True
_lock = threading.Lock()


# ---------------------------------------------------------------- ANSI colors
def _c(code, s):
    return f"\x1b[{code}m{s}\x1b[0m" if _USE_COLOR else str(s)


def g(s):  return _c("32", s)
def r(s):  return _c("31", s)
def y(s):  return _c("33", s)
def cy(s): return _c("36", s)
def mg(s): return _c("35", s)
def bo(s): return _c("1", s)
def dim(s): return _c("2", s)


def enable_color(v: bool):
    global _USE_COLOR
    _USE_COLOR = v


def score_str(v, width=2):
    if v is None:
        return dim("n/a".rjust(width))
    s = f"{v:+g}".rjust(width)
    return g(s) if v > 0 else (r(s) if v < 0 else y(s))


def fx(v, nd=2, sep=True):
    if v is None:
        return "n/a"
    return f"{v:{',' if sep else ''}.{nd}f}"


# --------------------------------------------------------------- CSV writer
class CsvWriter:
    """Append-only CSV with header creation + optional daily rotation."""

    def __init__(self, path, header, rotate_daily=False, rotate_bytes=DAILY_LOG_MAX_BYTES):
        self.path = path
        self.header = list(header)
        self.rotate_daily = rotate_daily
        self.rotate_bytes = rotate_bytes
        self._day = None
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._ensure_header()

    def _ensure_header(self):
        with _lock:
            if not os.path.isfile(self.path) or os.path.getsize(self.path) == 0:
                with open(self.path, "w", newline="") as fh:
                    csv.writer(fh).writerow(self.header)

    def _maybe_rotate(self):
        if not self.rotate_daily:
            return
        today = datetime.now().strftime("%Y%m%d")
        if self._day is None:
            self._day = today
        elif self._day != today:
            self._day = today
            self._rotate()

    def _rotate(self):
        """Move current file to a gzipped timestamped archive."""
        with _lock:
            if not os.path.isfile(self.path):
                return
            ts = time.strftime("%Y%m%d_%H%M%S")
            dest = f"{self.path}.{ts}.gz"
            try:
                with open(self.path, "rb") as src, gzip.open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                with open(self.path, "w", newline="") as fh:
                    csv.writer(fh).writerow(self.header)
            except OSError:
                pass

    def append(self, row: dict | list):
        self._maybe_rotate()
        if isinstance(row, dict):
            row = [row.get(h) for h in self.header]
        with _lock:
            try:
                with open(self.path, "a", newline="") as fh:
                    csv.writer(fh).writerow(row)
            except OSError as e:
                print(r(f"[logger] write failed {self.path}: {e}"))


# ------------------------------------------------------------- JSONL writer
class JsonlWriter:
    """Append-only JSON Lines writer, optionally byte-rotated + gzipped."""

    def __init__(self, path, rotate_bytes=DAILY_LOG_MAX_BYTES):
        self.path = path
        self.rotate_bytes = rotate_bytes
        self._size = 0
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)

    def append(self, record: dict):
        line = json.dumps(record, default=str) + "\n"
        with _lock:
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line)
                self._size += len(line)
                if self._size >= self.rotate_bytes:
                    self._rotate()
                    self._size = 0
            except OSError as e:
                print(r(f"[logger] audit write failed: {e}"))

    def _rotate(self):
        if not os.path.isfile(self.path):
            return
        ts = time.strftime("%Y%m%d_%H%M%S")
        dest = f"{self.path}.{ts}.gz"
        try:
            with open(self.path, "rb") as src, gzip.open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.remove(self.path)
        except OSError:
            pass


# ----------------------------------------------------------------- registry
class Loggers:
    """Bundles the three output sinks + console."""
    def __init__(self, log_dir=LOG_DIR):
        self.dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self.trade_csv = CsvWriter(
            os.path.join(log_dir, TRADE_ANALYSIS_CSV),
            header=[
                "trade_id", "strategy_name", "regime_at_entry", "regime_at_exit",
                "entry_timestamp", "exit_timestamp", "holding_days",
                "entry_spot", "exit_spot", "entry_vix", "exit_vix",
                "legs_summary", "total_credit_received", "total_debit_paid",
                "net_premium", "max_risk", "realized_pnl", "realized_pnl_percent",
                "exit_reason", "slippage_total_points", "transaction_costs",
                "composite_score_at_entry", "vol_score", "edge_score",
                "trend_score", "flow_score", "days_to_expiry_at_entry",
                "expiry_date", "paper_trade",
            ])
        self.regime_csv = CsvWriter(
            os.path.join(log_dir, REGIME_LOG_CSV),
            header=["timestamp_ist", "composite", "regime", "vol_raw", "vol_conf",
                    "edge_raw", "edge_conf", "trend_raw", "trend_conf",
                    "flow_raw", "flow_conf", "spot", "vix", "override"])
        self.audit_writer = JsonlWriter(os.path.join(log_dir, AUDIT_LOG_CSV))

    def audit(self, event: str, **fields):
        rec = {"ts": datetime.now().isoformat(), "event": event}
        rec.update(fields)
        self.audit_writer.append(rec)

    def close(self):
        pass
