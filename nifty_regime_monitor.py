#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 NIFTY MARKET REGIME MONITOR  —  Upstox API v2/v3
================================================================================
 Implements the 8-step vol-regime algorithm:

   Step 0  Data windowing & quality filter (OI >= 50 lots, bid > 0)
   Step 1  Volatility surface  : Term spread (V_fwd - V_spot) + 25d-Put/Call
                                 skew z-score            -> Vol_Score
   Step 2  Realised vs implied : RV(20d) vs ATM IV        -> Edge_Score
   Step 3  Trend & momentum    : ADX(14) on 5-min bars +
                                 EMA-50 slope             -> Trend_Score
   Step 4  Order flow          : Net delta-weighted OI change (15 min) +
                                 3rd-OTM-put spread ratio vs 1h avg -> Flow_Score
   Step 5  Persistence filter  : 3 consecutive identical readings to confirm
   Step 6  Macro override      : high-impact event -> EVENT_HEDGE (6h before /
                                 2h after, from events.json)
   Step 7  Weighted aggregation: 0.30*Vol + 0.30*Edge + 0.25*Trend + 0.15*Flow
   Step 8  Regime mapping      : STRONG_SELL_VOL ... STRONG_BUY_VOL

 Runs continuously (default: every 5 minutes) until Ctrl+C.

 USAGE
 -----
   pip install requests
   python3 nifty_regime_monitor.py                      # uses ./env.txt or env vars
   python3 nifty_regime_monitor.py --env uploads/env.txt --interval 300
   python3 nifty_regime_monitor.py --once               # single cycle
   python3 nifty_regime_monitor.py --demo               # offline synthetic feed

 FILES (created next to the script / cwd)
 ----------------------------------------
   env.txt                 UPSTOX_API_KEY / UPSTOX_API_SECRET / UPSTOX_ACCESS_TOKEN
   events.json             economic calendar (auto-created template; edit me!)
   regime_state.json       skew history, flow snapshots, persistence buffers
   regime_log.csv          one row per cycle
   instruments_cache*.json daily cache of the NSE FO instrument master (NIFTY slice)
================================================================================
"""

import argparse
import base64
import csv
import gzip
import json
import math
import os
import random
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    print("ERROR: the 'requests' package is required.  Install it with:  pip install requests")
    sys.exit(1)

# ------------------------------------------------------------------------------
# 0. CONSTANTS / CONFIG
# ------------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30), name="IST")

API_V2 = "https://api.upstox.com/v2"
API_V3 = "https://api.upstox.com/v3"
INSTRUMENT_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"

KEY_NIFTY = "NSE_INDEX|Nifty 50"
KEY_VIX = "NSE_INDEX|India VIX"

WEIGHTS = {"vol": 0.30, "edge": 0.30, "trend": 0.25, "flow": 0.15}

TERM_THRESHOLD = 0.5          # V_fwd - V_spot      (contango / backwardation)
SKEW_Z_STEEP = 1.5            # z >  1.5  -> fear    (Skew_Score = -1)
SKEW_Z_FLAT = -1.0            # z < -1.0  -> complacent (Skew_Score = +1)
EDGE_RICH = 5.0               # IV - RV > 5  -> rich (Edge_Score = +1)
EDGE_CHEAP = 0.0              # IV - RV < 0  -> cheap (Edge_Score = -1)
ADX_TREND = 25.0              # ADX > 25 + slope -> trending
EMA_SLOPE_PCT = 0.05          # |EMA50 now - EMA50 20 bars ago| > 0.05% of spot
RV_WINDOW = 20                # trading days
RV_ANNUALISE = 252
SKEW_HISTORY_DAYS = 30        # z-score lookback
SKEW_MIN_DAYS = 10            # minimum history before z is trusted
TREND_BARS_REQUIRED = 75      # EMA50 + 20-bar slope + ADX warmup
SPREAD_AVG_MIN = 60           # minutes for spread-ratio average
EVENT_PRE_HOURS = 6           # macro override window: 6h before ...
EVENT_POST_HOURS = 2          # ... to 2h after a high-impact event

REGIME_ACTION = {
    "STRONG_SELL_VOL": "Deploy max size on Short Straddles / Iron Condors.",
    "MILD_SELL_VOL": "Deploy moderate size; prefer credit spreads over naked straddles.",
    "NEUTRAL / MIXED": "Hold current positions; do not initiate new entries.",
    "BUY_VOL / DEFENSIVE": "Reduce short size by 60%; consider long Put hedges.",
    "STRONG_BUY_VOL": "Flatten all short positions; deploy Long Straddles / Strangles.",
    "EVENT_HEDGE": "MACRO OVERRIDE: flatten shorts, switch to long-gamma or sit flat.",
}


def map_regime(x: float):
    if x > 0.45:
        return "STRONG_SELL_VOL"
    if x >= 0.15:
        return "MILD_SELL_VOL"
    if x > -0.15:
        return "NEUTRAL / MIXED"
    if x >= -0.45:
        return "BUY_VOL / DEFENSIVE"
    return "STRONG_BUY_VOL"


def now_ist() -> datetime:
    return datetime.now(IST)


# ------------------------- tiny ANSI colour helpers ---------------------------
_USE_COLOR = True


def _c(code: str, s) -> str:
    return f"\x1b[{code}m{s}\x1b[0m" if _USE_COLOR else str(s)


def g(s):   return _c("32", s)          # green
def r(s):   return _c("31", s)          # red
def y(s):   return _c("33", s)          # yellow
def cy(s):  return _c("36", s)          # cyan
def mg(s):  return _c("35", s)          # magenta
def bo(s):  return _c("1", s)           # bold
def dim(s): return _c("2", s)           # dim


def score_str(v, width=2):
    if v is None:
        return dim("n/a".rjust(width))
    s = f"{v:+g}".rjust(width)
    return g(s) if v > 0 else (r(s) if v < 0 else y(s))


def fx(v, nd=2, sep=True):
    if v is None:
        return "n/a"
    return f"{v:{',' if sep else ''}.{nd}f}"


# ------------------------------------------------------------------------------
# 1. CREDENTIALS
# ------------------------------------------------------------------------------
def load_env_file(path):
    """Parse KEY = "value" style files (env.txt / .env)."""
    out = {}
    if not os.path.isfile(path):
        return out
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
            if not m:
                continue
            key, val = m.group(1), m.group(2).strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            out[key] = val
    return out


def resolve_creds(args):
    env = {}
    sources = []
    for cand in ([args.env] if args.env else ["./env.txt", "./uploads/env.txt", "./.env", os.path.expanduser("~/.upstox_env.txt")]):
        d = load_env_file(cand)
        if d:
            env.update(d)
            sources.append(cand)
    for k in ("UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_ACCESS_TOKEN"):
        if os.environ.get(k):
            env.setdefault(k, os.environ[k])
    token = env.get("UPSTOX_ACCESS_TOKEN", "")
    if not token:
        print(r("ERROR: no UPSTOX_ACCESS_TOKEN found."))
        print("       Put it in env.txt  or  export UPSTOX_ACCESS_TOKEN=...")
        sys.exit(2)
    return token, (sources[0] if sources else "environment variables")


def token_expiry(token):
    """Decode (not verify) the JWT to read the 'exp' claim."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        d = json.loads(base64.urlsafe_b64decode(payload))
        return datetime.fromtimestamp(int(d["exp"]), IST)
    except Exception:
        return None


# ------------------------------------------------------------------------------
# 2. UPSTOX REST CLIENT
# ------------------------------------------------------------------------------
class AuthError(Exception):
    pass


class ApiError(Exception):
    pass


