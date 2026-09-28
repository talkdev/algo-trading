#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dual_momentum_screener.py -- Dual Momentum Screener for NSE cash equities (v1.0)
================================================================================

WHAT THIS IS

    This is a SCREENER ONLY.
      * It places NO orders.
      * It connects to NO broker execution API (no Zerodha, no Upstox, no Kite,
        no SmartAPI -- nothing. Broker credentials in env.txt are NOT read or
        used here; they belong to the order-execution side of the repo).
      * It has NO trade engine, NO backtester and NO portfolio simulation.
      * It has NO stop-loss / position-sizing logic.
    It fetches NSE daily OHLCV via yfinance, computes dual-momentum signals,
    ranks the survivors and prints + exports a candidate watchlist. Nothing more.
    The order path (if any) lives in other files, entirely separate from this one.

DUAL MOMENTUM IN THREE SENTENCES

    1. RELATIVE STRENGTH (RS) ranks every stock in the universe against the rest
       of the universe using its 12-1 month return, so a candidate must be a
       genuine cross-sectional leader, not merely a stock that went up.
    2. ABSOLUTE MOMENTUM (AM) checks each stock against a cash/risk-free hurdle
       (here: the hardcoded 6.5% annual Indian T-bill proxy), so a leader is only
       tradable if it is beating cash over the same window.
    3. The MARKET REGIME filter (Nifty 50 above a rising 200-SMA, plus an India
       VIX overlay) decides whether the whole long book should be switched on;
       a candidate must pass RS AND AM AND the regime to appear in the output.

NSE-SPECIFIC THRESHOLDS AND WHY THEY ARE SET THIS WAY

    * 12-1 momentum window (RS_LOOKBACK_MONTHS=12, RS_SKIP_MONTHS=1)
      The most recent month is SKIPPED because short-horizon returns in Indian
      equities show strong mean reversion / reversal (a stock that spiked last
      month tends to give some back). Standard academic momentum is the return
      from t-12 to t-1 months.
    * 200-SMA regime confirmation (REGIME_MA_PERIOD=200, REGIME_REQUIRE_RISING)
      ~200 trading days is roughly one year of NSE sessions. Long-only momentum
      suffers its worst drawdowns when it keeps buying leaders into a falling
      index, so we require Nifty 50 to be above its 200-SMA AND for that 200-SMA
      to itself be rising over the last 20 sessions.
    * India VIX overlay (USE_VIX_FILTER, VIX_MAX=25)
      NSE momentum crashes cluster in high-volatility regimes. A smoothed VIX
      above 25 is treated as RISK-OFF so the screener stops surfacing fresh
      longs during panic phases. VIX_SMOOTH_DAYS=5 removes single-day spikes.
    * Volatility-adjusted ranking (USE_VOL_ADJUSTED_RS)
      Raw 12-1 return can be dominated by one erratic, illiquid mid-cap. Ranking
      on momentum/volatility penalises erratic movers and favours smooth trends,
      which translates into tighter, more tradable charts.
    * MAX_ANNUALIZED_VOL=0.60 is a HARD FILTER
      60% annualised vol is extremely high for an NSE cash equity. Stocks above
      it are usually recent listings, circuit-locked counters or post-corporate-
      action artefacts, and their position sizing becomes impractical.
    * MIN_ADV_CR=5.0 (Rs 5 crore of 20-day average daily traded value)
      Keeps the candidate liquid enough for retail/mid-size tickets without
      moving the price. MIN_PRICE=20 excludes penny stocks whose tick size and
      spreads distort returns. MIN_HISTORY_TRADING_DAYS=250 guarantees a full
      year of history exists before the 12-1 window is even computed.

HOW TO RUN

    python dual_momentum_screener.py                     # whole universe.json
    python dual_momentum_screener.py --top 10 --rs-floor 70
    python dual_momentum_screener.py --refresh --csv my_picks.csv
    python dual_momentum_screener.py --selftest          # offline unit checks
    python dual_momentum_screener.py --no-vol-adjust --quiet

HOW THE UNIVERSE IS DEFINED (universe.json is the single source of truth)

    Every run screens the symbols listed in universe.json, key "symbols" --
    plain NSE trading symbols WITHOUT the ".NS" suffix. Edit that JSON array to
    add/remove names; no code change is needed, duplicates are removed and file
    order is preserved. Nothing is scraped at runtime.

    WHERE THE FILE IS FOUND (first hit wins):
      1. --universe-file PATH, or the UNIVERSE_FILE env variable / CONFIG key
      2. <script directory>/universe.json      <-- normal repo layout, e.g.
         C:\\Users\\Administrator\\Desktop\\algo-trading\\stock-data\\universe.json
         sitting next to this script
      3. <current working directory>/universe.json
      4. <cwd>/stock-data/universe.json, <script dir>/../stock-data/universe.json
    If no candidate exists the run stops with exit code 3 and prints every path
    it looked in.

    --universe can only SUBSET that file, never replace or extend it:
        json      (default) -> the whole file; "nifty500" is an alias
        nifty200            -> first 200 entries of the file, in file order
        nifty50             -> entries of the file that are also NIFTY_50 members
    The NIFTY_50 list below is therefore only a membership mask for that last
    subset; it never adds symbols that are absent from the JSON file.

    Keep RISK_FREE_RATE_ANNUAL current by editing CONFIG when the 3-month
    T-bill yield moves materially (it is hardcoded on purpose -- never fetched).

DISCLAIMER

    Past momentum performance does not guarantee future results. This tool is a
    ranking aid for research, not investment advice. Verify every candidate
    against corporate actions, liquidity and your own risk limits before acting.
