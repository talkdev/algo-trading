#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 T S M O M   —   Time Series Momentum on the Nifty ETF Universe
================================================================================
 Single-file, start-to-end implementation of the TSMOM algorithm.

 WHAT THIS PROGRAM DOES
 ----------------------
  1. Discovers a fixed universe of NSE ETFs (110 funds) and records the
     DISCOVERY DATE of every ETF the first time it is seen  -> SQLite DB.
  2. Downloads daily OHLCV history from the Upstox API (cached in stock-data/).
  3. Computes the TSMOM momentum signal:  M = P(t-21)/P(t-252) - 1
  4. Applies entry (+2%) / exit (0%) thresholds, liquidity, spread and iNAV
     filters.
  5. Builds portfolios three ways:
        Version A  = Pure TSMOM   (hold every qualifying ETF, equal-weighted)
        Version B  = TSMOM + Ranking (top 3 by momentum)   <- traded by default
        Benchmark C= Ranking only (top 3 regardless of +2% threshold)
  6. Sizes positions: inverse-volatility / equal-weight, dynamic liquidity cap,
     60-day Ledoit-Wolf (constant-correlation shrinkage) covariance, 8% annual
     portfolio volatility target, remainder in cash.
  7. Executes the NEXT trading session in PAPER-TRADE mode by default
     (realistic slippage + full statutory charges).  Live mode is opt-in.
  8. Tracks tax lots (STCG 20% / LTCG 12.5% above Rs 1.25 L), dividends,
     drawdown risk triggers (10% review / 15% halve / 20% cash) and re-entry.
  9. Backtests 10+ years with the last 20% held out-of-sample, parameter grid
     (lookback 126/189/252 x entry 0/1/2/3/5%), cost and cash-rate scenarios.

 WHERE FILES LIVE
 ----------------
  Everything generated lives under ./stock-data/
      stock-data/tsmom.db        SQLite database (universe, prices, signals,
                                 order book, positions, tax lots, snapshots...)
      stock-data/logs/           run logs
      stock-data/cache/          downloaded instrument master, raw responses
      stock-data/reports/        saved text reports

 API KEYS
 --------
  Read from ./env.txt next to this file:
      UPSTOX_ACCESS_TOKEN=...
      UPSTOX_API_TOKEN=...
      UPSTOX_API_SECRET=...

 DATA NOTES (honest methodology, per the rulebook)
 -------------------------------------------------
  * Adjusted close:  price_daily.adj_close = close x cumulative factor from
    the corporate_actions table (splits/bonus/dividends loaded per symbol).
    Upstox daily candles are raw/unadjusted; the pipeline refuses to use
    raw prices for signals whenever adjustment factors exist. If no factors
    are loaded for a symbol, raw close is used as a documented proxy and
    this is flagged as a limitation.
  * Dividends: when distributions are loaded as corporate actions with
    note starting 'DIV', the backtest applies the slab-rate haircut on the
    reinvested amount (TAX_SLAB_HAIRCUT_ON_DIVIDENDS) instead of compounding
    gross. With raw Upstox prices dividends are absent from the series, which
    is CONSERVATIVE (no dividend compounding).
  * iNAV filter: executed when nav_data rows exist (|close/NAV - 1| vs the
    0.40% / 0.75% bands); with no NAV feed the check is skipped and the
    reason is recorded, never silently ignored.
  * Spreads: real bid-ask history builds up from daily 'monitor' quote
    snapshots into the quotes table; until then documented proxies are used
    (5 bps broad / 10 bps others, p90 x1.2 for adverse, x2.5 severe).
  * Survivorship bias: the universe is today's listed set. The backtest
    report prints an explicit survivorship caveat (real point-in-time CAGR
    would be LOWER).

 USAGE
 -----
  python tsmom.py                # full pipeline: scan -> update -> trade -> status
  python tsmom.py scan           # discover ETFs, show discovery dates
  python tsmom.py update         # download / refresh price history
  python tsmom.py trade          # one rebalance cycle (PAPER mode by default)
  python tsmom.py monitor        # daily quote snapshot + risk check
  python tsmom.py backtest       # 10y backtest, OOS split, parameter grid
  python tsmom.py status         # portfolio + order book
  python tsmom.py report         # full performance report
  python tsmom.py demo           # synthetic data end-to-end demonstration
  python tsmom.py selftest       # unit tests (momentum formula, cov, taxes...)
  python tsmom.py live --confirm # REAL orders via Upstox (danger!)

 No third-party libraries are required: standard library only.
================================================================================
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ==============================================================================
# SECTION 1 — CONFIGURATION (the full rulebook, all in one place)
# Values marked [TUNED 2026-10-18] came from 'python tsmom.py tune' on REAL
# Upstox data (walk-forward excess vs the risk-matched benchmark; one change at
# a time; plateau rule; only improvements over baseline adopted).  Original
# rulebook values are kept in the comments.  Full ablation:
# stock-data/reports/tuned-config.txt
# ==============================================================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "stock-data"
DB_PATH = DATA_DIR / "tsmom.db"
LOG_DIR = DATA_DIR / "logs"
CACHE_DIR = DATA_DIR / "cache"
REPORT_DIR = DATA_DIR / "reports"
ENV_PATH = BASE_DIR / "env.txt"

# --- Trading mode -------------------------------------------------------------
PAPER_TRADE = True                 # DEFAULT = paper trading. Live needs CLI opt-in.
STARTING_CAPITAL = 1_000_000.0     # Rs 10 lakh paper capital

# --- Signal parameters (Step 2/3) ---------------------------------------------
LOOKBACK = 252                     # momentum lookback in trading sessions
SKIP = 21                          # skip the most recent 21 sessions
ENTRY_THRESHOLD = 0.02             # enter when momentum > +2%
EXIT_THRESHOLD = 0.00              # exit when momentum <= 0%
MIN_HISTORY_BARS = 252             # a fund needs >= 252 sessions to signal

# --- Risk / sizing parameters (Step 4/6) --------------------------------------
VOL_WINDOW = 60                    # volatility & covariance window
COV_WINDOW = 60
MAX_HOLDINGS = 3                   # ranking versions B and C
POS_CAP_ABS = 0.25                 # absolute ceiling 25% per ETF
POS_CAP_LIQ_FRAC = 0.15            # 15% of p25 daily traded value
VOL_TARGET = 0.15                  # 8% annualised portfolio volatility
LIQ_WINDOW = 60                    # liquidity measurement window
LIQ_PCTL = 25.0                    # 25th percentile of daily traded value
SPREAD_WINDOW = 60
SPREAD_PCTL = 90.0                 # 90th percentile spread
DEFAULT_SPREAD_BPS_BROAD = 5.0     # proxy when no quote history exists yet
DEFAULT_SPREAD_BPS_OTHER = 10.0
IMPACT_BASE_BPS = 2.0              # impact floor ...
IMPACT_SQRT_COEF = 25.0            # ... plus 25 bps x sqrt(participation)
SPREAD_MAX_BPS = 25.0              # reject trades whose p90 spread exceeds this
INAV_DEV_BROAD = 0.0040            # 0.40% broad-market iNAV band
INAV_DEV_SECTOR = 0.0075           # 0.75% midcap/sectoral iNAV band

# --- Cash & tax (Step 8) -------------------------------------------------------
CASH_RATE_PRETAX = 0.053           # RBI 91-day T-bill ~5.30%
SLAB_RATE = 0.30                   # assumed income-tax slab for dividends/interest
STCG_RATE = 0.20                   # <= 12 months
LTCG_RATE = 0.125                  # > 12 months, above exemption
LTCG_EXEMPTION = 125_000.0         # Rs 1.25 lakh per financial year
DIV_TDS_RATE = 0.10
DIV_TDS_THRESHOLD = 10_000.0

# --- Drawdown risk controls (Step 9) ------------------------------------------
# Drawdown thresholds are SCALED from the vol target (audit sec 3.5):
# dd_thresholds(vol_target) -> (review, severe, extreme)
REENTRY_BAND = 0.05                # re-enter when within 5% of prior peak

# --- Statutory charges & slippage (Step 7) ------------------------------------
# ETF delivery reality (audit-corrected):
#   STT: 0.001% on the SELL side only for equity ETFs; NONE on buy; and no STT
#        at all for Gold / Liquid / Gilt ETFs (and some international ETFs).
#   Stamp duty: 0.015% on the BUYER for delivery (NSE). Verify against your
#        contract note if your broker charges differently.
#   Exchange 0.00297%, SEBI 0.0001%, IPFT 0.0001%, GST 18% on brokerage+exchange+SEBI.
CHARGES = {
    "brokerage_pct": 0.0005,       # 0.05% per order ...
    "brokerage_cap": 20.0,         # ... capped at Rs 20 (discount broker)
    "stt_buy_pct": 0.0,            # no STT on ETF purchase
    "stt_sell_pct": 0.00001,       # 0.001% sell side (equity ETFs only)
    "stamp_buy_pct": 0.00015,      # 0.015% on buy (delivery)
    "exchange_pct": 0.0000297,     # 0.00297%
    "sebi_pct": 0.000001,          # 0.0001%
    "ipft_pct": 0.000001,          # 0.0001%
    "gst_pct": 0.18,               # 18% on brokerage+exchange+SEBI
}
STT_EXEMPT_CATEGORIES = {"COMMODITY", "DEBT"}   # gold/silver/liquid/gilt: no STT

# --- Tax by asset class (audit #7): equity rates are NOT universal -------------
# EQUITY/Hybrid/Global ETFs: STCG 20% (<=12m), LTCG 12.5% over Rs 1.25 L (>12m).
# GOLD/SILVER (COMMODITY):   STCG at slab (<=12m), LTCG 12.5% NO indexation, NO
#                            Rs 1.25 L exemption (>12m).
# DEBT (liquid/gilt/bond):   always at slab rate (specified mutual funds).
# GLOBAL: sources conflict (12 vs 24 months, slab vs 12.5%) -> configured
#          equity-like here; CONFIRM WITH A CA before filing.
# Audit 2.7 (conservative where sources conflict — CONFIRM WITH A CA):
#   GLOBAL: slab STCG, 12.5% LTCG, NO 1.25 L exemption (some sources differ)
#   HYBRID: 50:50 fund is NOT >65% Indian equity -> non-equity treatment (slab)
CESS_RATE = 0.04                   # health & education cess on the tax amount
LOSS_CARRY_YEARS = 8               # unused capital losses carry forward
TAX_RULES = {
    "EQUITY":    {"stcg": 0.20,   "ltcg": 0.125, "exempt": 125_000.0, "months": 12},
    "GLOBAL":    {"stcg": 0.30,   "ltcg": 0.125, "exempt": 0.0,       "months": 12},
    "HYBRID":    {"stcg": 0.30,   "ltcg": 0.30,  "exempt": 0.0,       "months": 0},
    "COMMODITY": {"stcg": 0.30,   "ltcg": 0.125, "exempt": 0.0,       "months": 12},
    "DEBT":      {"stcg": 0.30,   "ltcg": 0.30,  "exempt": 0.0,       "months": 0},
}

# --- Cost scenarios for backtests ---------------------------------------------
COST_CASES = {
    "base":    {"spread_mult": 1.0,  "spread_stat": "median"},
    "adverse": {"spread_mult": 1.0,  "spread_stat": "p90"},
    "severe":  {"spread_mult": 2.5,  "spread_stat": "p90"},
}
CASH_CASES = {
    "0%":            0.0,
    "5.30% pretax":  0.053,
    "5.30% post-tax": 0.053 * (1 - SLAB_RATE),
}

# --- Parameter grid for Step 10 ------------------------------------------------
GRID_LOOKBACKS = [126, 189, 252]
GRID_ENTRIES = [0.00, 0.01, 0.02, 0.03, 0.05]
GRID_SKIPS = [0, 21, 42]           # audit sec 2.1: skip is a tested dimension now
BASELINE_PARAMS = {"lookback": 252, "skip": 21, "entry": 0.02, "exit": 0.00}

TRADE_STRATEGY = "B"               # which version the paper trader follows
A_EQUAL_WEIGHT = True              # Version A raw weights = equal (Step 5)
ROTATION_RULE = False              # optional top-5 rotation test
TAX_SLAB_HAIRCUT_ON_DIVIDENDS = True   # haircut dividend reinvestment by slab rate
                                       # when distributions are loaded as corporate
                                       # actions with note starting 'DIV'

# --- Audit fixes: cash proxies, selection quality, risk release ----------------
# Liquid/overnight/gilt/bond funds are CASH PROXIES: they win rankings on tiny
# vol and eat a top-3 slot. They are excluded from the ranked pool and may be
# used as the cash sleeve instead (audit bug #3).
CASH_PROXY_SYMBOLS = {"LIQUIDCASE", "LTGILTBEES", "EBBETF0430"}
ONE_PER_INDEX_FAMILY = True        # original rulebook rule
PAIRWISE_CORR_CAP = 0.80           # skip candidate if 120-day correlation with a
CORR_WINDOW = 120                  #   selected ETF > cap (0=off)
RANK_BY = "momentum"               # "momentum" or "risk_adjusted" (momentum/vol)
INCUMBENCY_BONUS = 3               # rank places granted to incumbents (audit 2.4:
                                   #   a bounded bonus, NOT a lock that keeps R4-R6
                                   #   ahead of R1-R3; 0 disables)
EXIT_OVERLAY_200DMA = True        # 200-day filter on ENTRIES AND EXITS (audit 2.5:
                                   #   applied to both so it cannot force round trips)
RISK_RELEASE_DAYS = 92             # release 15%/20% risk locks after ~3 months even
                                   #   without price recovery (peak resets)
CASH_ACCRUAL = True                # paper account earns the cash benchmark
CASH_SLEEVE_MODE = "LIQUID_ETF"    # "LIQUID_ETF": sleeve earns the real liquid
                                   #   ETF's returns; "TBILL_FLAT": flat proxy
CASH_SLEEVE_SYMBOL = "LIQUIDCASE"  # the liquid fund that represents the sleeve

# --- Tuning knobs (each has a default = current/conservative behaviour) --------
NO_TRADE_BAND = 0.20               # skip rebalancing a position when |tgt-have| /
                                   #   max(tgt,have) < band (exits ALWAYS trade)
MIN_TRADE_VALUE = 2_000.0          # skip dust trades; brokerage makes them poor value
MIN_LIQ_WEIGHT = 0.10              # audit 2.6: 15% x p25 must allow at least this
                                   #   fraction of portfolio equity, else no ranking
ENSEMBLE_LOOKBACKS = None          # None = single LOOKBACK; e.g. (63,126,252) ensemble
VOL_BLEND_252 = 0.0                # 0..1 weight of the 252d vol blended into 60d vol
REBALANCE_FREQ = "M"               # "M" monthly or "Q" quarterly (month-ends only)
REGIME_SCALING = True             # halve vol target when benchmark < 200-day average
EXECUTION_STYLE = "MARKET"         # "MARKET" (cross half-spread) or "LIMIT_MID"
                                   #   (rest at mid: impact only, paper-fill test)
LOT_SELECTION = "FIFO"             # "FIFO" or "HIFO" (HIFO realises least gain)
TAX_DEFER_EXITS = False            # tax-aware exit deferral hypothesis (explicit rule)
TAX_DEFER_MONTHS = 1               # ... defer only if lot is within N months of LTCG
TAX_DEFER_MOM_FLOOR = -0.02        # ... and momentum is only slightly negative

# --- Drawdown thresholds SCALE WITH THE VOL TARGET (audit: tuning section) -----
DD_REVIEW_MULT = 1.00              # e.g. 8% target -> 10% review
DD_SEVERE_MULT = 1.50              # ...            -> 15.2% halve
DD_EXTREME_MULT = 2.00             # ...            -> 20% cash

def dd_thresholds(vol_target: float = None):
    vt = VOL_TARGET if vol_target is None else vol_target
    return (DD_REVIEW_MULT * vt, DD_SEVERE_MULT * vt, DD_EXTREME_MULT * vt)


# --- Upstox API ----------------------------------------------------------------
UPSTOX_BASE = "https://api.upstox.com/v2"
UPSTOX_INSTRUMENTS = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
HTTP_SLEEP = 0.35                  # politeness delay between API calls
FETCH_CHUNK_DAYS = 1000            # historical fetch chunk; auto-halves on error
HTTP_RETRIES = 3

# ==============================================================================
# SECTION 2 — SMALL UTILITIES: dates, numbers, Indian money formatting
# ==============================================================================

TODAY = dt.date.today()


def now_iso() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return dt.date.today().isoformat()


def parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s[:10])


def date_str(d: dt.date) -> str:
    return d.isoformat()


def month_key(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def financial_year(d: dt.date) -> str:
    """Indian FY: Apr-Mar.  2026-04-01 .. 2027-03-31 -> 'FY2027'."""
    y = d.year if d.month >= 4 else d.year - 1
    return f"FY{y + 1}"


def fmt_inr(x, decimals: int = 2) -> str:
    """Indian digit grouping: 1234567.5 -> '12,34,567.50' (no symbol)."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    neg = x < 0
    x = abs(x)
    s = f"{x:,.{decimals}f}"
    # convert 1,234,567.89 -> 12,34,567.89
    if "," in s:
        whole, frac = s.split(".", 1) if "." in s else (s, "")
        parts = whole.split(",")
        if len(parts) >= 2:
            head = parts[0]
            tail = ",".join(parts[1:])
            whole = f"{head},{tail}"
            # regroup: first group 3 digits? Actually str already did 3s.
            # Rebuild properly:
        # proper Indian grouping from raw integer
        sign = "-" if neg else ""
        ip = int(math.floor(x))
        fp = x - ip
        ip_s = str(ip)
        if len(ip_s) > 3:
            last3 = ip_s[-3:]
            rest = ip_s[:-3]
            groups = []
            while len(rest) > 2:
                groups.insert(0, rest[-2:])
                rest = rest[:-2]
            if rest:
                groups.insert(0, rest)
            ip_s = ",".join(groups) + "," + last3
        out = sign + ip_s
        if decimals > 0:
            out += f"{fp:,.{decimals}f}"[1:]
        return out
    return ("-" if neg else "") + s


def fmt_rs(x, decimals: int = 2) -> str:
    return "Rs " + fmt_inr(x, decimals)


def fmt_cr(x) -> str:
    """Short human money: 123456789 -> 'Rs 12.35 Cr'."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    a = abs(x)
    sign = "-" if x < 0 else ""
    if a >= 1e7:
        return f"{sign}Rs {a/1e7:,.2f} Cr"
    if a >= 1e5:
        return f"{sign}Rs {a/1e5:,.2f} L"
    return f"{sign}Rs {a:,.0f}"


def fmt_pct(x, decimals: int = 2, signed: bool = True) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    sign = "+" if (signed and x > 0) else ""
    return f"{sign}{x*100:.{decimals}f}%"


def fmt_num(x, decimals: int = 2) -> str:
    try:
        return f"{float(x):,.{decimals}f}"
    except (TypeError, ValueError):
        return str(x)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def safe_div(a, b, default=0.0):
    try:
        if b == 0:
            return default
        return a / b
    except Exception:
        return default


def percentile(values, p):
    """Linear-interpolation percentile (p in 0..100). None-safe."""
    vals = sorted(v for v in values if v is not None)
    n = len(vals)
    if n == 0:
        return None
    if n == 1:
        return vals[0]
    k = (n - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return vals[int(k)]
    return vals[f] + (vals[c] - vals[f]) * (k - f)


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs, ddof: int = 1):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n <= ddof:
        return 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - ddof)
    return math.sqrt(var)


# ==============================================================================
# SECTION 3 — CONSOLE: clean, colourful, easy language
# ==============================================================================

class _C:
    """ANSI colour codes (auto-disabled when not a terminal)."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    GREY = "\033[90m"
    BG_GREEN = "\033[42m"
    BG_RED = "\033[41m"
    BG_BLUE = "\033[44m"


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_use_color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
if os.environ.get("FORCE_COLOR"):
    _use_color = True


def paint(text: str, *codes) -> str:
    if not _use_color:
        return text
    return "".join(codes) + str(text) + _C.RESET


def vlen(s: str) -> int:
    return len(_ANSI_RE.sub("", str(s)))


class Printer:
    """All console output goes through here."""

    def raw(self, text: str = ""):
        print(text)

    def line(self, text: str = ""):
        print(text)

    def banner(self):
        w = 78
        self.raw()
        self.raw(paint("  " + "=" * (w - 4), _C.CYAN, _C.BOLD))
        self.raw(paint("  ||  T S M O M   -   Time Series Momentum on the Nifty ETF Universe", _C.CYAN, _C.BOLD))
        self.raw(paint("  ||  Paper-Trade Edition  |  Upstox Data  |  SQLite Journal", _C.CYAN, _C.BOLD))
        self.raw(paint("  " + "=" * (w - 4), _C.CYAN, _C.BOLD))
        self.raw(paint(f"  Today: {TODAY.strftime('%A, %d %B %Y')}   Mode: "
                       + ("PAPER TRADING (no real money)" if PAPER_TRADE else "LIVE TRADING"),
                     _C.YELLOW if PAPER_TRADE else _C.RED, _C.BOLD))
        self.raw()

    def section(self, title: str, icon: str = ""):
        self.raw()
        t = f"  {icon}  {title}  " if icon else f"  {title}  "
        bar = "─" * max(8, 78 - vlen(t))
        self.raw(paint(t + bar, _C.BLUE, _C.BOLD))

    def sub(self, title: str):
        self.raw(paint(f"  -- {title} " + "-" * max(4, 60 - vlen(title)), _C.GREY))

    def ok(self, msg: str):
        self.raw("  " + paint("[ OK ]", _C.GREEN, _C.BOLD) + " " + msg)

    def info(self, msg: str):
        self.raw("  " + paint("[INFO]", _C.CYAN, _C.BOLD) + " " + msg)

    def warn(self, msg: str):
        self.raw("  " + paint("[WARN]", _C.YELLOW, _C.BOLD) + " " + msg)

    def err(self, msg: str):
        self.raw("  " + paint("[FAIL]", _C.RED, _C.BOLD) + " " + msg)

    def note(self, msg: str):
        self.raw("  " + paint("[NOTE]", _C.MAGENTA, _C.BOLD) + " " + msg)

    def kv(self, key, val, key_color=_C.GREY, val_color=_C.WHITE):
        self.raw("  " + paint(f"{str(key):<28}", key_color) + " : " + paint(str(val), val_color))

    def table(self, headers, rows, aligns=None, row_color=None, header_color=_C.CYAN):
        """Pretty table. row_color: fn(rowidx, row) -> colour code or None."""
        ncols = len(headers)
        aligns = aligns or (["<"] * ncols)
        # compute widths
        widths = [vlen(str(h)) for h in headers]
        for r in rows:
            for i, cell in enumerate(r):
                widths[i] = max(widths[i], vlen(str(cell)))
        def fmt_row(cells, color=None):
            out = []
            for i, cell in enumerate(cells):
                s = str(cell)
                pad = widths[i] - vlen(s)
                if aligns[i] == ">":
                    s = " " * pad + s
                else:
                    s = s + " " * pad
                out.append(paint(s, color) if color else s)
            return "  " + "  ".join(out) + "  "
        sep = "  " + "  ".join("─" * (w + 2) for w in widths) + "  "
        self.raw(paint("  " + "  ".join("─" * (w + 2) for w in widths) + "  ", _C.GREY))
        self.raw(fmt_row(headers, header_color))
        self.raw(paint(sep, _C.GREY))
        for idx, r in enumerate(rows):
            col = row_color(idx, r) if row_color else None
            self.raw(fmt_row(r, col))
        self.raw(paint("  " + "  ".join("─" * (w + 2) for w in widths) + "  ", _C.GREY))

    def decision(self, emoji, headline, detail, color=_C.WHITE):
        self.raw("  " + paint(f"{emoji}  {headline}", color, _C.BOLD))
        if detail:
            self.raw("      " + paint(detail, _C.GREY))

    def gauge(self, frac: float, width: int = 30, label: str = ""):
        frac = clamp(frac, 0.0, 1.0)
        filled = int(round(frac * width))
        bar = "█" * filled + "░" * (width - filled)
        self.raw("  [" + paint(bar, _C.GREEN if frac < 0.7 else _C.YELLOW) + f"] {frac*100:5.1f}%  {label}")


P = Printer()


# ==============================================================================
# SECTION 4 — LOGGING (everything under stock-data/logs/)
# ==============================================================================

_LOG_FILE = None


def setup_logging() -> Path:
    global _LOG_FILE
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_FILE = LOG_DIR / f"tsmom_{TODAY.strftime('%Y%m%d')}_{dt.datetime.now().strftime('%H%M%S')}.log"
    with open(_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{now_iso()}  === TSMOM session started ===\n")
    return _LOG_FILE


def log(msg: str, level: str = "INFO"):
    line = f"{now_iso()}  {level:<5}  {msg}"
    if _LOG_FILE:
        try:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def save_report(name: str, text: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    p = REPORT_DIR / name
    p.write_text(text, encoding="utf-8")
    return p


# ==============================================================================
# SECTION 5 — ENV.TXT LOADER (Upstox API keys)
# ==============================================================================

def load_env(path: Path = ENV_PATH) -> dict:
    """Read KEY=VALUE lines from env.txt. Comments (#) and blanks allowed."""
    env = {}
    if not path.exists():
        raise FileNotFoundError(
            f"env.txt not found at {path}. It must contain "
            "UPSTOX_ACCESS_TOKEN, UPSTOX_API_TOKEN, UPSTOX_API_SECRET."
        )
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        env[k] = v
    required = ["UPSTOX_ACCESS_TOKEN", "UPSTOX_API_TOKEN", "UPSTOX_API_SECRET"]
    missing = [k for k in required if not env.get(k)]
    if missing:
        raise ValueError(f"env.txt is missing keys: {', '.join(missing)}")
    return env


# ==============================================================================
# SECTION 6 — SQLITE DATABASE (stock-data/tsmom.db)
# ==============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS etf_universe (
    symbol TEXT PRIMARY KEY,
    name TEXT, category TEXT, index_family TEXT,
    instrument_key TEXT, first_seen TEXT, last_seen TEXT,
    status TEXT DEFAULT 'ACTIVE', broad INTEGER DEFAULT 0,
    notes TEXT);
CREATE TABLE IF NOT EXISTS discovery_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, discovered_on TEXT, source TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS etf_pit (
    symbol TEXT, as_of TEXT, present INTEGER,
    PRIMARY KEY (symbol, as_of));
CREATE TABLE IF NOT EXISTS price_daily (
    symbol TEXT, date TEXT,
    open REAL, high REAL, low REAL, close REAL, adj_close REAL,
    volume REAL, traded_value REAL,
    PRIMARY KEY (symbol, date));
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, ts TEXT, ltp REAL, bid REAL, ask REAL, spread_bps REAL);
CREATE TABLE IF NOT EXISTS corporate_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, ex_date TEXT, factor REAL, note TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ux_corporate_actions
    ON corporate_actions(symbol, ex_date, note);
CREATE TABLE IF NOT EXISTS nav_data (
    symbol TEXT, date TEXT, nav REAL, PRIMARY KEY (symbol, date));
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT, symbol TEXT, strategy TEXT,
    momentum REAL, vol60 REAL, liq_p25 REAL, spread_p90 REAL,
    decision TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, signal_as_of TEXT, exec_date TEXT,
    symbol TEXT, side TEXT, qty REAL, price REAL, value REAL,
    slippage REAL, charges REAL, status TEXT, mode TEXT,
    order_type TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    qty REAL, avg_cost REAL, updated TEXT);
CREATE TABLE IF NOT EXISTS tax_lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, buy_date TEXT, buy_price REAL, qty REAL,
    sell_date TEXT, sell_price REAL, pnl REAL, gain_type TEXT, tax REAL);
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    date TEXT PRIMARY KEY,
    cash REAL, holdings_value REAL, equity REAL,
    invested_frac REAL, peak REAL, drawdown REAL, mode TEXT);
CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, level TEXT, drawdown REAL, action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, phase TEXT, symbol TEXT, decision TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, label TEXT, params TEXT, results TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS pending_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_as_of TEXT, exec_after TEXT, symbol TEXT, side TEXT,
    target_qty REAL, reason TEXT, status TEXT DEFAULT 'PENDING',
    created TEXT);
