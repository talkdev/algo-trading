#!/usr/bin/env python3
r"""
VAM-AF v2.0 -- SINGLE-FILE NSE swing SCREENER. Python 3.10+; standard library only.
No pip packages, helper Python files, trading engine, orders, sizing or holdings.

INSTALL / INPUTS
---------------
Save this file as C:\Users\Administrator\Desktop\algo-trading\stock-momentum.py.
It reads ONLY the existing env.txt beside it and stock-data\universe.json.
Strategy defaults are in Config below, not config.json. Paths are script-relative.
env.txt contains UPSTOX_ACCESS_TOKEN=your_current_real_token; the API key/secret
may remain there but are not needed for market-data GET requests. No OAuth/token
refresh is automated. Duplicate token entries are rejected; optional Bearer prefix
is normalized. No secrets or raw profile payloads are logged. Protect env.txt.

universe.json is {"exchange":"NSE", "segment":"NSE_EQ", "count":500,
"symbols":[...uppercase NSE symbols...]}. count must match array length. Original
500-symbol and revised 498-symbol files both work. The user-maintained list is not
a verified current official Nifty 500 list. No automatic symbol substitutions.

COMMANDS (Command Prompt or PowerShell from the project folder)
--------------------------------------------------------------
py -3 stock-momentum.py --open-report
py -3 stock-momentum.py --check-universe --refresh-instruments
py -3 stock-momentum.py --ignore-market-regime --open-report
py -3 stock-momentum.py --allow-previous-session --open-report
py -3 stock-momentum.py --as-of 2026-09-25
py -3 stock-momentum.py --strict-universe
py -3 stock-momentum.py --diagnose
py -3 stock-momentum.py --show
py -3 stock-momentum.py --history
py -3 stock-momentum.py --version

WHAT v2 FIXES
-------------
1. Universe preflight prints each unresolved, ambiguous, duplicate-key or non-EQ
   symbol BEFORE downloading stock candles. Same-day cache is refreshed once if
   mappings fail. Default: exclude these symbols explicitly and rank the mapped
   EQ subset only if at least 95% of input symbols resolve. Report coverage and all
   exclusions in console, SQLite run metadata, HTML and excluded_stocks.csv.
   --strict-universe restores fail-on-any-exclusion, immediately. No guesswork
   aliases or BE/BZ inclusion. Unknown/data/API failures during candle fetching
   still fail early and preserve the last valid selections.
2. Market regime is ALWAYS measured as Nifty close > SMA50. Never edit it to True.
   --ignore-market-regime is a separate explicit research override, labeled
   FILTER BYPASSED in reports and recorded independently in SQLite. OFF without
   bypass produces zero selected stocks, but ranked_candidates.csv is available.
3. Expected sessions use the Upstox NSE market-timings API, including holidays
   and special sessions. Before configured 18:00 IST cutoff use a prior completed
   session. Require market close + 30 minutes as well, for special late sessions.
   If today's DAILY historical bar is missing after these gates, request the
   documented V3 INTRADAY endpoint with days/1 for today's daily bar and merge
   ONLY the exact current session into history. No fabricated date or minute-bar
   aggregation. Source is recorded per stock and in benchmark metadata.
   Provider daily-bar finality is not independently guaranteed by these buffers.
4. No silent older-session fallback. Missing expected-session data records
   WAITING_DATA and retains prior selections. Retry later or deliberately pass
   --allow-previous-session, which labels any fallback and enforces a 4-calendar-
   day maximum lag relative to expected session. --as-of always requires an exact
   session and cannot be combined with fallback. Past --as-of is checked against
   benchmark candles directly; it cannot rewind newer published state.
5. Detailed sanitized HTTP errors distinguish 401/403 and structured API errors
   from HTML/non-JSON gateway responses. GETs are paced and retry 429/5xx/network
   failures; authentication failure aborts immediately. No redirects forward tokens.
6. Every scan saves diagnostics, including preflight/date failures. No long 500-
   stock scan just to discover mapping errors. Version and input path are printed.

STRATEGY
--------
Default top 15 eligible stocks, strict ADT > INR 250,000,000 (25 crore).
Stock close > SMA50; 20-session return > 0; Nifty close > SMA50 unless bypassed.
VAM = (close / close_20_sessions_ago - 1) / (sample stddev of 20 daily log
returns * sqrt(252)). Sample stddev uses ddof=1; 20 returns need 21 closes.
At least 50 aligned completed sessions required with default parameters.
ATR = simple mean of 20 true ranges, informational only (not Wilder's ATR).
ADT = mean(close * volume), an approximation rather than reported traded value.
Tie scores use symbol ascending; never force 15 stocks when fewer qualify.
Contiguous short histories (e.g. IPOs) are excluded; missing/gapped/stale stock
sessions or invalid OHLCV block publication. No forward-fill or mixed-date ranks.
A >25% absolute daily price jump in the required window is excluded for corporate-
action review. No independent split/dividend/demerger adjustments are performed.
This heuristic can reject legitimate moves and cannot catch every corporate action.
No profitability/backtest claim. Research candidates are not buy/sell instructions.

STORAGE / REPORTS (all automatically generated under stock-data)
---------------------------------------------------------------
momentum.sqlite3: compatible with previous version; DO NOT DELETE your history.
reports/dashboard.html: self-contained selected list, filter status, scope/history.
reports/selected_stocks.csv: latest successful selected snapshot.
reports/ranked_candidates.csv: eligible stock ranks BEFORE the market gate;
  NOT selected stocks or holdings. Useful when actual market regime is OFF.
reports/excluded_stocks.csv: latest attempt's explicit universe mapping exclusions.
reports/new_additions.csv: additions in latest successful scan only.
reports/discovery_history.csv: permanent first discoveries and re-entries.
reports/latest.json, scan_NNNNNN.json: state and per-run audit including data source.
logs/screener.log, cache/instruments.json, screener.lock: runtime support outputs.
Reports are static; refresh after a scan or use --show to rebuild from SQLite.
If report export fails (e.g. file locked in Excel), close it and --show. SQLite
is authoritative; report failure does not erase a successful selection commit.

DISCOVERY HISTORY
-----------------
First-ever selected instrument key => FIRST_DISCOVERY. After absence, returning
key => RE_ENTRY. Original first-discovery timestamp never resets. Same selection
rerun/rank change => no duplicate event. Actual IST observation time is separate
from signal date. Regime-off, bypass toggles or changing the effective universe
can change membership and hence produce later re-entries. Failed/pending scans
never count as removals. A symbol change with same key is not a new discovery.
Persisted config_json includes runtime coverage, expected session and source.
The schema is backward compatible. Back up DB with scans stopped, or SQLite's
online backup API; do not copy only a live WAL database's main file.

Exit codes: 0 success, 2 failure, 3 waiting for required data, 130 interrupted.
Schedule after 18:15 IST; allow 20+ minutes if today's bars need two requests each.
GET request pacing defaults to 1.1 seconds; other apps also consume account limits.
An OS lock prevents overlap. No local credentials are embedded in this script.

Official API references:
https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/
https://upstox.com/developer/api-documentation/v3/get-intra-day-candle-data/
https://upstox.com/developer/api-documentation/get-market-timings/
https://upstox.com/developer/api-documentation/instruments/
"""


import argparse
from contextlib import contextmanager
import csv
from dataclasses import dataclass, asdict, replace
from datetime import date, datetime, time as dt_time, timedelta, timezone
from email.utils import parsedate_to_datetime
import gzip
import html
from http.client import HTTPException
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import sqlite3
import statistics
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, getproxies
import webbrowser

# India has no DST; fixed UTC+05:30 avoids a tzdata installation on Windows.
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
VERSION = "2.0.0"


# ============================================================================
# STRATEGY SETTINGS AND CALCULATIONS
# ============================================================================




class DataError(ValueError):
    """Incomplete/unreliable data: must not replace last good selections."""


class DataPending(DataError):
    """Required completed-session data has not arrived. Retain previous results."""