"""

# =============================================================================
# IMPORTS -- strictly limited to the approved dependency set.
# =============================================================================
import argparse
import csv
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

try:
    import yfinance as yf            # market data only -- never used for orders
except Exception:                    # pragma: no cover - environment dependent
    yf = None

try:
    import requests                  # optional; retry/session helper only
except Exception:                    # pragma: no cover - environment dependent
    requests = None


# =============================================================================
# CONFIG -- every tunable parameter lives here, and every one is used below.
# =============================================================================
CONFIG = {
    # ---- Universe -----------------------------------------------------------
    # universe.json is ALWAYS the source of symbols; UNIVERSE_SOURCE can only
    # subset it ("json" = whole file; "nifty500" is an alias for the same thing).
    "UNIVERSE_SOURCE": "json",            # "json" | "nifty50" | "nifty200" | "nifty500"
    "UNIVERSE_JSON": "universe.json",     # repo file stock-data/universe.json
    # Optional explicit path to the universe file (absolute or relative to the
    # CWD). Empty = auto-detect: script dir first, then CWD, then repo layouts.
    # Overridable with --universe-file PATH or the UNIVERSE_FILE env variable.
    "UNIVERSE_FILE": "",
    "MIN_PRICE": 20.0,                    # Rs  -- exclude penny stocks
    "MIN_ADV_CR": 5.0,                    # Rs crore -- 20-day avg daily value
    "MIN_HISTORY_TRADING_DAYS": 250,      # ~12 months of trading days

    # ---- Relative Strength (RS) --------------------------------------------
    "RS_LOOKBACK_MONTHS": 12,
    "RS_SKIP_MONTHS": 1,                  # 12-1 window (skip reversal month)
    "RS_MIN_RATING": 80,                  # 0-100 rating floor
    "RS_TOP_DECILE": True,                # refine to RS >= 90 when possible
    "MIN_PICKS_FOR_DECILE": 10,           # else fall back to RS_MIN_RATING

    # ---- Absolute Momentum (AM) --------------------------------------------
    "AM_LOOKBACK_MONTHS": 12,
    "AM_USE_RISK_FREE": True,
    "RISK_FREE_RATE_ANNUAL": 0.065,       # 3M T-bill proxy -- update manually!
    "RISK_FREE_RATE_UPDATE_HINT": "3-month Indian T-bill yield; review quarterly",

    # ---- Volatility-adjusted momentum --------------------------------------
    "VOL_LOOKBACK_DAYS": 20,              # ~1 month of daily returns
    "USE_VOL_ADJUSTED_RS": True,
    "MAX_ANNUALIZED_VOL": 0.60,           # hard filter for NSE mid-caps

    # ---- Market regime filter (Nifty 50) -----------------------------------
    "REGIME_MA_PERIOD": 200,
    "REGIME_REQUIRE_RISING": True,
    "REGIME_SLOPE_LOOKBACK": 20,

    # ---- VIX risk overlay ---------------------------------------------------
    "USE_VIX_FILTER": True,
    "VIX_MAX": 25.0,
    "VIX_SMOOTH_DAYS": 5,                 # simple MA on India VIX closes

    # ---- Data quality -------------------------------------------------------
    "MAX_DATA_STALENESS_DAYS": 5,         # reject if last bar older than this
    "YF_BATCH_SIZE": 50,                  # tickers per yfinance call
    "YF_RETRY_ATTEMPTS": 3,
    "YF_RETRY_SLEEP_SEC": 2.0,
    "DATA_PERIOD": "2y",                  # yfinance period (2y > 12-1 window)
    "DATA_INTERVAL": "1d",

    # ---- Caching ------------------------------------------------------------
    "CACHE_DIR": ".cache_dual_momentum",
    "CACHE_TTL_HOURS": 12,                # ignore cache older than this
    "CACHE_VERSION": "v2",                # bump to invalidate old cache files

    # ---- Output -------------------------------------------------------------
    "OUTPUT_TOP_N": 30,
    "OUTPUT_CSV": "dual_momentum_picks.csv",
    "VERBOSE": True,
}

# -----------------------------------------------------------------------------
# Fixed constants (not tunables)
# -----------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))   # Asia/Kolkata, no tz database
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BENCHMARK_TICKERS = ["^NSEI", "^INDIAVIX", "GOLDBEES.NS", "LIQUIDBEES.NS"]
BENCHMARK_SET = set(BENCHMARK_TICKERS)
NIFTY_TICKER = "^NSEI"          # Nifty 50 index
VIX_TICKER = "^INDIAVIX"        # India VIX
TRADING_DAYS_PER_YEAR = 252     # NSE trading days used for annualisation
NIFTY_50_MIN_EXPECTED = 200     # warn below this many unique symbols
UNIVERSE_SOURCE_DEFAULT = "json"  # always screen the universe.json file
BENCHMARKS_IN_DATA = ("^NSEI", "^INDIAVIX", "GOLDBEES.NS", "LIQUIDBEES.NS")

# Populated by get_universe() so the report can state which file was screened.
UNIVERSE_INFO = {"path": None, "file_count": 0, "mode": None, "count": 0}

# Exactly 50 unique Nifty 50 constituents (fixed reference list, no duplicates).
NIFTY_50 = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "SBIN", "BHARTIARTL", "BAJFINANCE", "KOTAKBANK",
    "LT", "AXISBANK", "ASIANPAINT", "MARUTI", "TATASTEEL",
    "WIPRO", "HCLTECH", "ITC", "VBL", "SUNPHARMA",
    "TATAMOTORS", "POWERGRID", "NTPC", "TITAN", "ULTRACEMCO",
    "NESTLEIND", "BAJAJFINSV", "TATACONSUM", "CIPLA", "M&M",
    "HINDALCO", "JSWSTEEL", "GRASIM", "SHREECEM", "EICHERMOT",
    "HEROMOTOCO", "ADANIENT", "ADANIPORTS", "COALINDIA", "ONGC",
    "BPCL", "DRREDDY", "DIVISLAB", "APOLLOHOSP", "BRITANNIA",
    "TECHM", "INDUSINDBK", "BAJAJ-AUTO", "SBILIFE", "HDFCLIFE",
]

# Exact CSV column order required by the spec.
CSV_COLUMNS = [
    "rank", "symbol", "close", "ret_12_1_pct", "vol_pct", "rs_rating",
    "am_ok", "adv_cr", "regime_ok", "run_date",
]

# Rejection counters are printed in this fixed order (labels are exact).
REJECT_ORDER = [
    "price < MIN_PRICE",
    "ADV < MIN_ADV_CR",
    "vol too high",
    "no momentum",
    "history too short",
    "stale data",
    "RS < floor",
    "AM fail",
    "regime off",
]

# Module-level registry so fetch_daily_data() can report funnel counts to
# run_screener() without changing its required return type (dict of frames).
FETCH_STATS = {"requested": 0, "downloaded": 0, "history_passed": 0, "stale": 0}


# =============================================================================
# SMALL UTILITIES
# =============================================================================
def now_ist():
    """Current aware datetime in IST (Asia/Kolkata) using a fixed offset."""
    return datetime.now(IST)


def log(msg, cfg=None):
    """Print unless --quiet disabled verbosity (VERBOSE flag)."""
    if cfg is not None and not cfg.get("VERBOSE", True):
        return
    print(msg)
    try:
        sys.stdout.flush()
    except Exception:
        pass


def safe_float(x):
    """Return a finite float or None. Never raises."""
    try:
        v = float(x)
    except Exception:
        return None
    if not math.isfinite(v):
        return None
    return v


def _stable_hash(text):
    """
    Deterministic 64-bit FNV-1a hash (hex string).

    Used for the cache filename because Python's built-in hash() is randomised
    per process (PYTHONHASHSEED) and would produce a new cache file each run.
    hashlib is deliberately NOT imported -- it is outside the approved imports.
    """
    h = 0xcbf29ce484222325
    for byte in str(text).encode("utf-8"):
        h ^= byte
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return format(h, "016x")


class StderrCapture(object):
    """
    Minimal write sink used to keep yfinance's per-ticker "delisted / no data"
    chatter off the console. The captured text is summarised by the caller and
    the real sys.stderr is always restored afterwards. Pure `sys` -- the logging
    or contextlib modules are deliberately not imported.
    """

    def __init__(self):
        self.chunks = []

    def write(self, text):
        if text:
            self.chunks.append(str(text))
        return len(text) if text else 0

    def flush(self):
        pass

    def summary(self, max_names=8):
        """Condense captured text into short, useful lines."""
        blob = "".join(self.chunks)
        lines = [ln.strip() for ln in blob.replace("\r", "\n").split("\n")
                 if ln.strip()]
        if not lines:
            return ""
        delisted = []
        for line in lines:
            if "delisted" not in line and "No data found" not in line:
                continue
            name = line.split(":")[0].lstrip("$ ").strip()
            # Only keep clean ticker-like names; yfinance also dumps summary
            # lines such as "Failed downloads: ['X.NS']" which are not names.
            if (not name or " " in name or "[" in name or "'" in name
                    or "Failed downloads" in name):
                continue
            if name not in delisted:
                delisted.append(name)
        if delisted:
            shown = ", ".join(delisted[:max_names])
            more = "" if len(delisted) <= max_names else \
                " (+%d more)" % (len(delisted) - max_names)
            return "no data for %d ticker(s): %s%s" % (len(delisted), shown, more)
        return lines[0][:180]


def resolve_path(path, cfg=None):
    """
    Resolve a CONFIG path. Absolute paths are returned unchanged; relative paths
    are resolved against the directory containing this script so that
    `python stock-data/dual_momentum_screener.py` behaves identically from any
    working directory. (OUTPUT_CSV is additionally mirrored to the CWD by
    run_screener, per spec: "CSV path is resolved relative to the CWD".)
    """
    if not path:
        return path
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(SCRIPT_DIR, path))


def load_env_file(cfg=None):
    """
    Load KEY=VALUE pairs from env.txt (repo convention, also .env) if present.

    Implementation detail: this is a tiny built-in parser -- python-dotenv is
    NOT an approved dependency. Values already present in os.environ win, so
    shell exports always override the file. Broker keys are loaded for the rest
    of the repo but are intentionally unused by this screener.
    """
    loaded = {}
    candidates = [
        resolve_path("env.txt", cfg),
        resolve_path(os.path.join(os.getcwd(), "env.txt"), cfg),
        resolve_path(".env", cfg),
        os.path.join(os.getcwd(), ".env"),
    ]
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if not key:
                        continue
                    loaded[key] = value
                    if key not in os.environ:
                        os.environ[key] = value
        except Exception as exc:
            log("WARNING: could not read env file %s (%s)" % (path, exc), cfg)
            continue
        break   # first existing file wins
    return loaded


# =============================================================================
# PART 4 -- UNIVERSE
# =============================================================================
def _universe_file_candidates(cfg):
    """
    Build the ordered list of places the universe file is looked for.

    Precedence:
      1. explicit override -- CONFIG["UNIVERSE_FILE"] or the UNIVERSE_FILE
         environment variable (also settable from env.txt). Relative values are
         tried against the CWD and then against the script directory.
      2. CONFIG["UNIVERSE_JSON"] resolved against the script directory -- this is
         what makes the standard Windows layout work, e.g.
         C:\\Users\\Administrator\\Desktop\\algo-trading\\stock-data\\dual_momentum_screener.py
         + ...\\stock-data\\universe.json
      3. CONFIG["UNIVERSE_JSON"] resolved against the current working directory.
      4. A few conventional repo layouts: <cwd>/stock-data/<name>,
         <script dir>/<name>, <script dir>/../stock-data/<name>,
         <cwd>/../stock-data/<name>.
    Returns (explicit_paths, auto_paths) -- de-duplicated, normalised paths that
    may or may not exist.
    """
    base_name = os.path.basename(cfg["UNIVERSE_JSON"])

    explicit_paths = []
    explicit = cfg.get("UNIVERSE_FILE") or os.environ.get("UNIVERSE_FILE")
    if explicit:
        expanded = os.path.expanduser(str(explicit).strip())
        if os.path.isabs(expanded):
            explicit_paths.append(expanded)
        else:
            explicit_paths.append(os.path.join(os.getcwd(), expanded))
            explicit_paths.append(os.path.join(SCRIPT_DIR, expanded))

    cfg_path = cfg["UNIVERSE_JSON"]
    auto_paths = [
        resolve_path(cfg_path, cfg),                                  # script dir
        os.path.join(os.getcwd(), cfg_path),                          # cwd
        os.path.join(os.getcwd(), "stock-data", base_name),           # cwd/stock-data
        os.path.join(SCRIPT_DIR, base_name),                          # script dir/name
        os.path.join(SCRIPT_DIR, os.pardir, "stock-data", base_name),
        os.path.join(os.getcwd(), os.pardir, "stock-data", base_name),
    ]

    def _dedupe(paths):
        seen = set()
        ordered = []
        for path in paths:
            try:
                norm = os.path.normpath(path)
            except Exception:
                continue
            if norm in seen:
                continue
            seen.add(norm)
            ordered.append(norm)
        return ordered

    return _dedupe(explicit_paths), _dedupe(auto_paths)


def _universe_file_path(cfg):
    """
    Locate the universe JSON file.

    An explicit --universe-file / UNIVERSE_FILE path wins; if it is missing a
    warning is printed and auto-detection continues, so a typo is visible rather
    than silently ignored. Returns (path, all_searched_paths); path is None when
    nothing exists.
    """
    explicit_paths, auto_paths = _universe_file_candidates(cfg)
    if explicit_paths:
        for path in explicit_paths:
            if os.path.isfile(path):
                return path, explicit_paths + auto_paths
        log("WARNING: the universe file you specified was not found (%s); "
            "falling back to auto-detection."
            % (cfg.get("UNIVERSE_FILE") or os.environ.get("UNIVERSE_FILE")), cfg)
    for path in auto_paths:
        if os.path.isfile(path):
            return path, explicit_paths + auto_paths
    return None, explicit_paths + auto_paths


def _load_universe_json(cfg):
    """
    Read universe.json -- THE SINGLE SOURCE OF TRUTH for stock symbols.

    Returns (symbols, path) where symbols is a de-duplicated, upper-cased list of
    NSE trading symbols WITHOUT the ".NS" suffix (entries saved with the suffix
    are tolerated). Nothing is scraped or fetched: this is a local file read.
    Returns ([], path_or_None) when the file is missing or unusable.
    """
    path, _ = _universe_file_path(cfg)
    if path is None:
        return [], None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:
        log("ERROR: could not parse universe file %s (%s)." % (path, exc), cfg)
        return [], path

    symbols = payload.get("symbols", []) if isinstance(payload, dict) else payload
    if not isinstance(symbols, list):
        log("ERROR: %s does not contain a 'symbols' list." % path, cfg)
        return [], path

    clean = []
    seen = set()
    for sym in symbols:
        s = str(sym).strip().upper()
        if not s or s.startswith("^") or s.startswith("#"):
            continue                  # skip indices / comment placeholders
        if s.endswith(".NS"):
            s = s[:-3]                # tolerate symbols saved with the suffix
        if s not in seen:
            seen.add(s)
            clean.append(s)
    return clean, path


def get_universe(cfg):
    """
    Return the NSE symbols (WITHOUT the ".NS" suffix) to screen.

    The universe ALWAYS comes from universe.json -- every symbol screened is one
    that is written in that file, and every symbol written in that file is
    screened. UNIVERSE_SOURCE never introduces a symbol of its own; it can only
    subset the file:

        "json" | "nifty500" : the entire file (default; nifty500 is an alias)
        "nifty200"          : the first 200 entries of the file, in file order
        "nifty50"           : entries of the file that are also NIFTY_50 members

    A missing/empty/unreadable universe file is a fatal configuration error: the
    screener refuses to run against some other list rather than silently
    screening something the user did not ask for.
    """
    mode = str(cfg.get("UNIVERSE_SOURCE", UNIVERSE_SOURCE_DEFAULT)).strip().lower()
    if mode not in ("json", "nifty50", "nifty200", "nifty500"):
        log("WARNING: unknown UNIVERSE_SOURCE '%s' -- using the full universe "
            "file." % mode, cfg)
        mode = "json"

    symbols, path = _load_universe_json(cfg)

    if not symbols:
        if path is None:
            _, searched = _universe_file_path(cfg)
            print("ERROR: universe file \"%s\" was not found. Looked in:"
                  % cfg["UNIVERSE_JSON"])
            for candidate in searched[:8]:
                print("         %s" % candidate)
        else:
            print("ERROR: universe file found but unusable: %s" % path)
        print("       The screener always screens the symbols listed in "
              "\"%s\"." % cfg["UNIVERSE_JSON"])
        print("       Put the file next to this script (e.g. "
              "<repo>\\stock-data\\universe.json) or point at it explicitly "
              "with --universe-file PATH.")
        raise SystemExit(3)

    nifty50_set = set(NIFTY_50)
    if mode in ("json", "nifty500"):
        universe = list(symbols)
    elif mode == "nifty200":
        universe = symbols[:200]
        print("NOTE: UNIVERSE_SOURCE=nifty200 -- first 200 entries of %s."
              % os.path.basename(path))
    else:  # nifty50
        universe = [s for s in symbols if s in nifty50_set]
        print("NOTE: UNIVERSE_SOURCE=nifty50 -- %d of %d file entries are "
              "NIFTY_50 members." % (len(universe), len(symbols)))

    # Recorded for the report header (and for the self-test).
    UNIVERSE_INFO["path"] = path
    UNIVERSE_INFO["file_count"] = len(symbols)
    UNIVERSE_INFO["mode"] = mode
    UNIVERSE_INFO["count"] = len(universe)

    print("Universe file: %s" % path)
    print("Universe: %d unique symbols loaded." % len(universe))
    if len(universe) < NIFTY_50_MIN_EXPECTED:
        print("WARNING: universe has fewer than %d unique symbols (%d). "
              "Check %s." % (NIFTY_50_MIN_EXPECTED, len(universe),
                             os.path.basename(path)))
    return universe


# =============================================================================
# PART 5 -- DATA FETCHING (CACHE + RETRY, NEVER RAISES)
# =============================================================================
def _todays_bar():
    """Today's IST date (used for staleness maths)."""
    return now_ist().date()