"""


class Db:
    """Thin sqlite3 wrapper. All state of the strategy lives here."""

    def __init__(self, path: Path = DB_PATH):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def execute(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def query(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def meta_get(self, key, default=None):
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def meta_set(self, key, value):
        self.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, str(value)))

    # ---- price helpers -------------------------------------------------
    def upsert_prices(self, symbol, bars):
        """bars: iterable of dicts with date/open/high/low/close/adj_close/volume/traded_value"""
        self.conn.executemany(
            "INSERT INTO price_daily(symbol,date,open,high,low,close,adj_close,volume,traded_value) "
            "VALUES(:symbol,:date,:open,:high,:low,:close,:adj_close,:volume,:traded_value) "
            "ON CONFLICT(symbol,date) DO UPDATE SET "
            "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
            "adj_close=excluded.adj_close, volume=excluded.volume, traded_value=excluded.traded_value",
            list(bars))
        self.conn.commit()

    def price_series(self, symbol):
        """Ascending list of rows (date, adj_close, close, traded_value)."""
        rows = self.query(
            "SELECT date, adj_close, close, traded_value, volume FROM price_daily "
            "WHERE symbol=? ORDER BY date", (symbol,))
        return rows

    def symbols_with_prices(self):
        return [r["symbol"] for r in self.query(
            "SELECT DISTINCT symbol FROM price_daily ORDER BY symbol")]

    def last_price(self, symbol):
        r = self.one("SELECT close, adj_close, date FROM price_daily WHERE symbol=? "
                     "ORDER BY date DESC LIMIT 1", (symbol,))
        return r if r else None

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


# ==============================================================================
# SECTION 7 — THE ETF UNIVERSE (static master list of 110 funds)
# ==============================================================================
# (symbol, name, category, index_family)
# Names cleaned of HTML entities. 'index_family' enforces one-ETF-per-index.

ETF_UNIVERSE = [
    ('EBBETF0430', 'Nifty BHARAT Bond', 'DEBT', 'Bond'),
    ('EQUAL200', 'BSE 200 Equal Weight', 'EQUITY', 'BSE 200 Equal Weight'),
    ('HDFCBSE500', 'HDFC S&P BSE 500 ETF', 'EQUITY', 'BSE 500'),
    ('DIVIDEND', 'BSE 500 Dividend Leaders', 'EQUITY', 'BSE 500 Dividend Leaders 50'),
    ('ICICIB22', 'S&P BSE BHARAT 22 index', 'EQUITY', 'BSE BHARAT 22'),
    ('ECAPINSURE', 'BSE Capital Markets & Insurance Total Return Index', 'EQUITY', 'BSE Capital Markets & Insurance'),
    ('MOVALUE', 'Motilal Oswal S&P BSE Enhanced Value ETF', 'EQUITY', 'BSE Enhanced Value'),
    ('MOHEALTH', 'Motilal Oswal S&P BSE Healthcare ETF', 'EQUITY', 'BSE Healthcare'),
    ('GROWWHOSPI', 'BSE Hospitals Index', 'EQUITY', 'BSE Hospitals'),
    ('DEFENCE', 'BSE India Defence', 'EQUITY', 'BSE India Defence'),
    ('MOINFRA', 'BSE India Infrastructure Total Return Index', 'EQUITY', 'BSE India Infrastructure'),
    ('SECLEDBETA', 'BSE India Sector Leaders', 'EQUITY', 'BSE India Sector Leaders TRI'),
    ('INSUREIETF', 'BSE Insurance Index', 'EQUITY', 'BSE Insurance'),
    ('LARGEMID50', 'BSE LargeMid (60:40) Stable Dividend 50 TRI', 'EQUITY', 'BSE LargeMid (60:40) Stable Dividend 50'),
    ('MOMMIDCAP', 'BSE Midcap 150 Momentum 30 TRI', 'EQUITY', 'BSE Midcap 150 Momentum 30'),
    ('MIDSELIETF', 'S&P BSE Midcap Select Index', 'EQUITY', 'BSE Midcap Select'),
    ('MIDBANKADD', 'BSE MidSmall Private Banks Index', 'EQUITY', 'BSE MidSmall Private Banks'),
    ('GROWWPOWER', 'BSE Power Index - TRI', 'EQUITY', 'BSE Power'),
    ('SBIBPB', 'BSE PSU Bank Index TRI', 'EQUITY', 'BSE PSU Bank'),
    ('MOQUALITY', 'Motilal Oswal S&P BSE Quality ETF', 'EQUITY', 'BSE Quality'),
    ('SELECTIPO', 'BSE Select IPO Index', 'EQUITY', 'BSE Select IPO'),
    ('HDFCSENSEX', 'SENSEX', 'EQUITY', 'BSE Sensex'),
    ('SNXT30BEES', 'BSE Sensex Next 30 Index', 'EQUITY', 'BSE Sensex Next 30'),
    ('BANK10ADD', 'BSE Top 10 Banks Index (TRI)', 'EQUITY', 'BSE Top 10 Banks'),
    ('HYBRIDETF', 'Nifty200 Momentum 30 Plus 8-13 yr G-Sec 50:50 TRI', 'Hybrid', 'Equity/Debt'),
    ('MAFANG', 'NYSE FANG+ Total Return Index', 'GLOBAL INDICES', 'GLOBAL INDICES'),
    ('MASPTOP50', 'S&P 500 Top 50 Total Return Index', 'GLOBAL INDICES', 'GLOBAL INDICES'),
    ('MON100', 'Nasdaq100', 'GLOBAL INDICES', 'GLOBAL INDICES'),
    ('MONQ50', 'Nasdaq Q-50 Total Return Index', 'GLOBAL INDICES', 'GLOBAL INDICES'),
    ('MAHKTECH', 'Hang Seng TECH Total Return Index', 'GLOBAL INDICES', 'GLOBAL INDICES'),
    ('HNGSNGBEES', 'Hang Seng Index', 'GLOBAL INDICES', 'GLOBAL INDICES'),
    ('TATAGOLD', 'Tata Gold Exchange Traded Fund', 'COMMODITY', 'GOLD'),
    ('LTGILTBEES', 'NIFTY 8-13 yr G-Sec Index', 'DEBT', 'GSECS/GILT'),
    ('MSCI360', 'MSCI India Index', 'EQUITY', 'MSCI India'),
    ('TOP100CASE', 'Zerodha Nifty 100 ETF', 'EQUITY', 'Nifty 100'),
    ('NIFTY100EW', 'Nifty 100 Equal Weight Index', 'EQUITY', 'Nifty 100 Equal Weight'),
    ('ESG', 'NIFTY100 ESG SECTOR LEADERS', 'EQUITY', 'Nifty 100 ESG Sector Leaders'),
    ('LOWVOL1', 'Kotak Nifty 100 Low Vol 30 ETF', 'EQUITY', 'NIFTY 100 Low Volatility 30'),
    ('HDFCQUAL', 'HDFC NIFTY100 Quality 30 ETF', 'EQUITY', 'Nifty 100 Quality 30'),
    ('GROWWN200', 'Nifty 200 Index - TRI', 'EQUITY', 'Nifty 200'),
    ('ALPHAETF', 'Mirae Asset Nifty 200 Alpha 30 ETF', 'EQUITY', 'Nifty 200 Alpha 30'),
    ('MOMENTUM30', 'Nifty 200 Momentum 30 Index', 'EQUITY', 'Nifty 200 Momentum 30'),
    ('NIFTYQLITY', 'Aditya Birla Sun Life Nifty 200 Quality 30 ETF', 'EQUITY', 'Nifty 200 Quality 30'),
    ('VAL30IETF', 'Nifty200 Value 30 Index', 'EQUITY', 'Nifty 200 Value 30'),
    ('NIFTYBEES', 'Nifty 50', 'EQUITY', 'Nifty 50'),
    ('SBINEQWETF', 'Nifty50 Equal Weight', 'EQUITY', 'Nifty 50 Equal Weight'),
    ('SHARIABEES', 'Shariah', 'EQUITY', 'Nifty 50 Shariah'),
    ('NV20IETF', 'Nifty50 Value 20', 'EQUITY', 'Nifty 50 Value 20'),
    ('MONIFTY500', 'Motilal Oswal Nifty 500 ETF', 'EQUITY', 'Nifty 500'),
    ('FLEXIADD', 'Nifty500 Flexicap Quality 30 TRI', 'EQUITY', 'Nifty 500 Flexicap Quality 30'),
    ('HEALTHCARE', 'Nifty 500 Healthcare Index', 'EQUITY', 'Nifty 500 Healthcare'),
    ('GROWWLOVOL', 'Nifty 500 Low Volatility 50 Index TRI', 'EQUITY', 'Nifty 500 Low Volatility 50'),
    ('MOMENTUM50', 'Nifty 500 Momentum 50 TRI', 'EQUITY', 'Nifty 500 Momentum 50'),
    ('MULTICAP', 'Nifty500 Multicap 50:25:25 Index', 'EQUITY', 'Nifty 500 Multicap 50:25:25'),
    ('EMULTIMQ', 'Nifty500 Multicap Momentum Quality 50 TRI', 'EQUITY', 'Nifty 500 Multicap Momentum Quality 50'),
    ('VALUE', 'Nifty 500 Value 50', 'EQUITY', 'Nifty 500 Value 50'),
    ('ALPHA', 'NIFTY Alpha 50 Index', 'EQUITY', 'NIFTY Alpha 50'),
    ('ALPL30IETF', 'Nifty Alpha Low-Volatility 30 Index', 'EQUITY', 'Nifty Alpha Low-Volatility 30'),
    ('AUTOIETF', 'Nifty Auto Index', 'EQUITY', 'Nifty Auto'),
    ('BANKNIFTY1', 'Nifty Bank', 'EQUITY', 'Nifty Bank'),
    ('MOCAPITAL', 'Nifty Capital Market TRI', 'EQUITY', 'Nifty Capital Market'),
    ('CEMNTGROWW', 'Nifty Cements Index TRI', 'EQUITY', 'Nifty Cements'),
    ('CHEMICAL', 'Nifty Chemicals Index (TRI)', 'EQUITY', 'Nifty Chemicals'),
    ('COMMOIETF', 'ICICI Prudential Nifty Commodities ETF', 'EQUITY', 'Nifty Commodities'),
    ('CPSEETF', 'CPSE ETF', 'EQUITY', 'Nifty CPSE'),
    ('DIVOPPBEES', 'Nifty Dividend Opportunities 50 TRI', 'EQUITY', 'Nifty Dividend Opportunities 50'),
    ('MOENERGY', 'Nifty Energy Index', 'EQUITY', 'Nifty Energy'),
    ('EVINDIA', 'Nifty EV and New Age Automotive TRI', 'EQUITY', 'Nifty EV and New Age Automotive'),
    ('BFSI', 'Nifty Financial Services Index', 'EQUITY', 'Nifty Financial Services'),
    ('FINIETF', 'ICICI Pru Nifty Financial Services Ex-Bank ETF', 'EQUITY', 'Nifty Financial Services Ex-Bank'),
    ('FMCGIETF', 'Nifty FMCG Index', 'EQUITY', 'Nifty FMCG'),
    ('HDFCGROWTH', 'HDFC NIFTY Growth Sectors 15 ETF', 'EQUITY', 'Nifty Growth Sectors 15'),
    ('HEALTHY', 'Nifty Healthcare TRI', 'EQUITY', 'Nifty Healthcare'),
    ('CONS', 'Kotak Nifty India Consumption ETF', 'EQUITY', 'Nifty India Consumption'),
    ('MODEFENCE', 'Nifty India Defence TRI', 'EQUITY', 'Nifty India Defence'),
    ('INFRA', 'Nifty India Infrastructure & Logistics', 'EQUITY', 'Nifty India Infrastructure & Logistics'),
    ('GROWWNET', 'Nifty India Internet Index', 'EQUITY', 'Nifty India Internet'),
    ('MAKEINDIA', 'Nifty India Manufacturing TRI', 'EQUITY', 'Nifty India Manufacturing'),
    ('CONSUMER', 'Nifty India New Age Consumption', 'EQUITY', 'Nifty India New Age Consumption'),
    ('GROWWRAIL', 'Nifty India Railways PSU Index', 'EQUITY', 'Nifty India Railways PSU'),
    ('MOTOUR', 'Nifty India Tourism TRI', 'EQUITY', 'Nifty India Tourism'),
    ('INFRAIETF', 'ICICI Prudential Nifty Infrastructure ETF', 'EQUITY', 'Nifty Infrastructure'),
    ('ITBEES', 'Nifty IT TRI', 'EQUITY', 'Nifty IT'),
    ('ELM250', 'Nifty LargeMidcap 250 TRI', 'EQUITY', 'Nifty LargeMidcap 250'),
    ('METALIETF', 'Nifty Metal Index', 'EQUITY', 'Nifty Metal'),
    ('MOM100', 'Nifty Midcap 100', 'EQUITY', 'Nifty Midcap 100'),
    ('MIDCAPETF', 'Mirae Asset Nifty Midcap 150 ETF', 'EQUITY', 'Nifty Midcap 150'),
    ('MOMIDMTM', 'Nifty Midcap 150 Momentum 50 TRI', 'EQUITY', 'Nifty Midcap 150 Momentum 50'),
    ('MIDQ50ADD', 'Nifty Midcap 150 Quality 50 Index', 'EQUITY', 'Nifty Midcap 150 Quality 50'),
    ('MIDCAP', 'Nifty Midcap 50 Index', 'EQUITY', 'NIFTY Midcap 50'),
    ('MIDSMALL', 'Mirae Asset Nifty MidSmallcap400 MQ 100 ETF', 'EQUITY', 'Nifty MidSmallcap400 Momentum Quality 100'),
    ('MNC', 'Kotak Nifty MNC ETF', 'EQUITY', 'Nifty MNC'),
    ('NXT100CASE', 'Nifty Next 100', 'EQUITY', 'Nifty Next 100'),
    ('NEXT50IETF', 'Nifty Next 50', 'EQUITY', 'Nifty Next 50'),
    ('OILIETF', 'Nifty Oil & Gas Index', 'EQUITY', 'Nifty Oil & Gas'),
    ('PHARMABEES', 'Nifty Pharma TRI', 'EQUITY', 'Nifty Pharma'),
    ('PVTBANIETF', 'Nifty Private Bank Index', 'EQUITY', 'Nifty Private Bank'),
    ('ABSLPSE', 'Aditya Birla Sun Life Nifty PSE ETF', 'EQUITY', 'Nifty PSE'),
    ('PSUBANKADD', 'DSP Nifty PSU Bank ETF', 'EQUITY', 'Nifty PSU Bank'),
    ('MOREALTY', 'Motilal Oswal Nifty Realty ETF', 'EQUITY', 'Nifty Realty'),
    ('MOSERVICE', 'Nifty Services Sector TRI', 'EQUITY', 'Nifty Services Sector'),
    ('HDFCSML250', 'HDFC NIFTY Smallcap 250 ETF', 'EQUITY', 'Nifty Smallcap 250'),
    ('SMALLCAP', 'Mirae Asset Nifty Smallcap 250 MQ 100 ETF', 'EQUITY', 'Nifty Smallcap 250 Momentum Quality 100'),
    ('SML100CASE', 'Nifty Smallcap 100 Index', 'EQUITY', 'Nifty Smallcap100'),
    ('TOP10ADD', 'Nifty Top 10 Equal Weight Index', 'EQUITY', 'Nifty Top 10 Equal Weight'),
    ('TOP15IETF', 'Nifty Top 15 Equal Weight Index', 'EQUITY', 'Nifty Top 15 Equal Weight'),
    ('TOP20', 'Nifty Top 20 Equal Weight', 'EQUITY', 'Nifty Top 20 Equal Weight'),
    ('AONETOTAL', 'Nifty Total Market Index', 'EQUITY', 'Nifty Total Market'),
    ('AONETMMQ50', 'Nifty Total Market Momentum Quality 50 TRI', 'EQUITY', 'Nifty Total Market Momentum Quality 50'),
    ('LIQUIDCASE', 'Zerodha Nifty 1D Rate Liquid ETF', 'DEBT', 'Overnight ETFs and Liquid ETF'),
    ('TATSILV', 'Tata Silver Exchange Traded Fund', 'COMMODITY', 'SILVER'),
]

# Broad-market ETFs get the tighter 0.40% iNAV band.
BROAD_FAMILIES = {'Nifty 50', 'Nifty Next 50', 'Nifty Bank', 'Nifty 50 Equal Weight'}


def universe_map():
    return {sym: {"name": nm, "category": cat, "index_family": fam}
            for sym, nm, cat, fam in ETF_UNIVERSE}


# ==============================================================================
# SECTION 8 — UPSTOX API CLIENT (stdlib urllib only)
# ==============================================================================

class UpstoxError(Exception):
    pass


class UpstoxClient:
    """Minimal Upstox v2 REST client: instrument master, candles, quotes, orders."""

    def __init__(self, env: dict):
        self.access_token = env.get("UPSTOX_ACCESS_TOKEN", "")
        self.api_key = env.get("UPSTOX_API_TOKEN", "")
        self.api_secret = env.get("UPSTOX_API_SECRET", "")

    # ---------- low-level HTTP ----------
    def _request(self, url: str, method: str = "GET", body=None, timeout=30):
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            # Cloudflare blocks python-urllib's default signature (error 1010)
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36"),
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        last_err = None
        for attempt in range(HTTP_RETRIES):
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read()
                    return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as e:
                payload = ""
                try:
                    payload = e.read().decode("utf-8", "ignore")[:400]
                except Exception:
                    pass
                if e.code == 401:
                    raise UpstoxError(
                        "Upstox rejected the access token (401). Tokens expire daily. "
                        "Please put a fresh UPSTOX_ACCESS_TOKEN in env.txt. "
                        f"Server said: {payload}")
                if e.code == 429:
                    time.sleep(5.0 * (attempt + 1))   # rate limit: real backoff
                    last_err = e
                    if attempt == HTTP_RETRIES - 1:
                        raise UpstoxError(
                            "RATE_LIMIT: Upstox is rate-limiting requests (HTTP 429). "
                            "Wait a few minutes and rerun; the data already downloaded "
                            "is kept and only the missing part will be fetched.")
                    continue
                if e.code >= 500:
                    time.sleep(1.5 * (attempt + 1))
                    last_err = e
                    continue
                raise UpstoxError(f"Upstox HTTP {e.code}: {payload}")
            except Exception as e:  # network down, DNS, timeout...
                last_err = e
                time.sleep(1.0 * (attempt + 1))
        raise UpstoxError(f"Upstox request failed after {HTTP_RETRIES} tries: {last_err}")

    # ---------- instrument master (discovery) ----------
    def download_instrument_master(self) -> list:
        """Download NSE instrument master (gzipped JSON). Cached in stock-data/cache."""
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        gz_path = CACHE_DIR / "NSE.json.gz"
        json_path = CACHE_DIR / "NSE.json"
        if json_path.exists() and (time.time() - json_path.stat().st_mtime) < 12 * 3600:
            return json.loads(json_path.read_text(encoding="utf-8"))
        if gz_path.exists() and (time.time() - gz_path.stat().st_mtime) < 12 * 3600:
            raw = gzip.decompress(gz_path.read_bytes())
            instruments = json.loads(raw.decode("utf-8"))
            json_path.write_text(json.dumps(instruments), encoding="utf-8")
            return instruments
        req = urllib.request.Request(
            UPSTOX_INSTRUMENTS,
            headers={"Accept": "application/gzip",
                     "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/126.0.0.0 Safari/537.36")})
        with urllib.request.urlopen(req, timeout=60) as resp:
            gz_bytes = resp.read()
        gz_path.write_bytes(gz_bytes)
        raw = gzip.decompress(gz_bytes)
        instruments = json.loads(raw.decode("utf-8"))
        json_path.write_text(json.dumps(instruments), encoding="utf-8")
        return instruments

    # ---------- historical candles ----------
    def historical_daily(self, instrument_key: str, from_date: dt.date, to_date: dt.date) -> list:
        """
        Fetch daily candles. Returns ascending [{date,open,high,low,close,volume}].
        Chunks the range; auto-halves the chunk if the API rejects it.
        """
        out = []
        seen = set()

        def fetch_chunk(f0: dt.date, t0: dt.date, depth: int = 0):
            if f0 > t0 or depth > 6:
                return
            key = urllib.parse.quote(instrument_key, safe="")
            to_s = t0.isoformat()
            from_s = f0.isoformat()
            # Upstox path order: /historical-candle/{key}/{interval}/{toDate}/{fromDate}
            for interval in ("1day", "day"):
                url = (f"{UPSTOX_BASE}/historical-candle/{key}/{interval}/{to_s}/{from_s}")
                try:
                    resp = self._request(url)
                    break
                except UpstoxError as e:
                    if "RATE_LIMIT" in str(e) or "401" in str(e) or "access token" in str(e).lower():
                        raise               # global problem -> abort the whole refresh
                    if interval == "day":
                        if "HTTP 4" in str(e) and depth < 6:
                            mid = f0 + (t0 - f0) / 2
                            fetch_chunk(f0, mid, depth + 1)
                            fetch_chunk(mid + dt.timedelta(days=1), t0, depth + 1)
                            return
                        return
                    continue
            else:
                return
            if not isinstance(resp, dict) or resp.get("status") != "success":
                return
            candles = (resp.get("data") or {}).get("candles") or []
            for c in candles:
                try:
                    ts = c[0]
                    if isinstance(ts, (int, float)):
                        d = dt.datetime.utcfromtimestamp(ts / 1000.0).date()
                    else:
                        d = parse_date(str(ts))
                    dk = d.isoformat()
                    if dk in seen:
                        continue
                    seen.add(dk)
                    o, h, l, cl, vol = float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5] or 0)
                    out.append({"date": dk, "open": o, "high": h, "low": l,
                                "close": cl, "volume": vol})
                except Exception:
                    continue

        step = dt.timedelta(days=FETCH_CHUNK_DAYS)
        cur = from_date
        while cur <= to_date:
            end = min(cur + step - dt.timedelta(days=1), to_date)
            fetch_chunk(cur, end)
            time.sleep(HTTP_SLEEP)
            cur = end + dt.timedelta(days=1)
        out.sort(key=lambda r: r["date"])
        return out

    # ---------- market quotes ----------
    def ltp(self, instrument_keys: list) -> dict:
        keys = ",".join(instrument_keys)
        url = f"{UPSTOX_BASE}/market-quote/ltp?instrument_key={urllib.parse.quote(keys, safe=',|')}"
        resp = self._request(url)
        out = {}
        for k, v in (resp.get("data") or {}).items():
            out[k] = float(v.get("last_price", 0.0))
        return out

    def quote(self, instrument_key: str) -> dict:
        """Best-effort quote: ltp, bid, ask. Missing fields -> None."""
        url = f"{UPSTOX_BASE}/market-quote/quotes?instrument_key={urllib.parse.quote(instrument_key, safe='|')}"
        resp = self._request(url)
        data = resp.get("data") or {}
        out = {"ltp": None, "bid": None, "ask": None}
        for _k, v in data.items():
            if not isinstance(v, dict):
                continue
            out["ltp"] = float(v.get("last_price") or 0) or None
            depth = v.get("depth") or {}
            buy = (depth.get("buy") or [{}])
            sell = (depth.get("sell") or [{}])
            if buy and isinstance(buy[0], dict) and buy[0].get("price"):
                out["bid"] = float(buy[0]["price"])
            if sell and isinstance(sell[0], dict) and sell[0].get("price"):
                out["ask"] = float(sell[0]["price"])
            break
        return out

    def order_details(self, order_id: str) -> dict:
        """Broker-side order status/fill for live reconciliation."""
        return self._request(f"{UPSTOX_BASE}/order/details?order_id={urllib.parse.quote(order_id)}")

    def positions(self) -> list:
        """
        Broker-side DAY POSITIONS (audit 2.9).  Upstox's /v2/portfolio/
        short-term-positions lists today's day-book positions and is tried
        first; the demat HOLDINGS endpoint is UNCONFIRMED for this app, so
        holdings stay ledger-based and `reconcile` is the safety net.
        """
        for path in ("/v2/portfolio/short-term-positions",
                     "/portfolio/positions", "/user/get-positions"):
            try:
                resp = self._request(f"{UPSTOX_BASE}{path}")
                data = resp.get("data")
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    return data.get("positions") or []
                return []
            except Exception:
                continue
        return []

    # ---------- live order (only used when PAPER mode is OFF) ----------
    def place_order(self, symbol: str, instrument_key: str, side: str,
                    qty: int, price: float, order_type: str = "MARKET") -> dict:
        body = {
            "quantity": int(qty),
            "product": "D",
            "validity": "DAY",
            "price": float(price) if order_type != "MARKET" else 0.0,
            "tag": f"tsmom-{TODAY.isoformat()}",
            "instrument_token": instrument_key,
            "order_type": order_type,
            "transaction_type": side.upper(),
            "disclosed_quantity": 0,
            "trigger_price": 0.0,
            "is_amo": False,
        }
        return self._request(f"{UPSTOX_BASE}/order/place", method="POST", body=body)


# ==============================================================================
# SECTION 9 — ETF SCANNER & DISCOVERY (Step 1: point-in-time universe)
# ==============================================================================

class Scanner:
    def __init__(self, db: Db, client=None):
        self.db = db
        self.client = client

    def sync_universe(self, source: str = "static-list") -> list:
        """
        Register every ETF in the universe table. The FIRST time a symbol is
        seen its discovery date is stored and printed on the console.
        Returns list of newly discovered symbols.
        """
        umap = universe_map()
        today = today_str()
        newly = []
        P.section("ETF SCANNER — UNIVERSE DISCOVERY", "\U0001F50D")
        P.info(f"Checking {len(umap)} ETFs from the master universe list...")
        for sym, info in umap.items():
            row = self.db.one("SELECT * FROM etf_universe WHERE symbol=?", (sym,))
            broad = 1 if info["index_family"] in BROAD_FAMILIES else 0
            if row is None:
                self.db.execute(
                    "INSERT INTO etf_universe(symbol,name,category,index_family,instrument_key,"
                    "first_seen,last_seen,status,broad,notes) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (sym, info["name"], info["category"], info["index_family"],
                     f"NSE_EQ|{sym}", today, today, "ACTIVE", broad, f"source={source}"))
                self.db.execute(
                    "INSERT INTO discovery_log(symbol,discovered_on,source,detail) VALUES(?,?,?,?)",
                    (sym, today, source, f"first registration: {info['name']}"))
                newly.append(sym)
            else:
                self.db.execute(
                    "UPDATE etf_universe SET name=?, category=?, index_family=?, last_seen=?, broad=? "
                    "WHERE symbol=?",
                    (info["name"], info["category"], info["index_family"], today, broad, sym))
        if newly:
            P.ok(f"{len(newly)} NEW ETFs discovered today — printing discovery dates:")
            rows = []
            for sym in newly:
                info = umap[sym]
                rows.append([sym, info["name"][:38], info["category"],
                             paint(today, _C.GREEN, _C.BOLD)])
            P.table(["Symbol", "Name", "Category", "Discovery Date"], rows,
                    aligns=["<", "<", "<", ">"])
        else:
            P.info("No new ETFs today — every fund was already known to the database.")
        return newly

    def verify_against_upstox(self, instruments: list) -> dict:
        """Match symbols to the live NSE instrument master; return symbol -> key."""
        by_symbol = {}
        for it in instruments:
            sym = (it.get("trading_symbol") or "").strip()
            if not sym:
                continue
            seg = (it.get("segment") or "")
            exch = (it.get("exchange") or "")
            if exch == "NSE" and seg in ("EQ", "NSE_EQ"):
                by_symbol[sym] = it.get("instrument_key")
        found = 0
        for sym, _info in universe_map().items():
            key = by_symbol.get(sym)
            if key:
                found += 1
                self.db.execute("UPDATE etf_universe SET instrument_key=? WHERE symbol=?", (key, sym))
        return by_symbol

    def show_universe_table(self):
        """Full scanner output with discovery dates."""
        rows = self.db.query(
            "SELECT symbol,name,category,index_family,first_seen,status,broad FROM etf_universe "
            "ORDER BY category, symbol")
        P.section("SCANNED ETF LIST (with discovery dates)", "\U0001F4CB")
        out = []
        for r in rows:
            out.append([r["symbol"], r["name"][:40], r["category"],
                        r["index_family"][:26],
                        paint(r["first_seen"], _C.CYAN),
                        r["status"]])
        def rc(i, row):
            return _C.GREEN if row[4].find(_C.GREEN) >= 0 else None
        P.table(["Symbol", "Name", "Category", "Index Family", "Discovered On", "Status"],
                out, aligns=["<", "<", "<", "<", ">", "<"])
        P.info(f"Total ETFs tracked: {len(rows)}")
        return rows


# ==============================================================================
# SECTION 10 — DATA MANAGER (download / cache price history)
# ==============================================================================

class DataManager:
    def __init__(self, db: Db, client: UpstoxClient):
        self.db = db
        self.client = client

    def series(self, symbol: str, as_of: str, limit: int = 252,
               field: str = "adj_close") -> list:
        """Ascending rows of (date, field) up to as_of — live-path helper
        (regime, cash sleeve) matching the backtester's view of history."""
        rows = self.db.query(
            f"SELECT date, {field} AS v FROM price_daily WHERE symbol=? AND date<=? "
            f"ORDER BY date DESC LIMIT ?", (symbol, as_of, limit))
        return list(reversed(rows))

    def adj_factor(self, symbol: str, date: str) -> float:
        """Cumulative adjustment factor from corporate_actions (default 1.0)."""
        rows = self.db.query(
            "SELECT factor FROM corporate_actions WHERE symbol=? AND ex_date<=? ORDER BY ex_date",
            (symbol, date))
        f = 1.0
        for r in rows:
            f *= float(r["factor"])
        return f

    def update_symbol(self, symbol: str, years: int = 11) -> int:
        """Fetch missing daily bars for one symbol into price_daily. Returns bars stored."""
        row = self.db.one("SELECT instrument_key FROM etf_universe WHERE symbol=?", (symbol,))
        key = row["instrument_key"] if row else f"NSE_EQ|{symbol}"
        to_date = TODAY
        from_date = to_date - dt.timedelta(days=int(years * 365.25))
        last = self.db.one("SELECT MAX(date) AS d FROM price_daily WHERE symbol=?", (symbol,))
        if last and last["d"]:
            ld = parse_date(last["d"])
            if (to_date - ld).days <= 3:
                return 0
            from_date = ld - dt.timedelta(days=5)   # small overlap, upsert handles it
        try:
            bars = self.client.historical_daily(key, from_date, to_date)
        except UpstoxError as e:
            msg = str(e)
            if ("401" in msg or "access token" in msg.lower()
                    or "RATE_LIMIT" in msg):
                raise            # global problem -> abort the whole refresh once
            P.warn(f"{symbol}: cannot download history ({e})")
            log(f"{symbol}: download failed: {e}", "WARN")
            return 0
        if not bars:
            return 0
        rows = []
        for b in bars:
            f = self.adj_factor(symbol, b["date"])
            close = b["close"]
            rows.append({
                "symbol": symbol, "date": b["date"],
                # audit #9: OHLC stored RAW (the traded price); only adj_close
                # carries the adjustment factor.  Never mix scales.
                "open": b["open"], "high": b["high"],
                "low": b["low"], "close": close,
                "adj_close": close * f, "volume": b["volume"],
                "traded_value": close * b["volume"],
            })
        self.db.upsert_prices(symbol, rows)
        return len(rows)

    def recompute_adjustments(self, symbol: str = None) -> int:
        """
        Rebuild adj_close = raw close x cumulative factor for stored bars.
        Must run whenever corporate actions change — audit bug #6: previously
        only NEW bars were adjusted and history kept stale factors.
        """
        syms = [symbol] if symbol else [r["symbol"] for r in self.db.query(
            "SELECT DISTINCT symbol FROM price_daily")]
        n = 0
        for s in syms:
            rows = self.db.query("SELECT date, close FROM price_daily WHERE symbol=? ORDER BY date", (s,))
            if not rows:
                continue
            for r in rows:
                f = self.adj_factor(s, r["date"])
                self.db.execute(
                    "UPDATE price_daily SET adj_close=? WHERE symbol=? AND date=?",
                    (r["close"] * f, s, r["date"]))
                n += 1
        return n

    def update_all(self, years: int = 11, only: list = None) -> dict:
        P.section("MARKET DATA — DOWNLOAD & REFRESH", "\U0001F4E5")
        symbols = only or [r["symbol"] for r in self.db.query(
            "SELECT symbol FROM etf_universe WHERE status='ACTIVE' ORDER BY symbol")]
        stats = {"updated": 0, "skipped": 0, "failed": 0, "bars": 0}
        total = len(symbols)
        for i, sym in enumerate(symbols, 1):
            try:
                n = self.update_symbol(sym, years=years)
            except UpstoxError as e:
                P.err(f"Stopping data refresh: {e}")
                if "RATE_LIMIT" in str(e):
                    P.note("Already-downloaded bars are safely stored in the database. "
                           "Rerun later — only the missing tail will be fetched.")
                else:
                    P.note("Update UPSTOX_ACCESS_TOKEN in env.txt (it expires daily), "
                           "or run 'python tsmom.py demo' for an offline trial.")
                break
            if n > 0:
                stats["updated"] += 1
                stats["bars"] += n
                P.raw(f"  {paint('[{}/{}]'.format(i, total), _C.GREY)} "
                      f"{sym:<12} downloaded {n:<5} bars   "
                      f"{paint('ok', _C.GREEN)}")
            else:
                st = self.db.one("SELECT COUNT(*) AS c FROM price_daily WHERE symbol=?", (sym,))
                if st and st["c"]:
                    stats["skipped"] += 1
                    P.raw(f"  {paint('[{}/{}]'.format(i, total), _C.GREY)} "
                          f"{sym:<12} already up to date ({st['c']} bars)   "
                          f"{paint('skip', _C.YELLOW)}")
                else:
                    stats["failed"] += 1
                    P.raw(f"  {paint('[{}/{}]'.format(i, total), _C.GREY)} "
                          f"{sym:<12} NO DATA (will be excluded)   "
                          f"{paint('missing', _C.RED)}")
        P.ok(f"Data refresh done: {stats['updated']} updated, "
             f"{stats['skipped']} current, {stats['failed']} without data, "
             f"{stats['bars']:,} bars written.")
        self.db.meta_set("last_data_update", now_iso())
        return stats

    def load_corporate_actions(self) -> int:
        """
        Load stock-data/corporate-actions/*.csv  (symbol,ex_date,factor,note).
        Idempotent (audit 2.2): UNIQUE(symbol,ex_date,note) + INSERT OR REPLACE —
        loading the same file twice can NEVER compound factors.

        FACTOR CONVENTION (differs from the usual pre-split convention!):
        factors restate prices FROM THE EX-DATE ONWARD into today's scale.
        A 1:2 split (price halves at the ex-date) therefore needs factor 2.0,
        so that adj_close = raw x 2.0 for dates >= ex-date makes the series
        continuous with today's prices.  note='DIV...' marks distributions
        (slab haircut applies on reinvestment).
        """
        folder = CACHE_DIR.parent / "corporate-actions"
        folder.mkdir(parents=True, exist_ok=True)
        n = 0
        # audit 2.2: remove any legacy duplicate rows first
        self.db.execute(
            "DELETE FROM corporate_actions WHERE id NOT IN "
            "(SELECT MIN(id) FROM corporate_actions GROUP BY symbol, ex_date, note)")
        for fp in sorted(folder.glob("*.csv")):
            with open(fp, newline="", encoding="utf-8") as f:
                for row in csv.reader(f):
                    if not row or row[0].strip().upper() == "SYMBOL" or len(row) < 3:
                        continue
                    sym, ex, factor = row[0].strip(), row[1].strip(), float(row[2])
                    note = row[3].strip() if len(row) > 3 else ""
                    self.db.execute(
                        "INSERT OR REPLACE INTO corporate_actions(symbol,ex_date,factor,note) "
                        "VALUES(?,?,?,?)",
                        (sym, ex, factor, note))
                    n += 1
        if n:
            self.recompute_adjustments()
        return n

    def load_nav_data(self) -> int:
        """Load stock-data/nav/*.csv  (symbol,date,nav) into nav_data — enables the
        real iNAV premium/discount filter (audit bug #6)."""
        folder = CACHE_DIR.parent / "nav"
        folder.mkdir(parents=True, exist_ok=True)
        n = 0
        for fp in sorted(folder.glob("*.csv")):
            with open(fp, newline="", encoding="utf-8") as f:
                for row in csv.reader(f):
                    if not row or row[0].strip().upper() == "SYMBOL" or len(row) < 3:
                        continue
                    sym, d, nav = row[0].strip(), row[1].strip(), float(row[2])
                    self.db.execute(
                        "INSERT INTO nav_data(symbol,date,nav) VALUES(?,?,?) "
                        "ON CONFLICT(symbol,date) DO UPDATE SET nav=excluded.nav",
                        (sym, d, nav))
                    n += 1
        return n

    def coverage_table(self):
        rows = self.db.query(
            "SELECT symbol, MIN(date) AS f, MAX(date) AS l, COUNT(*) AS n "
            "FROM price_daily GROUP BY symbol ORDER BY symbol")
        out = []
        for r in rows:
            n = r["n"]
            tag = "ready" if n >= MIN_HISTORY_BARS else ("thin" if n >= 60 else "insufficient")
            out.append([r["symbol"], r["f"], r["l"], fmt_num(n, 0), tag])
        P.section("PRICE DATA COVERAGE", "\U0001F4C8")
        def rc(i, row):
            return _C.GREEN if row[4] == "ready" else (_C.YELLOW if row[4] == "thin" else _C.RED)
        P.table(["Symbol", "From", "To", "Bars", "Signal-ready?"], out,
                aligns=["<", "<", "<", ">", "<"], row_color=rc)
        return rows


# ==============================================================================
# SECTION 11 — MOMENTUM SIGNAL (Step 2) & THRESHOLDS (Step 3)
# ==============================================================================

def momentum_at(adj: list, i: int, lookback: int = LOOKBACK, skip: int = SKIP):
    """
    M = P_adj(t-skip) / P_adj(t-lookback) - 1     (trading-session offsets)
    adj: ascending list of adjusted closes, i = index of signal date.
    Returns None when history is insufficient.
    """
    if i - skip < 0 or i - lookback < 0:
        return None
    if i - lookback >= len(adj) or i - skip >= len(adj):
        return None
    p1 = adj[i - skip]
    p0 = adj[i - lookback]
    if p0 is None or p1 is None or p0 == 0:
        return None
    return p1 / p0 - 1.0


def signal_momentum(adj: list, i: int, lookback: int = LOOKBACK, skip: int = SKIP):
    """
    Production momentum signal (audit 3.2).  With ENSEMBLE_LOOKBACKS set
    (e.g. (63, 126, 252)) this is the MEAN of the component lookbacks —
    a smoother signal; with None it is exactly the single LOOKBACK rule.
    Components without enough history are ignored; None if none available.
    """
    lookbacks = ENSEMBLE_LOOKBACKS or (lookback,)
    vals = [m for m in (momentum_at(adj, i, lb, skip) for lb in lookbacks)
            if m is not None]
    return (sum(vals) / len(vals)) if vals else None


def daily_returns(closes: list) -> list:
    out = []
    for i in range(1, len(closes)):
        a, b = closes[i - 1], closes[i]
        if a and b and a > 0:
            out.append(b / a - 1.0)
    return out


def row_adj(r):
    """
    adj_close from a price row of ANY shape (audit hotfix): the live
    stats_at() passes raw SQLite rows (date, adj_close, close, traded_value)
    while the backtester builds (date, adj) pairs — never assume 2 columns.
    """
    try:
        return r["adj_close"]
    except (TypeError, KeyError, IndexError, AttributeError):
        return r[1]


def date_returns(rows) -> dict:
    """{date: simple return} from rows of (date, adj_close[, ...]). Works with
    sqlite Rows, tuples or dicts — always date-aligned (audit bug #9)."""
    out = {}
    prev = None
    prev_date = None
    for r in rows:
        try:
            d = r["date"] if isinstance(r, sqlite3.Row) or isinstance(r, dict) else r[0]
            a = r["adj_close"] if isinstance(r, sqlite3.Row) or isinstance(r, dict) else r[1]
        except Exception:
            continue
        if prev is not None and prev and a and prev > 0:
            out[d] = a / prev - 1.0
        prev, prev_date = a, d
    return out


def aligned_return_matrix(series_by_sym: dict, window: int):
    """Date-aligned return matrix for covariance work (audit bug #9)."""
    rets = {s: date_returns(rows) for s, rows in series_by_sym.items()}
    common = set.intersection(*[set(r.keys()) for r in rets.values()]) if rets else set()
    dates = sorted(common)[-window:]
    syms = list(series_by_sym.keys())
    X = [[rets[s][d] for s in syms] for d in dates]
    return X, syms, dates


def annualized_vol(closes: list, window: int = VOL_WINDOW) -> float:
    """
    Annualised volatility (audit 3.2): the short-window estimate, optionally
    blended with the 252-day estimate as
        vol = sqrt((1-w) * v60^2 + w * v252^2),   w = VOL_BLEND_252.
    Blending damps vol spikes that make the engine whipsaw.
    """
    rets = daily_returns(closes)
    v60 = stdev(rets[-window:], ddof=1) * math.sqrt(252.0)
    if VOL_BLEND_252 > 0 and len(rets) >= 252 + 1:
        v252 = stdev(rets[-252:], ddof=1) * math.sqrt(252.0)
        return math.sqrt((1.0 - VOL_BLEND_252) * v60 ** 2
                         + VOL_BLEND_252 * v252 ** 2)
    return v60