@dataclass(frozen=True)
class Config:
    top_n: int = 15
    momentum_days: int = 20
    volatility_days: int = 20
    sma_days: int = 50
    liquidity_days: int = 20
    atr_days: int = 20
    min_adt_inr: float = 250_000_000
    annualization_days: int = 252
    benchmark_key: str = "NSE_INDEX|Nifty 50"
    history_calendar_days: int = 240
    eod_ready_ist: str = "18:00"
    max_benchmark_age_days: int = 4
    request_interval_seconds: float = 1.1
    request_timeout_seconds: float = 30
    retries: int = 3
    max_abs_daily_return: float = 0.25
    strict_universe: bool = False
    min_universe_coverage: float = 0.95
    ignore_market_regime: bool = False
    allow_previous_session: bool = False
    refresh_instruments: bool = False

    @property
    def required_bars(self):
        return max(self.sma_days, self.momentum_days + 1,
                   self.volatility_days + 1, self.liquidity_days, self.atr_days + 1)

    def validate(self):
        cfg = self
        for name in ("top_n", "momentum_days", "volatility_days", "sma_days",
                     "liquidity_days", "atr_days", "annualization_days",
                     "history_calendar_days", "max_benchmark_age_days"):
            value = getattr(cfg, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if cfg.volatility_days < 2 or not 1 <= cfg.top_n <= 500:
            raise ValueError("volatility_days >= 2 and top_n in 1..500 required")
        for name in ("min_adt_inr", "request_interval_seconds", "request_timeout_seconds", "max_abs_daily_return"):
            value = getattr(cfg, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if cfg.request_interval_seconds < 1.0:
            raise ValueError("Use request_interval_seconds >= 1.0 for conservative pacing")
        if type(cfg.retries) is not int or not 0 <= cfg.retries <= 8:
            raise ValueError("retries must be in 0..8")
        if cfg.history_calendar_days < cfg.required_bars * 2 or cfg.history_calendar_days > 3650:
            raise ValueError("history_calendar_days must be >= 2 * required_bars and <= 3650")
        ready = dt_time.fromisoformat(cfg.eod_ready_ist)
        if ready.tzinfo or ready < dt_time(16, 0):
            raise ValueError("eod_ready_ist must be a local time at/after 16:00")
        if not isinstance(cfg.benchmark_key, str) or not cfg.benchmark_key.startswith("NSE_INDEX|"):
            raise ValueError("benchmark_key must identify an NSE index")
        for name in ("strict_universe", "ignore_market_regime", "allow_previous_session", "refresh_instruments"):
            if type(getattr(cfg, name)) is not bool:
                raise ValueError(f"{name} must be true or false")
        if not isinstance(cfg.min_universe_coverage, (int, float)) or not 0 < cfg.min_universe_coverage <= 1:
            raise ValueError("min_universe_coverage must be in (0, 1]")
        return cfg

    def dictionary(self):
        return asdict(self)


def load_universe(path):
    obj = json.loads(path.read_text(encoding="utf-8-sig"))
    values = obj.get("symbols")
    if obj.get("exchange") != "NSE" or obj.get("segment") != "NSE_EQ":
        raise ValueError("Universe must use exchange NSE and segment NSE_EQ")
    if not isinstance(values, list) or not values:
        raise ValueError("universe.symbols must be a nonempty JSON array")
    if any(not isinstance(s, str) or not s or s != s.strip().upper() for s in values):
        raise ValueError("Symbols must be nonempty uppercase strings without surrounding whitespace")
    if len(values) != len(set(values)):
        raise ValueError("Duplicate universe symbols; correct universe.json")
    if obj.get("count") != len(values):
        raise ValueError(f"Universe count must equal symbols length ({len(values)})")
    return values


def completed_cutoff(now, cfg):
    now = now.astimezone(IST)
    # No guesses about holidays: actual benchmark candles determine session dates.
    return now.date() if now.time() >= dt_time.fromisoformat(cfg.eod_ready_ist) else now.date() - timedelta(days=1)


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


def parse_candles(raw, cutoff):
    if not isinstance(raw, list):
        raise DataError("Candles must be an array")
    by_day = {}
    for row in raw:
        try:
            if len(row) < 6:
                raise ValueError()
            stamp = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError()
            day = stamp.astimezone(IST).date()
            if day > cutoff:
                continue
            o, h, l, c, v = map(float, row[1:6])
            if not all(math.isfinite(x) for x in (o, h, l, c, v)):
                raise ValueError()
            if min(o, h, l, c) <= 0 or v < 0 or l > min(o, c) or h < max(o, c) or h < l:
                raise ValueError()
            bar = Bar(day, o, h, l, c, v)
            if day in by_day and by_day[day] != bar:
                raise DataError(f"Conflicting duplicate candles on {day}")
            by_day[day] = bar
        except (TypeError, ValueError, IndexError, AttributeError) as exc:
            if isinstance(exc, DataError):
                raise
            raise DataError("Invalid candle timestamp/OHLCV") from None
    return sorted(by_day.values(), key=lambda b: b.day)


def assess(symbol, key, bars, benchmark_bars, cfg):
    """Return metrics and exclusion reasons; data-integrity errors raise DataError."""
    if not bars:
        raise DataError("No completed candles")
    if bars[-1].day != benchmark_bars[-1].day:
        raise DataError(f"Stale stock candle: {bars[-1].day}; benchmark: {benchmark_bars[-1].day}")
    expected = [b.day for b in benchmark_bars[-cfg.required_bars:]]
    observed = [b.day for b in bars[-cfg.required_bars:]]
    result = {"symbol": symbol, "instrument_key": key, "as_of": bars[-1].day.isoformat()}
    if len(bars) < cfg.required_bars:
        # Only a contiguous short suffix counts as insufficient listing/history.
        if observed != expected[-len(observed):]:
            raise DataError("Missing sessions in short stock history")
        return {**result, "eligible": False, "reasons": ["insufficient_history"]}
    if observed != expected:
        raise DataError("Stock sessions do not match benchmark sessions; no forward filling")
    bars = bars[-cfg.required_bars:]
    closes = [b.close for b in bars]
    daily_simple = [b / a - 1 for a, b in zip(closes, closes[1:])]
    ret = closes[-1] / closes[-cfg.momentum_days - 1] - 1
    log_returns = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    vol = statistics.stdev(log_returns[-cfg.volatility_days:]) * math.sqrt(cfg.annualization_days)
    sma = statistics.mean(closes[-cfg.sma_days:])
    adt = statistics.mean(b.close * b.volume for b in bars[-cfg.liquidity_days:])
    trs = [max(b.high - b.low, abs(b.high - prev.close), abs(b.low - prev.close))
           for prev, b in zip(bars, bars[1:])]
    atr = statistics.mean(trs[-cfg.atr_days:])
    reasons = []
    if adt <= cfg.min_adt_inr:
        reasons.append("liquidity_not_above_threshold")
    if closes[-1] <= sma:
        reasons.append("close_not_above_sma")
    if ret <= 0:
        reasons.append("nonpositive_absolute_return")
    if vol <= 1e-12:
        reasons.append("zero_or_negligible_volatility")
    if any(abs(r) > cfg.max_abs_daily_return for r in daily_simple):
        reasons.append("large_price_jump_review_corporate_actions")
    return {**result, "close": closes[-1], "sma": sma, "momentum_return": ret,
            "annual_volatility": vol, "vam": ret / vol if vol > 1e-12 else None,
            "adt_inr": adt, "atr": atr, "eligible": not reasons, "reasons": reasons}


def rank_candidates(metrics):
    rows = [dict(m) for m in metrics if m.get("eligible")]
    rows.sort(key=lambda r: (-r["vam"], r["symbol"]))
    for i, row in enumerate(rows, 1):
        row["rank"] = i
    return rows


# ============================================================================
# STANDARD-LIBRARY HTTP TRANSPORT
# ============================================================================

class _NoRedirect(HTTPRedirectHandler):
    """Never forward a bearer token through a redirected request."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _Response:
    def __init__(self, status_code, headers, content):
        self.status_code = status_code
        self.headers = headers
        self.content = content

    def json(self):
        return json.loads(self.content)


class _HttpSession:
    """Small standard-library GET transport; no third-party requests dependency."""
    def __init__(self):
        self.opener = build_opener(_NoRedirect())

    def get(self, url, headers, timeout, allow_redirects=False):
        if allow_redirects:
            raise ValueError("Redirects are disabled for credential safety")
        req = Request(url, headers=headers, method="GET")
        try:
            with self.opener.open(req, timeout=timeout) as response:
                return _Response(response.status, response.headers, response.read())
        except HTTPError as exc:
            # HTTP errors are structured responses; NEVER log their raw body.
            with exc:
                return _Response(exc.code, exc.headers, exc.read())

    def close(self):
        pass  # every urllib response is closed by its context manager





# ============================================================================
# CREDENTIAL LOADING AND UPSTOX MARKET DATA
# ============================================================================

MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"


class ApiError(RuntimeError):
    def __init__(self, message, status_code=None, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details or {}



class AuthError(ApiError):
    pass


def read_token(root):
    """Read only UPSTOX_ACCESS_TOKEN from local env.txt; never execute its contents."""
    env_path = root / "env.txt"
    if not env_path.is_file():
        raise ValueError(f"Missing {env_path}; keep your existing env.txt beside this script")
    token = ""
    token_entries = 0
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep or key.strip() != "UPSTOX_ACCESS_TOKEN":
            continue
        token_entries += 1
        if token_entries > 1:
            raise ValueError("Multiple UPSTOX_ACCESS_TOKEN entries in env.txt; keep exactly one current token")
        value = value.strip()
        if value.startswith(("'", '"')):
            end = value.find(value[0], 1)
            if end < 0:
                raise ValueError("Unclosed quote for UPSTOX_ACCESS_TOKEN in env.txt")
            tail = value[end + 1:].strip()
            if tail and not tail.startswith("#"):
                raise ValueError("Unexpected text after quoted UPSTOX_ACCESS_TOKEN in env.txt")
            value = value[1:end]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        token = value.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()  # send exactly one Bearer prefix
    if len(token) < 20 or token.lower() in ("your_access_token", "your_current_real_token", "replace_me"):
        raise ValueError("UPSTOX_ACCESS_TOKEN is missing/placeholder; update your local env.txt")
    if any(ch.isspace() for ch in token):
        raise ValueError("UPSTOX_ACCESS_TOKEN must not contain whitespace")
    return token


def atomic_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def safe_error_text(value, token):
    """Redact credentials/identifiers before logging allowlisted API error fields."""
    if not isinstance(value, (str, int)):
        return ""
    text = str(value)
    if token:
        for secret in (token, quote(token, safe="")):
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)bearer\s+[^\s\"',;<>]+", "Bearer [REDACTED]", text)
    text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?", "[REDACTED JWT]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[REDACTED EMAIL]", text)
    text = re.sub(r"[A-Za-z0-9_./+=-]{64,}", "[REDACTED LONG VALUE]", text)
    return " ".join(text.split())[:400]


def response_error_details(response, token):
    """Never return raw HTML, success payloads, request headers or profile fields."""
    details = {"response_kind": "non-JSON", "errors": []}
    raw = getattr(response, "content", b"")
    try:
        obj = response.json()
        details["response_kind"] = "JSON"
    except (ValueError, TypeError, UnicodeError):
        obj = None
        if isinstance(raw, bytes) and (b"<html" in raw[:2048].lower() or b"<!doctype html" in raw[:2048].lower()):
            details["response_kind"] = "HTML"
    if isinstance(obj, dict):
        errors = obj.get("errors", [])
        if isinstance(errors, dict):
            errors = [errors]
        if not isinstance(errors, list):
            errors = []
        for error in [obj] + errors[:5]:
            if not isinstance(error, dict):
                continue
            code = safe_error_text(error.get("errorCode", error.get("error_code", "")), token)
            message = safe_error_text(error.get("message", ""), token)
            if code or message:
                details["errors"].append({"code": code, "message": message})
    return details


def http_failure_message(response, url, details):
    endpoint = urlsplit(url).path  # current call sites contain no secret/query in path
    parts = [f"HTTP {response.status_code} on {endpoint}", f"response={details['response_kind']}"]
    for error in details["errors"]:
        parts.append((error["code"] + ": " + error["message"]).strip(": "))
    if response.status_code == 401:
        parts.append("Unauthorized for this request; inspect the API error code")
    elif response.status_code == 403:
        parts.append("Forbidden; this alone does not prove that the token is invalid")
    if not details["errors"]:
        parts.append("No structured API error supplied; raw response omitted for privacy")
    return " | ".join(parts)


class Upstox:
    def __init__(self, token, cfg, data_dir):
        self.cfg = cfg
        self.data_dir = data_dir
        self.session = _HttpSession()
        self.token = token
        self.next_request = 0.0
        self.instrument_cache_used = False
        self.current_day_ready = False
        self.bar_sources = {}

    def close(self):
        self.session.close()

    def get(self, url, authenticated=True):
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "User-Agent": "VAM-AF-NSE-Screener/2.0 (Python urllib)"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.token}"
        for attempt in range(self.cfg.retries + 1):
            time.sleep(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + self.cfg.request_interval_seconds
            try:
                response = self.session.get(url, headers=headers,
                                            timeout=self.cfg.request_timeout_seconds,
                                            allow_redirects=False)
            except (URLError, OSError, HTTPException):
                # No raw exceptions/response bodies: they can expose sensitive request data.
                if attempt == self.cfg.retries:
                    raise ApiError("Market-data network request failed after retries") from None
                time.sleep(min(2 ** attempt, 30))
                continue
            if response.status_code in (401, 403):
                details = response_error_details(response, self.token)
                raise AuthError(http_failure_message(response, url, details), response.status_code, details)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self.cfg.retries:
                    details = response_error_details(response, self.token)
                    raise ApiError(http_failure_message(response, url, details) + " | retries exhausted",
                                   response.status_code, details)
                delay = 2 ** attempt
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    try:
                        delay = max(delay, parsedate_to_datetime(retry_after).timestamp() - time.time())
                    except (ValueError, TypeError, OverflowError):
                        pass
                # Never retry earlier than requested. Long delays abort this run instead.
                if delay > 300:
                    raise ApiError("Rate limited; Retry-After > 300 seconds. Retry the screener later")
                time.sleep(delay)
                continue
            if response.status_code != 200:
                details = response_error_details(response, self.token)
                raise ApiError(http_failure_message(response, url, details), response.status_code, details)
            return response
        raise ApiError("Unreachable retry state")

    def instruments(self, refresh=False):
        path = self.data_dir / "cache" / "instruments.json"
        today = datetime.now(IST).date().isoformat()
        self.instrument_cache_used = False
        if path.is_file() and not (refresh or self.cfg.refresh_instruments):
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved["date"] == today and isinstance(saved["instruments"], list) and saved["instruments"]:
                    self.instrument_cache_used = True
                    return saved["instruments"]
            except (ValueError, KeyError):
                pass
        content = self.get(MASTER_URL, authenticated=False).content
        try:
            if content[:2] == b"\x1f\x8b":
                content = gzip.decompress(content)
            rows = json.loads(content)
            if not isinstance(rows, list) or not rows:
                raise ValueError()
        except (ValueError, OSError, EOFError):
            raise DataError("Invalid instrument master response") from None
        atomic_json(path, {"date": today, "instruments": rows})
        return rows

    def candle_payload(self, url, cutoff):
        response = self.get(url)
        try:
            obj = response.json()
            if obj.get("status") != "success":
                raise ValueError()
            raw = obj["data"]["candles"]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise DataError("Invalid candle API response") from None
        return parse_candles(raw, cutoff)

    def history(self, key, cutoff):
        start = cutoff - timedelta(days=self.cfg.history_calendar_days)
        encoded = quote(key, safe="")
        url = f"https://api.upstox.com/v3/historical-candle/{encoded}/days/1/{cutoff}/{start}"
        bars = self.candle_payload(url, cutoff)
        self.bar_sources[key] = "historical_v3"
        now = datetime.now(IST)
        # Use the documented DAILY intraday endpoint, not a made-up aggregation
        # of minute LTPs. Only fetch today's bar after calendar-confirmed close,
        # 30-minute buffer, AND the configured EOD cutoff.
        if (cutoff == now.date() and self.current_day_ready
                and now.time() >= dt_time.fromisoformat(self.cfg.eod_ready_ist)
                and (not bars or bars[-1].day < cutoff)):
            today_url = f"https://api.upstox.com/v3/historical-candle/intraday/{encoded}/days/1"
            today_bars = self.candle_payload(today_url, cutoff)
            today_bars = [bar for bar in today_bars if bar.day == cutoff]
            if today_bars:
                bars = sorted([bar for bar in bars if bar.day != cutoff] + today_bars, key=lambda bar: bar.day)
                self.bar_sources[key] = "historical_v3+intraday_daily_v3_after_close"
        return bars

    def timing_end(self, day):
        response = self.get(f"https://api.upstox.com/v2/market/timings/{day}", authenticated=False)
        try:
            obj = response.json()
            if obj.get("status") != "success" or not isinstance(obj["data"], list):
                raise ValueError()
            ends = []
            for row in obj["data"]:
                if not isinstance(row, dict):
                    raise ValueError()
                if row.get("exchange") != "NSE":
                    continue
                opening = datetime.fromtimestamp(float(row["start_time"]) / 1000, IST)
                closing = datetime.fromtimestamp(float(row["end_time"]) / 1000, IST)
                if opening.date() != day or closing.date() != day or closing <= opening:
                    raise ValueError()
                ends.append(closing)
            return max(ends) if ends else None
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError, OSError):
            raise DataError(f"Invalid NSE market timings for {day}; cannot verify completed session") from None

    def expected_session(self, cutoff, now):
        # Consult exchange timings, including holidays and special weekend/evening
        # sessions. No assumption that every weekday is a trading day.
        for offset in range(15):
            day = cutoff - timedelta(days=offset)
            closing = self.timing_end(day)
            if closing is None:
                continue
            ready_at = max(closing + timedelta(minutes=30),
                           datetime.combine(day, dt_time.fromisoformat(self.cfg.eod_ready_ist), IST))
            if now >= ready_at:
                self.current_day_ready = day == now.date()
                return day
        raise DataError("No completed NSE session found in 15 days of market timings")


EXCLUSION_REASONS = {"unresolved_symbol", "non_eq_series", "ambiguous_symbol", "duplicate_instrument"}


def resolve_symbols(master, symbols):
    matches, seen_series = {}, {}
    for row in master:
        if not isinstance(row, dict):
            raise DataError("Invalid instrument master record")
        if row.get("segment") != "NSE_EQ":
            continue
        symbol, series, key = row.get("trading_symbol"), row.get("instrument_type"), row.get("instrument_key")
        if not isinstance(symbol, str):
            continue
        seen_series.setdefault(symbol, set()).add(str(series))
        if series == "EQ" and isinstance(key, str) and key.startswith("NSE_EQ|"):
            matches.setdefault(symbol, set()).add(key)
    resolved, issues, used_keys = {}, [], set()
    for symbol in symbols:
        keys = matches.get(symbol, set())
        if len(keys) == 1:
            key = next(iter(keys))
            if key in used_keys:
                issues.append({"symbol": symbol, "reason": "duplicate_instrument",
                               "detail": "Same instrument key already mapped under another input symbol; no double counting"})
            else:
                resolved[symbol] = key
                used_keys.add(key)
        elif len(keys) > 1:
            issues.append({"symbol": symbol, "reason": "ambiguous_symbol",
                           "detail": "Multiple EQ instrument keys; no guessed mapping"})
        elif symbol in seen_series:
            issues.append({"symbol": symbol, "reason": "non_eq_series",
                           "detail": "Available series: " + ", ".join(sorted(seen_series[symbol])) + "; this screener accepts EQ only"})
        else:
            detail = "No exact NSE_EQ / EQ match in the current master; check universe.json"
            suggestion = {"HEG": "HEGAM", "ABBOTIND": "ABBOTINDIA"}.get(symbol)
            if suggestion and suggestion in matches:
                detail += f". Possible correction: {suggestion}; review corporate actions before changing it (not auto-substituted)"
            issues.append({"symbol": symbol, "reason": "unresolved_symbol", "detail": detail})
    return resolved, issues


def universe_preflight(client, symbols, cfg):
    master = client.instruments()
    resolved, issues = resolve_symbols(master, symbols)
    if issues and getattr(client, "instrument_cache_used", False):
        print("Refreshing cached instrument master once before classifying exclusions...")
        resolved, issues = resolve_symbols(client.instruments(refresh=True), symbols)
    for issue in issues:
        print(f"EXCLUDED {issue['symbol']}: {issue['reason']} - {issue['detail']}")
    ratio = len(resolved) / len(symbols)
    print(f"Universe: {len(resolved)}/{len(symbols)} mapped ({ratio:.1%}); {len(issues)} explicit exclusions")
    return resolved, issues, ratio


def enforce_universe_policy(resolved, issues, ratio, cfg):
    if not resolved:
        raise DataError("No instruments resolved; check the universe/master")
    if issues and cfg.strict_universe:
        raise DataError(f"Strict-universe mode: {len(issues)} exclusions. Stopped BEFORE stock candle downloads")
    if ratio < cfg.min_universe_coverage:
        raise DataError(f"Mapped coverage {ratio:.1%} below required {cfg.min_universe_coverage:.1%}; scan not published")


# ============================================================================
# PERSISTENT SQLITE SELECTIONS AND DISCOVERIES
# ============================================================================

@contextmanager
def process_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    acquired = False
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Another screener/report process is running") from None
        acquired = True
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
          id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
          requested_cutoff TEXT NOT NULL, as_of TEXT, status TEXT NOT NULL,
          regime_on INTEGER, benchmark_close REAL, benchmark_sma REAL,
          config_json TEXT NOT NULL, universe_json TEXT NOT NULL,
          issues_json TEXT NOT NULL DEFAULT '[]', error TEXT
        );
        CREATE TABLE IF NOT EXISTS candidates (
          run_id INTEGER NOT NULL REFERENCES runs(id), symbol TEXT NOT NULL,
          instrument_key TEXT NOT NULL, rank INTEGER NOT NULL, metrics_json TEXT NOT NULL,
          PRIMARY KEY(run_id, instrument_key)
        );
        CREATE TABLE IF NOT EXISTS selections (
          run_id INTEGER NOT NULL REFERENCES runs(id), symbol TEXT NOT NULL,
          instrument_key TEXT NOT NULL, rank INTEGER NOT NULL, metrics_json TEXT NOT NULL,
          PRIMARY KEY(run_id, instrument_key)
        );
        CREATE TABLE IF NOT EXISTS discoveries (
          instrument_key TEXT PRIMARY KEY, first_symbol TEXT NOT NULL,
          first_discovered_at TEXT NOT NULL, first_signal_date TEXT NOT NULL,
          first_run_id INTEGER NOT NULL REFERENCES runs(id)
        );
        CREATE TABLE IF NOT EXISTS addition_events (
          id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
          instrument_key TEXT NOT NULL, symbol TEXT NOT NULL,
          event_type TEXT NOT NULL CHECK(event_type IN ('FIRST_DISCOVERY','RE_ENTRY')),
          discovered_at TEXT NOT NULL, signal_date TEXT NOT NULL,
          UNIQUE(run_id, instrument_key)
        );
        CREATE INDEX IF NOT EXISTS runs_status ON runs(status, id);
        CREATE VIEW IF NOT EXISTS current_selected AS
          SELECT s.*, d.first_discovered_at, d.first_signal_date
          FROM selections s JOIN discoveries d USING(instrument_key)
          WHERE s.run_id=(SELECT MAX(id) FROM runs WHERE status='SUCCESS');
        """)

    def close(self):
        self.db.close()

    def recover_interrupted(self, now):
        # Caller must hold process lock, so no other legitimate run is active.
        with self.db:
            self.db.execute("UPDATE runs SET status='FAILED', finished_at=?, error='Interrupted before atomic publication' WHERE status='RUNNING'", (now,))

    def start(self, now, cutoff, cfg, symbols):
        with self.db:
            return self.db.execute("""INSERT INTO runs(started_at,requested_cutoff,status,config_json,universe_json)
              VALUES (?,?,'RUNNING',?,?)""", (now, cutoff, json.dumps(cfg), json.dumps(symbols))).lastrowid

    def latest(self):
        row = self.db.execute("SELECT * FROM runs WHERE status='SUCCESS' ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def set_context(self, run_id, context):
        with self.db:
            row = self.db.execute("SELECT config_json FROM runs WHERE id=?", (run_id,)).fetchone()
            cfg = json.loads(row[0])
            cfg["runtime"] = context
            self.db.execute("UPDATE runs SET config_json=? WHERE id=?", (json.dumps(cfg), run_id))

    def finish_failure(self, run_id, now, issues, error, as_of=None, status="FAILED"):
        if status not in ("FAILED", "WAITING_DATA"):
            raise ValueError("Invalid failure status")
        with self.db:
            self.db.execute("UPDATE runs SET status=?,finished_at=?,issues_json=?,error=?,as_of=? WHERE id=?",
                            (status, now, json.dumps(issues), error, as_of, run_id))

    def publish(self, run_id, now, as_of, regime_on, benchmark_close, benchmark_sma,
                candidates, top_n, issues, filter_bypassed=False):
        previous = self.latest()
        if previous and as_of < previous["as_of"]:
            raise ValueError("Refusing to replace current state with an older signal date")
        selected = candidates[:top_n] if (regime_on or filter_bypassed) else []
        # Short transaction; any exception rolls back snapshots AND events.
        with self.db:
            # Store the override independently from the actual market measurement.
            cfg_row = self.db.execute("SELECT config_json FROM runs WHERE id=?", (run_id,)).fetchone()
            stored_cfg = json.loads(cfg_row[0])
            stored_cfg["ignore_market_regime"] = bool(filter_bypassed)
            self.db.execute("UPDATE runs SET config_json=? WHERE id=?", (json.dumps(stored_cfg), run_id))
            old_keys = {r[0] for r in self.db.execute("SELECT instrument_key FROM current_selected")}
            for table, rows in (("candidates", candidates), ("selections", selected)):
                self.db.executemany(f"INSERT INTO {table} VALUES (?,?,?,?,?)", [
                    (run_id, r["symbol"], r["instrument_key"], r["rank"], json.dumps(r, allow_nan=False)) for r in rows])
            for row in selected:
                key = row["instrument_key"]
                if key in old_keys:
                    continue
                first = self.db.execute("SELECT 1 FROM discoveries WHERE instrument_key=?", (key,)).fetchone() is None
                if first:
                    self.db.execute("INSERT INTO discoveries VALUES (?,?,?,?,?)",
                                    (key, row["symbol"], now, as_of, run_id))
                self.db.execute("""INSERT INTO addition_events
                  (run_id,instrument_key,symbol,event_type,discovered_at,signal_date) VALUES (?,?,?,?,?,?)""",
                  (run_id, key, row["symbol"], "FIRST_DISCOVERY" if first else "RE_ENTRY", now, as_of))
            self.db.execute("""UPDATE runs SET status='SUCCESS',finished_at=?,as_of=?,regime_on=?,
              benchmark_close=?,benchmark_sma=?,issues_json=? WHERE id=?""",
              (now, as_of, int(regime_on), benchmark_close, benchmark_sma, json.dumps(issues), run_id))
        return selected

    def report_data(self):
        latest = self.latest()
        last_attempt = self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        selected = []
        for row in self.db.execute("SELECT * FROM current_selected ORDER BY rank"):
            selected.append({**json.loads(row["metrics_json"]), "first_discovered_at": row["first_discovered_at"],
                             "first_signal_date": row["first_signal_date"]})
        events = [dict(r) for r in self.db.execute("""SELECT e.*, d.first_discovered_at, d.first_signal_date
          FROM addition_events e JOIN discoveries d USING(instrument_key) ORDER BY e.id DESC""")]
        candidates = []
        if latest:
            candidates = [json.loads(r[0]) for r in self.db.execute(
                "SELECT metrics_json FROM candidates WHERE run_id=? ORDER BY rank", (latest["id"],))]
        return {"latest_success": latest, "last_attempt": dict(last_attempt) if last_attempt else None,
                "selected": selected, "events": events, "candidates": candidates}


# ============================================================================
# HTML, CSV AND CONSOLE REPORTS
# ============================================================================

SELECT_FIELDS = ["rank", "symbol", "instrument_key", "as_of", "close", "sma", "momentum_return",
                 "annual_volatility", "vam", "adt_inr", "atr", "first_discovered_at", "first_signal_date",
                 "candle_source", "market_regime_on", "market_filter_bypassed"]
EVENT_FIELDS = ["id", "run_id", "symbol", "instrument_key", "event_type", "discovered_at", "signal_date",
                "first_discovered_at", "first_signal_date"]


def atomic_text(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def csv_text(rows, fields):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        # Defensive spreadsheet formula protection for editable symbols.
        safe = {k: ("'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v)
                for k, v in row.items()}
        writer.writerow(safe)
    return out.getvalue()


def esc(value):
    return html.escape(str(value))


def table(headers, rows):
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>" for row in rows)
    if not body:
        body = f'<tr><td colspan="{len(headers)}" class="muted">No records.</td></tr>'
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'



def run_regime_label(run):
    if not run:
        return "NO VALID SCAN YET"
    actual = run["benchmark_close"] > run["benchmark_sma"]
    cfg = json.loads(run["config_json"])
    label = "ON" if actual else "OFF"
    if cfg.get("ignore_market_regime", False):
        return f"FILTER BYPASSED (actual regime {label})"
    if bool(run["regime_on"]) != actual:
        return f"LEGACY FLAG INCONSISTENT (actual regime {label}); rerun current script"
    return f"REGIME {label}"


def run_scope(run):
    if not run:
        return "No published scan"
    context = json.loads(run["config_json"]).get("runtime", {})
    text = f"Universe: {context.get('resolved_count', '?')}/{context.get('universe_count', '?')} mapped; {context.get('excluded_count', '?')} exclusions"
    text += f" | expected session: {context.get('expected_session', run['as_of'])} | signal: {run['as_of']}"
    if context.get("prior_session_used"):
        text += " | PRIOR-SESSION FALLBACK EXPLICITLY ENABLED"
    return text


def write_reports(store, data_dir):
    data = store.report_data()
    report_dir = data_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    latest = data["latest_success"]
    attempt = data["last_attempt"]
    events = data["events"]
    additions = [e for e in events if latest and e["run_id"] == latest["id"]]
    data["new_additions_last_success"] = additions
    data["report_generated_at"] = datetime.now(IST).isoformat(timespec="seconds")
    atomic_json(report_dir / "latest.json", data)
    atomic_text(report_dir / "selected_stocks.csv", csv_text(data["selected"], SELECT_FIELDS))
    atomic_text(report_dir / "new_additions.csv", csv_text(additions, EVENT_FIELDS))
    atomic_text(report_dir / "discovery_history.csv", csv_text(events, EVENT_FIELDS))
    cfg = json.loads(latest["config_json"]) if latest else {}
    status = run_regime_label(latest)
    failure = ""
    if attempt and attempt["status"] != "SUCCESS":
        failure = f'<div class="alert">Latest attempt #{attempt["id"]}: {esc(attempt["status"])} — {esc(attempt["error"] or "in progress")}. Selections below are from the last successful scan, not this attempt.</div>'
    session = latest["as_of"] if latest else "—"
    cutoff = latest["requested_cutoff"] if latest else "—"
    benchmark = f'{latest["benchmark_close"]:,.2f} / {latest["benchmark_sma"]:,.2f}' if latest else "—"
    rows = [[r["rank"], r["symbol"], f'{r["close"]:,.2f}', f'{r["momentum_return"]:.2%}',
             f'{r["annual_volatility"]:.2%}', f'{r["vam"]:.4f}', f'{r["adt_inr"] / 1e7:.2f}',
             f'{r["atr"]:.2f}', r["first_discovered_at"]] for r in data["selected"]]
    event_headers = ["Symbol", "Event", "Discovered at (IST)", "Signal date", "First discovered (IST)", "Run"]
    def event_rows(items):
        return [[e["symbol"], e["event_type"], e["discovered_at"], e["signal_date"], e["first_discovered_at"], e["run_id"]] for e in items]
    issues = json.loads(attempt["issues_json"]) if attempt else []
    exclusions = [i for i in issues if i.get("reason") in EXCLUSION_REASONS]
    atomic_text(report_dir / "excluded_stocks.csv", csv_text(exclusions, ["symbol", "reason", "detail"]))
    atomic_text(report_dir / "ranked_candidates.csv", csv_text(data["candidates"], SELECT_FIELDS))
    issue_rows = [[i.get("symbol", "BENCHMARK"), i.get("reason", ""), i.get("detail", "")] for i in issues]
    page = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VAM-AF · NSE Swing Screener</title><style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}body{{margin:0;background:#0d1421;color:#e4ebf5;font:15px system-ui,Segoe UI,sans-serif}}main{{max-width:1440px;margin:auto;padding:40px 28px}}.eyebrow{{color:#55d6b7;letter-spacing:.16em;font-size:12px;font-weight:700}}h1{{font-size:34px;margin:10px 0}}h2{{font-size:20px;margin:0 0 8px}}p,.muted{{color:#9caec4;line-height:1.6}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;margin:28px 0}}.card,section{{background:#141f30;border:1px solid #28394e;border-radius:12px;padding:22px}}.card strong{{display:block;font-size:21px;margin:10px 0;color:#6de6c7}}section{{margin:20px 0}}.scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:13px;margin-top:18px;white-space:nowrap}}th{{text-align:left;color:#97b1ce;font-size:11px;text-transform:uppercase;letter-spacing:.05em}}th,td{{padding:13px 12px;border-bottom:1px solid #29394e}}tbody tr:hover{{background:#1b2c42}}.alert{{background:#422c1e;border:1px solid #a7713e;border-radius:10px;padding:18px;line-height:1.6;margin-top:22px}}.badge{{display:inline-block;background:#213c3b;color:#6de6c7;padding:6px 12px;border-radius:20px;font-size:12px}}footer{{font-size:12px;color:#8194ad;line-height:1.7}}code{{color:#bdd4ed}}summary{{cursor:pointer;color:#bdd4ed}}
</style></head><body><main><div class="eyebrow">NSE CASH EQUITIES / DAILY SIGNALS</div>
<h1>Volatility-Adjusted Momentum</h1><p>Absolute-filter swing screener · Read-only market data · No orders or position management</p><span class="badge">{esc(status)}</span><p>{esc(run_scope(latest))}</p>{failure}
<div class="cards"><div class="card">Signal session<strong>{esc(session)}</strong>Requested cutoff: {esc(cutoff)}</div>
<div class="card">Selected stocks<strong>{len(data["selected"])} / {cfg.get("top_n", 15)}</strong>Liquidity &gt; ₹{cfg.get("min_adt_inr", 250000000) / 1e7:g} crore ADT</div>
<div class="card">Nifty 50 close / SMA<strong>{benchmark}</strong>Strictly above SMA required</div>
<div class="card">Additions in last valid scan<strong>{len(additions)}</strong>{len(events)} lifetime addition events</div></div>
<section><h2>Selected stocks</h2><p>Latest successful snapshot, not holdings. First-discovery timestamps never reset. An enforced regime-off scan has no selected stocks. An explicit bypass is labeled above; eligible stock ranks remain available in ranked_candidates.csv.</p>
{table(["Rank", "Symbol", "Close ₹", f'{cfg.get("momentum_days",20)}D return', "Annual vol.", "VAM", "ADT ₹ crore", "ATR ₹", "First discovered (IST)"], rows)}</section>
<section><h2>New additions · last successful scan</h2><p>FIRST_DISCOVERY means first-ever selection. RE_ENTRY means a return after absence. Re-running an unchanged selection adds no duplicate events.</p>{table(event_headers, event_rows(additions))}</section>
<section><h2>Discovery history · all dates</h2><p>Permanent separate list of every first discovery and re-entry, newest first. Discovery time is when this screener observed selection; signal date is the candle date.</p>{table(event_headers, event_rows(events))}</section>
<section><details><summary>Latest attempt diagnostics ({len(issues)})</summary>{table(["Symbol", "Reason", "Detail"], issue_rows)}</details></section>
<footer>Report generated: {esc(data["report_generated_at"])} · Last successful completion: {esc(latest["finished_at"] if latest else "none")}<br>
This is a static report; rerun the screener or use --show to refresh. Expected sessions use Upstox NSE market timings. Dates are never relabeled. Current-session daily bars may come from the daily intraday endpoint only after confirmed close and the configured buffer; provider finality is not independently guaranteed.<br>
API candle adjustment status is not assumed. Verify corporate actions, listings, liquidity and data quality independently. This is a research screener, not investment advice or a backtested performance claim.</footer>
</main></body></html>'''
    atomic_text(report_dir / "dashboard.html", page)
    return data, report_dir / "dashboard.html"


def print_report(data, full_history=False):
    latest = data["latest_success"]
    attempt = data["last_attempt"]
    if attempt and attempt["status"] != "SUCCESS":
        print(f'WARNING: Latest attempt #{attempt["id"]} {attempt["status"]}: {attempt["error"]}')
        print("Previous successful selections, if any, are retained.")
    if latest:
        print(f'\nSignal date: {latest["as_of"]} | requested cutoff: {latest["requested_cutoff"]} | '
              f'{run_regime_label(latest)} | run #{latest["id"]}')
        print(run_scope(latest))
        print(f'Completed at: {latest["finished_at"]}')
    else:
        print("No successful scan stored yet.")
    print("\nSELECTED STOCKS")
    print(f'{"Rank":<6}{"Symbol":<16}{"VAM":>10}{"Return":>11}{"ADT(cr)":>12}  First discovered (IST)')
    for r in data["selected"]:
        print(f'{r["rank"]:<6}{r["symbol"]:<16}{r["vam"]:>10.4f}{r["momentum_return"]:>10.2%}{r["adt_inr"]/1e7:>12.2f}  {r["first_discovered_at"]}')
    if not data["selected"]:
        print("(none)")
    print("\nNEW ADDITIONS - LAST SUCCESSFUL SCAN")
    for event in data["new_additions_last_success"]:
        print(f'{event["symbol"]:<16} {event["event_type"]:<16} {event["discovered_at"]}  signal={event["signal_date"]}')
    if not data["new_additions_last_success"]:
        print("(none)")
    if full_history:
        print("\nALL DISCOVERY / RE-ENTRY EVENTS (permanent history)")
        for event in data["events"]:
            print(f'{event["symbol"]:<16} {event["event_type"]:<16} {event["discovered_at"]}  signal={event["signal_date"]}')
        if not data["events"]:
            print("(none)")
    else:
        print(f'\nPermanent history: {len(data["events"])} events; see dashboard.html, discovery_history.csv or --history.')


# ============================================================================
# END-TO-END SCAN AND COMMAND LINE
# ============================================================================

def now_ist():
    return datetime.now(IST).isoformat(timespec="seconds")


def configure_logging(data_dir):
    folder = data_dir / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("vam")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(folder / "screener.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        # Explicit IST log timestamp, independent of host timezone.
        formatter = logging.Formatter("%(asctime)s IST %(levelname)s %(message)s")
        formatter.converter = lambda timestamp: datetime.fromtimestamp(timestamp, IST).timetuple()
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def scan(root, data_dir, store, cfg, symbols, cutoff, exact_date, logger):
    run_id = store.start(now_ist(), cutoff.isoformat(), cfg.dictionary(), symbols)
    client = None
    issues, metrics, ranked, as_of = [], [], [], None
    context = {"version": VERSION, "universe_count": len(symbols), "excluded_count": 0,
               "resolved_count": 0, "filter_bypassed": cfg.ignore_market_regime}
    status, failure_message = "RUNNING", None
    try:
        client = Upstox(read_token(root), cfg, data_dir)
        print(f'Run #{run_id}: {len(symbols)} input symbols; cutoff {cutoff}; ADT > INR {cfg.min_adt_inr:,.0f}')
        resolved, mapping_issues, coverage = universe_preflight(client, symbols, cfg)
        issues.extend(mapping_issues)
        context.update(resolved_count=len(resolved), excluded_count=len(mapping_issues), coverage=coverage)
        store.set_context(run_id, context)
        enforce_universe_policy(resolved, mapping_issues, coverage, cfg)
        if mapping_issues:
            print("WARNING: ranking uses the explicitly reduced universe. No symbols were auto-renamed; universe.json is unchanged.")
        now = datetime.now(IST)
        # An explicit past date is verified by an exact matching daily benchmark
        # candle. For auto/current-date mode, consult the market timing calendar.
        if exact_date and cutoff < now.date():
            expected = cutoff
            context["calendar_source"] = "explicit_past_session_requires_exact_benchmark"
        else:
            expected = client.expected_session(cutoff, now)
            context["calendar_source"] = "upstox_market_timings"
            if exact_date and expected != cutoff:
                raise DataPending(f"Requested {cutoff} is not a completed NSE session according to market timings")
        context["expected_session"] = expected.isoformat()
        print(f"Expected completed NSE session: {expected} (requested cutoff: {cutoff})")
        benchmark = client.history(cfg.benchmark_key, expected)
        if not benchmark:
            raise DataPending(f"No benchmark daily candles returned for requested session {expected}")
        signal_day = benchmark[-1].day
        as_of = signal_day.isoformat()
        if signal_day > expected:
            raise DataError("Benchmark returned a future session; refusing to publish")
        if signal_day < expected:
            detail = f"Expected {expected}; latest available benchmark candle is {signal_day}"
            if exact_date or not cfg.allow_previous_session:
                raise DataPending(detail + ". No stale-date substitution made. Retry after provider publication, "
                                  "or explicitly use --allow-previous-session for an older-session research scan")
            if (expected - signal_day).days > cfg.max_benchmark_age_days:
                raise DataPending(detail + "; exceeds the configured fallback age limit")
            context["prior_session_used"] = True
            issues.append({"symbol": "BENCHMARK", "reason": "explicit_prior_session_fallback", "detail": detail})
            print("WARNING: PRIOR-SESSION FALLBACK ENABLED. " + detail)
        else:
            context["prior_session_used"] = False
        if len(benchmark) < cfg.required_bars:
            raise DataError(f"Benchmark needs at least {cfg.required_bars} completed daily candles")
        previous = store.latest()
        if previous and as_of < previous["as_of"]:
            raise DataError("Older signal date cannot replace current selections; historical backfill is not supported")
        close = benchmark[-1].close
        sma = statistics.mean(b.close for b in benchmark[-cfg.sma_days:])
        regime_on = close > sma  # actual measurement: NEVER force this to True
        context.update(actual_regime_on=regime_on, benchmark_close=close, benchmark_sma=sma,
                       benchmark_source=getattr(client, "bar_sources", {}).get(cfg.benchmark_key, "historical_v3"))
        print(f'Nifty 50: close {close:.2f}; SMA{cfg.sma_days} {sma:.2f}; actual regime {"ON" if regime_on else "OFF"}')
        if cfg.ignore_market_regime:
            print("WARNING: MARKET FILTER BYPASSED by explicit option; actual regime is unchanged. Research-only override.")
        elif not regime_on:
            print("Market filter is OFF: selected list will be empty. Stock candidates will still be ranked for research.")
        for index, (symbol, key) in enumerate(resolved.items(), 1):
            try:
                bars = client.history(key, signal_day)
                result = assess(symbol, key, bars, benchmark, cfg)
                result["candle_source"] = getattr(client, "bar_sources", {}).get(key, "historical_v3")
                result["market_regime_on"] = regime_on
                result["market_filter_bypassed"] = cfg.ignore_market_regime
                metrics.append(result)
                if not result["eligible"]:
                    issues.append({"symbol": symbol, "reason": ", ".join(result["reasons"])})
            except (ApiError, DataError) as exc:
                issues.append({"symbol": symbol, "reason": "data_error", "detail": str(exc)})
                print(f"DATA ERROR {symbol}: {exc}", file=sys.stderr)
                # Mapping exclusions are an explicit policy; HTTP/data failures
                # are not. Fail early instead of publishing biased partial ranks.
                raise
            if index % 25 == 0 or index == len(resolved):
                print(f"Fetched {index}/{len(resolved)} mapped stocks; universe exclusions: {len(mapping_issues)}; data errors: 0", flush=True)
        ranked = rank_candidates(metrics)
        context.update(eligible_count=len(ranked), processed_count=len(metrics))
        store.set_context(run_id, context)
        store.publish(run_id, now_ist(), as_of, regime_on, close, sma, ranked, cfg.top_n, issues,
                      filter_bypassed=cfg.ignore_market_regime)
        status = "SUCCESS"
        logger.info("Run %s SUCCESS signal=%s actual_regime=%s bypass=%s mapped=%s/%s", run_id, as_of,
                    regime_on, cfg.ignore_market_regime, len(resolved), len(symbols))
        return 0
    except DataPending as exc:
        status, failure_message = "WAITING_DATA", str(exc)
        store.set_context(run_id, context)
        store.finish_failure(run_id, now_ist(), issues, failure_message, as_of, status=status)
        print(f"WAITING FOR DATA: {failure_message}", file=sys.stderr)
        logger.warning("Run %s WAITING_DATA: %s", run_id, failure_message)
        return 3
    except (ApiError, DataError, ValueError, OSError) as exc:
        status, failure_message = "FAILED", str(exc)
        store.set_context(run_id, context)
        store.finish_failure(run_id, now_ist(), issues, failure_message, as_of)
        logger.error("Run %s FAILED: %s", run_id, failure_message)
        print(f"SCAN FAILED: {failure_message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        status, failure_message = "FAILED", "Interrupted by user"
        store.finish_failure(run_id, now_ist(), issues, failure_message, as_of)
        print("Interrupted. Previous selection retained.", file=sys.stderr)
        return 130
    except Exception as exc:
        status, failure_message = "FAILED", f"Unexpected {type(exc).__name__}; previous selection retained"
        store.finish_failure(run_id, now_ist(), issues, failure_message, as_of)
        logger.error("Run %s FAILED: %s", run_id, failure_message)
        print(failure_message, file=sys.stderr)
        return 2
    finally:
        if client:
            client.close()
        # Always leave a diagnostic audit, including early preflight/date failures.
        # Audit export failure must NOT undo a committed SQLite selection.
        try:
            atomic_json(data_dir / "reports" / f"scan_{run_id:06d}.json",
                        {"run_id": run_id, "status": status, "signal_date": as_of,
                         "context": context, "issues": issues, "metrics": metrics,
                         "ranked_candidates": ranked, "error": failure_message})
        except OSError:
            print("WARNING: Could not export per-run audit; SQLite remains authoritative. Close locked files and retry --show.", file=sys.stderr)


def diagnose_api(root, data_dir):
    """Four small read-only probes. Never writes selections or prints profile data."""
    cfg = Config().validate()
    token = read_token(root)
    client = Upstox(token, cfg, data_dir)
    cutoff = completed_cutoff(datetime.now(IST), cfg)
    start = cutoff - timedelta(days=14)
    index = quote(cfg.benchmark_key, safe="")
    # Fixed liquid-equity identifier is only a connectivity probe, not a universe override.
    equity = quote("NSE_EQ|INE002A01018", safe="")
    probes = [
        ("PROFILE", "https://api.upstox.com/v2/user/profile"),
        ("V3_NIFTY50", f"https://api.upstox.com/v3/historical-candle/{index}/days/1/{cutoff}/{start}"),
        ("V3_RELIANCE", f"https://api.upstox.com/v3/historical-candle/{equity}/days/1/{cutoff}/{start}"),
        ("V2_NIFTY50_COMPARISON", f"https://api.upstox.com/v2/historical-candle/{index}/day/{cutoff}/{start}"),
    ]
    print("API DIAGNOSTICS - read-only; no selection/database changes")
    print(f"Script: {Path(__file__).resolve()}")
    print(f"Env file: {(root / 'env.txt').resolve()}")
    print(f"Python: {sys.version.split()[0]} | executable: {sys.executable}")
    print(f"Proxy configured for HTTP(S): {bool(set(getproxies()) & {'http', 'https', 'all'})} (addresses not shown)")
    print("Token loaded locally; credentials and profile payload will NOT be printed or saved.")
    print("V2 is a legacy comparison only; production screening still uses V3.\n")
    rows = []
    try:
        for name, url in probes:
            row = {"probe": name}
            try:
                response = client.get(url)
                row["http_status"] = response.status_code
                try:
                    obj = response.json()
                    if not isinstance(obj, dict) or obj.get("status") != "success":
                        raise ValueError()
                    if name == "PROFILE":
                        if not isinstance(obj.get("data"), dict):
                            raise ValueError()
                        row["ok"] = True
                        row["result"] = "Profile endpoint accepted this token. Personal data omitted."
                    else:
                        candles = obj["data"]["candles"]
                        parsed = parse_candles(candles, cutoff)
                        row["ok"] = bool(parsed)
                        row["candle_count"] = len(parsed)
                        row["latest_candle_date"] = parsed[-1].day.isoformat() if parsed else None
                        row["result"] = (f"Valid completed candles: {len(parsed)}; latest: {row['latest_candle_date']}"
                                         if parsed else "HTTP accepted, but no completed candles returned")
                except (ValueError, KeyError, TypeError, AttributeError):
                    row["ok"] = False
                    row["result"] = "HTTP 200 but unexpected/invalid payload; contents omitted for privacy"
            except ApiError as exc:
                row.update(ok=False, http_status=exc.status_code, result=str(exc), details=exc.details)
            print(f"{name}: HTTP {row['http_status']} | {row['result']}")
            rows.append(row)
    finally:
        client.close()
    by_name = {row["probe"]: row for row in rows}
    profile = by_name["PROFILE"]
    v3 = by_name["V3_NIFTY50"]
    if profile["ok"] and not v3["ok"]:
        conclusion = ("Token was accepted by the profile endpoint. The V3 candle request failed separately; "
                      "inspect its HTTP/API error instead of assuming global token invalidity.")
    elif v3["ok"]:
        conclusion = "V3 benchmark probe passed. Try the full scan; diagnostic success does not validate all 500 stocks."
    else:
        conclusion = ("No successful V3 benchmark probe. Compare the error codes and response types above; "
                      "these results alone may not distinguish token scope, account permission, service or network rejection.")
    print("\n" + conclusion)
    print("An HTML/non-JSON 403 may be an intermediary/gateway rejection, not a structured Upstox token error.")
    result = {"checked_at": now_ist(), "probes": rows, "conclusion": conclusion}
    destination = data_dir / "reports" / "api_diagnostics.json"
    with process_lock(data_dir / "screener.lock"):
        atomic_json(destination, result)
    print(f"Sanitized diagnostics: {destination}")
    print("Review before sharing. Do NOT share env.txt, a token, or raw profile/HTTP response data.")
    return 0 if v3["ok"] else 2


def config_from_args(args):
    # Preserve editable Config defaults; a supplied CLI flag explicitly enables it.
    names = ("ignore_market_regime", "allow_previous_session", "strict_universe", "refresh_instruments")
    overrides = {name: True for name in names if getattr(args, name, False)}
    return replace(Config(), **overrides).validate()


def main(root=None):
    root = Path(root).resolve() if root is not None else Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="VAM-AF NSE swing screener; GET-only API; never places orders.")
    parser.add_argument("--show", action="store_true", help="Regenerate/show saved selections without API access")
    parser.add_argument("--history", action="store_true", help="Display ALL discovery/re-entry events offline (implies --show)")
    parser.add_argument("--open-report", action="store_true", help="Open local HTML dashboard after scan/show")
    parser.add_argument("--as-of", type=date.fromisoformat, metavar="YYYY-MM-DD", help="Exact completed session; cannot rewind current state")
    parser.add_argument("--diagnose", action="store_true", help="Probe API access without scanning or modifying selections; sanitized output only")
    parser.add_argument("--ignore-market-regime", action="store_true", help="Research override; explicitly labeled BYPASSED, never changes actual regime")
    parser.add_argument("--allow-previous-session", action="store_true", help="Explicitly permit a recent older benchmark session if the expected session is unavailable")
    parser.add_argument("--strict-universe", action="store_true", help="Fail before candle downloads if any input symbol cannot be mapped")
    parser.add_argument("--refresh-instruments", action="store_true", help="Refresh the public instrument master instead of using same-day cache")
    parser.add_argument("--check-universe", action="store_true", help="Resolve/list symbols only; no selection/database changes")
    parser.add_argument("--version", action="version", version=f"VAM-AF {VERSION}")
    args = parser.parse_args()
    if args.as_of and args.allow_previous_session:
        parser.error("--as-of requires an exact session; do not combine it with --allow-previous-session")
    scan_flags = args.ignore_market_regime or args.allow_previous_session or args.strict_universe or args.refresh_instruments
    if (args.show or args.history or args.diagnose) and (scan_flags or args.check_universe):
        parser.error("Scan options cannot be combined with --show/--history/--diagnose")
    if args.check_universe and (args.as_of or args.ignore_market_regime or args.allow_previous_session or args.open_report):
        parser.error("--check-universe only supports --strict-universe and --refresh-instruments")
    if args.diagnose and (args.show or args.history or args.as_of or args.open_report):
        parser.error("Use --diagnose by itself")
    if args.as_of and (args.show or args.history):
        parser.error("--as-of cannot be used with offline --show/--history")
    print(f"VAM-AF {VERSION} | script: {Path(__file__).resolve()}")
    data_dir = root / "stock-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    logger = configure_logging(data_dir)
    store = None
    try:
        if args.diagnose:
            return diagnose_api(root, data_dir)
        with process_lock(data_dir / "screener.lock"):
            if args.check_universe:
                cfg = config_from_args(args)
                symbols = load_universe(data_dir / "universe.json")
                client = Upstox(read_token(root), cfg, data_dir)
                try:
                    resolved, issues, ratio = universe_preflight(client, symbols, cfg)
                    atomic_json(data_dir / "reports" / "universe_check.json",
                                {"checked_at": now_ist(), "resolved": resolved, "issues": issues, "coverage": ratio})
                    enforce_universe_policy(resolved, issues, ratio, cfg)
                    return 0
                finally:
                    client.close()
            store = Store(data_dir / "momentum.sqlite3")
            store.recover_interrupted(now_ist())
            code = 0
            if not (args.show or args.history):
                cfg = config_from_args(args)
                print(f"Universe file: {data_dir / 'universe.json'}")
                symbols = load_universe(data_dir / "universe.json")
                cutoff = completed_cutoff(datetime.now(IST), cfg)
                if args.as_of:
                    if args.as_of > cutoff:
                        raise ValueError("--as-of exceeds safe completed-candle cutoff; wait until eod_ready_ist")
                    cutoff = args.as_of
                code = scan(root, data_dir, store, cfg, symbols, cutoff, bool(args.as_of), logger)
            data, dashboard = write_reports(store, data_dir)
            print_report(data, full_history=True)
            print(f"\nDashboard: {dashboard}\nDatabase: {data_dir / 'momentum.sqlite3'}")
            if args.open_report:
                webbrowser.open(dashboard.as_uri())
            return code
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        if store:
            store.close()

if __name__ == "__main__":
    raise SystemExit(main())