def _cache_file_path(tickers, cfg):
    """Cache path keyed on (universe, period, interval, cache version)."""
    key = "|".join([
        cfg["CACHE_VERSION"],
        cfg["DATA_PERIOD"],
        cfg["DATA_INTERVAL"],
        ",".join(sorted(tickers)),
    ])
    cache_dir = resolve_path(cfg["CACHE_DIR"], cfg)
    return os.path.join(cache_dir, "daily_data_%s.pkl" % _stable_hash(key))


def _cache_is_fresh(path, cfg):
    """True if the cache file exists and is younger than CACHE_TTL_HOURS."""
    try:
        if not os.path.isfile(path):
            return False
        age_hours = (time.time() - os.path.getmtime(path)) / 3600.0
        return age_hours < float(cfg["CACHE_TTL_HOURS"])
    except Exception:
        return False


def _normalise_frame(raw):
    """
    Convert one ticker's raw yfinance frame into [open, high, low, close,
    volume] with numeric dtypes and a DatetimeIndex. Returns None if unusable.
    """
    if raw is None or not isinstance(raw, pd.DataFrame) or raw.empty:
        return None

    df = raw.copy()
    rename = {}
    for col in df.columns:
        name = str(col).strip().lower().replace("_", " ")
        if name in ("open", "high", "low", "close", "volume"):
            rename[col] = name
        elif name == "adj close":        # only relevant if close is absent
            rename[col] = "adj close"
    df = df.rename(columns=rename)

    if "close" not in df.columns and "adj close" in df.columns:
        df["close"] = df["adj close"]

    wanted = ["open", "high", "low", "close", "volume"]
    for col in wanted:
        if col not in df.columns:
            df[col] = np.nan
    df = df[wanted]

    for col in wanted:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["close"])
    if df.empty:
        return None

    if not isinstance(df.index, pd.DatetimeIndex):
        try:
            df.index = pd.to_datetime(df.index)
        except Exception:
            return None
    df = df[~df.index.isna()]
    df = df.sort_index()
    return df if not df.empty else None


def _extract_batch_frames(raw, batch):
    """Pull per-ticker frames out of a yfinance download result."""
    out = {}
    if raw is None or not isinstance(raw, pd.DataFrame) or raw.empty:
        return out

    if isinstance(raw.columns, pd.MultiIndex):
        level0 = set(str(c) for c in raw.columns.get_level_values(0))
        level1 = set(str(c) for c in raw.columns.get_level_values(1))
        for ticker in batch:
            try:
                if ticker in level0:
                    sub = raw[ticker]
                elif ticker in level1:      # newer "Price"/"Ticker" layouts
                    sub = raw.xs(ticker, axis=1, level=1)
                else:
                    continue
                frame = _normalise_frame(sub)
                if frame is not None:
                    out[ticker] = frame
            except Exception:
                continue
    else:
        # Single-ticker flat frame: yfinance drops the ticker level entirely.
        if len(batch) == 1:
            frame = _normalise_frame(raw)
            if frame is not None:
                out[batch[0]] = frame
        else:
            # Flat frame with a multi-ticker request is ambiguous -- ignore it.
            return out
    return out