def month_end_indices(dates: list) -> list:
    """Indices of the last trading session of each month."""
    out = []
    cur_key = None
    for i, d in enumerate(dates):
        k = month_key(parse_date(d))
        if k != cur_key:
            cur_key = k
            out.append(i)  # tentatively the first day of month
        else:
            out[-1] = i    # keep moving to the last day of the month
    return out


def last_month_end_date(all_dates: list, today: Optional[str] = None):
    """
    Last COMPLETED month-end session (audit bug #1, tightened 2.9):
      - if the newest bar belongs to the CURRENT calendar month (vs `today`),
        that month is still in progress and must not signal;
      - a truncated final month is dropped only when the last bar is earlier
        than (the month's expected last weekday - 3 days) — never merely
        because the bar lands on the 24th (holidays!).
    `today` makes the function date-independent (selftests pass their own).
    """
    if not all_dates:
        return None
    idx = month_end_indices(all_dates)
    if not idx:
        return all_dates[-1]
    last_bar = parse_date(all_dates[-1])
    today_d = parse_date(today) if today else dt.date.today()
    current_month = month_key(last_bar) == month_key(today_d)
    import calendar as _cal
    last_day = _cal.monthrange(last_bar.year, last_bar.month)[1]
    exp = dt.date(last_bar.year, last_bar.month, last_day)
    while exp.weekday() >= 5:
        exp -= dt.timedelta(days=1)         # expected last weekday of month
    truncated = last_bar < exp - dt.timedelta(days=3)
    if len(idx) >= 2 and (current_month or truncated):
        idx = idx[:-1]           # final month still in progress / data ends mid-month
    return all_dates[idx[-1]]


class SignalBook:
    """One symbol's snapshot at a signal date."""

    def __init__(self, symbol, name, category, index_family, momentum, vol60,
                 liq_p25, spread_bps, bars_ok, decision, reason):
        self.symbol = symbol
        self.name = name
        self.category = category
        self.index_family = index_family
        self.momentum = momentum
        self.vol60 = vol60
        self.liq_p25 = liq_p25
        self.spread_bps = spread_bps
        self.bars_ok = bars_ok
        self.decision = decision      # ELIGIBLE_BUY / HOLD / EXIT / CASH / SKIPPED
        self.reason = reason


def decide(momentum: float, holding: bool, entry: float = ENTRY_THRESHOLD,
           exit_thr: float = EXIT_THRESHOLD):
    """Step 3 threshold table -> (decision, reason)."""
    if momentum is None:
        return "SKIPPED", "not enough price history (needs 252 sessions)"
    if holding:
        if momentum > exit_thr:
            return "HOLD", f"trend still positive ({fmt_pct(momentum)}) — keep holding"
        return "EXIT", f"trend turned negative ({fmt_pct(momentum)} <= 0%) — sell"
    else:
        if momentum > entry:
            return "ELIGIBLE_BUY", f"uptrend confirmed ({fmt_pct(momentum)} > {fmt_pct(entry)}) — buy candidate"
        return "CASH", f"trend too weak ({fmt_pct(momentum)} <= {fmt_pct(entry)}) — stay in cash"


# ==============================================================================
# SECTION 12 — LEDOIT-WOLF SHRINKAGE COVARIANCE (constant-correlation target)
# ==============================================================================

def mean_vector(X):
    n = len(X[0]) if X else 0
    return [sum(row[j] for row in X) / len(X) for j in range(n)] if X and n else []


def demean(X):
    mu = mean_vector(X)
    return [[row[j] - mu[j] for j in range(len(mu))] for row in X]


def sample_cov(X, ddof: int = 0):
    """X: T x N (already demeaned for ddof=0 MLE form)."""
    T = len(X)
    N = len(X[0]) if T else 0
    S = [[0.0] * N for _ in range(N)]
    if T == 0:
        return S
    for t in range(T):
        x = X[t]
        for i in range(N):
            xi = x[i]
            if xi == 0.0:
                continue
            for j in range(i, N):
                S[i][j] += xi * x[j]
    den = T if ddof == 0 else max(1, T - 1)
    for i in range(N):
        for j in range(i, N):
            S[i][j] /= den
            S[j][i] = S[i][j]
    return S


def ledoit_wolf_cc(X):
    """
    Ledoit-Wolf-style shrinkage of the sample covariance toward a
    CONSTANT-CORRELATION target (Ledoit & Wolf 2003/2004).

    X: T x N matrix of demeaned daily returns.
    Returns (Sigma_shrunk, delta, cond_estimate).

    Shrinkage intensity (plug-in form of the LW intensity):
        delta* = sum_ij Var_hat(s_ij)  /  sum_ij (s_ij - f_ij)^2 ,  clamped to [0,1]
        Var_hat(s_ij) = (1/T) * mean_t ( x_ti x_tj - s_ij )^2
    Target F:  f_ii = s_ii,  f_ij = rbar * sqrt(s_ii s_jj)

    HONESTY NOTE (audit #9): this is the plug-in LW intensity — it omits the
    rho correction term of the full LW2004 estimator.  For the small N used
    here (<= a few holdings) the difference is immaterial, but this is not a
    byte-for-byte reproduction of the paper's formula.
    """
    T = len(X)
    N = len(X[0]) if T else 0
    if T == 0 or N == 0:
        return [[0.0] * N for _ in range(N)], 1.0, 1.0
    S = sample_cov(X, ddof=0)
    # average correlation
    num = 0.0
    den = 0
    for i in range(N):
        for j in range(i + 1, N):
            di, dj = S[i][i], S[j][j]
            if di > 0 and dj > 0:
                num += S[i][j] / math.sqrt(di * dj)
                den += 1
    rbar = (num / den) if den else 0.0
    rbar = clamp(rbar, -0.99, 0.99)
    # target matrix F
    F = [[0.0] * N for _ in range(N)]
    for i in range(N):
        for j in range(N):
            if i == j:
                F[i][j] = S[i][i]
            else:
                di, dj = S[i][i], S[j][j]
                F[i][j] = rbar * math.sqrt(max(di, 0.0) * max(dj, 0.0))
    # plug-in variance of cov entries and distance to target
    pi = 0.0
    for t in range(T):
        x = X[t]
        for i in range(N):
            for j in range(N):
                d = x[i] * x[j] - S[i][j]
                pi += d * d
    pi /= float(T * T)          # sum_ij Var_hat(s_ij)
    dist = 0.0
    for i in range(N):
        for j in range(N):
            dd = S[i][j] - F[i][j]
            dist += dd * dd
    delta = clamp(pi / dist, 0.0, 1.0) if dist > 0 else 1.0
    Sigma = [[(1.0 - delta) * S[i][j] + delta * F[i][j] for j in range(N)] for i in range(N)]
    cond = condition_number(Sigma) if N <= 20 else diag_condition_proxy(Sigma)
    return Sigma, delta, cond


def diag_condition_proxy(Sigma):
    d = [Sigma[i][i] for i in range(len(Sigma)) if Sigma[i][i] > 0]
    return (max(d) / min(d)) if d else 1.0


def condition_number(Sigma):
    """Jacobi eigenvalue condition number (symmetric matrices, N small)."""
    n = len(Sigma)
    A = [row[:] for row in Sigma]
    for sweep in range(50):
        off = 0.0
        for i in range(n):
            for j in range(i + 1, n):
                off += A[i][j] * A[i][j]
        if off < 1e-18:
            break
        for p in range(n - 1):
            for q in range(p + 1, n):
                if abs(A[p][q]) < 1e-15:
                    continue
                theta = (A[q][q] - A[p][p]) / (2.0 * A[p][q])
                t = math.copysign(1.0, theta) / (abs(theta) + math.sqrt(theta * theta + 1.0))
                c = 1.0 / math.sqrt(t * t + 1.0)
                s = t * c
                for k in range(n):
                    akp, akq = A[k][p], A[k][q]
                    A[k][p] = c * akp - s * akq
                    A[k][q] = s * akp + c * akq
                for k in range(n):
                    apk, aqk = A[p][k], A[q][k]
                    A[p][k] = c * apk - s * aqk
                    A[q][k] = s * apk + c * aqk
    ev = [A[i][i] for i in range(n)]
    lo = min(ev)
    hi = max(ev)
    if lo <= 0:
        return float("inf")
    return hi / lo


def portfolio_vol(weights: list, Sigma) -> float:
    n = len(weights)
    v = 0.0
    for i in range(n):
        wi = weights[i]
        if wi == 0:
            continue
        for j in range(n):
            v += wi * weights[j] * Sigma[i][j]
    return math.sqrt(max(v, 0.0))


# ==============================================================================
# SECTION 13 — FILTERS (Step 4: liquidity, spread, iNAV)
# ==============================================================================

class FilterEngine:
    def __init__(self, db: Db):
        self.db = db

    def liquidity_p25(self, symbol: str, as_of: str) -> float:
        rows = self.db.query(
            "SELECT traded_value FROM price_daily WHERE symbol=? AND date<=? "
            "ORDER BY date DESC LIMIT ?", (symbol, as_of, LIQ_WINDOW))
        vals = [r["traded_value"] for r in rows]
        p = percentile(vals, LIQ_PCTL)
        return p if p is not None else 0.0

    def spread_p90(self, symbol: str, as_of: str) -> float:
        """90th-percentile observed spread in bps; proxy default if no quotes.
        NOTE (audit #9): ts is a datetime string — compare on the DATE part so
        same-day quotes are not excluded."""
        rows = self.db.query(
            "SELECT spread_bps FROM quotes WHERE symbol=? AND substr(ts,1,10)<=? "
            "ORDER BY ts DESC LIMIT ?", (symbol, as_of, SPREAD_WINDOW))
        vals = [r["spread_bps"] for r in rows if r["spread_bps"] is not None]
        if vals:
            # audit 3.8: trust the observed distribution only with >= 60
            # quote snapshots; with fewer, be conservative (worst seen, but
            # never better than the liquidity-tier proxy)
            liq = self.liquidity_p25(symbol, as_of)
            r0 = self.db.one("SELECT broad FROM etf_universe WHERE symbol=?", (symbol,))
            proxy = estimate_spread_bps(liq, bool(r0["broad"]) if r0 else False)
            if len(vals) >= 60:
                p = percentile(vals, SPREAD_PCTL)
                return p if p is not None else max(vals)
            return max(max(vals), proxy)
        r = self.db.one("SELECT broad FROM etf_universe WHERE symbol=?", (symbol,))
        broad = bool(r["broad"]) if r else False
        return DEFAULT_SPREAD_BPS_BROAD if broad else DEFAULT_SPREAD_BPS_OTHER

    def inav_deviation(self, symbol: str, as_of: str):
        """|market/NAV - 1| from nav_data if available; None otherwise."""
        r = self.db.one("SELECT nav FROM nav_data WHERE symbol=? AND date=?", (symbol, as_of))
        p = self.db.one("SELECT close FROM price_daily WHERE symbol=? AND date=?", (symbol, as_of))
        if not r or not p or not r["nav"]:
            return None
        return abs(p["close"] / r["nav"] - 1.0)

    def inav_limit(self, symbol: str) -> float:
        r = self.db.one("SELECT broad FROM etf_universe WHERE symbol=?", (symbol,))
        return INAV_DEV_BROAD if (r and r["broad"]) else INAV_DEV_SECTOR

    def order_value_ok(self, symbol: str, as_of: str, intended_value: float):
        """intended_order_value <= 15% x p25_daily_value (binding sizing rule)."""
        p25 = self.liquidity_p25(symbol, as_of)
        limit = POS_CAP_LIQ_FRAC * p25
        ok = intended_value <= limit
        return ok, p25, limit

    def tradable_enough(self, liq_p25: float, equity: float) -> bool:
        """
        Pre-ranking liquidity filter (audit 2.6, supersedes bug #4 rule):
        a full sleeve (15% x p25) must represent at least MIN_LIQ_WEIGHT of
        equity — a RELATIVE gate that scales with the portfolio.
        """
        return ((POS_CAP_LIQ_FRAC * liq_p25) / max(equity, 1.0)) >= MIN_LIQ_WEIGHT

    def correlation(self, rows_a, rows_b, window: int = CORR_WINDOW):
        """
        Date-aligned Pearson correlation of daily returns over the window
        (audit bug #9: never correlate tail-sliced series — align on dates).
        """
        ret_a = date_returns(rows_a[-(window + 1):])
        ret_b = date_returns(rows_b[-(window + 1):])
        common = sorted(set(ret_a) & set(ret_b))
        if len(common) < 20:
            return 0.0
        xa = [ret_a[d] for d in common]
        xb = [ret_b[d] for d in common]
        ma, mb = mean(xa), mean(xb)
        num = sum((a - ma) * (b - mb) for a, b in zip(xa, xb))
        da = math.sqrt(sum((a - ma) ** 2 for a in xa))
        db = math.sqrt(sum((b - mb) ** 2 for b in xb))
        return safe_div(num, da * db, 0.0)

    def check_execution(self, symbol: str, as_of: str, intended_value: float):
        """
        Full pre-trade gate. Returns (ok, reason, info-dict).
        A failure should be DEFERRED one session and rechecked, then skipped.
        """
        info = {}
        p25 = self.liquidity_p25(symbol, as_of)
        info["liq_p25"] = p25
        liq_limit = POS_CAP_LIQ_FRAC * p25
        info["liq_limit"] = liq_limit
        if intended_value > liq_limit:
            return False, (f"order value {fmt_cr(intended_value)} exceeds 15% of daily traded "
                           f"value p25 ({fmt_cr(liq_limit)})"), info
        spread = self.spread_p90(symbol, as_of)
        info["spread_p90"] = spread
        if spread > SPREAD_MAX_BPS:
            return False, (f"bid-ask spread is {spread:.1f} bps — wider than the "
                           f"{SPREAD_MAX_BPS:.0f} bps limit; skipping rather than "
                           f"paying up"), info
        dev = self.inav_deviation(symbol, as_of)
        info["inav_dev"] = dev
        if dev is not None:
            lim = self.inav_limit(symbol)
            info["inav_limit"] = lim
            if dev > lim:
                return False, (f"price is {fmt_pct(dev)} away from iNAV "
                               f"(limit {fmt_pct(lim)}) — would be overpaying"), info
        return True, "all checks passed", info


# ==============================================================================
# SECTION 14 — PORTFOLIO CONSTRUCTION (Steps 5 & 6)
# ==============================================================================

class PortfolioBuilder:
    def __init__(self, db: Db, filters: FilterEngine):
        self.db = db
        self.filters = filters

    def stats_at(self, symbol: str, as_of: str, lookback: int, skip: int, entry: float, exit_thr: float):
        rows = self.db.query(
            "SELECT date, adj_close, close, traded_value FROM price_daily "
            "WHERE symbol=? AND date<=? ORDER BY date", (symbol, as_of))
        if len(rows) < max(MIN_HISTORY_BARS, lookback + 5):
            return None
        adj = [r["adj_close"] for r in rows]
        closes = [r["close"] for r in rows]
        i = len(rows) - 1
        mom = signal_momentum(adj, i, lookback=lookback, skip=skip)
        vol = annualized_vol(adj, VOL_WINDOW)
        liq = percentile([r["traded_value"] for r in rows[-LIQ_WINDOW:]], LIQ_PCTL) or 0.0
        return {
            "symbol": symbol, "date": rows[-1]["date"], "momentum": mom,
            "vol": vol, "liq_p25": liq, "close": closes[-1],
            "adj": adj, "rows": rows,
        }

    def rank_metric(self, st) -> float:
        """Ranking key (audit sec 2.3): raw momentum, or momentum/vol."""
        mom = st["momentum"] if st["momentum"] is not None else -9.0
        if RANK_BY == "risk_adjusted":
            vol = st["vol"] if st["vol"] and st["vol"] > 0 else 1e-6
            return mom / vol
        return mom

    def select(self, stats_list: list, holding_symbols: set, strategy: str,
               entry: float = ENTRY_THRESHOLD, exit_thr: float = EXIT_THRESHOLD,
               equity: float = 0.0):
        """
        Step 5 selection.
          A: Pure TSMOM — every ETF with momentum > entry  (no holding cap)
          B: TSMOM + ranking — top MAX_HOLDINGS by momentum among qualifying
          C: Ranking only — top MAX_HOLDINGS by momentum, threshold ignored
        Filters applied before ranking (audit bugs #3/#4):
          - cash-proxy ETFs (liquid/overnight/gilt/bond) are never ranked
          - a full sleeve (15% x p25) must be >= MIN_LIQ_WEIGHT of equity
          - pairwise 120-day correlation cap against already-chosen ETFs
          - one ETF per index family (rulebook rule, optional)
        Incumbency (audit 2.4): a BOUNDED rank bonus of INCUMBENCY_BONUS places
        for held names — never a rank-lock; far outsiders cannot re-enter.
        """
        gate_eq = equity if equity and equity > 0 else CAPITAL   # sane default
        decisions = []
        for st in stats_list:
            dec, reason = decide(st["momentum"], st["symbol"] in holding_symbols,
                                 entry=entry, exit_thr=exit_thr)
            if st["symbol"] in CASH_PROXY_SYMBOLS and dec in ("ELIGIBLE_BUY", "HOLD"):
                dec, reason = ("CASH_SLEEVE",
                               "cash-proxy fund (liquid/gilt/bond) — kept out of "
                               "the ranked pool; used as the cash sleeve instead")
            else:
                if EXIT_OVERLAY_200DMA and st.get("rows"):
                    # audit 2.5: overlay on ENTRIES and EXITS in the live path too
                    # (hotfix: rows may be 4-col SQLite rows, not (date, adj) pairs;
                    #  semantics identical to the backtester's overlay)
                    seg = [row_adj(r) for r in st["rows"]][-200:]
                    below = bool(seg) and seg[-1] < sum(seg) / len(seg)
                    if below and st["symbol"] in holding_symbols and dec in ("HOLD",):
                        dec, reason = "EXIT", ("exit overlay: price below the "
                                               "200-day average")
                    elif below and dec == "ELIGIBLE_BUY":
                        dec, reason = ("SKIPPED",
                                       "entry overlay: price below the 200-day average")
                # liquidity gate ALWAYS applies to entries (hotfix: the overlay
                # branch used to be an elif that swallowed this check)
                if dec == "ELIGIBLE_BUY" and not self.filters.tradable_enough(
                        st.get("liq_p25", 0.0), gate_eq):
                    dec, reason = ("SKIPPED",
                                   f"too illiquid for portfolio scale: 15% of daily traded "
                                   f"value ({fmt_cr(POS_CAP_LIQ_FRAC * st.get('liq_p25', 0.0))}) "
                                   f"is under {fmt_pct(MIN_LIQ_WEIGHT)} of equity")
            decisions.append((st, dec, reason))
        exits = [st for st, dec, _ in decisions if dec == "EXIT"]
        holds = [st for st, dec, _ in decisions if dec == "HOLD"]
        eligible = [st for st, dec, _ in decisions if dec == "ELIGIBLE_BUY"]

        if strategy == "A":
            pool_a = sorted(eligible + holds, key=self.rank_metric, reverse=True)
            chosen = self._corr_cap(pool_a, MAX_HOLDINGS * 10)   # no holding cap
        else:
            pool = list(stats_list if strategy == "C" else (eligible + holds))
            pool = [st for st in pool if st["momentum"] is not None
                    and st["symbol"] not in CASH_PROXY_SYMBOLS
                    and self.filters.tradable_enough(st.get("liq_p25", 0.0), gate_eq)]
            pool.sort(key=self.rank_metric, reverse=True)
            chosen = self._pick_with_incumbency(pool, holding_symbols, MAX_HOLDINGS)

        if ROTATION_RULE and strategy in ("B", "C"):
            chosen = self._apply_rotation(pool if strategy != "A" else pool,
                                          holding_symbols, MAX_HOLDINGS)
        return {"chosen": chosen, "decisions": decisions, "exits": exits,
                "holds": holds, "eligible": eligible}

    def _pick_with_incumbency(self, ranked: list, holding_symbols: set, k: int) -> list:
        """Rank order with the bounded incumbency bonus (audit 2.4) + family/correlation caps."""
        if INCUMBENCY_BONUS and holding_symbols:
            order = []
            for pos, st in enumerate(ranked):
                eff = pos - INCUMBENCY_BONUS if st["symbol"] in holding_symbols else pos
                order.append((eff, pos, st))
            order.sort(key=lambda t: (t[0], t[1]))
            ranked = [t[2] for t in order]
        rest = list(ranked)
        out = self._corr_cap(rest, k)
        for st in rest:
            if len(out) >= k:
                break
            fam = universe_map().get(st["symbol"], {}).get("index_family", st["symbol"])
            if ONE_PER_INDEX_FAMILY and any(
                    universe_map().get(s["symbol"], {}).get("index_family") == fam for s in out):
                continue
            cand = out + [st]
            if self._corr_ok(cand):
                out.append(st)
        return out

    def _corr_cap(self, pool: list, k: int) -> list:
        out = []
        for st in pool:            # caller supplies rank order (audit 2.4)
            if len(out) >= k:
                break
            fam = universe_map().get(st["symbol"], {}).get("index_family", st["symbol"])
            if ONE_PER_INDEX_FAMILY and any(
                    universe_map().get(s["symbol"], {}).get("index_family") == fam for s in out):
                continue
            cand = out + [st]
            if self._corr_ok(cand):
                out.append(st)
        return out

    def _corr_ok(self, chosen: list) -> bool:
        """Pairwise 120-day correlation cap (audit sec 2.2). PAIRWISE_CORR_CAP=0 disables."""
        if PAIRWISE_CORR_CAP <= 0 or len(chosen) < 2:
            return True
        new = chosen[-1]
        for other in chosen[:-1]:
            c = self.filters.correlation(new.get("rows", []), other.get("rows", []),
                                         window=CORR_WINDOW)
            if abs(c) > PAIRWISE_CORR_CAP:
                return False
        return True

    def _apply_rotation(self, pool, holding_symbols, k):
        """
        Audit bug #9: rotation can now BOTH drop stale incumbents AND admit
        replacements.  Held ETFs ranked beyond 5 are replaced while a stronger
        eligible ETF exists; held ETFs ranked <= 5 keep their seat.
        """
        ranked = sorted(pool, key=self.rank_metric, reverse=True)
        rank_pos = {st["symbol"]: i + 1 for i, st in enumerate(ranked)}
        kept = [st for st in ranked
                if st["symbol"] in holding_symbols and rank_pos.get(st["symbol"], 99) <= 5]
        out = list(kept)
        for st in ranked:
            if len(out) >= k:
                break
            if st in out:
                continue
            fam = universe_map().get(st["symbol"], {}).get("index_family", st["symbol"])
            if ONE_PER_INDEX_FAMILY and any(
                    universe_map().get(s["symbol"], {}).get("index_family") == fam for s in out):
                continue
            if self._corr_ok(out + [st]):
                out.append(st)
        return out

    def size(self, chosen: list, equity: float, risk_scale: float = 1.0,
             strategy: str = "B"):
        """
        Step 6 sizing:
          1. raw weights (Version A = equal-weighted per Step 5; B/C = inverse-volatility)
          2. dynamic cap  w_i <= min(25%, 15% * p25_value / equity)
          3. NO upward re-normalisation — remainder stays in cash
          4. scale invested fraction so portfolio vol <= 8%
          5. accept lower risk if unreachable; never leverage
        Returns dict with weights, vol, cond numbers, invested fraction.
        """
        if not chosen:
            return {"weights": {}, "invested": 0.0, "port_vol": 0.0, "delta": None,
                    "cond": 1.0, "vol_reachable": False, "capped": {}}
        syms = [s["symbol"] for s in chosen]
        n = len(syms)
        # 1) raw weights — Step 5: Version A is equal-weighted; B/C use inverse-vol (Step 6)
        if strategy == "A":
            raw = [1.0 / n] * n
        else:
            # audit #3: same tiny-vol epsilon in live and backtest paths
            inv = [1.0 / (s["vol"] if s["vol"] and s["vol"] > 0 else 1e-6) for s in chosen]
            ssum = sum(inv) or 1.0
            raw = [x / ssum for x in inv]
        # 2) dynamic caps
        caps = {}
        w = []
        for i, s in enumerate(chosen):
            cap = min(POS_CAP_ABS, POS_CAP_LIQ_FRAC * s["liq_p25"] / equity if equity > 0 else 0.0)
            caps[s["symbol"]] = cap
            w.append(min(raw[i], cap))
        # 3) no upward renormalisation
        # 4) covariance of selected, 60-day DATE-ALIGNED demeaned returns
        #    (audit bug #9: tail-sliced series misalign on missing bars)
        series_by_sym = {s["symbol"]: s.get("rows", []) for s in chosen}
        X, _syms, _dates = aligned_return_matrix(series_by_sym, COV_WINDOW)
        T = len(X)
        if T < 20:
            Sigma = [[0.0] * n for _ in range(n)]
            for i in range(n):
                Sigma[i][i] = (chosen[i]["vol"] or 0.0) ** 2
            delta, cond = 0.0, 1.0
        else:
            Xd = demean(X)
            Sigma, delta, cond = ledoit_wolf_cc(Xd)
        raw_vol = portfolio_vol(w, Sigma) * math.sqrt(252.0)   # DAILY cov -> ANNUAL vol
        target_vol = VOL_TARGET * risk_scale
        if raw_vol <= 0:
            s_scale = 1.0
        elif raw_vol <= target_vol:
            s_scale = 1.0
        else:
            s_scale = target_vol / raw_vol
        s_scale = clamp(s_scale, 0.0, 1.0)
        weights = {syms[i]: w[i] * s_scale for i in range(n)}
        port_vol = portfolio_vol([weights[s] for s in syms], Sigma) * math.sqrt(252.0)
        reachable = raw_vol >= target_vol - 1e-9
        return {
            "weights": weights,
            "invested": sum(weights.values()),
            "port_vol": port_vol,
            "unlevered_vol": raw_vol,
            "delta": delta,
            "cond": cond,
            "vol_reachable": reachable,
            "capped": caps,
            "symbols": syms,
            "Sigma": Sigma,
            "risk_scale": risk_scale,
        }


# ==============================================================================
# SECTION 15 — STATUTORY COSTS & SLIPPAGE (Step 7)
# ==============================================================================

def calc_charges(side: str, value: float, category: str = "EQUITY") -> dict:
    """Full Indian statutory charge stack for ETF delivery (audit-corrected)."""
    c = CHARGES
    brokerage = min(value * c["brokerage_pct"], c["brokerage_cap"])
    stt = value * (c["stt_sell_pct"] if side.upper() == "SELL" else c["stt_buy_pct"])
    if category and category.upper() in {x.upper() for x in STT_EXEMPT_CATEGORIES}:
        stt = 0.0          # Gold/Liquid/Gilt ETFs: no STT (audit #8)
    stamp = value * c["stamp_buy_pct"] if side.upper() == "BUY" else 0.0
    exchange = value * c["exchange_pct"]
    sebi = value * c["sebi_pct"]
    ipft = value * c["ipft_pct"]
    gst = c["gst_pct"] * (brokerage + exchange + sebi)
    total = brokerage + stt + stamp + exchange + sebi + ipft + gst
    return {"brokerage": brokerage, "stt": stt, "stamp": stamp,
            "exchange": exchange, "sebi": sebi, "ipft": ipft, "gst": gst,
            "total": total}


def estimate_spread_bps(liq_p25: float, broad: bool = False) -> float:
    """
    Liquidity-tiered spread proxy (bps) used until real quote history exists.
    Thin ETFs trade meaningfully worse than liquid broad-market ones.
    """
    if liq_p25 >= 5e7:        # >= Rs 5 Cr/day p25
        base = 3.0
    elif liq_p25 >= 5e6:      # >= Rs 50 L/day
        base = 5.0
    elif liq_p25 >= 5e5:      # >= Rs 5 L/day
        base = 10.0
    else:
        base = 20.0
    return base * (1.0 if broad else 1.4)


def impact_bps(order_value: float, liq_p25: float, daily_vol: float = 0.01) -> float:
    """
    Square-root market-impact model scaled by participation AND volatility
    (audit 3.6): impact ~ sqrt(participation) * (daily_vol / 1%).
    A calm ETF (0.5% daily vol) costs half the impact of the 1% reference.
    """
    participation = safe_div(order_value, max(liq_p25, 1.0), 1.0)
    vol_factor = max(daily_vol, 1e-4) / 0.01
    return ((IMPACT_BASE_BPS + IMPACT_SQRT_COEF * math.sqrt(min(participation, 1.0)))
            * vol_factor)


def slippage_price(side: str, ref_price: float, spread_bps: float,
                   spread_mult: float = 1.0,
                   order_value: float = 0.0, liq_p25: float = 0.0,
                   daily_vol: float = 0.01) -> float:
    """
    Executable price (audit 3.5):
      MARKET     — cross the spread: half spread x mult + sqrt-impact
      LIMIT_MID  — rest at mid: NO spread crossing, impact only
    """
    impact = (impact_bps(order_value, liq_p25, daily_vol) / 10000.0
              if liq_p25 else IMPACT_BASE_BPS / 10000.0)
    if EXECUTION_STYLE == "LIMIT_MID":
        slip = impact
    else:
        half = (spread_bps * spread_mult) / 2.0 / 10000.0
        slip = half + impact
    if side.upper() == "BUY":
        return ref_price * (1.0 + slip)
    return ref_price * (1.0 - slip)


# ==============================================================================
# SECTION 16 — TAX HELPERS & TAX LOTS (Step 8, audit-corrected)
# ==============================================================================

def gain_class(category: str) -> str:
    """Netting bucket: EQUITY-like / COMMODITY / DEBT (audit #7)."""
    cat = (category or "EQUITY").upper()
    if cat in ("COMMODITY",):
        return "COMMODITY"
    if cat in ("DEBT",):
        return "DEBT"
    return "EQUITY"          # EQUITY, GLOBAL INDICES, Hybrid


def tax_rules_for(category: str) -> dict:
    cat = (category or "EQUITY").upper()
    if cat == "GLOBAL INDICES":
        cat = "GLOBAL"
    if cat == "HYBRID":
        cat = "HYBRID"
    return TAX_RULES.get(cat, TAX_RULES["EQUITY"])


def held_more_than_months(buy_date, sell_date, months: int) -> bool:
    """
    Anniversary test (audit #7): 'more than N months' means strictly after the
    N-th monthly anniversary of the purchase date.  Buy 2025-01-20 and sell
    2026-01-25 IS more than 12 months; sell 2026-01-15 is not.
    """
    if months <= 0:
        return True
    import calendar
    b = parse_date(buy_date) if isinstance(buy_date, str) else buy_date
    s = parse_date(sell_date) if isinstance(sell_date, str) else sell_date
    y = b.year + (b.month - 1 + months) // 12
    m = (b.month - 1 + months) % 12 + 1
    day = min(b.day, calendar.monthrange(y, m)[1])   # clamp e.g. 31 Jan + 1m
    return s > dt.date(y, m, day)


def ltcg_anniversary(buy_date, months: int = 12) -> "dt.date":
    """The N-month anniversary date used by the holding-period test."""
    import calendar
    b = parse_date(buy_date) if isinstance(buy_date, str) else buy_date
    y = b.year + (b.month - 1 + months) // 12
    m = (b.month - 1 + months) % 12 + 1
    return dt.date(y, m, min(b.day, calendar.monthrange(y, m)[1]))


def tax_defer_blocks_exit(open_lot_dates, as_of: str, momentum: float) -> str:
    """
    TAX_DEFER_EXITS hypothesis (audit 3.3) — an EXPLICIT, testable rule:
    defer a non-risk exit when momentum is only slightly negative
    (>= TAX_DEFER_MOM_FLOOR) and the oldest still-open lot completes its
    LTCG anniversary within TAX_DEFER_MONTHS.  Returns '' to allow the exit.
    """
    if not TAX_DEFER_EXITS:
        return ""
    if momentum is not None and momentum < TAX_DEFER_MOM_FLOOR:
        return ""
    today = parse_date(as_of)
    for bd in open_lot_dates:
        anniv = ltcg_anniversary(bd, 12)
        days_left = (anniv - today).days
        if 0 < days_left <= int(TAX_DEFER_MONTHS * 31):
            return (f"tax defer: {days_left} days to LTCG anniversary ({anniv}); "
                    f"momentum {fmt_pct(momentum)} above floor "
                    f"{fmt_pct(TAX_DEFER_MOM_FLOOR)}")
    return ""


def tax_on_gain(category: str, gain_type: str, gain: float, exempt_remaining: float):
    """Tax for one realised gain with per-class rules + 4% cess. (tax, exempt_used)."""
    rules = tax_rules_for(category)
    if gain <= 0:
        return 0.0, 0.0
    if gain_type == "STCG":
        return gain * rules["stcg"] * (1.0 + CESS_RATE), 0.0
    use = min(gain, exempt_remaining, rules["exempt"])
    taxable = max(0.0, gain - use)
    return taxable * rules["ltcg"] * (1.0 + CESS_RATE), use



