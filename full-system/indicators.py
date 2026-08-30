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
            from main import IST
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