def preflight_network(cfg):
    """
    Optional reachability probe before the download loop (uses `requests` when
    installed; the screener runs fine without it).

    This ONLY checks that Yahoo Finance is reachable so an offline run fails
    fast with a clear message instead of 11 silent empty batches. The response
    body is discarded -- no data is parsed here and no other host is contacted.
    Returns True/False/None (None = not checked).
    """
    if requests is None:
        return None
    probe_url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI"
    try:
        resp = requests.get(probe_url, params={"range": "1d", "interval": "1d"},
                            timeout=8)
        ok = int(getattr(resp, "status_code", 0)) < 500
        if not ok:
            log("WARNING: market data host returned HTTP %s; downloads may fail."
                % getattr(resp, "status_code", "?"), cfg)
        return ok
    except Exception as exc:
        log("WARNING: no connectivity to the market data host (%s). "
            "Continuing -- yfinance failures will be logged and skipped."
            % exc, cfg)
        return False


def _download_batch(batch, cfg):
    """
    Download one batch with retries. Returns dict of frames (possibly empty).
    NEVER raises: yfinance errors are logged and swallowed.
    """
    if yf is None:
        return {}
    attempts = max(1, int(cfg["YF_RETRY_ATTEMPTS"]))
    sleep_s = float(cfg["YF_RETRY_SLEEP_SEC"])
    for attempt in range(1, attempts + 1):
        # yfinance chatters to stderr about delisted/empty tickers; capture that
        # and print a one-line digest instead of hundreds of raw lines.
        real_stderr = sys.stderr
        capture = StderrCapture()
        try:
            sys.stderr = capture
            raw = yf.download(
                tickers=" ".join(batch),
                period=cfg["DATA_PERIOD"],
                interval=cfg["DATA_INTERVAL"],
                auto_adjust=True,          # split/bonus adjusted prices
                group_by="ticker",
                threads=True,
                progress=False,
            )
            frames = _extract_batch_frames(raw, batch)
            if frames:
                missing = [t for t in batch if t not in frames]
                if missing:
                    note = capture.summary()
                    log("  %d/%d tickers returned data%s"
                        % (len(frames), len(batch),
                           (" -- " + note) if note else ""), cfg)
                return frames
            note = capture.summary()
            log("  batch attempt %d/%d returned no usable rows (size %d)%s"
                % (attempt, attempts, len(batch),
                   (" -- " + note) if note else ""), cfg)
        except Exception as exc:
            log("  yfinance error on attempt %d/%d: %s"
                % (attempt, attempts, exc), cfg)
        finally:
            sys.stderr = real_stderr
        if attempt < attempts:
            time.sleep(sleep_s)
    log("  WARNING: batch of %d symbols failed after %d attempts -- skipped."
        % (len(batch), attempts), cfg)
    return {}


def _to_yf_ticker(symbol):
    """
    Map a plain NSE trading symbol to its Yahoo Finance ticker.

    NSE cash equities on Yahoo carry the ".NS" suffix (RELIANCE -> RELIANCE.NS).
    Indices ("^NSEI") and already-suffixed tickers ("GOLDBEES.NS") pass through.
    """
    s = str(symbol).strip()
    if not s:
        return s
    if s.startswith("^") or "." in s:
        return s
    return s + ".NS"


def fetch_daily_data(symbols, cfg, refresh=False):
    """
    Fetch daily OHLCV for `symbols` plus the fixed benchmark tickers.

    Returns {symbol: DataFrame[open, high, low, close, volume]}. Uses a pickle
    cache under CACHE_DIR unless refresh=True or the cache is older than
    CACHE_TTL_HOURS. Must NEVER raise on a yfinance failure -- it logs, skips and
    continues. Equity symbols are filtered for history length and staleness;
    benchmark tickers are exempt (they are needed for the regime filter).
    """
    global FETCH_STATS

    tickers = []
    seen = set()
    for sym in list(symbols) + list(BENCHMARK_TICKERS):
        ticker = _to_yf_ticker(sym)          # RELIANCE -> RELIANCE.NS
        if ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)

    FETCH_STATS = {"requested": 0, "downloaded": 0, "history_passed": 0,
                   "stale": 0, "dropped_history": 0}
    FETCH_STATS["requested"] = len(
        [s for s in symbols if _to_yf_ticker(s) not in BENCHMARK_SET])

    cache_path = _cache_file_path(tickers, cfg)
    data = {}
    cached_stats = None

    # ---- 1/2. Cache path -----------------------------------------------------
    if not refresh and _cache_is_fresh(cache_path, cfg):
        try:
            cached = pd.read_pickle(cache_path)
            if isinstance(cached, dict) and cached:
                if isinstance(cached.get("data"), dict):
                    # Current format: frames + funnel stats from the fetch run.
                    data = cached["data"]
                    cached_stats = cached.get("stats")
                else:
                    data = cached               # legacy plain-dict cache
                log("Cache hit: %s (%d symbols, TTL %sh)"
                    % (os.path.basename(cache_path), len(data),
                       cfg["CACHE_TTL_HOURS"]), cfg)
        except Exception as exc:
            log("WARNING: cache unreadable (%s) -- refetching." % exc, cfg)
            data = {}
            cached_stats = None

    # ---- 3/4. Download ------------------------------------------------------
    if not data:
        if yf is None:
            log("ERROR: yfinance is not installed. Run: pip install yfinance",
                cfg)
            return {}
        batch_size = max(1, int(cfg["YF_BATCH_SIZE"]))
        batches = [tickers[i:i + batch_size]
                   for i in range(0, len(tickers), batch_size)]
        preflight_network(cfg)
        log("Downloading %d tickers in %d batch(es) of up to %d ..."
            % (len(tickers), len(batches), batch_size), cfg)
        for idx, batch in enumerate(batches, start=1):
            log("  batch %d/%d (%d tickers)" % (idx, len(batches), len(batch)),
                cfg)
            frames = _download_batch(batch, cfg)
            for ticker, frame in frames.items():
                data[ticker] = frame

    # ---- 5/6. Quality filters ----------------------------------------------
    min_hist = int(cfg["MIN_HISTORY_TRADING_DAYS"])
    max_stale = int(cfg["MAX_DATA_STALENESS_DAYS"])
    today = _todays_bar()

    kept = {}
    dropped_history = 0
    dropped_stale = 0
    if cached_stats:
        # Reuse the funnel numbers recorded when the cache was written, so a
        # cache hit reports the same breakdown as the original download.
        dropped_history = int(cached_stats.get("dropped_history", 0))
        dropped_stale = int(cached_stats.get("stale", 0))
    else:
        # Symbols yfinance never returned (delisted, renamed, bad ticker) are
        # counted with the history failures so the funnel stays additive.
        never_arrived = [t for t in tickers
                         if t not in BENCHMARK_SET and t not in data]
        dropped_history += len(never_arrived)
        if never_arrived:
            log("  no data returned for %d symbol(s): %s"
                % (len(never_arrived), ", ".join(never_arrived[:15]) +
                   ("" if len(never_arrived) <= 15 else " ...")), cfg)

    for ticker, frame in data.items():
        is_bench = ticker in BENCHMARK_SET
        if not is_bench:
            if frame is None or len(frame) < min_hist:
                dropped_history += 1
                continue
            if not is_fresh(frame, max_stale, today):
                dropped_stale += 1
                continue
        kept[ticker] = frame

    equity_kept = len([t for t in kept if t not in BENCHMARK_SET])
    downloaded = (int(cached_stats.get("downloaded", 0)) if cached_stats
                  else len([t for t in data if t not in BENCHMARK_SET]))
    FETCH_STATS["downloaded"] = downloaded
    FETCH_STATS["history_passed"] = equity_kept
    FETCH_STATS["stale"] = dropped_stale
    FETCH_STATS["dropped_history"] = dropped_history

    # ---- 7. Persist cache (frames + funnel stats) ---------------------------
    try:
        cache_dir = os.path.dirname(cache_path)
        if cache_dir and not os.path.isdir(cache_dir):
            os.makedirs(cache_dir, exist_ok=True)
        pd.to_pickle({
            "data": kept,
            "stats": {
                "downloaded": len([t for t in kept if t not in BENCHMARK_SET]),
                "history_passed": equity_kept,
                "dropped_history": dropped_history,
                "stale": dropped_stale,
            },
        }, cache_path)
        log("Cache written: %s" % os.path.basename(cache_path), cfg)
    except Exception as exc:
        log("WARNING: could not write cache (%s) -- continuing." % exc, cfg)

    # ---- 8. Report ----------------------------------------------------------
    print("Fetched %d symbols, %d passed history + staleness filter."
          % (downloaded, equity_kept))
    if dropped_history or dropped_stale:
        log("  (dropped: %d too little history / no data, %d stale)"
            % (dropped_history, dropped_stale), cfg)
    return kept


# =============================================================================
# PART 6 -- INDICATOR FUNCTIONS (all return None/False on bad input, no raises)
# =============================================================================
def _month_end_resample(close):
    """
    Monthly last-price series. Tries the modern 'ME' alias first and falls back
    to the legacy 'M' alias so the code works across pandas versions.
    """
    if close is None or len(close) == 0:
        return None
    series = close
    if not isinstance(series, pd.Series):
        try:
            series = pd.Series(series)
        except Exception:
            return None
    if not isinstance(series.index, pd.DatetimeIndex):
        try:
            series.index = pd.to_datetime(series.index)
        except Exception:
            return None
    series = pd.to_numeric(series, errors="coerce").dropna()
    if series.empty:
        return None
    for rule in ("ME", "M"):          # pandas >=2.2 uses "ME"; older accepts "M"
        try:
            out = series.resample(rule).last()
            if out is not None and len(out) > 0:
                return out
        except Exception:
            continue
    return None