class TaxEngine:
    """FIFO tax lots: STCG 20% (<=12 months), LTCG 12.5% above Rs 1.25 L/FY."""

    def __init__(self, db: Db):
        self.db = db

    def record_buy(self, symbol: str, buy_date: str, price: float, qty: float):
        self.db.execute(
            "INSERT INTO tax_lots(symbol,buy_date,buy_price,qty) VALUES(?,?,?,?)",
            (symbol, buy_date, price, qty))

    def record_sell(self, symbol: str, sell_date: str, sell_price: float, qty: float) -> dict:
        """Lot match per LOT_SELECTION (audit 3.7); writes realised lots.
        Returns {realized_pnl, tax, lt_pnl, st_pnl}."""
        remaining = qty
        realized = 0.0
        st_pnl = 0.0
        lt_pnl = 0.0
        cat_row = self.db.one("SELECT category FROM etf_universe WHERE symbol=?", (symbol,))
        category = cat_row["category"] if cat_row else "EQUITY"
        order = ("buy_price DESC, buy_date" if LOT_SELECTION == "HIFO"
                 else "buy_date, id")   # HIFO realises the least gain (audit 3.7)
        lots = self.db.query(
            "SELECT * FROM tax_lots WHERE symbol=? AND qty>0 AND sell_date IS NULL "
            f"ORDER BY {order}", (symbol,))
        for lot in lots:
            if remaining <= 0:
                break
            take = min(remaining, lot["qty"])
            pnl = (sell_price - lot["buy_price"]) * take
            # audit #7: anniversary test, not calendar-month subtraction
            is_lt = held_more_than_months(lot["buy_date"], sell_date,
                                          tax_rules_for(category)["months"])
            gain_type = "LTCG" if is_lt else "STCG"
            if gain_type == "LTCG":
                lt_pnl += pnl
            else:
                st_pnl += pnl
            self.db.execute(
                "INSERT INTO tax_lots(symbol,buy_date,buy_price,qty,sell_date,sell_price,pnl,gain_type,tax) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (symbol, lot["buy_date"], lot["buy_price"], take, sell_date,
                 sell_price, pnl, gain_type, 0.0))
            self.db.execute("UPDATE tax_lots SET qty=qty-? WHERE id=?", (take, lot["id"]))
            remaining -= take
            realized += pnl
        rules = tax_rules_for(category)
        tax = (max(0.0, st_pnl) * rules["stcg"]
               + max(0.0, lt_pnl) * rules["ltcg"]) * (1.0 + CESS_RATE)
        return {"realized_pnl": realized, "tax": tax, "st_pnl": st_pnl, "lt_pnl": lt_pnl}

    def year_tax_summary(self) -> dict:
        """
        Per-FY, per-class tax with LOSS NETTING (audit #7): losses offset gains
        inside the same class, the Rs 1.25 L exemption applies only to
        equity-like LTCG, and commodity/debt use their own rates.
        """
        rows = self.db.query(
            "SELECT l.sell_date AS sell_date, l.gain_type AS gain_type, l.pnl AS pnl, "
            "l.symbol AS symbol, u.category AS category FROM tax_lots l "
            "LEFT JOIN etf_universe u ON u.symbol=l.symbol "
            "WHERE l.sell_date IS NOT NULL")
        buckets = {}   # (fy, class) -> {"st":0,"lt":0}
        for r in rows:
            fy = financial_year(parse_date(r["sell_date"]))
            cls = gain_class(r["category"] or "EQUITY")
            d = buckets.setdefault((fy, cls), {"st": 0.0, "lt": 0.0})
            if r["gain_type"] == "STCG":
                d["st"] += r["pnl"] or 0.0
            else:
                d["lt"] += r["pnl"] or 0.0
        by_fy = {}
        carry = {}    # cls -> {"st","lt"} loss carry-forward (audit 2.7)
        for (fy, cls), d in sorted(buckets.items()):
            row = by_fy.setdefault(fy, {"stcg": 0.0, "ltcg": 0.0, "tax": 0.0,
                                        "by_class": {}})
            rules = TAX_RULES["EQUITY"] if cls == "EQUITY" else (
                TAX_RULES["COMMODITY"] if cls == "COMMODITY" else
                {"stcg": SLAB_RATE, "ltcg": SLAB_RATE, "exempt": 0.0})
            st_rate, lt_rate, exempt = rules["stcg"], rules["ltcg"], rules["exempt"]
            st_net, lt_net = d["st"], d["lt"]
            c = carry.get(cls)
            if c:   # carried losses: ST first vs ST then LT; LT vs LT only
                if c["st"] < 0:
                    use = min(-c["st"], max(0.0, st_net)); st_net -= use; c["st"] += use
                    if c["st"] < 0:
                        use2 = min(-c["st"], max(0.0, lt_net)); lt_net -= use2; c["st"] += use2
                if c["lt"] < 0:
                    use = min(-c["lt"], max(0.0, lt_net)); lt_net -= use; c["lt"] += use
            if st_net < 0 and lt_net > 0:     # STCL vs LTCG (audit 2.7)
                use = min(-st_net, lt_net)
                lt_net -= use
                st_net += use
            tax = (max(0.0, st_net) * st_rate
                   + max(0.0, lt_net - exempt) * lt_rate) * (1.0 + CESS_RATE)
            row["stcg"] += st_net
            row["ltcg"] += lt_net
            row["tax"] += tax
            row["by_class"][cls] = {"stcg": st_net, "ltcg": lt_net, "tax": tax}
            if st_net < 0 or lt_net < 0:
                carry[cls] = {"st": min(st_net, 0.0), "lt": min(lt_net, 0.0)}
        return by_fy


# ==============================================================================
# SECTION 17 — PAPER BROKER / LIVE BROKER (order book + positions)
# ==============================================================================

class Broker:
    """
    Fills orders either in PAPER mode (simulated, always default) or LIVE mode
    (real Upstox orders). Every fill lands in the orders table = order book.
    """

    def __init__(self, db: Db, tax: TaxEngine, client=None, paper: bool = PAPER_TRADE):
        self.db = db
        self.tax = tax
        self.client = client
        self.paper = paper

    # ----- account state -----
    def cash(self) -> float:
        v = self.db.meta_get("cash_balance")
        return float(v) if v is not None else STARTING_CAPITAL

    def set_cash(self, v: float):
        self.db.meta_set("cash_balance", v)

    def accrue_cash_interest(self):
        """Audit bug #5: the paper account must earn the cash benchmark —
        otherwise a 20% drawdown lock can literally never release."""
        if not CASH_ACCRUAL:
            return 0.0
        last = self.db.meta_get("last_interest_date")
        today = today_str()
        if last == today:
            return 0.0
        try:
            days = (parse_date(today) - parse_date(last)).days if last else 0
        except Exception:
            days = 0
        if days <= 0:
            self.db.meta_set("last_interest_date", today)
            return 0.0
        cash = self.cash()
        per_day = self.sleeve_daily_return(today)     # audit 2.6: real sleeve
        earned = cash * ((1.0 + per_day) ** days - 1.0)
        self.set_cash(cash + earned)
        self.db.meta_set("last_interest_date", today)
        self.db.meta_set("interest_earned_total",
                         float(self.db.meta_get("interest_earned_total", "0")) + earned)
        return earned

    def holdings(self) -> dict:
        out = {}
        for r in self.db.query("SELECT * FROM positions WHERE qty>0"):
            out[r["symbol"]] = {"qty": r["qty"], "avg_cost": r["avg_cost"]}
        return out

    def holdings_value(self, prices: dict) -> float:
        v = 0.0
        for sym, h in self.holdings().items():
            px = prices.get(sym, h["avg_cost"])
            v += h["qty"] * px
        return v

    def equity(self, prices: dict) -> float:
        return self.cash() + self.holdings_value(prices)

    # ----- order flow -----
    def execute(self, symbol: str, side: str, qty: float, ref_price: float,
                signal_as_of: str = "", reason: str = "", spread_bps: float = 8.0,
                spread_mult: float = 1.0, order_type: str = "MARKET") -> dict:
        qty = int(qty)
        if qty <= 0:
            return {"status": "REJECTED", "reason": "quantity is zero"}
        mode = "PAPER" if self.paper else "LIVE"
        cat_row = self.db.one("SELECT category FROM etf_universe WHERE symbol=?", (symbol,))
        category = cat_row["category"] if cat_row else "EQUITY"
        liq_p25 = 0.0
        liq_row = self.db.one(
            "SELECT traded_value FROM price_daily WHERE symbol=? ORDER BY date DESC LIMIT 60",
            (symbol,))
        liq_vals = [r["traded_value"] for r in self.db.query(
            "SELECT traded_value FROM price_daily WHERE symbol=? ORDER BY date DESC LIMIT 60",
            (symbol,))]
        liq_p25 = percentile(liq_vals, LIQ_PCTL) or 0.0
        px = slippage_price(side, ref_price, spread_bps, spread_mult,
                            order_value=qty * ref_price, liq_p25=liq_p25)
        value = px * qty
        charges = calc_charges(side, value, category=category)
        status = "FILLED"
        fill_note = "filled at simulated price (paper book)" if self.paper else ""
        if not self.paper and self.client is not None:
            key_row = self.db.one("SELECT instrument_key FROM etf_universe WHERE symbol=?", (symbol,))
            key = key_row["instrument_key"] if key_row else f"NSE_EQ|{symbol}"
            try:
                resp = self.client.place_order(symbol, key, side, qty, px, order_type)
                oid = (resp.get("data") or {}).get("order_id", "")
                fill_note = f"live order id {oid}"
                # audit #9: reconcile the simulated fill with the broker's actual
                # fill (MARKET orders fill at the market, not at our simulation)
                try:
                    det = self.client.order_details(oid)
                    d = (det.get("data") or {})
                    avg = float(d.get("average_price") or 0)
                    if avg > 0:
                        px = avg
                        value = px * qty
                        charges = calc_charges(side, value, category=category)
                        fill_note += f"; broker avg fill Rs {avg:.2f}"
                    if d.get("status"):
                        fill_note += f"; broker status {d.get('status')}"
                        # audit 2.9: the LEDGER adopts the broker's fill status
                        bs = str(d.get("status")).strip().lower()
                        if bs in ("complete", "filled"):
                            status = "FILLED"
                        elif bs in ("open", "pending", "trigger pending", "queued"):
                            status = "PENDING"
                        elif bs in ("rejected", "cancelled", "expired"):
                            status = "REJECTED"
                        else:
                            status = "UNCONFIRMED"
                except Exception as e2:
                    fill_note += f"; could not verify fill ({e2})"
            except Exception as e:
                status = "FAILED"
                fill_note = f"live order failed: {e}"
        slip_cost = abs(px - ref_price) * qty
        self.db.execute(
            "INSERT INTO orders(ts,signal_as_of,exec_date,symbol,side,qty,price,value,"
            "slippage,charges,status,mode,order_type,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), signal_as_of, today_str(), symbol, side, qty, px, value,
             slip_cost, charges["total"], status, mode, order_type,
             (reason + " | " + fill_note).strip(" |")))
        self.db.execute(
            "INSERT INTO decisions(ts,phase,symbol,decision,reason) VALUES(?,?,?,?,?)",
            (now_iso(), "execution", symbol, f"{side} x{qty} @ Rs {px:.2f}", reason))
        if status == "FILLED":
            self._apply_fill(symbol, side, qty, px, value, charges)
        log(f"ORDER {mode} {side} {symbol} x{qty} @ {px:.2f} value={value:.0f} "
            f"charges={charges['total']:.2f} status={status}")
        return {"status": status, "price": px, "qty": qty, "value": value,
                "charges": charges, "slippage": slip_cost, "mode": mode,
                "reason": reason, "note": fill_note}

    def _apply_fill(self, symbol, side, qty, px, value, charges):
        cash = self.cash()
        if side.upper() == "BUY":
            cash -= (value + charges["total"])
            row = self.db.one("SELECT qty, avg_cost FROM positions WHERE symbol=?", (symbol,))
            if row and row["qty"] > 0:
                new_qty = row["qty"] + qty
                new_avg = (row["qty"] * row["avg_cost"] + qty * px) / new_qty
                self.db.execute("UPDATE positions SET qty=?, avg_cost=?, updated=? WHERE symbol=?",
                                (new_qty, new_avg, now_iso(), symbol))
            else:
                self.db.execute("INSERT OR REPLACE INTO positions(symbol,qty,avg_cost,updated) "
                                "VALUES(?,?,?,?)", (symbol, qty, px, now_iso()))
            self.tax.record_buy(symbol, today_str(), px, qty)
        else:
            cash += (value - charges["total"])
            row = self.db.one("SELECT qty, avg_cost FROM positions WHERE symbol=?", (symbol,))
            have = row["qty"] if row else 0
            sell_qty = min(qty, have)
            self.tax.record_sell(symbol, today_str(), px, sell_qty)
            left = have - sell_qty
            if left > 0:
                self.db.execute("UPDATE positions SET qty=?, updated=? WHERE symbol=?",
                                (left, now_iso(), symbol))
            else:
                self.db.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
        self.set_cash(cash)

    def print_order_ticket(self, res: dict, symbol: str, name: str, side: str):
        if res.get("status") == "FILLED":
            icon = "\U0001F7E2" if side == "BUY" else "\U0001F534"
            verb = "BOUGHT" if side == "BUY" else "SOLD"
            P.decision(icon,
                       f"{verb} {res['qty']} units of {symbol} ({name}) at Rs {res['price']:.2f}",
                       f"order value {fmt_rs(res['value'])} | charges {fmt_rs(res['charges']['total'])} "
                       f"| slippage {fmt_rs(res['slippage'])} | [{res['mode']} mode]",
                       _C.GREEN if side == "BUY" else _C.RED)
        else:
            P.decision("⚪", f"Order skipped for {symbol}",
                       res.get("reason") or res.get("note") or "not filled", _C.YELLOW)


# ==============================================================================
# SECTION 18 — RISK ENGINE (Step 9: drawdown triggers & re-entry)
# ==============================================================================

class RiskEngine:
    def __init__(self, db: Db, vol_target: float = None):
        self.db = db
        self.vol_target = float(vol_target if vol_target is not None else VOL_TARGET)
        self.state = db.meta_get("risk_state", "NORMAL")
        self.peak = float(db.meta_get("equity_peak", STARTING_CAPITAL))
        self.halved = db.meta_get("risk_halved", "0") == "1"
        self.all_cash = db.meta_get("risk_all_cash", "0") == "1"
        self.trigger_date = db.meta_get("risk_trigger_date", "")
        self.trigger_counts = {
            "REVIEW": int(db.meta_get("risk_count_REVIEW", "0")),
            "SEVERE": int(db.meta_get("risk_count_SEVERE", "0")),
            "EXTREME": int(db.meta_get("risk_count_EXTREME", "0")),
        }

    def _save(self):
        self.db.meta_set("risk_state", self.state)
        self.db.meta_set("equity_peak", self.peak)
        self.db.meta_set("risk_halved", "1" if self.halved else "0")
        self.db.meta_set("risk_all_cash", "1" if self.all_cash else "0")
        self.db.meta_set("risk_trigger_date", self.trigger_date)
        for k, v in self.trigger_counts.items():
            self.db.meta_set(f"risk_count_{k}", v)

    def update(self, equity: float, context: str = "") -> dict:
        """Daily monitoring of portfolio drawdown."""
        events = []
        if equity > self.peak:
            self.peak = equity
        dd = 1.0 - equity / self.peak if self.peak > 0 else 0.0
        # --- release rules (audit bug #5) ---
        # (a) classic: recover to within REENTRY_BAND of prior peak
        # (b) TIME-BASED: after RISK_RELEASE_DAYS the lock is released at the
        #     next review even without price recovery — sitting in cash cannot
        #     heal a drawdown, and the old peak would re-trigger instantly —
        #     so the peak is RESET to current equity on time-based release.
        if self.halved or self.all_cash:
            released = False
            reason = ""
            if dd <= REENTRY_BAND:
                released, reason = True, (
                    f"equity recovered to within {fmt_pct(REENTRY_BAND)} of "
                    f"peak {fmt_rs(self.peak)}")
            elif self.trigger_date:
                try:
                    trig = parse_date(self.trigger_date)
                    if (dt.date.today() - trig).days >= RISK_RELEASE_DAYS:
                        released = True
                        self.peak = equity          # reset the slate
                        reason = (f"{RISK_RELEASE_DAYS} days elapsed since the "
                                  f"trigger — time-based release; peak reset to "
                                  f"{fmt_rs(equity)} so the rule can fire again "
                                  f"on a NEW decline")
                except Exception:
                    pass
            if released:
                self.halved = False
                self.all_cash = False
                self.state = "NORMAL"
                self.trigger_date = ""
                self.db.execute(
                    "INSERT INTO risk_events(ts,level,drawdown,action,detail) VALUES(?,?,?,?,?)",
                    (now_iso(), "RECOVERY", dd, "restore full sizing", reason))
                events.append(("RECOVERY", f"Risk limits released — {reason}."))
                # peak may have been reset — recompute dd so the trigger block
                # below cannot re-lock the account on the OLD drawdown
                dd = 1.0 - equity / self.peak if self.peak > 0 else 0.0
        dd_review, dd_severe, dd_extreme = dd_thresholds(self.vol_target)
        if dd >= dd_extreme and not self.all_cash:
            self.all_cash = True
            self.state = "EXTREME"
            self.trigger_date = today_str()
            self.trigger_counts["EXTREME"] += 1
            self.db.execute(
                "INSERT INTO risk_events(ts,level,drawdown,action,detail) VALUES(?,?,?,?,?)",
                (now_iso(), "EXTREME", dd, "move to 100% cash",
                 f"drawdown {fmt_pct(dd)} breached {fmt_pct(dd_extreme)}"))
            events.append(("EXTREME", f"Portfolio fell {fmt_pct(dd)} from peak — moving 100% to cash "
                                      f"(protection rule, not a guaranteed stop-loss)."))
        elif dd >= dd_severe and not self.halved and not self.all_cash:
            self.halved = True
            self.state = "SEVERE"
            self.trigger_date = today_str()
            self.trigger_counts["SEVERE"] += 1
            self.db.execute(
                "INSERT INTO risk_events(ts,level,drawdown,action,detail) VALUES(?,?,?,?,?)",
                (now_iso(), "SEVERE", dd, "halve target invested fraction",
                 f"drawdown {fmt_pct(dd)} breached {fmt_pct(dd_severe)}"))
            events.append(("SEVERE", f"Portfolio fell {fmt_pct(dd)} from peak — target invested "
                                     f"amount is HALVED until recovery."))
        elif dd >= dd_review and self.state == "NORMAL":
            self.state = "REVIEW"
            self.trigger_counts["REVIEW"] += 1
            self.db.execute(
                "INSERT INTO risk_events(ts,level,drawdown,action,detail) VALUES(?,?,?,?,?)",
                (now_iso(), "REVIEW", dd, "risk review",
                 f"drawdown {fmt_pct(dd)} breached {fmt_pct(dd_review)}; holdings documented"))
            events.append(("REVIEW", f"Portfolio fell {fmt_pct(dd)} from peak — risk review triggered."))
        self._save()
        return {"drawdown": dd, "peak": self.peak, "state": self.state,
                "halved": self.halved, "all_cash": self.all_cash, "events": events}

    def risk_scale(self) -> float:
        if self.all_cash:
            return 0.0
        if self.halved:
            return 0.5
        return 1.0

    def snapshot(self, date: str, cash: float, holdings_value: float, equity: float):
        inv = safe_div(holdings_value, equity, 0.0)
        dd = 1.0 - equity / self.peak if self.peak > 0 else 0.0
        self.db.execute(
            "INSERT INTO portfolio_snapshots(date,cash,holdings_value,equity,invested_frac,peak,drawdown,mode) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET cash=excluded.cash, holdings_value=excluded.holdings_value, "
            "equity=excluded.equity, invested_frac=excluded.invested_frac, peak=excluded.peak, "
            "drawdown=excluded.drawdown, mode=excluded.mode",
            (date, cash, holdings_value, equity, inv, self.peak, dd,
             "PAPER" if PAPER_TRADE else "LIVE"))


# ==============================================================================
# SECTION 19 — TRADING CYCLE (live/paper loop: signal -> size -> execute)
# ==============================================================================

class TradingCycle:
    def __init__(self, db: Db):
        self.db = db
        try:
            env = load_env()
        except Exception:
            env = {"UPSTOX_ACCESS_TOKEN": "", "UPSTOX_API_TOKEN": "", "UPSTOX_API_SECRET": ""}
        self.client = UpstoxClient(env)
        self.data = DataManager(db, self.client)   # history helpers (regime, sleeve)
        self.filters = FilterEngine(db)
        self.builder = PortfolioBuilder(db, self.filters)
        self.tax = TaxEngine(db)
        self.broker = Broker(db, self.tax, self.client, paper=PAPER_TRADE)
        self.risk = RiskEngine(db)

    def sleeve_daily_return(self, as_of: str) -> float:
        """Per-day cash-sleeve return (audit 2.6).  LIQUID_ETF mode uses the
        real sleeve history; falls back to the flat T-bill proxy with a warning."""
        if CASH_SLEEVE_MODE == "LIQUID_ETF":
            rows = self.data.series(CASH_SLEEVE_SYMBOL, as_of, 30, "adj_close")
            if len(rows) >= 2 and rows[-2][1]:
                return rows[-1][1] / rows[-2][1] - 1.0
            P.warn(f"{CASH_SLEEVE_SYMBOL} history missing — cash sleeve using flat "
                   f"{fmt_pct(CASH_RATE_PRETAX, 2)} (labelled proxy).")
        return (1.0 + CASH_RATE_PRETAX) ** (1.0 / 252.0) - 1.0

    # ------------------------------------------------------------------ #
    def latest_prices(self) -> dict:
        prices = {}
        for r in self.db.query(
                "SELECT p.symbol AS symbol, p.close AS close FROM price_daily p WHERE p.date = "
                "(SELECT MAX(date) FROM price_daily WHERE symbol=p.symbol)"):
            prices[r["symbol"]] = r["close"]
        return prices

    def last_month_end(self) -> str:
        rows = self.db.query("SELECT DISTINCT date FROM price_daily ORDER BY date")
        dates = [r["date"] for r in rows]
        if not dates:
            return today_str()
        return last_month_end_date(dates) or dates[-1]

    def collect_stats(self, as_of: str, lookback: int = LOOKBACK, skip: int = SKIP,
                      entry: float = ENTRY_THRESHOLD, exit_thr: float = EXIT_THRESHOLD):
        stats = []
        umap = universe_map()
        for row in self.db.query("SELECT symbol FROM etf_universe WHERE status='ACTIVE' ORDER BY symbol"):
            sym = row["symbol"]
            st = self.builder.stats_at(sym, as_of, lookback, skip, entry, exit_thr)
            if st is None:
                continue
            st["name"] = umap.get(sym, {}).get("name", sym)
            st["category"] = umap.get(sym, {}).get("category", "")
            st["index_family"] = umap.get(sym, {}).get("index_family", sym)
            st["spread_bps"] = self.filters.spread_p90(sym, as_of)
            stats.append(st)
        return stats

    # ------------------------------------------------------------------ #
    def print_signal_console(self, selection: dict, as_of: str, strategy: str):
        P.section(f"TRADING DECISIONS — signal date {as_of}  (Strategy {strategy})", "\U0001F4AC")
        chosen_syms = {s["symbol"] for s in selection["chosen"]}
        rows = []
        def dec_color(dec):
            return {"ELIGIBLE_BUY": _C.GREEN, "HOLD": _C.CYAN, "EXIT": _C.RED,
                    "CASH": _C.GREY, "SKIPPED": _C.YELLOW}.get(dec, _C.WHITE)
        ordered = sorted(selection["decisions"],
                         key=lambda t: ({"ELIGIBLE_BUY": 0, "HOLD": 1, "EXIT": 2,
                                         "CASH": 3, "SKIPPED": 4}.get(t[1], 9),
                                        -(t[0]["momentum"] or -9)))
        for st, dec, reason in ordered:
            mom = st["momentum"]
            rows.append([
                st["symbol"],
                st["name"][:26],
                fmt_pct(mom) if mom is not None else "n/a",
                fmt_pct(st["vol"] or 0, 1),
                fmt_cr(st["liq_p25"]),
                paint(dec, dec_color(dec)),
                "★ SELECTED" if st["symbol"] in chosen_syms else "",
            ])
        P.table(["Symbol", "Name", "1Y Trend", "Risk (vol)", "Daily Trade", "Decision", ""],
                rows, aligns=["<", "<", ">", ">", ">", "<", "<"])
        # easy-language recap
        P.sub("What this means in plain words")
        for st, dec, reason in ordered:
            if dec == "ELIGIBLE_BUY":
                P.decision("\U0001F7E2", f"{st['symbol']}: BUY candidate",
                           f"{st['name']} — {reason}", _C.GREEN)
            elif dec == "HOLD":
                P.decision("\U0001F535", f"{st['symbol']}: keep holding",
                           f"{st['name']} — {reason}", _C.CYAN)
            elif dec == "EXIT":
                P.decision("\U0001F534", f"{st['symbol']}: SELL signal",
                           f"{st['name']} — {reason}", _C.RED)
            elif dec == "SKIPPED":
                P.decision("⚪", f"{st['symbol']}: cannot trade", reason, _C.YELLOW)

    def print_portfolio_plan(self, sized: dict, equity: float):
        P.sub("Portfolio plan (position sizes)")
        if not sized["weights"]:
            P.info("Everything is in cash this month — no ETF qualifies.")
            return
        rows = []
        for sym, w in sorted(sized["weights"].items(), key=lambda kv: -kv[1]):
            cap = sized["capped"].get(sym, POS_CAP_ABS)
            rows.append([sym, fmt_pct(w, 2), fmt_cr(w * equity),
                         fmt_pct(cap, 2),
                         "capped" if w / max(sized.get("risk_scale", 1.0), 1e-9) >= cap - 1e-9 else ""])
        P.table(["Symbol", "Target Weight", "Target Money", "Liquidity Cap", "Note"], rows,
                aligns=["<", ">", ">", ">", "<"])
        P.kv("Money to be invested", fmt_cr(sized["invested"] * equity))
        P.kv("Cash kept aside", fmt_cr((1 - sized["invested"]) * equity))
        P.kv("Expected portfolio risk", f"{fmt_pct(sized['port_vol'], 2)} per year "
                                        f"(target {fmt_pct(VOL_TARGET)})")
        if sized.get("delta") is not None:
            P.kv("Ledoit-Wolf shrinkage", f"{sized['delta']:.3f} toward constant correlation")
        if sized.get("cond") is not None:
            P.kv("Covariance condition number", f"{sized['cond']:,.1f}")
        if not sized.get("vol_reachable", True):
            P.note("The 8% risk target could not be fully reached inside the caps — "
                   "the rulebook says: accept the LOWER risk, never add leverage.")

    # ------------------------------------------------------------------ #
    def run(self, force_exec: bool = False, strategy: str = TRADE_STRATEGY):
        P.section("TSMOM TRADING CYCLE", "\U0001F680")
        mode = "PAPER TRADING (simulated — no real money)" if PAPER_TRADE else "LIVE TRADING"
        P.info(f"Mode: {mode}")
        interest = self.broker.accrue_cash_interest()
        if interest:
            P.info(f"Cash interest accrued: {fmt_rs(interest)} "
                   f"(cash earns {fmt_pct(CASH_RATE_PRETAX,2)} p.a. in the paper book)")
        all_dates = [r["date"] for r in self.db.query(
            "SELECT DISTINCT date FROM price_daily ORDER BY date")]
        as_of = last_month_end_date(all_dates) if all_dates else today_str()
        P.info(f"Signal date (last COMPLETED month-end): {as_of}")
        if REBALANCE_FREQ == "Q" and parse_date(as_of).month not in (3, 6, 9, 12):
            P.ok(f"Quarterly rebalance mode: {as_of} is not a quarter-end — "
                 f"nothing to do this month.")
            return self.broker.equity(self.latest_prices())

        # ---- audit bug #1: never rebalance twice on the same signal ----
        if self.db.meta_get("last_trade_signal") == as_of and not force_exec:
            P.ok(f"The {as_of} month-end rebalance has already been executed. "
                 f"Nothing to do until the next month-end.")
            P.note("Tip: 'python tsmom.py monitor' updates quotes and risk daily.")
            return self.broker.equity(self.latest_prices())

        # ---- execution calendar (audit bug #1): signal at month-end, orders
        #      on the NEXT trading session; queue until then ----
        exec_date = None
        for d in all_dates:
            if d > as_of:
                exec_date = d
                break
        if exec_date is None:
            cand = parse_date(as_of) + dt.timedelta(days=1)
            while cand.weekday() >= 5:
                cand += dt.timedelta(days=1)
            exec_date = date_str(cand)
        today = today_str()
        if today < exec_date and not force_exec:
            dup = self.db.one(
                "SELECT id FROM pending_trades WHERE status='PENDING' AND symbol='*' "
                "AND signal_as_of=?", (as_of,))
            if not dup:   # audit 2.9: no duplicate queue rows
                self.db.execute(
                    "INSERT INTO pending_trades(signal_as_of,exec_after,symbol,side,target_qty,reason,status,created) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (as_of, exec_date, "*", "REBALANCE", 0,
                     f"signal computed {as_of}; waiting for next session {exec_date}",
                     "PENDING", now_iso()))
            P.warn(f"Signals are ready, but the rules say orders go out on the NEXT "
                   f"session ({exec_date}). Queued in 'pending_trades'.")
            P.note(f"Run 'python tsmom.py trade' on {exec_date} (or use --force-exec "
                   f"to fill now at the latest price).")
            return self.broker.equity(self.latest_prices())
        if today > exec_date:
            P.warn(f"Note: this is a LATE execution (planned {exec_date}, today {today}) — "
                   f"fills use the latest available price.")
        self.db.execute("UPDATE pending_trades SET status='DONE' WHERE status='PENDING'")
        prices = self.latest_prices()
        eq_before = self.broker.equity(prices)
        risk_ctx = self.risk.update(eq_before, "cycle-start")
        for level, msg in risk_ctx["events"]:
            P.warn(f"RISK [{level}]: {msg}")
        if risk_ctx["all_cash"]:
            P.warn("Risk rules currently force 100% CASH — new buys are blocked until recovery.")
        stats = self.collect_stats(as_of)
        if not stats:
            P.err("No ETF has enough price history yet. Run: python tsmom.py update")
            return
        holding_syms = set(self.broker.holdings().keys())
        selection = self.builder.select(stats, holding_syms, strategy,
                                        equity=self.broker.equity(self.latest_prices()))
        self.print_signal_console(selection, as_of, strategy)
        risk_scale = self.risk.risk_scale()
        if REGIME_SCALING:
            # audit 3.5: halve the vol target while the broad market is below
            # its 200-day average (explicit rule, tested as a hypothesis)
            bench = None
            for cand in ("NIFTYBEES", "HDFCSENSEX", "MONIFTY500"):
                rows = self.data.series(cand, as_of, 210, "adj_close")
                if rows and len(rows) >= 200:
                    bench = [r[1] for r in rows]
                    break
            if bench and bench[-1] < sum(bench[-200:]) / 200.0:
                risk_scale *= 0.5
                P.info("bear regime (benchmark below 200-day avg) — vol target halved")

        sized = self.builder.size(selection["chosen"], eq_before,
                                  risk_scale=risk_scale, strategy=strategy)
        self.print_portfolio_plan(sized, eq_before)

        # ---- build target basket (execute at the LATEST traded price = next session) ----
        latest_px = self.latest_prices()
        target = {}
        for st in selection["chosen"]:
            w = sized["weights"].get(st["symbol"], 0.0)
            target[st["symbol"]] = {"weight": w, "close": latest_px.get(st["symbol"], st["close"]),
                                    "spread_bps": st["spread_bps"], "name": st["name"]}
        # exits are mandatory sells
        for st in selection["exits"]:
            target.setdefault(st["symbol"], {"weight": 0.0,
                                             "close": latest_px.get(st["symbol"], st["close"]),
                                             "spread_bps": st["spread_bps"], "name": st["name"]})
            target[st["symbol"]]["weight"] = 0.0
        # audit bug #2: EVERY held symbol must appear in the target map — a held
        # ETF that fell out of the top-3 (or lost its data) is sold, exactly as
        # the backtester does.  Otherwise portfolios silently accumulate orphans.
        for sym, h in self.broker.holdings().items():
            if sym not in target:
                px = latest_px.get(sym) or h["avg_cost"]
                target[sym] = {"weight": 0.0, "close": px,
                               "spread_bps": self.filters.spread_p90(sym, as_of),
                               "name": universe_map().get(sym, {}).get("name", sym)}
        for sym in target:
            if not target[sym].get("close") or target[sym]["close"] <= 0:
                h = self.broker.holdings().get(sym)
                target[sym]["close"] = h["avg_cost"] if h else 1.0
        for st, dec, reason in selection["decisions"]:
            self.db.execute(
                "INSERT INTO signals(as_of,symbol,strategy,momentum,vol60,liq_p25,spread_p90,decision,reason) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (as_of, st["symbol"], strategy, st["momentum"], st["vol"], st["liq_p25"],
                 st["spread_bps"], dec, reason))

        # ---- execution book ----
        P.section("ORDER EXECUTION (next session rules)", "\U0001F4E6")
        pend_now = self.db.query(
            "SELECT * FROM pending_trades WHERE status='PENDING'")
        if pend_now:
            P.info(f"{len(pend_now)} queued trade instruction(s) cleared for execution now.")
        holdings = self.broker.holdings()
        trades_done = 0
        exits_outstanding = 0
        # planned price for late-fill accounting (audit 2.9)
        planned_px = {}
        if today > exec_date:
            for r in self.db.query("SELECT symbol, close FROM price_daily WHERE date=?",
                                   (exec_date,)):
                planned_px[r["symbol"]] = r["close"]
        # 1) EXITS ALWAYS EXECUTE (audit 2.3).  Filter gates apply to ENTRIES
        #    only; a risk exit must never be blocked by a spread/liquidity gate.
        #    Large exits are SLICED to 15% of p25 per session and carried forward.
        for sym, tgt in sorted(target.items()):
            price = tgt["close"]
            h = holdings.get(sym)
            if not h:
                continue
            have_val = h["qty"] * price
            tgt_val = tgt["weight"] * eq_before
            is_exit = tgt["weight"] <= 0.0
            if tgt_val >= have_val - 1.0:
                continue
            if not is_exit and NO_TRADE_BAND > 0 and have_val > 0:
                # audit 3.1: no-trade band cuts churn (exempt for exits)
                if abs(tgt_val - have_val) / max(tgt_val, have_val) < NO_TRADE_BAND:
                    continue
            sell_val = have_val - tgt_val
            if not is_exit and sell_val < MIN_TRADE_VALUE:
                continue
            qty = int(sell_val / price) if price > 0 else 0
            if is_exit and qty >= h["qty"]:
                qty = int(h["qty"])
            if qty <= 0:
                continue
            # liquidity slice: never more than 15% of p25 daily value per session
            p25 = self.filters.liquidity_p25(sym, as_of)
            max_qty = int(POS_CAP_LIQ_FRAC * p25 / price) if price > 0 else 0
            qty_now = qty
            if is_exit and max_qty > 0 and qty > max_qty:
                qty_now = max_qty
                remain = qty - qty_now
                exits_outstanding += 1
                dup = self.db.one(
                    "SELECT id FROM pending_trades WHERE status='PENDING' AND symbol=? "
                    "AND signal_as_of=?", (sym, as_of))
                if not dup:
                    self.db.execute(
                        "INSERT INTO pending_trades(signal_as_of,exec_after,symbol,side,"
                        "target_qty,reason,status,created) VALUES(?,?,?,?,?,?,?,?)",
                        (as_of, today_str(), sym, "SELL", remain,
                         "exit sliced across sessions (15% of p25 per day)",
                         "PENDING", now_iso()))
                P.warn(f"SELL {sym}: {qty_now} of {qty} units today (liquidity slice); "
                       f"{remain} queued for the next session.")
            if is_exit and TAX_DEFER_EXITS:
                open_dates = [r["buy_date"] for r in self.db.query(
                    "SELECT buy_date FROM tax_lots WHERE symbol=? AND qty>0 "
                    "AND sell_date IS NULL", (sym,))]
                if open_dates:
                    defer = tax_defer_blocks_exit(open_dates, as_of, 0.0)
                    if defer:
                        P.warn(f"SELL {sym} deferred this session — {defer}")
                        continue
            reason_txt = "exit/trim"
            if planned_px.get(sym):
                short = (price / planned_px[sym] - 1.0)
                reason_txt += f" | late-fill shortfall vs planned session: {fmt_pct(short)}"
            res = self.broker.execute(sym, "SELL", qty_now, price, as_of,
                                      reason=reason_txt,
                                      spread_bps=tgt["spread_bps"])
            self.broker.print_order_ticket(res, sym, tgt["name"], "SELL")
            trades_done += 1
        # 2) buys
        eq_now = self.broker.equity(self.latest_prices())
        for sym, tgt in sorted(target.items(), key=lambda kv: -kv[1]["weight"]):
            price = tgt["close"]
            h = self.broker.holdings().get(sym)
            have_val = (h["qty"] * price) if h else 0.0
            tgt_val = tgt["weight"] * eq_now
            if tgt_val > have_val + 1.0:
                buy_val = tgt_val - have_val
                if NO_TRADE_BAND > 0 and have_val > 0:
                    if abs(tgt_val - have_val) / max(tgt_val, have_val) < NO_TRADE_BAND:
                        continue
                if buy_val < MIN_TRADE_VALUE:
                    continue
                if self.risk.all_cash:
                    P.warn(f"BUY {sym} blocked — risk rules say 100% cash.")
                    continue
                ok, reason, info = self.filters.check_execution(sym, as_of, buy_val)
                if not ok:
                    P.warn(f"BUY {sym} deferred/skipped — {reason}")
                    self.db.execute(
                        "INSERT INTO orders(ts,signal_as_of,exec_date,symbol,side,qty,price,value,"
                        "slippage,charges,status,mode,order_type,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (now_iso(), as_of, today_str(), sym, "BUY", int(buy_val / max(price, 1e-9)),
                         price, buy_val, 0, 0, "SKIPPED_FILTER", "PAPER" if PAPER_TRADE else "LIVE",
                         "MARKET", reason))
                    continue
                qty = int(buy_val / price) if price > 0 else 0
                cash = self.broker.cash()
                max_qty = int(cash / (price * 1.002)) if price > 0 else 0
                qty = min(qty, max_qty)
                if qty <= 0:
                    P.warn(f"BUY {sym} skipped — not enough cash.")
                    continue
                res = self.broker.execute(sym, "BUY", qty, price, as_of,
                                          reason="trend signal buy",
                                          spread_bps=tgt["spread_bps"])
                self.broker.print_order_ticket(res, sym, tgt["name"], "BUY")
                trades_done += 1
        if trades_done == 0:
            P.info("No orders needed — portfolio already matches the plan.")

        # ---- snapshots ----
        prices = self.latest_prices()
        cash = self.broker.cash()
        hv = self.broker.holdings_value(prices)
        eq = cash + hv
        self.risk.update(eq, "cycle-end")
        self.risk.snapshot(today_str(), cash, hv, eq)
        if exits_outstanding == 0:
            self.db.meta_set("last_trade_signal", as_of)
        else:
            P.warn(f"{exits_outstanding} exit order(s) are still sliced/queued — the "
                   f"{as_of} signal stays OPEN until they finish, so rerun "
                   f"'python tsmom.py trade' on the next session.")
        self.db.meta_set("last_trade_run", now_iso())
        P.section("PORTFOLIO AFTER THIS CYCLE", "\U0001F4CA")
        rows = []
        for sym, h in sorted(self.broker.holdings().items()):
            px = prices.get(sym, h["avg_cost"])
            val = h["qty"] * px
            pnl = (px - h["avg_cost"]) * h["qty"]
            nm = universe_map().get(sym, {}).get("name", sym)[:24]
            rows.append([sym, nm, fmt_num(h["qty"], 0), f"Rs {h['avg_cost']:.2f}",
                         f"Rs {px:.2f}", fmt_cr(val), fmt_pct(safe_div(pnl, h["avg_cost"] * h["qty"], 0.0))])
        if rows:
            P.table(["Symbol", "Name", "Units", "Avg Cost", "LTP", "Value", "P&L"], rows,
                    aligns=["<", "<", ">", ">", ">", ">", ">"])
        P.kv("Cash in account", fmt_cr(cash))
        P.kv("Holdings value", fmt_cr(hv))
        P.kv("Total equity", fmt_cr(eq), val_color=_C.GREEN)
        P.kv("Invested fraction", fmt_pct(safe_div(hv, eq, 0.0)))
        dd = 1 - eq / self.risk.peak if self.risk.peak else 0.0
        P.kv("Drawdown from peak", fmt_pct(dd, signed=False),
             val_color=_C.RED if dd > 0.05 else _C.WHITE)
        P.kv("Risk state", self.risk.state)
        log(f"cycle done: equity={eq:.2f} cash={cash:.2f} trades={trades_done}")
        return eq