class Upstox:
    def __init__(self, token, timeout=15):
        self.h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self.timeout = timeout

    def _get(self, url, params=None, retries=3):
        for attempt in range(1, retries + 1):
            try:
                resp = requests.get(url, params=params, headers=self.h, timeout=self.timeout)
            except requests.RequestException as e:
                if attempt == retries:
                    raise ApiError(f"network error: {e}")
                time.sleep(1.5 * attempt)
                continue
            if resp.status_code in (401, 403):
                raise AuthError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code == 429:
                time.sleep(2.0 * attempt)
                continue
            if resp.status_code >= 500:
                if attempt == retries:
                    raise ApiError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                time.sleep(1.5 * attempt)
                continue
            if resp.status_code != 200:
                raise ApiError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        raise ApiError("unreachable")

    # ---- market data -------------------------------------------------------
    def quotes(self, keys):
        data = self._get(f"{API_V2}/market-quote/quotes", {"instrument_key": ",".join(keys)})
        return data.get("data", {})

    def option_chain(self, underlying_key, expiry_iso):
        data = self._get(f"{API_V2}/option/chain",
                         {"instrument_key": underlying_key, "expiry_date": expiry_iso})
        return normalize_chain(data.get("data", []))

    def daily_candles(self, key, days=100):
        to = now_ist().date()
        frm = to - timedelta(days=days)
        data = self._get(f"{API_V2}/historical-candle/{key.replace('|', '%7C', 1) if False else key}/day/{to}/{frm}")
        return parse_candles(data)

    def hist_5m(self, key, days=8):
        to = now_ist().date()
        frm = to - timedelta(days=days)
        data = self._get(f"{API_V3}/historical-candle/{key}/minutes/5/{to}/{frm}")
        return parse_candles(data)

    def intraday_5m(self, key):
        try:
            data = self._get(f"{API_V3}/historical-candle/intraday/{key}/minutes/5")
            return parse_candles(data)
        except ApiError:
            return []

    def instruments(self, cache_dir="."):
        return load_instruments(cache_dir)


def fnum(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def parse_candles(payload):
    """[[ts,o,h,l,c,v,oi]...] -> [{'dt':datetime,'o','h','l','c'}...] ascending."""
    rows = (payload or {}).get("data", {}).get("candles", []) or []
    out = []
    for row in rows:
        try:
            ts = row[0].replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts)
            out.append({"dt": dt.astimezone(IST), "o": float(row[1]), "h": float(row[2]),
                        "l": float(row[3]), "c": float(row[4])})
        except Exception:
            continue
    out.sort(key=lambda b: b["dt"])
    return out


def normalize_chain(raw):
    """Raw option-chain rows -> sorted list of strike dicts with call/put legs."""
    out = []
    for s in raw or []:
        c = s.get("call_options") or {}
        p = s.get("put_options") or {}
        cm, pm = c.get("market_data") or {}, p.get("market_data") or {}
        cg, pg = c.get("option_greeks") or {}, p.get("option_greeks") or {}

        def leg(md, gr):
            return {"bid": fnum(md.get("bid_price")), "ask": fnum(md.get("ask_price")),
                    "iv": fnum(gr.get("iv")), "oi": fnum(md.get("oi")),
                    "prev_oi": fnum(md.get("prev_oi")), "delta": fnum(gr.get("delta")),
                    "ltp": fnum(md.get("ltp"))}

        out.append({"strike": fnum(s.get("strike_price")),
                    "spot": fnum(s.get("underlying_spot_price")),
                    "c": leg(cm, cg), "p": leg(pm, pg)})
    out = [s for s in out if s["strike"] is not None]
    out.sort(key=lambda s: s["strike"])
    return out


# ------------------------------------------------------------------------------
# 3. INSTRUMENT MASTER (expiries + futures) with daily on-disk cache
# ------------------------------------------------------------------------------
def load_instruments(cache_dir=".", force=False):
    """Returns {'futs':[{key,symbol,expiry}], 'expiries':[{date,weekly}], 'source':str}"""
    today = now_ist().date().isoformat()
    cache = os.path.join(cache_dir, f"instruments_cache_{today}.json")
    if not force and os.path.isfile(cache):
        try:
            with open(cache) as fh:
                d = json.load(fh)
            if d.get("date") == today and d.get("expiries"):
                d["source"] = f"cache {os.path.basename(cache)}"
                return d
        except Exception:
            pass
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(INSTRUMENT_MASTER_URL, headers=headers, timeout=90)
    resp.raise_for_status()
    rows = json.loads(gzip.decompress(resp.content).decode("utf-8"))
    nifty = [x for x in rows if x.get("segment") == "NSE_FO" and x.get("name") == "NIFTY"]
    futs = sorted(({"key": f["instrument_key"], "symbol": f.get("trading_symbol", "NIFTY FUT"),
                    "expiry": ms_to_date(f["expiry"])} for f in nifty if f.get("instrument_type") == "FUT"),
                  key=lambda f: f["expiry"])
    seen = {}
    for o in nifty:
        if o.get("instrument_type") not in ("CE", "PE"):
            continue
        d = ms_to_date(o["expiry"])
        seen[d] = seen.get(d, False) or bool(o.get("weekly"))
    expiries = [{"date": d, "weekly": seen[d]} for d in sorted(seen)]
    data = {"date": today, "futs": futs, "expiries": expiries, "source": "instrument master"}
    try:
        with open(cache, "w") as fh:
            json.dump(data, fh)
    except OSError:
        pass
    return data


def ms_to_date(ms):
    return datetime.fromtimestamp(int(ms) / 1000, IST).date().isoformat()


def pick_expiries(master, today=None):
    """-> (nearest_expiry, monthly_for_fwd or None). Monthly = weekly==False."""
    today = (today or now_ist().date()).isoformat()
    upcoming = [e for e in master["expiries"] if e["date"] >= today]
    if not upcoming:
        return None, None
    nearest = upcoming[0]["date"]
    monthlies = [e["date"] for e in upcoming if not e["weekly"]]
    fwd_monthly = next((d for d in monthlies if d > nearest), None)
    return nearest, fwd_monthly


# ------------------------------------------------------------------------------
# 4. MATH / INDICATORS
# ------------------------------------------------------------------------------
def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(spot, strike, T, iv_pct, r, is_call):
    if T <= 0 or iv_pct <= 0 or spot <= 0 or strike <= 0:
        if is_call:
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0
    sq = math.sqrt(T)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv_pct ** 2 / 1e4) * T) / (iv_pct / 100.0 * sq)
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
    """Wilder ADX. bars: ascending [{'h','l','c'}]. -> (adx, +di, -di) or None."""
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
    dx = [abs(a - b) / (a + b) * 100 if (a + b) > 0 else 0.0
          for a, b in zip(pdi, ndi)]
    adx_line = _wilder(dx, n)
    if not adx_line:
        return None
    return adx_line[-1], pdi[-1], ndi[-1]


def realised_vol_pct(closes, window=RV_WINDOW):
    closes = closes[-(window + 1):]
    if len(closes) < window + 1:
        return None, len(closes) - 1
    rets = [math.log(closes[i + 1] / closes[i]) for i in range(len(closes) - 1)]
    return statistics.stdev(rets) * math.sqrt(RV_ANNUALISE) * 100.0, len(rets)