def momentum_12_1(close, lookback_months, skip_months):
    """
    Compute the 12-1 momentum return (lookback_months / skip_months configurable).

    Steps:
      1. Convert close to a monthly series using resample(...).last().
      2. end_price   = monthly.iloc[-1 - skip_months]
      3. start_price = monthly.iloc[-1 - skip_months - lookback_months]
      4. Return (end_price / start_price) - 1.
    Returns None on short/NaN series or a non-positive start price. Never raises.
    """
    try:
        lookback_months = int(lookback_months)
        skip_months = int(skip_months)
        if lookback_months <= 0 or skip_months < 0:
            return None
        monthly = _month_end_resample(close)
        if monthly is None:
            return None
        needed = lookback_months + skip_months + 1      # inclusive endpoints
        if len(monthly) < needed:
            return None
        end_idx = -1 - skip_months
        start_idx = -1 - skip_months - lookback_months
        if abs(start_idx) > len(monthly):
            return None
        end_price = safe_float(monthly.iloc[end_idx])
        start_price = safe_float(monthly.iloc[start_idx])
        if end_price is None or start_price is None or start_price <= 0:
            return None
        return (end_price / start_price) - 1.0
    except Exception:
        return None


def realized_vol(close, lookback_days):
    """
    Annualised realised volatility from daily log returns.
      1. log_ret = log(close / close.shift(1))
      2. take the last `lookback_days` returns (drop NaN)
      3. return std(ddof=1) * sqrt(252)
    Returns None if fewer than `lookback_days` returns are available. No raises.
    """
    try:
        lookback_days = int(lookback_days)
        if lookback_days < 2 or close is None:
            return None
        series = pd.to_numeric(pd.Series(close).astype("float64"),
                               errors="coerce")
        if len(series.dropna()) < lookback_days + 1:
            return None
        log_ret = np.log(series / series.shift(1)).replace([np.inf, -np.inf],
                                                           np.nan).dropna()
        if len(log_ret) < lookback_days:
            return None
        window = log_ret.tail(lookback_days)
        if len(window) < 2:
            return None
        value = float(window.std(ddof=1)) * math.sqrt(TRADING_DAYS_PER_YEAR)
        return value if math.isfinite(value) else None
    except Exception:
        return None


def sma(series, period):
    """Simple moving average via series.rolling(period).mean()."""
    try:
        period = int(period)
        if period <= 0:
            return pd.Series(dtype="float64")
        s = pd.to_numeric(pd.Series(series).astype("float64"), errors="coerce")
        return s.rolling(period).mean()
    except Exception:
        return pd.Series(dtype="float64")


def rs_rating(returns_series):
    """
    Cross-sectional percentile rating (1-100) via rank(pct=True) * 100.
    NaN inputs propagate as NaN. Never raises.
    """
    try:
        s = pd.to_numeric(pd.Series(returns_series).astype("float64"),
                          errors="coerce")
        if s.empty:
            return s
        rated = s.rank(pct=True, na_option="keep") * 100.0
        rated = rated.where(s.notna())      # NaN in -> NaN out
        return rated
    except Exception:
        return pd.Series(dtype="float64")


def above_200sma_rising(close, ma_period, slope_lookback):
    """
    True if last_close > SMA(ma_period)[-1]
    AND SMA(ma_period)[-1] > SMA(ma_period)[-1 - slope_lookback].
    False on short/NaN series. If REGIME_REQUIRE_RISING is disabled by the
    caller, only the above-SMA half is applied (see _market_regime).
    """
    try:
        ma_period = int(ma_period)
        slope_lookback = int(slope_lookback)
        if close is None or len(close) < ma_period + slope_lookback + 1:
            return False
        series = pd.to_numeric(pd.Series(close).astype("float64"),
                               errors="coerce")
        if len(series.dropna()) < ma_period + slope_lookback + 1:
            return False
        ma = sma(series, ma_period)
        last_close = safe_float(series.iloc[-1])
        last_ma = safe_float(ma.iloc[-1])
        prev_idx = -1 - slope_lookback
        if abs(prev_idx) > len(ma):
            return False
        prev_ma = safe_float(ma.iloc[prev_idx])
        if last_close is None or last_ma is None or prev_ma is None:
            return False
        if not (last_close > last_ma):
            return False
        return bool(last_ma > prev_ma)
    except Exception:
        return False


def is_fresh(df, max_staleness_days, today=None):
    """
    True if the last bar in df is within `max_staleness_days` calendar days of
    today (IST). False on empty/NaN-index frames. Never raises.
    """
    try:
        if df is None or len(df) == 0:
            return False
        last = df.index[-1]
        if isinstance(last, datetime):
            last_date = last.date() if last.tzinfo is None else last.astimezone(IST).date()
        else:
            last_date = pd.Timestamp(last).date()
        ref = today if today is not None else now_ist().date()
        return (ref - last_date).days <= int(max_staleness_days)
    except Exception:
        return False


# =============================================================================
# PART 7 -- SCREENER PIPELINE
# =============================================================================
def _market_regime(data, cfg):
    """
    Compute the market regime from Nifty 50 (^NSEI) and India VIX (^INDIAVIX).

    Returns (regime_ok, details_dict). Never raises.
    """
    details = {
        "nifty_close": None, "nifty_sma": None, "nifty_rising": None,
        "vix_value": None, "vix_ok": None, "ma_ok": None,
    }
    ma_period = int(cfg["REGIME_MA_PERIOD"])
    slope = int(cfg["REGIME_SLOPE_LOOKBACK"])

    nifty = data.get(NIFTY_TICKER)
    if nifty is None or nifty.empty or "close" not in nifty.columns:
        print("Market regime: RISK-OFF (Nifty 50 data unavailable)")
        regime_ok = False
    else:
        nifty_close = nifty["close"]
        # above_200sma_rising() returns False if REGIME_REQUIRE_RISING data is
        # insufficient; here we additionally allow pure above-SMA mode when
        # REGIME_REQUIRE_RISING is switched off in CONFIG.
        regime_ok = above_200sma_rising(nifty_close, ma_period, slope)
        if not cfg["REGIME_REQUIRE_RISING"]:
            try:
                ma = sma(nifty_close, ma_period)
                lc = safe_float(pd.to_numeric(nifty_close, errors="coerce").iloc[-1])
                lm = safe_float(ma.iloc[-1])
                regime_ok = bool(lc is not None and lm is not None and lc > lm)
            except Exception:
                regime_ok = False
        details["nifty_close"] = safe_float(
            pd.to_numeric(nifty_close, errors="coerce").iloc[-1])
        details["nifty_sma"] = safe_float(sma(nifty_close, ma_period).iloc[-1])
        details["ma_ok"] = regime_ok
        details["nifty_rising"] = cfg["REGIME_REQUIRE_RISING"]
        # Purely informational: is the last close above the 200-SMA?
        try:
            details["above_ma"] = bool(
                details["nifty_close"] is not None and
                details["nifty_sma"] is not None and
                details["nifty_close"] > details["nifty_sma"])
        except Exception:
            details["above_ma"] = None

    vix_ok = True
    if cfg["USE_VIX_FILTER"]:
        vix = data.get(VIX_TICKER)
        if vix is None or vix.empty or "close" not in vix.columns:
            vix_ok = False
            print("Market regime: RISK-OFF (VIX data unavailable)")
        else:
            try:
                vix_series = pd.to_numeric(vix["close"], errors="coerce")
                vix_smoothed = vix_series.rolling(int(cfg["VIX_SMOOTH_DAYS"])).mean()
                vix_clean = vix_smoothed.dropna()
                if vix_clean.empty:
                    vix_ok = False
                else:
                    details["vix_value"] = safe_float(vix_clean.iloc[-1])
                    vix_ok = bool(details["vix_value"] is not None and
                                  details["vix_value"] < float(cfg["VIX_MAX"]))
            except Exception:
                vix_ok = False
    details["vix_ok"] = vix_ok

    return bool(regime_ok and vix_ok), details


def _stock_metrics(frame, cfg):
    """
    Per-stock metrics + reject reason for one symbol.
    Returns (metrics_dict_or_None, reject_reason_or_None).
    """
    close = frame["close"]
    volume = frame["volume"]

    momentum = momentum_12_1(close, cfg["RS_LOOKBACK_MONTHS"], cfg["RS_SKIP_MONTHS"])
    if momentum is None:
        return None, "no momentum"

    last_close = safe_float(close.iloc[-1])
    if last_close is None or last_close < float(cfg["MIN_PRICE"]):
        return None, "price < MIN_PRICE"

    # 20-day average daily traded value in Rs crore: mean(close*volume)/1e7.
    try:
        recent = pd.DataFrame({"c": pd.to_numeric(close, errors="coerce"),
                               "v": pd.to_numeric(volume, errors="coerce")}).tail(20)
        trad_value = (recent["c"] * recent["v"]).mean() / 1e7
        adv_cr = safe_float(trad_value)
    except Exception:
        adv_cr = None
    if adv_cr is None or adv_cr < float(cfg["MIN_ADV_CR"]):
        return None, "ADV < MIN_ADV_CR"

    vol_ann = realized_vol(close, int(cfg["VOL_LOOKBACK_DAYS"]))
    if vol_ann is None or vol_ann > float(cfg["MAX_ANNUALIZED_VOL"]):
        return None, "vol too high"

    return {
        "symbol": None,
        "momentum": momentum,
        "vol_ann": vol_ann,
        "adv_cr": adv_cr,
        "last_close": last_close,
    }, None