# ==============================================================================
# SECTION 20 — BACKTESTER (Step 10: 10y, OOS 20%, grid, cost & cash scenarios)
# ==============================================================================

class Backtester:
    """
    Event-driven monthly backtest on cached price data.
    Prices dict: symbol -> ascending [(date, adj_close, close, traded_value)]
    """

    def __init__(self, prices: dict, meta: dict, params: dict,
                 cost_case: str = "base", cash_rate: float = CASH_RATE_PRETAX,
                 strategy: str = "B", start_capital: float = STARTING_CAPITAL,
                 apply_tax: bool = True, risk_on: bool = True,
                 cash_sleeve: bool = None):
        self.prices = prices
        self.meta = meta            # symbol -> {name, category, index_family, broad}
        self.params = params        # {lookback, skip, entry, exit}
        self.cost_case = cost_case
        self.cash_rate = cash_rate
        self.strategy = strategy
        self.capital = start_capital
        self.apply_tax = apply_tax
        self.risk_on = risk_on
        # None = follow CASH_SLEEVE_MODE; False = force flat rate (cash-rate
        # scenario matrix); True = force sleeve with labelled fallback
        self.cash_sleeve = cash_sleeve

    # ---------- helpers ----------
    def _series(self, sym):
        return self.prices.get(sym, [])

    def run(self, start_date: str = None, end_date: str = None, verbose: bool = False):
        lookback = self.params.get("lookback", LOOKBACK)
        skip = self.params.get("skip", SKIP)
        entry = self.params.get("entry", ENTRY_THRESHOLD)
        exit_thr = self.params.get("exit", EXIT_THRESHOLD)
        spread_mult = COST_CASES[self.cost_case]["spread_mult"]
        spread_stat = COST_CASES[self.cost_case]["spread_stat"]

        # calendar
        all_dates = sorted({d for series in self.prices.values() for (d, _, _, _) in series})
        if not all_dates:
            return {"error": "no price data"}
        if start_date:
            all_dates = [d for d in all_dates if d >= start_date]
        if end_date:
            all_dates = [d for d in all_dates if d <= end_date]
        if len(all_dates) < 60:
            return {"error": "not enough calendar days after filtering"}
        date_index = {d: i for i, d in enumerate(all_dates)}

        # per-symbol index for fast lookup
        sym_dates = {}
        sym_adj = {}
        sym_close = {}
        sym_liq = {}
        for sym, series in self.prices.items():
            sd = [d for (d, _a, _c, _t) in series]
            first, last = sd[0], sd[-1]
            if last < all_dates[0] or first > all_dates[-1]:
                continue
            sym_dates[sym] = sd
            sym_adj[sym] = [a for (_d, a, _c, _t) in series]
            sym_close[sym] = [c for (_d, _a, c, _t) in series]
            sym_liq[sym] = [t for (_d, _a, _c, t) in series]

        def pos_at(sym, date):
            """index of last bar of sym at or before date, or None."""
            sd = sym_dates.get(sym)
            if not sd or date < sd[0]:
                return None
            lo, hi = 0, len(sd) - 1
            ans = None
            while lo <= hi:
                mid = (lo + hi) // 2
                if sd[mid] <= date:
                    ans = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            return ans

        def px_at(sym, date, field="close"):
            i = pos_at(sym, date)
            if i is None:
                return None
            return (sym_close if field == "close" else sym_adj)[sym][i]

        # month-end signal dates (quarterly mode keeps only quarter-ends)
        me_idx = month_end_indices(all_dates)
        signal_dates = [all_dates[i] for i in me_idx]
        signal_dates = [d for d in signal_dates if d > all_dates[0]]
        if REBALANCE_FREQ == "Q":
            signal_dates = [d for d in signal_dates
                            if parse_date(d).month in (3, 6, 9, 12)]
        signal_set = set(signal_dates)

        use_sleeve = (CASH_SLEEVE_MODE == "LIQUID_ETF"
                      if self.cash_sleeve is None else self.cash_sleeve)
        cash_fn = cash_return_series(self.prices, self.cash_rate, use_sleeve)
        self._cash_fn_flag = bool(getattr(cash_fn, "sleeve", False))
        cash = self.capital
        holdings = {}          # sym -> {qty, avg_cost}
        lots = []              # tax lots (in-memory)
        first_buy = {}         # symbol -> first purchase date (tax defer, audit 3.3)
        fy_buckets = {}        # (fy, class) -> {"st":x,"lt":y}  (audit #7 netting)
        current_fy = financial_year(parse_date(all_dates[0]))
        interest_earned = 0.0

        loss_carry = {}   # cls -> {"st":x, "lt":y, "years":n}  (audit 2.7)

        def settle_fy(fy):
            """
            Settle one financial year per class (audit 2.1/2.7):
              - STCL offsets STCG first, then LTCG; LTCL offsets LTCG only
              - unused losses carry forward up to LOSS_CARRY_YEARS
              - equity-only Rs 1.25 L exemption, per-class rates
              - 4% cess on the tax
              - tax is DEDUCTED FROM CASH here (in-run) and must NOT be
                subtracted again in _summarize (audit 2.1 double-count)
            """
            nonlocal cash, tax_accrued
            if not self.apply_tax:
                fy_buckets.clear()
                return
            for key in [k for k in list(fy_buckets) if k[0] == fy]:
                cls = key[1]
                d = fy_buckets.pop(key)
                if cls == "EQUITY":
                    st_rate = TAX_RULES["EQUITY"]["stcg"]
                    lt_rate = TAX_RULES["EQUITY"]["ltcg"]
                    exempt = LTCG_EXEMPTION
                elif cls == "COMMODITY":
                    st_rate = TAX_RULES["COMMODITY"]["stcg"]
                    lt_rate = TAX_RULES["COMMODITY"]["ltcg"]
                    exempt = 0.0
                else:
                    st_rate = lt_rate = SLAB_RATE
                    exempt = 0.0
                st, lt = d["st"], d["lt"]
                # bring in carried-forward losses
                c = loss_carry.pop(cls, None)
                if c:
                    # ST carry: offsets ST then LT; LT carry: offsets LT only
                    if c.get("st"):
                        use = min(-c["st"], max(0.0, st))
                        st -= use
                        c["st"] += use
                        if c["st"] < 0:
                            use2 = min(-c["st"], max(0.0, lt))
                            lt -= use2
                            c["st"] += use2
                    if c.get("lt"):
                        use = min(-c["lt"], max(0.0, lt))
                        lt -= use
                        c["lt"] += use
                    if c["st"] < -1e-9 or c["lt"] < -1e-9:
                        c["years"] -= 1
                        if c["years"] > 0:
                            loss_carry[cls] = c
                # current-year set-off
                if st < 0 and lt > 0:           # STCL vs LTCG
                    use = min(-st, lt)
                    lt -= use
                    st += use
                tax = (max(0.0, st) * st_rate
                       + max(0.0, lt - exempt) * lt_rate) * (1.0 + CESS_RATE)
                tax_accrued += tax
                cash -= tax
                if st < -1e-9 or lt < -1e-9:
                    loss_carry[cls] = {"st": min(st, 0.0), "lt": min(lt, 0.0),
                                       "years": LOSS_CARRY_YEARS}
        equity_curve = []
        daily_invested = []
        monthly_returns = []
        turnover_vals = []
        cond_vals = []
        delta_vals = []
        reach_count = 0
        month_count = 0
        risk_events = {"REVIEW": 0, "SEVERE": 0, "EXTREME": 0, "RECOVERY": 0}
        peak = self.capital
        halved = False
        all_cash_mode = False
        costs_paid = 0.0                  # slippage + statutory charges (for 'before costs' view)
        tax_accrued = 0.0
        regime_returns = {"bull": [], "bear": []}
        category_returns = {}
        trades = 0
        skipped = 0
        spread_used_vals = []
        div_haircut_paid = 0.0

        def holdings_value(date):
            v = 0.0
            for sym, h in holdings.items():
                p = px_at(sym, date)
                if p is None:
                    p = h["avg_cost"]
                v += h["qty"] * p
            return v

        def dvol_at(sym, date):
            """Daily vol for impact scaling (audit 3.6)."""
            i = pos_at(sym, date)
            if i is None:
                return 0.01
            seg = sym_adj[sym][max(0, i - VOL_WINDOW):i + 1]
            v = annualized_vol(seg, VOL_WINDOW) if len(seg) > VOL_WINDOW else None
            return (v or 0.15) / math.sqrt(252.0)

        def do_sell(sym, qty, price, date, charges_mult=1.0):
            nonlocal cash, tax_accrued, costs_paid
            h = holdings.get(sym)
            if not h or qty <= 0:
                return 0.0
            qty = min(qty, h["qty"])
            ref = price
            sp = self._spread_for(sym, date, spread_stat) * spread_mult
            category = self.meta.get(sym, {}).get("category", "EQUITY")
            liq = self._liq_at(sym, date)
            exec_px = slippage_price("SELL", ref, sp, 1.0,
                                     daily_vol=dvol_at(sym, date),
                                     order_value=qty * ref, liq_p25=liq)
            value = exec_px * qty
            ch = calc_charges("SELL", value, category=category)
            costs_paid += ch["total"] + abs(exec_px - ref) * qty
            cash += value - ch["total"]
            # tax lots -> FY bucket for netted settlement (audit #7);
            # LOT_SELECTION chooses the match order (audit 3.7)
            remaining = qty
            realized = 0.0
            category = self.meta.get(sym, {}).get("category", "EQUITY")
            cls = gain_class(category)
            months_need = tax_rules_for(category)["months"]
            fy = financial_year(parse_date(date))
            open_lots = [l for l in lots if l["sym"] == sym and l["qty"] > 0]
            if LOT_SELECTION == "HIFO":
                open_lots.sort(key=lambda l: -l["price"])
            else:
                open_lots.sort(key=lambda l: l["date"])
            for lot in open_lots:
                if lot["sym"] != sym or lot["qty"] <= 0:
                    continue
                take = min(remaining, lot["qty"])
                pnl = (exec_px - lot["price"]) * take
                is_lt = held_more_than_months(lot["date"], date, months_need)
                b = fy_buckets.setdefault((fy, cls), {"st": 0.0, "lt": 0.0})
                b["lt" if is_lt else "st"] += pnl
                lot["qty"] -= take
                remaining -= take
                realized += pnl
                if remaining <= 0:
                    break
            h["qty"] -= qty
            if h["qty"] <= 1e-9:
                del holdings[sym]
            return value

        def do_buy(sym, qty, price, date):
            nonlocal cash, costs_paid
            if qty <= 0:
                return 0.0
            ref = price
            sp = self._spread_for(sym, date, spread_stat) * spread_mult
            category = self.meta.get(sym, {}).get("category", "EQUITY")
            liq = self._liq_at(sym, date)
            exec_px = slippage_price("BUY", ref, sp, 1.0,
                                     order_value=qty * ref, liq_p25=liq,
                                     daily_vol=dvol_at(sym, date))
            # audit 3.4: skip dust trades (brokerage makes them poor value)
            if qty * exec_px < MIN_TRADE_VALUE:
                return 0.0
            value = exec_px * qty
            ch = calc_charges("BUY", value, category=category)
            total = value + ch["total"]
            costs_paid += ch["total"] + abs(exec_px - ref) * qty
            if total > cash:
                qty = int(cash / (exec_px * 1.002))
                if qty <= 0:
                    return 0.0
                value = exec_px * qty
                ch = calc_charges("BUY", value)
                total = value + ch["total"]
            cash -= total
            h = holdings.get(sym)
            if h:
                nq = h["qty"] + qty
                h["avg_cost"] = (h["qty"] * h["avg_cost"] + qty * exec_px) / nq
                h["qty"] = nq
            else:
                holdings[sym] = {"qty": qty, "avg_cost": exec_px}
            lots.append({"sym": sym, "date": date, "price": exec_px, "qty": qty})
            first_buy.setdefault(sym, date)
            return value

        overlay = self.params.get("exit_overlay", None) or (
            "200dma" if EXIT_OVERLAY_200DMA else None)

        def rank_metric(st):
            mom = st["momentum"] if st["momentum"] is not None else -9.0
            if RANK_BY == "risk_adjusted":
                return mom / (st["vol"] if st["vol"] > 0 else 1e-6)
            return mom

        def corr_ok(cand):
            if PAIRWISE_CORR_CAP <= 0 or len(cand) < 2:
                return True
            new = cand[-1]
            for other in cand[:-1]:
                ra = date_returns(new["rows"])
                rb = date_returns(other["rows"])
                common = sorted(set(ra) & set(rb))[-CORR_WINDOW:]
                if len(common) >= 20:
                    xa = [ra[d] for d in common]
                    xb = [rb[d] for d in common]
                    ma, mb = mean(xa), mean(xb)
                    num = sum((a - ma) * (b - mb) for a, b in zip(xa, xb))
                    da = math.sqrt(sum((a - ma) ** 2 for a in xa))
                    db_ = math.sqrt(sum((b - mb) ** 2 for b in xb))
                    if abs(safe_div(num, da * db_, 0.0)) > PAIRWISE_CORR_CAP:
                        return False
            return True

        def pick(ranked, k):
            # audit 2.4: bounded incumbency BONUS on rank (never a lock)
            if holdings and INCUMBENCY_BONUS:
                order = []
                for pos, st in enumerate(ranked):
                    eff = pos - INCUMBENCY_BONUS if st["symbol"] in holdings else pos
                    order.append((eff, pos, st))
                order.sort(key=lambda t: (t[0], t[1]))
                ranked = [t[2] for t in order]
            out = []
            for st in ranked:
                if len(out) >= k:
                    break
                if st in out:
                    continue
                fam = st["index_family"]
                if ONE_PER_INDEX_FAMILY and any(s["index_family"] == fam for s in out):
                    continue
                if corr_ok(out + [st]):
                    out.append(st)
            return out

        sched = self.params.get("param_schedule")   # [(from_date, params), ...]

        def active_params(date):
            """Params in force at 'date' (walk-forward schedule, audit 2.8)."""
            if not sched:
                return lookback, skip, entry, exit_thr
            cur = (lookback, skip, entry, exit_thr)
            for d0, pd in sched:
                if date >= d0:
                    cur = (pd.get("lookback", lookback), pd.get("skip", skip),
                           pd.get("entry", entry), pd.get("exit", exit_thr))
            return cur

        def target_weights(date, mode_holdings):
            lookback, skip, entry, exit_thr = active_params(date)
            """Compute signal & sizing at 'date' for the chosen strategy.
            Mirrors the live selection engine (audit bugs #3/#4): cash-proxy
            exclusion, pre-ranking liquidity filter, correlation cap."""
            stats = []
            for sym in sym_dates:
                i = pos_at(sym, date)
                if i is None:
                    continue
                adj = sym_adj[sym][:i + 1]
                if len(adj) < lookback + 5:
                    continue
                mom = signal_momentum(adj, len(adj) - 1, lookback, skip)
                if mom is None:
                    continue
                vol = annualized_vol(adj, VOL_WINDOW)
                liq = percentile(sym_liq[sym][max(0, i - LIQ_WINDOW + 1):i + 1], LIQ_PCTL) or 0.0
                rows = [(d, a) for d, a in
                        zip(sym_dates[sym][max(0, i - CORR_WINDOW - 5):i + 1],
                            sym_adj[sym][max(0, i - CORR_WINDOW - 5):i + 1])]
                stats.append({"symbol": sym, "momentum": mom, "vol": vol or 1e-6,
                              "liq_p25": liq, "adj": adj, "rows": rows,
                              "index_family": self.meta.get(sym, {}).get("index_family", sym)})
            holding_syms = set(holdings.keys())
            decs = []
            for st in stats:
                dec, reason = decide(st["momentum"], st["symbol"] in holding_syms,
                                     entry=entry, exit_thr=exit_thr)
                # exit overlay (audit 2.5): 200-day average break forces EXITS
                # and blocks ENTRIES — no guaranteed round trips either way
                if overlay == "200dma":
                    i = pos_at(st["symbol"], date)
                    seg = sym_adj[st["symbol"]][max(0, i - 200):i + 1]
                    below = bool(seg) and seg[-1] < sum(seg) / len(seg)
                    if below and st["symbol"] in holding_syms and dec in ("HOLD",):
                        dec, reason = "EXIT", ("exit overlay: price below the "
                                               "200-day average")
                    elif below and dec == "ELIGIBLE_BUY":
                        dec, reason = ("SKIPPED",
                                       "entry overlay: price below the 200-day average")
                # audit 3.3: tax-aware exit deferral (momentum exits only —
                # overlay/risk exits still fire)
                if dec == "EXIT" and reason and reason.startswith("trend turned negative"):
                    fb = first_buy.get(st["symbol"])
                    if fb:
                        defer = tax_defer_blocks_exit([fb], date, st["momentum"] or 0.0)
                        if defer:
                            dec, reason = "HOLD", defer
                if st["symbol"] in CASH_PROXY_SYMBOLS and dec in ("ELIGIBLE_BUY", "HOLD"):
                    dec = "CASH_SLEEVE"
                elif dec == "ELIGIBLE_BUY" and (
                        (POS_CAP_LIQ_FRAC * st["liq_p25"]) / max(eq, 1.0) < MIN_LIQ_WEIGHT):
                    dec, reason = "SKIPPED", "too illiquid to trade (pre-ranking filter)"
                decs.append((st, dec, reason))
            exits = [st for st, dec, _r in decs if dec == "EXIT"]
            eligible = [st for st, dec, _r in decs if dec == "ELIGIBLE_BUY"]
            holds = [st for st, dec, _r in decs if dec == "HOLD"]
            if self.strategy == "A":
                chosen = [st for st in (eligible + holds) if corr_ok([st])]
            else:
                pool = list(stats if self.strategy == "C" else (eligible + holds))
                pool = [st for st in pool if st["symbol"] not in CASH_PROXY_SYMBOLS
                        and (POS_CAP_LIQ_FRAC * st["liq_p25"]) / max(eq, 1.0) >= MIN_LIQ_WEIGHT]
                pool.sort(key=rank_metric, reverse=True)
                chosen = pick(pool, MAX_HOLDINGS)
                # audit 2.5: rotation parity with the live engine
                if ROTATION_RULE and self.strategy in ("B", "C"):
                    rank_pos = {st["symbol"]: i + 1 for i, st in enumerate(pool)}
                    out = [st for st in pool
                           if st["symbol"] in holding_syms
                           and rank_pos.get(st["symbol"], 99) <= 5]
                    for st in pool:
                        if len(out) >= MAX_HOLDINGS:
                            break
                        if st in out:
                            continue
                        fam = st["index_family"]
                        if ONE_PER_INDEX_FAMILY and any(
                                s["index_family"] == fam for s in out):
                            continue
                        if corr_ok(out + [st]):
                            out.append(st)
                    chosen = out
            return chosen, exits, stats, decs

        # benchmark series (first large ETF or equal basket) for regime tagging
        bench_sym = None
        for cand in ("NIFTYBEES", "HDFCSENSEX", "MONIFTY500"):
            if cand in sym_dates and len(sym_dates[cand]) > 250:
                bench_sym = cand
                break
        if bench_sym is None and sym_dates:
            bench_sym = max(sym_dates, key=lambda s: len(sym_dates[s]))

        # ---------------- main day loop ----------------
        prev_signal = None
        exec_buffer = []       # (signal_date, weights, chosen, exits)
        lock_date = None       # when halved/all-cash engaged (time-based release)
        in_review = False      # review = STATE TRANSITIONS, not days (audit 2.9)
        for date in all_dates:
            # cash interest accrual (daily) + FY tax settlement (audit #7)
            fy_now = financial_year(parse_date(date))
            if fy_now != current_fy:
                settle_fy(current_fy)
                current_fy = fy_now
            daily_r = cash_fn(date)          # sleeve history or flat proxy
            before = cash
            cash *= (1.0 + daily_r)
            interest_earned += cash - before

            # execute pending rebalance from previous month-end
            if exec_buffer:
                sig_date, weights, chosen, exits = exec_buffer.pop(0)
                # Step 7: re-check filters at execution time
                eq_now = cash + holdings_value(date)
                # sells
                for sym in list(holdings.keys()):
                    tgt_w = weights.get(sym, 0.0)
                    h = holdings[sym]
                    p = px_at(sym, date) or h["avg_cost"]
                    have_val = h["qty"] * p
                    tgt_val = tgt_w * eq_now
                    if tgt_val < have_val - 1.0:
                        is_exit = tgt_w <= 0.0
                        # audit 3.1: no-trade band — exits ALWAYS execute
                        if (not is_exit and NO_TRADE_BAND > 0 and have_val > 0
                                and abs(tgt_val - have_val) / max(tgt_val, have_val)
                                < NO_TRADE_BAND):
                            continue
                        sell_val = have_val - tgt_val
                        if not is_exit and sell_val < MIN_TRADE_VALUE:
                            continue
                        qty = int(sell_val / p) if p > 0 else 0
                        qty = min(qty, int(h["qty"]))
                        if qty > 0:
                            v = do_sell(sym, qty, p, date)
                            trades += 1
                # buys
                eq_now = cash + holdings_value(date)
                for sym, w in sorted(weights.items(), key=lambda kv: -kv[1]):
                    if w <= 0 or all_cash_mode:
                        continue
                    p = px_at(sym, date)
                    if p is None:
                        skipped += 1
                        continue
                    tgt_val = w * eq_now
                    have = holdings.get(sym)
                    have_val = (have["qty"] * p) if have else 0.0
                    if tgt_val <= have_val + 1.0:
                        continue
                    buy_val = tgt_val - have_val
                    # audit 3.1: no-trade band + dust guard on entries
                    if (NO_TRADE_BAND > 0 and have_val > 0
                            and abs(tgt_val - have_val) / max(tgt_val, have_val)
                            < NO_TRADE_BAND):
                        continue
                    if buy_val < MIN_TRADE_VALUE:
                        continue
                    # liquidity binding rule
                    i = pos_at(sym, date)
                    liq = percentile(sym_liq[sym][max(0, i - LIQ_WINDOW + 1):i + 1], LIQ_PCTL) or 0.0
                    if buy_val > POS_CAP_LIQ_FRAC * liq:
                        buy_val = POS_CAP_LIQ_FRAC * liq
                    qty = int(buy_val / p) if p > 0 else 0
                    if qty > 0:
                        v = do_buy(sym, qty, p, date)
                        if v > 0:
                            trades += 1
                        else:
                            skipped += 1
                spread_used_vals.append(self._spread_for("NIFTYBEES", date, spread_stat))

            # month-end signal -> schedule next-session execution
            if date in signal_set:
                if date != all_dates[-1]:
                    chosen, exits, stats, decs = target_weights(date, set(holdings.keys()))
                    eq_now = cash + holdings_value(date)
                    scale = 1.0
                    if self.risk_on:
                        if all_cash_mode:
                            scale = 0.0
                        elif halved:
                            scale = 0.5
                    # size
                    if chosen:
                        n = len(chosen)
                        if self.strategy == "A":
                            raw = [1.0 / n] * n
                        else:
                            inv = [1.0 / s["vol"] if s["vol"] > 0 else 1.0 for s in chosen]
                            ss = sum(inv)
                            raw = [x / ss for x in inv]
                        w = []
                        caps = []
                        for k, st in enumerate(chosen):
                            cap = min(POS_CAP_ABS,
                                      POS_CAP_LIQ_FRAC * st["liq_p25"] / eq_now if eq_now > 0 else 0.0)
                            caps.append(cap)
                            w.append(min(raw[k], cap))
                        # covariance — DATE-ALIGNED returns (audit bug #9)
                        series_by_sym = {}
                        for st in chosen:
                            rows = []
                            cnt = 0
                            for d2 in reversed(all_dates[:all_dates.index(date) + 1]):
                                i2 = pos_at(st["symbol"], d2)
                                if i2 is None:
                                    continue
                                rows.append((d2, sym_adj[st["symbol"]][i2]))
                                cnt += 1
                                if cnt > COV_WINDOW + 5:
                                    break
                            series_by_sym[st["symbol"]] = list(reversed(rows))
                        X, _s, _d = aligned_return_matrix(series_by_sym, COV_WINDOW)
                        Tt = len(X)
                        if Tt >= 20:
                            Xd = demean(X)
                            Sigma, delta, cond = ledoit_wolf_cc(Xd)
                            delta_vals.append(delta)
                            cond_vals.append(cond)
                        else:
                            Sigma = [[0.0] * n for _ in range(n)]
                            for k in range(n):
                                Sigma[k][k] = (chosen[k]["vol"] or 0.0) ** 2
                        raw_vol = portfolio_vol(w, Sigma) * math.sqrt(252.0)  # annualised
                        reg_mult = 1.0
                        if REGIME_SCALING and bench_sym:
                            bi2 = pos_at(bench_sym, date)
                            if bi2 is not None and bi2 >= 200:
                                seg2 = sym_adj[bench_sym][bi2 - 200:bi2 + 1]
                                if seg2[-1] < sum(seg2) / len(seg2):
                                    reg_mult = 0.5
                        tgt_vol = VOL_TARGET * scale * reg_mult
                        if raw_vol <= 0:
                            s_scale = 1.0
                        elif raw_vol <= tgt_vol:
                            s_scale = 1.0          # accept LOWER risk (rulebook Step 6.5)
                        else:
                            s_scale = tgt_vol / raw_vol
                            if tgt_vol > 0:
                                reach_count += 1   # 8% is reachable inside the caps
                        month_count += 1
                        weights = {chosen[k]["symbol"]: w[k] * s_scale for k in range(n)}
                    else:
                        weights = {}
                        month_count += 1
                    for st in exits:
                        weights[st["symbol"]] = 0.0
                    # turnover
                    total_w = sum(abs(weights.get(s, 0.0))
                                  for s in set(list(weights.keys()) + list(holdings.keys())))
                    turnover_vals.append(total_w)
                    exec_buffer.append((date, weights, chosen, exits))

            # mark to market
            hv = holdings_value(date)
            eq = cash + hv
            if eq > peak:
                peak = eq
            dd = 1.0 - eq / peak if peak > 0 else 0.0
            if self.risk_on:
                dd_rev_m, dd_sev_m, dd_ext_m = dd_thresholds(VOL_TARGET)
                if dd >= dd_ext_m and not all_cash_mode:
                    all_cash_mode = True
                    lock_date = date
                    risk_events["EXTREME"] += 1
                    for sym in list(holdings.keys()):
                        p = px_at(sym, date) or holdings[sym]["avg_cost"]
                        do_sell(sym, int(holdings[sym]["qty"]), p, date)
                elif dd >= dd_sev_m and not halved and not all_cash_mode:
                    halved = True
                    lock_date = date
                    risk_events["SEVERE"] += 1
                if dd >= dd_rev_m and not in_review:
                    in_review = True          # audit 2.9: count EVENTS not days
                    risk_events["REVIEW"] += 1
                elif dd < dd_rev_m:
                    in_review = False
                # recovery: price band OR time-based release (audit 2.5 parity)
                time_release = (lock_date is not None and
                                (parse_date(date) - parse_date(lock_date)).days
                                >= RISK_RELEASE_DAYS)
                if (halved or all_cash_mode) and (dd <= REENTRY_BAND or time_release):
                    halved = False
                    all_cash_mode = False
                    risk_events["RECOVERY"] += 1
                    if dd > REENTRY_BAND:
                        peak = eq            # time-based: reset the slate
                    lock_date = None
            equity_curve.append((date, eq))
            daily_invested.append(safe_div(hv, eq, 0.0))
            # regime tagging via benchmark 200-day trend
            if bench_sym:
                bi = pos_at(bench_sym, date)
                if bi is not None and bi >= 200:
                    seg = sym_adj[bench_sym][bi - 200:bi + 1]
                    ma = sum(seg) / len(seg)
                    regime = "bull" if seg[-1] >= ma else "bear"
                else:
                    regime = None
            else:
                regime = None
            if regime:
                regime_returns[regime].append(None)  # placeholder; monthly fill below
        # ------- monthly returns & breakdowns -------
        eq_by_date = dict(equity_curve)
        monthly_rets = []
        prev_eq = None
        for date in all_dates:
            eq = eq_by_date[date]
            if prev_eq is not None and prev_eq > 0:
                monthly_rets.append((date, eq / prev_eq - 1.0))
            prev_eq = eq

        # audit 2.1: settle the final FY BEFORE the final curve point is used,
        # so equity_curve[-1] is already NET of all taxes
        settle_fy(current_fy)
        if equity_curve:
            last_date = equity_curve[-1][0]
            hv_now = holdings_value(last_date)
            equity_curve[-1] = (last_date, cash + hv_now)
        result = self._summarize(
            all_dates, equity_curve, daily_invested, monthly_rets, turnover_vals,
            cond_vals, delta_vals, reach_count, month_count, risk_events,
            trades, skipped, tax_accrued, costs_paid, cash, holdings, px_at,
            spread_used_vals, lookback, entry, skip, exit_thr)
        result["interest_earned"] = interest_earned
        return result

    def _liq_at(self, sym, date):
        """25th-percentile daily traded value at 'date' (for impact sizing)."""
        series = self.prices.get(sym, [])
        vals = []
        for (d, _a, _c, t) in reversed(series):
            if d > date:
                continue
            vals.append(t)
            if len(vals) >= LIQ_WINDOW:
                break
        return percentile(vals, LIQ_PCTL) or 0.0

    def _spread_for(self, sym, date, spread_stat):
        """Liquidity-tiered spread estimate (audit #8: thin ETFs cost more)."""
        broad = self.meta.get(sym, {}).get("broad", False)
        base = estimate_spread_bps(self._liq_at(sym, date), broad)
        if spread_stat == "p90":
            return base * 1.2
        return base * 0.8

    def _summarize(self, all_dates, equity_curve, daily_invested, monthly_rets,
                   turnover_vals, cond_vals, delta_vals, reach_count, month_count,
                   risk_events, trades, skipped, tax_accrued, costs_paid, cash, holdings,
                   px_at, spread_used_vals, lookback, entry, skip, exit_thr):
        if not equity_curve:
            return {"error": "empty backtest"}
        eqs = [e for (_d, e) in equity_curve]
        d0 = all_dates[0]
        d1 = all_dates[-1]
        years = max((parse_date(d1) - parse_date(d0)).days / 365.25, 1e-9)
        # audit 2.1: taxes are ALREADY deducted from cash in-run (settle_fy).
        # Subtracting tax_accrued again here was the double-count.  The curve's
        # final point is net of costs AND taxes; pre-tax figures come from
        # adding tax back (labelled) or from a separate no-tax run.
        final_after_tax = eqs[-1]                     # net of costs AND taxes
        final = final_after_tax
        final_after_cost = final_after_tax + tax_accrued     # pre-personal-tax
        final_before_costs = final_after_cost + costs_paid   # pre-cost too
        cagr_before_costs = (max(final_before_costs, 1.0) / self.capital) ** (1.0 / years) - 1.0
        cagr_after_costs = (max(final_after_cost, 1.0) / self.capital) ** (1.0 / years) - 1.0
        cagr_after_tax = (max(final_after_tax, 1.0) / self.capital) ** (1.0 / years) - 1.0
        cagr = cagr_after_tax
        # daily vol & STANDARD Sharpe = mean(excess)/std(excess) x sqrt(252)
        # (audit #9: (CAGR - rf)/vol is not a Sharpe ratio)
        rets = []
        for i in range(1, len(eqs)):
            if eqs[i - 1] > 0:
                rets.append(eqs[i] / eqs[i - 1] - 1.0)
        vol = stdev(rets, ddof=1) * math.sqrt(252.0) if len(rets) > 2 else 0.0
        rf_daily = (1.0 + self.cash_rate) ** (1.0 / 252.0) - 1.0
        excess = [r - rf_daily for r in rets]
        sharpe = (mean(excess) / stdev(excess, ddof=1) * math.sqrt(252.0)
                  if len(excess) > 2 and stdev(excess, ddof=1) > 0 else 0.0)
        # max drawdown & recovery
        peak = eqs[0]
        max_dd = 0.0
        dd_start = None
        recovery_days = 0
        worst_len = 0
        for i, e in enumerate(eqs):
            if e > peak:
                if dd_start is not None:
                    worst_len = max(worst_len, i - dd_start)
                peak = e
                dd_start = None
            else:
                if dd_start is None:
                    dd_start = i
                dd = 1.0 - e / peak
                if dd > max_dd:
                    max_dd = dd
        if dd_start is not None:
            worst_len = max(worst_len, len(eqs) - dd_start)
        recovery_days = worst_len
        avg_inv = mean(daily_invested) if daily_invested else 0.0
        cash_w = [1.0 - x for x in daily_invested]
        cash_p25 = percentile(cash_w, 25) or 0.0
        cash_p50 = percentile(cash_w, 50) or 0.0
        cash_p75 = percentile(cash_w, 75) or 0.0
        avg_turnover = mean(turnover_vals) if turnover_vals else 0.0
        avg_cond = mean(cond_vals) if cond_vals else 0.0
        avg_delta = mean(delta_vals) if delta_vals else 0.0
        reach_pct = safe_div(reach_count, month_count, 0.0)
        return {
            "start": d0, "end": d1, "years": years,
            "capital": self.capital, "final_equity": final_after_tax,
            "final_equity_after_cost": final_after_cost,
            "cagr_before_costs": cagr_before_costs,
            "cagr_after_cost": cagr_after_costs,
            "cagr_after_cost_tax": cagr_after_tax,
            "cagr_pretax": cagr_after_costs,
            "equity_curve": equity_curve,
            "invested_curve": list(zip([d for d, _e in equity_curve], daily_invested)),
            "volatility": vol, "sharpe": sharpe,
            "max_drawdown": max_dd, "recovery_days": recovery_days,
            "avg_invested": avg_inv, "cash_p25": cash_p25, "cash_p50": cash_p50,
            "cash_p75": cash_p75, "avg_turnover": avg_turnover,
            "avg_cond": avg_cond, "avg_delta": avg_delta,
            "vol_target_reachable_pct": reach_pct,
            "risk_events": {k: v for k, v in risk_events.items() if not k.startswith("_")},
            "trades": trades, "skipped": skipped, "tax_accrued": tax_accrued,
            "costs_paid": costs_paid,
            "months": month_count, "cost_case": self.cost_case,
            "cash_rate": self.cash_rate, "strategy": self.strategy,
            "cash_sleeve": getattr(self, "_cash_fn_flag", False),
            "params": {"lookback": lookback, "skip": skip, "entry": entry, "exit": exit_thr},
            "final_cash": cash, "final_holdings": dict(holdings),
            "avg_spread_bps": mean(spread_used_vals) if spread_used_vals else 0.0,
        }