# ------------------------------------------------------------------------------
# 5. PERSISTENT STATE (skew history, flow snapshots, persistence buffers)
# ------------------------------------------------------------------------------
class StateStore:
    def __init__(self, path):
        self.path = path
        self.d = {"skew_history": [], "snapshots": [], "buffers": {}, "confirmed": {},
                  "last_cycle": None}
        try:
            with open(path) as fh:
                self.d.update(json.load(fh))
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            with open(self.path, "w") as fh:
                json.dump(self.d, fh)
        except OSError as e:
            print(r(f"  [state] could not save {self.path}: {e}"))

    # -- skew history (one value per calendar day) ----------------------------
    def record_skew(self, skew, today_iso):
        h = [e for e in self.d["skew_history"] if e.get("date") != today_iso]
        h.append({"date": today_iso, "skew": round(skew, 4)})
        h.sort(key=lambda e: e["date"])
        self.d["skew_history"] = h[-60:]

    def skew_zscore(self, today_iso):
        hist = [e["skew"] for e in self.d["skew_history"]
                if e.get("date") != today_iso][-SKEW_HISTORY_DAYS:]
        if len(hist) < SKEW_MIN_DAYS:
            return None, len(hist)
        sd = statistics.stdev(hist)
        if sd < 1e-9:
            return None, len(hist)
        z = (self.d["skew_history"][-1]["skew"] if self.d["skew_history"][-1]["date"] == today_iso else 0.0, )
        # z of *today's* value vs distribution of previous days:
        cur = next((e["skew"] for e in reversed(self.d["skew_history"]) if e["date"] == today_iso), None)
        if cur is None:
            return None, len(hist)
        return (cur - statistics.mean(hist)) / sd, len(hist)

    # -- flow snapshots -------------------------------------------------------
    def add_snapshot(self, snap):
        self.d["snapshots"].append(snap)
        cutoff = (now_ist() - timedelta(minutes=75)).isoformat()
        self.d["snapshots"] = [s for s in self.d["snapshots"] if s.get("ts", "") >= cutoff]

    def snapshot_near(self, now, min_age_s, target_age_s, max_age_s):
        """Snapshot closest to target age inside [min,max]."""
        best, best_d = None, None
        for s in reversed(self.d["snapshots"]):
            try:
                age = (now - datetime.fromisoformat(s["ts"])).total_seconds()
            except (KeyError, ValueError):
                continue
            if min_age_s <= age <= max_age_s:
                d = abs(age - target_age_s)
                if best_d is None or d < best_d:
                    best, best_d = s, d
        return best

    def stale_gap(self, now, limit_s):
        last = self.d.get("last_cycle")
        if not last:
            return False
        try:
            return (now - datetime.fromisoformat(last)).total_seconds() > limit_s
        except ValueError:
            return False


# ------------------------------------------------------------------------------
# 6. THE FOUR SCORING MODULES
# ------------------------------------------------------------------------------
def quality(leg, min_oi):
    return (leg["oi"] is not None and leg["oi"] >= min_oi
            and leg["bid"] is not None and leg["bid"] > 0
            and leg["ask"] is not None and leg["ask"] > 0)


def leg_delta(leg, spot, strike, T, rate, is_call):
    """Prefer API greeks; fall back to Black-Scholes."""
    d = leg["delta"]
    if d is not None and 0.01 < abs(d) < 0.99:
        return d
    if leg["iv"]:
        return bs_delta(spot, strike, T, leg["iv"], rate, is_call)
    return None


def atm_strike(chain, spot):
    if not chain:
        return None
    return min(chain, key=lambda s: abs(s["strike"] - spot))


def module_vol(chain_near, chain_monthly, vix_spot, state, now, cfg):
    """Step 1 — volatility surface. Returns (raw, avail, detail, notes)."""
    notes = []
    if not chain_near:
        return None, False, "no option chain", ["chain unavailable"]
    spots = [s["spot"] for s in chain_near if s["spot"]]
    spot = spots[0] if spots else None
    if not spot:
        return None, False, "no spot in chain", ["chain missing underlying_spot_price"]

    # 1.3 term spread ---------------------------------------------------------
    v_fwd = None
    if chain_monthly:
        atmm = atm_strike(chain_monthly, spot)
        ivs = [atmm["c"]["iv"], atmm["p"]["iv"]] if atmm else []
        ivs = [v for v in ivs if v and v > 0]
        if ivs:
            v_fwd = statistics.mean(ivs)
    if v_fwd is None:
        notes.append("forward VIX unavailable -> Term_Score neutral")
        t_spread, term_score = None, 0
    else:
        t_spread = v_fwd - vix_spot
        term_score = 1 if t_spread > TERM_THRESHOLD else (-1 if t_spread < -TERM_THRESHOLD else 0)
    term_txt = (f"T_spread {t_spread:+.2f} " + {1: "CONTANGO", -1: "BACKWARDATION", 0: "FLAT"}[term_score]) \
        if t_spread is not None else "T_spread n/a"

    # 1.4 25-delta skew -------------------------------------------------------
    T = years_to_expiry(expiry_dt(cfg["near_expiry"]), now)
    c25 = p25 = None
    best_c = best_p = None
    for s in chain_near:
        if quality(s["c"], cfg["min_oi"]) and s["c"]["iv"]:
            d = leg_delta(s["c"], spot, s["strike"], T, cfg["risk_free"], True)
            if d is not None and (best_c is None or abs(d - 0.25) < abs(best_c[0] - 0.25)):
                best_c = (d, s)
        if quality(s["p"], cfg["min_oi"]) and s["p"]["iv"]:
            d = leg_delta(s["p"], spot, s["strike"], T, cfg["risk_free"], False)
            if d is not None and (best_p is None or abs(d + 0.25) < abs(best_p[0] + 0.25)):
                best_p = (d, s)
    if best_c and best_p and abs(best_c[0] - 0.25) < 0.15 and abs(best_p[0] + 0.25) < 0.15:
        skew = best_p[1]["p"]["iv"] - best_c[1]["c"]["iv"]
        state.record_skew(skew, now.date().isoformat())
        z, ndays = state.skew_zscore(now.date().isoformat())
        if z is None:
            notes.append(f"skew z warming up ({ndays}/{SKEW_MIN_DAYS} days history)")
            skew_score = 0
        else:
            skew_score = -1 if z > SKEW_Z_STEEP else (1 if z < SKEW_Z_FLAT else 0)
        skew_txt = f"skew25 {skew:+.2f} (z {z:+.2f}, {ndays}d)" if z is not None else f"skew25 {skew:+.2f} (z warming)"
    else:
        skew, z, skew_score = None, None, 0
        skew_txt = "skew25 n/a"
        notes.append("25-delta legs not found (illiquid chain?)")

    vol_score = 0.5 * term_score + 0.5 * skew_score
    detail = f"{term_txt} | {skew_txt}"
    return vol_score, True, detail, notes


def module_edge(chain_near, rv, spot):
    """Step 2 — realised vs implied. Returns (raw, avail, detail, notes)."""
    notes = []
    if rv is None:
        return None, False, "RV unavailable", ["insufficient daily history"]
    if not chain_near:
        return None, False, "no chain", ["chain unavailable"]
    atm = atm_strike(chain_near, spot)
    ivs = [v for v in (atm["c"]["iv"], atm["p"]["iv"]) if v and v > 0] if atm else []
    if not ivs:
        return None, False, "ATM IV unavailable", ["ATM IV missing/degenerate"]
    iv_atm = statistics.mean(ivs)
    edge = iv_atm - rv
    raw = 1 if edge > EDGE_RICH else (-1 if edge < EDGE_CHEAP else 0)
    tag = "RICH (seller's edge)" if raw == 1 else ("CHEAP (buyer's edge)" if raw == -1 else "FAIR")
    detail = f"IV_atm {iv_atm:.2f}% - RV{RV_WINDOW} {rv:.2f}% = {edge:+.2f} -> {tag}"
    return raw, True, detail, notes