def run_screener(cfg, refresh=False):
    """
    Full pipeline (steps 1-10). Prints progress, the candidate table, the
    rejection breakdown and exports OUTPUT_CSV. Places no orders. Never raises
    on market-data failures -- it degrades to "no candidates" and reports why.
    """
    started = time.time()
    run_dt = now_ist()
    run_date_iso = run_dt.strftime("%Y-%m-%d")
    rejects = {label: 0 for label in REJECT_ORDER}
    rejects["history too short"] = 0
    rejects["stale data"] = 0

    log("=" * 60, cfg)
    log(" STEP 1/10  Universe + data", cfg)
    symbols = get_universe(cfg)
    data = fetch_daily_data(symbols, cfg, refresh=refresh)
    equity_data = {t: f for t, f in data.items() if t not in BENCHMARK_SET}
    rejects["history too short"] = int(FETCH_STATS.get("dropped_history", 0))
    rejects["stale data"] = int(FETCH_STATS.get("stale", 0))
    log(" STEP 1/10  done: %d of %d symbols usable"
        % (len(equity_data), len(symbols)), cfg)

    log(" STEP 2/10  Market regime (^NSEI, ^INDIAVIX)", cfg)
    regime_ok, reg = _market_regime(data, cfg)

    log(" STEP 3/10  Per-stock metrics", cfg)
    rows = []
    for ticker, frame in equity_data.items():
        symbol = ticker[:-3] if ticker.endswith(".NS") else ticker
        metrics, reason = _stock_metrics(frame, cfg)
        if metrics is None:
            rejects[reason] = rejects.get(reason, 0) + 1
            continue
        metrics["symbol"] = symbol
        rows.append(metrics)

    if not rows:
        print("No symbols produced valid metrics -- nothing to rank.")
        _print_reject_breakdown(cfg, rejects)
        _print_post_run_notes(cfg, regime_ok, 0)
        return

    df = pd.DataFrame(rows)[["symbol", "momentum", "vol_ann", "adv_cr",
                             "last_close"]]

    log(" STEP 4/10  Relative Strength rating (%d survivors)"
        % len(df), cfg)
    # IMPORTANT: the rating is computed over the FULL survivor cross-section,
    # BEFORE any RS floor / AM filter, so percentiles are not distorted.
    if cfg["USE_VOL_ADJUSTED_RS"]:
        rs_raw = df["momentum"] / df["vol_ann"]
        rs_raw = rs_raw.replace([np.inf, -np.inf], np.nan)
    else:
        rs_raw = df["momentum"]
    df["rs_rating"] = rs_rating(rs_raw)

    log(" STEP 5/10  Absolute Momentum flag", cfg)
    if cfg["AM_USE_RISK_FREE"]:
        # Hurdle for the same horizon as the momentum window: (1+rf)^(t/12)-1.
        am_threshold = ((1.0 + float(cfg["RISK_FREE_RATE_ANNUAL"])) **
                        (float(cfg["AM_LOOKBACK_MONTHS"]) / 12.0)) - 1.0
    else:
        am_threshold = 0.0
    df["am_ok"] = df["momentum"] > am_threshold

    log(" STEP 6/10  Final filter (RS >= %s, AM, regime=%s)"
        % (cfg["RS_MIN_RATING"], "ON" if regime_ok else "OFF"), cfg)
    above_floor = df[df["rs_rating"] >= float(cfg["RS_MIN_RATING"])]
    rejects["RS < floor"] += int(len(df) - len(above_floor))
    pass_am = above_floor[above_floor["am_ok"]]
    rejects["AM fail"] += int(len(above_floor) - len(pass_am))

    if regime_ok:
        final = pass_am.copy()
    else:
        rejects["regime off"] += int(len(pass_am))
        final = pass_am.iloc[0:0].copy()

    if cfg["RS_TOP_DECILE"] and len(final) > 0:
        top_decile = final[final["rs_rating"] >= 90]
        if len(top_decile) >= int(cfg["MIN_PICKS_FOR_DECILE"]):
            final = top_decile
        else:
            print("Top-decile filter disabled (only %d stocks >= 90)."
                  % len(top_decile))

    log(" STEP 7/10  Ranking", cfg)
    final = final.sort_values(by=["rs_rating", "momentum"],
                              ascending=[False, False]).reset_index(drop=True)
    total_passed = int(len(final))

    log(" STEP 8/10  Output (top %d)" % int(cfg["OUTPUT_TOP_N"]), cfg)
    output_rows = final.head(int(cfg["OUTPUT_TOP_N"])).reset_index(drop=True)

    _print_report_header(cfg, run_dt, symbols, len(equity_data), regime_ok, reg,
                         total_passed)
    _print_candidate_table(cfg, output_rows)
    _print_benchmark_context(cfg, data)

    log(" STEP 9/10  Rejection breakdown", cfg)
    _print_reject_breakdown(cfg, rejects)

    written = _write_csv(cfg, output_rows, regime_ok, run_date_iso)

    log(" STEP 10/10 Post-run sanity", cfg)
    _print_post_run_notes(cfg, regime_ok, total_passed)

    if written:
        print("Exported %d rows -> %s" % (len(output_rows), written))
    else:
        print("WARNING: could not export %s" % cfg["OUTPUT_CSV"])
    print("=" * 60)
    log("Done in %.1f s" % (time.time() - started), cfg)


# =============================================================================
# PART 8 -- OUTPUT FORMATTING / CSV
# =============================================================================
_TABLE_WIDTHS = [3, 12, 9, 8, 7, 4, 3, 9]
_TABLE_HEAD = ["#", "SYMBOL", "CLOSE", "12-1M%", "VOL%", "RS", "AM", "ADV(Cr)"]


def _table_line(values, numeric):
    """Build one fixed-width table line (shared by header and data rows)."""
    parts = []
    for idx, width in enumerate(_TABLE_WIDTHS):
        value = values[idx]
        if numeric[idx]:
            text = str(value).rjust(width)
        else:
            text = str(value).ljust(width)
        parts.append(text[:width])
    return "  " + "  ".join(parts)


def _print_report_header(cfg, run_dt, symbols, fetched, regime_ok, reg,
                         total_passed):
    """Header block (format per spec)."""
    universe_label = "%s (%d symbols)" % (
        os.path.basename(UNIVERSE_INFO.get("path") or
                         cfg.get("UNIVERSE_JSON", "universe.json")),
        int(UNIVERSE_INFO.get("file_count") or len(symbols)))
    if UNIVERSE_INFO.get("mode") in ("nifty50", "nifty200"):
        universe_label += " -- subset: %s" % UNIVERSE_INFO["mode"]
    print("")
    print("  " + "=" * 60)
    print("   DUAL MOMENTUM SCREENER  (NSE | long-only | v1.0)")
    print("  " + "=" * 60)
    print("   Run time           : %s" % run_dt.strftime("%Y-%m-%d %H:%M IST"))

    regime_bits = []
    if reg.get("nifty_close") is not None and reg.get("nifty_sma") is not None:
        relay = ">" if reg.get("above_ma") else "<"
        regime_bits.append("Nifty %s %s 200SMA %s"
                           % (format(reg["nifty_close"], ",.0f"), relay,
                              format(reg["nifty_sma"], ",.0f")))
        # Slope of the 200-SMA over REGIME_SLOPE_LOOKBACK sessions.
        if cfg["REGIME_REQUIRE_RISING"]:
            regime_bits.append("rising" if reg.get("ma_ok") else "not rising")
    elif reg.get("nifty_close") is not None:
        regime_bits.append("Nifty %s" % format(reg["nifty_close"], ",.0f"))
    if cfg["USE_VIX_FILTER"] and reg.get("vix_value") is not None:
        regime_bits.append("VIX %.1f" % reg["vix_value"])
    regime_note = ("   (" + ", ".join(regime_bits) + ")") if regime_bits else ""

    print("   Universe           : %s" % universe_label)
    print("   Symbols fetched    : %d / %d" % (fetched, len(symbols)))
    print("   History filter     : %d passed"
          % int(FETCH_STATS.get("history_passed", fetched)))
    print("   Market regime      : %s%s"
          % ("RISK-ON " if regime_ok else "RISK-OFF", regime_note))
    print("   Risk-free rate     : %.2f%% annual"
          % (float(cfg["RISK_FREE_RATE_ANNUAL"]) * 100.0))
    print("   RS window          : %d-%d months (%s)"
          % (int(cfg["RS_LOOKBACK_MONTHS"]), int(cfg["RS_SKIP_MONTHS"]),
             "vol-adjusted" if cfg["USE_VOL_ADJUSTED_RS"] else "raw"))
    print("   RS floor           : %d" % int(cfg["RS_MIN_RATING"]))
    print("   Passed all filters : %d stocks" % total_passed)
    print("")