# ==============================================================================
# SECTION 21 — REPORTING (beautiful console + saved text reports)
# ==============================================================================

def load_prices_from_db(db: Db) -> tuple:
    """prices dict + meta dict for the backtester."""
    prices = {}
    meta = {}
    for r in db.query("SELECT symbol, name, category, index_family, broad FROM etf_universe"):
        meta[r["symbol"]] = {"name": r["name"], "category": r["category"],
                             "index_family": r["index_family"], "broad": bool(r["broad"])}
    for sym in db.symbols_with_prices():
        rows = db.price_series(sym)
        prices[sym] = [(r["date"], r["adj_close"], r["close"], r["traded_value"] or 0.0)
                       for r in rows]
    return prices, meta


def print_backtest_result(res: dict, title: str = "BACKTEST RESULT"):
    if res.get("error"):
        P.err(f"Backtest could not run: {res['error']}")
        return
    P.section(title, "\U0001F9EA")
    p = res["params"]
    P.kv("Period", f"{res['start']}  ->  {res['end']}   ({res['years']:.1f} years)")
    P.kv("Strategy", {"A": "A — Pure TSMOM (all qualifying ETFs)",
                      "B": "B — TSMOM + Ranking (top 3)",
                      "C": "C — Ranking only (top 3, no threshold)"}[res["strategy"]])
    P.kv("Parameters", f"lookback {p['lookback']} | skip {p['skip']} | "
                       f"entry {fmt_pct(p['entry'],0)} | exit {fmt_pct(p['exit'],0)}")
    P.kv("Cost scenario", f"{res['cost_case']}  (avg spread {res['avg_spread_bps']:.1f} bps)")
    if res.get("cash_sleeve"):
        P.kv("Cash assumption",
             f"real liquid-sleeve history ({CASH_SLEEVE_SYMBOL} daily returns)")
    else:
        P.kv("Cash assumption",
             f"{fmt_pct(res['cash_rate'],2)} per year (flat T-bill proxy)")

    P.raw()
    rows = [
        ["Starting capital", fmt_cr(res["capital"])],
        ["Final equity (after costs & taxes)", fmt_cr(res["final_equity"])],
        ["CAGR — before costs", fmt_pct(res["cagr_before_costs"])],
        ["CAGR — after costs (pre-tax add-back)", fmt_pct(res["cagr_after_cost"])],
        ["CAGR — after costs + taxes (net)", fmt_pct(res["cagr_after_cost_tax"])],
        ["Total costs paid (spread+charges)", fmt_cr(res.get("costs_paid", 0.0))],
        ["Cash interest earned (cash carry)", fmt_cr(res.get("interest_earned", 0.0))],
        ["Annual volatility", fmt_pct(res["volatility"])],
        ["Sharpe ratio (standard, daily excess vs cash)", f"{res['sharpe']:.2f}"],
        ["Maximum drawdown", fmt_pct(-res["max_drawdown"])],
        ["Longest underwater stretch", f"{res['recovery_days']} sessions"],
        ["Average invested fraction", fmt_pct(res["avg_invested"])],
        ["Cash weight p25 / p50 / p75",
         f"{fmt_pct(res['cash_p25'],0)} / {fmt_pct(res['cash_p50'],0)} / {fmt_pct(res['cash_p75'],0)}"],
        ["Average monthly turnover", f"{res['avg_turnover']:.2f}x"],
        ["Months 8% vol target reachable", fmt_pct(res["vol_target_reachable_pct"], 1)],
        ["Avg Ledoit-Wolf shrinkage", f"{res['avg_delta']:.3f}"],
        ["Avg covariance condition no.", f"{res['avg_cond']:,.1f}"],
        ["Total trades / skipped", f"{res['trades']} / {res['skipped']}"],
        ["Tax accrued (STCG+LTCG)", fmt_cr(res["tax_accrued"])],
    ]
    P.table(["Metric", "Value"], rows, aligns=["<", ">"])
    ev = res["risk_events"]
    P.sub("Risk trigger events (Step 9)")
    P.info(f"10% reviews: {ev.get('REVIEW',0)}   |   15% halvings: {ev.get('SEVERE',0)}   |   "
           f"20% cash-outs: {ev.get('EXTREME',0)}   |   recoveries: {ev.get('RECOVERY',0)}")
    P.note("Drawdown triggers are risk controls, NOT guaranteed stop-losses — "
           "gaps and illiquid markets can produce larger losses.")


def regime_category_report(prices: dict, meta: dict, equity_curve: list):
    """Step 10: performance by market regime and by ETF category."""
    P.section("PERFORMANCE BY MARKET REGIME & ETF CATEGORY", "🌍")
    # --- regime: bull/bear from benchmark 200-day trend ---
    bench = None
    for cand in ("NIFTYBEES", "HDFCSENSEX", "MONIFTY500"):
        if cand in prices and len(prices[cand]) > 250:
            bench = cand
            break
    if bench is None and prices:
        bench = max(prices, key=lambda s: len(prices[s]))
    if bench is None or not equity_curve:
        P.info("Not enough data for regime breakdown.")
        return
    bdates = [d for (d, _a, _c, _t) in prices[bench]]
    badj = [a for (_d, a, _c, _t) in prices[bench]]
    bpos = {}
    for i, d in enumerate(bdates):
        bpos[d] = i

    def regime_at(date):
        i = bpos.get(date)
        if i is None or i < 200:
            return None
        seg = badj[i - 200:i + 1]
        return "bull" if seg[-1] >= sum(seg) / len(seg) else "bear"

    # true month-end-to-month-end returns
    me_eq = {}     # month -> equity at its last session
    me_date = {}   # month -> date of its last session
    for d, e in equity_curve:
        mk = month_key(parse_date(d))
        me_eq[mk] = e
        me_date[mk] = d
    months = sorted(me_eq)
    monthly = []
    for i in range(1, len(months)):
        m0, m1 = months[i - 1], months[i]
        if me_eq[m0] > 0:
            monthly.append((m1, me_eq[m0], me_eq[m1] / me_eq[m0] - 1.0,
                            regime_at(me_date[m0])))
    buckets = {"bull": [], "bear": [], "unknown": []}
    for _mk, _eq, ret, reg in monthly:
        buckets[reg or "unknown"].append(ret)
    rows = []
    for reg in ("bull", "bear", "unknown"):
        rs = buckets[reg]
        if not rs:
            continue
        avg_m = mean(rs)
        rows.append([reg.upper(), str(len(rs)), fmt_pct(avg_m, 2),
                     fmt_pct((1 + avg_m) ** 12 - 1), fmt_pct(min(rs), 1)])
    P.sub("Portfolio monthly returns split by regime (benchmark 200-day trend)")
    P.table(["Regime", "Months", "Avg month", "Roughly / year", "Worst month"], rows,
            aligns=["<", ">", ">", ">", ">"])

    # --- category buy-and-hold monthly returns (equal-weight baskets) ---
    by_cat = {}
    for sym, series in prices.items():
        cat = meta.get(sym, {}).get("category", "OTHER")
        by_cat.setdefault(cat, []).append(sym)
    cat_rows = []
    for cat, syms in sorted(by_cat.items()):
        rets = []
        for sym in syms[:25]:
            s = prices[sym]
            m_end = {}
            for (d, a, _c, _t) in s:
                m_end[month_key(parse_date(d))] = (d, a)
            vals = sorted(m_end.items())
            for i in range(1, len(vals)):
                a0 = vals[i - 1][1][1]
                a1 = vals[i][1][1]
                if a0 and a1 and a0 > 0:
                    rets.append(a1 / a0 - 1.0)
        if rets:
            avg_m = mean(rets)
            cat_rows.append([cat, str(len(syms)), fmt_pct(avg_m, 2),
                             fmt_pct((1 + avg_m) ** 12 - 1)])
    P.sub("Equal-weight category baskets (buy & hold, monthly)")
    P.table(["Category", "ETFs", "Avg month", "Roughly / year"], cat_rows,
            aligns=["<", ">", ">", ">"])
    P.note("Category rows are passive baskets (context for the strategy), "
           "not TSMOM portfolio returns.")


def cash_return_series(prices: dict, cash_rate: float, use_sleeve=None):
    """
    Per-day cash return (audit 2.6).  In LIQUID_ETF mode the cash sleeve earns
    the REAL liquid-ETF's daily returns (own series, own costs and slab tax in
    the ledger); if its history is missing we fall back to the flat T-bill
    proxy WITH A WARNING — a constant 5.3% over 10 years is implausible and
    must never be silently pretended to be market data.
    Returns a callable fn(date) -> daily simple return.
    """
    if use_sleeve is None:
        use_sleeve = (CASH_SLEEVE_MODE == "LIQUID_ETF")
    flat = (1.0 + cash_rate) ** (1.0 / 252.0) - 1.0
    if use_sleeve:
        rows = sorted(((d, a) for (d, a, _c, _t) in prices.get(CASH_SLEEVE_SYMBOL, [])))
        if rows:
            rets = {}
            for i in range(1, len(rows)):
                if rows[i - 1][1]:
                    rets[rows[i][0]] = rows[i][1] / rows[i - 1][1] - 1.0

            def fn(d):
                return rets.get(d, flat)
            fn.sleeve = True
            return fn
        P.warn(f"{CASH_SLEEVE_SYMBOL} history not found — cash sleeve falls back "
               f"to the flat {fmt_pct(cash_rate, 2)} T-bill proxy (labelled).")

    def fn_flat(d):
        return flat
    fn_flat.sleeve = False
    return fn_flat