def module_trend(bars5, spot):
    """Step 3 — ADX + EMA slope. Returns (raw, avail, detail, notes)."""
    notes = []
    if len(bars5) < TREND_BARS_REQUIRED:
        return None, False, f"only {len(bars5)} 5-min bars", [f"need >= {TREND_BARS_REQUIRED} bars"]
    closes = [b["c"] for b in bars5]
    ax = adx14([{"h": b["h"], "l": b["l"], "c": b["c"]} for b in bars5])
    ema = ema_series(closes, 50)
    if ax is None or len(ema) < 21:
        return None, False, "indicator warmup", ["not enough bars"]
    adx_v, pdi, ndi = ax
    slope = ema[-1] - ema[-21]
    slope_pct = slope / spot * 100.0 if spot else 0.0
    above = spot > ema[-1]
    if adx_v > ADX_TREND and abs(slope_pct) > EMA_SLOPE_PCT:
        raw = 1 if above else -1
        dirn = "bullish" if above else "bearish"
    else:
        raw = 0
        dirn = "range-bound"
    detail = (f"ADX {adx_v:.1f} (+DI {pdi:.0f}/-DI {ndi:.0f}) | "
              f"EMA50 slope {slope_pct:+.3f}% | spot {'>' if above else '<'} EMA50 -> {dirn}")
    return raw, True, detail, notes


def module_flow(chain_near, state, now, cfg):
    """Step 4 — order flow & microstructure. Returns (raw, avail, detail, notes)."""
    notes = []
    if not chain_near:
        return None, False, "no chain", ["chain unavailable"]
    spot = next((s["spot"] for s in chain_near if s["spot"]), None)
    T = years_to_expiry(expiry_dt(cfg["near_expiry"]), now)

    # -- record this cycle's snapshot ----------------------------------------
    strikes = {}
    for s in chain_near:
        coi = s["c"]["oi"] if (s["c"]["oi"] is not None and s["c"]["oi"] >= cfg["min_oi"]) else None
        poi = s["p"]["oi"] if (s["p"]["oi"] is not None and s["p"]["oi"] >= cfg["min_oi"]) else None
        if coi is None and poi is None:
            continue
        cd = leg_delta(s["c"], spot, s["strike"], T, cfg["risk_free"], True) if coi is not None else None
        pd = leg_delta(s["p"], spot, s["strike"], T, cfg["risk_free"], False) if poi is not None else None
        strikes[f"{s['strike']:.0f}"] = [coi, poi, cd, pd]

    # 3rd OTM put spread ratio
    otm_puts = [s for s in chain_near if spot is not None and s["strike"] < spot
                and quality(s["p"], cfg["min_oi"])]
    spr = None
    spr_strike = None
    if len(otm_puts) >= 3:
        third = sorted(otm_puts, key=lambda s: s["strike"], reverse=True)[2]
        bid, ask = third["p"]["bid"], third["p"]["ask"]
        mid = (bid + ask) / 2.0
        if mid and mid > 0:
            spr = (ask - bid) / mid
            spr_strike = third["strike"]
    state.add_snapshot({"ts": now.isoformat(), "strikes": strikes, "spr": spr, "spot": spot})

    # -- net delta-weighted OI change vs ~15 min ago --------------------------
    ref = state.snapshot_near(now, cfg["flow_min_age"], cfg["flow_target_age"], cfg["flow_max_age"])
    net_flow = None
    if ref:
        dcall = dput = 0.0
        for k, (coi, poi, cd, pd) in strikes.items():
            old = ref["strikes"].get(k)
            if not old:
                continue
            if coi is not None and old[0] is not None and cd is not None:
                dcall += (coi - old[0]) * cd
            if poi is not None and old[1] is not None and pd is not None:
                dput += (poi - old[1]) * pd
        net_flow = dcall + dput

    # -- spread ratio vs 1-hour average ---------------------------------------
    spr_avg = None
    hist = []
    for s in state.d["snapshots"]:
        if s.get("spr") is None:
            continue
        try:
            age = (now - datetime.fromisoformat(s["ts"])).total_seconds()
        except ValueError:
            continue
        if age <= cfg.get("spread_window_min", SPREAD_AVG_MIN) * 60:
            hist.append((s["ts"], s["spr"]))
    if len(hist) >= 3:
        span_min = (now - datetime.fromisoformat(hist[0][0])).total_seconds() / 60.0
        if span_min >= cfg.get("spread_span_min", 20):
            spr_avg = statistics.mean(v for _, v in hist)

    if net_flow is None or spr_avg is None:
        why = []
        if net_flow is None:
            why.append("net-flow warming up (needs a snapshot 10-30 min old)")
        if spr_avg is None:
            why.append("spread baseline warming up (needs ~20 min history)")
        detail = (f"Net_dOI(15m): {'n/a' if net_flow is None else fx(net_flow, 0)} | "
                  f"3rd-OTM-put({spr_strike if spr_strike else '-'}) SPR "
                  f"{f'{spr:.4f}' if spr is not None else 'n/a'} vs 1h avg "
                  f"{f'{spr_avg:.4f}' if spr_avg is not None else 'n/a'}")
        return None, False, detail, why

    if spr < spr_avg * 0.985:
        spr_state = "CONTRACTING"
    elif spr > spr_avg * 1.015:
        spr_state = "WIDENING"
    else:
        spr_state = "FLAT"
    if net_flow > 0 and spr_state == "CONTRACTING":
        raw = 1
        tag = "aggressive bullish flow"
    elif net_flow < 0 and spr_state == "WIDENING":
        raw = -1
        tag = "defensive / panic flow"
    else:
        raw = 0
        tag = "mixed"
    detail = (f"Net_dOI(15m) {net_flow:+,.0f} | SPR {spr:.4f} vs avg {spr_avg:.4f} -> {spr_state} | {tag}")
    return raw, True, detail, notes


def years_to_expiry(exp_dt, now):
    if exp_dt is None:
        return 1.0 / 365.0
    return max((exp_dt - now).total_seconds(), 1.0) / (365.0 * 24 * 3600)


def expiry_dt(expiry_iso):
    try:
        return datetime.strptime(expiry_iso, "%Y-%m-%d").replace(hour=15, minute=30, tzinfo=IST)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------------
# 7. PERSISTENCE FILTER (Step 5)
# ------------------------------------------------------------------------------
MODULES = ["vol", "edge", "trend", "flow"]


class Persistence:
    def __init__(self, state):
        self.state = state
        self.buf = {m: list(state.d.get("buffers", {}).get(m, []))[-3:] for m in MODULES}
        self.conf = {m: int(state.d.get("confirmed", {}).get(m, 0)) for m in MODULES}
        self.output = {m: (self.conf[m], None, "") for m in MODULES}

    def update(self, name, raw):
        """Returns (confirmed, raw_used, note). raw=None -> hold previous."""
        if raw is None:
            self.output[name] = (self.conf[name], None, "no reading - holding previous")
            return self.output[name]
        b = self.buf[name]
        b.append(raw)
        if len(b) > 3:
            b.pop(0)
        if len(b) == 3 and b[0] == b[1] == b[2]:
            self.conf[name] = raw
            self.output[name] = (raw, raw, "confirmed (3 consecutive)")
            return self.output[name]
        self.output[name] = (self.conf[name], raw, f"unconfirmed - needs {3 - len(b)} more consecutive")
        return self.output[name]

    def persist(self):
        self.state.d["buffers"] = {m: self.buf[m] for m in MODULES}
        self.state.d["confirmed"] = self.conf