def _print_candidate_table(cfg, rows):
    """Formatted candidate table, at most OUTPUT_TOP_N rows."""
    top_n = int(cfg["OUTPUT_TOP_N"])
    print("  TOP %d DUAL MOMENTUM CANDIDATES (LONG)" % top_n)
    print("")
    print(_table_line(_TABLE_HEAD, [False] * len(_TABLE_HEAD)).rstrip())
    print("  " + "-" * (sum(_TABLE_WIDTHS) + 2 * (len(_TABLE_WIDTHS) - 1)))
    for i in range(len(rows)):
        row = rows.iloc[i]
        print(_table_line([
            "%d" % (i + 1),
            str(row["symbol"]),
            "%.2f" % float(row["last_close"]),
            "%.1f" % (float(row["momentum"]) * 100.0),
            "%.1f" % (float(row["vol_ann"]) * 100.0),
            "%.0f" % float(row["rs_rating"]),
            "YES",      # only am_ok rows survive the Step 6 filter
            "%.1f" % float(row["adv_cr"]),
        ], [True, False, True, True, True, True, True, True]))
    if len(rows) == 0:
        print("  (no candidates passed all filters)")
    print("")


def _print_benchmark_context(cfg, data):
    """
    Informational only: GOLDBEES (gold proxy) and LIQUIDBEES (cash proxy) are
    fetched as per spec Part 2 but do NOT influence scoring in v1.0.
    """
    lines = []
    for ticker in ("GOLDBEES.NS", "LIQUIDBEES.NS"):
        frame = data.get(ticker)
        if frame is not None and len(frame) > 0:
            last = safe_float(frame["close"].iloc[-1])
            if last is not None:
                lines.append("%s %.2f" % (ticker, last))
    if lines:
        print("  BENCHMARK CONTEXT (informational, not used in scoring)")
        print("    " + " | ".join(lines))
        print("")


def _print_reject_breakdown(cfg, rejects):
    """Rejection counters in the fixed spec order."""
    print("  REJECTION BREAKDOWN")
    for label in REJECT_ORDER:
        print("    %-23s: %d" % (label, int(rejects.get(label, 0))))
    print("")


def _print_post_run_notes(cfg, regime_ok, total_passed):
    """Step 10 sanity notes."""
    if not regime_ok:
        print("NOTE: RISK-OFF regime. Candidates should be treated as "
              "watchlist only, not entries.")
    if total_passed < 5:
        print("WARNING: fewer than 5 candidates passed. Consider loosening "
              "filters or waiting for a stronger regime.")