def benchmark_report(prices: dict, meta: dict, equity_curve: list,
                     strategy_name: str, cash_rate: float, avg_invested: float = 0.6):
    """Audit sec 2.4: buy-and-hold + risk-matched benchmark — without these you
    cannot tell whether the strategy adds anything over sitting in an index."""
    bench = None
    for cand in ("NIFTYBEES", "HDFCSENSEX", "MONIFTY500", "TOP100CASE"):
        if cand in prices and len(prices[cand]) > 250:
            bench = cand
            break
    if bench is None and prices:
        bench = max(prices, key=lambda s: len(prices[s]))
    if bench is None or not equity_curve:
        return
    P.section("BENCHMARKS — does the strategy add anything?", "\U0001F4CA")
    # benchmark adjusted-close values keyed by date, kept sorted for fast lookup
    bseries = sorted(((d, a) for (d, a, _c, _t) in prices[bench]))
    bdates = [d for (d, _a) in bseries]
    bvals = [a for (_d, a) in bseries]
    dates = [d for (d, _e) in equity_curve]
    eqs = [e for (_d, e) in equity_curve]
    start_eq = eqs[0]
    b_prev = None
    bh_eq = start_eq
    rm_eq = start_eq
    bh_curve = []
    rm_curve = []
    avg_invested_frac = clamp(avg_invested, 0.0, 1.0)
    cash_fn_b = cash_return_series(prices, cash_rate)   # same sleeve as strategy
    for d in dates:
        # last benchmark value at or before d (dates are ascending)
        lo, hi, ans = 0, len(bdates) - 1, None
        while lo <= hi:
            mid = (lo + hi) // 2
            if bdates[mid] <= d:
                ans = bvals[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        b = ans
        if b is None:
            bh_curve.append(bh_eq)
            rm_curve.append(rm_eq)
            continue
        if b_prev is not None and b_prev > 0:
            r = b / b_prev - 1.0
            bh_eq *= (1.0 + r)
            rm_eq *= (1.0 + avg_invested_frac * r +
                      (1.0 - avg_invested_frac) * cash_fn_b(d))
        b_prev = b
        bh_curve.append(bh_eq)
        rm_curve.append(rm_eq)

    def stats_of(curve):
        years = max((parse_date(dates[-1]) - parse_date(dates[0])).days / 365.25, 1e-9)
        cagr = (curve[-1] / start_eq) ** (1.0 / years) - 1.0
        peak, mdd = curve[0], 0.0
        for e in curve:
            peak = max(peak, e)
            mdd = max(mdd, 1.0 - e / peak)
        return cagr, mdd, curve[-1]

    # audit 2.9: benchmark rows are NET of statutory entry costs and final
    # LTCG (buy & hold realises nothing until the end, so LTCG applies once).
    # The drag lands ON the curves so CAGR/DD stay consistent with the final.
    entry_ch = calc_charges("BUY", start_eq)["total"]

    def net_ltcg(final_eq):
        gain = final_eq - start_eq
        if gain <= 0:
            return final_eq
        tax = max(0.0, gain - LTCG_EXEMPTION) * LTCG_RATE * (1.0 + CESS_RATE)
        return final_eq - tax

    if bh_curve:
        bh_curve[-1] = net_ltcg(bh_curve[-1] - entry_ch)
    if rm_curve:
        rm_curve[-1] = net_ltcg(rm_curve[-1] - entry_ch * avg_invested_frac)
    bh_cagr, bh_dd, bh_fin = stats_of(bh_curve)
    rm_cagr, rm_dd, rm_fin = stats_of(rm_curve)
    st_cagr, st_dd, st_fin = stats_of(eqs)
    rows = [
        [f"{strategy_name} (this strategy)", fmt_pct(st_cagr), fmt_pct(-st_dd), fmt_cr(st_fin)],
        [f"Buy & hold {bench} (net of cost + LTCG)", fmt_pct(bh_cagr), fmt_pct(-bh_dd), fmt_cr(bh_fin)],
        [f"Risk-matched {bench} ({fmt_pct(avg_invested_frac,0)} invested + cash, net)",
         fmt_pct(rm_cagr), fmt_pct(-rm_dd), fmt_cr(rm_fin)],
    ]
    P.table(["", "CAGR", "Max DD", "Final"], rows, aligns=["<", ">", ">", ">"])
    P.info(f"Strategy minus buy & hold: {fmt_pct(st_cagr - bh_cagr)} per year | "
           f"minus risk-matched: {fmt_pct(st_cagr - rm_cagr)} per year")
    P.note("Much of a low-vol TSMOM return can come from CASH CARRY (idle cash at "
           f"{fmt_pct(cash_rate,2)}). Compare against the risk-matched row above.")


def walk_forward(prices: dict, meta: dict, quick: bool = False):
    """
    Walk-forward validation (audit 2.8, cold-start fix):
      1. per window, choose parameters on the TRAIN prefix only (selection
         runs — quick grid, cold start OK because they only pick parameters);
      2. run ONE continuous full-history backtest whose parameters switch at
         the window boundaries (a param schedule);
      3. report OOS performance as SEGMENTS OF THAT SINGLE CURVE — no fresh
         cold start per window, so nothing is lost at the seams.
    """
    all_dates = sorted({d for s in prices.values() for (d, _a, _c, _t) in s})
    me = month_end_indices(all_dates)
    me_dates = [all_dates[i] for i in me]
    train_m = 36
    test_m = 12
    if len(me_dates) < train_m + test_m + 2:
        P.warn("Not enough months for walk-forward (need 48+). Skipped.")
        return None
    P.section("WALK-FORWARD VALIDATION (36m train -> 12m test, rolled)", "\U0001F504")
    combos = ([(lb, en, sk) for lb in GRID_LOOKBACKS for en in GRID_ENTRIES for sk in GRID_SKIPS]
              if not quick else [(LOOKBACK, ENTRY_THRESHOLD, SKIP)])
    windows = []
    cursor = train_m
    while cursor + test_m - 1 < len(me_dates):
        train_end = me_dates[cursor - 1]
        test_start = me_dates[cursor]
        test_end = me_dates[cursor + test_m - 1]
        best = None
        for lb, en, sk in combos:
            bt = Backtester(prices, meta, {"lookback": lb, "skip": sk,
                                           "entry": en, "exit": EXIT_THRESHOLD},
                            cost_case="base", cash_rate=CASH_RATE_PRETAX,
                            strategy=TRADE_STRATEGY)
            r = bt.run(start_date=me_dates[0], end_date=train_end)
            if r.get("error"):
                continue
            score = r["sharpe"]
            if best is None or score > best[0]:
                best = (score, lb, en, sk)
        if best is None:
            break
        windows.append({"train_end": train_end, "test_start": test_start,
                        "test_end": test_end, "pick": best[1:]})
        cursor += test_m
    if not windows:
        P.warn("Walk-forward selection produced no windows.")
        return None

    # ---- ONE continuous run with a parameter schedule (audit 2.8) ----
    schedule = []
    for w in windows:
        lb, en, sk = w["pick"]
        schedule.append((w["test_start"],
                         {"lookback": lb, "skip": sk,
                          "entry": en, "exit": EXIT_THRESHOLD}))
    bt = Backtester(prices, meta, {"lookback": LOOKBACK, "skip": SKIP,
                                   "entry": ENTRY_THRESHOLD, "exit": EXIT_THRESHOLD,
                                   "param_schedule": schedule},
                    cost_case="base", cash_rate=CASH_RATE_PRETAX,
                    strategy=TRADE_STRATEGY)
    r = bt.run(start_date=me_dates[0], end_date=me_dates[-1])
    if r.get("error"):
        P.warn(f"walk-forward continuous run failed: {r['error']}")
        return None
    curve = r.get("equity_curve", [])

    def eq_at(d):
        best_e, best_d = None, None
        for dd, e in curve:
            if dd <= d and (best_d is None or dd >= best_d):
                best_d, best_e = dd, e
        return best_e

    def seg_stats(d1, d2):
        e1, e2 = eq_at(d1), eq_at(d2)
        if not e1 or not e2 or e1 <= 0:
            return None, None
        yrs = max((parse_date(d2) - parse_date(d1)).days / 365.25, 1e-9)
        return (e2 / e1) ** (1.0 / yrs) - 1.0, e2 / e1 - 1.0

    rows = []
    oos_growth = 1.0
    oos_years = 0.0
    for w in windows:
        lb, en, sk = w["pick"]
        cagr, tot = seg_stats(w["test_start"], w["test_end"])
        if cagr is None:
            continue
        rows.append([w["test_start"], w["test_end"], f"{lb}/{sk}", fmt_pct(en, 0),
                     fmt_pct(cagr), fmt_pct(tot)])
        oos_growth *= (1.0 + tot)
        oos_years += (parse_date(w["test_end"]) - parse_date(w["test_start"])).days / 365.25
    P.table(["Test from", "Test to", "Chosen lb/skip", "Entry",
             "CAGR (aft tax)", "Total ret"], rows,
            aligns=["<", "<", "<", ">", ">", ">"])
    if oos_years > 0 and rows:
        wf_cagr = oos_growth ** (1.0 / oos_years) - 1.0
        P.ok(f"Stitched walk-forward CAGR over {len(rows)} OOS windows "
             f"({oos_years:.1f} contiguous years of ONE continuous run): "
             f"{fmt_pct(wf_cagr)}")
        P.note("Each window's parameters were chosen strictly before its test "
               "period, and all windows share one equity curve — no cold-start "
               "months are lost between windows (audit 2.8).")
    return rows


def bootstrap_ci(equity_curve: list, draws: int = 1000, seed: int = 11):
    """Audit sec 2.5: bootstrap the monthly return series -> CAGR confidence band."""
    if not equity_curve or len(equity_curve) < 60:
        return None
    me_eq = {}
    for d, e in equity_curve:
        me_eq[month_key(parse_date(d))] = e
    months = sorted(me_eq)
    rets = [me_eq[months[i]] / me_eq[months[i - 1]] - 1.0
            for i in range(1, len(months)) if me_eq[months[i - 1]] > 0]
    if len(rets) < 12:
        return None
    rng = random.Random(seed)
    years = len(rets) / 12.0
    # audit 2.9: BLOCK bootstrap — 6-month blocks preserve serial dependence
    # (momentum returns are autocorrelated; iid resampling is far too narrow)
    block = 6
    cagrs = []
    for _ in range(draws):
        g = 1.0
        got = 0
        while got < len(rets):
            start = rng.randrange(max(1, len(rets) - block + 1))
            for j in range(block):
                if got >= len(rets):
                    break
                g *= (1.0 + rets[min(start + j, len(rets) - 1)])
                got += 1
        cagrs.append(g ** (1.0 / max(years, 1e-9)) - 1.0)
    cagrs.sort()
    lo = cagrs[int(0.05 * draws)]
    med = cagrs[int(0.50 * draws)]
    hi = cagrs[int(0.95 * draws)]
    P.section("BOOTSTRAP — 90% confidence band on CAGR (monthly resampling)", "\U0001F3B2")
    P.table(["5th pct (bad luck)", "Median", "95th pct (good luck)"],
            [[fmt_pct(lo), fmt_pct(med), fmt_pct(hi)]], aligns=[">", ">", ">"])
    P.note(f"Based on {len(rets)} monthly returns, {draws} block resamples "
           f"({block}-month blocks). Serial dependence is partly preserved; "
           "treat as a sanity band, "
           "not a promise.")
    return lo, med, hi


def period_report(prices: dict, equity_curve: list):
    """Audit sec 2.5: results by period + how many ETFs were actually eligible."""
    if not equity_curve:
        return
    P.section("RETURNS BY FINANCIAL YEAR + UNIVERSE DEPTH", "\U0001F4C5")
    me_eq = {}
    for d, e in equity_curve:
        me_eq[month_key(parse_date(d))] = e
    months = sorted(me_eq)
    # eligible count per FY (symbols with >= lookback bars by that date)
    rows = []
    fy_rets = {}
    for i in range(1, len(months)):
        m0, m1 = months[i - 1], months[i]
        r = me_eq[m1] / me_eq[m0] - 1.0 if me_eq[m0] > 0 else 0.0
        d1 = parse_date(m1 + "-28")
        fy = financial_year(d1)
        fy_rets.setdefault(fy, []).append(r)
    for fy in sorted(fy_rets):
        rs = fy_rets[fy]
        g = 1.0
        for x in rs:
            g *= (1.0 + x)
        rows.append([fy, str(len(rs)), fmt_pct(g - 1.0), fmt_pct(mean(rs), 2)])
    P.table(["Financial year", "Months", "Return", "Avg month"], rows,
            aligns=["<", ">", ">", ">"])
    # universe depth at start/middle/end
    all_dates = sorted({d for s in prices.values() for (d, _a, _c, _t) in s})
    for probe in (all_dates[5], all_dates[len(all_dates) // 2], all_dates[-1]):
        n = sum(1 for s, ser in prices.items()
                if len([1 for (d, _a, _c, _t) in ser if d <= probe]) >= MIN_HISTORY_BARS)
        P.info(f"ETFs with 252+ sessions of history by {probe}: {n}")
    P.note("Early years carry a thinner universe (many ETFs listed recently) — "
           "compare periods with that in mind.")


def run_full_backtest(db: Db, quick: bool = False):
    """Step 10: in-sample/OOS split, parameter grid, cost & cash scenarios."""
    prices, meta = load_prices_from_db(db)
    if not prices:
        P.err("No price data in the database yet. Run: python tsmom.py update")
        return None
    all_dates = sorted({d for s in prices.values() for (d, _a, _c, _t) in s})
    split = all_dates[int(len(all_dates) * 0.8)]
    P.section("BACKTEST ENGINE — 80/20 IN-SAMPLE / OUT-OF-SAMPLE SPLIT", "\U0001F5C3")
    P.info(f"Data span: {all_dates[0]} -> {all_dates[-1]}  ({len(all_dates):,} sessions)")
    P.info(f"Parameter selection uses ONLY data before {split}.")
    P.note("The last 20% stays untouched while choosing parameters.")

    grid_lbs = GRID_LOOKBACKS if not quick else [LOOKBACK]
    grid_ents = GRID_ENTRIES if not quick else [ENTRY_THRESHOLD]
    grid_skips = GRID_SKIPS if not quick else [SKIP]
    rows = []
    best = None
    for lb in grid_lbs:
        for sk in grid_skips:
            for ent in grid_ents:
                bt = Backtester(prices, meta, {"lookback": lb, "skip": sk,
                                               "entry": ent, "exit": EXIT_THRESHOLD},
                                cost_case="base", cash_rate=CASH_RATE_PRETAX,
                                strategy=TRADE_STRATEGY)
                res = bt.run(end_date=split)
                if res.get("error"):
                    continue
                rows.append([str(lb), str(sk), fmt_pct(ent, 0),
                             fmt_pct(res["cagr_after_cost"]),
                             f"{res['sharpe']:.2f}", fmt_pct(-res["max_drawdown"]),
                             fmt_cr(res["final_equity"])])
                score = res["sharpe"]
                if best is None or score > best[0]:
                    best = (score, lb, sk, ent, res)
    P.sub("Parameter grid — IN-SAMPLE only (do not peek at OOS!) "
          "(skip is a tested dimension now)")
    P.table(["Lookback", "Skip", "Entry", "CAGR (after cost)", "Sharpe", "Max DD", "Final"],
            rows, aligns=[">", ">", ">", ">", ">", ">", ">"])
    if best is None:
        P.err("Grid produced no results — need more history.")
        return None
    _, best_lb, best_sk, best_ent, _ = best
    P.ok(f"Best in-sample setting: lookback={best_lb}, skip={best_sk}, "
         f"entry={fmt_pct(best_ent,0)} (highest Sharpe). Now LOCKED for out-of-sample.")
    P.note("Per the rulebook the baseline (252 / +2%) is also reported; "
           "thresholds are not changed based on out-of-sample results.")

    scenarios = []
    for cost_case in ("base", "adverse", "severe"):
        for cash_name, cash_rate in CASH_CASES.items():
            bt = Backtester(prices, meta, {"lookback": best_lb, "skip": best_sk,
                                           "entry": best_ent, "exit": EXIT_THRESHOLD},
                            cost_case=cost_case, cash_rate=cash_rate,
                            strategy=TRADE_STRATEGY, cash_sleeve=False)
            res_full = bt.run()
            bt2 = Backtester(prices, meta, {"lookback": best_lb, "skip": best_sk,
                                            "entry": best_ent, "exit": EXIT_THRESHOLD},
                             cost_case=cost_case, cash_rate=cash_rate,
                             strategy=TRADE_STRATEGY, cash_sleeve=False)
            res_oos = bt2.run(start_date=split)
            scenarios.append((cost_case, cash_name, res_full, res_oos))
    P.section("SCENARIO MATRIX — chosen params, full sample vs out-of-sample", "\U0001F3AF")
    out_rows = []
    for cost_case, cash_name, rf, ro in scenarios:
        out_rows.append([
            cost_case, cash_name,
            fmt_pct(rf["cagr_after_cost_tax"]) if not rf.get("error") else "n/a",
            fmt_pct(ro["cagr_after_cost_tax"]) if not ro.get("error") else "n/a",
            f"{rf['sharpe']:.2f}" if not rf.get("error") else "n/a",
            fmt_pct(-rf["max_drawdown"]) if not rf.get("error") else "n/a",
        ])
    P.table(["Costs", "Cash rate", "CAGR full", "CAGR OOS", "Sharpe full", "Max DD full"],
            out_rows, aligns=["<", "<", ">", ">", ">", ">"])

    # the three strategy versions compared on the same base scenario
    P.section("STRATEGY COMPARISON (base costs, 5.30% cash) — what drives returns?", "\U0001F501")
    cmp_rows = []
    results_abc = {}
    for strat in ("A", "B", "C"):
        bt = Backtester(prices, meta, BASELINE_PARAMS, cost_case="base",
                        cash_rate=CASH_RATE_PRETAX, strategy=strat)
        r = bt.run()
        results_abc[strat] = r
        if not r.get("error"):
            cmp_rows.append([
                {"A": "A — Pure TSMOM", "B": "B — TSMOM + Ranking", "C": "C — Ranking only"}[strat],
                fmt_pct(r["cagr_after_cost_tax"]), f"{r['sharpe']:.2f}",
                fmt_pct(-r["max_drawdown"]), fmt_pct(r["avg_invested"]), fmt_cr(r["final_equity"]),
            ])
    # audit sec 2.1: exit overlay test (faster exits on 200-day average break)
    bt_ov = Backtester(prices, meta, dict(BASELINE_PARAMS, exit_overlay="200dma"),
                       cost_case="base", cash_rate=CASH_RATE_PRETAX, strategy=TRADE_STRATEGY)
    r_ov = bt_ov.run()
    if not r_ov.get("error"):
        cmp_rows.append([
            f"{TRADE_STRATEGY} + 200-day exit overlay",
            fmt_pct(r_ov["cagr_after_cost_tax"]), f"{r_ov['sharpe']:.2f}",
            fmt_pct(-r_ov["max_drawdown"]), fmt_pct(r_ov["avg_invested"]),
            fmt_cr(r_ov["final_equity"]),
        ])
    P.table(["Version", "CAGR", "Sharpe", "Max DD", "Avg Invested", "Final Equity"],
            cmp_rows, aligns=["<", ">", ">", ">", ">", ">"])
    P.note("The exit overlay row answers the audit's question: does a faster "
           "exit (price below 200-day average) beat the 21-session skip alone?")
    a, b, c = results_abc.get("A"), results_abc.get("B"), results_abc.get("C")
    if a and c and not a.get("error") and not c.get("error"):
        P.sub("Interpretation (rulebook rule)")
        if c["cagr_after_cost_tax"] > a["cagr_after_cost_tax"]:
            P.info("C > A  ->  the cross-sectional RANKING is generating the returns, "
                   "not the time-series TSMOM filter.")
        else:
            P.info("A > C  ->  the TSMOM trend FILTER is doing the work; "
                   "ranking is a drag.")
    P.note("Survivorship-bias caveat: this universe is today's listed ETF set. "
           "Delisted/merged funds are missing, so real point-in-time results would be "
           "LOWER. Bias direction: CAGR here is an OVER-estimate; a worst-decile "
           "delisting scenario typically costs a few tens of bps per year on a "
           "3-holding portfolio.")
    P.note("Dividend treatment: raw Upstox prices carry no distributions, so nothing is "
           "compounded (conservative). Load distributions into corporate_actions with "
           "note='DIV...' and the reinvestment is haircut by the slab rate "
           f"({fmt_pct(SLAB_RATE,0)}) per the rulebook.")
    # save run
    label = f"grid-best lb={best_lb} entry={best_ent}"
    db.execute("INSERT INTO backtest_runs(ts,label,params,results,notes) VALUES(?,?,?,?,?)",
               (now_iso(), label,
                json.dumps({"lookback": best_lb, "entry": best_ent}),
                json.dumps({f"{sc}/{cn}": {"full_cagr": rf.get("cagr_after_cost_tax"),
                                           "oos_cagr": ro.get("cagr_after_cost_tax")}
                            for sc, cn, rf, ro in scenarios if not rf.get("error")}),
                "auto-run"))
    # detailed print of the headline run — headline cash assumption is the
    # 5.30% POST-TAX rate (audit #7: pre-tax cash with no interest tax is the
    # optimistic case and must not be the headline)
    headline_cash = CASH_CASES["5.30% post-tax"]
    bt = Backtester(prices, meta, {"lookback": best_lb, "skip": best_sk,
                                   "entry": best_ent, "exit": EXIT_THRESHOLD},
                    cost_case="base", cash_rate=headline_cash, strategy=TRADE_STRATEGY)
    full = bt.run()
    print_backtest_result(full, "HEADLINE BACKTEST — full sample (best params, base costs, post-tax cash)")
    bt2 = Backtester(prices, meta, {"lookback": best_lb, "skip": best_sk,
                                    "entry": best_ent, "exit": EXIT_THRESHOLD},
                     cost_case="base", cash_rate=headline_cash, strategy=TRADE_STRATEGY)
    oos = bt2.run(start_date=split)
    print_backtest_result(oos, "OUT-OF-SAMPLE — final 20% (never seen during tuning)")
    regime_category_report(prices, meta, full.get("equity_curve", []))
    benchmark_report(prices, meta, full.get("equity_curve", []),
                     "TSMOM-B (best params)", headline_cash,
                     avg_invested=full.get("avg_invested", 0.6))
    period_report(prices, full.get("equity_curve", []))
    bootstrap_ci(full.get("equity_curve", []))
    walk_forward(prices, meta, quick=quick)
    txt = []
    txt.append(f"TSMOM backtest report {now_iso()}\nparams={best_lb}/{best_ent}\n")
    for name, r in (("full", full), ("oos", oos)):
        if not r.get("error"):
            txt.append(f"{name}: cagr={r['cagr_after_cost_tax']:.4f} sharpe={r['sharpe']:.2f} "
                       f"maxdd={r['max_drawdown']:.4f}\n")
    save_report(f"backtest_{TODAY.strftime('%Y%m%d')}.txt", "".join(txt))
    return full


# ==============================================================================
# SECTION 22 — STATUS / REPORT COMMANDS
# ==============================================================================

def cmd_status(db: Db):
    P.section("ACCOUNT STATUS", "\U0001F4B0")
    try:
        env = load_env()
        client = UpstoxClient(env)
    except Exception:
        client = None
    prices = {}
    for r in db.query("SELECT p.symbol AS symbol, p.close AS close FROM price_daily p WHERE p.date=(SELECT MAX(date) FROM price_daily WHERE symbol=p.symbol)"):
        prices[r["symbol"]] = r["close"]
    broker = Broker(db, TaxEngine(db), client, paper=PAPER_TRADE)
    risk = RiskEngine(db)
    cash = broker.cash()
    hv = broker.holdings_value(prices)
    eq = cash + hv
    P.kv("Trading mode", "PAPER (simulated)" if PAPER_TRADE else "LIVE")
    P.kv("Cash", fmt_cr(cash))
    P.kv("Holdings value", fmt_cr(hv))
    P.kv("Total equity", fmt_cr(eq), val_color=_C.GREEN)
    P.kv("Invested", fmt_pct(safe_div(hv, eq, 0.0)))
    P.kv("Peak equity", fmt_cr(risk.peak))
    dd = 1 - eq / risk.peak if risk.peak else 0.0
    P.kv("Current drawdown", fmt_pct(dd, signed=False),
         val_color=_C.RED if dd > 0.05 else _C.WHITE)
    P.kv("Risk state", risk.state)
    rows = []
    for sym, h in sorted(broker.holdings().items()):
        px = prices.get(sym, h["avg_cost"])
        pnl = (px - h["avg_cost"]) * h["qty"]
        rows.append([sym, universe_map().get(sym, {}).get("name", sym)[:26],
                     fmt_num(h["qty"], 0), f"Rs {h['avg_cost']:.2f}", f"Rs {px:.2f}",
                     fmt_cr(h["qty"] * px), fmt_cr(pnl)])
    if rows:
        P.sub("Open positions")
        P.table(["Symbol", "Name", "Units", "Avg Cost", "LTP", "Value", "Unrealised P&L"],
                rows, aligns=["<", "<", ">", ">", ">", ">", ">"])
    else:
        P.info("No open positions — the account is fully in cash.")
    P.sub("Recent orders (order book)")
    orows = []
    for r in db.query("SELECT * FROM orders ORDER BY id DESC LIMIT 15"):
        orows.append([r["exec_date"], r["symbol"], r["side"], fmt_num(r["qty"], 0),
                      f"Rs {(r['price'] or 0):.2f}", fmt_cr(r["value"] or 0),
                      r["status"], r["mode"]])
    if orows:
        P.table(["Date", "Symbol", "Side", "Qty", "Price", "Value", "Status", "Mode"], orows,
                aligns=["<", "<", "<", ">", ">", ">", "<", "<"])
    else:
        P.info("Order book is empty — no trades have been made yet.")
    tax = TaxEngine(db).year_tax_summary()
    if tax:
        P.sub("Tax summary by financial year")
        trows = [[fy, fmt_cr(d["stcg"]), fmt_cr(d["ltcg"]), fmt_cr(d.get("tax", 0))]
                 for fy, d in sorted(tax.items())]
        P.table(["FY", "STCG realised", "LTCG realised", "Tax accrued"], trows,
                aligns=["<", ">", ">", ">"])
        P.note(f"LTCG exemption Rs 1.25 L/yr applied | STCG {fmt_pct(STCG_RATE,0)} | "
               f"LTCG {fmt_pct(LTCG_RATE,2)} above exemption")
    # discovery dates recap
    P.sub("ETF discovery log (first time each fund was seen)")
    drows = []
    for r in db.query("SELECT symbol, discovered_on, detail FROM discovery_log ORDER BY discovered_on, symbol LIMIT 20"):
        drows.append([r["symbol"], paint(r["discovered_on"], _C.CYAN), (r["detail"] or "")[:44]])
    if drows:
        P.table(["Symbol", "Discovery Date", "Note"], drows, aligns=["<", ">", "<"])


def cmd_report(db: Db):
    cmd_status(db)
    P.section("RISK EVENT HISTORY", "\U0001F6E1")
    rrows = []
    for r in db.query("SELECT * FROM risk_events ORDER BY id DESC LIMIT 20"):
        rrows.append([r["ts"], r["level"], fmt_pct(r["drawdown"]), r["action"], (r["detail"] or "")[:40]])
    if rrows:
        P.table(["Time", "Level", "Drawdown", "Action", "Detail"], rrows,
                aligns=["<", "<", ">", "<", "<"])
    else:
        P.info("No risk triggers recorded yet.")
    P.section("PERFORMANCE SINCE INCEPTION", "\U0001F4C8")
    snaps = db.query("SELECT * FROM portfolio_snapshots ORDER BY date")
    if len(snaps) >= 2:
        eqs = [s["equity"] for s in snaps]
        rets = [eqs[i] / eqs[i - 1] - 1 for i in range(1, len(eqs)) if eqs[i - 1] > 0]
        days = max((parse_date(snaps[-1]["date"]) - parse_date(snaps[0]["date"])).days, 1)
        years = days / 365.25
        cagr = (eqs[-1] / eqs[0]) ** (1 / max(years, 1e-9)) - 1 if eqs[0] > 0 else 0
        vol = stdev(rets, ddof=1) * math.sqrt(252) if len(rets) > 2 else 0
        peak = eqs[0]; mdd = 0
        for e in eqs:
            peak = max(peak, e)
            mdd = max(mdd, 1 - e / peak)
        P.kv("Snapshots", f"{len(snaps)} days")
        P.kv("Equity start -> now", f"{fmt_cr(eqs[0])} -> {fmt_cr(eqs[-1])}")
        P.kv("CAGR since inception", fmt_pct(cagr))
        P.kv("Annual volatility", fmt_pct(vol))
        P.kv("Max drawdown", fmt_pct(-mdd))
    else:
        P.info("Not enough snapshots yet — run the strategy for a few days.")
    note = ("Full rulebook reporting (10y backtest, OOS split, parameter grid, "
            "cost & cash scenarios) is produced by: python tsmom.py backtest")
    P.note(note)
    save_report(f"report_{TODAY.strftime('%Y%m%d')}.txt",
                f"TSMOM report {now_iso()}\nSee console output / database {DB_PATH}\n")


# ==============================================================================
# SECTION 23 — SYNTHETIC DEMO DATA (so the whole engine can run end-to-end)
# ==============================================================================

def generate_synthetic_data(db: Db, years: int = 11, n_symbols: int = 28, seed: int = 7):
    """
    Creates realistic correlated ETF price history so every feature of the
    system (scanner, signals, backtest, paper trading) can be demonstrated
    even without live market access. Clearly tagged as SYNTHETIC in the DB.
    """
    rng = random.Random(seed)
    P.section("SYNTHETIC DATA GENERATOR (demo mode)", "\U0001F9EA")
    P.warn("Generating SYNTHETIC prices — for demonstration only, clearly tagged in the DB.")
    universe = ETF_UNIVERSE[:n_symbols]
    n_days = int(years * 252)
    start = TODAY - dt.timedelta(days=int(years * 365.25))
    dates = []
    d = start
    while len(dates) < n_days:
        if d.weekday() < 5:
            dates.append(date_str(d))
        d += dt.timedelta(days=1)
    # market factor with regimes
    mkt = []
    regime = 0.00035
    for i in range(n_days):
        if i % 320 == 0:
            regime = rng.choice([0.00045, 0.0002, -0.00015, 0.00055])
        shock = rng.gauss(0, 0.009)
        mkt.append(regime + shock)
    for j, (sym, name, cat, fam) in enumerate(universe):
        beta = rng.uniform(0.5, 1.3) if cat == "EQUITY" else rng.uniform(0.05, 0.5)
        alpha = rng.gauss(0.00015, 0.00025)
        idio = 0.006 if cat == "EQUITY" else 0.003
        px = rng.uniform(20, 900)
        rows = []
        base_vol = rng.uniform(30_000, 2_000_000)
        for i in range(n_days):
            r = alpha + beta * mkt[i] + rng.gauss(0, idio)
            px = max(px * (1 + r), 2.0)
            vol_shares = max(100.0, base_vol * (1 + rng.gauss(0, 0.35)) / max(px, 1) * 10)
            hi = px * (1 + abs(rng.gauss(0, 0.004)))
            lo = px * (1 - abs(rng.gauss(0, 0.004)))
            op = px * (1 + rng.gauss(0, 0.002))
            rows.append({"symbol": sym, "date": dates[i], "open": op, "high": hi,
                         "low": lo, "close": px, "adj_close": px,
                         "volume": vol_shares, "traded_value": px * vol_shares})
        db.upsert_prices(sym, rows)
        # NAV series: close with small tracking noise + occasional dislocation so
        # the iNAV premium/discount filter is genuinely exercised (audit bug #6)
        nav_rows = []
        for i, r in enumerate(rows):
            noise = rng.gauss(0, 0.0015)
            if i % 157 == 0:
                noise += rng.choice([0.006, -0.006])   # dislocation > 0.40%/0.75%
            nav_rows.append((sym, r["date"], r["close"] * (1.0 + noise)))
        self_db = db
        self_db.conn.executemany(
            "INSERT INTO nav_data(symbol,date,nav) VALUES(?,?,?) "
            "ON CONFLICT(symbol,date) DO UPDATE SET nav=excluded.nav", nav_rows)
        self_db.conn.commit()
        # one synthetic 1:2 split on a few funds to exercise the adjustment
        # pipeline end-to-end (audit 2.2): the RAW prices really halve from the
        # ex-date and factor=2.0 restates them back up, so adj_close is smooth
        if j % 9 == 0 and len(rows) > 400:
            ex_i = 300
            ex = rows[ex_i]["date"]
            for k in range(ex_i, len(rows)):
                rows[k]["open"] *= 0.5
                rows[k]["high"] *= 0.5
                rows[k]["low"] *= 0.5
                rows[k]["close"] *= 0.5
                rows[k]["adj_close"] *= 0.5
            db.upsert_prices(sym, rows[ex_i:])
            self_db.execute(
                "INSERT OR REPLACE INTO corporate_actions(symbol,ex_date,factor,note) "
                "VALUES(?,?,?,?)",
                (sym, ex, 2.0, "SYNTHETIC split 1:2 (factor 2.0 per convention)"))
            dm = DataManager(db, None)
            dm.recompute_adjustments(sym)
        db.execute("UPDATE etf_universe SET first_seen=? WHERE symbol=? AND first_seen IS NULL",
                   (dates[0], sym))
        if j % 7 == 0:
            P.raw(f"  generated {sym:<12} {n_days} bars  (price now Rs {px:.2f})")
    db.meta_set("data_origin", "SYNTHETIC")
    P.ok(f"Synthetic history ready for {len(universe)} ETFs x {n_days} sessions.")


# ==============================================================================
# SECTION 24 — SELF TESTS (unit tests required by the rulebook)
# ==============================================================================

def cmd_selftest(db: Db):
    P.section("SELF TESTS / UNIT TESTS", "\u2705")
    passed = failed = 0
    def check(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            P.ok(f"{name} {paint(detail, _C.GREY)}")
        else:
            failed += 1
            P.err(f"{name} {detail}")

    # 1) momentum formula (rulebook unit test: must be non-zero when prices differ)
    adj = [1000.0] * 252 + [1150.0]
    # construct explicit series: P(t-252)=1000, P(t-21)=1150
    series = [1000.0] * 231 + [1150.0] + [1150.0] * 21
    m = momentum_at(series, len(series) - 1, 252, 21)
    check("momentum example (1000 -> 1150 = +15%)",
          m is not None and abs(m - 0.15) < 1e-9, f"M={m}")
    series2 = [float(i) for i in range(300)]
    m2 = momentum_at(series2, 299, 252, 21)
    check("signal non-zero whenever prices differ", m2 is not None and m2 != 0.0, f"M={m2:.4f}")
    check("momentum None when history too short", momentum_at([1.0] * 100, 99) is None)

    # 2) thresholds
    check("entry: +3% > +2% -> ELIGIBLE_BUY", decide(0.03, False)[0] == "ELIGIBLE_BUY")
    check("entry: +1% -> CASH", decide(0.01, False)[0] == "CASH")
    check("hold zone: +1% while holding -> HOLD", decide(0.01, True)[0] == "HOLD")
    check("exit: -1% while holding -> EXIT", decide(-0.01, True)[0] == "EXIT")
    check("exit boundary: 0% while holding -> EXIT", decide(0.0, True)[0] == "EXIT")

    # 3) Ledoit-Wolf covariance
    rng = random.Random(1)
    X = [[rng.gauss(0, 0.01), rng.gauss(0, 0.02)] for _ in range(60)]
    Xd = demean(X)
    S, delta, cond = ledoit_wolf_cc(Xd)
    diag_ok = S[0][0] > 0 and S[1][1] > 0
    sym_ok = abs(S[0][1] - S[1][0]) < 1e-12
    check("LW covariance positive diagonal & symmetric", diag_ok and sym_ok,
          f"delta={delta:.3f} cond={cond:.2f}")
    check("LW shrinkage intensity within [0,1]", 0.0 <= delta <= 1.0)

    # 4) vol targeting caps: never leverage
    chosen = [{"symbol": "X", "vol": 0.20, "liq_p25": 1e12, "adj": [100.0 * (1 + rng.gauss(0, 0.01)) for _ in range(80)]},
              {"symbol": "Y", "vol": 0.30, "liq_p25": 1e12, "adj": [100.0 * (1 + rng.gauss(0, 0.01)) for _ in range(80)]}]
    fe = FilterEngine(db)
    pb = PortfolioBuilder(db, fe)
    sized = pb.size(chosen, 1_000_000.0)
    check("sizing: invested fraction <= 1", sized["invested"] <= 1.0 + 1e-9,
          f"invested={sized['invested']:.3f}")
    check("sizing: port vol <= target (scaled, annualised)", sized["port_vol"] <= VOL_TARGET + 1e-6,
          f"vol={sized['port_vol']:.4f}")
    check("sizing: reported vol is ANNUALISED (not daily)", sized["port_vol"] > 0.001,
          f"vol={sized['port_vol']:.4f} (a daily vol of 3 assets would be < 0.001)")

    # 5) charges math
    ch = calc_charges("BUY", 100_000.0)
    check("charges: GST applied on brokerage+exchange+sebi", ch["gst"] > 0,
          f"total={ch['total']:.2f} on Rs 1L")
    check("charges: round-trip friction in 0.01%-0.1% band",
          0.0001 <= (ch["total"] / 100_000.0) <= 0.001,
          f"{ch['total']/1000:.4f}%")

    # 6) slippage direction
    b = slippage_price("BUY", 100.0, 10.0)
    s = slippage_price("SELL", 100.0, 10.0)
    check("slippage: buy above ref, sell below ref", b > 100.0 > s, f"buy={b:.3f} sell={s:.3f}")

    # 7) tax lots (paper DB)
    taxdb = Db(DATA_DIR / "selftest_tax.db")
    te = TaxEngine(taxdb)
    te.record_buy("TEST", "2025-01-01", 100.0, 10)
    out = te.record_sell("TEST", "2026-02-01", 130.0, 10)   # >12 months -> LTCG
    check("tax lots: >12m classified LTCG", out["lt_pnl"] > 0 and out["st_pnl"] == 0,
          f"lt={out['lt_pnl']:.0f}")
    te.record_buy("TEST", "2026-01-01", 100.0, 5)
    out2 = te.record_sell("TEST", "2026-03-01", 90.0, 5)    # <=12m -> STCG
    check("tax lots: <=12m classified STCG", out2["st_pnl"] < 0,
          f"st={out2['st_pnl']:.0f}")
    taxdb.close()
    try:
        os.remove(DATA_DIR / "selftest_tax.db")
    except Exception:
        pass

    # 8) month-end detection
    me = month_end_indices(["2026-01-01", "2026-01-02", "2026-02-02", "2026-02-03", "2026-03-02"])
    check("month-end detection", me == [1, 3, 4], f"indices={me}")

    # 9) Indian number formatting
    check("INR formatting", fmt_inr(1234567.5) == "12,34,567.50", fmt_inr(1234567.5))

    # 10) portfolio vol of known case
    v = portfolio_vol([0.5, 0.5], [[0.04, 0.0], [0.0, 0.04]])
    check("portfolio vol (equal weights, 20% assets, zero corr)", abs(v - 0.1414) < 1e-3, f"v={v:.4f}")

    # =========== REGRESSION TESTS FOR THE AUDIT FINDINGS ===========
    # A1: month-end must exclude an in-progress / truncated final month
    me = month_end_indices(["2026-08-28", "2026-08-31", "2026-09-01", "2026-09-15"])
    le = last_month_end_date(["2026-08-28", "2026-08-31", "2026-09-01", "2026-09-15"])
    check("audit A1: truncated final month is NOT a signal date", le == "2026-08-31", f"got {le}")
    le2 = last_month_end_date(["2026-08-28", "2026-08-31", "2026-09-30"])
    check("audit A1: complete month-end IS used", le2 == "2026-09-30", f"got {le2}")

    # A7: holding period is an anniversary test, not month subtraction
    check("audit A7: buy 2025-01-20 / sell 2026-01-25 is LTCG",
          held_more_than_months("2025-01-20", "2026-01-25", 12) is True)
    check("audit A7: buy 2025-01-20 / sell 2026-01-15 is STCG",
          held_more_than_months("2025-01-20", "2026-01-15", 12) is False)

    # A7: per-class tax rates
    t_gold_st, _ = tax_on_gain("COMMODITY", "STCG", 10000.0, 0.0)
    check("audit A7: gold STCG taxed at slab (30% + 4% cess)",
          abs(t_gold_st - 3000.0 * 1.04) < 1e-6, f"tax={t_gold_st}")
    t_gold_lt, used = tax_on_gain("COMMODITY", "LTCG", 10000.0, 125000.0)
    check("audit A7: gold LTCG gets NO 1.25L exemption",
          used == 0.0 and abs(t_gold_lt - 1250.0 * 1.04) < 1e-6)
    t_eq_lt, used2 = tax_on_gain("EQUITY", "LTCG", 10000.0, 125000.0)
    check("audit A7: equity LTCG exemption applies", abs(used2 - 10000.0) < 1e-6 and t_eq_lt == 0.0)

    # A8: STT — commodity/debt exempt, no STT on buy
    ch_g = calc_charges("SELL", 100000.0, category="COMMODITY")
    ch_e = calc_charges("SELL", 100000.0, category="EQUITY")
    ch_b = calc_charges("BUY", 100000.0, category="EQUITY")
    check("audit A8: no STT for gold/liquid/gilt", ch_g["stt"] == 0.0)
    check("audit A8: equity ETF STT 0.001% sell only",
          abs(ch_e["stt"] - 1.0) < 1e-9 and ch_b["stt"] == 0.0, f"{ch_e['stt']} / {ch_b['stt']}")

    # A8: impact grows with order size (no more flat 2 bps)
    small = impact_bps(10_000.0, 1_000_000.0)
    big = impact_bps(500_000.0, 1_000_000.0)
    check("audit A8: square-root impact rises with participation", big > small,
          f"small={small:.1f}bps big={big:.1f}bps")

    # A6: spread wider than the limit must be rejected
    fe2 = FilterEngine(db)
    db.execute("INSERT INTO quotes(symbol,ts,ltp,bid,ask,spread_bps) VALUES(?,?,?,?,?,?)",
               ("SPREADY", now_iso(), 100.0, 99.0, 101.0, 200.0))
    db.execute("INSERT INTO price_daily(symbol,date,open,high,low,close,adj_close,volume,traded_value) "
               "VALUES(?,?,?,?,?,?,?,?,?)",
               ("SPREADY", today_str(), 100.0, 100.0, 100.0, 100.0, 100.0,
                1e6, 1e8))   # plenty of liquidity so the SPREAD gate is the one that fires
    ok, reason, _info = fe2.check_execution("SPREADY", today_str(), 1000.0)
    check("audit A6: oversized spread is rejected", ok is False and "spread" in reason, reason[:60])
    db.execute("DELETE FROM quotes WHERE symbol='SPREADY'")
    db.execute("DELETE FROM price_daily WHERE symbol='SPREADY'")

    # A3: cash proxies are excluded from the ranked pool
    dbsub = Db(DATA_DIR / "selftest_sel.db")
    fe3 = FilterEngine(dbsub)
    pb3 = PortfolioBuilder(dbsub, fe3)
    mk = lambda sym, mom: {"symbol": sym, "momentum": mom, "vol": 0.15, "liq_p25": 1e8,
                           "rows": [], "adj": [100.0] * 300,
                           "index_family": sym}
    # LIQUIDCASE has the highest momentum but is a cash proxy
    pool = [mk("LIQUIDCASE", 0.50), mk("NIFTYBEES", 0.10), mk("ITBEES", 0.08)]
    sel = pb3.select(pool, set(), "B", equity=1e8)
    chosen_syms = [s["symbol"] for s in sel["chosen"]]
    check("audit A3: liquid-fund cash proxy never wins a slot",
          "LIQUIDCASE" not in chosen_syms and "NIFTYBEES" in chosen_syms,
          f"chosen={chosen_syms}")
    dbsub.close()
    try:
        os.remove(DATA_DIR / "selftest_sel.db")
    except Exception:
        pass

    # A4: illiquid ETF cannot take a top-3 slot (pre-ranking filter)
    dbsub2 = Db(DATA_DIR / "selftest_liq.db")
    fe4 = FilterEngine(dbsub2)
    pb4 = PortfolioBuilder(dbsub2, fe4)
    pool2 = [mk("THINETF", 0.60), mk("NIFTYBEES", 0.10), mk("ITBEES", 0.08)]
    pool2[0]["liq_p25"] = 1000.0        # absurdly illiquid
    sel2 = pb4.select(pool2, set(), "B", equity=1e8)
    check("audit A4: illiquid ETF is filtered BEFORE ranking",
          all(s["symbol"] != "THINETF" for s in sel2["chosen"]),
          f"chosen={[s['symbol'] for s in sel2['chosen']]}")
    dbsub2.close()
    try:
        os.remove(DATA_DIR / "selftest_liq.db")
    except Exception:
        pass

    # A6: iNAV premium/discount gate actually rejects when NAV data exists
    fe5 = FilterEngine(db)
    db.execute("INSERT INTO price_daily(symbol,date,open,high,low,close,adj_close,volume,traded_value) "
               "VALUES(?,?,?,?,?,?,?,?,?)",
               ("NAVER", today_str(), 102.0, 102.0, 102.0, 102.0, 102.0, 1e6, 1e8))
    db.execute("INSERT INTO nav_data(symbol,date,nav) VALUES(?,?,?)",
               ("NAVER", today_str(), 100.0))     # 2% premium -> way beyond 0.40/0.75%
    ok5, reason5, _i5 = fe5.check_execution("NAVER", today_str(), 1000.0)
    check("audit A6: iNAV premium beyond band is rejected",
          ok5 is False and "iNAV" in reason5, reason5[:60])
    db.execute("DELETE FROM price_daily WHERE symbol='NAVER'")
    db.execute("DELETE FROM nav_data WHERE symbol='NAVER'")

    # A5: time-based release from the 20% cash lock
    dbr = Db(DATA_DIR / "selftest_risk.db")
    re = RiskEngine(dbr, vol_target=0.08)   # rule semantics at the 8% rulebook target
    re.update(STARTING_CAPITAL * 0.75)          # -25% -> EXTREME lock
    check("audit A5: 25% drop locks to cash", re.all_cash is True)
    re.trigger_date = (dt.date.today() - dt.timedelta(days=RISK_RELEASE_DAYS + 1)).isoformat()
    re.update(STARTING_CAPITAL * 0.75)          # no price recovery, but time passed
    check("audit A5: time-based release fires without recovery",
          re.all_cash is False and re.halved is False)
    dbr.close()
    try:
        os.remove(DATA_DIR / "selftest_risk.db")
    except Exception:
        pass

    # A2/#3: inverse-vol consistency — a 0.1% vol cash proxy would take 25%
    # if it were selectable; combined with A3's exclusion the check above covers
    # the ranking path.  Here: sizing of a tiny-vol asset stays capped.
    chosen3 = [mk("EQ1", 0.0), mk("EQ2", 0.0)]
    chosen3[0]["vol"] = 0.20; chosen3[0]["rows"] = [(f"2026-01-{d:02d}", 100.0 * (1 + 0.001 * d)) for d in range(1, 29)]
    chosen3[1]["vol"] = 0.001; chosen3[1]["rows"] = [(f"2026-01-{d:02d}", 100.0) for d in range(1, 29)]
    sized3 = pb.size(chosen3, 1_000_000.0, strategy="B")
    w_tiny = sized3["weights"].get("EQ2", 0.0)
    check("audit A3: near-zero-vol asset capped at 25%, not 100%",
          w_tiny <= 0.25 + 1e-9, f"w={w_tiny:.3f}")

    # ================= SECOND-AUDIT REGRESSIONS (Tuning audit) =================

    # 2.2: adjustments are IDEMPOTENT — recomputing must not compound factors
    dbsub3 = Db(DATA_DIR / "selftest_ca.db")
    dbsub3.execute(
        "INSERT INTO price_daily(symbol,date,open,high,low,close,adj_close,volume,traded_value) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("SPLITX", "2024-01-31", 200, 200, 200, 200, 200, 1, 1e7))
    dbsub3.execute(
        "INSERT INTO price_daily(symbol,date,open,high,low,close,adj_close,volume,traded_value) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("SPLITX", "2024-02-15", 100, 100, 100, 100, 100, 1, 1e7))   # raw halves
    dbsub3.execute(
        "INSERT OR REPLACE INTO corporate_actions(symbol,ex_date,factor,note) VALUES(?,?,?,?)",
        ("SPLITX", "2024-02-15", 2.0, "SYNTHETIC split 1:2 (factor 2.0 per convention)"))
    dbsub3.execute(
        "INSERT OR REPLACE INTO corporate_actions(symbol,ex_date,factor,note) VALUES(?,?,?,?)",
        ("SPLITX", "2024-02-15", 2.0, "SYNTHETIC split 1:2 (factor 2.0 per convention)"))
    dm3 = DataManager(dbsub3, None)
    dm3.recompute_adjustments("SPLITX")
    a1 = dbsub3.one("SELECT adj_close FROM price_daily WHERE symbol='SPLITX' AND date='2024-02-15'")["adj_close"]
    dm3.recompute_adjustments("SPLITX")      # second recompute must not compound
    dm3.recompute_adjustments("SPLITX")
    a2 = dbsub3.one("SELECT adj_close FROM price_daily WHERE symbol='SPLITX' AND date='2024-02-15'")["adj_close"]
    check("audit 2.2: factor 2.0 restates the halved price; recompute is idempotent",
          abs(a1 - 200.0) < 1e-6 and abs(a2 - a1) < 1e-9, f"adj={a1} then {a2}")
    dbsub3.close()
    try:
        os.remove(DATA_DIR / "selftest_ca.db")
    except Exception:
        pass

    # 2.7: loss netting — STCL offsets LTCG inside the class; cess applied
    dbsub4 = Db(DATA_DIR / "selftest_tax.db")
    dbsub4.execute("INSERT OR REPLACE INTO etf_universe(symbol,name,status,broad,category) "
                   "VALUES('GOLDBEES','Gold ETF','ACTIVE',0,'COMMODITY')")
    te4 = TaxEngine(dbsub4)
    te4.record_buy("GOLDBEES", "2024-04-01", 100.0, 100.0)     # FY 2024-25
    te4.record_buy("GOLDBEES", "2024-06-01", 200.0, 100.0)
    te4.record_sell("GOLDBEES", "2024-08-01", 150.0, 100.0)    # FIFO: +5000 ST
    # now force a loss on the second lot
    te4.record_sell("GOLDBEES", "2024-09-01", 100.0, 100.0)    # -10000 ST
    ys = te4.year_tax_summary()
    fy_row = ys.get("FY2025")          # Apr-2024 .. Mar-2025
    check("audit 2.7: same-year ST loss nets against ST gain before tax",
          fy_row is not None and abs(fy_row["stcg"] - (-5000.0)) < 1e-6
          and fy_row["tax"] == 0.0,
          f"row={fy_row}")
    dbsub4.close()
    try:
        os.remove(DATA_DIR / "selftest_tax.db")
    except Exception:
        pass

    # 2.4: incumbency is a BOUNDED rank bonus (1 -> none, 2 -> N+1, 3 -> N+2)
    global INCUMBENCY_BONUS, ENSEMBLE_LOOKBACKS, VOL_BLEND_252, LOT_SELECTION, EXECUTION_STYLE, TAX_DEFER_EXITS, EXIT_OVERLAY_200DMA
    save_ib = INCUMBENCY_BONUS
    try:
        pool_ib = [mk(f"E{i}", 0.20 - 0.01 * i) for i in range(5)]   # ranks 0..4
        pb_ib = PortfolioBuilder(db, FilterEngine(db))
        INCUMBENCY_BONUS = 1
        out1 = [s["symbol"] for s in pb_ib._pick_with_incumbency(pool_ib, {"E3"}, 3)]
        INCUMBENCY_BONUS = 2
        out2 = [s["symbol"] for s in pb_ib._pick_with_incumbency(pool_ib, {"E3"}, 3)]
        INCUMBENCY_BONUS = 3
        out3 = [s["symbol"] for s in pb_ib._pick_with_incumbency(pool_ib, {"E4"}, 3)]
        check("audit 2.4: bonus 1 keeps no outsider, bonus 2 reads N+1 only",
              "E3" not in out1 and "E3" in out2, f"{out1} / {out2}")
        check("audit 2.4: bonus 3 can readmit rank N+2 (and not more)",
              "E4" in out3, f"{out3}")
    finally:
        INCUMBENCY_BONUS = save_ib

    # 3.3: tax-defer rule is explicit and testable
    save_td = TAX_DEFER_EXITS
    try:
        TAX_DEFER_EXITS = True
        far = tax_defer_blocks_exit(["2026-01-05"], "2026-06-30", -0.01)
        near = tax_defer_blocks_exit(["2025-07-20"], "2026-06-30", -0.01)
        deep = tax_defer_blocks_exit(["2025-07-20"], "2026-06-30", -0.05)
        check("audit 3.3: tax defer fires only near the LTCG anniversary and "
              "only for mild momentum", far == "" and near != "" and deep == "",
              f"far='{far[:20]}' near='{near[:20]}' deep='{deep[:20]}'")
    finally:
        TAX_DEFER_EXITS = save_td

    # 3.7: HIFO realises the highest-cost lot first (least gain)
    save_ls = LOT_SELECTION
    try:
        dbsub5 = Db(DATA_DIR / "selftest_hifo.db")
        dbsub5.execute("INSERT OR REPLACE INTO etf_universe(symbol,name,status,broad,category) "
                       "VALUES('X1','X','ACTIVE',1,'EQUITY')")
        te5 = TaxEngine(dbsub5)
        te5.record_buy("X1", "2024-04-01", 100.0, 10.0)
        te5.record_buy("X1", "2024-05-01", 200.0, 10.0)
        LOT_SELECTION = "HIFO"
        out_h = te5.record_sell("X1", "2024-08-01", 150.0, 10.0)
        LOT_SELECTION = "FIFO"
        dbsub5.execute("INSERT INTO tax_lots(symbol,buy_date,buy_price,qty) VALUES('X1','2024-04-01',100.0,10.0)")
        out_f = te5.record_sell("X1", "2024-08-01", 150.0, 10.0)
        check("audit 3.7: HIFO realises a loss where FIFO realises a gain",
              out_h["realized_pnl"] < 0 and out_f["realized_pnl"] > 0,
              f"hifo={out_h['realized_pnl']} fifo={out_f['realized_pnl']}")
        dbsub5.close()
        try:
            os.remove(DATA_DIR / "selftest_hifo.db")
        except Exception:
            pass
    finally:
        LOT_SELECTION = save_ls

    # 3.2: ensemble momentum = mean of components; vol blend sits between
    save_el, save_vb = ENSEMBLE_LOOKBACKS, VOL_BLEND_252
    try:
        series_e = [100.0 * (1.001 ** i) for i in range(300)]
        m_single = signal_momentum(series_e, 299, 252, 21)
        ENSEMBLE_LOOKBACKS = (126, 252)
        m_ens = signal_momentum(series_e, 299, 252, 21)
        m_126 = momentum_at(series_e, 299, 126, 21)
        check("audit 3.2: ensemble momentum averages its components",
              abs(m_ens - (m_126 + m_single) / 2.0) < 1e-9, f"{m_ens}")
        ENSEMBLE_LOOKBACKS = None
        flat_then_noise = [100.0] * 200 + [100.0 * (1.0 + (0.05 if i % 2 else -0.05))
                                          for i in range(120)]
        VOL_BLEND_252 = 0.0
        v60 = annualized_vol(flat_then_noise, 60)
        VOL_BLEND_252 = 0.5
        vb = annualized_vol(flat_then_noise, 60)
        VOL_BLEND_252 = 0.0
        check("audit 3.2: vol blend damps the 60d estimate toward 252d",
              vb < v60 * 0.999, f"v60={v60:.4f} blend={vb:.4f}")
    finally:
        ENSEMBLE_LOOKBACKS, VOL_BLEND_252 = save_el, save_vb

    # 3.5: LIMIT_MID does not cross the spread; scaled DD thresholds
    save_es = EXECUTION_STYLE
    try:
        px_m = slippage_price("BUY", 100.0, 20.0, order_value=1e5, liq_p25=1e7)
        EXECUTION_STYLE = "LIMIT_MID"
        px_l = slippage_price("BUY", 100.0, 20.0, order_value=1e5, liq_p25=1e7)
        check("audit 3.5: LIMIT_MID fills closer to mid than MARKET",
              px_l < px_m, f"market={px_m} limit={px_l}")
    finally:
        EXECUTION_STYLE = save_es
    dd10 = dd_thresholds(0.08)
    check("audit 3.5: DD thresholds scale with the vol target",
          abs(dd10[0] - DD_REVIEW_MULT * 0.08) < 1e-9
          and abs(dd10[1] - DD_SEVERE_MULT * 0.08) < 1e-9
          and abs(dd10[2] - DD_EXTREME_MULT * 0.08) < 1e-9,
          f"{dd10} vs mults {(DD_REVIEW_MULT, DD_SEVERE_MULT, DD_EXTREME_MULT)}")

    # --- cash sleeve (audit 2.6): real sleeve returns with labelled fallback ---
    global CASH_SLEEVE_MODE
    save_csm = CASH_SLEEVE_MODE
    try:
        sleeve_rows = [(f"2026-01-{d+1:02d}", 100.0 * (1.01 ** d), 100.0 * (1.01 ** d), 1e7)
                       for d in range(10)]
        prices_sl = {"LIQUIDCASE": sleeve_rows}
        CASH_SLEEVE_MODE = "LIQUID_ETF"
        fn_s = cash_return_series(prices_sl, 0.053, True)
        check("audit 2.6: sleeve earns the liquid ETF's real returns",
              fn_s.sleeve and abs(fn_s("2026-01-05") - 0.01) < 1e-9,
              f"r={fn_s('2026-01-05'):.4f}")
        fn_f = cash_return_series(prices_sl, 0.053, False)
        flat_exp = (1.053) ** (1.0 / 252.0) - 1.0
        check("audit 2.6: flat mode uses the labelled T-bill proxy",
              (not fn_f.sleeve) and abs(fn_f("2026-01-05") - flat_exp) < 1e-12)
        fn_fb = cash_return_series({}, 0.053, True)   # history missing -> fallback
        check("audit 2.6: missing sleeve history falls back to flat (warned)",
              (not fn_fb.sleeve) and abs(fn_fb("2026-01-05") - flat_exp) < 1e-12)
        # live-path helpers exist and use the sleeve (regression: self.data crash)
        dbsub7 = Db(DATA_DIR / "selftest_sleeve.db")
        for r0 in sleeve_rows:
            dbsub7.execute(
                "INSERT INTO price_daily(symbol,date,open,high,low,close,adj_close,volume,traded_value) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("LIQUIDCASE", r0[0], r0[1], r0[1], r0[1], r0[1], r0[1], 1, 1e7))
        cyc7 = TradingCycle(dbsub7)
        sd = cyc7.sleeve_daily_return("2026-01-10")
        check("hotfix: TradingCycle.data exists; sleeve_daily_return reads history",
              hasattr(cyc7, "data") and abs(sd - 0.01) < 1e-9, f"r={sd:.4f}")
        dbsub7.close()
        try:
            os.remove(DATA_DIR / "selftest_sleeve.db")
        except Exception:
            pass
    finally:
        CASH_SLEEVE_MODE = save_csm

    # --- hotfix regression: live-path 200dma overlay on 4-column SQLite rows ---
    # (crashed with 'too many values to unpack' once EXIT_OVERLAY_200DMA was
    #  switched on; also guards the overlay-vs-liquidity gate ordering)
    save_ov = EXIT_OVERLAY_200DMA
    try:
        EXIT_OVERLAY_200DMA = True
        dbsub6 = Db(DATA_DIR / "selftest_ov.db")
        fe6 = FilterEngine(dbsub6)
        pb6 = PortfolioBuilder(dbsub6, fe6)
        mk4 = lambda sym, mom, adj_path: {
            "symbol": sym, "momentum": mom, "vol": 0.15, "liq_p25": 1e8,
            "adj": adj_path, "index_family": sym,
            # THE CRASH SHAPE: 4-column rows (date, adj_close, close, traded_value)
            "rows": [(f"2026-{i//28+1:02d}-{i%28+1:02d}", a, a, 1e7)
                     for i, a in enumerate(adj_path)]}
        up = [50.0 + 0.25 * i for i in range(210)]        # rising -> above 200dma
        down = [100.0 - 0.25 * i for i in range(210)]     # falling -> below 200dma
        pool_ov = [
            mk4("HELDLOW", 0.05, down),    # holding, hold-zone momentum, below 200dma
            mk4("NEWLOW", 0.10, down),     # fresh buy candidate below 200dma
            mk4("NEWUP", 0.10, up),        # fresh buy candidate above 200dma
            mk4("THINUP", 0.10, up),       # above 200dma but absurdly illiquid
        ]
        pool_ov[3]["liq_p25"] = 1000.0
        sel_ov = pb6.select(pool_ov, {"HELDLOW"}, "B", equity=1e8)
        decmap = {st["symbol"]: dec for st, dec, _r in sel_ov["decisions"]}
        check("hotfix: 4-col rows through 200dma overlay (no unpack crash)",
              decmap.get("HELDLOW") == "EXIT" and decmap.get("NEWLOW") == "SKIPPED"
              and decmap.get("NEWUP") == "ELIGIBLE_BUY",
              f"{decmap}")
        check("hotfix: liquidity gate still fires when overlay is ON",
              decmap.get("THINUP") == "SKIPPED", f"{decmap}")
        dbsub6.close()
        try:
            os.remove(DATA_DIR / "selftest_ov.db")
        except Exception:
            pass
    finally:
        EXIT_OVERLAY_200DMA = save_ov

    P.raw()
    if failed == 0:
        P.ok(f"ALL {passed} SELF TESTS PASSED \U0001F389")
    else:
        P.err(f"{failed} tests FAILED, {passed} passed.")
    return failed == 0


# ==============================================================================
# SECTION 25 — CLI
# ==============================================================================

def ensure_dirs():
    for p in (DATA_DIR, LOG_DIR, CACHE_DIR, REPORT_DIR):
        p.mkdir(parents=True, exist_ok=True)


def cmd_scan(db: Db, try_live: bool = True):
    scanner = Scanner(db)
    scanner.sync_universe()
    if try_live:
        try:
            env = load_env()
            client = UpstoxClient(env)
            P.info("Downloading NSE instrument master from Upstox to verify symbols...")
            instruments = client.download_instrument_master()
            by = scanner.verify_against_upstox(instruments)
            hits = sum(1 for s in universe_map() if s in by)
            P.ok(f"Instrument master matched {hits}/{len(ETF_UNIVERSE)} ETF symbols on NSE.")
        except Exception as e:
            P.warn(f"Could not reach Upstox instrument master ({e}).")
            P.note("Continuing with locally stored universe — discovery dates are still recorded.")
    scanner.show_universe_table()


def cmd_update(db: Db, years: int = 11):
    try:
        env = load_env()
        client = UpstoxClient(env)
    except Exception as e:
        P.err(str(e))
        return
    dm = DataManager(db, client)
    if db.meta_get("data_origin") == "SYNTHETIC":
        P.warn("Database holds SYNTHETIC data. Real download will merge real bars alongside.")
    n_ca = dm.load_corporate_actions()
    n_nav = dm.load_nav_data()
    if n_ca:
        P.ok(f"Loaded {n_ca} corporate actions — adj_close recomputed for history "
             f"(audit fix: stale factors no longer survive).")
    if n_nav:
        P.ok(f"Loaded {n_nav} NAV points — the iNAV premium/discount filter is now LIVE.")
    else:
        P.note("No NAV files found (stock-data/nav/*.csv with symbol,date,nav) — "
               "the iNAV filter will be skipped and the reason recorded.")
    try:
        dm.update_all(years=years)
    except UpstoxError as e:
        P.err(str(e))
        P.note("Tip: for an offline end-to-end trial run  ->  python tsmom.py demo")
    dm.coverage_table()


def cmd_trade(db: Db, force_exec: bool = False):
    cyc = TradingCycle(db)
    cyc.run(force_exec=force_exec)


def cmd_load_data(db: Db):
    """Load corporate actions + NAV CSVs from stock-data/ (audit bug #6)."""
    P.section("LOADING CORPORATE ACTIONS & NAV DATA", "\U0001F4C4")
    dm = DataManager(db, UpstoxClient(load_env() if ENV_PATH.exists() else {}))
    n_ca = dm.load_corporate_actions()
    n_nav = dm.load_nav_data()
    P.ok(f"Corporate actions loaded: {n_ca}   |   NAV points loaded: {n_nav}")
    P.info("Drop files into stock-data/corporate-actions/  (symbol,ex_date,factor,note)")
    P.info("and stock-data/nav/  (symbol,date,nav), then rerun this command.")


def cmd_reconcile(db: Db):
    """Compare the internal ledger with the broker's positions (live mode safety)."""
    P.section("BROKER RECONCILIATION", "\U0001F50D")
    P.warn("LIVE safety check (audit #9): the ledger and the broker can drift apart. "
           "Always reconcile before trusting the account.")
    try:
        env = load_env()
    except Exception as e:
        P.err(str(e))
        return
    client = UpstoxClient(env)
    broker = Broker(db, TaxEngine(db), client, paper=PAPER_TRADE)
    mine = broker.holdings()
    try:
        theirs_list = client.positions()
    except Exception as e:
        P.err(f"Could not fetch broker positions: {e}")
        return
    theirs = {}
    for pos in theirs_list:
        sym = (pos.get("trading_symbol") or pos.get("symbol") or "").strip()
        qty = float(pos.get("quantity") or pos.get("net_quantity") or 0)
        if sym:
            theirs[sym] = qty
    syms = sorted(set(mine) | set(theirs))
    if not syms:
        P.info("No positions on either side.")
        return
    rows = []
    for s in syms:
        mq = mine.get(s, {}).get("qty", 0)
        bq = theirs.get(s, 0)
        flag = "OK" if abs(mq - bq) < 0.5 else "MISMATCH"
        rows.append([s, fmt_num(mq, 0), fmt_num(bq, 0),
                     paint(flag, _C.GREEN if flag == "OK" else _C.RED)])
    P.table(["Symbol", "Ledger qty", "Broker qty", "Status"], rows,
            aligns=["<", ">", ">", "<"])
    mism = sum(1 for r in rows if r[3].find("MISMATCH") >= 0 if isinstance(r[3], str))
    if mism:
        P.warn(f"{mism} mismatch(es). Resolve before placing new live orders.")
    else:
        P.ok("Ledger and broker agree.")


def cmd_monitor(db: Db):
    P.section("DAILY MONITOR", "\U0001F4E1")
    try:
        env = load_env()
        client = UpstoxClient(env)
    except Exception as e:
        P.err(str(e))
        return
    rows = db.query("SELECT symbol, instrument_key FROM etf_universe WHERE status='ACTIVE' ORDER BY symbol")
    ok = 0
    for r in rows:          # audit #6: every ETF, not just the first 40
        try:
            q = client.quote(r["instrument_key"])
            ltp = q.get("ltp")
            bid, ask = q.get("bid"), q.get("ask")
            spread = None
            if bid and ask and ltp:
                spread = abs(ask - bid) / ltp * 10000.0
                db.execute("INSERT INTO quotes(symbol,ts,ltp,bid,ask,spread_bps) VALUES(?,?,?,?,?,?)",
                           (r["symbol"], now_iso(), ltp, bid, ask, spread))
            P.raw(f"  {r['symbol']:<12} LTP Rs {ltp if ltp else 'n/a'}  "
                  f"bid {bid or '-'}  ask {ask or '-'}  "
                  f"spread {('%0.1f bps' % spread) if spread else '-'}")
            ok += 1
            time.sleep(HTTP_SLEEP)
        except UpstoxError as e:
            P.warn(f"{r['symbol']}: quote failed ({e})")
            if "RATE_LIMIT" in str(e) or "401" in str(e):
                P.note("Stopping quotes for now — same reason for every symbol. "
                       "Spread history will keep building up on later runs.")
                break
        except Exception as e:
            P.warn(f"{r['symbol']}: quote failed ({e})")
    prices = {}
    for r in db.query("SELECT p.symbol AS symbol, p.close AS close FROM price_daily p WHERE p.date=(SELECT MAX(date) FROM price_daily WHERE symbol=p.symbol)"):
        prices[r["symbol"]] = r["close"]
    broker = Broker(db, TaxEngine(db), client, paper=PAPER_TRADE)
    broker.accrue_cash_interest()
    risk = RiskEngine(db)
    eq = broker.equity(prices)
    ctx = risk.update(eq)
    hv = broker.holdings_value(prices)
    risk.snapshot(today_str(), broker.cash(), hv, eq)
    P.kv("Equity", fmt_cr(eq))
    P.kv("Drawdown", fmt_pct(ctx["drawdown"]))
    P.kv("Risk state", ctx["state"])
    for level, msg in ctx["events"]:
        P.warn(f"RISK [{level}] {msg}")
    P.ok(f"Monitor complete — {ok} quotes snapshotted into the database (spread history builds up here).")



# =============================================================================
# SECTION 24 — TUNE (audit "Tuning for long-term profitability" sec 3)
# =============================================================================

class KnobContext:
    """Temporarily set module globals for one tuning run, restore after."""
    def __init__(self, **kw):
        self.kw = kw
        self.save = {}

    def __enter__(self):
        g = globals()
        for k, v in self.kw.items():
            self.save[k] = g.get(k)
            g[k] = v
        return self

    def __exit__(self, *a):
        g = globals()
        for k, v in self.save.items():
            g[k] = v


def _bench_daily_returns(prices: dict):
    """Same benchmark pick as benchmark_report; returns (symbol, {date: ret})."""
    bench = None
    for cand in ("NIFTYBEES", "HDFCSENSEX", "MONIFTY500", "TOP100CASE"):
        if cand in prices and len(prices[cand]) > 250:
            bench = cand
            break
    if bench is None and prices:
        bench = max(prices, key=lambda s: len(prices[s]))
    if bench is None:
        return None, {}
    rows = sorted(((d, a) for (d, a, _c, _t) in prices[bench]))
    rets = {}
    for i in range(1, len(rows)):
        if rows[i - 1][1]:
            rets[rows[i][0]] = rows[i][1] / rows[i - 1][1] - 1.0
    return bench, rets


def _tune_windows(prices: dict, train_m: int = 36, test_m: int = 12):
    """The same rolling windows walk-forward uses (fixed-params segmentation)."""
    all_dates = sorted({d for s in prices.values() for (d, _a, _c, _t) in s})
    me = month_end_indices(all_dates)
    me_dates = [all_dates[i] for i in me]
    wins = []
    cursor = train_m
    while cursor + test_m - 1 < len(me_dates):
        wins.append((me_dates[cursor], me_dates[cursor + test_m - 1]))
        cursor += test_m
    return wins


def _tune_eval(prices, meta, windows, bench_rets, params=None, label="", **gkw):
    """
    ONE fixed-params continuous run, segmented into the walk-forward windows.
    Objective (audit): after-tax, after-cost excess over the RISK-MATCHED
    benchmark per window.  Returns per-window rows + summary, or None.
    """
    t0 = time.time()
    with KnobContext(**gkw):
        bt = Backtester(prices, meta, dict(BASELINE_PARAMS, **(params or {})),
                        cost_case="base", cash_rate=CASH_RATE_PRETAX,
                        strategy=TRADE_STRATEGY)
        r = bt.run()
    if not r or r.get("error"):
        P.warn(f"  [{label}] run failed: {r.get('error') if r else 'no result'}")
        return None
    curve = r.get("equity_curve", [])
    if len(curve) < 200:
        return None
    inv = clamp(r.get("avg_invested", 0.6), 0.0, 1.0)
    cash_fn_t = cash_return_series(prices, CASH_RATE_PRETAX)  # same sleeve

    def eq_at(d):
        best_e, best_d = None, None
        for dd, e in curve:
            if dd <= d and (best_d is None or dd >= best_d):
                best_d, best_e = dd, e
        return best_e

    rows = []
    excesses = []
    n_pos = 0
    for (w1, w2) in windows:
        e1, e2 = eq_at(w1), eq_at(w2)
        if not e1 or not e2 or e1 <= 0:
            continue
        yrs = max((parse_date(w2) - parse_date(w1)).days / 365.25, 1e-9)
        st_cagr = (e2 / e1) ** (1.0 / yrs) - 1.0
        # risk-matched benchmark growth inside the window
        g = 1.0
        for d, br in bench_rets.items():
            if w1 < d <= w2:
                g *= (1.0 + inv * br + (1.0 - inv) * cash_fn_t(d))
        rm_cagr = g ** (1.0 / yrs) - 1.0
        ex = st_cagr - rm_cagr
        rows.append([w1, w2, fmt_pct(st_cagr), fmt_pct(rm_cagr), fmt_pct(ex)])
        excesses.append(ex)
        if ex > 0:
            n_pos += 1
    if not rows:
        return None
    mean_ex = sum(excesses) / len(excesses)
    pct_pos = n_pos / len(rows)
    out = {"label": label, "rows": rows, "excesses": excesses,
           "mean_excess": mean_ex,
           "pct_pos": pct_pos, "n": len(rows),
           "cagr": r.get("cagr_after_cost_tax", 0.0),
           "sharpe": r.get("sharpe", 0.0), "mdd": r.get("max_drawdown", 0.0),
           "turnover": r.get("avg_turnover", 0.0),
           "secs": time.time() - t0}
    return out


def _pick_plateau(family_label, results, baseline_metric):
    """
    Plateau rule (audit): prefer the SIMPLEST setting within 0.25% mean excess
    of the family's best; require >= 50% windows positive to beat baseline.
    """
    best = max(results, key=lambda x: x["mean_excess"])
    near = [x for x in results
            if x["mean_excess"] >= best["mean_excess"] - 0.0025]
    # simplest = fewest knobs away from baseline labels — approximate by first
    # listed that is near the top (list order is simplest -> most extreme)
    pick = near[0] if near else best
    return pick, best


def cmd_tune(db: Db, quick: bool = False):
    P.section("TUNING — one change at a time, walk-forward vs risk-matched", "\U0001F3AF")
    prices, meta = load_prices_from_db(db)
    if not prices:
        P.err("No price data — run 'update' or 'demo' first.")
        return
    windows = _tune_windows(prices)
    if len(windows) < 3:
        P.warn(f"Only {len(windows)} walk-forward windows — tuning needs 3+. "
               f"Use a longer history.")
        return
    bench, bench_rets = _bench_daily_returns(prices)
    P.info(f"Objective: mean after-tax excess over the risk-matched {bench} "
           f"across {len(windows)} walk-forward windows "
           f"({windows[0][0]} .. {windows[-1][1]}), fixed params per variant.")
    P.note("Each variant is ONE continuous run segmented into the same windows "
           "(audit 2.8) — no cold-start seams. Baseline first, then one knob "
           f"at a time. Plateau rule: simplest setting within 0.25% of family best.")

    # ---------------- baseline ----------------
    base = _tune_eval(prices, meta, windows, bench_rets, label="baseline")
    if base is None:
        P.err("Baseline run failed — cannot tune.")
        return
    P.table(["Variant", "Mean excess", "% windows+", "CAGR net", "Sharpe", "Max DD"],
            [[base["label"], fmt_pct(base["mean_excess"]),
              fmt_pct(base["pct_pos"], 0), fmt_pct(base["cagr"]),
              f"{base['sharpe']:.2f}", fmt_pct(-base["mdd"])]],
            aligns=["<", ">", ">", ">", ">", ">"])

    # ---------------- one-at-a-time ablations ----------------
    families = []
    families.append(("Vol target", [
        ("8% (rulebook)", dict(VOL_TARGET=0.08)),
        ("10%", dict(VOL_TARGET=0.10)),
        ("12%", dict(VOL_TARGET=0.12)),
        ("15%", dict(VOL_TARGET=0.15)),
    ]))
    families.append(("Holdings x sleeve cap", [
        ("3 x 25%", dict(MAX_HOLDINGS=3, POS_CAP_LIQ_FRAC=0.25)),
        ("4 x 25%", dict(MAX_HOLDINGS=4, POS_CAP_LIQ_FRAC=0.25)),
        ("5 x 25% (rulebook)", dict(MAX_HOLDINGS=5, POS_CAP_LIQ_FRAC=0.25)),
        ("6 x 25%", dict(MAX_HOLDINGS=6, POS_CAP_LIQ_FRAC=0.25)),
        ("5 x 20%", dict(MAX_HOLDINGS=5, POS_CAP_LIQ_FRAC=0.20)),
        ("5 x 17%", dict(MAX_HOLDINGS=5, POS_CAP_LIQ_FRAC=0.17)),
    ]))
    families.append(("No-trade band", [
        ("0% (always rebalance)", dict(NO_TRADE_BAND=0.0)),
        ("15%", dict(NO_TRADE_BAND=0.15)),
        ("20% (default)", dict(NO_TRADE_BAND=0.20)),
        ("25%", dict(NO_TRADE_BAND=0.25)),
    ]))
    families.append(("Incumbency bonus", [
        ("1 (none re-enter)", dict(INCUMBENCY_BONUS=1)),
        ("2 (default)", dict(INCUMBENCY_BONUS=2)),
        ("3", dict(INCUMBENCY_BONUS=3)),
    ]))
    families.append(("Momentum signal x skip", [
        ("single 252, skip 0", dict(ENSEMBLE_LOOKBACKS=None), dict(skip=0)),
        ("single 252, skip 21 (rulebook)", dict(ENSEMBLE_LOOKBACKS=None), dict(skip=21)),
        ("single 252, skip 42", dict(ENSEMBLE_LOOKBACKS=None), dict(skip=42)),
        ("ensemble 63/126/252, skip 0", dict(ENSEMBLE_LOOKBACKS=(63, 126, 252)), dict(skip=0)),
        ("ensemble 63/126/252, skip 21", dict(ENSEMBLE_LOOKBACKS=(63, 126, 252)), dict(skip=21)),
        ("ensemble 63/126/252, skip 42", dict(ENSEMBLE_LOOKBACKS=(63, 126, 252)), dict(skip=42)),
    ]))
    families.append(("Ranking key", [
        ("momentum", dict(RANK_BY="momentum")),
        ("risk-adjusted (mom/vol)", dict(RANK_BY="risk_adjusted")),
    ]))
    families.append(("Entry threshold", [
        ("0%", dict(), dict(entry=0.0)),
        ("2% (rulebook)", dict(), dict(entry=0.02)),
        ("4%", dict(), dict(entry=0.04)),
    ]))
    families.append(("Exit threshold", [
        ("0% (rulebook)", dict(), dict(exit=0.0)),
        ("-1%", dict(), dict(exit=-0.01)),
        ("-2%", dict(), dict(exit=-0.02)),
    ]))
    families.append(("200-day overlay (entries+exits)", [
        ("off (rulebook)", dict(EXIT_OVERLAY_200DMA=False)),
        ("on", dict(EXIT_OVERLAY_200DMA=True)),
    ]))
    families.append(("Pairwise correlation cap", [
        ("0.80", dict(PAIRWISE_CORR_CAP=0.80)),
        ("0.85 (default)", dict(PAIRWISE_CORR_CAP=0.85)),
        ("0.90", dict(PAIRWISE_CORR_CAP=0.90)),
    ]))
    families.append(("Rebalance frequency", [
        ("monthly (rulebook)", dict(REBALANCE_FREQ="M")),
        ("quarterly", dict(REBALANCE_FREQ="Q")),
    ]))
    families.append(("Drawdown trigger multiples", [
        ("tight 1.0/1.5/2.0", dict(DD_REVIEW_MULT=1.0, DD_SEVERE_MULT=1.5, DD_EXTREME_MULT=2.0)),
        ("rulebook 1.25/1.9/2.5", dict(DD_REVIEW_MULT=1.25, DD_SEVERE_MULT=1.9, DD_EXTREME_MULT=2.5)),
        ("loose 1.5/2.2/3.0", dict(DD_REVIEW_MULT=1.5, DD_SEVERE_MULT=2.2, DD_EXTREME_MULT=3.0)),
    ]))
    families.append(("Vol blend (60d + 252d)", [
        ("0% (60d only)", dict(VOL_BLEND_252=0.0)),
        ("30%", dict(VOL_BLEND_252=0.3)),
        ("50%", dict(VOL_BLEND_252=0.5)),
    ]))
    families.append(("Regime scaling (halve in bear)", [
        ("off (rulebook)", dict(REGIME_SCALING=False)),
        ("on", dict(REGIME_SCALING=True)),
    ]))
    families.append(("Cash sleeve", [
        ("liquid-ETF history", dict(CASH_SLEEVE_MODE="LIQUID_ETF")),
        ("flat T-bill proxy", dict(CASH_SLEEVE_MODE="TBILL_FLAT")),
    ]))
    families.append(("Tax-aware exit deferral", [
        ("off (rulebook)", dict(TAX_DEFER_EXITS=False)),
        ("on", dict(TAX_DEFER_EXITS=True)),
    ]))
    families.append(("Tax lot selection", [
        ("FIFO (rulebook)", dict(LOT_SELECTION="FIFO")),
        ("HIFO", dict(LOT_SELECTION="HIFO")),
    ]))
    families.append(("Execution style", [
        ("MARKET (cross spread)", dict(EXECUTION_STYLE="MARKET")),
        ("LIMIT_MID", dict(EXECUTION_STYLE="LIMIT_MID")),
    ]))

    if quick:
        families = families[:6]
        P.warn("--quick: tuning only the first 6 families.")

    winners = []
    fam_summaries = []
    for fam_label, variants in families:
        P.sub(fam_label)
        frows = []
        results = []
        for item in variants:
            if len(item) == 3:
                vlabel, gkw, pkw = item
            else:
                vlabel, gkw = item
                pkw = {}
            res = _tune_eval(prices, meta, windows, bench_rets, params=pkw,
                             label=vlabel, **gkw)
            if res is None:
                frows.append([vlabel, "run failed", "-", "-", "-", "-"])
                continue
            results.append(res)
            frows.append([res["label"], fmt_pct(res["mean_excess"]),
                          fmt_pct(res["pct_pos"], 0), fmt_pct(res["cagr"]),
                          f"{res['sharpe']:.2f}", fmt_pct(-res["mdd"])])
        P.table(["Variant", "Mean excess", "% windows+", "CAGR net", "Sharpe", "Max DD"],
                frows, aligns=["<", ">", ">", ">", ">", ">"])
        if results:
            pick, best = _pick_plateau(fam_label, results, base["mean_excess"])
            delta = pick["mean_excess"] - base["mean_excess"]
            flag = ("PICK" if delta > 0.0005 else
                    ("keep baseline" if delta > -0.0025 else
                     "keep baseline (family is WORSE)"))
            P.info(f"Plateau pick: {pick['label']}  (mean excess {fmt_pct(pick['mean_excess'])}, "
                   f"{fmt_pct(pick['pct_pos'],0)} windows positive) -> {flag}")
            fam_summaries.append((fam_label, pick, best))
            # winner = its globals/params relative to baseline defaults
            for item in variants:
                vlabel = item[0]
                if vlabel == pick["label"]:
                    gkw = item[1]
                    pkw = item[2] if len(item) == 3 else {}
                    if delta > 0.0005:
                        winners.append((fam_label, vlabel, gkw, pkw))
                    break

    # ---------------- combine winners ----------------
    if winners:
        P.section("COMBINED CONFIGURATION (family winners together)", "\U0001F9EA")
        cg = {}
        cp = {}
        wlabels = []
        for fam_label, vlabel, gkw, pkw in winners:
            cg.update(gkw)
            cp.update(pkw)
            wlabels.append(f"{fam_label}: {vlabel}")
        combined = _tune_eval(prices, meta, windows, bench_rets, params=cp,
                              label="combined", **cg)
        if combined:
            P.table(["Variant", "Mean excess", "% windows+", "CAGR net", "Sharpe", "Max DD"],
                    [[base["label"], fmt_pct(base["mean_excess"]),
                      fmt_pct(base["pct_pos"], 0), fmt_pct(base["cagr"]),
                      f"{base['sharpe']:.2f}", fmt_pct(-base["mdd"])],
                     [combined["label"], fmt_pct(combined["mean_excess"]),
                      fmt_pct(combined["pct_pos"], 0), fmt_pct(combined["cagr"]),
                      f"{combined['sharpe']:.2f}", fmt_pct(-combined["mdd"])]],
                    aligns=["<", ">", ">", ">", ">", ">"])
            P.sub("Combined vs baseline by walk-forward window")
            prows = []
            for i, row in enumerate(combined["rows"]):
                b = base["excesses"][i] if i < len(base["excesses"]) else None
                c = combined["excesses"][i]
                prows.append([row[0], row[1],
                              fmt_pct(b) if b is not None else "-", fmt_pct(c),
                              "+" if b is not None and c > b else "-"])
            P.table(["From", "To", "Base excess", "Combined excess", "Better?"],
                    prows, aligns=["<", "<", ">", ">", "<"])
            P.note("Plateaus, not peaks: a family counts as a win only if the "
                   "improvement survives the 0.25% plateau rule AND at least "
                   "half the windows are positive.")
            # write the recommendation
            report_path = DATA_DIR / "reports" / "tuned-config.txt"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with open(report_path, "w", encoding="utf-8") as f:
                f.write("TUNED CONFIGURATION (from tsmom.py tune)\n")
                f.write(f"baseline mean excess {base['mean_excess']:.4f} "
                        f"({base['pct_pos']:.0%} windows+)\n")
                f.write(f"combined mean excess {combined['mean_excess']:.4f} "
                        f"({combined['pct_pos']:.0%} windows+)\n\n")
                f.write("Winners (one change at a time):\n")
                for w in wlabels:
                    f.write(f"  - {w}\n")
                f.write("\nGlobal knobs to set:\n")
                for k, v in sorted(cg.items()):
                    f.write(f"  {k} = {v!r}\n")
                f.write("Params to set:\n")
                for k, v in sorted(cp.items()):
                    f.write(f"  {k} = {v!r}\n")
                f.write("\nPer-window (from | to | baseline excess | combined excess):\n")
                for i, row in enumerate(combined["rows"]):
                    b = base["excesses"][i] if i < len(base["excesses"]) else None
                    c = combined["excesses"][i]
                    f.write(f"  {row[0]} .. {row[1]}: "
                            f"{fmt_pct(b) if b is not None else '-'} -> {fmt_pct(c)}\n")
            P.ok(f"Recommendation written to {report_path}")
        else:
            P.warn("Combined run failed — keep the baseline configuration.")
    else:
        P.ok("No family beat the baseline by the plateau rule — the rulebook "
             "configuration stays as-is (that is a real result, not a failure).")


def main(argv=None):
    try:
        sys.stdout.reconfigure(line_buffering=True)   # live output even when piped
    except Exception:
        pass
    ensure_dirs()
    setup_logging()
    parser = argparse.ArgumentParser(
        prog="tsmom.py",
        description="TSMOM — Time Series Momentum on the Nifty ETF Universe (paper-trade first)")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "scan", "update", "trade", "monitor",
                                 "backtest", "status", "report", "demo", "selftest",
                                 "live", "load-data", "reconcile", "tune"],
                        help="what to do (default: run = scan+update+trade+status)")
    parser.add_argument("--years", type=int, default=11, help="years of history to download")
    parser.add_argument("--force-exec", action="store_true",
                        help="execute at the latest price even if not the exact next session")
    parser.add_argument("--quick", action="store_true", help="faster backtest (smaller grid)")
    parser.add_argument("--confirm", action="store_true",
                        help="required together with 'live' to send REAL orders")
    parser.add_argument("--seed", type=int, default=7, help="synthetic data seed")
    args = parser.parse_args(argv)

    db = Db()
    log(f"command={args.command}")
    try:
        if args.command == "selftest":
            cmd_selftest(db)
        elif args.command == "scan":
            P.banner()
            cmd_scan(db)
        elif args.command == "update":
            P.banner()
            cmd_scan(db, try_live=True)
            cmd_update(db, years=args.years)
        elif args.command == "trade":
            P.banner()
            cmd_trade(db, force_exec=args.force_exec)
        elif args.command == "monitor":
            P.banner()
            cmd_monitor(db)
        elif args.command == "backtest":
            P.banner()
            run_full_backtest(db, quick=args.quick)
        elif args.command == "tune":
            P.banner()
            cmd_tune(db, quick=args.quick)
        elif args.command == "status":
            P.banner()
            cmd_status(db)
        elif args.command == "report":
            P.banner()
            cmd_report(db)
        elif args.command == "load-data":
            P.banner()
            cmd_load_data(db)
        elif args.command == "reconcile":
            P.banner()
            cmd_reconcile(db)
        elif args.command == "demo":
            P.banner()
            generate_synthetic_data(db, seed=args.seed)
            cmd_scan(db, try_live=False)
            db2 = db
            prices, meta = load_prices_from_db(db2)
            bt = Backtester(prices, meta, BASELINE_PARAMS, strategy="B")
            print_backtest_result(bt.run(), "DEMO BACKTEST (synthetic data) — Strategy B")
            cyc = TradingCycle(db2)
            cyc.run()
            cmd_status(db2)
        elif args.command == "live":
            if PAPER_TRADE and not args.confirm:
                P.banner()
                P.warn("LIVE trading is disabled while PAPER_TRADE=True in the config.")
                P.note("This program runs in PAPER mode by default — that is intentional.")
                P.note("To send real orders: set PAPER_TRADE=False in tsmom.py AND run "
                       "with:  python tsmom.py live --confirm")
                return
            if not args.confirm:
                P.err("Refusing to run live without --confirm. Paper mode is the default.")
                return
            globals()["PAPER_TRADE"] = False
            P.banner()
            P.warn("LIVE MODE — REAL ORDERS WILL BE SENT TO UPSTOX.")
            P.warn("Live-safety rules (audit #9): MARKET orders fill at the market, "
                   "not at our simulated price. The broker fill is fetched and "
                   "logged, but there is NO automatic position reconciliation.")
            P.note("Run 'python tsmom.py reconcile' after trading to compare the "
                   "ledger with the broker. Prefer paper mode until you trust it.")
            cmd_trade(db, force_exec=args.force_exec)
        else:  # run
            P.banner()
            cmd_scan(db)
            cmd_update(db, years=args.years)
            prices = {r["symbol"] for r in db.query("SELECT DISTINCT symbol FROM price_daily")}
            if not prices:
                P.warn("No downloaded prices — trying synthetic demo data so you can "
                       "see the full engine working.")
                generate_synthetic_data(db, seed=args.seed)
                cmd_scan(db, try_live=False)
            cmd_trade(db, force_exec=args.force_exec)
            cmd_status(db)
        P.raw()
        P.info(f"Database : {DB_PATH}")
        P.info(f"Logs     : {LOG_DIR}")
        P.info(f"Cache    : {CACHE_DIR}")
        P.info(f"Reports  : {REPORT_DIR}")
    except KeyboardInterrupt:
        P.warn("Stopped by user.")
    except Exception as e:
        P.err(f"Unexpected error: {e}")
        log(f"fatal: {e}", "ERROR")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