# ------------------------------------------------------------------------------
# 8. MACRO OVERRIDE (Step 6)
# ------------------------------------------------------------------------------
EVENT_TEMPLATE = {
    "_comment": [
        "Economic calendar for the Step-6 macro override.",
        "'start' format: 'YYYY-MM-DD HH:MM' in IST (24h). Only impact 'high' events trigger.",
        "Override window: 6 hours BEFORE the event until 2 hours AFTER.",
        "Add real events (RBI policy, US CPI/FOMC, Union Budget, elections results...) here."
    ],
    "events": [
        {"name": "EXAMPLE - US CPI Release", "start": "2026-09-11 18:30", "impact": "high"},
        {"name": "EXAMPLE - RBI MPC Decision", "start": "2026-10-01 10:00", "impact": "high"},
        {"name": "EXAMPLE - Union Budget Speech", "start": "2027-02-01 11:00", "impact": "high"},
    ],
}


def load_events(path):
    if not os.path.isfile(path):
        try:
            with open(path, "w") as fh:
                json.dump(EVENT_TEMPLATE, fh, indent=2)
        except OSError:
            pass
    try:
        with open(path) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return [], "calendar unreadable"
    out = []
    for e in d.get("events", []):
        try:
            dt = datetime.strptime(e["start"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
            out.append({"name": e.get("name", "?"), "dt": dt,
                        "high": str(e.get("impact", "high")).lower() == "high"})
        except (KeyError, ValueError):
            continue
    return out, None


def macro_status(events, now):
    """-> (override_active, event, text)"""
    for e in events:
        if not e["high"]:
            continue
        if e["dt"] - timedelta(hours=EVENT_PRE_HOURS) <= now <= e["dt"] + timedelta(hours=EVENT_POST_HOURS):
            return True, e, f"OVERRIDE ACTIVE: '{e['name']}' @ {e['dt']:%d-%b %H:%M} IST"
    upcoming = sorted((e for e in events if e["high"] and e["dt"] > now), key=lambda e: e["dt"])
    if upcoming:
        e = upcoming[0]
        return False, e, f"next high-impact: '{e['name']}' {e['dt']:%d-%b %Y %H:%M} IST"
    return False, None, "no high-impact events in calendar"


# ------------------------------------------------------------------------------
# 9. REGIME LOG
# ------------------------------------------------------------------------------
def append_log(path, row):
    new = not os.path.isfile(path)
    try:
        with open(path, "a", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["timestamp_ist", "composite", "regime", "vol_raw", "vol_conf",
                            "edge_raw", "edge_conf", "trend_raw", "trend_conf",
                            "flow_raw", "flow_conf", "spot", "vix", "override"])
            w.writerow(row)
    except OSError as e:
        print(r(f"  [log] write failed: {e}"))


# ------------------------------------------------------------------------------
# 10. REPORT RENDERER
# ------------------------------------------------------------------------------
W = 84


def rule(ch="-"):
    return dim(ch * W)


def header_line(txt):
    return cy(" " + txt) + " " + dim("─" * max(0, W - 2 - len(txt)))


def build_report(cyc, now, mk, snap, mods, pers, composite, regime, macro, notes, cfg, interval):
    L = []
    L.append(bo(cy("╔" + "═" * (W - 2) + "╗")))
    title = "NIFTY MARKET REGIME MONITOR  ·  Upstox  ·  " + now.strftime("%a %d-%b-%Y %H:%M:%S IST")
    L.append(bo(cy("║" + title.center(W - 2) + "║")))
    L.append(bo(cy("╚" + "═" * (W - 2) + "╝")))

    phase, phase_c = mk["phase"]
    phase_txt = f"Market: {phase}" + (f" (closes {mk.get('close_at','')})" if phase == "OPEN" else "")
    nxt = (now + timedelta(seconds=interval)).strftime("%H:%M:%S")
    L.append(f"  Cycle #{cyc}  ·  {phase_c(phase_txt)}  ·  next check {nxt}  ·  Ctrl+C to stop")
    L.append(rule())

    L.append(header_line("MARKET SNAPSHOT"))
    L.append(f"   NIFTY 50   {bo(fx(snap['spot']))}  ({snap['chg']})   as of {snap['qts']}")
    L.append(f"   Near FUT   {fx(snap['fut'])}  (basis {snap['basis']})      expiry {snap['fut_exp']}")
    L.append(f"   India VIX  {fx(snap['vix'])}  ({snap['vix_chg']})   "
             f"V_fwd {fx(snap['v_fwd'])} (monthly {snap['monthly_exp'] or 'n/a'} ATM straddle)")
    L.append(f"   RV{RV_WINDOW} {fx(snap['rv'])}%  ·  IV_atm {fx(snap['iv_atm'])}%  ·  "
             f"PCR {fx(snap['pcr'])}  ·  strikes used {snap['n_strikes']}/{snap['n_total']}")
    if snap["oi_levels"]:
        L.append("   " + snap["oi_levels"])
    L.append(rule())

    L.append(header_line("MODULE SCORES  (raw -> confirmed)"))
    labels = {"vol": "[1] VOL SURFACE ", "edge": "[2] VOL EDGE    ",
              "trend": "[3] TREND       ", "flow": "[4] ORDER FLOW  "}
    for m in MODULES:
        conf, raw, note = pers.output[m]
        conf_s = score_str(conf)
        if raw is None:
            raw_s = dim(" n/a")
        else:
            raw_s = score_str(raw)
        L.append(f"   {labels[m]} {mods[m]['detail']}")
        L.append(f"   {' ' * len(labels[m])} score: {raw_s} -> {conf_s}   {dim(note)}")
    L.append(rule())

    L.append(header_line("AGGREGATION (Step 5-7)"))
    terms = " + ".join(f"{w}·({pers.conf[m]:+g})" for m, w in WEIGHTS.items())
    L.append(f"   Composite = {terms} = {bo(f'{composite:+.3f}')}")
    if macro["override"]:
        L.append("   Macro: " + r(bo(macro["text"])))
    else:
        L.append("   Macro override: OFF  (" + macro["text"] + ")")
    L.append(rule())

    if regime == "EVENT_HEDGE":
        reg_s = bo(mg("EVENT_HEDGE"))
    else:
        col = g if composite > 0.15 else (r if composite < -0.15 else y)
        reg_s = bo(col(regime))
    L.append("  " + bo("★ REGIME: ") + reg_s)
    L.append("    Action : " + REGIME_ACTION[regime])
    if notes:
        L.append(rule())
        L.append(dim("   notes: " + " | ".join(notes)))
    return "\n".join(L)


def market_phase(now):
    if now.weekday() >= 5:
        return "CLOSED (weekend)", y
    t = now.hour * 60 + now.minute
    if 9 * 60 + 15 <= t <= 15 * 60 + 30:
        return "OPEN", g
    if t < 9 * 60 + 15:
        return "PRE-OPEN", y
    return "CLOSED (after hours)", y


# ------------------------------------------------------------------------------
# 11. DEMO PROVIDER (offline synthetic feed, same interface as Upstox)
# ------------------------------------------------------------------------------
class DemoProvider:
    def __init__(self):
        self.cycle = 0
        self.rng = random.Random(11)
        self._oi_state = {}
        self._cum = 0.0
        self._monthly = None

    def tick(self):
        """Advance the synthetic scenario once per monitor cycle."""
        self.cycle += 1

    def scenario(self):
        phases = [
            dict(name="calm uptrend", vix=11.2, vfwd=12.3, iv=17.5, rv=8.5, adx=31,
                 slope=0.09, dir=1, flow=1, spr_rel=[0.85, 0.90, 0.95], skew=2.2),
            dict(name="range chop", vix=12.0, vfwd=12.15, iv=12.4, rv=11.8, adx=17,
                 slope=0.01, dir=0, flow=0, spr_rel=[1.0, 1.0, 1.0], skew=1.4),
            dict(name="panic selloff", vix=17.5, vfwd=15.2, iv=18.6, rv=21.0, adx=29,
                 slope=-0.12, dir=-1, flow=-1, spr_rel=[1.20, 1.12, 1.05], skew=6.5),
            dict(name="recovery", vix=13.0, vfwd=13.9, iv=14.2, rv=12.0, adx=27,
                 slope=0.07, dir=1, flow=1, spr_rel=[0.88, 0.92, 0.97], skew=2.8),
        ]
        return phases[(self.cycle // 3) % len(phases)], self.cycle % 3

    def _spot(self, sc):
        return 24175.65 + sc["dir"] * (self.cycle * 35) + self.rng.uniform(-15, 15)

    def instruments(self):
        t = now_ist().date()
        wk = [t + timedelta(days=((1 - t.weekday()) % 7) + 7 * i) for i in range(6)]
        mon = wk[-1]
        self._monthly = mon.isoformat()
        return {"futs": [{"key": "NSE_FO|DEMOFUT", "symbol": "NIFTY FUT DEMO",
                          "expiry": mon.isoformat()}],
                "expiries": [{"date": d.isoformat(), "weekly": d != mon} for d in wk],
                "source": "demo"}

    def quotes(self, keys):
        sc, i = self.scenario()
        spot = self._spot(sc)
        prev_close = 24175.65
        out = {}
        if KEY_NIFTY in keys:
            out["NSE_INDEX:Nifty 50"] = {"last_price": spot, "net_change": spot - prev_close,
                                         "ohlc": {"close": prev_close},
                                         "timestamp": now_ist().isoformat()}
        if KEY_VIX in keys:
            out["NSE_INDEX:India VIX"] = {"last_price": sc["vix"], "net_change": -0.2,
                                          "ohlc": {"close": sc["vix"] + 0.2},
                                          "timestamp": now_ist().isoformat()}
        for k in keys:
            if "FUT" in k:
                out["NSE_FO:DEMOFUT"] = {"last_price": spot * 1.0012, "net_change": 5,
                                         "ohlc": {"close": spot}, "timestamp": now_ist().isoformat()}
        return out

    def option_chain(self, underlying, expiry):
        sc, i = self.scenario()
        spot = self._spot(sc)
        is_monthly = (self._monthly is not None and expiry >= self._monthly)
        step = 50.0
        lo = math.floor((spot - 900) / step) * step
        exp_dt = datetime.strptime(expiry, "%Y-%m-%d").replace(hour=15, minute=30, tzinfo=IST)
        T = max((exp_dt - now_ist()).total_seconds(), 1.0) / (365 * 24 * 3600)
        rows = []
        self._cum += 18000.0 * sc["flow"]
        for n in range(37):
            k = lo + n * step
            m = (k - spot) / spot
            base_iv = sc["vfwd"] if is_monthly else sc["iv"]
            ivc = base_iv + 900 * m * m
            ivp = ivc + sc["skew"] * 0.25 + max(0.0, -m) * sc["skew"]
            d_c = bs_delta(spot, k, T, ivc, 0.065, True)
            d_p = bs_delta(spot, k, T, ivp, 0.065, False)
            tv_c = max(0.3, spot * ivc / 100 * math.sqrt(T) * 0.4)
            tv_p = max(0.3, spot * ivp / 100 * math.sqrt(T) * 0.4)
            ltp_c = max(0.05, (spot - k) + tv_c if k < spot else tv_c)
            ltp_p = max(0.05, (k - spot) + tv_p if k > spot else tv_p)
            base = max(60.0, 90000 * math.exp(-((k - spot) / 300) ** 2))
            key = f"{k:.0f}"
            prev_c, prev_p = self._oi_state.get(key, (base, base))
            w = math.exp(-((k - spot) / 300) ** 2)
            if d_c is not None and 0.15 < d_c < 0.45:
                prev_c += self._cum * w - (0 if sc["flow"] == 0 else self.rng.uniform(-2000, 2000))
            if d_p is not None and -0.45 < d_p < -0.15:
                prev_p += (-self._cum if sc["flow"] else 0) * w - (0 if sc["flow"] == 0 else self.rng.uniform(-2000, 2000))
            prev_c, prev_p = max(3000.0, prev_c), max(3000.0, prev_p)
            self._oi_state[key] = (prev_c, prev_p)
            spr_put = (0.012 + 0.02 * max(0.0, (spot - k) / spot)) * sc["spr_rel"][i] * 30
            rows.append({
                "expiry": expiry, "strike_price": k, "underlying_spot_price": spot,
                "call_options": {"market_data": {"bid_price": round(ltp_c - 0.4, 2), "ask_price": round(ltp_c + 0.4, 2),
                                                 "oi": prev_c, "prev_oi": prev_c - 26000 * sc["flow"], "ltp": round(ltp_c, 2)},
                                 "option_greeks": {"delta": round(d_c, 4), "iv": round(ivc, 2)}},
                "put_options": {"market_data": {"bid_price": round(ltp_p - spr_put, 2), "ask_price": round(ltp_p + spr_put, 2),
                                                "oi": prev_p, "prev_oi": prev_p + 26000 * sc["flow"], "ltp": round(ltp_p, 2)},
                                "option_greeks": {"delta": round(d_p, 4), "iv": round(ivp, 2)}}})
        return normalize_chain(rows)

    def daily_candles(self, key, days=100):
        sc, _ = self.scenario()
        out, c = [], 24300.0
        rng = random.Random(3)
        t = now_ist()
        for i in range(70, 0, -1):
            sig = sc["rv"] / 100 / math.sqrt(252)
            c = c * math.exp(rng.gauss(0.0002, sig if i <= 21 else 0.007))
            d = (t - timedelta(days=i)).replace(hour=0, minute=0, tzinfo=IST)
            out.append({"dt": d, "o": c, "h": c * 1.004, "l": c * 0.996, "c": c})
        return out

    def hist_5m(self, key, days=8):
        sc, _ = self.scenario()
        spot = self._spot(sc)
        rng = random.Random(5)
        closes = [spot]
        drift = sc["slope"] / 100 * spot / 12
        for i in range(219, -1, -1):          # walk backwards, series ends at spot
            if sc["dir"] == 0:
                step = 16 * math.sin(i / 9.0) * 0.5 + rng.gauss(0, 5)
            else:
                step = drift + rng.gauss(0, 4)
            closes.append(closes[-1] - step)
        closes.reverse()
        t0 = now_ist().replace(second=0, microsecond=0) - timedelta(minutes=5 * 220)
        return [{"dt": t0 + timedelta(minutes=5 * i), "o": c - 2, "h": c + 4, "l": c - 4,
                 "c": closes[i]} for i, c in enumerate(closes)]

    def intraday_5m(self, key):
        return []


# ------------------------------------------------------------------------------
# 12. MAIN CYCLE
# ------------------------------------------------------------------------------
def get_chain_safe(client, ukey, expiry):
    try:
        return client.option_chain(ukey, expiry), None
    except (ApiError, AuthError) as e:
        return None, str(e)


def run_cycle(cyc, client, master, state, pers, cfg):
    now = now_ist()
    notes = []
    if hasattr(client, "tick"):
        client.tick()
    ukey = cfg["underlying_key"]

    # ---------- quotes ----------
    qkeys = [KEY_NIFTY, KEY_VIX] + [f["key"] for f in master["futs"][:1]]
    try:
        q = client.quotes(qkeys)
    except AuthError:
        raise
    except ApiError as e:
        q, notes = {}, notes + [f"quotes failed: {e}"]

    def qv(key):
        """Match a quote by its instrument_token (response keys use symbols)."""
        suffix = key.split("|")[-1].lower()
        for k, v in q.items():
            tok = str(v.get("instrument_token", "")).lower()
            if tok.endswith(suffix) or suffix in k.lower():
                return v
        return {}

    spot_q = fnum(qv(KEY_NIFTY).get("last_price"))
    vix = fnum(qv(KEY_VIX).get("last_price")) or 0.0
    vix_chg = fnum(qv(KEY_VIX).get("net_change"))

    # ---------- option chains ----------
    chain_near, err1 = get_chain_safe(client, ukey, cfg["near_expiry"])
    chain_monthly = None
    if cfg["monthly_expiry"] and cfg["monthly_expiry"] != cfg["near_expiry"]:
        chain_monthly, err2 = get_chain_safe(client, ukey, cfg["monthly_expiry"])
    if err1:
        notes.append(f"near chain error: {err1}")
    spot = None
    for ch in (chain_near, chain_monthly):
        if ch:
            s = next((x["spot"] for x in ch if x["spot"]), None)
            if s:
                spot = s
                break
    if spot is None:
        spot = spot_q
    if spot is None:
        print(r("FATAL: cannot determine NIFTY spot (quotes + chain both failed)."))
        raise ApiError("no spot")

    # quality-filtered strike count for display
    n_total = len(chain_near or [])
    n_used = sum(1 for s in (chain_near or [])
                 if quality(s["c"], cfg["min_oi"]) or quality(s["p"], cfg["min_oi"]))

    # ---------- history ----------
    try:
        daily = client.daily_candles(KEY_NIFTY)
    except (ApiError, AuthError) as e:
        daily, _ = [], notes.append(f"daily candles failed: {e}")
    rv, nret = realised_vol_pct([b["c"] for b in daily])
    if rv is None:
        notes.append(f"RV needs {RV_WINDOW + 1} daily closes (have {nret + 1})")

    try:
        bars = client.hist_5m(KEY_NIFTY, days=cfg["hist_days"]) + client.intraday_5m(KEY_NIFTY)
    except (ApiError, AuthError) as e:
        bars, _ = [], notes.append(f"5-min candles failed: {e}")
    seen_dt = set()
    uniq = []
    for b in sorted(bars, key=lambda b: b["dt"]):
        if b["dt"] not in seen_dt:
            uniq.append(b)
            seen_dt.add(b["dt"])
    bars = uniq[-300:]

    # ---------- modules ----------
    mods = {}
    raws = {}
    raws["vol"], avail, det, nt = module_vol(chain_near, chain_monthly, vix, state, now, cfg)
    mods["vol"] = {"detail": det}
    notes += nt
    raws["edge"], avail, det, nt = module_edge(chain_near, rv, spot)
    mods["edge"] = {"detail": det}
    notes += nt
    raws["trend"], avail, det, nt = module_trend(bars, spot)
    mods["trend"] = {"detail": det}
    notes += nt
    raws["flow"], avail, det, nt = module_flow(chain_near, state, now, cfg)
    mods["flow"] = {"detail": det}
    notes += nt

    for m in MODULES:
        pers.update(m, raws[m])
    pers.persist()
    state.d["last_cycle"] = now.isoformat()
    state.save()

    # ---------- aggregate ----------
    composite = sum(WEIGHTS[m] * pers.conf[m] for m in MODULES)
    override, event, macro_txt = macro_status(cfg["events"], now)
    regime = "EVENT_HEDGE" if override else map_regime(composite)

    # ---------- snapshot display ----------
    fut_q = qv(master["futs"][0]["key"]) if master["futs"] else {}
    fut_px = fnum(fut_q.get("last_price"))
    net_chg = fnum(qv(KEY_NIFTY).get("net_change"))
    chg_pct = (net_chg / (spot - net_chg) * 100.0) if (net_chg is not None and spot and net_chg and spot != net_chg) else None
    qts = qv(KEY_NIFTY).get("timestamp", "")
    atm = atm_strike(chain_near, spot) if chain_near else None
    iv_atm = statistics.mean([v for v in ((atm["c"]["iv"] if atm else None),
                                          (atm["p"]["iv"] if atm else None)) if v and v > 0]) if atm else None
    pcr = None
    if chain_near:
        tc = sum(s["c"]["oi"] or 0 for s in chain_near)
        tp = sum(s["p"]["oi"] or 0 for s in chain_near)
        pcr = tp / tc if tc else None
    oi_txt = ""
    if chain_near:
        mx_c = max((s for s in chain_near if s["c"]["oi"]), key=lambda s: s["c"]["oi"], default=None)
        mx_p = max((s for s in chain_near if s["p"]["oi"]), key=lambda s: s["p"]["oi"], default=None)
        if mx_c and mx_p:
            oi_txt = (f"max Call OI {mx_c['strike']:.0f} ({fx(mx_c['c']['oi'],0)}) · "
                      f"max Put OI {mx_p['strike']:.0f} ({fx(mx_p['p']['oi'],0)})")

    snap = {
        "spot": spot, "chg": (f"{net_chg:+.2f} | {chg_pct:+.2f}%" if net_chg is not None and chg_pct is not None
                              else (f"{net_chg:+.2f}" if net_chg is not None else "n/a")),
        "qts": qts[11:16] + " IST" if qts else "n/a",
        "fut": fut_px, "basis": f"{(fut_px - spot):+.2f}" if (fut_px and spot) else "n/a",
        "fut_exp": (master["futs"][0]["expiry"] if master.get("futs") else "n/a"),
        "vix": vix, "vix_chg": f"{vix_chg:+.2f}" if vix_chg is not None else "n/a",
        "v_fwd": (statistics.mean([v for v in ((atm_strike(chain_monthly, spot)["c"]["iv"]),
                                               (atm_strike(chain_monthly, spot)["p"]["iv"])) if v and v > 0])
                  if chain_monthly and atm_strike(chain_monthly, spot) else None),
        "monthly_exp": cfg["monthly_expiry"], "near_exp": cfg["near_expiry"],
        "rv": rv, "iv_atm": iv_atm, "pcr": pcr,
        "n_strikes": n_used, "n_total": n_total, "oi_levels": oi_txt,
    }

    rep = build_report(cyc, now, {"phase": market_phase(now)}, snap, mods, pers,
                       composite, regime, {"override": override, "text": macro_txt},
                       notes, cfg, cfg["interval"])
    print("\n" + rep)
    append_log(cfg["log_file"], [now.isoformat(timespec="seconds"), f"{composite:+.3f}", regime,
                                 raws["vol"], pers.conf["vol"], raws["edge"], pers.conf["edge"],
                                 raws["trend"], pers.conf["trend"], raws["flow"], pers.conf["flow"],
                                 spot, vix, int(override)])
    return regime


# ------------------------------------------------------------------------------
# 13. ENTRYPOINT
# ------------------------------------------------------------------------------
def seed_skew_history(state, path="skew_seed.csv"):
    """Optional: pre-load past 25d-P/C skew values (columns: date,skew) so the
    Step-1 z-score works before the monitor has accumulated its own history."""
    if not os.path.isfile(path):
        return 0
    have = {e["date"] for e in state.d["skew_history"]}
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.lower().startswith("date"):
                continue
            parts = line.split(",")
            try:
                d = datetime.strptime(parts[0].strip(), "%Y-%m-%d").date().isoformat()
                rows.append((d, float(parts[1])))
            except (ValueError, IndexError):
                continue
    added = 0
    for d, v in sorted(rows):
        if d not in have:
            state.d["skew_history"].append({"date": d, "skew": v})
            have.add(d)
            added += 1
    state.d["skew_history"].sort(key=lambda e: e["date"])
    state.d["skew_history"] = state.d["skew_history"][-60:]
    return added


def parse_args():
    p = argparse.ArgumentParser(description="NIFTY market-regime monitor (Upstox API)")
    p.add_argument("--env", help="path to env file with UPSTOX_* keys (default: ./env.txt, env vars)")
    p.add_argument("--interval", type=int, default=300, help="seconds between checks (default 300 = 5 min)")
    p.add_argument("--once", action="store_true", help="run a single cycle and exit")
    p.add_argument("--demo", action="store_true", help="offline synthetic-data demo (no API calls)")
    p.add_argument("--state-file", default="regime_state.json")
    p.add_argument("--events-file", default="events.json")
    p.add_argument("--log-file", default="regime_log.csv")
    p.add_argument("--min-oi", type=float, default=50.0, help="quality filter: min OI in lots (default 50)")
    p.add_argument("--risk-free", type=float, default=6.5, help="risk-free rate %% for BS fallback (default 6.5)")
    p.add_argument("--hist-days", type=int, default=8, help="calendar days of 5-min candles to fetch")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--flow-min-age", type=float, default=600, help="min age (s) of reference OI snapshot")
    p.add_argument("--flow-target-age", type=float, default=900, help="target age (s), default 15 min")
    p.add_argument("--flow-max-age", type=float, default=1800, help="max age (s) of reference OI snapshot")
    return p.parse_args()


def main():
    global _USE_COLOR
    args = parse_args()
    _USE_COLOR = not args.no_color  # colours kept even when piped; disable with --no-color
    if os.name == "nt":
        os.system("")  # enable ANSI on Windows terminals
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print(bo(cy("═" * 84)))
    print(bo(cy(" NIFTY MARKET REGIME MONITOR — starting up")))
    print(bo(cy("═" * 84)))

    # ---- credentials / provider ---------------------------------------------
    if args.demo:
        client, token_exp, cred_src = DemoProvider(), None, "demo mode (no API)"
    else:
        token, cred_src = resolve_creds(args)
        token_exp = token_expiry(token)
        client = Upstox(token)
        if token_exp:
            left = token_exp - now_ist()
            if left.total_seconds() <= 0:
                print(r(f"ERROR: access token EXPIRED at {token_exp:%d-%b-%Y %H:%M} IST."))
                print("       Regenerate the token via the Upstox login flow and update env.txt.")
                sys.exit(2)
            warn = y if left < timedelta(hours=2) else dim
            print(f"  token OK — expires {token_exp:%d-%b-%Y %H:%M} IST ({left})")

    print(f"  credentials : {cred_src}")
    print(f"  interval    : {args.interval}s   state: {args.state_file}   log: {args.log_file}")

    # ---- instruments / expiries ----------------------------------------------
    try:
        master = client.instruments()
    except Exception as e:
        if args.demo:
            raise
        print(y(f"  instrument master failed ({e}) — probing likely expiry dates ..."))
        master = {"futs": [], "expiries": []}
    near_exp, monthly_exp = pick_expiries(master)
    if not args.demo:
        if not near_exp:
            # fallback: probe likely expiry dates (NSE weeklies are Tuesdays)
            t = now_ist().date()
            tues = sorted({t + timedelta(days=((1 - t.weekday()) % 7) + 7 * i) for i in range(10)})
            found = []
            for d in tues:
                try:
                    ch = client.option_chain(KEY_NIFTY, d.isoformat())
                    if ch:
                        found.append(d.isoformat())
                except (ApiError, AuthError):
                    continue
            if found:
                near_exp = found[0]
                last_tue_of_month = [d for i, d in enumerate(found)
                                     if i == len(found) - 1 or found[i + 1][:7] != d[:7]]
                monthly_exp = next((d for d in last_tue_of_month if d > near_exp), None)
                master = {"futs": [], "expiries": [], "source": "expiry probe (degraded)"}
        if not near_exp:
            print(r("ERROR: could not determine any NIFTY option expiry."))
            sys.exit(2)
        print(f"  near expiry : {near_exp}   monthly (for V_fwd): {monthly_exp or 'n/a'}"
              f"   [{master.get('source','')}]")

    events, ev_err = load_events(args.events_file)
    n_high = sum(1 for e in events if e["high"])
    print(f"  calendar    : {len(events)} events ({n_high} high-impact) in {args.events_file}"
          + (y(f"  [{ev_err}]") if ev_err else ""))
    print(dim("  " + "─" * 82))
    print(dim("  algo: Vol(0.30)+Edge(0.30)+Trend(0.25)+Flow(0.15) · persistence 3x · macro override 6h/-2h"))

    cfg = {"near_expiry": near_exp, "monthly_expiry": monthly_exp,
           "min_oi": args.min_oi, "risk_free": args.risk_free / 100.0,
           "hist_days": args.hist_days, "events": events, "log_file": args.log_file,
           "interval": args.interval,
           "flow_min_age": args.flow_min_age, "flow_target_age": args.flow_target_age,
           "flow_max_age": args.flow_max_age,
           "spread_window_min": 60, "spread_span_min": 20,
           "underlying_key": "DEMO" if args.demo else KEY_NIFTY}
    if args.demo:
        # compress the flow/spread warm-up so the whole pipeline is visible in demo
        cfg["flow_min_age"] = max(2 * args.interval, 4.0)
        cfg["flow_target_age"] = max(3 * args.interval, 6.0)
        cfg["flow_max_age"] = max(12 * args.interval, 30.0)
        cfg["spread_window_min"] = max(12 * args.interval / 60.0, 1.0)
        cfg["spread_span_min"] = max(2 * args.interval / 60.0, 0.05)

    state = StateStore(args.state_file)
    seeded = seed_skew_history(state)
    if seeded:
        print(f"  skew seed   : loaded {seeded} day(s) from skew_seed.csv")
    if args.demo and not state.d["skew_history"]:
        # pre-seed 25 days of skew history so z-scores work from cycle 1
        rng = random.Random(9)
        base = now_ist().date()
        state.d["skew_history"] = [{"date": (base - timedelta(days=i)).isoformat(),
                                    "skew": round(rng.gauss(1.6, 0.8), 3)} for i in range(25, 0, -1)]

    # stale-state guard: if the program was idle for > 30 min, reset persistence
    if state.stale_gap(now_ist(), 1800):
        state.d["buffers"], state.d["confirmed"] = {}, {}
        state.d["snapshots"] = []
        print(y("  state was stale (>30 min gap) — persistence buffers reset"))

    pers = Persistence(state)
    regime_prev = None
    changes = []
    cyc = 0
    try:
        while True:
            cyc += 1
            try:
                regime = run_cycle(cyc, client, master, state, pers, cfg)
            except AuthError as e:
                print(r(f"\nAUTH ERROR: {e}"))
                print(r("The Upstox access token expired or was revoked. Generate a new one"))
                print(r("(Upstox app → login → token) and update env.txt, then rerun."))
                return 2
            except ApiError as e:
                print(r(f"cycle aborted: {e} — retrying next interval"))
                regime = regime_prev
            if regime != regime_prev and regime_prev is not None:
                changes.append((now_ist(), regime_prev, regime))
                print(bo(mg(f"\n  ★★★ REGIME CHANGE: {regime_prev}  →  {regime} ★★★")))
            regime_prev = regime
            if args.once:
                break
            # interruptible sleep
            end = time.time() + args.interval
            try:
                while time.time() < end:
                    time.sleep(min(5.0, end - time.time()))
            except KeyboardInterrupt:
                raise
    except KeyboardInterrupt:
        pass

    print("\n" + bo(cy("═" * 84)))
    print(bo(cy(" Monitor stopped by user.")))
    print(f"  cycles run : {cyc}")
    print(f"  last regime: {bo(regime_prev)}")
    print(f"  regime changes this session: {len(changes)}")
    for t0, a, b in changes:
        print(f"    {t0:%d-%b %H:%M:%S}  {a}  →  {b}")
    print(f"  full history in {args.log_file} · skew/flow memory in {args.state_file}")
    print(bo(cy("═" * 84)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