def _write_csv(cfg, rows, regime_ok, run_date_iso):
    """
    Write the candidate rows (exact column order) and return the path written.
    The CSV is written to the CWD-resolved OUTPUT_CSV path, and additionally
    mirrored next to this script when running from elsewhere, so the file is
    easy to find either way.
    """
    csv_path = cfg["OUTPUT_CSV"]
    if not os.path.isabs(csv_path):
        csv_path = os.path.join(os.getcwd(), csv_path)
    csv_path = os.path.normpath(csv_path)

    regime_flag = "YES" if regime_ok else "NO"
    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(CSV_COLUMNS)
            for i in range(len(rows)):
                row = rows.iloc[i]
                writer.writerow([
                    i + 1,
                    str(row["symbol"]),
                    "%.2f" % float(row["last_close"]),
                    "%.2f" % (float(row["momentum"]) * 100.0),
                    "%.2f" % (float(row["vol_ann"]) * 100.0),
                    "%.2f" % float(row["rs_rating"]),
                    "YES",                      # only am_ok rows survive
                    "%.2f" % float(row["adv_cr"]),
                    regime_flag,
                    run_date_iso,
                ])
    except Exception as exc:
        print("WARNING: CSV write failed (%s)" % exc)
        return None

    # Mirror into the repo folder too (helps when run from another CWD).
    try:
        mirror = resolve_path(cfg["OUTPUT_CSV"], cfg)
        if os.path.abspath(mirror) != os.path.abspath(csv_path):
            with open(mirror, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(CSV_COLUMNS)
                for i in range(len(rows)):
                    row = rows.iloc[i]
                    writer.writerow([
                        i + 1, str(row["symbol"]),
                        "%.2f" % float(row["last_close"]),
                        "%.2f" % (float(row["momentum"]) * 100.0),
                        "%.2f" % (float(row["vol_ann"]) * 100.0),
                        "%.2f" % float(row["rs_rating"]),
                        "YES", "%.2f" % float(row["adv_cr"]),
                        regime_flag, run_date_iso,
                    ])
    except Exception:
        pass
    return csv_path


# =============================================================================
# PART 10 -- SELF-TEST (NO NETWORK)
# =============================================================================
def _rising_monthly_series(months=14, monthly_growth=0.01, start=100.0):
    """
    Monthly-indexed, monthly-stepped price series with compounding growth.
    Using month-end timestamps makes the resample step a no-op so the expected
    return can be computed analytically.
    """
    idx = pd.date_range(end="2026-09-30", periods=months, freq="ME")
    values = [start * ((1.0 + monthly_growth) ** i) for i in range(months)]
    return pd.Series(values, index=idx)


def run_selftest():
    """
    Offline unit checks (synthetic data only, NO network). Prints PASS/FAIL per
    check and returns 0 if everything passed, else 1.
    """
    print("Running dual momentum screener self-test (no network required) ...")
    print("")
    results = []

    def check(name, passed, detail=""):
        status = "PASS" if passed else "FAIL"
        results.append(bool(passed))
        line = "  [%s] %s" % (status, name)
        if detail:
            line += "  -- %s" % detail
        print(line)

    def quiet_call(fn, *args, **kwargs):
        """Call fn with stdout swallowed so its progress prints stay out of the
        self-test report (restores stdout even if fn raises)."""
        real_stdout = sys.stdout
        try:
            sys.stdout = StderrCapture()
            return fn(*args, **kwargs)
        finally:
            sys.stdout = real_stdout

    # 1. Linearly (compounding) rising monthly series -> expected return.
    try:
        growth = 0.01
        series = _rising_monthly_series(14, growth)
        expected = ((1.0 + growth) ** 12) - 1.0        # 12 months, skip the last
        got = momentum_12_1(series, 12, 1)
        ok = got is not None and abs(got - expected) <= 0.005
        check("momentum_12_1 rising series", ok,
              "got %.4f expected %.4f" % (got if got is not None else float("nan"),
                                          expected))
    except Exception as exc:
        check("momentum_12_1 rising series", False, "exception: %s" % exc)

    # 2. Flat series -> approximately zero.
    try:
        flat = _rising_monthly_series(14, 0.0)
        got = momentum_12_1(flat, 12, 1)
        ok = got is not None and abs(got) < 1e-9
        check("momentum_12_1 flat series ~ 0", ok, "got %r" % got)
    except Exception as exc:
        check("momentum_12_1 flat series ~ 0", False, "exception: %s" % exc)

    # 3. Too-short series -> None.
    try:
        short = _rising_monthly_series(10, 0.01)       # needs 14 months
        got = momentum_12_1(short, 12, 1)
        check("momentum_12_1 short series -> None", got is None, "got %r" % got)
    except Exception as exc:
        check("momentum_12_1 short series -> None", False, "exception: %s" % exc)

    # 4. Constant daily log return -> ~0 realised vol.
    try:
        prices = pd.Series([100.0 * (1.001 ** i) for i in range(40)])
        vol = realized_vol(prices, 20)
        ok = vol is not None and abs(vol) < 1e-6
        check("realized_vol constant return ~ 0", ok, "got %r" % vol)
    except Exception as exc:
        check("realized_vol constant return ~ 0", False, "exception: %s" % exc)

    # 5. rs_rating on 5 values -> within [1, 100], strictly increasing.
    try:
        raw = pd.Series([0.1, 0.2, 0.3, 0.4, 0.5])
        rated = rs_rating(raw)
        in_range = bool(((rated >= 1.0) & (rated <= 100.0)).all())
        increasing = bool(all(rated.iloc[i] < rated.iloc[i + 1]
                              for i in range(len(rated) - 1)))
        check("rs_rating range + monotonic", in_range and increasing,
              "values %s" % [round(float(v), 2) for v in rated])
    except Exception as exc:
        check("rs_rating range + monotonic", False, "exception: %s" % exc)

    # 6. above_200sma_rising: True on a 250-bar rise, False on a 250-bar fall.
    try:
        up = pd.Series(np.linspace(100.0, 349.0, 250))
        down = pd.Series(np.linspace(349.0, 100.0, 250))
        res_up = above_200sma_rising(up, 200, 20)
        res_down = above_200sma_rising(down, 200, 20)
        check("above_200sma_rising up/down", res_up is True and res_down is False,
              "up=%s down=%s" % (res_up, res_down))
    except Exception as exc:
        check("above_200sma_rising up/down", False, "exception: %s" % exc)

    # 7. Synthetic 400-bar stock frame -> valid momentum_12_1.
    try:
        idx = pd.bdate_range(end="2026-09-28", periods=400)
        prices = pd.Series(np.linspace(100.0, 260.0, 400), index=idx)
        frame = pd.DataFrame({"open": prices, "high": prices, "low": prices,
                              "close": prices, "volume": 1_000_000.0})
        got = momentum_12_1(frame["close"], 12, 1)
        check("synthetic 400-bar frame momentum", got is not None,
              "got %r" % got)
    except Exception as exc:
        check("synthetic 400-bar frame momentum", False, "exception: %s" % exc)

    # 8. is_fresh: today -> True, 30 days ago -> False.
    try:
        today = now_ist().date()
        idx_today = pd.DatetimeIndex([pd.Timestamp(today)])
        idx_old = pd.DatetimeIndex([pd.Timestamp(today - timedelta(days=30))])
        fresh = pd.DataFrame({"close": [100.0]}, index=idx_today)
        old = pd.DataFrame({"close": [100.0]}, index=idx_old)
        ok = (is_fresh(fresh, 5, today) is True and
              is_fresh(old, 5, today) is False)
        check("is_fresh today/30 days old", ok,
              "today=%s old=%s" % (is_fresh(fresh, 5, today),
                                   is_fresh(old, 5, today)))
    except Exception as exc:
        check("is_fresh today/30 days old", False, "exception: %s" % exc)

    # 9. 90th percentile of a 100-stock universe -> rating >= 90.
    try:
        raw = pd.Series(np.arange(1.0, 101.0))
        rated = rs_rating(raw)
        p90 = float(rated.iloc[89])          # 90th value of 100
        check("rs_rating 90th percentile >= 90", p90 >= 90.0,
              "got %.2f" % p90)
    except Exception as exc:
        check("rs_rating 90th percentile >= 90", False, "exception: %s" % exc)

    # 10. The universe IS the universe.json file: same names, same order,
    #     de-duplicated, no ".NS" suffix leakage, and >= 200 entries.
    try:
        approx = {"UNIVERSE_SOURCE": "json", "UNIVERSE_JSON": "universe.json",
                  "VERBOSE": False}
        uni = quiet_call(get_universe, approx)
        raw, json_path = _load_universe_json(approx)
        ok = (uni == raw and len(uni) >= NIFTY_50_MIN_EXPECTED and
              len(set(uni)) == len(uni) and
              all(not s.endswith(".NS") for s in uni))
        check("universe == universe.json contents", ok,
              "count=%d file=%s" % (len(uni), os.path.basename(json_path or "-")))
    except SystemExit as exc:
        check("universe == universe.json contents", False,
              "universe file missing/unusable (exit %s)" % exc.code)
    except Exception as exc:
        check("universe == universe.json contents", False,
              "exception: %s" % exc)

    # 10b. Subset modes may only REMOVE symbols from the file -- never add any.
    try:
        approx = {"UNIVERSE_JSON": "universe.json", "VERBOSE": False}
        raw, _ = _load_universe_json(approx)
        raw_set = set(raw)
        subsets = {}
        for mode in ("nifty50", "nifty200", "nifty500"):
            cfg_mode = dict(approx)
            cfg_mode["UNIVERSE_SOURCE"] = mode
            subsets[mode] = quiet_call(get_universe, cfg_mode)
        ok = (all(s in raw_set for sub in subsets.values() for s in sub) and
              subsets["nifty500"] == raw and
              len(subsets["nifty200"]) <= 200 and
              len(subsets["nifty50"]) <= len(NIFTY_50))
        check("subset modes only filter universe.json", ok,
              "nifty50=%d nifty200=%d nifty500=%d file=%d"
              % (len(subsets["nifty50"]), len(subsets["nifty200"]),
                 len(subsets["nifty500"]), len(raw)))
    except SystemExit as exc:
        check("subset modes only filter universe.json", False,
              "universe file missing/unusable (exit %s)" % exc.code)
    except Exception as exc:
        check("subset modes only filter universe.json", False,
              "exception: %s" % exc)

    # 10c. The universe file is found even when the process runs from another
    #      working directory (the "script invoked from a different folder" case).
    try:
        orig_cwd = os.getcwd()
        tmp_dir = orig_cwd
        for candidate in (os.path.abspath(os.sep), "/tmp", os.path.expanduser("~")):
            if os.path.isdir(candidate):
                tmp_dir = candidate
                break
        os.chdir(tmp_dir)
        try:
            found, _searched = _universe_file_path(approx)
            uni = quiet_call(get_universe, approx)
        finally:
            os.chdir(orig_cwd)
        ok = (found is not None and len(uni) == len(raw) and
              os.path.normcase(os.path.dirname(found)) ==
              os.path.normcase(os.path.dirname(json_path or "")))
        check("universe file resolves from any cwd", ok,
              "from %s -> %s" % (tmp_dir, found or "NOT FOUND"))
    except Exception as exc:
        check("universe file resolves from any cwd", False,
              "exception: %s" % exc)

    # 11. NIFTY_50 is exactly 50 unique names.
    try:
        ok = len(NIFTY_50) == 50 and len(set(NIFTY_50)) == 50
        check("NIFTY_50 == 50 unique names", ok, "count=%d unique=%d"
              % (len(NIFTY_50), len(set(NIFTY_50))))
    except Exception as exc:
        check("NIFTY_50 == 50 unique names", False, "exception: %s" % exc)

    # 12. CSV column order matches the required spec exactly.
    try:
        expected = ["rank", "symbol", "close", "ret_12_1_pct", "vol_pct",
                    "rs_rating", "am_ok", "adv_cr", "regime_ok", "run_date"]
        check("CSV column order", CSV_COLUMNS == expected,
              "|".join(CSV_COLUMNS))
    except Exception as exc:
        check("CSV column order", False, "exception: %s" % exc)

    # 13. CONFIG parameter usage audit (no orphan parameters).
    try:
        with open(os.path.abspath(__file__), "r", encoding="utf-8") as fh:
            source = fh.read()
        orphans = []
        for key in CONFIG.keys():
            if source.count('"%s"' % key) <= 1:      # only the CONFIG definition
                orphans.append(key)
        check("every CONFIG key is referenced in code", not orphans,
              "orphans=%s" % (orphans if orphans else "none"))
    except Exception as exc:
        check("every CONFIG key is referenced in code", False,
              "exception: %s" % exc)

    failed = results.count(False)
    print("")
    print("Self-test result: %d/%d checks passed."
          % (len(results) - failed, len(results)))
    return 0 if failed == 0 else 1


# =============================================================================
# PART 9 -- CLI
# =============================================================================
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=("Dual momentum screener for NSE cash equities. "
                     "Screener only -- no orders, no broker, no backtest."),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--universe-file", dest="universe_file", default=None,
                        help=("explicit path to the universe JSON, e.g. "
                              "C:\\...\\algo-trading\\stock-data\\universe.json "
                              "(default: auto-detect next to this script)"))
    parser.add_argument("--universe", choices=["json", "nifty50", "nifty200",
                                               "nifty500"],
                        default=None,
                        help=("subset of the universe.json file to screen "
                              "(default: json = the whole file)"))
    parser.add_argument("--top", type=int, default=None,
                        help="number of rows to print/export")
    parser.add_argument("--rs-floor", dest="rs_floor", type=int, default=None,
                        help="minimum RS rating (0-100)")
    parser.add_argument("--no-vol-adjust", dest="no_vol_adjust",
                        action="store_true",
                        help="rank on raw momentum instead of momentum/vol")
    parser.add_argument("--csv", dest="csv", default=None,
                        help="output CSV path")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore the local cache and refetch")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress step-by-step progress output")
    parser.add_argument("--selftest", action="store_true",
                        help="run offline unit checks and exit (no network)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    cfg = dict(CONFIG)                 # run-time copy; CONFIG stays pristine

    # CLI overrides (explicitly required by the spec)
    if args.universe:
        cfg["UNIVERSE_SOURCE"] = args.universe
    if args.universe_file:
        cfg["UNIVERSE_FILE"] = args.universe_file
    if args.top is not None:
        cfg["OUTPUT_TOP_N"] = int(args.top)
    if args.rs_floor is not None:
        cfg["RS_MIN_RATING"] = int(args.rs_floor)
    if args.no_vol_adjust:
        cfg["USE_VOL_ADJUSTED_RS"] = False
    if args.csv:
        cfg["OUTPUT_CSV"] = args.csv
    if args.quiet:
        cfg["VERBOSE"] = False

    # env.txt is loaded so the rest of the repo shares one environment file.
    # The screener itself never uses broker credentials.
    load_env_file(cfg)

    if args.selftest:
        return run_selftest()

    if cfg["VERBOSE"]:
        print("NOTE: RISK_FREE_RATE_ANNUAL=%.3f -- %s"
              % (float(cfg["RISK_FREE_RATE_ANNUAL"]),
                 cfg.get("RISK_FREE_RATE_UPDATE_HINT", "")))

    run_screener(cfg, refresh=args.refresh)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(130)
    except Exception as exc:                 # last-resort guard: never traceback
        print("FATAL: unexpected error: %s" % exc)
        sys.exit(2)
