#!/usr/bin/env python3
"""Upstox fair-value console screener -- Python 3.10+, standard library only.

Run:
  python upstox_fair_value_screener.py --demo --details
  python upstox_fair_value_screener.py --self-test
  python upstox_fair_value_screener.py --init
  python upstox_fair_value_screener.py --diagnose-instruments --symbols ITC,DABUR,MARICO
  python upstox_fair_value_screener.py --symbols ITC,DABUR,MARICO --details
  python upstox_fair_value_screener.py --universe nifty500 --watch

Credentials are read from env.txt; never embedded in this source or logged.
Real-data default is a read-only Upstox-only COMPARISON screener. EPS x sector
P/E references are NOT intrinsic values or trading signals. Missing accounting
data are NOT treated as zero; complete health scores are NOT fabricated.
Optional --analysis-mode full retains the independent full valuation engine,
NOT InvestingPro's proprietary model. Full-mode orders are separately gated.
See README.md and --help for the full input contract and operational limitations.

Official API documentation checked 2026-10-15:
  https://upstox.com/developer/api-documentation/fundamentals/
  https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/
  https://upstox.com/developer/api-documentation/get-full-market-quote-v3/
  https://upstox.com/developer/api-documentation/place-gtt-order/
  https://upstox.com/developer/api-documentation/v3/place-order/
"""
from __future__ import annotations

import argparse
import base64
import calendar
import contextlib
import csv
import dataclasses
import datetime as dt
import decimal
import gzip
import hashlib
import io
import json
import math
import os
import random
import re
import secrets
import sqlite3
import statistics as stats
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable, Iterable

VERSION = "1.0.2"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")
CRORE = 10_000_000.0
API = "https://api.upstox.com"
HFT = "https://api-hft.upstox.com"
MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
SUSPENDED_URL = "https://assets.upstox.com/market-quote/instruments/exchange/suspended-instrument.json.gz"
NIFTY500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
BENCHMARK_NAME = "Nifty 50"
DEFAULT_SYMBOLS = "ITC,HINDUNILVR,DABUR,MARICO,JYOTHYLAB,EMAMILTD"

DEFAULT_CONFIG: dict[str, Any] = {
    "assumptions_as_of": None,
    "risk_free_rate": 0.07,
    "equity_risk_premium": 0.06,
    "tax_rate_assumption": 0.25,
    "terminal_growth": 0.045,
    "projection_years": 5,
    "beta_bounds": [0.6, 1.8],
    "wacc_bounds": [0.11, 0.155],
    "growth_bounds": [-0.15, 0.20],
    "dcf_latest_vs_average_tolerance": 0.40,
    "model_weights": {"dcf": 0.40, "peers": 0.35, "historical": 0.25},
    "peer_multiple_weights": {"pe": 0.40, "ev_ebitda": 0.40, "pb": 0.20},
    "minimum_peers": 4,
    "minimum_rank_sample": 5,
    "maximum_peer_pe": 100.0,
    "minimum_historical_observations": 252,
    "minimum_market_cap_crore": 500.0,
    "large_mid_upside_threshold": 0.35,
    "small_upside_threshold": 0.45,
    "overvalued_upside_threshold": -0.15,
    "health_threshold": 3.5,
    "quality_threshold": 3.5,
    "cash_pillar_threshold": 3.0,
    "health_exit_threshold": 2.75,
    "minimum_interest_coverage": 3.0,
    "maximum_net_debt_ebitda": 3.0,
    "minimum_positive_cfo_fraction": 0.60,
    "minimum_daily_turnover_crore": 5.0,
    "minimum_valid_models_for_action": 2,
    "max_model_dispersion": 2.5,
    "max_annual_age_days": 550,
    "max_earnings_age_days": 200,
    "max_quote_age_seconds": 180,
    "max_trade_age_seconds": 180,
    "max_spread_fraction": 0.005,
    "max_stock_allocation": 0.05,
    "max_sector_allocation": 0.20,
    "risk_fraction_per_trade": 0.005,
    "target_fair_value_fraction": 0.90,
    "max_stop_distance_fraction": 0.25,
    "minimum_reward_risk": 1.5,
    "cash_cost_buffer": 0.005,
    "maximum_turnover_participation": 0.005,
    "fundamental_cache_hours": 6.0,
    "slow_refresh_hours": 6.0,
    "api_requests_per_second": 4.0,
    "tick_size_divisor": 100.0,
    "history_price_basis": "unverified",
    "benchmark_price_basis": "split_adjusted",
}


class ScreenerError(Exception):
    pass


class AuthenticationError(ScreenerError):
    pass


class ApiError(ScreenerError):
    def __init__(self, status: int, message: str, code: str = ""):
        super().__init__(f"HTTP {status}: {code + ': ' if code else ''}{message}")
        self.status = status
        self.code = code


class AmbiguousWriteError(ScreenerError):
    """A mutation might have reached the broker. Never automatically retry it."""


class DataError(ScreenerError):
    pass


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            value = value.strip().replace(",", "").replace("₹", "")
            if value in {"", "-", "--", "NA", "N/A", "null", "None"}:
                return None
            if value.startswith("(") and value.endswith(")"):
                value = "-" + value[1:-1]
            value = value.rstrip("%")
        answer = float(value)
        return answer if math.isfinite(answer) else None
    except (TypeError, ValueError):
        return None


def positive(value: Any) -> float | None:
    answer = num(value)
    return answer if answer is not None and answer > 0 else None


def ratio(a: Any, b: Any, positive_denominator: bool = True) -> float | None:
    a, b = num(a), num(b)
    if a is None or b is None or b == 0 or (positive_denominator and b < 0):
        return None
    return num(a / b)


def mean_valid(values: Iterable[Any]) -> float | None:
    values = [v for item in values if (v := num(item)) is not None]
    return stats.fmean(values) if values else None


def quantile(values: Iterable[float], q: float) -> float | None:
    values = sorted(v for x in values if (v := num(x)) is not None)
    if not values:
        return None
    p = (len(values) - 1) * q
    lo, hi = math.floor(p), math.ceil(p)
    return values[lo] + (values[hi] - values[lo]) * (p - lo)


def clip(x: float, bounds: list[float] | None) -> float:
    return min(max(x, bounds[0]), bounds[1]) if bounds else x


def parse_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d-%b-%Y", "%b %Y", "%B %Y"):
        try:
            parsed = dt.datetime.strptime(value[:10] if fmt == "%Y-%m-%d" else value, fmt)
            if fmt in ("%b %Y", "%B %Y"):
                return dt.date(parsed.year, parsed.month, calendar.monthrange(parsed.year, parsed.month)[1])
            return parsed.date()
        except ValueError:
            pass
    return None


def parse_timestamp(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=IST) if value.tzinfo is None else value.astimezone(IST)
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            v = float(value)
            if v > 1e14:
                v /= 1_000_000
            elif v > 1e11:
                v /= 1_000
            return dt.datetime.fromtimestamp(v, IST)
        answer = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return answer.replace(tzinfo=IST) if answer.tzinfo is None else answer.astimezone(IST)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def release_timestamp(value: Any) -> dt.datetime | None:
    answer = parse_timestamp(value)
    if answer and isinstance(value, str) and len(value) == 10:
        # Unknown filing time: first usable AFTER this date, never at midnight.
        return dt.datetime.combine(answer.date(), dt.time(23, 59, 59), IST)
    return answer


def fmt(value: Any, decimals: int = 2) -> str:
    v = num(value)
    return f"{v:,.{decimals}f}" if v is not None else "N/A"


def pct(value: Any) -> str:
    v = num(value)
    return f"{100 * v:+.2f}%" if v is not None else "N/A"


def clean_json(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(clean_json(value), indent=2, allow_nan=False), encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def table(headers: list[str], rows: list[list[Any]]) -> None:
    rendered = [[str(x) for x in row] for row in rows]
    widths = [max([len(h)] + [len(row[i]) for row in rendered]) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rendered:
        print("  ".join(v.ljust(widths[i]) for i, v in enumerate(row)))


def load_env(path: Path) -> dict[str, str]:
    """No shell execution, dotenv dependency or token output. File wins over OS env."""
    values = {k: v for k, v in os.environ.items() if k.startswith("UPSTOX_")}
    if not path.exists():
        return values
    for line_number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise DataError(f"{path.name}:{line_number}: expected NAME=value")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise DataError(f"{path.name}:{line_number}: invalid variable name")
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise DataError(f"{path.name}:{line_number}: unclosed quotation")
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        values[key] = value
    return values


def save_env_token(path: Path, token: str) -> None:
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = [line for line in old.splitlines()
             if not re.match(r"^\s*(?:export\s+)?UPSTOX_ACCESS_TOKEN\s*=", line)]
    lines.append("UPSTOX_ACCESS_TOKEN=" + token)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class Redactor:
    def __init__(self, env: dict[str, str]):
        self.values = sorted([v for k, v in env.items()
                              if any(s in k for s in ("TOKEN", "SECRET", "API_KEY")) and v],
                             key=len, reverse=True)

    def __call__(self, message: Any) -> str:
        text = str(message)
        for secret in self.values:
            if len(secret) >= 4:
                text = text.replace(secret, "<redacted>")
        return re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "<redacted-jwt>", text)


def token_expiry(token: str) -> dt.datetime | None:
    """Unverified JWT payload: convenience expiry hint ONLY, not authentication."""
    try:
        part = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return parse_timestamp(payload.get("exp"))
    except (ValueError, IndexError, TypeError, json.JSONDecodeError):
        return None


class State:
    """Non-secret market caches, debounced alerts, and durable order intentions."""
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, created REAL, payload TEXT);
            CREATE TABLE IF NOT EXISTS alerts (key TEXT PRIMARY KEY, signal TEXT);
            CREATE TABLE IF NOT EXISTS intents (
              id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE, mode TEXT, instrument TEXT,
              symbol TEXT, action TEXT, status TEXT, created TEXT, payload TEXT, response TEXT);
        """)
        self.db.commit()
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        self.cache_hits = 0

    def cached(self, key: str, ttl: float) -> Any | None:
        row = self.db.execute("SELECT created,payload FROM cache WHERE key=?", (key,)).fetchone()
        if row and time.time() - row["created"] <= ttl:
            self.cache_hits += 1
            return json.loads(row["payload"])
        return None

    def put_cache(self, key: str, value: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)",
                        (key, time.time(), json.dumps(clean_json(value), allow_nan=False)))
        self.db.commit()

    def new_alert(self, key: str, signal: str) -> bool:
        row = self.db.execute("SELECT signal FROM alerts WHERE key=?", (key,)).fetchone()
        changed = not row or row[0] != signal
        if changed:
            self.db.execute("INSERT OR REPLACE INTO alerts VALUES (?,?)", (key, signal))
            self.db.commit()
        return changed

    def reserve_intent(self, fingerprint: str, mode: str, instrument: str, symbol: str,
                       action: str, payload: dict[str, Any]) -> int | None:
        try:
            with self.db:
                cur = self.db.execute("""INSERT INTO intents
                    (fingerprint,mode,instrument,symbol,action,status,created,payload,response)
                    VALUES (?,?,?,?,?,'RESERVED',?,?, '{}')""",
                    (fingerprint, mode, instrument, symbol, action, now_ist().isoformat(),
                     json.dumps(clean_json(payload))))
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def set_intent(self, ident: int, status: str, response: Any) -> None:
        self.db.execute("UPDATE intents SET status=?,response=? WHERE id=?",
                        (status, json.dumps(clean_json(response)), ident))
        self.db.commit()

    def intents(self, mode: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM intents" + (" WHERE mode=?" if mode else "") + " ORDER BY id"
        rows = self.db.execute(query, (mode,) if mode else ()).fetchall()
        answer = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            item["response"] = json.loads(item["response"] or "{}")
            answer.append(item)
        return answer

    def close(self) -> None:
        self.db.close()


class RateLimiter:
    """Conservative aggregate cap: 4/sec, 450/min, 1800/30min by default."""
    def __init__(self, per_second: float):
        self.interval = 1 / max(0.1, min(per_second, 9.0))
        self.last = 0.0
        self.times: deque[float] = deque()

    def wait(self) -> None:
        while True:
            now = time.monotonic()
            while self.times and now - self.times[0] >= 1800:
                self.times.popleft()
            delay = max(0, self.last + self.interval - now)
            if len(self.times) >= 1800:
                delay = max(delay, self.times[-1800] + 1800 - now + 0.01)
            recent = [t for t in self.times if now - t < 60]
            if len(recent) >= 450:
                delay = max(delay, recent[-450] + 60 - now + 0.01)
            if delay <= 0:
                self.last = now
                self.times.append(now)
                return
            time.sleep(delay)


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.get_method() != "GET":
            raise AmbiguousWriteError("Broker mutation redirected; outcome requires reconciliation, no automatic follow/resend")
        old_host = urllib.parse.urlparse(req.full_url).hostname
        new_host = urllib.parse.urlparse(newurl).hostname
        if old_host != new_host and req.has_header("Authorization"):
            raise ScreenerError("Refusing authenticated cross-host redirect")
        if urllib.parse.urlparse(newurl).scheme != "https":
            raise ScreenerError("Refusing non-HTTPS redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Http:
    def __init__(self, env: dict[str, str], state: State, config: dict[str, Any],
                 opener: Any = None, sleeper: Callable = time.sleep):
        self.env, self.state, self.config = env, state, config
        self.redact = Redactor(env)
        self.opener = opener or urllib.request.build_opener(SafeRedirect())
        self.limiter = RateLimiter(config["api_requests_per_second"])
        self.sleep = sleeper
        self.requests = 0

    def request(self, method: str, url: str, params: dict[str, Any] | None = None,
                payload: dict[str, Any] | None = None, account: bool = False,
                auth: bool = True, ttl: float = 0, form: bool = False,
                binary: bool = False) -> Any:
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        if urllib.parse.urlparse(url).scheme != "https":
            raise ScreenerError("Only HTTPS requests are supported")
        key = hashlib.sha256(url.encode()).hexdigest()
        if method == "GET" and ttl > 0:
            cached = self.state.cached(key, ttl)
            if cached is not None:
                return cached
        headers = {"Accept": "application/json", "User-Agent": "UpstoxConsoleScreener/" + VERSION}
        if auth:
            access = self.env.get("UPSTOX_ACCESS_TOKEN", "")
            analytics = self.env.get("UPSTOX_ANALYTICS_TOKEN", "")
            # Analytics can serve read-only account APIs from a registered IP;
            # mutations NEVER fall back to analytics credentials.
            token = (access if len(access) >= 40 else analytics) if account and method == "GET" else access if account else (analytics or access)
            if not token or len(token) < 40:
                raise AuthenticationError("A complete valid Upstox token is required in env.txt; the supplied value is missing or truncated")
            headers["Authorization"] = "Bearer " + token
        if method != "GET" and "/order/" in url and self.env.get("UPSTOX_ALGO_NAME"):
            headers["X-Algo-Name"] = self.env["UPSTOX_ALGO_NAME"]
        data = None
        if payload is not None:
            if form:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                data = urllib.parse.urlencode(payload).encode()
            else:
                headers["Content-Type"] = "application/json"
                data = json.dumps(payload, allow_nan=False).encode()
        attempts = 4 if method == "GET" else 1  # Never retry ANY mutation/token exchange.
        for attempt in range(attempts):
            self.limiter.wait()
            self.requests += 1
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self.opener.open(req, timeout=25) as response:
                    raw = response.read()
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                if binary:
                    return raw
                answer = json.loads(raw.decode("utf-8-sig"))
                if isinstance(answer, dict) and answer.get("status") == "error":
                    error = (answer.get("errors") or [{}])[0]
                    raise ApiError(200, self.redact(error.get("message", "API returned error")),
                                   str(error.get("errorCode", "")))
                if method == "GET" and ttl > 0:
                    self.state.put_cache(key, answer)
                return answer
            except urllib.error.HTTPError as exc:
                try:
                    body = json.loads(exc.read().decode("utf-8"))
                    error = (body.get("errors") or [{}])[0]
                    message = self.redact(error.get("message", "Request rejected"))
                    error_code = str(error.get("errorCode", ""))
                except (ValueError, UnicodeError, AttributeError):
                    message, error_code = "Request rejected; non-JSON response", ""
                if exc.code == 401:
                    raise AuthenticationError("Upstox rejected the token (401). Renew it using --login or the developer dashboard") from None
                if method != "GET" and exc.code >= 500:
                    raise AmbiguousWriteError(f"Broker write returned {exc.code}; reconcile before retrying") from None
                if method == "GET" and exc.code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                    retry_after = num(exc.headers.get("Retry-After")) if exc.headers else None
                    self.sleep(min(60, retry_after if retry_after is not None else 0.75 * 2 ** attempt + random.random() / 4))
                    continue
                raise ApiError(exc.code, message, error_code) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                if method != "GET":
                    raise AmbiguousWriteError("Network failure during broker write; outcome UNKNOWN, no automatic resend") from None
                if attempt + 1 < attempts:
                    self.sleep(0.75 * 2 ** attempt)
                    continue
                raise ScreenerError("Network request failed after bounded retries: " + self.redact(type(exc).__name__)) from None
            except (json.JSONDecodeError, UnicodeDecodeError, EOFError, gzip.BadGzipFile) as exc:
                if method != "GET":
                    raise AmbiguousWriteError("Unreadable broker write response; reconcile before retrying") from None
                raise DataError("Invalid JSON or compressed response from provider") from exc
        raise ScreenerError("Unexpected request termination")


class Upstox:
    is_demo = False

    def __init__(self, http: Http):
        self.http = http
        self.state = http.state
        self.config = http.config
        self.fetch_competitors = True
        self.instrument_keys_by_isin: dict[str, str] = {}

    def validate_token(self) -> None:
        token = self.http.env.get("UPSTOX_ANALYTICS_TOKEN") or self.http.env.get("UPSTOX_ACCESS_TOKEN", "")
        if len(token) < 40:
            raise AuthenticationError("env.txt contains a missing/truncated access token. Replace it locally with the complete token; do not send it in chat")
        expiry = token_expiry(token)
        if expiry and expiry <= now_ist():
            raise AuthenticationError("Token expiry hint is in the past. Refresh the token locally using --login")
        # Use a protected quote endpoint, not possibly public historical/status
        # endpoints. This known index key is also checked against the BOD master
        # in Phase 1 before any valuation work. No account identity is logged.
        self.http.request("GET", API + "/v2/market-quote/ltp", params={"instrument_key": "NSE_INDEX|Nifty 50"})

    def instruments(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        # Fresh at every scheduled slow-layer refresh: stale BOD ISIN/tick data
        # must not cross a trading day merely because a generic TTL remains.
        master = self.http.request("GET", MASTER_URL, auth=False)
        suspended = self.http.request("GET", SUSPENDED_URL, auth=False)
        self.instrument_keys_by_isin = {r["isin"]: r["instrument_key"] for r in master
                                       if isinstance(r, dict) and r.get("segment") == "NSE_EQ"
                                       and r.get("instrument_type") == "EQ" and r.get("isin") and r.get("instrument_key")}
        return master, suspended

    def nifty500(self) -> list[dict[str, str]]:
        raw = self.http.request("GET", NIFTY500_URL, auth=False, binary=True)
        return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))

    def quotes(self, keys: list[str], ltp_only: bool = False) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        # 200, rather than 500, also avoids URL-length problems at HTTP proxies.
        for offset in range(0, len(keys), 200):
            result = self.http.request("GET", API + ("/v2/market-quote/ltp" if ltp_only else "/v3/market-quote/quotes"),
                                       params={"instrument_key": ",".join(keys[offset:offset + 200])})
            for quote in (result.get("data") or {}).values():
                key = quote.get("instrument_token") or quote.get("instrument_key")
                if key in keys:
                    out[key] = quote
        return out

    def candles(self, key: str, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(key, safe="")
        result = self.http.request("GET", f"{API}/v3/historical-candle/{encoded}/days/1/{end}/{start}", ttl=6 * 3600)
        return decode_candles(result.get("data", {}).get("candles", []), start, end)

    def fundamental_bundle(self, isin: str, statement_type: str) -> dict[str, Any]:
        endpoints = {
            "profile": ("profile", {}),
            "balance": ("balance-sheet", {"type": statement_type, "fs": "true"}),
            "cash": ("cash-flow", {"type": statement_type, "fs": "true"}),
            "annual_income": ("income-statement", {"type": statement_type, "time_period": "yearly", "fs": "true"}),
            "quarter_income": ("income-statement", {"type": statement_type, "time_period": "quarterly", "fs": "false"}),
            "ratios": ("key-ratios", {}),
            "actions": ("corporate-actions", {}),
            "competitors": ("competitors", {}),
        }
        result: dict[str, Any] = {"errors": [], "fetched_at": now_ist().isoformat()}
        if not self.fetch_competitors:
            endpoints.pop("competitors")
            result["competitors"] = []
            result["competitors_status"] = "NOT_REQUESTED_IN_COMPARISON_MODE"
        ttl = self.config["fundamental_cache_hours"] * 3600
        for name, (endpoint, params) in endpoints.items():
            try:
                data = self.http.request("GET", f"{API}/v2/fundamentals/{urllib.parse.quote(isin, safe='')}/{endpoint}",
                                         params=params, ttl=ttl).get("data")
                result[name] = data
            except AuthenticationError:
                raise
            except ScreenerError as exc:
                # A known backend mismatch reports an invalid instrument key
                # for the documented ISIN competitors route. Try the ACTUAL
                # BOD instrument_key once for this read only, never for writes.
                key = self.instrument_keys_by_isin.get(isin)
                if name == "competitors" and isinstance(exc, ApiError) and exc.status == 400 and exc.code == "UDAPI100011" and key:
                    try:
                        fallback = self.http.request("GET", f"{API}/v2/fundamentals/{urllib.parse.quote(key, safe='')}/competitors", ttl=ttl)
                        result[name] = fallback.get("data")
                        result["competitors_status"] = "INSTRUMENT_KEY_READ_FALLBACK"
                        continue
                    except AuthenticationError:
                        raise
                    except ScreenerError as fallback_error:
                        result["errors"].append(f"competitors instrument_key fallback: {fallback_error}")
                result[name] = None
                result["errors"].append(f"{endpoint}: {exc}")
        return result

    def sector(self, isin: str) -> str | None:
        data = self.http.request("GET", f"{API}/v2/fundamentals/{isin}/profile", ttl=6 * 3600).get("data") or {}
        return data.get("sector")

    def market(self) -> dict[str, Any]:
        data = self.http.request("GET", API + "/v2/market/status/NSE").get("data") or {}
        try:
            timing = self.http.request("GET", API + "/v2/market/timings/" + str(now_ist().date()), ttl=3600).get("data") or []
            data["timings"] = [item for item in timing if item.get("exchange") == "NSE"]
        except AuthenticationError:
            raise
        except ScreenerError as exc:
            data["timing_error"] = str(exc)
        return data

    def account(self) -> dict[str, Any]:
        result: dict[str, Any] = {"errors": [], "complete": True}
        endpoints = {
            "holdings": "/v2/portfolio/long-term-holdings",
            "positions": "/v2/portfolio/short-term-positions",
            "funds": "/v2/user/get-funds-and-margin",
            "orders": "/v2/order/retrieve-all",
            "gtt": "/v3/order/gtt",
        }
        for label, endpoint in endpoints.items():
            try:
                response = self.http.request("GET", API + endpoint, account=True)
                result[label] = response if isinstance(response, list) else response.get("data")
                if result[label] is None:
                    raise DataError(f"{label}: missing data in broker response")
            except ScreenerError as exc:
                result[label] = None
                result["complete"] = False
                result["errors"].append(f"{label}: {exc}")
        return result

    def place_gtt(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.http.request("POST", API + "/v3/order/gtt/place", payload=payload, account=True)

    def place_limit(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.http.request("POST", HFT + "/v3/order/place", payload=payload, account=True)

    def get_gtt(self, ident: str) -> list[dict[str, Any]]:
        return self.http.request("GET", API + "/v3/order/gtt", params={"gtt_order_id": ident}, account=True).get("data") or []

    def cancel_gtt(self, ident: str) -> dict[str, Any]:
        return self.http.request("DELETE", API + "/v3/order/gtt/cancel", payload={"gtt_order_id": ident}, account=True)

    def order_details(self, ident: str) -> dict[str, Any]:
        return self.http.request("GET", API + "/v2/order/details", params={"order_id": ident}, account=True).get("data") or {}


def decode_candles(rows: list, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
    out = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        date = parse_date(row[0])
        values = [num(item) for item in row[1:6]]
        if not date or not (start <= date <= end) or any(v is None for v in values):
            continue
        op, hi, lo, cl, volume = values
        if min(op, hi, lo, cl) <= 0 or volume < 0 or hi < max(op, lo, cl) or lo > min(op, hi, cl):
            continue
        out[date] = {"date": date.isoformat(), "open": op, "high": hi, "low": lo,
                     "close": cl, "volume": volume, "raw_close": cl}
    return [out[d] for d in sorted(out)]


def norm_label(label: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(label).lower())


# Ambiguous labels deliberately omitted: Operating Profit != necessarily EBIT/EBITDA;
# Total Liabilities != debt; Investing Cash Flow != CapEx; Equity Capital != share count.
LINE_ALIASES = {
    "revenue": ["Revenue From Operations", "Net Sales", "Sales", "Revenue"],
    "ebitda": ["EBITDA", "Earnings Before Interest Taxes Depreciation And Amortisation"],
    "ebit": ["EBIT", "Earnings Before Interest And Tax", "Operating Profit EBIT"],
    "depreciation": ["Depreciation And Amortisation", "Depreciation And Amortization", "Depreciation"],
    "net_profit": ["Profit After Tax", "Net Profit"],
    "parent_net_profit": ["Profit Attributable To Owners Of Parent", "Net Profit Attributable To Shareholders"],
    "pbt": ["Profit Before Tax"],
    "tax": ["Tax Expense", "Tax"],
    "eps_diluted": ["EPS - Diluted", "Diluted Earnings Per Share", "Diluted EPS"],
    "eps_basic": ["EPS - Basic", "Basic EPS"],
    "interest_expense": ["Finance Costs", "Interest Expense", "Interest"],
    "cash_interest": ["Interest Paid"],
    "cfo": ["Cash Flow From Operations", "Net Cash From Operating Activities"],
    "capex": ["Capital Expenditure", "Purchase Of Property Plant Equipment And Intangible Assets"],
    "ppe_capex": ["Purchase Of Property Plant And Equipment", "Purchase Of Fixed Assets"],
    "intangible_capex": ["Purchase Of Intangible Assets"],
    "debt": ["Total Debt", "Total Borrowings"],
    "long_debt": ["Long Term Borrowings", "Non Current Borrowings"],
    "short_debt": ["Short Term Borrowings", "Current Borrowings"],
    "lease_liabilities": ["Lease Liabilities", "Total Lease Liabilities"],
    "cash": ["Cash And Cash Equivalents", "Cash & Cash Equivalents"],
    "equity": ["Equity Attributable To Owners Of Parent", "Shareholders Equity", "Total Equity"],
    "total_assets": ["Total Assets"],
    "current_assets": ["Current Assets"],
    "current_liabilities": ["Current Liabilities"],
    "minority_interest": ["Non Controlling Interests", "Minority Interest"],
    "preferred_equity": ["Preference Share Capital", "Preferred Equity"],
    "share_capital": ["Equity Share Capital", "Paid Up Equity Share Capital"],
    "face_value": ["Face Value Per Share", "Face Value"],
    "non_operating_assets": ["Non Operating Investments"],
    # Display-only aggregates: never silently become ordinary equity/debt/cash
    # available to a DCF bridge.
    "cash_reported": ["Cash (End of the year)", "Cash At End Of Year", "Closing Cash Balance"],
    "equity_total_reported": ["Equity Capital"],
    "total_liabilities_reported": ["Total Liabilities"],
    "other_income": ["Other Income"],
    "operating_profit_api": ["Operating Profit"],
}
PER_SHARE_FIELDS = {"eps_diluted", "eps_basic", "face_value"}
MONETARY_FIELDS = set(LINE_ALIASES) - PER_SHARE_FIELDS


def unit_factor(data: dict[str, Any]) -> float:
    units = str(data.get("units_in", "")).lower()
    factors = {"crore": CRORE, "crores": CRORE, "inr": 1, "rupees": 1, "million": 1e6, "lakh": 1e5}
    if units not in factors:
        raise DataError(f"Unrecognised monetary units '{units or 'missing'}'; refusing to guess")
    return factors[units]


def parse_full_statement(data: dict[str, Any], records: dict[str, dict[str, Any]]) -> None:
    multiplier = unit_factor(data)
    lines = {norm_label(item.get("particular")): item.get("history", [])
             for item in (data.get("full_statement") or []) if isinstance(item, dict)}
    for field, aliases in LINE_ALIASES.items():
        history = next((lines[norm_label(alias)] for alias in aliases if norm_label(alias) in lines), [])
        for row in history:
            period = parse_date(row.get("period"))
            v = num(row.get("value"))
            if not period or v is None:
                continue
            if field not in PER_SHARE_FIELDS:
                v *= multiplier
            if field in {"capex", "ppe_capex", "intangible_capex", "cash_interest"}:
                v = abs(v)  # API statement cash outflows use negative signs.
            item = records.setdefault(str(period), {"period": str(period), "available_on": None})
            item[field] = v
    for item in records.values():
        if "capex" not in item and num(item.get("ppe_capex")) is not None and num(item.get("intangible_capex")) is not None:
            item["capex"] = item["ppe_capex"] + item["intangible_capex"]
        if "debt" not in item and num(item.get("long_debt")) is not None and num(item.get("short_debt")) is not None:
            item["debt"] = item["long_debt"] + item["short_debt"]
        if "ebit" not in item and num(item.get("ebitda")) is not None and num(item.get("depreciation")) is not None:
            item["ebit"] = item["ebitda"] - item["depreciation"]
        if "ebitda" not in item and num(item.get("ebit")) is not None and num(item.get("depreciation")) is not None:
            item["ebitda"] = item["ebit"] + item["depreciation"]


def parse_categories(data: dict[str, Any], key: str, mapping: dict[str, str],
                     records: dict[str, dict[str, Any]]) -> None:
    multiplier = unit_factor(data)
    normalized_mapping = {norm_label(name): field for name, field in mapping.items()}
    for item in data.get(key, []) or []:
        field = normalized_mapping.get(norm_label(item.get("category")))
        if not field:
            continue
        for row in item.get("history", []) or []:
            period, value = parse_date(row.get("period")), num(row.get("value"))
            if period and value is not None:
                record = records.setdefault(str(period), {"period": str(period), "available_on": None})
                # Detailed statement labels, when present, take precedence over summaries.
                record.setdefault(field, value if field in PER_SHARE_FIELDS else value * multiplier)


def parse_actions(events: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]], list[str]]:
    actions, warnings = [], []
    for event in events or []:
        name = str(event.get("name", "")).lower()
        if not any(word in name for word in ("bonus", "split", "rights", "demerger", "merger", "consolidation")):
            continue
        date = parse_date(event.get("expiry_date"))
        details = {norm_label(item.get("name")): item.get("value") for item in event.get("event_details", [])}
        factor = None
        if "bonus" in name:
            match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*", str(event.get("ratio", "")))
            if match and float(match[2]) > 0:
                factor = 1 + float(match[1]) / float(match[2])
        elif "split" in name or "consolidation" in name:
            old_face = positive(details.get("oldfacevalue"))
            new_face = positive(details.get("newfacevalue"))
            if old_face and new_face:
                factor = old_face / new_face
            # A bare split ratio has ambiguous direction: do not infer it.
        if not date or factor is None or factor <= 0:
            warnings.append(f"Unresolved corporate action: {event.get('name')} {event.get('expiry_date')}; supply verified factor/basis in supplement")
            actions.append({"date": str(date) if date else None, "kind": name, "factor": None})
        else:
            actions.append({"date": str(date), "kind": name, "factor": factor})
    return actions, warnings


def normalize_bundle(instrument: dict[str, Any], bundle: dict[str, Any], config: dict[str, Any],
                     statement_type: str = "consolidated") -> dict[str, Any]:
    annual: dict[str, dict[str, Any]] = {}
    quarterly: dict[str, dict[str, Any]] = {}
    errors = list(bundle.get("errors", []))
    bundle = dict(bundle)
    for name in ("balance", "cash", "annual_income", "quarter_income"):
        data = bundle.get(name)
        if isinstance(data, dict) and data.get("type") and data["type"] != statement_type:
            errors.append(f"{name}: API returned {data['type']} but requested {statement_type}; statement excluded to avoid mixing bases")
            bundle[name] = None
    for name in ("balance", "cash", "annual_income"):
        data = bundle.get(name)
        if not isinstance(data, dict):
            continue
        try:
            parse_full_statement(data, annual)
            if name == "cash":
                parse_categories(data, "cash_flow", {"operating": "cfo"}, annual)
            elif name == "annual_income":
                parse_categories(data, "income_statement", {"revenue": "revenue", "net_profit": "net_profit",
                                                           "operating_profit": "operating_profit_api"}, annual)
            else:
                factor = unit_factor(data)
                for row in data.get("history", []):
                    date = parse_date(row.get("period"))
                    if date:
                        record = annual.setdefault(str(date), {"period": str(date), "available_on": None})
                        if num(row.get("total_asset")) is not None:
                            record.setdefault("total_assets", num(row["total_asset"]) * factor)
                        if num(row.get("total_liability")) is not None:
                            record.setdefault("total_liabilities_reported", num(row["total_liability"]) * factor)
                        # Never convert total_liability to financial debt.
        except DataError as exc:
            errors.append(f"{name}: {exc}")
    data = bundle.get("quarter_income")
    if isinstance(data, dict):
        try:
            parse_categories(data, "income_statement", {"revenue": "revenue", "net_profit": "net_profit",
                                                       "operating_profit": "operating_profit_api"}, quarterly)
            # full_statement is documented as annual, even on quarterly responses.
            # Therefore it MUST NOT be relabelled as quarterly TTM components.
        except DataError as exc:
            errors.append(f"quarter_income: {exc}")
    profile = bundle.get("profile") or {}
    sector = profile.get("sector") or "UNKNOWN"
    actions, action_warnings = parse_actions(bundle.get("actions"))
    result = {
        "isin": instrument["isin"], "symbol": instrument["trading_symbol"],
        "instrument_key": instrument["instrument_key"], "name": instrument.get("name", ""),
        "sector": sector, "provider_sector": sector, "industry": sector, "currency": "INR", "unit": "rupees",
        "accounting_basis": statement_type, "source": "Upstox Fundamentals API",
        "source_as_of": bundle.get("fetched_at"), "shares_outstanding": None,
        "diluted_shares": None, "shares_as_of": None,
        "history_price_basis": config["history_price_basis"], "eps_basis": "unverified",
        "actions_coverage_verified": False, "point_in_time_verified": False,
        "manual_review_complete": False, "last_reviewed_financial_period": None,
        "manual_review_as_of": None, "tick_verified": False,
        "debt_includes_leases": None, "cfo_interest_classification": "unknown",
        "annual": sorted(annual.values(), key=lambda item: item["period"]),
        "quarterly": sorted(quarterly.values(), key=lambda item: item["period"]),
        "actions": actions, "historical_snapshots": [],
        "competitor_keys": extract_competitor_keys(bundle.get("competitors")),
        "provider_ratios": bundle.get("ratios") or [],
        "warnings": errors + action_warnings,
    }
    return result


def read_supplements(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if data.get("currency") != "INR" or data.get("unit") != "rupees":
        raise DataError("Supplement must declare currency=INR and unit=rupees. Convert crore inputs to rupees first")
    records = data.get("companies", [])
    if not isinstance(records, list):
        raise DataError("Supplement 'companies' must be a list of ISIN-matched records")
    result = {}
    for record in records:
        isin = record.get("isin")
        if not isin or isin in result:
            raise DataError("Every supplement company needs a unique ISIN")
        if record.get("unit", "rupees") != "rupees" or record.get("currency", "INR") != "INR":
            raise DataError(f"{isin}: inconsistent units or currency")
        flag_names = ("actions_coverage_verified", "point_in_time_verified", "manual_review_complete", "tick_verified", "debt_includes_leases")
        for flag in flag_names:
            if record.get(flag) is not None and not isinstance(record[flag], bool):
                raise DataError(f"{isin}: {flag} must be a JSON boolean, not text/number")
        for key in ("shares_outstanding", "diluted_shares", "tick_size_rupees", "beta_override", "growth_override", "cost_of_debt_override", "tax_rate_override", "terminal_growth_override"):
            if record.get(key) is not None:
                value = num(record[key])
                if value is None:
                    raise DataError(f"{isin}: {key} must be a finite number or null")
                record[key] = value
        for field in ("annual", "quarterly"):
            seen_periods = set()
            for row in record.get(field, []):
                period = parse_date(row.get("period"))
                if period in seen_periods:
                    raise DataError(f"{isin}: duplicate {field} period; do not merge differently restated versions")
                seen_periods.add(period)
                for key in set(MONETARY_FIELDS) | PER_SHARE_FIELDS | {"tax_rate", "change_operating_nwc"}:
                    if row.get(key) is not None:
                        value = num(row[key])
                        if value is None:
                            raise DataError(f"{isin}: {field}.{key} must be a finite number or null")
                        row[key] = value
                if not parse_date(row.get("period")):
                    raise DataError(f"{isin}: invalid period in {field}")
                if row.get("available_on"):
                    publication = release_timestamp(row["available_on"])
                    if publication is None or (period and publication.date() < period):
                        raise DataError(f"{isin}: invalid/pre-period available_on timestamp")
                if num(row.get("capex")) is not None and num(row["capex"]) < 0:
                    raise DataError(f"{isin}: normalized CapEx must be a POSITIVE cash outflow")
                if num(row.get("debt")) is not None and num(row["debt"]) < 0:
                    raise DataError(f"{isin}: debt cannot be negative")
        result[isin] = record
    return result


def merge_supplement(company: dict[str, Any], supplement: dict[str, Any] | None,
                     statement_type: str) -> dict[str, Any]:
    company["accounting_basis"] = statement_type
    if not supplement:
        return company
    if supplement.get("accounting_basis", statement_type) != statement_type:
        raise DataError(f"{company['symbol']}: refusing to mix standalone and consolidated statements")
    # Null template cells never erase verified provider values. Corporate
    # actions are merged, not erased by an old/empty supplement after a NEW
    # split or bonus is reported by the broker.
    combined_actions = {(a.get("date"), canonical_action_kind(a.get("kind", ""))): dict(a)
                        for a in company.get("actions", [])}
    for action in supplement.get("actions", []):
        key = (action.get("date"), canonical_action_kind(action.get("kind", "")))
        combined_actions[key] = dict(action)
    company["actions"] = list(combined_actions.values())
    for key, value in supplement.items():
        if key not in {"annual", "quarterly", "warnings", "isin", "symbol", "instrument_key", "source", "actions", "provider_sector"} and value is not None:
            company[key] = value
    for field in ("annual", "quarterly"):
        rows = {row["period"]: dict(row) for row in company.get(field, [])}
        for row in supplement.get(field, []):
            period = str(parse_date(row["period"]))
            target = rows.setdefault(period, {"period": period, "available_on": None})
            for key, value in row.items():
                if key != "period" and value is not None:
                    target[key] = value
        company[field] = [rows[key] for key in sorted(rows)]
    company["source"] += " + " + str(supplement.get("source", "user-supplied supplement"))
    company["warnings"] += supplement.get("warnings", [])
    if supplement.get("actions_coverage_verified") and all(positive(a.get("factor")) and parse_date(a.get("date")) for a in company["actions"]):
        company["warnings"] = [warning for warning in company["warnings"] if not warning.startswith("Unresolved corporate action:")]
    return company


def available_rows(company: dict[str, Any], field: str, clock: dt.datetime) -> list[dict[str, Any]]:
    result = []
    for row in company.get(field, []):
        period = parse_date(row.get("period"))
        publication = release_timestamp(row.get("available_on"))
        if period and period <= clock.date() and (publication is None or publication <= clock):
            result.append(row)
    return sorted(result, key=lambda row: row["period"])


def growth_rate(rows: list[dict[str, Any]], field: str, years: int) -> float | None:
    valid = [row for row in rows if positive(row.get(field))]
    if len(valid) < 2:
        return None
    latest = valid[-1]
    end = parse_date(latest["period"])
    candidates = [row for row in valid[:-1]
                  if abs((end - parse_date(row["period"])).days / 365.25 - years) <= 0.35]
    if not candidates:
        return None
    start = min(candidates, key=lambda row: abs((end - parse_date(row["period"])).days / 365.25 - years))
    elapsed = (end - parse_date(start["period"])).days / 365.25
    return num((latest[field] / start[field]) ** (1 / elapsed) - 1)


def factors_between(company: dict[str, Any], start: dt.date, end: dt.date) -> float | None:
    factor = 1.0
    for action in company.get("actions", []):
        date = parse_date(action.get("date"))
        if date is None:
            return None
        if start < date <= end:
            f = positive(action.get("factor"))
            if f is None:
                return None
            factor *= f
    return factor


def adjusted_candles(company: dict[str, Any], candles: list[dict[str, Any]],
                     clock: dt.datetime) -> tuple[list[dict[str, Any]], str | None]:
    basis = company.get("history_price_basis", "unverified")
    if basis not in {"raw", "split_adjusted"}:
        return [], "Historical price adjustment basis is unverified; set it only after checking splits/bonuses"
    if not company.get("actions_coverage_verified"):
        return [], "Corporate-action coverage has not been verified for the historical window"
    out = []
    for candle in candles:
        row = dict(candle)
        factor = factors_between(company, parse_date(row["date"]), clock.date()) if basis == "raw" else 1.0
        if factor is None:
            return [], "Unresolved split/rights/demerger; historical returns and beta are unavailable"
        for field in ("open", "high", "low", "close"):
            row[field] /= factor
        row["volume"] *= factor
        out.append(row)
    return out, None


def completed_candles(candles: list[dict[str, Any]], clock: dt.datetime) -> list[dict[str, Any]]:
    # Conservative cutoff; today's candle is excluded during continuous trading.
    return [row for row in candles if parse_date(row["date"]) < clock.date()
            or (parse_date(row["date"]) == clock.date() and clock.time() >= dt.time(16, 0))]


def weekly_candles(candles: list[dict[str, Any]], clock: dt.datetime,
                   complete_only: bool = True) -> list[dict[str, Any]]:
    buckets: dict[dt.date, list[dict[str, Any]]] = defaultdict(list)
    for candle in candles:
        date = parse_date(candle["date"])
        monday = date - dt.timedelta(days=date.weekday())
        buckets[monday].append(candle)
    out = []
    for monday, rows in sorted(buckets.items()):
        friday = monday + dt.timedelta(days=4)
        if complete_only and (clock.date() < friday or (clock.date() == friday and clock.time() < dt.time(16, 0))):
            continue
        rows.sort(key=lambda row: row["date"])
        out.append({"date": str(friday), "open": rows[0]["open"], "close": rows[-1]["close"],
                    "high": max(row["high"] for row in rows), "low": min(row["low"] for row in rows),
                    "volume": sum(row["volume"] for row in rows)})
    return out


def stock_beta(stock: list[dict[str, Any]], benchmark: list[dict[str, Any]],
               clock: dt.datetime) -> tuple[float | None, int]:
    floor = clock.date() - dt.timedelta(days=2 * 366)
    left = {row["date"]: row["close"] for row in weekly_candles(stock, clock) if parse_date(row["date"]) >= floor}
    right = {row["date"]: row["close"] for row in weekly_candles(benchmark, clock) if parse_date(row["date"]) >= floor}
    dates = sorted(set(left) & set(right))
    stock_returns, index_returns = [], []
    for old, new in zip(dates, dates[1:]):
        # Do not compare a two-week stock return with a one-week index return.
        if (parse_date(new) - parse_date(old)).days != 7:
            continue
        stock_returns.append(left[new] / left[old] - 1)
        index_returns.append(right[new] / right[old] - 1)
    count = len(stock_returns)
    if count < 52:
        return None, count
    mx, my = stats.fmean(stock_returns), stats.fmean(index_returns)
    denominator = sum((r - my) ** 2 for r in index_returns)
    if denominator <= 1e-12:
        return None, count
    return sum((x - mx) * (y - my) for x, y in zip(stock_returns, index_returns)) / denominator, count


def price_return(candles: list[dict[str, Any]], days: int) -> float | None:
    if not candles:
        return None
    end = parse_date(candles[-1]["date"])
    target = end - dt.timedelta(days=days)
    available = [row for row in candles if parse_date(row["date"]) <= target]
    if not available or (target - parse_date(available[-1]["date"])).days > 10:
        return None
    return candles[-1]["close"] / available[-1]["close"] - 1

# -------------------------- Phase 2: valuations --------------------------


def financial_context(company: dict[str, Any], quote: dict[str, Any], clock: dt.datetime,
                      config: dict[str, Any]) -> dict[str, Any]:
    annual = available_rows(company, "annual", clock)
    quarters = available_rows(company, "quarterly", clock)
    latest = annual[-1] if annual else {}
    # Balance/cash history can lag the income statement; use the latest known
    # value for EACH stock item and preserve its date, never assume zero.
    balance_fields = ("debt", "cash", "equity", "minority_interest", "preferred_equity",
                      "non_operating_assets", "lease_liabilities", "current_assets",
                      "current_liabilities", "total_assets")
    balance, balance_dates = {}, {}
    for field in balance_fields:
        matches = [row for row in annual + quarters if num(row.get(field)) is not None]
        if matches:
            row = max(matches, key=lambda item: item["period"])
            balance[field], balance_dates[field] = num(row[field]), row["period"]
        else:
            balance[field], balance_dates[field] = None, None
    shares = positive(company.get("shares_outstanding"))
    diluted = positive(company.get("diluted_shares"))
    share_basis_date = parse_date(company.get("shares_as_of"))
    diluted_basis_date = parse_date(company.get("diluted_shares_as_of")) or share_basis_date
    share_adjustment_note = "explicit current-share snapshot"
    if share_basis_date and shares is not None:
        adjustment = factors_between(company, share_basis_date, clock.date())
        if adjustment is None:
            shares = None
            share_adjustment_note = "unresolved corporate action after share-count snapshot"
        else:
            shares *= adjustment
            if adjustment != 1:
                share_adjustment_note = f"share snapshot adjusted by verified split/bonus factor {adjustment:g}"
    if diluted_basis_date and diluted is not None:
        adjustment = factors_between(company, diluted_basis_date, clock.date())
        diluted = diluted * adjustment if adjustment is not None else None
    # Paid-up face-value share capital is a usable current-share proxy ONLY with
    # an explicit face value; weighted diluted shares are not silently inferred.
    if shares is None:
        candidates = [row for row in annual if positive(row.get("share_capital")) and positive(row.get("face_value"))]
        if candidates:
            capital_record = candidates[-1]
            adjustment = factors_between(company, parse_date(capital_record["period"]), clock.date())
            shares = capital_record["share_capital"] / capital_record["face_value"] * adjustment if adjustment else None
            share_adjustment_note = "paid-up face-value capital proxy, corporate-action adjusted; verify independently"
    current = {}
    earnings_basis = {}
    earnings_dates = {}
    for field in ("revenue", "ebitda", "ebit", "parent_net_profit", "net_profit", "eps_diluted", "interest_expense"):
        four = quarters[-4:]
        contiguous = len(four) == 4 and all(65 <= (parse_date(b["period"]) - parse_date(a["period"])).days <= 115
                                         for a, b in zip(four, four[1:]))
        if contiguous and all(num(row.get(field)) is not None for row in four):
            # EPS must be on a consistent current-share basis across quarters.
            if field == "eps_diluted" and company.get("eps_basis") == "as_reported":
                adjusted = []
                for row in four:
                    factor = factors_between(company, parse_date(row.get("share_basis_date", row["period"])), clock.date())
                    if factor is None:
                        break
                    adjusted.append(row[field] / factor)
                if len(adjusted) == 4:
                    current[field] = sum(adjusted)
                    earnings_basis[field] = "TTM, split-normalized"
            else:
                current[field] = sum(row[field] for row in four)
                earnings_basis[field] = "TTM"
        if field in current:
            earnings_dates[field] = four[-1]["period"]
        if field not in current:
            matches = [row for row in annual if num(row.get(field)) is not None]
            if matches:
                record = matches[-1]
                value = record[field]
                if field == "eps_diluted" and company.get("eps_basis") == "as_reported":
                    factor = factors_between(company, parse_date(record.get("share_basis_date", record["period"])), clock.date())
                    value = value / factor if factor else None
                current[field] = value
                earnings_basis[field] = "FY " + record["period"]
                earnings_dates[field] = record["period"]
            else:
                current[field] = None
                earnings_basis[field] = "missing"
                earnings_dates[field] = None
    # EPS is per-share already; do not derive it from LTP/provider P/E, which
    # would make fair-value estimates circular. Basic EPS is not substituted.
    price = positive(quote.get("last_price"))
    market_cap = price * shares if price and shares else None
    bridge_keys = ("debt", "cash", "minority_interest", "preferred_equity", "non_operating_assets")
    bridge_known = all(num(balance.get(field)) is not None for field in bridge_keys)
    ev = (market_cap + balance["debt"] + balance["minority_interest"] + balance["preferred_equity"]
          - balance["cash"] - balance["non_operating_assets"]) if market_cap is not None and bridge_known else None
    bvps = ratio(balance["equity"], shares)
    market_cap_crore = market_cap / CRORE if market_cap is not None else None
    tier = ("small" if market_cap_crore < 5000 else "mid" if market_cap_crore < 25000 else "large") if market_cap_crore else "unknown"
    return {"annual": annual, "quarterly": quarters, "latest": latest, "current": current,
            "earnings_basis": earnings_basis, "earnings_dates": earnings_dates,
            "balance": balance, "balance_dates": balance_dates,
            "shares": shares, "diluted_shares": diluted, "share_adjustment_note": share_adjustment_note,
            "price": price, "market_cap": market_cap,
            "market_cap_crore": market_cap_crore, "tier": tier, "ev": ev, "bvps": bvps,
            "bridge_known": bridge_known,
            "pe": ratio(price, positive(current["eps_diluted"])),
            "pb": ratio(price, positive(bvps)),
            "ev_ebitda": ratio(positive(ev), positive(current["ebitda"]))}


def fcff_from_record(row: dict[str, Any], company: dict[str, Any], config: dict[str, Any]) -> tuple[float | None, str]:
    tax = num(row.get("tax_rate"))
    if tax is None:
        tax = ratio(row.get("tax"), positive(row.get("pbt")))
    if tax is None or not (0 <= tax <= 0.5):
        tax = config["tax_rate_assumption"]
    capex = num(row.get("capex"))
    if capex is None or capex < 0:
        return None, "Missing/invalid positive CapEx; investing cash flow is not a CapEx proxy"
    ebit, depreciation, nwc = (num(row.get(k)) for k in ("ebit", "depreciation", "change_operating_nwc"))
    if None not in (ebit, depreciation, nwc):
        return ebit * (1 - tax) + depreciation - capex - nwc, "EBIT(1-tax)+D&A-CapEx-change_operating_NWC"
    cfo = num(row.get("cfo"))
    if cfo is None:
        return None, "Missing CFO and insufficient EBIT/reinvestment inputs"
    classification = row.get("cfo_interest_classification", company.get("cfo_interest_classification", "unknown"))
    cash_interest = num(row.get("cash_interest"))
    debt = num(row.get("debt"))
    if cash_interest is None and debt == 0 and num(row.get("interest_expense")) == 0:
        cash_interest = 0.0
    if cash_interest is None:
        return None, "Need cash interest and its operating/financing classification to unlever CFO"
    if classification == "operating" or (debt == 0 and cash_interest == 0):
        return cfo - capex + cash_interest * (1 - tax), "CFO-CapEx+cash_interest(1-tax), interest in CFO"
    if classification == "financing":
        # CFO excludes the interest cash payment but reported taxes retain its
        # tax shield. Remove that shield for an unlevered cash-flow estimate.
        return cfo - capex - cash_interest * tax, "CFO-CapEx-cash_interest*tax, interest outside CFO"
    return None, "CFO interest classification unknown; cannot discount levered cash flow using WACC"


def ev_to_equity(ev: float, balance: dict[str, Any]) -> float | None:
    keys = ("cash", "debt", "minority_interest", "preferred_equity", "non_operating_assets")
    if any(num(balance.get(key)) is None for key in keys):
        return None
    return ev + balance["cash"] + balance["non_operating_assets"] - balance["debt"] - balance["minority_interest"] - balance["preferred_equity"]


def discounted_fcff(baseline: float, growth: float, wacc: float, terminal_growth: float,
                    years: int = 5) -> dict[str, Any]:
    if wacc <= terminal_growth or wacc <= 0 or baseline <= 0:
        raise DataError("DCF requires positive baseline FCFF and WACC > terminal growth")
    cash = baseline
    projections = []
    for year in range(1, years + 1):
        # Conservative taper, bounded assumptions disclosed in the report.
        rate = growth if year <= 3 else growth * 0.9 ** (year - 3)
        cash *= 1 + rate
        discount = (1 + wacc) ** year
        projections.append({"year": year, "growth": rate, "fcff": cash, "present_value": cash / discount})
    terminal = cash * (1 + terminal_growth) / (wacc - terminal_growth)
    terminal_pv = terminal / (1 + wacc) ** years
    enterprise = sum(row["present_value"] for row in projections) + terminal_pv
    return {"enterprise_value": enterprise, "terminal_value": terminal, "terminal_pv": terminal_pv,
            "terminal_share": terminal_pv / enterprise, "projections": projections}


def unsupported_business(company: dict[str, Any]) -> bool:
    label = (str(company.get("sector", "")) + " " + str(company.get("industry", ""))).lower()
    return bool(re.search(r"\b(bank|banks|banking|insurance|insurer|nbfc|finance|financial|financials|investment trust|reit)\b", label))


def unavailable(reason: str) -> dict[str, Any]:
    return {"value": None, "low": None, "high": None, "status": "UNAVAILABLE", "reason": reason, "warnings": []}


def dcf_model(company: dict[str, Any], ctx: dict[str, Any], adjusted: list[dict[str, Any]],
              benchmark: list[dict[str, Any]], clock: dt.datetime, config: dict[str, Any]) -> dict[str, Any]:
    if unsupported_business(company):
        return unavailable("Generic industrial FCFF DCF is unsuitable for banks/NBFCs/insurers/REITs; use a sector-specific model")
    if ctx["diluted_shares"] is None or ctx["shares"] is None:
        return unavailable("Need current shares outstanding and a current diluted share-count assumption")
    if not ctx["bridge_known"]:
        missing = [field for field in ("debt", "cash", "minority_interest", "preferred_equity", "non_operating_assets")
                   if ctx["balance"].get(field) is None]
        return unavailable("Enterprise-to-equity bridge missing: " + ", ".join(missing) + "; verified zero is different from unknown")
    cashflows, failures = [], []
    for row in ctx["annual"][-3:]:
        value, method = fcff_from_record(row, company, config)
        if value is not None:
            cashflows.append({"period": row["period"], "fcff": value, "method": method})
        else:
            failures.append(row["period"] + ": " + method)
    if len(cashflows) < 3:
        return unavailable("Need 3 complete annual FCFF records. " + "; ".join(failures))
    average, latest = stats.fmean(row["fcff"] for row in cashflows), cashflows[-1]["fcff"]
    tolerance = config["dcf_latest_vs_average_tolerance"]
    baseline = latest if average > 0 and latest > 0 and abs(latest - average) / average <= tolerance else average
    if baseline <= 0:
        return unavailable("Non-positive normalized FCFF: this growth-only DCF needs a manually specified turnaround forecast")
    beta_raw, beta_count = stock_beta(adjusted, benchmark, clock) if adjusted else (None, 0)
    beta_override = num(company.get("beta_override"))
    beta_source = "observed two-year weekly price beta (not total-return beta)"
    if beta_override is not None:
        beta_raw, beta_source = beta_override, "explicit user beta override"
    if beta_raw is None:
        return unavailable(f"Beta unavailable ({beta_count} aligned weekly returns); verify price basis/actions or provide beta_override")
    beta = clip(beta_raw, config["beta_bounds"])
    ke = config["risk_free_rate"] + beta * config["equity_risk_premium"]
    debt = ctx["balance"]["debt"]
    tax = num(company.get("tax_rate_override"))
    if tax is None:
        tax = config["tax_rate_assumption"]
    if not 0 <= tax <= 0.5:
        return unavailable("Invalid effective tax assumption")
    kd = num(company.get("cost_of_debt_override"))
    if debt == 0:
        kd = 0.0
    elif kd is None:
        avg_debt = mean_valid(row.get("debt") for row in ctx["annual"][-2:])
        kd = ratio(ctx["current"].get("interest_expense"), avg_debt)
    if kd is None or kd < 0:
        return unavailable("Cost of debt unavailable: provide interest expense/average debt or cost_of_debt_override")
    equity = ctx["market_cap"]
    if equity is None or equity <= 0:
        return unavailable("Market equity weight unavailable")
    wacc_raw = ke * equity / (equity + debt) + kd * (1 - tax) * debt / (equity + debt)
    wacc = clip(wacc_raw, config["wacc_bounds"])
    growths = {f"{field}_{years}y": growth_rate(ctx["annual"], field, years)
               for field in ("revenue", "ebitda") for years in (3, 5)}
    growth_override = num(company.get("growth_override"))
    growth = growth_override if growth_override is not None else mean_valid(growths.values())
    if growth is None:
        return unavailable("Historical revenue/EBITDA growth unavailable; provide a reviewed growth_override")
    growth_raw = growth
    growth = clip(growth, config["growth_bounds"])
    terminal = num(company.get("terminal_growth_override"))
    terminal = config["terminal_growth"] if terminal is None else terminal
    if wacc <= terminal:
        return unavailable("WACC must exceed terminal growth")
    warnings = []
    if beta != beta_raw:
        warnings.append(f"Beta constrained by heuristic bounds: raw {beta_raw:.3f}, applied {beta:.3f}")
    if wacc != wacc_raw:
        warnings.append(f"WACC constrained by heuristic bounds: raw {100*wacc_raw:.2f}%, applied {100*wacc:.2f}%")
    if growth != growth_raw:
        warnings.append(f"Growth constrained by heuristic bounds: raw {100*growth_raw:.2f}%, applied {100*growth:.2f}%")
    if company.get("debt_includes_leases") is not True:
        warnings.append("Lease/debt consistency unverified; DCF bridge needs manual review")
    scenarios = {}
    assumptions = {
        "bear": (max(-0.25, growth - 0.05), wacc + 0.02, max(0, terminal - 0.01)),
        "base": (growth, wacc, terminal),
        "bull": (min(0.25, growth + 0.03), max(terminal + 0.015, wacc - 0.01), terminal + 0.005),
    }
    for name, (g, discount, terminal_g) in assumptions.items():
        result = discounted_fcff(baseline, g, discount, terminal_g, config["projection_years"])
        eq = ev_to_equity(result["enterprise_value"], ctx["balance"])
        result.update({"equity_value": eq, "value": eq / ctx["diluted_shares"], "growth": g,
                       "wacc": discount, "terminal_growth": terminal_g})
        scenarios[name] = result
    base = scenarios["base"]
    if base["value"] <= 0:
        return unavailable("DCF equity value is non-positive after debt and other claims")
    if base["terminal_share"] > 0.80:
        warnings.append(f"Terminal value supplies {100*base['terminal_share']:.1f}% of enterprise value; high assumption sensitivity")
    sensitivity = []
    for discount in (wacc - 0.01, wacc, wacc + 0.01):
        for terminal_g in (max(0, terminal - 0.005), terminal, terminal + 0.005):
            if discount <= terminal_g:
                value = None
            else:
                ev = discounted_fcff(baseline, growth, discount, terminal_g, config["projection_years"])["enterprise_value"]
                value = ev_to_equity(ev, ctx["balance"]) / ctx["diluted_shares"]
            sensitivity.append({"wacc": discount, "terminal_growth": terminal_g, "value": value})
    return {"value": base["value"], "low": max(0, scenarios["bear"]["value"]), "high": scenarios["bull"]["value"],
            "status": "OK", "reason": "", "warnings": warnings, "baseline_fcff": baseline,
            "cashflow_history": cashflows, "beta_raw": beta_raw, "beta": beta, "beta_observations": beta_count,
            "beta_source": beta_source, "cost_of_equity": ke, "cost_of_debt": kd,
            "wacc_raw": wacc_raw, "wacc": wacc, "growth_raw": growth_raw, "growth": growth,
            "growth_inputs": growths, "terminal_growth": terminal, "scenarios": scenarios,
            "sensitivity": sensitivity}


def peers_model(company: dict[str, Any], ctx: dict[str, Any], contexts: dict[str, dict[str, Any]],
                companies: dict[str, dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    sector = company.get("industry") or company.get("sector")
    if sector in {None, "UNKNOWN"}:
        return unavailable("Industry/sector classification missing")
    peers = [isin for isin, c in companies.items() if isin != company["isin"]
             and (c.get("industry") or c.get("sector")) == sector
             and isin in contexts and contexts[isin]["tier"] == ctx["tier"]
             and contexts[isin]["market_cap_crore"] is not None
             and contexts[isin]["market_cap_crore"] >= config["minimum_market_cap_crore"]]
    minimum = config["minimum_peers"]
    if len(peers) < minimum:
        return unavailable(f"Only {len(peers)} same-industry/same-custom-cap-tier peers; need {minimum}. Include more suitable peers, not unrelated large companies")
    components = {}
    for metric, weight in config["peer_multiple_weights"].items():
        if weight <= 0:
            continue
        if metric == "ev_ebitda" and unsupported_business(company):
            components[metric] = {"value": None, "reason": "EV/EBITDA not used for financial businesses", "count": 0}
            continue
        values, symbols = [], []
        for isin in peers:
            multiple = positive(contexts[isin].get(metric))
            if multiple is None or (metric == "pe" and multiple > config["maximum_peer_pe"]):
                continue
            values.append(multiple)
            symbols.append(companies[isin]["symbol"])
        if len(values) < minimum:
            components[metric] = {"value": None, "reason": f"{len(values)} valid multiples, need {minimum}", "count": len(values)}
            continue
        lower, median, upper = (quantile(values, q) for q in (0.25, 0.5, 0.75))
        def implied(mult: float) -> float | None:
            if metric == "pe":
                eps = positive(ctx["current"].get("eps_diluted"))
                return eps * mult if eps else None
            if metric == "pb":
                return ctx["bvps"] * mult if positive(ctx["bvps"]) else None
            ebitda = positive(ctx["current"].get("ebitda"))
            if not ebitda or not ctx["bridge_known"] or not ctx["diluted_shares"]:
                return None
            eq = ev_to_equity(ebitda * mult, ctx["balance"])
            # Limited-liability scenario floor, not a missing-data default.
            # Low EV multiples can leave no residual equity even when the
            # median scenario is positive; preserve a computed zero lower band.
            return max(0.0, eq / ctx["diluted_shares"])
        components[metric] = {"value": implied(median), "low": implied(lower), "high": implied(upper),
                              "median_multiple": median, "low_multiple": lower, "high_multiple": upper,
                              "count": len(values), "peer_symbols": symbols, "weight": weight,
                              "reason": "" if implied(median) is not None else "Target numerator/bridge unavailable"}
    applicable = [(k, v) for k, v in components.items() if positive(v.get("value"))]
    if not applicable:
        out = unavailable("No valid peer-implied valuation components")
        out["components"] = components
        return out
    total_weight = sum(config["peer_multiple_weights"][k] for k, _ in applicable)
    out = {field: sum(v[field] * config["peer_multiple_weights"][k] / total_weight for k, v in applicable)
           for field in ("value", "low", "high")}
    out.update({"status": "OK", "reason": "", "warnings": [], "components": components,
                "peer_count": len(peers), "tier": ctx["tier"]})
    if "pb" in dict(applicable):
        out["warnings"].append("P/B is a configured component; review its suitability for intangible-heavy businesses")
    return out


def build_historical_snapshots(company: dict[str, Any], clock: dt.datetime) -> list[dict[str, Any]]:
    if company.get("historical_snapshots"):
        result = []
        for row in company["historical_snapshots"]:
            date = release_timestamp(row.get("available_on"))
            if date and date <= clock:
                result.append(dict(row))
        return sorted(result, key=lambda row: release_timestamp(row["available_on"]))
    # Reconstruct only from actual publication timestamps, not fiscal-end dates.
    annual = available_rows(company, "annual", clock)
    quarters = available_rows(company, "quarterly", clock)
    snapshots = []
    for i in range(3, len(quarters)):
        rows = quarters[i - 3:i + 1]
        if any(release_timestamp(row.get("available_on")) is None or num(row.get("eps_diluted")) is None for row in rows):
            continue
        if any(not 65 <= (parse_date(b["period"]) - parse_date(a["period"])).days <= 115 for a, b in zip(rows, rows[1:])):
            continue
        basis_date = parse_date(rows[-1].get("share_basis_date", rows[-1]["period"]))
        eps_values = []
        for row in rows:
            factor = factors_between(company, parse_date(row.get("share_basis_date", row["period"])), basis_date)
            if factor is None:
                break
            eps_values.append(row["eps_diluted"] / factor if company.get("eps_basis") == "as_reported" else row["eps_diluted"])
        if len(eps_values) == 4:
            published = max(release_timestamp(row["available_on"]) for row in rows)
            snapshots.append({"available_on": published.isoformat(), "ttm_eps": sum(eps_values),
                              "share_basis_date": str(basis_date), "basis": company.get("eps_basis")})
    # Annual FY EPS is a TTM value at year-end. Keep it active only after filing;
    # quarterly TTM values (if supplied) supersede it on their publication dates.
    for row in annual:
        publication = release_timestamp(row.get("available_on"))
        if publication and num(row.get("eps_diluted")) is not None:
            snapshots.append({"available_on": publication.isoformat(), "ttm_eps": row["eps_diluted"],
                              "share_basis_date": row.get("share_basis_date", row["period"]),
                              "basis": company.get("eps_basis")})
    return sorted(snapshots, key=lambda row: release_timestamp(row["available_on"]))


def historical_model(company: dict[str, Any], ctx: dict[str, Any], candles: list[dict[str, Any]],
                     clock: dt.datetime, config: dict[str, Any]) -> dict[str, Any]:
    if not company.get("point_in_time_verified"):
        return unavailable("Need publication-dated, point-in-time financial snapshots. Current/restated histories cannot safely reconstruct historical P/E")
    if not company.get("actions_coverage_verified") or company.get("history_price_basis") not in {"raw", "split_adjusted"}:
        return unavailable("Verify corporate actions and raw/split-adjusted historical price basis first")
    if company.get("eps_basis") not in {"as_reported", "split_adjusted"}:
        return unavailable("Historical EPS share-adjustment basis is unverified")
    snapshots = build_historical_snapshots(company, clock)
    if not snapshots:
        return unavailable("No filing-dated EPS snapshots available; fiscal-end timestamps are not release timestamps")
    pe_values, pb_values, dates = [], [], []
    active = None
    index = 0
    for candle in candles:
        date = parse_date(candle["date"])
        # Daily close follows that day's announcements only when time is known.
        # A date-only availability is interpreted conservatively as end-of-day,
        # so it first becomes usable on the NEXT trading day.
        cutoff = dt.datetime.combine(date, dt.time(15, 30), IST)
        while index < len(snapshots):
            raw_release = snapshots[index].get("available_on")
            release = parse_timestamp(raw_release)
            if isinstance(raw_release, str) and len(raw_release) == 10:
                release = dt.datetime.combine(release.date(), dt.time(23, 59, 59), IST)
            if release >= cutoff:
                break
            active = snapshots[index]
            index += 1
        if active is None:
            continue
        basis_date = parse_date(active.get("share_basis_date"))
        if not basis_date:
            continue
        eps, bv = num(active.get("ttm_eps")), num(active.get("book_value_per_share"))
        effective_basis = active.get("basis", company["eps_basis"])
        end = clock.date() if company["history_price_basis"] == "split_adjusted" else date
        if effective_basis == "as_reported":
            factor = factors_between(company, basis_date, end)
            if not factor:
                continue
            eps = eps / factor if eps is not None else None
            bv = bv / factor if bv is not None else None
        elif effective_basis == "split_adjusted" and company["history_price_basis"] == "raw":
            factor = factors_between(company, date, clock.date())
            if not factor:
                continue
            eps = eps * factor if eps is not None else None
            bv = bv * factor if bv is not None else None
        multiple = ratio(candle["close"], positive(eps))
        if multiple is not None and multiple > 0:
            pe_values.append(multiple)
            dates.append(candle["date"])
        multiple_pb = ratio(candle["close"], positive(bv))
        if multiple_pb is not None and multiple_pb > 0:
            pb_values.append(multiple_pb)
    if len(pe_values) < config["minimum_historical_observations"]:
        return unavailable(f"Only {len(pe_values)} valid point-in-time P/E observations; need {config['minimum_historical_observations']}")
    eps = positive(ctx["current"].get("eps_diluted"))
    if eps is None:
        return unavailable("Current positive diluted EPS unavailable")
    p25, med, p75 = (quantile(pe_values, q) for q in (0.25, 0.5, 0.75))
    warnings = []
    coverage_years = (parse_date(dates[-1]) - parse_date(dates[0])).days / 365.25
    if coverage_years < 4.5:
        warnings.append(f"Partial historical coverage: {coverage_years:.2f} years, not a complete five-year band")
    return {"value": eps * med, "low": eps * p25, "high": eps * p75, "status": "OK", "reason": "",
            "warnings": warnings, "median_pe": med, "p25_pe": p25, "p75_pe": p75,
            "pe_observations": len(pe_values), "pb_observations": len(pb_values),
            "median_pb": quantile(pb_values, 0.5), "coverage_years": coverage_years,
            "first_observation": dates[0], "last_observation": dates[-1]}


def blend_models(models: dict[str, dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    applicable = {k: v for k, v in models.items() if positive(v.get("value")) and config["model_weights"].get(k, 0) > 0}
    if not applicable:
        return {"value": None, "low": None, "high": None, "weights": {}, "count": 0,
                "quarantined": [], "dispersion": None, "review_required": True}
    median = stats.median(model["value"] for model in applicable.values())
    quarantined = [name for name, model in applicable.items()
                   if len(applicable) >= 3 and (model["value"] > 2 * median or model["value"] < 0.4 * median)]
    # Outliers are not silently erased: quarantine is reported and stops orders.
    usable = {name: value for name, value in applicable.items() if name not in quarantined}
    total = sum(config["model_weights"][name] for name in usable)
    weights = {name: config["model_weights"][name] / total for name in usable}
    out = {field: sum(model.get(field, model["value"]) * weights[name] for name, model in usable.items())
           for field in ("value", "low", "high")}
    dispersion = max(m["value"] for m in applicable.values()) / min(m["value"] for m in applicable.values())
    out.update({"weights": weights, "count": len(usable), "quarantined": quarantined,
                "dispersion": dispersion, "review_required": bool(quarantined) or dispersion > config["max_model_dispersion"]})
    return out


# --------------------- Phase 3: sector-relative health -------------------

PILLARS = {
    "cash": {"cfo_ebitda": True, "fcf_sales": True, "interest_coverage": True, "positive_fcf_fraction": True},
    "profitability": {"roe": True, "roce": True, "operating_margin": True, "margin_stability": True},
    "growth": {"revenue_growth_3y": True, "eps_growth_3y": True, "quarter_profit_growth": True},
    "solvency": {"debt_equity": False, "net_debt_ebitda": False, "current_ratio": True},
    "value_momentum": {"earnings_yield": True, "return_6m": True, "return_12m": True},
}


def health_metrics(company: dict[str, Any], ctx: dict[str, Any], adjusted: list[dict[str, Any]],
                   clock: dt.datetime) -> dict[str, Any]:
    if unsupported_business(company):
        return {metric: None for definitions in PILLARS.values() for metric in definitions}
    current, balance = ctx["current"], ctx["balance"]
    rows = ctx["annual"]
    cash_rows = [row for row in rows if num(row.get("cfo")) is not None and num(row.get("capex")) is not None]
    latest_cash = cash_rows[-1] if cash_rows else {}
    fcfs = [row["cfo"] - row["capex"] for row in cash_rows[-5:]]
    # Do not divide annual CFO by TTM EBITDA spanning a different period.
    annual_ebitda = num(latest_cash.get("ebitda"))
    annual_revenue = num(latest_cash.get("revenue"))
    cfo = num(latest_cash.get("cfo"))
    fcf = cfo - latest_cash["capex"] if cfo is not None else None
    debt, cash, equity = (balance.get(key) for key in ("debt", "cash", "equity"))
    ebit, interest = current.get("ebit"), current.get("interest_expense")
    interest_coverage = ratio(ebit, interest)
    if interest == 0 and debt == 0 and positive(ebit):
        interest_coverage = 100.0  # Explicit debt-free upper cap, not infinity.
    parent_profit = current.get("parent_net_profit")
    avg_equity = mean_valid(row.get("equity") for row in rows[-2:])
    capital_employed = (equity + debt - cash) if None not in (equity, debt, cash) else None
    margins = [ratio(row.get("ebitda"), row.get("revenue")) for row in rows[-5:]]
    margins = [m for m in margins if m is not None]
    eps_rows = []
    if company.get("actions_coverage_verified") and company.get("eps_basis") in {"as_reported", "split_adjusted"}:
        for row in rows:
            eps = num(row.get("eps_diluted"))
            factor = factors_between(company, parse_date(row.get("share_basis_date", row["period"])), clock.date()) if company["eps_basis"] == "as_reported" else 1
            if eps is not None and factor:
                eps_rows.append({"period": row["period"], "eps_diluted": eps / factor})
    quarter_growth = None
    quarters = ctx["quarterly"]
    if len(quarters) >= 5:
        latest = quarters[-1]
        previous = [row for row in quarters[:-1] if abs((parse_date(latest["period"]) - parse_date(row["period"])).days - 365) <= 15]
        if previous and positive(previous[-1].get("net_profit")) and num(latest.get("net_profit")) is not None:
            quarter_growth = latest["net_profit"] / previous[-1]["net_profit"] - 1
    return {"cfo_ebitda": ratio(cfo, annual_ebitda), "fcf_sales": ratio(fcf, annual_revenue),
            "interest_coverage": min(interest_coverage, 100.0) if interest_coverage is not None else None,
            "positive_fcf_fraction": sum(f > 0 for f in fcfs) / len(fcfs) if len(fcfs) >= 3 else None,
            "roe": ratio(parent_profit, avg_equity), "roce": ratio(ebit, capital_employed),
            "operating_margin": ratio(current.get("ebitda"), current.get("revenue"))
                                if ctx["earnings_dates"].get("ebitda") == ctx["earnings_dates"].get("revenue") else None,
            "margin_stability": 1 / (1 + stats.pstdev(margins)) if len(margins) >= 3 else None,
            "revenue_growth_3y": growth_rate(rows, "revenue", 3),
            "eps_growth_3y": growth_rate(eps_rows, "eps_diluted", 3), "quarter_profit_growth": quarter_growth,
            "debt_equity": ratio(debt, equity),
            "net_debt_ebitda": ratio(debt - cash, current.get("ebitda")) if debt is not None and cash is not None else None,
            "current_ratio": ratio(balance.get("current_assets"), balance.get("current_liabilities")),
            "earnings_yield": ratio(ebit, ctx.get("ev")), "return_6m": price_return(adjusted, 183),
            "return_12m": price_return(adjusted, 365)}


def percentile_score(value: float | None, samples: list[float], higher_better: bool, minimum: int) -> float | None:
    valid = [v for x in samples if (v := num(x)) is not None]
    if value is None or len(valid) < minimum:
        return None
    # Midrank with the target included in the sample: tied stocks get equal
    # scores; an all-tied sector receives 2.5/5, not 5/5.
    less = sum(v < value - 1e-12 for v in valid)
    equal = sum(abs(v - value) <= 1e-12 for v in valid)
    rank = (less + max(0, equal - 1) / 2) / (len(valid) - 1)
    rank = min(1.0, max(0.0, rank))
    return 5 * (rank if higher_better else 1 - rank)


def health_scores(companies: dict[str, dict[str, Any]], metrics: dict[str, dict[str, Any]],
                  config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for isin, company in companies.items():
        sector = company.get("industry") or company.get("sector")
        group = [key for key, c in companies.items() if key in metrics
                 and (c.get("industry") or c.get("sector")) == sector and sector not in {None, "UNKNOWN"}]
        pillars, details = {}, {}
        for pillar, definitions in PILLARS.items():
            details[pillar] = {}
            for metric, higher_better in definitions.items():
                samples = [metrics[key].get(metric) for key in group]
                value = metrics[isin].get(metric)
                score = percentile_score(value, samples, higher_better, config["minimum_rank_sample"])
                details[pillar][metric] = {"raw": value, "score": score,
                                           "sample_count": sum(num(x) is not None for x in samples),
                                           "higher_better": higher_better}
            scores = [item["score"] for item in details[pillar].values() if item["score"] is not None]
            minimum_metrics = max(2, math.ceil(len(definitions) * 0.66))
            pillars[pillar] = stats.fmean(scores) if len(scores) >= minimum_metrics else None
        composite = mean_valid(pillars.values()) if all(v is not None for v in pillars.values()) else None
        fundamental = [pillars[key] for key in ("cash", "profitability", "growth", "solvency")]
        quality = stats.fmean(fundamental) if all(v is not None for v in fundamental) else None
        out[isin] = {"score": composite, "quality_score": quality, "pillars": pillars,
                     "details": details, "rank_universe_size": len(group),
                     "label": "Custom 5-pillar composite, not InvestingPro Financial Health"}
    return out


# ---------------- Phase 4: liquidity/structure/signal checks --------------


def tick_round(value: float, tick: float, direction: str = "nearest") -> float:
    if tick <= 0 or not math.isfinite(value):
        raise DataError("Invalid tick or price")
    rounding = {"up": decimal.ROUND_CEILING, "down": decimal.ROUND_FLOOR,
                "nearest": decimal.ROUND_HALF_UP}[direction]
    val, step = decimal.Decimal(str(value)), decimal.Decimal(str(tick))
    return float((val / step).to_integral_value(rounding=rounding) * step)


def instrument_tick(instrument: dict[str, Any], company: dict[str, Any], config: dict[str, Any]) -> float | None:
    explicit = positive(company.get("tick_size_rupees"))
    if explicit:
        return explicit
    tick = positive(instrument.get("tick_size"))
    return tick / config["tick_size_divisor"] if tick else None


def quote_checks(quote: dict[str, Any], tick: float | None, clock: dt.datetime,
                 config: dict[str, Any]) -> dict[str, Any]:
    price = positive(quote.get("last_price"))
    updated, traded = parse_timestamp(quote.get("timestamp")), parse_timestamp(quote.get("last_trade_time"))
    quote_age = (clock - updated).total_seconds() if updated else None
    trade_age = (clock - traded).total_seconds() if traded else None
    fresh = (quote_age is not None and -5 <= quote_age <= config["max_quote_age_seconds"]
             and trade_age is not None and -5 <= trade_age <= config["max_trade_age_seconds"])
    lower, upper = positive(quote.get("lower_circuit_limit")), positive(quote.get("upper_circuit_limit"))
    depth = quote.get("depth") or {}
    bids = [row for row in depth.get("buy", []) if positive(row.get("quantity")) and positive(row.get("price"))]
    asks = [row for row in depth.get("sell", []) if positive(row.get("quantity")) and positive(row.get("price"))]
    bid = max((row["price"] for row in bids), default=None)
    ask = min((row["price"] for row in asks), default=None)
    spread = (ask - bid) / price if price and bid and ask and ask >= bid else None
    tolerance = max(tick or 0.01, 0.001)
    circuit_known = lower is not None and upper is not None and lower < upper
    lower_locked = bool(price and lower and price <= lower + tolerance and not bids)
    upper_locked = bool(price and upper and price >= upper - tolerance and not asks)
    return {"fresh": fresh, "quote_age": quote_age, "trade_age": trade_age,
            "circuit_known": circuit_known, "lower_locked": lower_locked, "upper_locked": upper_locked,
            "bid": bid, "ask": ask, "spread": spread,
            "spread_ok": spread is not None and 0 <= spread <= config["max_spread_fraction"],
            "lower": lower, "upper": upper, "depth_present": bool(bids and asks)}


def structure_checks(candles: list[dict[str, Any]], quote: dict[str, Any], tick: float | None,
                     clock: dt.datetime) -> dict[str, Any]:
    completed = completed_candles(candles, clock)
    previous = [row for row in completed if parse_date(row["date"]) < clock.date()]
    price = positive(quote.get("last_price"))
    last20 = previous[-20:]
    low20 = min(row["low"] for row in last20) if len(last20) == 20 else None
    today_low = positive((quote.get("ohlc") or {}).get("low"))
    no_new_low = bool(low20 is not None and today_low is not None and today_low > low20)
    higher_daily_close = bool(len(completed) >= 2 and completed[-1]["close"] > completed[-2]["close"])
    weeks = weekly_candles(completed, clock)
    week_high = weeks[-1]["high"] if weeks else None
    reclaimed_week = bool(price and week_high and price > week_high)
    confirmation = no_new_low and (higher_daily_close or reclaimed_week)
    swing_low = None
    # Two complete weekly bars on either side make a confirmed local swing.
    for i in range(2, len(weeks) - 2):
        window = weeks[i - 2:i + 3]
        if all(weeks[i]["low"] < bar["low"] for bar in window if bar is not weeks[i]):
            if price and weeks[i]["low"] < price:
                swing_low = weeks[i]["low"]
    stop_basis = "confirmed weekly swing low"
    if swing_low is None and len(weeks) >= 4:
        swing_low = min(row["low"] for row in weeks[-4:])
        stop_basis = "lowest low of four complete weeks (fallback, not a confirmed swing)"
    stop = tick_round(swing_low - (tick or 0.01), tick or 0.01, "down") if swing_low else None
    turnover = stats.fmean(row["close"] * row["volume"] for row in previous[-20:]) if len(previous) >= 20 else None
    recent = [row for row in completed if parse_date(row["date"]) >= clock.date() - dt.timedelta(days=366)]
    return {"no_new_20d_low": no_new_low, "low_20d": low20, "today_low": today_low,
            "higher_daily_close": higher_daily_close, "previous_week_high": week_high,
            "reclaimed_week_high": reclaimed_week, "confirmed": confirmation,
            "stop": stop, "stop_basis": stop_basis, "average_turnover": turnover,
            "turnover_method": "20 completed sessions: close * volume turnover proxy",
            "year_high": max((row["high"] for row in recent), default=None),
            "year_low": min((row["low"] for row in recent), default=None)}


def freshness_checks(company: dict[str, Any], ctx: dict[str, Any], clock: dt.datetime,
                     config: dict[str, Any]) -> tuple[bool, list[str]]:
    issues = []
    annual = ctx["annual"]
    if not annual or (clock.date() - parse_date(annual[-1]["period"])).days > config["max_annual_age_days"]:
        issues.append("Annual financial data missing/stale")
    quarters = ctx["quarterly"]
    latest_earnings = quarters[-1]["period"] if quarters else (annual[-1]["period"] if annual else None)
    if not latest_earnings or (clock.date() - parse_date(latest_earnings)).days > config["max_earnings_age_days"]:
        issues.append("Earnings data missing/stale")
    for field in ("debt", "cash"):
        date = parse_date(ctx["balance_dates"].get(field))
        if not date or (clock.date() - date).days > config["max_annual_age_days"]:
            issues.append(f"{field} snapshot missing/stale")
    eps_date = parse_date(ctx.get("earnings_dates", {}).get("eps_diluted"))
    if not eps_date or (clock.date() - eps_date).days > config["max_earnings_age_days"]:
        issues.append("Actual EPS reporting period missing/stale (not excused by a newer revenue-only quarter)")
    shares_date = parse_date(company.get("shares_as_of"))
    if not shares_date or shares_date > clock.date() or (clock.date() - shares_date).days > config["max_annual_age_days"]:
        issues.append("Current/diluted share count as-of date missing/stale")
    return not issues, issues


def evaluate_stock(company: dict[str, Any], instrument: dict[str, Any], ctx: dict[str, Any],
                   models: dict[str, dict[str, Any]], health: dict[str, Any], quote: dict[str, Any],
                   adjusted: list[dict[str, Any]], market: dict[str, Any], clock: dt.datetime,
                   config: dict[str, Any]) -> dict[str, Any]:
    fair = blend_models(models, config)
    price = ctx["price"]
    upside = (fair["value"] - price) / price if price and fair["value"] else None
    discount = (fair["value"] - price) / fair["value"] if price and fair["value"] else None
    threshold = config["small_upside_threshold"] if ctx["tier"] == "small" else config["large_mid_upside_threshold"]
    if upside is None:
        signal = "INSUFFICIENT_DATA"
    elif upside >= threshold:
        signal = "UNDERVALUED_CANDIDATE"
    elif upside <= config["overvalued_upside_threshold"]:
        signal = "OVERVALUED"
    else:
        signal = "WITHIN_MODEL_BAND"
    tick = instrument_tick(instrument, company, config)
    qc = quote_checks(quote, tick, clock, config)
    sc = structure_checks(adjusted, quote, tick, clock) if adjusted else {
        "confirmed": False, "average_turnover": None, "stop": None,
        "no_new_20d_low": False, "stop_basis": "unavailable"}
    fresh_fundamentals, fresh_issues = freshness_checks(company, ctx, clock, config)
    gates = {
        "valuation_upside": upside is not None and upside >= threshold,
        "custom_health": health.get("score") is not None and health["score"] >= config["health_threshold"],
        "fundamental_quality": health.get("quality_score") is not None and health["quality_score"] >= config["quality_threshold"],
        "cash_flow_quality": health.get("pillars", {}).get("cash") is not None and health["pillars"]["cash"] >= config["cash_pillar_threshold"],
        "liquidity": sc.get("average_turnover") is not None and sc["average_turnover"] >= config["minimum_daily_turnover_crore"] * CRORE,
        "not_circuit_locked": qc["circuit_known"] and not qc["lower_locked"] and not qc["upper_locked"],
        "price_structure": bool(sc["confirmed"]),
        "fresh_fundamentals": fresh_fundamentals,
        "fresh_quote_and_trade": qc["fresh"],
        "live_two_sided_depth": qc["spread_ok"] and qc["depth_present"],
        "model_count": fair["count"] >= config["minimum_valid_models_for_action"] and positive(models["dcf"].get("value")) is not None,
        "model_agreement": not fair["review_required"],
        "reviewed_data_and_governance": review_is_current(company, ctx, clock),
        "verified_tick": company.get("tick_verified") is True and tick is not None,
        "lease_consistency": company.get("debt_includes_leases") is True,
        "minimum_market_cap": ctx["market_cap_crore"] is not None and ctx["market_cap_crore"] >= config["minimum_market_cap_crore"],
        "supported_business": not unsupported_business(company),
        "normal_market_session": tradable_session(market, quote, clock),
        "positive_equity": positive(ctx["balance"].get("equity")) is not None,
        "positive_earnings": positive(ctx["current"].get("eps_diluted")) is not None and positive(ctx["current"].get("ebit")) is not None,
        "absolute_cash_flow_safety": absolute_cash_flow_safety(ctx, config),
        "absolute_debt_safety": absolute_debt_safety(ctx, config),
    }
    return {"symbol": company["symbol"], "name": company.get("name"), "isin": company["isin"],
            "instrument_key": company["instrument_key"], "sector": company["sector"],
            "industry": company["industry"], "price": price, "fair": fair, "models": models,
            "health": health, "context": ctx, "upside": upside, "discount": discount,
            "upside_threshold": threshold, "signal": signal, "gates": gates,
            "ready": all(gates.values()), "failed_gates": [k for k, v in gates.items() if not v],
            "quote_checks": qc, "structure": sc, "tick": tick,
            "source": company["source"], "source_as_of": company.get("source_as_of"),
            "provider_ratios": company.get("provider_ratios", []),
            "warnings": list(company.get("warnings", [])) + fresh_issues
                        + [warning for model in models.values() for warning in model.get("warnings", [])],
            "plan": None}

# -------------------- Upstox-only relative comparisons -------------------


def extract_competitor_keys(payload: Any) -> list[str]:
    """Accept documented lists and common wrapper shapes; never guess keys."""
    if isinstance(payload, dict):
        payload = payload.get("competitors", payload.get("data", []))
    if not isinstance(payload, list):
        return []
    keys = [row.get("instrument_key") for row in payload if isinstance(row, dict)]
    return list(dict.fromkeys(key for key in keys if isinstance(key, str) and "|" in key))


def sanitize_company_dump(value: Any) -> Any:
    """Defense in depth. Caller supplies company responses, NOT env/account."""
    forbidden = {"authorization", "headers", "accesstoken", "analyticstoken", "apitoken", "apisecret", "apikey",
                 "clientsecret", "clientid", "password", "upstoxaccesstoken", "upstoxanalyticstoken",
                 "upstoxapitoken", "upstoxapikey", "upstoxapisecret"}
    if isinstance(value, dict):
        return {key: "[REDACTED]" if norm_label(key) in forbidden else sanitize_company_dump(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_company_dump(item) for item in value]
    return value


RATIO_NAMES = {
    "pe": ("P/E", "multiple"), "pb": ("P/B", "multiple"),
    "ev_ebitda": ("EV/EBITDA", "multiple"),
    "roe": ("ROE", "percent_points"), "roce": ("ROCE", "percent_points"),
    "roa": ("ROA", "percent_points"), "quick_ratio": ("Quick Ratio", "multiple"),
}
RATIO_ALIASES = {
    "pe": "pe", "peratio": "pe", "priceearningsratio": "pe", "pricetoearnings": "pe",
    "pb": "pb", "pbratio": "pb", "pricebookratio": "pb", "pricetobook": "pb",
    "evebitda": "ev_ebitda", "evtoebitda": "ev_ebitda",
    "roe": "roe", "returnonequity": "roe", "roce": "roce", "returnoncapitalemployed": "roce",
    "roa": "roa", "returnonassets": "roa", "quickratio": "quick_ratio",
}


def reported_ratios(payload: Any) -> dict[str, dict[str, Any]]:
    """Return provider values; percent returns stay in percentage points.

    An unqualified numeric ROE of 28.53 means the provider's displayed 28.53%,
    not a measured 0.2853 ratio. No undocumented fractional-unit inference.
    """
    if isinstance(payload, dict):
        payload = payload.get("ratios", payload.get("key_ratios", payload.get("data", [])))
    result = {}
    if not isinstance(payload, list):
        return result
    for row in payload:
        if not isinstance(row, dict):
            continue
        key = RATIO_ALIASES.get(norm_label(row.get("name", row.get("ratio_name", ""))))
        if not key:
            continue
        label, unit = RATIO_NAMES[key]
        company, sector = num(row.get("company_value")), num(row.get("sector_value"))
        result[key] = {"label": label, "unit": unit, "company": company, "sector": sector,
                       "company_raw": row.get("company_value"), "sector_raw": row.get("sector_value"),
                       "difference": company - sector if company is not None and sector is not None else None,
                       "relative_gap": ratio(company, sector) - 1 if company is not None and positive(sector) else None}
    return result


def matching_annual_diagnostics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Pair flow denominators by exact fiscal period, never FY CFO / TTM sales."""
    cash_rows = [row for row in rows if num(row.get("cfo")) is not None][-5:]
    cfo_positive = sum(row["cfo"] > 0 for row in cash_rows)
    years = []
    for row in rows[-5:]:
        cfo, sales, capex = num(row.get("cfo")), num(row.get("revenue")), num(row.get("capex"))
        years.append({"period": row["period"], "revenue": sales, "cfo": cfo,
                      "cfo_revenue": ratio(cfo, sales), "capex": capex,
                      "cfo_minus_capex": cfo - capex if cfo is not None and capex is not None else None})
    liquid = [row for row in rows if num(row.get("current_assets")) is not None
              and positive(row.get("current_liabilities"))]
    current = liquid[-1] if liquid else {}
    return {"years": years, "revenue_cagr_3y": growth_rate(rows, "revenue", 3),
            "cfo_cagr_3y": growth_rate(rows, "cfo", 3),
            "cfo_positive_years": cfo_positive, "cfo_available_years": len(cash_rows),
            "current_ratio": ratio(current.get("current_assets"), current.get("current_liabilities")),
            "current_ratio_period": current.get("period")}


def indicative_price_diagnostics(company: dict[str, Any], candles: list[dict[str, Any]],
                                  adjusted: list[dict[str, Any]], price: float | None,
                                  clock: dt.datetime) -> dict[str, Any]:
    """Display unverified API levels without allowing valuation/trading gates."""
    verified = bool(adjusted) and company.get("actions_coverage_verified") is True
    source = adjusted if verified else candles
    rows = completed_candles([r for r in source if parse_date(r.get("date")) and positive(r.get("close"))
                             and positive(r.get("high")) and positive(r.get("low"))
                             and num(r.get("volume")) is not None and r["volume"] >= 0], clock)
    rows = sorted(rows, key=lambda r: r["date"])
    last20, last50 = rows[-20:], rows[-50:]
    enough20, enough50 = len(last20) == 20, len(last50) == 50
    ma20 = mean_valid(r["close"] for r in last20) if enough20 else None
    ma50 = mean_valid(r["close"] for r in last50) if enough50 else None
    high20 = max(r["high"] for r in last20) if enough20 else None
    low20 = min(r["low"] for r in last20) if enough20 else None
    turnover = mean_valid(r.get("raw_close", r["close"]) * r["volume"] for r in candles
                          if r.get("date") in {x["date"] for x in last20}) if enough20 else None
    # Turnover proxy uses price/volume from the SAME as-returned API candle.
    # The provider's underlying adjustment basis may itself be unverified.
    action_window = [a for a in company.get("actions", []) if not parse_date(a.get("date"))
                     or (last20 and parse_date(last20[0]["date"]) <= parse_date(a.get("date")) <= clock.date())]
    return {"basis": "VERIFIED_ADJUSTED_LEVELS" if verified else "UNVERIFIED_API_LEVELS_INDICATIVE",
            "last_completed_day": rows[-1]["date"] if rows else None, "completed_candles": len(rows),
            "ma20": ma20, "ma50": ma50, "high20": high20, "low20": low20,
            "ltp_vs_ma20": ratio(price, ma20) - 1 if price and ma20 else None,
            "above_prior_20d_high": bool(price > high20) if price and high20 else None,
            "below_prior_20d_low": bool(price < low20) if price and low20 else None,
            "average_turnover20_crore": turnover / CRORE if turnover is not None else None,
            "known_actions_in_20d_window": action_window,
            "notice": "Price/volume adjustment basis and action coverage may be incomplete. Raw levels are diagnostics, not return/beta or trading gates. Turnover is close x volume, not exchange traded-value."}


def relative_stock(company: dict[str, Any], quote: dict[str, Any], candles: list[dict[str, Any]],
                   adjusted: list[dict[str, Any]], clock: dt.datetime, config: dict[str, Any]) -> dict[str, Any]:
    ctx = financial_context(company, quote, clock, config)
    ratios = reported_ratios(company.get("provider_ratios"))
    snapshot_time = parse_timestamp(quote.get("timestamp"))
    trade_time = parse_timestamp(quote.get("last_trade_time"))
    snapshot_age = (clock - snapshot_time).total_seconds() if snapshot_time else None
    trade_age = (clock - trade_time).total_seconds() if trade_time else None
    timing = ("SNAPSHOT_TIME_MISSING" if snapshot_age is None else "SNAPSHOT_TIME_IN_FUTURE" if snapshot_age < -5
              else "SNAPSHOT_STALE" if snapshot_age > config["max_quote_age_seconds"] else "RECENT_SNAPSHOT")
    eps, price = num(ctx["current"].get("eps_diluted")), ctx["price"]
    eps_period = ctx["earnings_dates"].get("eps_diluted")
    pe_row = ratios.get("pe", {})
    sector_pe = positive(pe_row.get("sector"))
    warnings = list(company.get("warnings", []))
    if timing != "RECENT_SNAPSHOT":
        warnings.append("Quote timing: " + timing + "; LTP is displayed as supplied, not certified fresh. References remain read-only diagnostics")
    if trade_age is None or trade_age > config["max_trade_age_seconds"] or trade_age < -5:
        warnings.append("Last-trade timestamp missing/stale/future; LTP may not be current (stale last trade is normal outside continuous trading)")
    reference_reason = None
    if not price:
        reference_reason = "No valid live price"
    elif not positive(eps):
        reference_reason = "Positive diluted EPS is not supplied; no EPS inferred from provider P/E"
    elif not sector_pe:
        reference_reason = "Positive sector P/E is not supplied"
    elif not parse_date(eps_period):
        reference_reason = "EPS fiscal period unavailable"
    else:
        age = (clock.date() - parse_date(eps_period)).days
        maximum = config["max_earnings_age_days"] if ctx["earnings_basis"]["eps_diluted"].startswith("TTM") else config["max_annual_age_days"]
        if age > maximum:
            reference_reason = "EPS fiscal period is too old for the configured diagnostic age limit"
        elif company.get("eps_basis") not in {"split_adjusted", "as_reported"}:
            later_actions = [a for a in company.get("actions", []) if not parse_date(a.get("date"))
                             or parse_date(eps_period) < parse_date(a.get("date")) <= clock.date()]
            if later_actions:
                reference_reason = "Known post-EPS capital action; comparable EPS share basis unverified"
    reference = num(eps * sector_pe) if reference_reason is None else None
    gap = reference / price - 1 if reference is not None and price else None
    live_pe = ratio(price, positive(eps))
    if gap is None:
        signal = "REFERENCE_UNAVAILABLE"
    elif abs(gap) < 0.00005:
        signal = "LIVE_EPS_PE_AT_SECTOR"
    else:
        signal = "LIVE_EPS_PE_BELOW_SECTOR" if gap > 0 else "LIVE_EPS_PE_ABOVE_SECTOR"
    warnings.extend([
        "Sector benchmarks: provider snapshot date, earnings basis, weighting and peer coverage are not documented in this response. Do not equate FY-derived P/E with a confirmed same-basis sector P/E.",
        "EPS/share basis and complete corporate-action coverage remain unverified unless explicitly supplied. The displayed EPS x sector P/E is an illustrative comparison, NOT intrinsic fair value.",
        "No complete financial health/quality score, DCF, enterprise-to-equity bridge or buy/sell decision is inferred from missing fields.",
    ])
    report = {"mode": "upstox-only", "demo": "SYNTHETIC" in company.get("source", "").upper(),
              "symbol": company["symbol"], "isin": company["isin"], "instrument_key": company["instrument_key"],
              "sector": company["sector"], "price": price, "signal": signal, "ready": False,
              "orders_allowed": False, "source": company["source"], "retrieved_at": company.get("source_as_of"),
              "retrieval_notice": "Client bundle retrieval time, possibly cached; NOT the provider benchmark as-of or filing publication time",
              "accounting_basis": company.get("accounting_basis"), "reported_ratios": ratios,
              "eps_diluted": eps, "eps_basis": ctx["earnings_basis"].get("eps_diluted"), "eps_period": eps_period,
              "live_eps_pe": live_pe, "sector_pe_reference": reference, "reference_gap": gap,
              "reference_reason": reference_reason, "reference_status": "ILLUSTRATIVE_BASIS_UNVERIFIED" if reference is not None else "UNAVAILABLE",
              "reference_formula": "reported diluted EPS x provider sector P/E",
              "revenue": ctx["current"].get("revenue"), "revenue_basis": ctx["earnings_basis"].get("revenue"),
              "revenue_period": ctx["earnings_dates"].get("revenue"),
              "annual_diagnostics": matching_annual_diagnostics(ctx["annual"]),
              "balance_reported": {field: next(({"value": row[field], "period": row["period"]} for row in reversed(ctx["annual"])
                                       if num(row.get(field)) is not None), {"value": None, "period": None})
                                   for field in ("cash_reported", "equity_total_reported", "total_liabilities_reported")},
              "price_diagnostics": indicative_price_diagnostics(company, candles, adjusted, price, clock),
              "quote_time": quote.get("timestamp"), "last_trade_time": quote.get("last_trade_time"),
              "quote_timing": timing, "quote_snapshot_age_seconds": snapshot_age, "last_trade_age_seconds": trade_age,
              "warnings": list(dict.fromkeys(warnings))}
    provider_pe = positive(pe_row.get("company"))
    if live_pe and provider_pe and abs(live_pe / provider_pe - 1) > 0.05:
        report["warnings"].append("Provider company P/E differs from live-price / reported EPS by >5%; snapshot/earnings/share-basis mismatch requires review")
    return report


def print_relative_details(r: dict[str, Any]) -> None:
    print(f"\n{r['symbol']} | {r['sector']} | {r['isin']} | {r['accounting_basis']} | READ ONLY")
    print(f"  Quote timing {r['quote_timing']}; snapshot {r['quote_time'] or 'N/A'}; last trade {r['last_trade_time'] or 'N/A'}")
    print(f"  Diluted EPS INR {fmt(r['eps_diluted'])}; {r['eps_basis']}; price / EPS = {fmt(r['live_eps_pe'])}x")
    print(f"  EPS x sector P/E reference INR {fmt(r['sector_pe_reference'])}; gap vs LTP {pct(r['reference_gap'])}")
    print("  Reference is illustrative, basis/timing unverified; NOT intrinsic fair value or a price target.")
    if r["reference_reason"]:
        print("  Reference unavailable: " + r["reference_reason"])
    rows = []
    for key, (label, unit) in RATIO_NAMES.items():
        ratio_row = r["reported_ratios"].get(key, {})
        suffix = "%" if unit == "percent_points" else "x"
        render = lambda value: fmt(value) + suffix if num(value) is not None else "N/A"
        diff = ratio_row.get("difference")
        gap = (f"{diff:+.2f} pp" if diff is not None else "N/A") if unit == "percent_points" else pct(ratio_row.get("relative_gap"))
        rows.append([label, render(ratio_row.get("company")), render(ratio_row.get("sector")), gap])
    table(["Reported ratio", "Company", "Sector", "Difference / relative gap"], rows)
    print("  P/B and EV/EBITDA are ratio comparisons only: no invented BVPS, share count or equity bridge.")
    a = r["annual_diagnostics"]
    print(f"  Revenue INR {fmt(r['revenue']/CRORE if r['revenue'] is not None else None)} Cr; {r['revenue_basis']}; period {r['revenue_period'] or 'N/A'}")
    print(f"  Annual revenue 3Y CAGR {pct(a['revenue_cagr_3y'])}; annual CFO 3Y CAGR {pct(a['cfo_cagr_3y'])}")
    table(["Fiscal period", "Revenue Cr", "CFO Cr", "CFO / FY revenue", "CapEx Cr", "CFO-CapEx Cr"],
          [[y["period"], fmt(y["revenue"]/CRORE if y["revenue"] is not None else None),
            fmt(y["cfo"]/CRORE if y["cfo"] is not None else None), pct(y["cfo_revenue"]),
            fmt(y["capex"]/CRORE if y["capex"] is not None else None),
            fmt(y["cfo_minus_capex"]/CRORE if y["cfo_minus_capex"] is not None else None)] for y in a["years"]])
    print(f"  Positive CFO: {a['cfo_positive_years']}/{a['cfo_available_years']} available FYs (coverage, NOT a health score)")
    print(f"  Same-period current ratio {fmt(a['current_ratio'])}x; FY {a['current_ratio_period'] or 'N/A'}")
    balance_labels = {"cash_reported": "Reported cash at FY end (not a verified EV-bridge cash figure)",
                      "equity_total_reported": "Reported total equity (not paid-up share capital/share count)",
                      "total_liabilities_reported": "Reported total liabilities (NOT financial debt)"}
    for field, item in r["balance_reported"].items():
        if item["value"] is not None:
            print(f"  {balance_labels[field]}: INR {fmt(item['value']/CRORE)} Cr; {item['period']}")
    p = r["price_diagnostics"]
    print(f"  Price diagnostics: {p['basis']}; latest complete candle {p['last_completed_day'] or 'N/A'}")
    print(f"  API MA20 {fmt(p['ma20'])}; MA50 {fmt(p['ma50'])}; prior 20-day high/low {fmt(p['high20'])}/{fmt(p['low20'])}")
    print(f"  LTP vs MA20 {pct(p['ltp_vs_ma20'])}; API close x volume proxy average (20 days) INR {fmt(p['average_turnover20_crore'])} Cr")
    print(f"  Above prior 20-day high: {p['above_prior_20d_high']}; below prior 20-day low: {p['below_prior_20d_low']} (diagnostics only)")
    if p["known_actions_in_20d_window"]:
        print("  CAUTION: known capital action in the price window; raw levels may be incomparable")
    print("  " + p["notice"])
    for warning in r["warnings"]:
        print(textwrap.fill("  NOTE: " + warning, width=112, subsequent_indent="    "))


def print_relative_summary(results: list[dict[str, Any]], market: dict[str, Any], clock: dt.datetime,
                           top: int, details: bool) -> None:
    print(f"\nAS-OF {clock.isoformat()} | NSE session: {market.get('status', 'UNKNOWN')} | UPSTOX-ONLY / READ ONLY")
    ranked = sorted(results, key=lambda r: r["reference_gap"] if r["reference_gap"] is not None else -math.inf, reverse=True)
    shown = ranked[:top] if top else ranked
    table(["Symbol", "LTP INR", "Diluted EPS", "Live EPS P/E", "Sector P/E", "PE ref INR*", "Ref gap*", "Comparison"],
          [[r["symbol"], fmt(r["price"]), fmt(r["eps_diluted"]), fmt(r["live_eps_pe"]),
            fmt(r["reported_ratios"].get("pe", {}).get("sector")), fmt(r["sector_pe_reference"]),
            pct(r["reference_gap"]), r["signal"]] for r in shown])
    print("* EPS x provider sector P/E; illustrative, benchmark/share basis unverified. NOT intrinsic value, a buy/sell signal or guaranteed upside.")
    if top and len(ranked) > top:
        print(f"Console limited to {top} of {len(ranked)} rows; all rows exported.")
    if details:
        for r in shown:
            print_relative_details(r)
    print("\nTrading is disabled in comparison mode. No fabricated 0-5 health/quality scores or portfolio cash assumptions.")


def export_relative_reports(directory: Path, results: list[dict[str, Any]], clock: dt.datetime,
                            market: dict[str, Any], demo: bool, input_errors: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory / "latest_screen.json", {"version": VERSION, "mode": "upstox-only", "demo": demo,
                "as_of": clock, "market": market, "input_errors": input_errors, "orders_allowed": False,
                "notice": "Relative references only; benchmark/earnings/share basis unverified. NOT intrinsic values, targets or buy/sell signals.",
                "results": results})
    fields = ["symbol", "isin", "sector", "price", "eps_diluted", "eps_basis", "eps_period", "live_eps_pe",
              "sector_pe_reference", "reference_gap_pct", "reference_status", "reference_reason", "signal", "orders_allowed",
              "revenue_crore", "revenue_basis", "revenue_period", "revenue_cagr_3y_pct", "cfo_cagr_3y_pct",
              "cfo_positive_years", "cfo_available_years", "current_ratio", "current_ratio_period",
              "average_turnover20_crore", "ma20", "ma50", "prior_20d_high", "prior_20d_low", "price_basis",
              "quote_timing", "quote_snapshot_age_seconds", "last_trade_age_seconds", "data_source", "warnings"]
    fields += [f"{key}_{side}" for key in RATIO_NAMES for side in ("company", "sector")]
    temporary = directory / "latest_screen.csv.tmp"
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for r in results:
            a, p = r["annual_diagnostics"], r["price_diagnostics"]
            row = {key: r.get(key) for key in fields if key in r}
            row.update({"reference_gap_pct": 100*r["reference_gap"] if r["reference_gap"] is not None else None,
                        "revenue_crore": r["revenue"]/CRORE if r["revenue"] is not None else None,
                        "revenue_cagr_3y_pct": 100*a["revenue_cagr_3y"] if a["revenue_cagr_3y"] is not None else None,
                        "cfo_cagr_3y_pct": 100*a["cfo_cagr_3y"] if a["cfo_cagr_3y"] is not None else None,
                        "data_source": r["source"], "warnings": ";".join(r["warnings"]),
                        "price_basis": p["basis"], "ma20": p["ma20"], "ma50": p["ma50"],
                        "prior_20d_high": p["high20"], "prior_20d_low": p["low20"],
                        "average_turnover20_crore": p["average_turnover20_crore"]})
            row.update({key: a[key] for key in ("cfo_positive_years", "cfo_available_years", "current_ratio", "current_ratio_period")})
            row.update({f"{key}_{side}": r["reported_ratios"].get(key, {}).get(side)
                        for key in RATIO_NAMES for side in ("company", "sector")})
            writer.writerow(row)
    os.replace(temporary, directory / "latest_screen.csv")


# -------------------- Phase 5: portfolio and execution --------------------

TERMINAL_ORDER_STATUSES = {"complete", "completed", "cancelled", "canceled", "rejected"}
TERMINAL_INTENT_STATUSES = {"REJECTED", "CANCELLED", "CLOSED", "RESOLVED_MANUALLY", "PAPER_CLOSED"}


def isin_from_key(key: Any) -> str | None:
    if isinstance(key, str) and key.startswith(("NSE_EQ|", "BSE_EQ|")):
        return key.split("|", 1)[1]
    return None


def holding_sellable(holding: dict[str, Any]) -> int:
    # Deliberately conservative: do not sell unsettled/pledged/blocked stock.
    quantity = num(holding.get("quantity"))
    if quantity is None:
        return 0
    return max(0, math.floor(quantity - (num(holding.get("t1_quantity")) or 0)
                             - (num(holding.get("collateral_quantity")) or 0)
                             - (num(holding.get("cnc_used_quantity")) or 0)))


def sector_for_isin(isin: str, companies: dict[str, dict[str, Any]], client: Any,
                    memo: dict[str, str | None]) -> str | None:
    if isin in companies:
        sector = companies[isin].get("sector")
        return sector if sector and sector != "UNKNOWN" else None
    custom_taxonomy = any(c.get("provider_sector", c.get("sector")) != c.get("sector") for c in companies.values())
    if custom_taxonomy:
        # An API industry label cannot safely be added to a user-defined broad
        # sector budget. Include every held stock in the enriched universe.
        memo[isin] = None
        return None
    if isin not in memo:
        try:
            memo[isin] = client.sector(isin)
        except ScreenerError:
            memo[isin] = None
    return memo[isin]


def portfolio_view(account: dict[str, Any], companies: dict[str, dict[str, Any]], client: Any,
                   memo: dict[str, str | None]) -> dict[str, Any]:
    stocks: dict[str, float] = defaultdict(float)
    quantities: dict[str, float] = defaultdict(float)
    sectors: dict[str, float] = defaultdict(float)
    errors = list(account.get("errors", []))
    pending_buys, pending_sells = set(), set()
    pending_gtt_cash = 0.0
    unknown_sectors = set()
    holdings = account.get("holdings") or []
    positions = account.get("positions") or []
    orders = account.get("orders") or []
    gtts = account.get("gtt") or []
    if not all(isinstance(items, list) for items in (holdings, positions, orders, gtts)):
        return {"complete": False, "errors": errors + ["Unexpected portfolio response schema"],
                "stock_values": {}, "sector_values": {}, "quantities": {}, "pending_buys": [],
                "pending_sells": [], "unknown_sectors": [], "available_margin": None}

    def add(isin: str | None, quantity: float, price: float | None) -> None:
        if not isin or quantity <= 0:
            return
        if price is None or price <= 0:
            errors.append(f"Portfolio price missing for {isin}; exposure cannot be established")
            return
        value = quantity * price
        stocks[isin] += value
        quantities[isin] += quantity
        sector = sector_for_isin(isin, companies, client, memo)
        if not sector:
            unknown_sectors.add(isin)
        else:
            sectors[sector] += value
    for holding in holdings:
        isin = holding.get("isin") or isin_from_key(holding.get("instrument_token"))
        # Conservative T+1 inclusion may overstate exposure if already in
        # quantity; it never increases the buying allowance.
        qty = (num(holding.get("quantity")) or 0) + (num(holding.get("t1_quantity")) or 0)
        add(isin, qty, positive(holding.get("last_price")))
    for position in positions:
        isin = isin_from_key(position.get("instrument_token"))
        if isin and position.get("product") in {"D", "MTF", "I"}:
            # Conservatively add positive positions instead of subtracting
            # potentially stale holdings or relying on settlement timing.
            add(isin, abs(num(position.get("quantity")) or 0), positive(position.get("last_price")))
    for order in orders:
        isin = isin_from_key(order.get("instrument_token"))
        if not isin or str(order.get("status", "")).lower() in TERMINAL_ORDER_STATUSES:
            continue
        if order.get("transaction_type") == "BUY":
            pending_buys.add(isin)
            qty = num(order.get("pending_quantity"))
            if qty is None:
                qty = max(0, (num(order.get("quantity")) or 0) - (num(order.get("filled_quantity")) or 0))
            add(isin, qty, positive(order.get("price")))
        else:
            pending_sells.add(isin)
    for gtt in gtts:
        isin = isin_from_key(gtt.get("instrument_token"))
        if not isin:
            continue
        entry = next((rule for rule in gtt.get("rules", []) if rule.get("strategy") == "ENTRY"), {})
        if str(entry.get("status", "")).upper() in {"SCHEDULED", "PENDING", "OPEN", "TRIGGERED", "INACTIVE"}:
            side = entry.get("transaction_type", gtt.get("transaction_type", "BUY"))
            if side == "BUY":
                pending_buys.add(isin)
                quantity, trigger = num(gtt.get("quantity")) or 0, positive(entry.get("trigger_price"))
                add(isin, quantity, trigger)
                if trigger:
                    pending_gtt_cash += quantity * trigger
            else:
                pending_sells.add(isin)
    funds = account.get("funds") or {}
    available = num((funds.get("equity") or {}).get("available_margin")) if isinstance(funds, dict) else None
    if available is None:
        errors.append("Available equity margin missing; never interpreted as cash")
    if unknown_sectors:
        errors.append("Unclassified portfolio exposures: " + ", ".join(sorted(unknown_sectors)))
    return {"complete": account.get("complete", False) and not errors,
            "errors": errors, "stock_values": dict(stocks), "sector_values": dict(sectors),
            "quantities": dict(quantities), "pending_buys": sorted(pending_buys),
            "pending_sells": sorted(pending_sells), "unknown_sectors": sorted(unknown_sectors),
            "available_margin": available, "pending_gtt_cash": pending_gtt_cash}


class PaperAccount:
    """Transparent local simulation; no broker order is ever sent."""
    def __init__(self, state: State, capital: float, demo: bool):
        self.state = state
        self.key = "paper-account-v1-demo" if demo else "paper-account-v1-market"
        self.data = state.cached(self.key, 100 * 365 * 86400)
        if self.data is None:
            self.data = {"initial_capital": capital, "cash": capital, "positions": {}, "closed": []}
            self.save()

    def save(self) -> None:
        self.state.put_cache(self.key, self.data)

    def snapshot(self, results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        holdings = []
        for isin, pos in self.data["positions"].items():
            result = results.get(isin)
            price = result["price"] if result and result["price"] else pos["entry"]
            holdings.append({"isin": isin, "instrument_token": pos["instrument_key"],
                             "trading_symbol": pos["symbol"], "quantity": pos["quantity"], "last_price": price})
        return {"complete": True, "errors": [], "holdings": holdings, "positions": [], "orders": [], "gtt": [],
                "funds": {"equity": {"available_margin": self.data["cash"]}}}

    def buy(self, plan: dict[str, Any], cost_buffer: float) -> dict[str, Any]:
        if plan["isin"] in self.data["positions"]:
            raise ScreenerError("Paper position already exists; no automatic averaging/pyramiding")
        cost = plan["quantity"] * plan["entry"] * (1 + cost_buffer)
        if cost > self.data["cash"] + 1e-6:
            raise ScreenerError("Paper cash insufficient")
        self.data["cash"] -= cost
        self.data["positions"][plan["isin"]] = {**plan, "opened_at": now_ist().isoformat()}
        self.save()
        return {"simulated": True, "filled_quantity": plan["quantity"], "fill_price": plan["entry"],
                "note": "Simplified immediate fill at preview limit, cost buffer included; not an exchange-fill simulation"}

    def monitor(self, results: dict[str, dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
        closed = []
        for isin, position in list(self.data["positions"].items()):
            result = results.get(isin)
            if not result or not result["price"]:
                continue
            price = result["price"]
            health = result["health"].get("score")
            reason = ("STRUCTURAL_STOP" if price <= position["stop"] else
                      "FAIR_VALUE_TARGET" if price >= position["target"] else
                      "HEALTH_DOWNGRADE" if health is not None and health < config["health_exit_threshold"] else None)
            if not reason:
                continue
            proceeds = position["quantity"] * price * (1 - config["cash_cost_buffer"])
            self.data["cash"] += proceeds
            event = {"symbol": position["symbol"], "isin": isin, "reason": reason,
                     "quantity": position["quantity"], "fill_price": price,
                     "simulated": True, "closed_at": now_ist().isoformat()}
            self.data["closed"].append(event)
            del self.data["positions"][isin]
            closed.append(event)
        if closed:
            self.save()
        return closed


def plan_trade(result: dict[str, Any], instrument: dict[str, Any], view: dict[str, Any],
               capital: float, spendable_cash: float | None, config: dict[str, Any]) -> dict[str, Any]:
    def blocked(reason: str) -> dict[str, Any]:
        return {"status": "BLOCKED", "reason": reason, "quantity": 0}
    if not result["ready"]:
        return blocked("Research-only: " + ", ".join(result["failed_gates"]))
    if not view.get("complete"):
        return blocked("Portfolio/funds/sector snapshot incomplete")
    isin, sector = result["isin"], result["sector"]
    if isin in view["pending_buys"] or isin in view["pending_sells"]:
        return blocked("Existing open entry/exit; no duplicate order")
    if spendable_cash is None or spendable_cash <= 0:
        return blocked("No verified strategy cash available")
    entry = tick_round(result["quote_checks"]["ask"], result["tick"], "up")
    stop = result["structure"].get("stop")
    target = tick_round(config["target_fair_value_fraction"] * result["fair"]["value"], result["tick"], "down")
    if stop is None or not (0 < stop < entry < target):
        return blocked("Need a valid structural stop < entry < target")
    if (entry - stop) / entry > config["max_stop_distance_fraction"]:
        return blocked("Structural stop too distant for configured risk policy")
    reward_risk = (target - entry) / (entry - stop)
    if reward_risk < config["minimum_reward_risk"]:
        return blocked("Reward/risk below configured minimum")
    lower, upper = result["quote_checks"].get("lower"), result["quote_checks"].get("upper")
    if not lower or not upper or not lower <= entry <= upper:
        return blocked("Entry outside current circuit limits")
    stock_room = max(0, capital * config["max_stock_allocation"] - view["stock_values"].get(isin, 0))
    sector_room = max(0, capital * config["max_sector_allocation"] - view["sector_values"].get(sector, 0))
    liquidity_room = result["structure"]["average_turnover"] * config["maximum_turnover_participation"]
    budget = min(stock_room, sector_room, spendable_cash / (1 + config["cash_cost_buffer"]), liquidity_room)
    risk_budget = capital * config["risk_fraction_per_trade"]
    quantity = min(math.floor(budget / entry), math.floor(risk_budget / (entry - stop)))
    freeze = positive(instrument.get("freeze_quantity"))
    if freeze:
        quantity = min(quantity, math.floor(freeze))
    lot = int(positive(instrument.get("lot_size")) or 1)
    quantity -= quantity % lot
    if quantity < 1:
        return blocked("Cash, allocation, liquidity or stop-risk cap yields zero shares")
    return {"status": "PLANNED", "reason": "", "isin": isin, "symbol": result["symbol"],
            "instrument_key": result["instrument_key"], "sector": sector, "quantity": quantity,
            "entry": entry, "stop": stop, "target": target, "tick": result["tick"],
            "value": quantity * entry, "cash_reservation": quantity * entry * (1 + config["cash_cost_buffer"]),
            "rupee_risk": quantity * (entry - stop), "reward_risk": reward_risk,
            "stop_basis": result["structure"]["stop_basis"], "fair_value": result["fair"]["value"],
            "note": "Stop/GTT triggers are not guaranteed fills; gaps can exceed planned loss"}


def reserve_preview(view: dict[str, Any], plan: dict[str, Any]) -> None:
    if plan.get("status") != "PLANNED":
        return
    isin, sector = plan["isin"], plan["sector"]
    view["stock_values"][isin] = view["stock_values"].get(isin, 0) + plan["value"]
    view["sector_values"][sector] = view["sector_values"].get(sector, 0) + plan["value"]


def gtt_payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {"type": "MULTIPLE", "quantity": plan["quantity"], "product": "D",
            "instrument_token": plan["instrument_key"], "transaction_type": "BUY",
            "rules": [{"strategy": "ENTRY", "trigger_type": "IMMEDIATE", "trigger_price": plan["entry"], "market_protection": -1},
                      {"strategy": "TARGET", "trigger_type": "IMMEDIATE", "trigger_price": plan["target"], "market_protection": -1},
                      {"strategy": "STOPLOSS", "trigger_type": "IMMEDIATE", "trigger_price": plan["stop"], "market_protection": -1}]}


def limit_payload(key: str, side: str, quantity: int, price: float, tag: str) -> dict[str, Any]:
    return {"quantity": quantity, "product": "D", "validity": "DAY", "price": price,
            "tag": tag[:40], "instrument_token": key, "order_type": "LIMIT",
            "transaction_type": side, "disclosed_quantity": 0, "trigger_price": 0,
            "is_amo": False, "slice": False, "market_protection": -1}


def validate_live_flags(args: argparse.Namespace, env: dict[str, str]) -> None:
    if not args.execute:
        return
    if args.demo or args.paper_orders:
        raise ScreenerError("--execute cannot be combined with --demo or --paper-orders")
    if env.get("UPSTOX_LIVE_TRADING_ENABLED") != "YES":
        raise ScreenerError("Live writes disabled. Set UPSTOX_LIVE_TRADING_ENABLED=YES locally only after testing")
    if env.get("UPSTOX_REGISTERED_APP_ACK") != "YES":
        raise ScreenerError("A registered app/static-IP setup is required. Acknowledge verified setup with UPSTOX_REGISTERED_APP_ACK=YES")
    if not args.capital or not args.cash_budget:
        raise ScreenerError("Live mode needs explicit positive --capital and --cash-budget. Available margin is not necessarily unencumbered cash")
    if args.entry_method == "limit" and (not args.watch or env.get("UPSTOX_MANAGED_EXITS_ACK") != "YES"):
        raise ScreenerError("LIMIT entry has no broker bracket: requires --watch and UPSTOX_MANAGED_EXITS_ACK=YES. GTT is the default")
    if len(env.get("UPSTOX_ACCESS_TOKEN", "")) < 40:
        raise AuthenticationError("Live orders need a complete daily UPSTOX_ACCESS_TOKEN, not just an analytics token")
    if not sys.stdin.isatty():
        raise ScreenerError("Live execution requires an interactive terminal and confirmation for EVERY broker mutation")


def confirm_mutation(description: str, phrase: str) -> bool:
    print("\nLIVE BROKER ACTION -- " + description)
    print("Type exactly '" + phrase + "' to authorize this one request; anything else cancels.")
    if not sys.stdin.isatty():
        return False
    try:
        return input("> ").strip() == phrase
    except (EOFError, KeyboardInterrupt):
        print("Request not authorized.")
        return False


def live_pending_for_key(state: State, key: str) -> bool:
    return any(row["instrument"] == key and row["status"] not in TERMINAL_INTENT_STATUSES
               for row in state.intents("live"))


def reconcile_live(client: Any, state: State, account: dict[str, Any], quiet: bool = False) -> None:
    """Acknowledged != filled. Persist partial fills, uncertain writes and protection."""
    for intent in state.intents("live"):
        if intent["status"] in TERMINAL_INTENT_STATUSES:
            continue
        response = dict(intent["response"])
        data = response.get("broker", {}).get("data") or {}
        gtt_ids = data.get("gtt_order_ids", [])
        order_ids = data.get("order_ids", [])
        payload = intent["payload"]
        if not gtt_ids and not order_ids:
            # Recover a lost acknowledgement only from a unique matching broker
            # record. Never resubmit on an empty match: completed GTTs vanish.
            candidates = []
            if "rules" in payload:
                created = parse_timestamp(intent["created"])
                for gtt in account.get("gtt") or []:
                    entry = next((r for r in gtt.get("rules", []) if r.get("strategy") == "ENTRY"), {})
                    timestamp = parse_timestamp(gtt.get("created_at"))
                    price = payload["rules"][0]["trigger_price"]
                    if (gtt.get("instrument_token") == intent["instrument"] and gtt.get("quantity") == payload["quantity"]
                            and num(entry.get("trigger_price")) is not None and abs(entry["trigger_price"] - price) < 1e-6
                            and timestamp and created and abs((timestamp - created).total_seconds()) < 180):
                        candidates.append(gtt["gtt_order_id"])
                if len(candidates) == 1:
                    gtt_ids = candidates
                    response["broker"] = {"data": {"gtt_order_ids": candidates}, "recovered": True}
            else:
                candidates = [order["order_id"] for order in account.get("orders") or [] if order.get("tag") == payload.get("tag")]
                if candidates:
                    order_ids = candidates
                    response["broker"] = {"data": {"order_ids": candidates}, "recovered": True}
            if not gtt_ids and not order_ids:
                state.set_intent(intent["id"], "NEEDS_RECONCILIATION", response)
                if not quiet:
                    print(f"  Intent {intent['id']} {intent['symbol']}: outcome uncertain; automatic re-entry blocked")
                continue
        try:
            if gtt_ids:
                gtts = [gtt for gid in gtt_ids for gtt in client.get_gtt(gid)]
                response["gtt_details"] = gtts
                if not gtts:
                    # API does not return completed GTT orders. A disappearance
                    # is NOT proof that the entry was rejected or position flat.
                    state.set_intent(intent["id"], "NEEDS_RECONCILIATION", response)
                    continue
                rules = [rule for gtt in gtts for rule in gtt.get("rules", [])]
                entries = [rule for rule in rules if rule.get("strategy") == "ENTRY"]
                ids = [rule["order_id"] for rule in entries if rule.get("order_id")]
                fills = [client.order_details(oid) for oid in ids]
                response["entry_orders"] = fills
                filled = sum(int(num(row.get("filled_quantity")) or 0) for row in fills)
                response["filled_quantity"] = filled
                exit_rules = [rule for rule in rules if rule.get("strategy") in {"TARGET", "STOPLOSS"}]
                active_exits = {rule.get("strategy") for rule in exit_rules
                                if rule.get("status") in {"SCHEDULED", "OPEN", "PENDING"}}
                response["protection_verified"] = {"TARGET", "STOPLOSS"}.issubset(active_exits)
                exit_fills = [client.order_details(rule["order_id"]) for rule in exit_rules if rule.get("order_id")]
                response["exit_orders"] = exit_fills
                sold = sum(int(num(row.get("filled_quantity")) or 0) for row in exit_fills)
                if filled and sold >= filled:
                    status = "CLOSED"
                elif filled and not response["protection_verified"]:
                    status = "FILLED_PROTECTION_UNVERIFIED"
                elif 0 < filled < payload["quantity"]:
                    status = "PARTIAL"
                elif filled >= payload["quantity"]:
                    status = "GTT_ACTIVE"
                elif entries and all(rule.get("status") in {"FAILED", "CANCELLED", "EXPIRED"} for rule in entries):
                    status = "REJECTED"
                else:
                    status = "ENTRY_PENDING"
            else:
                fills = [client.order_details(oid) for oid in order_ids]
                response["orders"] = fills
                filled = sum(int(num(row.get("filled_quantity")) or 0) for row in fills)
                response["filled_quantity"] = filled
                if filled >= payload["quantity"]:
                    status = "CLOSED" if intent["action"] == "SELL" else "FILLED_WATCH_REQUIRED"
                elif filled > 0:
                    status = "PARTIAL"
                elif all(str(row.get("status", "")).lower() in {"rejected", "cancelled"} for row in fills):
                    status = "REJECTED"
                else:
                    status = "ENTRY_PENDING"
            state.set_intent(intent["id"], status, response)
            if not quiet:
                print(f"  Intent {intent['id']} {intent['symbol']}: {status}, verified filled quantity {filled}")
                if status in {"PARTIAL", "FILLED_PROTECTION_UNVERIFIED", "FILLED_WATCH_REQUIRED"}:
                    print("    ATTENTION: verify the position and exit protection in the broker app.")
        except ScreenerError as exc:
            response["reconcile_error"] = str(exc)
            state.set_intent(intent["id"], "NEEDS_RECONCILIATION", response)
            if not quiet:
                print(f"  Intent {intent['id']}: could not verify current state; no resend")


def execute_entry(client: Any, state: State, args: argparse.Namespace, env: dict[str, str],
                  result: dict[str, Any], plan: dict[str, Any], instrument: dict[str, Any],
                  companies: dict[str, dict[str, Any]], memo: dict[str, str | None],
                  adjusted: list[dict[str, Any]], config: dict[str, Any]) -> None:
    if client.is_demo:
        raise ScreenerError("Demo client can never execute a live mutation")
    if live_pending_for_key(state, plan["instrument_key"]):
        print("  Blocked: existing managed intent needs closure/reconciliation.")
        return
    phrase = f"BUY {plan['symbol']} {plan['quantity']}"
    if not confirm_mutation(f"{phrase} at limit {plan['entry']}, target {plan['target']}, stop {plan['stop']}", phrase):
        print("  Not submitted.")
        return
    # Recheck AFTER confirmation to close the stale-input window. Keep the
    # confirmed quantity/price fixed, rather than silently change the order.
    clock = now_ist()
    quote = client.quotes([plan["instrument_key"]]).get(plan["instrument_key"], {})
    market = client.market()
    account = client.account()
    view = portfolio_view(account, companies, client, memo)
    clock = now_ist()  # Freshness is checked AFTER all possibly slow reads.
    qc = quote_checks(quote, plan["tick"], clock, config)
    sc = structure_checks(adjusted, quote, plan["tick"], clock)
    price = positive(quote.get("last_price"))
    available = view.get("available_margin")
    spendable = max(0.0, min(available, args.cash_budget) - view.get("pending_gtt_cash", 0.0)) if available is not None else None
    fresh_result = {**result, "quote_checks": qc, "structure": sc, "price": price}
    fresh_result["ready"] = (result["ready"] and qc["fresh"] and qc["spread_ok"] and sc["confirmed"]
                             and tradable_session(market, quote, clock) and price is not None
                             and result["fair"]["value"] / price - 1 >= result["upside_threshold"])
    safer = plan_trade(fresh_result, instrument, view, args.capital, spendable, config)
    if (safer.get("status") != "PLANNED" or safer["quantity"] < plan["quantity"]
            or abs(safer["entry"] - plan["entry"]) > max(plan["tick"], plan["entry"] * 0.001)
            or plan["isin"] in view["pending_buys"] or live_pending_for_key(state, plan["instrument_key"])):
        print("  Fresh risk/quote/account check changed. NOT submitted; rerun and confirm a new plan.")
        return
    fingerprint = "live|BUY|" + plan["isin"] + "|" + str(clock.date())
    tag = "FV" + hashlib.sha256(fingerprint.encode()).hexdigest()[:24]
    payload = gtt_payload(plan) if args.entry_method == "gtt" else limit_payload(plan["instrument_key"], "BUY", plan["quantity"], plan["entry"], tag)
    ident = state.reserve_intent(fingerprint, "live", plan["instrument_key"], plan["symbol"], "BUY", payload)
    if ident is None:
        print("  Duplicate daily order fingerprint: NOT submitted.")
        return
    response = None
    try:
        response = client.place_gtt(payload) if args.entry_method == "gtt" else client.place_limit(payload)
        data = response.get("data") or {}
        ids = data.get("gtt_order_ids") or data.get("order_ids")
        if not ids:
            raise AmbiguousWriteError("Broker acknowledgement has no reference IDs")
        state.set_intent(ident, "ACKNOWLEDGED", {"broker": response, "plan": plan})
        print(f"  Broker acknowledged {', '.join(ids)}. This is NOT a fill confirmation.")
        reconcile_live(client, state, client.account())
    except AmbiguousWriteError as exc:
        state.set_intent(ident, "UNKNOWN", {"plan": plan, "error": str(exc)})
        print("  UNKNOWN OUTCOME: no resend. Inspect broker orders and use --ledger/--reconcile.")
    except ScreenerError as exc:
        acknowledged = response and ((response.get("data") or {}).get("gtt_order_ids") or (response.get("data") or {}).get("order_ids"))
        if acknowledged:
            state.set_intent(ident, "NEEDS_RECONCILIATION", {"broker": response, "plan": plan, "error": str(exc)})
            print("  Broker acknowledged but read-side verification failed; no resend, reconcile manually.")
        else:
            state.set_intent(ident, "REJECTED", {"plan": plan, "error": str(exc)})
            print("  Broker rejected the request: " + str(exc))


def health_exit_plans(results: dict[str, dict[str, Any]], account: dict[str, Any],
                      config: dict[str, Any]) -> list[dict[str, Any]]:
    exits = []
    for holding in account.get("holdings") or []:
        isin = holding.get("isin") or isin_from_key(holding.get("instrument_token"))
        result = results.get(isin)
        if not result:
            continue
        score = result["health"].get("score")
        if score is not None and score < config["health_exit_threshold"]:
            exits.append({"symbol": result["symbol"], "isin": isin,
                          "instrument_key": holding.get("instrument_token") or result["instrument_key"],
                          "quantity": holding_sellable(holding), "reason": "HEALTH_DOWNGRADE",
                          "health": score, "price": result["price"], "tick": result["tick"]})
    return exits


def execute_health_exit(client: Any, state: State, args: argparse.Namespace, env: dict[str, str],
                        plan: dict[str, Any], config: dict[str, Any]) -> None:
    exit_allowed = args.allow_health_exits or plan.get("reason") in {"LIMIT_TARGET", "LIMIT_STOP"}
    if not exit_allowed or env.get("UPSTOX_SELL_AUTHORIZED") != "YES":
        print("  Exit is an alert only. Live sales need --allow-health-exits and verified DDPI/TPIN authorization acknowledgement.")
        return
    if plan["quantity"] <= 0 or not plan["tick"]:
        print("  No verified settled, unpledged, unblocked sellable quantity/tick. Handle in broker app.")
        return
    account = client.account()
    if not account.get("complete"):
        print("  Exit blocked: account state incomplete.")
        return
    key = plan["instrument_key"]
    # Do not remove a protective bracket when a sale cannot even be attempted.
    preliminary_quote = client.quotes([key]).get(key, {})
    preliminary_checks = quote_checks(preliminary_quote, plan["tick"], now_ist(), config)
    if not preliminary_checks["fresh"] or not preliminary_checks["bid"] or not tradable_session(client.market(), preliminary_quote, now_ist()):
        print("  Sale not currently feasible: existing protection left untouched; use broker app if urgent.")
        return
    print("  WARNING: cancelling a GTT removes its protection. A later LIMIT sell may not fill; monitor until verified flat.")
    # Cancel only this program's known GTTs, one confirmed mutation at a time.
    managed_ids = set()
    for intent in state.intents("live"):
        if intent["instrument"] == key:
            managed_ids.update((intent["response"].get("broker", {}).get("data") or {}).get("gtt_order_ids", []))
    active_gtts = [g for g in account.get("gtt") or [] if g.get("instrument_token") == key]
    if any(g["gtt_order_id"] not in managed_ids for g in active_gtts):
        print("  Exit blocked: external GTT exists; reconcile it manually to avoid a duplicate sell.")
        return
    for gtt in active_gtts:
        gid = gtt["gtt_order_id"]
        if not confirm_mutation("Cancel managed GTT before discretionary health exit: " + gid, "CANCEL " + gid):
            return
        try:
            client.cancel_gtt(gid)
        except ScreenerError:
            print("  Cancellation outcome not verified; exit NOT submitted.")
            return
    account = client.account()
    pending = [o for o in account.get("orders") or [] if o.get("instrument_token") == key
               and str(o.get("status", "")).lower() not in TERMINAL_ORDER_STATUSES]
    still_gtt = any(g.get("instrument_token") == key for g in account.get("gtt") or [])
    if not account.get("complete") or pending or still_gtt:
        print("  A live order/GTT remains or account incomplete. Health sell blocked pending reconciliation.")
        return
    qty = min(plan["quantity"], sum(holding_sellable(h) for h in account.get("holdings") or []
                                    if (h.get("isin") or isin_from_key(h.get("instrument_token"))) == plan["isin"]))
    quote = client.quotes([key]).get(key, {})
    qc = quote_checks(quote, plan["tick"], now_ist(), config)
    if not qty or not qc["fresh"] or not qc["bid"] or not tradable_session(client.market(), quote, now_ist()):
        print("  Fresh quote/session/sellable-quantity check failed; health exit remains an alert.")
        return
    price = tick_round(qc["bid"], plan["tick"], "down")
    phrase = f"SELL {plan['symbol']} {qty}"
    if not confirm_mutation(f"Health downgrade exit: {phrase}, delivery LIMIT at {price}", phrase):
        return
    # Price is a fixed limit, not an unlimited market order. Revalidate the
    # session and account after the human prompt as well.
    account = client.account()
    current_qty = sum(holding_sellable(h) for h in account.get("holdings") or []
                      if (h.get("isin") or isin_from_key(h.get("instrument_token"))) == plan["isin"])
    if not account.get("complete") or current_qty < qty or client.market().get("status") != "NORMAL_OPEN":
        print("  State changed during confirmation; sell NOT sent.")
        return
    fingerprint = "live|SELL|" + plan["isin"] + "|" + str(now_ist().date())
    payload = limit_payload(key, "SELL", qty, price, "FVEXIT" + hashlib.sha256(fingerprint.encode()).hexdigest()[:20])
    ident = state.reserve_intent(fingerprint, "live", key, plan["symbol"], "SELL", payload)
    if ident is None:
        print("  Duplicate exit blocked.")
        return
    broker = None
    try:
        broker = client.place_limit(payload)
        if not (broker.get("data") or {}).get("order_ids"):
            raise AmbiguousWriteError("Missing sell order acknowledgement IDs")
        state.set_intent(ident, "ACKNOWLEDGED", {"broker": broker, "plan": plan})
        reconcile_live(client, state, client.account())
    except AmbiguousWriteError as exc:
        state.set_intent(ident, "UNKNOWN", {"error": str(exc), "plan": plan})
        print("  Sell outcome UNKNOWN: no retry; reconcile in broker app.")
    except ScreenerError as exc:
        if broker and (broker.get("data") or {}).get("order_ids"):
            state.set_intent(ident, "NEEDS_RECONCILIATION", {"broker": broker, "error": str(exc), "plan": plan})
            print("  Sell acknowledged but verification failed; no resend.")
        else:
            state.set_intent(ident, "REJECTED", {"error": str(exc), "plan": plan})
            print("  Sell rejected: " + str(exc))


def show_ledger(state: State) -> None:
    rows = state.intents()
    if not rows:
        print("No local order intentions recorded.")
        return
    table(["ID", "Mode", "Symbol", "Side", "State", "Created"],
          [[r["id"], r["mode"], r["symbol"], r["action"], r["status"], r["created"]] for r in rows])


def resolve_intent(state: State, ident: int) -> None:
    matches = [row for row in state.intents() if row["id"] == ident]
    if not matches:
        raise ScreenerError("Local intent ID does not exist")
    print("This only clears a LOCAL re-entry block. It does NOT cancel an order or close a broker position.")
    print("First inspect holdings, positions, order book and GTTs in Upstox. Never clear an uncertain live order blindly.")
    if confirm_mutation(f"Mark local intent {ident} manually reconciled", f"RESOLVED {ident}"):
        state.set_intent(ident, "RESOLVED_MANUALLY", {**matches[0]["response"], "manual_resolution_at": now_ist().isoformat()})
        print("Local intent marked reconciled by user.")

# -------------------- Offline, explicitly synthetic demo -----------------

class DemoUpstox:
    is_demo = True

    def __init__(self, state: State, config: dict[str, Any]):
        self.state, self.config = state, config
        self.clock = dt.datetime(2026, 10, 14, 13, 0, tzinfo=IST)
        self.supplements: dict[str, dict[str, Any]] = {}
        self.raw_instruments = []
        self.history = {}
        self.quote_data = {}
        self.bundles = {}
        self.mutations = 0
        rng = random.Random(17)
        start = self.clock.date() - dt.timedelta(days=5 * 366)
        days = []
        date = start
        while date < self.clock.date():
            if date.weekday() < 5:
                days.append(date)
            date += dt.timedelta(days=1)
        # Synthetic benchmark and companies, not actual exchange histories.
        index_prices = []
        index = 17000.0
        for n, date in enumerate(days):
            index *= 1 + 0.0002 + 0.006 * math.sin(n * 0.73) + rng.gauss(0, 0.001)
            index_prices.append({"date": str(date), "open": index * 0.999, "high": index * 1.004,
                                 "low": index * 0.996, "close": index, "raw_close": index, "volume": 0.0})
        self.index_key = "NSE_INDEX|Nifty 50"
        self.history[self.index_key] = index_prices
        self.raw_instruments.append({"segment": "NSE_INDEX", "instrument_type": "INDEX",
                                     "name": "Nifty 50", "instrument_key": self.index_key})
        for i in range(8):
            symbol = "DEMOVALUE" if i == 0 else f"DEMOPEER{i}"
            isin = f"DEMO{i:08d}"  # Explicitly not a real broker-tradable ISIN.
            key = "NSE_EQ|" + isin
            instrument = {"segment": "NSE_EQ", "instrument_type": "EQ", "security_type": "NORMAL",
                          "isin": isin, "trading_symbol": symbol, "name": symbol + " (SYNTHETIC)",
                          "instrument_key": key, "tick_size": 1.0, "lot_size": 1, "freeze_quantity": 100000}
            self.raw_instruments.append(instrument)
            shares = 50_000_000.0
            revenue_now = (1500 if i == 0 else 1300 + 90 * i) * CRORE
            growth = 0.17 if i == 0 else 0.055 + 0.008 * i
            margin = 0.20 if i == 0 else 0.175 - 0.002 * i
            annual = []
            for year in range(2019, 2027):
                revenue = revenue_now / (1 + growth) ** (2026 - year)
                m = margin if i == 0 else margin + 0.012 * math.sin(year + i)
                depreciation = revenue * 0.02
                ebitda = revenue * m
                ebit = ebitda - depreciation
                interest = revenue * (0.002 if i == 0 else 0.009 + i * 0.001)
                pbt = ebit - interest
                pat = pbt * 0.75
                row = {"period": f"{year}-03-31", "available_on": f"{year}-05-20T17:00:00+05:30",
                       "share_basis_date": f"{year}-03-31", "revenue": revenue, "ebitda": ebitda,
                       "ebit": ebit, "depreciation": depreciation, "interest_expense": interest,
                       "cash_interest": interest, "pbt": pbt, "tax": pbt * 0.25, "tax_rate": 0.25,
                       "net_profit": pat, "parent_net_profit": pat, "eps_diluted": pat / shares,
                       "cfo": revenue * (0.18 if i == 0 else 0.105 + i * 0.003),
                       "capex": revenue * (0.04 if i == 0 else 0.055 + i * 0.001),
                       "debt": revenue * (0.035 if i == 0 else 0.20 + i * 0.02),
                       "cash": revenue * (0.07 if i == 0 else 0.025),
                       "equity": revenue * (0.60 if i == 0 else 0.80 + i * 0.04),
                       "current_assets": revenue * (0.60 if i == 0 else 0.36),
                       "current_liabilities": revenue * (0.18 if i == 0 else 0.24 + i * 0.005),
                       "total_assets": revenue * 1.1, "minority_interest": 0.0,
                       "preferred_equity": 0.0, "non_operating_assets": 0.0}
                annual.append(row)
            quarterly = []
            for year in range(2020, 2027):
                for month in (3, 6, 9, 12):
                    period = dt.date(year, month, calendar.monthrange(year, month)[1])
                    if period > dt.date(2026, 6, 30):
                        continue
                    elapsed = (period - dt.date(2026, 3, 31)).days / 365.25
                    revenue = revenue_now * (1 + growth) ** elapsed / 4
                    ebitda = revenue * margin
                    ebit = ebitda - revenue * 0.02
                    interest = revenue * (0.002 if i == 0 else 0.009 + i * 0.001)
                    pat = (ebit - interest) * 0.75
                    quarterly.append({"period": str(period), "available_on": (period + dt.timedelta(days=45)).isoformat(),
                                      "share_basis_date": str(period), "revenue": revenue,
                                      "ebitda": ebitda, "ebit": ebit, "net_profit": pat,
                                      "parent_net_profit": pat, "interest_expense": interest, "eps_diluted": pat / shares})
            supplement = {"isin": isin, "symbol": symbol, "source": "SYNTHETIC DEMO FIXTURE -- NOT COMPANY FINANCIALS",
                          "source_as_of": str(self.clock.date()), "sector": "Packaged Foods (DEMO)",
                          "industry": "Packaged Foods (DEMO)", "currency": "INR", "unit": "rupees",
                          "accounting_basis": "consolidated", "shares_outstanding": shares,
                          "diluted_shares": shares, "shares_as_of": "2026-06-30", "history_price_basis": "raw",
                          "eps_basis": "as_reported", "actions_coverage_verified": True,
                          "point_in_time_verified": True, "manual_review_complete": True,
                          "last_reviewed_financial_period": "2026-06-30", "manual_review_as_of": "2026-10-14",
                          "tick_verified": True, "tick_size_rupees": 0.01, "debt_includes_leases": True,
                          "cfo_interest_classification": "operating", "actions": [],
                          "annual": annual, "quarterly": quarterly, "historical_snapshots": []}
            self.supplements[isin] = supplement
            current_price = 263.2 if i == 0 else 470.0 + 25 * i
            candles = []
            for n, date in enumerate(days):
                if i == 0:
                    price = 380 + 0.14 * n + 4 * math.sin(n * 0.22)
                    if n >= len(days) - 100:
                        progress = (n - len(days) + 100) / 60
                        price = 560 * (1 - min(progress, 1)) + 230 * min(progress, 1)
                    if n >= len(days) - 40:
                        price = 230 + (current_price - 0.4 - 230) * (n - len(days) + 40) / 39
                else:
                    progress = n / (len(days) - 1)
                    price = current_price * (0.58 + 0.42 * progress) * (1 + 0.002 * math.sin(n * 0.73 + i))
                candles.append({"date": str(date), "open": price * 0.998, "high": price * 1.006,
                                "low": price * 0.994, "close": price, "raw_close": price, "volume": 2_000_000.0})
            self.history[key] = candles
            self.quote_data[key] = {"instrument_token": key, "symbol": symbol, "last_price": current_price,
                                    "timestamp": self.clock.isoformat(), "last_trade_time": str(int(self.clock.timestamp() * 1000)),
                                    "volume": 1_500_000, "lower_circuit_limit": current_price * 0.8,
                                    "upper_circuit_limit": current_price * 1.2,
                                    "ohlc": {"open": current_price - 0.6, "high": current_price + 0.5,
                                             "low": current_price - 0.8, "close": candles[-1]["close"]},
                                    "depth": {"buy": [{"price": current_price - 0.01, "quantity": 5000}],
                                              "sell": [{"price": current_price + 0.01, "quantity": 5000}]}}
            # Deliberately use the real documented response shape. Supplements
            # add publication timestamps/share metadata the API does not expose.
            def statement(fields: list[tuple[str, str]]) -> dict[str, Any]:
                return {"type": "consolidated", "time_period": "yearly", "units_in": "crore",
                        "full_statement": [{"particular": label,
                                             "history": [{"period": dt.date.fromisoformat(row["period"]).strftime("%b %Y"),
                                                          "value": row[field] if field in PER_SHARE_FIELDS else row[field] / CRORE}
                                                         for row in reversed(annual)]}
                                            for field, label in fields]}
            income = statement([(key_, LINE_ALIASES[key_][0]) for key_ in
                                ("revenue", "ebitda", "ebit", "depreciation", "net_profit", "parent_net_profit",
                                 "eps_diluted", "interest_expense", "pbt", "tax")])
            cash = statement([(key_, LINE_ALIASES[key_][0]) for key_ in ("cfo", "capex", "cash_interest")])
            balance = statement([(key_, LINE_ALIASES[key_][0]) for key_ in
                                 ("debt", "cash", "equity", "current_assets", "current_liabilities", "total_assets",
                                  "minority_interest", "preferred_equity", "non_operating_assets")])
            quarter_data = {"units_in": "crore", "type": "consolidated", "time_period": "quarterly",
                            "income_statement": [{"category": field,
                                                   "history": [{"period": dt.date.fromisoformat(row["period"]).strftime("%b %Y"),
                                                                "value": row[field] / CRORE} for row in reversed(quarterly)]}
                                                  for field in ("revenue", "net_profit")]}
            self.bundles[isin] = {"profile": {"sector": "Packaged Foods (DEMO)"}, "balance": balance,
                                  "cash": cash, "annual_income": income, "quarter_income": quarter_data,
                                  "actions": [], "ratios": [
                                      {"name": "P/E", "company_value": current_price / annual[-1]["eps_diluted"], "sector_value": 24.0},
                                      {"name": "P/B", "company_value": current_price * shares / annual[-1]["equity"], "sector_value": 4.0},
                                      {"name": "ROE", "company_value": "24.00%", "sector_value": "18.00%"},
                                      {"name": "ROCE", "company_value": "27.00%", "sector_value": "21.00%"},
                                      {"name": "ROA", "company_value": "14.00%", "sector_value": "11.00%"},
                                      {"name": "Quick Ratio", "company_value": "1.40", "sector_value": "1.10"},
                                      {"name": "EV/EBITDA", "company_value": "12.00", "sector_value": "15.00"},
                                  ], "competitors": [], "errors": [],
                                  "fetched_at": self.clock.isoformat()}
        for isin, bundle in self.bundles.items():
            bundle["competitors"] = [{"instrument_key": row["instrument_key"]} for row in self.raw_instruments
                                      if row.get("isin") and row["isin"] != isin]

    def validate_token(self) -> None:
        return

    def instruments(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return self.raw_instruments, []

    def nifty500(self) -> list[dict[str, str]]:
        return [{"Symbol": row["trading_symbol"], "ISIN Code": row["isin"]} for row in self.raw_instruments if row.get("isin")]

    def quotes(self, keys: list[str], ltp_only: bool = False) -> dict[str, dict[str, Any]]:
        return {key: {**self.quote_data[key], "timestamp": self.clock.isoformat(),
                      "last_trade_time": str(int(self.clock.timestamp() * 1000))}
                for key in keys if key in self.quote_data}

    def candles(self, key: str, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
        return [dict(row) for row in self.history.get(key, []) if start <= parse_date(row["date"]) <= end]

    def fundamental_bundle(self, isin: str, statement_type: str) -> dict[str, Any]:
        return json.loads(json.dumps(self.bundles[isin]))

    def sector(self, isin: str) -> str | None:
        return self.supplements.get(isin, {}).get("sector")

    def market(self) -> dict[str, Any]:
        return {"status": "NORMAL_OPEN", "exchange": "NSE", "demo": True}

    def account(self) -> dict[str, Any]:
        return {"complete": True, "errors": [], "holdings": [], "positions": [], "orders": [], "gtt": [],
                "funds": {"equity": {"available_margin": 1_000_000.0}}, "simulated": True}

    def place_gtt(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise ScreenerError("No live order endpoint exists in the DEMO client")

    def place_limit(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise ScreenerError("No live order endpoint exists in the DEMO client")


# ----------------------------- Console report ----------------------------


def print_details(result: dict[str, Any]) -> None:
    print("\n" + "=" * 108)
    print(f"{result['symbol']} | {result['name']} | ISIN {result['isin']} | {result['sector']}")
    print(f"  Source: {result['source']} | retrieved/as-of: {result['source_as_of']}")
    print(f"  Price INR {fmt(result['price'])} | model fair value ~INR {fmt(result['fair']['value'])}")
    print(f"  Estimated upside {pct(result['upside'])} | discount to fair value {pct(result['discount'])}")
    print(f"  Scenario/model range ~INR {fmt(result['fair']['low'])} - {fmt(result['fair']['high'])}; NOT a statistical confidence interval")
    ctx = result["context"]
    print(f"  Market cap {fmt(ctx['market_cap_crore'])} Cr | custom size tier {ctx['tier']} (not SEBI classification)")
    print(f"  Outstanding shares {fmt(ctx['shares'], 0)} | diluted-share assumption {fmt(ctx['diluted_shares'], 0)}")
    print(f"  EPS {fmt(ctx['current'].get('eps_diluted'))} [{ctx['earnings_basis']['eps_diluted']}] | P/E {fmt(ctx['pe'])} | EV/EBITDA {fmt(ctx['ev_ebitda'])} | P/B {fmt(ctx['pb'])}")
    print("  Share-count basis: " + ctx["share_adjustment_note"])
    table(["Income input", "INR Cr", "Basis", "Period end"],
          [[field, fmt(ctx["current"][field] / CRORE if ctx["current"][field] is not None else None),
            ctx["earnings_basis"][field], ctx["earnings_dates"][field] or "unknown"]
           for field in ("revenue", "ebitda", "ebit", "parent_net_profit", "interest_expense")])
    table(["Equity-bridge / balance input", "INR Cr", "Snapshot date"],
          [[field, fmt(ctx["balance"][field] / CRORE if ctx["balance"][field] is not None else None),
            ctx["balance_dates"][field] or "unknown"]
           for field in ("debt", "cash", "equity", "minority_interest", "preferred_equity", "non_operating_assets")])
    table(["Annual period", "CFO Cr", "Positive CapEx Cr", "CFO-CapEx Cr"],
          [[row["period"], fmt(row.get("cfo") / CRORE if num(row.get("cfo")) is not None else None),
            fmt(row.get("capex") / CRORE if num(row.get("capex")) is not None else None),
            fmt((row["cfo"]-row["capex"]) / CRORE) if num(row.get("cfo")) is not None and num(row.get("capex")) is not None else "N/A"]
           for row in ctx["annual"][-5:]])
    if result.get("provider_ratios"):
        print("  Upstox key ratios (display-only, not substituted for missing accounting data):")
        table(["Ratio", "Company/provider", "Sector/provider"],
              [[row.get("name", "?"), row.get("company_value", "N/A"), row.get("sector_value", "N/A")]
               for row in result["provider_ratios"]])
    table(["Model", "Fair / share", "Low", "High", "Blend weight", "Status"],
          [[name.upper(), fmt(model.get("value")), fmt(model.get("low")), fmt(model.get("high")),
            pct(result["fair"]["weights"].get(name, 0)), model["status"]] for name, model in result["models"].items()])
    for name, model in result["models"].items():
        if model.get("reason"):
            print(textwrap.fill(f"  {name.upper()}: {model['reason']}", width=108, subsequent_indent="    "))
    if result["fair"]["quarantined"]:
        print("  OUTLIER REVIEW REQUIRED: " + ", ".join(result["fair"]["quarantined"]))
    dcf = result["models"]["dcf"]
    if dcf["value"] is not None:
        print(f"  DCF: beta raw/applied {fmt(dcf['beta_raw'], 3)}/{fmt(dcf['beta'], 3)}, {dcf['beta_observations']} weekly observations")
        print(f"       Ke {pct(dcf['cost_of_equity'])}, Kd {pct(dcf['cost_of_debt'])}, WACC raw/applied {pct(dcf['wacc_raw'])}/{pct(dcf['wacc'])}")
        print(f"       FCFF baseline {fmt(dcf['baseline_fcff']/CRORE)} Cr, growth {pct(dcf['growth'])}, terminal growth {pct(dcf['terminal_growth'])}")
        table(["Year", "Growth", "FCFF Cr", "Present value Cr"],
              [[r["year"], pct(r["growth"]), fmt(r["fcff"]/CRORE), fmt(r["present_value"]/CRORE)]
               for r in dcf["scenarios"]["base"]["projections"]])
        print(f"       Terminal contribution: {pct(dcf['scenarios']['base']['terminal_share'])}")
        table(["WACC", "Terminal growth", "DCF sensitivity INR/share"],
              [[pct(r["wacc"]), pct(r["terminal_growth"]), fmt(r["value"])] for r in dcf["sensitivity"]])
    peer = result["models"]["peers"]
    if peer.get("components"):
        table(["Peer component", "Median multiple", "Valid peers", "Implied price"],
              [[k, fmt(v.get("median_multiple")), v.get("count", 0), fmt(v.get("value"))] for k, v in peer["components"].items()])
        for key, component in peer["components"].items():
            if component.get("peer_symbols"):
                print(f"       {key} peers: " + ", ".join(component["peer_symbols"]))
    hist = result["models"]["historical"]
    if hist["value"] is not None:
        print(f"  Historical P/E: 25th/median/75th {fmt(hist['p25_pe'])}/{fmt(hist['median_pe'])}/{fmt(hist['p75_pe'])}; {hist['pe_observations']} observations, {fmt(hist['coverage_years'])} years")
    health = result["health"]
    print("  Raw health growth/margin/return ratios use decimals: 0.15 = 15%.")
    print(f"  CUSTOM health composite {fmt(health['score'])}/5 | fundamental-only quality {fmt(health['quality_score'])}/5")
    print(f"  Ranked within the {health['rank_universe_size']}-stock loaded industry sample, NOT the whole market.")
    for pillar, details in health["details"].items():
        print(f"    {pillar}: {fmt(health['pillars'][pillar])}/5")
        table(["Metric", "Raw value", "Score / 5", "Valid sample", "Direction"],
              [[name, fmt(m["raw"], 4), fmt(m["score"]), m["sample_count"], "higher" if m["higher_better"] else "lower"]
               for name, m in details.items()])
    structure, qc = result["structure"], result["quote_checks"]
    turnover = structure.get("average_turnover")
    print(f"  Turnover proxy {fmt(turnover/CRORE if turnover else None)} Cr/day | structural stop {fmt(structure.get('stop'))}")
    print(f"  Price levels: prior 20-day low {fmt(structure.get('low_20d'))}, today's low {fmt(structure.get('today_low'))}, previous-week high {fmt(structure.get('previous_week_high'))}")
    print(f"  Trailing-year high/low {fmt(structure.get('year_high'))}/{fmt(structure.get('year_low'))} | stop basis: {structure.get('stop_basis')}")
    print(f"  Confirmation: higher completed-day close={structure.get('higher_daily_close')}, week-high reclaim={structure.get('reclaimed_week_high')}")
    print(f"  Quote/trade ages {fmt(qc['quote_age'], 0)}/{fmt(qc['trade_age'], 0)} sec | bid/ask {fmt(qc['bid'])}/{fmt(qc['ask'])} | spread {pct(qc['spread'])}")
    table(["Gate", "Result"], [[gate, "PASS" if passed else "FAIL"] for gate, passed in result["gates"].items()])
    plan = result.get("plan")
    if plan:
        if plan["status"] == "PLANNED":
            print(f"  PLAN: {plan['quantity']} shares, entry {fmt(plan['entry'])}, stop {fmt(plan['stop'])}, target {fmt(plan['target'])}")
            print(f"        Order value INR {fmt(plan['value'])}, planned risk INR {fmt(plan['rupee_risk'])}, reward/risk {fmt(plan['reward_risk'])}")
        else:
            print(textwrap.fill("  ORDER BLOCKED: " + plan["reason"], width=108, subsequent_indent="    "))
    for warning in dict.fromkeys(result["warnings"]):
        print(textwrap.fill("  WARNING: " + warning, width=108, subsequent_indent="    "))


def print_summary(results: list[dict[str, Any]], market: dict[str, Any], clock: dt.datetime,
                  top: int, details: bool) -> None:
    print(f"\nAS-OF {clock.isoformat()} | NSE session: {market.get('status', 'UNKNOWN')}")
    ranked = sorted(results, key=lambda r: r["upside"] if r["upside"] is not None else -math.inf, reverse=True)
    visible = ranked[:top] if top > 0 else ranked
    table(["Symbol", "LTP INR", "Fair INR", "Upside", "Discount", "Health/5", "Quality/5", "Models", "Signal", "Order plan"],
          [[r["symbol"], fmt(r["price"]), fmt(r["fair"]["value"]), pct(r["upside"]), pct(r["discount"]),
            fmt(r["health"]["score"]), fmt(r["health"]["quality_score"]), r["fair"]["count"], r["signal"],
            str(r["plan"]["quantity"]) + " shares" if r.get("plan", {}).get("status") == "PLANNED" else "BLOCKED"] for r in visible])
    if top and len(ranked) > top:
        print(f"Console limited to {top} of {len(ranked)} rows; all rows exported to CSV/JSON.")
    if details:
        for result in visible:
            print_details(result)
    else:
        for result in visible:
            failures = result["failed_gates"]
            if failures:
                print(f"  {result['symbol']} blocked gates: {', '.join(failures)}")
    print("\nUndervalued is a model-dependent research signal, not a proven buy recommendation or return guarantee.")


def export_reports(directory: Path, results: list[dict[str, Any]], clock: dt.datetime,
                   config: dict[str, Any], market: dict[str, Any], demo: bool,
                   input_errors: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory / "latest_screen.json", {"version": VERSION, "demo": demo, "as_of": clock,
                "assumptions": config, "market": market, "input_errors": input_errors,
                "notice": "Custom methodology, not InvestingPro. Range is not a confidence interval.", "results": results})
    fields = ["symbol", "isin", "sector", "price", "fair_value", "fair_low", "fair_high", "upside_pct", "discount_pct",
              "health_score", "quality_score", "dcf", "peers", "historical", "model_count", "signal", "ready",
              "quantity", "entry", "stop", "target", "failed_gates", "data_source", "warnings"]
    temporary = directory / "latest_screen.csv.tmp"
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for result in results:
            plan = result.get("plan") or {}
            row = {key: result.get(key) for key in ("symbol", "isin", "sector", "price", "signal", "ready")}
            row.update({"fair_value": result["fair"]["value"], "fair_low": result["fair"]["low"], "fair_high": result["fair"]["high"],
                        "upside_pct": 100 * result["upside"] if result["upside"] is not None else None,
                        "discount_pct": 100 * result["discount"] if result["discount"] is not None else None,
                        "health_score": result["health"]["score"], "quality_score": result["health"]["quality_score"],
                        "dcf": result["models"]["dcf"]["value"], "peers": result["models"]["peers"]["value"],
                        "historical": result["models"]["historical"]["value"], "model_count": result["fair"]["count"],
                        "failed_gates": ";".join(result["failed_gates"]), "data_source": result["source"],
                        "warnings": ";".join(dict.fromkeys(result["warnings"]))})
            row.update({key: plan.get(key) for key in ("quantity", "entry", "stop", "target")})
            writer.writerow(row)
    os.replace(temporary, directory / "latest_screen.csv")


# -------------------------- Full pipeline runner -------------------------

def listing_scope(record: dict[str, Any]) -> tuple[str, str, str] | None:
    """An exchange/series/security identity, NOT just an ISIN/instrument_key.

    The suspended file contains alternate series (BE/BL/AF/etc.) with the SAME
    ISIN and often the SAME instrument_key as an active EQ listing.
    """
    parts = [record.get("segment"), record.get("instrument_type"), record.get("instrument_key")]
    if any(value is None or not str(value).strip() for value in parts):
        return None
    return tuple(str(value).strip().upper() for value in parts)


def listing_token(record: dict[str, Any]) -> str | None:
    value = record.get("exchange_token")
    if value is None or isinstance(value, bool) or not str(value).strip():
        return None
    text = str(value).strip()
    if text.isdigit():
        return str(int(text))
    numeric = num(value)
    if numeric is not None and numeric >= 0 and numeric.is_integer():
        return str(int(numeric))
    return text.upper()


class SuspensionIndex:
    """Exclude specific listings while retaining genuine suspension checks."""
    def __init__(self, records: list[dict[str, Any]]):
        self.exact: set[tuple[str, str, str, str]] = set()
        self.tokenless: set[tuple[str, str, str]] = set()
        self.scopes: set[tuple[str, str, str]] = set()
        self.incomplete_scope_count = 0
        self.alternate_series_count = 0
        for record in records:
            if not isinstance(record, dict):
                raise DataError("Suspended instrument file contains a non-object record; use --diagnose-instruments")
            scope = listing_scope(record)
            if scope is None:
                self.incomplete_scope_count += 1
                continue
            self.scopes.add(scope)
            if scope[0] == "NSE_EQ" and scope[1] != "EQ":
                self.alternate_series_count += 1
            token = listing_token(record)
            if token is None:
                # Fail conservatively ONLY for the same exchange/series/key.
                # Never let missing token data blacklist all series of an ISIN.
                self.tokenless.add(scope)
            else:
                self.exact.add((*scope, token))

    def contains(self, record: dict[str, Any]) -> bool:
        scope = listing_scope(record)
        if scope is None:
            return False
        token = listing_token(record)
        if scope in self.tokenless:
            return True
        if token is None:
            return scope in self.scopes
        return (*scope, token) in self.exact


def build_equity_universe(master: Any, suspended: Any, demo: bool = False) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    if not isinstance(master, list) or not isinstance(suspended, list):
        raise DataError("Expected JSON arrays for BOD and suspended instruments; use --diagnose-instruments")
    suspension = SuspensionIndex(suspended)
    counts = {"master_records": len(master), "suspended_records": len(suspended),
              "nse_eq_records": 0, "regular_eq_records": 0, "normal_company_candidates": 0,
              "excluded_exact_suspensions": 0, "excluded_security_type": 0,
              "excluded_non_company_isin": 0, "malformed_candidates": 0,
              "eligible_equities": 0, "duplicate_records": 0,
              "inactive_alternate_series_records": suspension.alternate_series_count,
              "incomplete_suspension_scopes": suspension.incomplete_scope_count}
    out = {}
    for raw in master:
        if not isinstance(raw, dict):
            raise DataError("Instrument master contains a non-object record; use --diagnose-instruments")
        if str(raw.get("segment", "")).strip().upper() != "NSE_EQ":
            continue
        counts["nse_eq_records"] += 1
        if str(raw.get("instrument_type", "")).strip().upper() != "EQ":
            continue
        counts["regular_eq_records"] += 1
        if str(raw.get("security_type", "NORMAL")).strip().upper() != "NORMAL":
            counts["excluded_security_type"] += 1
            continue
        isin = str(raw.get("isin") or "").strip().upper()
        symbol = str(raw.get("trading_symbol") or "").strip().upper()
        key = str(raw.get("instrument_key") or "").strip()
        if not isin or not symbol or not key:
            counts["malformed_candidates"] += 1
            continue
        if not demo and not isin.startswith("INE"):
            counts["excluded_non_company_isin"] += 1
            continue
        counts["normal_company_candidates"] += 1
        if suspension.contains(raw):
            counts["excluded_exact_suspensions"] += 1
            continue
        record = {**raw, "isin": isin, "trading_symbol": symbol,
                  "segment": "NSE_EQ", "instrument_type": "EQ", "security_type": "NORMAL"}
        if isin in out:
            counts["duplicate_records"] += 1
            if (listing_scope(out[isin]), listing_token(out[isin])) != (listing_scope(record), listing_token(record)):
                raise DataError(f"Conflicting regular EQ listings for {isin}; inspect --diagnose-instruments rather than silently pick one")
            continue
        out[isin] = record
    counts["eligible_equities"] = len(out)
    return out, counts


def print_instrument_counts(counts: dict[str, int]) -> None:
    print(f"  Instrument audit: BOD={counts['master_records']:,}, NSE_EQ={counts['nse_eq_records']:,}, regular EQ={counts['regular_eq_records']:,}")
    print(f"  Eligible normal company equities={counts['eligible_equities']:,}; specific listing suspensions excluded={counts['excluded_exact_suspensions']:,}")
    print(f"  Inactive alternate-series records={counts['inactive_alternate_series_records']:,}; these do NOT suspend an active EQ listing with the same ISIN/key")
    if counts["incomplete_suspension_scopes"]:
        print(f"  WARNING: {counts['incomplete_suspension_scopes']} suspension records have incomplete series identity; no ISIN-wide blacklist inferred")


def diagnose_instruments(client: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Public downloads ONLY. No token validation, account calls or mutations."""
    print("UPSTOX INSTRUMENT DIAGNOSTIC " + VERSION)
    print("Public instrument files only; no credentials, account requests or broker orders.")
    master, suspended = client.instruments()
    eligible, counts = build_equity_universe(master, suspended, demo=client.is_demo)
    print_instrument_counts(counts)
    requested = args.symbols or DEFAULT_SYMBOLS
    symbols = list(dict.fromkeys(s.strip().upper() for s in requested.split(",") if s.strip()))
    by_symbol = {record["trading_symbol"]: record for record in eligible.values()}
    suspension = SuspensionIndex(suspended)
    rows = []
    for symbol in symbols:
        active = by_symbol.get(symbol)
        if active:
            rows.append({"symbol": symbol, "status": "FOUND", "segment": active["segment"],
                         "series": active["instrument_type"], "exchange_token": listing_token(active),
                         "instrument_key": active["instrument_key"]})
            continue
        candidates = [r for r in master if isinstance(r, dict)
                      and str(r.get("trading_symbol", "")).strip().upper() == symbol
                      and str(r.get("segment", "")).strip().upper() == "NSE_EQ"]
        eq = next((r for r in candidates if str(r.get("instrument_type", "")).strip().upper() == "EQ"), None)
        if eq and suspension.contains(eq):
            reason = "EXCLUDED: specific EQ listing suspended"
        elif eq and str(eq.get("security_type", "NORMAL")).strip().upper() != "NORMAL":
            reason = "EXCLUDED: security type " + str(eq.get("security_type"))
        elif eq:
            reason = "EXCLUDED: company ISIN/record validation"
        elif candidates:
            reason = "NOT REGULAR EQ: " + ",".join(sorted({str(r.get("instrument_type")) for r in candidates}))
        else:
            reason = "NOT IN NSE BOD"
        record = eq or (candidates[0] if candidates else {})
        rows.append({"symbol": symbol, "status": reason, "segment": record.get("segment"),
                     "series": record.get("instrument_type"), "exchange_token": listing_token(record),
                     "instrument_key": record.get("instrument_key")})
    table(["Symbol", "Status", "Segment", "Series", "Exchange token", "Instrument key"],
          [[r["symbol"], r["status"], r["segment"] or "N/A", r["series"] or "N/A",
            r["exchange_token"] or "N/A", r["instrument_key"] or "N/A"] for r in rows])
    report = {"version": VERSION, "as_of": now_ist().isoformat(), "public_data_only": True,
              "counts": counts, "requested_symbols": rows}
    path = Path(args.output) / "instruments_diagnostic.json"
    atomic_json(path, report)
    print("Diagnostic saved to " + str(path))
    return report


class Screener:
    def __init__(self, args: argparse.Namespace, env: dict[str, str], config: dict[str, Any],
                 state: State, client: Any):
        self.args, self.env, self.config, self.state, self.client = args, env, config, state, client
        self.instruments: dict[str, dict[str, Any]] = {}
        self.companies: dict[str, dict[str, Any]] = {}
        self.history: dict[str, list[dict[str, Any]]] = {}
        self.adjusted: dict[str, list[dict[str, Any]]] = {}
        self.benchmark: list[dict[str, Any]] = []
        self.targets: list[str] = []
        self.input_errors: list[str] = []
        self.sector_memo: dict[str, str | None] = {}
        self.last_load: dt.datetime | None = None
        self.last_scheduled_date: dt.date | None = None
        self.fundamental_mtime: float | None = None
        self.paper: PaperAccount | None = None
        self.results: dict[str, dict[str, Any]] = {}
        self.capital = args.capital or 1_000_000.0
        self.report_dir = Path(args.output) / ("demo" if args.demo else "market")

    def say(self, text: str) -> None:
        if not self.args.quiet:
            print(text, flush=True)

    def clock(self) -> dt.datetime:
        return self.client.clock if self.client.is_demo else now_ist()

    def load(self) -> None:
        clock = self.clock()
        self.say("\nPHASE 1/5 -- authentication, active EQ listings, company financials and " +
                 ("recent API candles" if getattr(self.args, "analysis_mode", None) == "upstox-only" else "five-year candles"))
        if hasattr(self.client, "fetch_competitors"):
            self.client.fetch_competitors = getattr(self.args, "analysis_mode", None) != "upstox-only" or self.args.with_peers
        self.client.validate_token()
        self.say("  Authentication: OFFLINE DEMO, no credentials/network used" if self.client.is_demo else "  Read-only token validation successful; credentials never printed")
        master, suspended = self.client.instruments()
        self.instruments, instrument_counts = build_equity_universe(master, suspended, demo=self.client.is_demo)
        if not self.args.quiet:
            print_instrument_counts(instrument_counts)
        symbols = {item["trading_symbol"]: isin for isin, item in self.instruments.items()}
        if self.args.demo and not self.args.symbols:
            self.targets = list(self.instruments)
        elif self.args.universe == "nse":
            self.targets = list(self.instruments)
        elif self.args.universe == "nifty500":
            self.targets = []
            for item in self.client.nifty500():
                isin = item.get("ISIN Code") or item.get("ISIN")
                symbol = item.get("Symbol") or item.get("symbol")
                if isin in self.instruments:
                    self.targets.append(isin)
                elif symbol in symbols:
                    self.targets.append(symbols[symbol])
                    self.input_errors.append(f"{symbol}: index ISIN mismatch; used current master ISIN, check corporate actions")
                else:
                    self.input_errors.append(f"Index constituent unavailable/suspended in Upstox EQ master: {symbol}")
        else:
            requested = self.args.symbols or DEFAULT_SYMBOLS
            requested_symbols = [s.strip().upper() for s in requested.split(",") if s.strip()]
            self.targets = []
            for symbol in requested_symbols:
                if symbol in symbols:
                    self.targets.append(symbols[symbol])
                else:
                    self.input_errors.append(f"Unknown/non-normal/suspended NSE EQ symbol: {symbol}")
        self.targets = list(dict.fromkeys(self.targets))
        if self.args.limit > 0:
            self.targets = self.targets[:self.args.limit]
            self.say(f"  Explicit --limit applied: {len(self.targets)} targets")
        if not self.targets:
            raise DataError(f"No matching active normal NSE equities ({instrument_counts['eligible_equities']} eligible in downloaded BOD). "
                            + "; ".join(self.input_errors) + ". Run --diagnose-instruments --symbols <your symbols> for listing-level reasons")
        supplements = self.client.supplements if self.client.is_demo else read_supplements(Path(self.args.fundamentals) if self.args.fundamentals else None)
        if self.args.fundamentals:
            self.fundamental_mtime = Path(self.args.fundamentals).stat().st_mtime
        self.companies, self.history, self.adjusted = {}, {}, {}
        keys_to_isin = {item["instrument_key"]: isin for isin, item in self.instruments.items()}
        def load_company(isin: str) -> None:
            inst = self.instruments[isin]
            if self.args.fundamentals_only and not self.client.is_demo:
                bundle = {"errors": [], "fetched_at": clock.isoformat()}
                if isin not in supplements:
                    bundle["errors"].append("No local ISIN-matched fundamental record")
            else:
                bundle = self.client.fundamental_bundle(isin, self.args.statement_type)
            if self.args.dump_upstox:
                name = re.sub(r"[^A-Za-z0-9_-]", "_", inst["trading_symbol"]) + ".json"
                atomic_json(Path(self.args.dump_upstox) / name,
                            {"version": VERSION, "company_financials_only": True,
                             "symbol": inst["trading_symbol"], "isin": isin,
                             "instrument_key": inst["instrument_key"],
                             "retrieval_notice": "fetched_at is client retrieval, possibly cached; not benchmark as-of/publication time",
                             "responses": sanitize_company_dump(bundle)})
            company = normalize_bundle(inst, bundle, self.config, self.args.statement_type)
            if self.args.fundamentals_only and not self.client.is_demo:
                company["source"] = "Local normalized fundamentals"
            company = merge_supplement(company, supplements.get(isin), self.args.statement_type)
            self.companies[isin] = company
        for number, isin in enumerate(self.targets, 1):
            self.say(f"  Fundamentals target {number}/{len(self.targets)}: {self.instruments[isin]['trading_symbol']}")
            load_company(isin)
        added = []
        expand_peers = not self.args.no_peers and (getattr(self.args, "analysis_mode", None) != "upstox-only" or self.args.with_peers)
        if expand_peers:
            for isin in self.targets:
                for key in self.companies[isin]["competitor_keys"][:self.args.max_peers]:
                    peer = keys_to_isin.get(key)
                    if peer and peer not in self.companies and peer not in added:
                        added.append(peer)
            for number, isin in enumerate(added, 1):
                self.say(f"  One-hop competitor {number}/{len(added)}: {self.instruments[isin]['trading_symbol']}")
                load_company(isin)
        self.say(f"  {len(self.targets)} targets + {len(added)} background peers; upstream financial gaps stay visible")
        comparison_only = getattr(self.args, "analysis_mode", None) == "upstox-only"
        start = clock.date() - dt.timedelta(days=400 if comparison_only else 5 * 366)
        end = clock.date()
        self.benchmark = []
        if not comparison_only:
            index_items = [item for item in master if item.get("segment") == "NSE_INDEX" and item.get("name") == BENCHMARK_NAME]
            if not index_items:
                raise DataError("Nifty 50 benchmark not found in instrument master; no guessed instrument key")
            self.benchmark = self.client.candles(index_items[0]["instrument_key"], start, end)
            if self.config["benchmark_price_basis"] == "unverified":
                self.benchmark = []
                self.say("  Benchmark price basis unverified: observed betas disabled; reviewed overrides remain available")
        else:
            self.say("  Comparison mode: recent API candles only; no beta/benchmark or historical-valuation requests")
        for number, (isin, company) in enumerate(self.companies.items(), 1):
            self.say(f"  Candles {number}/{len(self.companies)}: {company['symbol']}")
            try:
                candles = self.client.candles(company["instrument_key"], start, end)
            except AuthenticationError:
                raise
            except ScreenerError as exc:
                candles = []
                company["warnings"].append("Historical API: " + str(exc))
            self.history[isin] = candles
            adjusted, error = adjusted_candles(company, candles, clock)
            self.adjusted[isin] = adjusted
            if error and getattr(self.args, "analysis_mode", None) != "upstox-only":
                company["warnings"].append(error)
            if not company.get("point_in_time_verified") and getattr(self.args, "analysis_mode", None) != "upstox-only":
                company["warnings"].append("Publication-dated/PIT financial history unavailable; historical self-valuation disabled, not estimated using today's EPS")
        if self.args.export_fundamentals:
            atomic_json(Path(self.args.export_fundamentals), {"schema_version": 1, "currency": "INR", "unit": "rupees",
                                                            "companies": list(self.companies.values())})
        self.last_load = clock
        if clock.time() >= dt.time.fromisoformat(self.args.schedule):
            self.last_scheduled_date = clock.date()

    def cycle_upstox_only(self, display: bool = True) -> dict[str, dict[str, Any]]:
        # Defense in depth for API/programmatic callers as well as the CLI.
        if self.args.execute or self.args.paper_orders:
            raise DataError("Orders are disabled in Upstox-only comparison mode")
        clock = self.clock()
        self.say("\nPHASE 2/5 -- diluted EPS x sector P/E reference (illustrative, NOT intrinsic fair value)")
        keys = [c["instrument_key"] for c in self.companies.values()]
        quotes = self.client.quotes(keys)
        results = {isin: relative_stock(company, quotes.get(company["instrument_key"], {}),
                                        self.history.get(isin, []), self.adjusted.get(isin, []), clock, self.config)
                   for isin, company in self.companies.items()}
        for r in results.values():
            r["demo"] = self.args.demo
        self.say("PHASE 3/5 -- reported ROE/ROCE/ROA, liquidity ratios, matched FY revenue/CFO trends; no complete health score")
        self.say("PHASE 4/5 -- live ratio comparisons and indicative price/turnover diagnostics; adjustment limits disclosed")
        try:
            market = self.client.market()
        except AuthenticationError:
            raise
        except ScreenerError as exc:
            market = {"status": "UNKNOWN", "error": str(exc)}
        self.say("PHASE 5/5 -- read-only export; trade execution and hypothetical portfolio sizing DISABLED")
        if self.args.show_account:
            account = self.client.account()
            margin = ((account.get("funds") or {}).get("equity") or {}).get("available_margin")
            self.say(f"  OPTIONAL account read: reported available margin INR {fmt(margin)} (not unencumbered strategy cash)")
            self.say(f"  Holdings rows {len(account.get('holdings') or [])}; positions rows {len(account.get('positions') or [])}; account complete {account.get('complete', False)}")
            for error in account.get("errors", []):
                self.say("  ACCOUNT WARNING: " + error)
        else:
            self.say("  Portfolio/funds/orders/GTT endpoints not requested. No simulated INR 1,000,000 balance.")
        if self.args.capital is not None or self.args.cash_budget is not None:
            self.say("  --capital/--cash-budget have no sizing effect in comparison-only mode.")
        shown = list(results.values()) if self.args.show_peers else [results[isin] for isin in self.targets]
        self.report_dir.mkdir(parents=True, exist_ok=True)
        for r in shown:
            key = ("DEMO:" if self.args.demo else "MARKET:") + "UPSTOX_ONLY:" + r["isin"]
            if self.state.new_alert(key, r["signal"]):
                event = {"time": clock.isoformat(), "mode": "upstox-only", "demo": self.args.demo,
                         "symbol": r["symbol"], "isin": r["isin"], "comparison": r["signal"],
                         "price": r["price"], "sector_pe_reference": r["sector_pe_reference"],
                         "reference_gap": r["reference_gap"], "orders_allowed": False,
                         "notice": "Relative comparison transition, NOT a trading signal"}
                with (self.report_dir / "comparison_alerts.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(clean_json(event), allow_nan=False) + "\n")
        self.results = results
        if display and not self.args.quiet:
            print_relative_summary(shown, market, clock, self.args.top, not self.args.summary_only)
            for error in self.input_errors:
                print("INPUT WARNING: " + error)
        export_relative_reports(self.report_dir, shown, clock, market, self.args.demo, self.input_errors)
        self.say(f"\n  Relative reports: {self.report_dir / 'latest_screen.csv'} and latest_screen.json")
        self.say(f"  Comparison transitions (not orders): {self.report_dir / 'comparison_alerts.jsonl'}")
        if self.args.dump_upstox:
            self.say(f"  Company-only raw financial responses: {Path(self.args.dump_upstox)} (no credentials/account data)")
        return results

    def cycle(self, display: bool = True) -> dict[str, dict[str, Any]]:
        if getattr(self.args, "analysis_mode", None) == "upstox-only":
            return self.cycle_upstox_only(display)
        clock = self.clock()
        self.say("\nPHASE 2/5 -- FCFF DCF, suitable peers, point-in-time historical bands, explicit blend")
        keys = [company["instrument_key"] for company in self.companies.values()]
        # Use full snapshots for actual action gates. LTP-only snapshots have no
        # freshness/depth/circuit metadata and must not authorize orders.
        quotes = self.client.quotes(keys)
        contexts = {isin: financial_context(company, quotes.get(company["instrument_key"], {}), clock, self.config)
                    for isin, company in self.companies.items()}
        models = {}
        for isin, company in self.companies.items():
            contexts[isin]["raw_metrics_warning"] = "Accounting quantities in INR; market tiers and thresholds are custom heuristics"
            models[isin] = {
                "dcf": dcf_model(company, contexts[isin], self.adjusted[isin], self.benchmark, clock, self.config),
                "peers": peers_model(company, contexts[isin], contexts, self.companies, self.config),
                "historical": historical_model(company, contexts[isin], self.history[isin], clock, self.config),
            }
        self.say("PHASE 3/5 -- five custom sector-relative pillars + separate fundamental quality score")
        metrics = {isin: health_metrics(company, contexts[isin], self.adjusted[isin], clock) for isin, company in self.companies.items()}
        scores = health_scores(self.companies, metrics, self.config)
        self.say("PHASE 4/5 -- live upside/discount, cash-flow quality, liquidity, circuits, price structure, provenance gates")
        try:
            market = self.client.market()
        except AuthenticationError:
            raise
        except ScreenerError as exc:
            market = {"status": "UNKNOWN", "error": str(exc)}
        results = {isin: evaluate_stock(company, self.instruments[isin], contexts[isin], models[isin], scores[isin],
                                        quotes.get(company["instrument_key"], {}), self.adjusted[isin], market, clock, self.config)
                   for isin, company in self.companies.items()}
        self.say("PHASE 5/5 -- broker holdings/positions/orders/GTT/funds, sizing, safe order preview and exit checks")
        broker_account = self.client.account()
        broker_view = portfolio_view(broker_account, self.companies, self.client, self.sector_memo)
        if not broker_view["complete"]:
            self.say("  Broker account incomplete: live orders blocked. " + "; ".join(broker_view["errors"]))
        else:
            self.say(f"  Broker available margin INR {fmt(broker_view['available_margin'])}; NOT automatically assumed to be unencumbered cash")
        if not self.args.demo and self.state.intents("live"):
            reconcile_live(self.client, self.state, broker_account, quiet=self.args.quiet)
        # Read-only --cash-budget previews use REAL broker exposures. Without
        # a cash budget, the preview is explicitly a separate paper scenario.
        use_paper_risk = self.args.paper_orders or self.args.demo or (not self.args.execute and self.args.cash_budget is None)
        if use_paper_risk:
            if self.paper is None:
                self.paper = PaperAccount(self.state, self.capital, self.args.demo)
            closes = self.paper.monitor(results, self.config) if self.args.paper_orders else []
            for event in closes:
                self.say(f"  PAPER EXIT {event['symbol']}: {event['reason']}, {event['quantity']} shares at {fmt(event['fill_price'])}")
                for intent in self.state.intents("demo-paper" if self.args.demo else "paper"):
                    if intent["symbol"] == event["symbol"] and intent["status"] == "PAPER_FILLED":
                        self.state.set_intent(intent["id"], "PAPER_CLOSED", {**intent["response"], "exit": event})
            view = portfolio_view(self.paper.snapshot(results), self.companies, self.client, self.sector_memo)
            spendable = min(self.paper.data["cash"], self.args.cash_budget) if self.args.cash_budget else self.paper.data["cash"]
            self.say(f"  {'PAPER' if self.args.paper_orders else 'PREVIEW'} strategy capital INR {fmt(self.capital)}, simulated cash INR {fmt(spendable)} (separate from broker account)")
        else:
            view = broker_view
            self.say("  Using real broker exposures and an explicit user cash cap for sizing; writes still disabled in preview.")
            spendable = max(0.0, min(args_cash, view["available_margin"]) - view.get("pending_gtt_cash", 0.0)) if (args_cash := self.args.cash_budget) is not None and view["available_margin"] is not None else None
        candidates = sorted((isin for isin in self.targets), key=lambda isin: results[isin]["upside"] if results[isin]["upside"] is not None else -math.inf, reverse=True)
        for isin in candidates:
            result = results[isin]
            if not use_paper_risk and live_pending_for_key(self.state, result["instrument_key"]):
                plan = {"status": "BLOCKED", "reason": "Managed live intent exists; reconcile it before another entry", "quantity": 0}
            elif use_paper_risk and self.paper and isin in self.paper.data["positions"]:
                plan = {"status": "BLOCKED", "reason": "Existing paper position; no automatic averaging", "quantity": 0}
            else:
                plan = plan_trade(result, self.instruments[isin], view, self.capital, spendable, self.config)
            result["plan"] = plan
            if plan["status"] == "PLANNED":
                if self.args.execute:
                    execute_entry(self.client, self.state, self.args, self.env, result, plan, self.instruments[isin],
                                  self.companies, self.sector_memo, self.adjusted[isin], self.config)
                elif self.args.paper_orders:
                    mode = "demo-paper" if self.args.demo else "paper"
                    fingerprint = mode + "|BUY|" + isin + "|" + str(clock.date())
                    ident = self.state.reserve_intent(fingerprint, mode, result["instrument_key"], result["symbol"], "BUY", gtt_payload(plan))
                    if ident is not None:
                        fill = self.paper.buy(plan, self.config["cash_cost_buffer"])
                        self.state.set_intent(ident, "PAPER_FILLED", {"fill": fill, "plan": plan})
                        self.say(f"  PAPER BUY {plan['symbol']}: {plan['quantity']} shares at {fmt(plan['entry'])}; zero broker writes")
                    else:
                        self.say(f"  PAPER duplicate daily intent blocked: {result['symbol']}")
                        result["plan"] = {"status": "BLOCKED", "reason": "Duplicate paper daily intent", "quantity": 0}
                        continue
                reserve_preview(view, plan)
                spendable = max(0, spendable - plan["cash_reservation"]) if spendable is not None else None
            signal_key = ("DEMO:" if self.args.demo else "MARKET:") + isin
            alert_signal = result["signal"] + ("|READY" if result["ready"] else "|RESEARCH_ONLY")
            if self.state.new_alert(signal_key, alert_signal):
                event = {"time": clock.isoformat(), "symbol": result["symbol"], "isin": isin, "signal": alert_signal,
                         "price": result["price"], "fair_value": result["fair"]["value"], "upside": result["upside"],
                         "discount": result["discount"], "health": result["health"]["score"], "demo": self.args.demo}
                self.report_dir.mkdir(parents=True, exist_ok=True)
                with (self.report_dir / "alerts.jsonl").open("a", encoding="utf-8") as file:
                    file.write(json.dumps(clean_json(event), allow_nan=False) + "\n")
                self.say(f"  ALERT {result['symbol']}: {alert_signal}, upside {pct(result['upside'])}")
        exits = health_exit_plans(results, broker_account, self.config)
        if self.args.execute and self.args.entry_method == "limit":
            exits += managed_limit_exit_plans(results, self.state, broker_account)
        for plan in exits:
            self.say(f"  EXIT REVIEW {plan['symbol']}: {plan['reason']}; health {fmt(plan.get('health'))}/5; sellable quantity {plan['quantity']}")
            if self.args.execute:
                execute_health_exit(self.client, self.state, self.args, self.env, plan, self.config)
        self.results = results
        shown = list(results.values()) if self.args.show_peers else [results[isin] for isin in self.targets]
        for result in shown:
            if result["plan"] is None:
                result["plan"] = {"status": "BACKGROUND_PEER", "reason": "Used for comparison only", "quantity": 0}
        if display and not self.args.quiet:
            print_summary(shown, market, clock, self.args.top, not self.args.summary_only)
            for error in self.input_errors:
                print("INPUT WARNING: " + error)
        export_reports(self.report_dir, shown, clock, self.config, market, self.args.demo, self.input_errors)
        self.say(f"\n  Reports: {self.report_dir / 'latest_screen.csv'} and latest_screen.json; transitions in alerts.jsonl")
        return results

    def run(self) -> None:
        comparison_only = getattr(self.args, "analysis_mode", None) == "upstox-only"
        self.say(("UPSTOX-ONLY COMPARISON SCREENER " if comparison_only else "UPSTOX FAIR-VALUE CONSOLE SCREENER ") + VERSION)
        self.say("Synthetic fixture mode: NO real company prices/financials and NO broker writes" if self.args.demo else
                 "Read-only relative comparisons; no intrinsic-value, complete-health-score or order claims" if comparison_only else
                 "Full-model research mode; complete financial inputs/verification required")
        if not comparison_only:
            self.say(f"ASSUMPTIONS (NOT live rates): Rf={pct(self.config['risk_free_rate'])}, ERP={pct(self.config['equity_risk_premium'])}, terminal g={pct(self.config['terminal_growth'])}, assumption date={self.config.get('assumptions_as_of') or 'not supplied'}")
        self.say("All financial totals normalized to rupees; console statement totals displayed in crore.")
        self.load()
        cycles = 0
        while True:
            if comparison_only:
                self.cycle_upstox_only()
            else:
                self.cycle()
            cycles += 1
            if not self.args.watch or (self.args.cycles > 0 and cycles >= self.args.cycles):
                break
            self.say(f"\nWatching; next {'read-only comparison' if comparison_only else 'price/portfolio'} scan in {self.args.interval} seconds. Ctrl+C stops the watcher; no broker cancellations.")
            time.sleep(self.args.interval)
            if self.client.is_demo:
                self.client.clock += dt.timedelta(seconds=self.args.interval)
            else:
                updated_env = load_env(Path(self.args.env))
                validate_live_flags(self.args, updated_env)
                self.env = updated_env
                self.client.http.env = updated_env
                self.client.http.redact = Redactor(updated_env)
            clock = self.clock()
            changed = self.args.fundamentals and Path(self.args.fundamentals).stat().st_mtime != self.fundamental_mtime
            scheduled = clock.date() != self.last_scheduled_date and clock.time() >= dt.time.fromisoformat(self.args.schedule)
            due = (clock - self.last_load).total_seconds() >= self.config["slow_refresh_hours"] * 3600
            if changed or scheduled or due:
                self.say("Slow-layer refresh: new day, six-hour interval or changed local filing data")
                self.load()


# -------------------------- Setup / OAuth / CLI --------------------------


def fundamental_template() -> dict[str, Any]:
    return {"schema_version": 1, "currency": "INR", "unit": "rupees",
            "notes": ["All monetary totals in rupees (1 crore = 10000000 rupees); EPS/BVPS in rupees/share; shares in actual units.",
                      "Null means unknown, NOT zero. Enter verified zero explicitly for absent debt/minority/preferred/nonoperating assets.",
                      "Filing publication timestamps, price basis and corporate-action factors must be independently checked.",
                      "Set review/verification flags TRUE only after reviewing source filings, units, share basis, governance and splits."],
            "companies": [{"isin": "INE154A01025", "symbol": "ITC", "sector": None, "industry": None,
                           "source": "REPLACE WITH audited consolidated filing/source reference", "source_as_of": None,
                           "accounting_basis": "consolidated", "shares_outstanding": None, "diluted_shares": None,
                           "shares_as_of": None, "debt_includes_leases": None, "cfo_interest_classification": "unknown",
                           "history_price_basis": "unverified", "eps_basis": "unverified",
                           "actions_coverage_verified": False, "point_in_time_verified": False,
                           "manual_review_complete": False, "last_reviewed_financial_period": None,
                           "manual_review_as_of": None, "tick_verified": False, "tick_size_rupees": None,
                           "beta_override": None, "cost_of_debt_override": None, "growth_override": None,
                           "actions": [], "historical_snapshots": [],
                           "annual": [{"period": "2026-03-31", "available_on": None, "share_basis_date": "2026-03-31",
                                       "revenue": None, "ebitda": None, "ebit": None, "depreciation": None,
                                       "parent_net_profit": None, "net_profit": None, "eps_diluted": None,
                                       "cfo": None, "capex": None, "cash_interest": None, "interest_expense": None,
                                       "tax_rate": None, "change_operating_nwc": None, "debt": None,
                                       "cash": None, "equity": None, "minority_interest": None,
                                       "preferred_equity": None, "non_operating_assets": None,
                                       "current_assets": None, "current_liabilities": None}],
                           "quarterly": []}]}


def initialize(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        "screener_config.json": DEFAULT_CONFIG,
        "fundamentals_template.json": fundamental_template(),
    }
    for name, data in files.items():
        path = directory / name
        if path.exists():
            print(f"Kept existing {path}")
        else:
            atomic_json(path, data)
            print(f"Created {path}")
    env_path = directory / "env.txt"
    if not env_path.exists():
        env_path.write_text("# Fill these LOCALLY; never paste real tokens into chat.\nUPSTOX_ACCESS_TOKEN=\nUPSTOX_API_TOKEN=\nUPSTOX_API_SECRET=\nUPSTOX_REDIRECT_URI=\n# Optional persistent read-only token\nUPSTOX_ANALYTICS_TOKEN=\n# Explicit live mode gates (default NO)\nUPSTOX_LIVE_TRADING_ENABLED=NO\nUPSTOX_REGISTERED_APP_ACK=NO\nUPSTOX_SELL_AUTHORIZED=NO\nUPSTOX_MANAGED_EXITS_ACK=NO\nUPSTOX_ALGO_NAME=\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(env_path, 0o600)
        print("Created env.txt with EMPTY secrets; do not share it.")
    else:
        print("Kept existing env.txt; no credentials overwritten.")


def oauth_login(env_path: Path, env: dict[str, str], http: Http) -> None:
    client_id = env.get("UPSTOX_API_KEY") or env.get("UPSTOX_API_TOKEN", "")
    secret = env.get("UPSTOX_API_SECRET", "")
    redirect = env.get("UPSTOX_REDIRECT_URI", "")
    if len(client_id) < 10 or len(secret) < 10 or not redirect:
        raise AuthenticationError("--login needs complete API key (UPSTOX_API_TOKEN or UPSTOX_API_KEY), API secret, and exactly registered UPSTOX_REDIRECT_URI")
    state = secrets.token_urlsafe(24)
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect, "state": state}
    url = API + "/v2/login/authorization/dialog?" + urllib.parse.urlencode(params)
    print("Open this authorization URL in your own browser (API client ID is public, secret is not):\n" + url)
    print("After logging in at Upstox, paste the FULL redirected URL here. Do not paste your account password/OTP.")
    if not sys.stdin.isatty():
        raise AuthenticationError("Interactive browser authorization requires your local terminal")
    redirect_url = input("Redirected URL: ").strip()
    parsed, registered = urllib.parse.urlparse(redirect_url), urllib.parse.urlparse(redirect)
    if (parsed.scheme, parsed.netloc, parsed.path) != (registered.scheme, registered.netloc, registered.path):
        raise AuthenticationError("Redirect does not match the registered URI")
    received = urllib.parse.parse_qs(parsed.query)
    if received.get("state", [None])[0] != state:
        raise AuthenticationError("OAuth state mismatch; authorization aborted")
    code = received.get("code", [None])[0]
    if not code:
        raise AuthenticationError("No single-use authorization code in callback")
    response = http.request("POST", API + "/v2/login/authorization/token", auth=False, form=True,
                            payload={"code": code, "client_id": client_id, "client_secret": secret,
                                     "redirect_uri": redirect, "grant_type": "authorization_code"})
    token = response.get("access_token")
    if not token:
        raise AuthenticationError("Authorization did not return an access token. No automatic retry of a single-use code")
    save_env_token(env_path, token)
    print("Access token saved privately to env.txt; its value was not printed.")


def validate_config(config: dict[str, Any]) -> None:
    for field in ("risk_free_rate", "equity_risk_premium", "tax_rate_assumption", "terminal_growth"):
        value = num(config.get(field))
        if value is None or not 0 <= value < 0.6:
            raise DataError(f"Invalid {field}; use decimal fractions, not percentage integers")
    if not isinstance(config.get("projection_years"), int) or not 1 <= config["projection_years"] <= 10:
        raise DataError("projection_years must be between 1 and 10")
    for field in ("beta_bounds", "wacc_bounds", "growth_bounds"):
        bounds = config.get(field)
        if bounds is not None and (not isinstance(bounds, list) or len(bounds) != 2 or any(num(v) is None for v in bounds) or bounds[0] > bounds[1]):
            raise DataError(f"Invalid {field}; two numbers or null required")
    for field in ("model_weights", "peer_multiple_weights"):
        weights = config.get(field)
        if not isinstance(weights, dict) or not weights or any(num(v) is None or v < 0 for v in weights.values()) or sum(weights.values()) <= 0:
            raise DataError(f"Invalid nonnegative {field}")
    if set(config["model_weights"]) - {"dcf", "peers", "historical"}:
        raise DataError("model_weights only supports dcf/peers/historical")
    if set(config["peer_multiple_weights"]) - {"pe", "ev_ebitda", "pb"}:
        raise DataError("peer_multiple_weights only supports pe/ev_ebitda/pb")
    for field in ("minimum_peers", "minimum_rank_sample", "minimum_historical_observations"):
        if not isinstance(config[field], int) or config[field] < (2 if field == "minimum_rank_sample" else 1):
            raise DataError(f"Invalid {field}")
    for field in ("risk_fraction_per_trade", "max_stock_allocation", "max_sector_allocation", "target_fair_value_fraction",
                  "cash_cost_buffer", "maximum_turnover_participation", "max_stop_distance_fraction"):
        if num(config[field]) is None or not 0 < config[field] <= 1:
            raise DataError(f"{field} must be a positive fraction <=1")
    for field in ("max_annual_age_days", "max_earnings_age_days", "max_quote_age_seconds", "max_trade_age_seconds",
                  "minimum_market_cap_crore", "max_model_dispersion", "slow_refresh_hours", "api_requests_per_second",
                  "minimum_interest_coverage", "maximum_net_debt_ebitda", "minimum_reward_risk"):
        if not positive(config[field]):
            raise DataError(f"{field} must be a positive finite number")
    for field in ("health_threshold", "quality_threshold", "cash_pillar_threshold", "health_exit_threshold"):
        if num(config[field]) is None or not 0 <= config[field] <= 5:
            raise DataError(f"{field} must be between 0 and 5")
    if not 1 <= config["minimum_valid_models_for_action"] <= 3:
        raise DataError("minimum_valid_models_for_action must be 1..3 (DCF still required)")
    if not 0 < config["minimum_positive_cfo_fraction"] <= 1:
        raise DataError("minimum_positive_cfo_fraction must be a positive fraction <=1")
    if not positive(config["tick_size_divisor"]):
        raise DataError("tick_size_divisor must be positive")
    if config["history_price_basis"] not in {"unverified", "raw", "split_adjusted"}:
        raise DataError("history_price_basis must be unverified/raw/split_adjusted")
    if config["benchmark_price_basis"] not in {"unverified", "raw", "split_adjusted"}:
        raise DataError("benchmark_price_basis must be unverified/raw/split_adjusted")


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Single-file Upstox comparison screener. Real-data default: read-only sector references, NOT intrinsic fair value. Full-model mode is optional.",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--env", default="env.txt", help="Credential file; values are never printed")
    p.add_argument("--analysis-mode", choices=["upstox-only", "full"], default=None,
                   help="Real-data default: Upstox-only relative comparisons, NOT intrinsic value. Demo default: full synthetic model")
    p.add_argument("--with-peers", action="store_true", help="Also request/expand named competitors in Upstox-only mode; otherwise use supplied sector benchmarks")
    p.add_argument("--show-account", action="store_true", help="Optional account read in Upstox-only mode; no hypothetical capital or orders")
    p.add_argument("--dump-upstox", default=None, help="Save raw COMPANY financial responses for diagnosis; never saves token/account/holdings data")
    p.add_argument("--config", default=None, help="Optional JSON assumption/threshold overrides")
    p.add_argument("--fundamentals", default=None, help="ISIN-matched rupee-denominated normalized financial supplement")
    p.add_argument("--fundamentals-only", action="store_true", help="Use local statements instead of Upstox Fundamentals; market data still Upstox")
    p.add_argument("--export-fundamentals", default=None, help="Export normalized upstream financials for audit/enrichment")
    p.add_argument("--statement-type", choices=["consolidated", "standalone"], default="consolidated")
    p.add_argument("--symbols", default=None, help="Comma-separated exact NSE EQ symbols; default six-stock consumer watchlist")
    p.add_argument("--universe", choices=["watchlist", "nifty500", "nse"], default="watchlist")
    p.add_argument("--limit", type=int, default=0, help="Explicit limit on TARGETS; 0 means no limit")
    p.add_argument("--max-peers", type=int, default=8, help="Maximum one-hop API competitors per target")
    p.add_argument("--no-peers", action="store_true", help="Do not auto-expand API competitors; include sufficient peers yourself")
    p.add_argument("--show-peers", action="store_true", help="Show background peers as well as requested targets")
    p.add_argument("--risk-free", type=float, default=None, help="Risk-free rate assumption as fraction, e.g. 0.07; not a live quote")
    p.add_argument("--erp", type=float, default=None, help="Equity risk premium assumption as fraction")
    p.add_argument("--capital", type=float, default=None, help="Strategy allocation base; preview/paper default INR 1000000; live requires explicit value")
    p.add_argument("--cash-budget", type=float, default=None, help="Explicit unencumbered strategy cash cap; REQUIRED for live execution")
    p.add_argument("--watch", action="store_true", help="Continuous quote/portfolio scans plus scheduled slow-layer refresh")
    p.add_argument("--interval", type=float, default=60, help="Seconds between scans; live data minimum 30")
    p.add_argument("--cycles", type=int, default=0, help="Number of watch scans; 0 means until Ctrl+C")
    p.add_argument("--schedule", default="08:30", help="IST daily slow-layer refresh time while --watch runs")
    p.add_argument("--top", type=int, default=0, help="Console rows to display; 0 shows all, exports always include all")
    p.add_argument("--summary-only", action="store_true", help="Suppress individual financial/model/gate details")
    p.add_argument("--details", action="store_true", help="Explicit alias for default detailed output")
    p.add_argument("--quiet", action="store_true", help="Exports only, except important live confirmations/errors")
    p.add_argument("--output", default="reports", help="CSV, JSON and transition-alert directory")
    p.add_argument("--state", default="screener_state.sqlite3", help="Market caches, paper account and durable intention ledger")
    p.add_argument("--refresh", action="store_true", help="Invalidate MARKET caches, preserving paper account and intentions")
    p.add_argument("--paper-orders", action="store_true", help="Enable LOCAL simulated entries/exits; never sends broker orders")
    p.add_argument("--execute", action="store_true", help="Separately gated, confirmed live broker writes; NOT unattended auto-buying")
    p.add_argument("--entry-method", choices=["gtt", "limit"], default="gtt", help="GTT has broker target/stop legs; limit entries need a running monitor")
    p.add_argument("--allow-health-exits", action="store_true", help="Permit individually confirmed live downgrade exits with sell authorization")
    p.add_argument("--ledger", action="store_true", help="Display saved local order intentions and exit")
    p.add_argument("--reconcile", action="store_true", help="Read-only reconciliation of live intentions with current broker data")
    p.add_argument("--resolve-intent", type=int, default=None, help="Manually clear one local block AFTER broker reconciliation; interactive, no broker write")
    p.add_argument("--init", action="store_true", help="Create config/template/empty env without overwriting existing files")
    p.add_argument("--login", action="store_true", help="Interactive OAuth authorization-code flow; save token to env.txt")
    p.add_argument("--demo", action="store_true", help="Offline SYNTHETIC fixtures exercising every analytical phase")
    p.add_argument("--self-test", action="store_true", help="Run embedded unit/integration tests; no credentials or broker writes")
    p.add_argument("--diagnose-instruments", action="store_true", help="Public-data-only listing audit; no credentials/account/trading calls")
    p.add_argument("--version", action="version", version=VERSION)
    return p


@contextlib.contextmanager
def process_lock(path: Path):
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ScreenerError(f"Another run may own {lock}. If its process is definitely stopped, remove the stale lock before restarting") from None
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            lock.unlink()


def cli(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    if args.self_test:
        return run_self_tests()
    if args.init:
        initialize(Path.cwd())
        return 0
    if args.analysis_mode is None:
        args.analysis_mode = "full" if args.demo else "upstox-only"
    if args.analysis_mode == "upstox-only" and (args.execute or args.paper_orders):
        raise DataError("Upstox-only references are not intrinsic valuations or trading signals. Execution/paper orders require explicitly selected full mode with complete reviewed data")
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if args.config:
        overrides = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
        unknown = set(overrides) - set(config)
        if unknown:
            raise DataError("Unknown config keys: " + ", ".join(sorted(unknown)))
        config.update(overrides)
    if args.risk_free is not None:
        config["risk_free_rate"] = args.risk_free
    if args.erp is not None:
        config["equity_risk_premium"] = args.erp
    validate_config(config)
    if args.symbols and args.universe != "watchlist":
        raise DataError("Use --symbols OR an index/all-equity --universe, not both")
    if args.details and args.summary_only:
        raise DataError("Use --details OR --summary-only")
    if (args.capital is not None and not positive(args.capital)) or (args.cash_budget is not None and not positive(args.cash_budget)):
        raise DataError("Capital/cash budget must be positive finite numbers")
    if args.limit < 0 or args.max_peers < 0 or args.top < 0 or args.cycles < 0:
        raise DataError("Limits/cycle counts cannot be negative")
    try:
        dt.time.fromisoformat(args.schedule)
    except ValueError:
        raise DataError("--schedule must be an IST time such as 08:30") from None
    if num(args.interval) is None or args.interval < (0.01 if args.demo else 30):
        raise DataError("--interval must be >=30 seconds for real data (>=0.01 for offline demo)")
    if args.fundamentals_only and not args.fundamentals:
        raise DataError("--fundamentals-only requires --fundamentals path.json")
    if args.refresh:
        config["fundamental_cache_hours"] = 0
    if args.diagnose_instruments and (args.execute or args.paper_orders or args.login or args.reconcile or args.resolve_intent is not None):
        raise DataError("--diagnose-instruments is public/read-only; do not combine it with execution/account/login operations")
    env_path = Path(args.env)
    env = {} if args.demo or args.diagnose_instruments else load_env(env_path)
    validate_live_flags(args, env)
    state_path = Path(args.state)
    if args.demo and args.state == "screener_state.sqlite3":
        state_path = Path("demo_state.sqlite3")
    with process_lock(state_path):
        state = State(state_path)
        try:
            if args.refresh:
                state.db.execute("DELETE FROM cache WHERE key NOT LIKE 'paper-account-%'")
                state.db.commit()
            if args.ledger:
                show_ledger(state)
                return 0
            if args.resolve_intent is not None:
                resolve_intent(state, args.resolve_intent)
                return 0
            client = DemoUpstox(state, config) if args.demo else Upstox(Http(env, state, config))
            if not args.demo:
                client.fetch_competitors = args.analysis_mode == "full" or args.with_peers
            if args.diagnose_instruments:
                diagnose_instruments(client, args)
                return 0
            if args.login:
                if args.demo:
                    raise DataError("--demo does not authenticate")
                oauth_login(env_path, env, client.http)
                return 0
            if args.reconcile:
                if args.demo:
                    raise DataError("Live reconciliation is not part of offline demo")
                client.validate_token()
                account = client.account()
                if not account["complete"]:
                    raise ScreenerError("Cannot reconcile: broker account snapshot incomplete. " + "; ".join(account["errors"]))
                reconcile_live(client, state, account)
                show_ledger(state)
                return 0
            Screener(args, env, config, state, client).run()
            return 0
        finally:
            state.close()


def tradable_session(market: dict[str, Any], quote: dict[str, Any], clock: dt.datetime) -> bool:
    if market.get("status") != "NORMAL_OPEN":
        return False
    timings = market.get("timings") or []
    if timings:
        in_timing = any(parse_timestamp(t.get("start_time")) and parse_timestamp(t.get("end_time"))
                        and parse_timestamp(t["start_time"]) <= clock < parse_timestamp(t["end_time"])
                        for t in timings)
        if not in_timing:
            return False
    if quote.get("cas_eligible"):
        status = (market.get("cas_eligible_status") or {}).get("status")
        if status and status != "NORMAL_OPEN":
            return False
        # Do not route this strategy into the closing auction when CAS status
        # is absent; new CAS-eligible securities close continuous trade ~15:15.
        if clock.time() >= dt.time(15, 15):
            return False
    return True


def managed_limit_exit_plans(results: dict[str, dict[str, Any]], state: State,
                             account: dict[str, Any]) -> list[dict[str, Any]]:
    """Optional LIMIT-entry target/stop monitor; each exit is manually confirmed.

    It is NOT an exchange stop or unattended protection. If settled quantity
    cannot be verified, emits an alert requiring action in the broker app.
    """
    out = []
    for intent in state.intents("live"):
        if intent["action"] != "BUY" or intent["status"] in TERMINAL_INTENT_STATUSES or "rules" in intent["payload"]:
            continue
        position = intent["response"].get("plan") or {}
        filled = int(num(intent["response"].get("filled_quantity")) or 0)
        isin = isin_from_key(intent["instrument"])
        result = results.get(isin)
        if not filled or not result or not result["price"] or not position:
            continue
        price = result["price"]
        reason = "LIMIT_STOP" if price <= position["stop"] else "LIMIT_TARGET" if price >= position["target"] else None
        if not reason:
            continue
        sellable = sum(holding_sellable(h) for h in account.get("holdings") or []
                       if (h.get("isin") or isin_from_key(h.get("instrument_token"))) == isin)
        out.append({"symbol": result["symbol"], "isin": isin, "instrument_key": intent["instrument"],
                    "quantity": min(filled, sellable), "reason": reason,
                    "health": result["health"].get("score"), "price": price, "tick": result["tick"],
                    "linked_entry_intent": intent["id"]})
    return out


def canonical_action_kind(kind: Any) -> str:
    label = str(kind).lower()
    for word in ("bonus", "split", "rights", "demerger", "merger", "consolidation"):
        if word in label:
            return word
    return label.strip()


def review_is_current(company: dict[str, Any], ctx: dict[str, Any], clock: dt.datetime) -> bool:
    if company.get("manual_review_complete") is not True:
        return False
    period = parse_date(company.get("last_reviewed_financial_period"))
    reviewed = parse_date(company.get("manual_review_as_of"))
    dates = [parse_date(row["period"]) for row in ctx["annual"] + ctx["quarterly"]]
    if not period or not reviewed or reviewed > clock.date() or not dates or period < max(dates):
        return False
    if reviewed < max(dates):
        return False
    action_dates = [parse_date(a.get("date")) for a in company.get("actions", [])]
    action_dates = [date for date in action_dates if date and date <= clock.date()]
    return not action_dates or reviewed >= max(action_dates)


def absolute_cash_flow_safety(ctx: dict[str, Any], config: dict[str, Any]) -> bool:
    rows = [row for row in ctx["annual"] if num(row.get("cfo")) is not None][-5:]
    if len(rows) < 3 or not positive(rows[-1].get("cfo")):
        return False
    return sum(row["cfo"] > 0 for row in rows) / len(rows) >= config["minimum_positive_cfo_fraction"]


def absolute_debt_safety(ctx: dict[str, Any], config: dict[str, Any]) -> bool:
    debt, cash = ctx["balance"].get("debt"), ctx["balance"].get("cash")
    if debt is None or cash is None:
        return False
    if debt == 0:
        return True
    cover = ratio(ctx["current"].get("ebit"), ctx["current"].get("interest_expense"))
    leverage = ratio(debt - cash, ctx["current"].get("ebitda"))
    return cover is not None and leverage is not None and cover >= config["minimum_interest_coverage"] and leverage <= config["maximum_net_debt_ebitda"]


# ------------------------- Embedded offline tests ------------------------

class ScreenerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = json.loads(json.dumps(DEFAULT_CONFIG))
        self.state = State(self.root / "tests.sqlite3")
        self.demo = DemoUpstox(self.state, self.config)
        self.args = make_parser().parse_args(["--demo", "--quiet", "--state", str(self.root / "tests.sqlite3"),
                                              "--output", str(self.root / "reports")])

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def loaded(self) -> Screener:
        runner = Screener(self.args, {}, self.config, self.state, self.demo)
        runner.load()
        return runner

    def demo_company(self, index: int = 0) -> tuple[dict, dict, dict]:
        isin = f"DEMO{index:08d}"
        inst = next(i for i in self.demo.raw_instruments if i.get("isin") == isin)
        c = normalize_bundle(inst, self.demo.fundamental_bundle(isin, "consolidated"), self.config)
        c = merge_supplement(c, self.demo.supplements[isin], "consolidated")
        quote = self.demo.quotes([inst["instrument_key"]])[inst["instrument_key"]]
        return c, financial_context(c, quote, self.demo.clock, self.config), quote

    # v1.0.2 limited-payload and comparison-mode regressions. The EPS/ratio/
    # CFO numbers below are those supplied by the user, NOT fresh API reads.
    def limited_company(self, hul: bool = False) -> dict[str, Any]:
        symbol, isin, sector = (("HINDUNILVR", "INE030A01027", "Household Products") if hul else
                                 ("ITC", "INE154A01025", "Tobacco"))
        pairs = ([29.77, 35.6, 9.14, 7.2, 18.77, 10.32, 22.41, 12.29, 26.21, 19.9, 0.92, 8.89, 21.64, 25.17]
                 if hul else [16.22, 18.44, 4.51, 2.27, 21.52, 12.46, 28.53, 14.37, 37.26, 18.65, 1.92, 1.49, 11.42, 10.76])
        names = ["P/E", "P/B", "ROA", "ROE", "ROCE", "Quick Ratio", "EV/EBITDA"]
        cfos = [9991, 15469, 11886, 10999] if hul else [18877.55, 17178.86, 17627.04, 18464.31]
        bundle = {"profile": {"sector": sector}, "fetched_at": "2026-10-15T12:17:00+05:30",
                  "annual_income": {"type": "consolidated", "units_in": "crore", "full_statement": [
                      {"particular": "EPS - Diluted", "history": [{"period": "Mar 2026", "value": 64 if hul else 16.51}]}]},
                  "cash": {"type": "consolidated", "units_in": "crore", "cash_flow": [
                      {"category": "Operating", "history": [{"period": f"Mar {2023+i}", "value": v} for i, v in enumerate(cfos)]}]},
                  "ratios": [{"name": name, "company_value": str(pairs[2*i]) + ("%" if name in {"ROA", "ROE", "ROCE"} else ""),
                              "sector_value": str(pairs[2*i+1]) + ("%" if name in {"ROA", "ROE", "ROCE"} else "")}
                             for i, name in enumerate(names)], "competitors": None, "actions": [], "errors": []}
        return normalize_bundle({"trading_symbol": symbol, "isin": isin, "instrument_key": "NSE_EQ|"+isin}, bundle, self.config)

    def limited_result(self, company: dict | None = None, price: float = 264.4) -> dict:
        return relative_stock(company or self.limited_company(), {"last_price": price}, [], [],
                              dt.datetime(2026, 10, 15, 12, 17, tzinfo=IST), self.config)

    def test_relative_itc_user_eps_sector_reference(self):
        r = self.limited_result()
        self.assertAlmostEqual(r["sector_pe_reference"], 304.4444)
        self.assertAlmostEqual(r["reference_gap"], 304.4444/264.4-1)
        self.assertAlmostEqual(r["live_eps_pe"], 264.4/16.51)
        self.assertEqual(r["eps_basis"], "FY 2026-03-31")
        self.assertEqual(r["signal"], "LIVE_EPS_PE_BELOW_SECTOR")
        self.assertFalse(r["ready"])
        self.assertFalse(r["orders_allowed"])
        self.assertNotIn("fair_value", r)
        self.assertNotIn("health", r)

    def test_relative_hul_user_eps_sector_reference(self):
        r = self.limited_result(self.limited_company(True), 1905.6)
        self.assertAlmostEqual(r["sector_pe_reference"], 2278.4)
        self.assertAlmostEqual(r["reference_gap"], 2278.4/1905.6-1)
        self.assertEqual(r["reported_ratios"]["quick_ratio"]["sector"], 8.89)

    def test_reported_returns_visible_without_internal_accounts(self):
        r = self.limited_result()
        self.assertEqual(r["reported_ratios"]["roe"]["company"], 28.53)
        self.assertEqual(r["reported_ratios"]["roce"]["company"], 37.26)
        self.assertEqual(r["reported_ratios"]["roa"]["company"], 21.52)
        self.assertAlmostEqual(r["reported_ratios"]["roe"]["difference"], 14.16)
        self.assertEqual(r["annual_diagnostics"]["cfo_available_years"], 4)
        self.assertEqual(r["annual_diagnostics"]["cfo_positive_years"], 4)
        self.assertTrue(all(y["capex"] is None and y["cfo_minus_capex"] is None for y in r["annual_diagnostics"]["years"]))

    def test_reported_ratio_wrappers_and_missing_values(self):
        r = reported_ratios({"key_ratios": [{"name": "Return On Equity", "company_value": "-2.50%", "sector_value": "10%"},
                                            {"name": "P/E", "company_value": "N/A", "sector_value": "20"}, None]})
        self.assertEqual(r["roe"]["company"], -2.5)
        self.assertIsNone(r["pe"]["relative_gap"])
        self.assertEqual(reported_ratios("unexpected"), {})

    def test_relative_eps_not_back_calculated_from_provider_pe(self):
        c = self.limited_company()
        for row in c["annual"]:
            row.pop("eps_diluted", None)
        r = self.limited_result(c)
        self.assertIsNone(r["eps_diluted"])
        self.assertIsNone(r["sector_pe_reference"])
        self.assertEqual(r["signal"], "REFERENCE_UNAVAILABLE")
        self.assertEqual(r["reported_ratios"]["pe"]["company"], 16.22)

    def test_relative_negative_eps_no_price_reference(self):
        c = self.limited_company()
        c["annual"][-1]["eps_diluted"] = -16.51
        self.assertIsNone(self.limited_result(c)["sector_pe_reference"])

    def test_relative_missing_sector_pe_no_reference(self):
        c = self.limited_company()
        c["provider_ratios"][0]["sector_value"] = "-"
        self.assertIsNone(self.limited_result(c)["sector_pe_reference"])

    def test_relative_missing_price_no_reference(self):
        self.assertIsNone(self.limited_result(price=0)["sector_pe_reference"])

    def test_relative_old_eps_no_reference(self):
        c = self.limited_company()
        c["annual"][-1].pop("eps_diluted")
        c["annual"][0]["eps_diluted"] = 16.51
        self.assertIn("too old", self.limited_result(c)["reference_reason"])

    def test_relative_known_capital_action_after_eps_blocked(self):
        c = self.limited_company()
        c["actions"] = [{"date": "2026-07-01", "kind": "split", "factor": 2}]
        self.assertIn("share basis", self.limited_result(c)["reference_reason"])

    def test_relative_unknown_action_date_blocked(self):
        c = self.limited_company()
        c["actions"] = [{"date": None, "kind": "split", "factor": None}]
        self.assertIsNone(self.limited_result(c)["sector_pe_reference"])

    def test_relative_pre_eps_action_does_not_suppress_reference(self):
        c = self.limited_company()
        c["actions"] = [{"date": "2025-01-01", "kind": "demerger", "factor": None}]
        self.assertAlmostEqual(self.limited_result(c)["sector_pe_reference"], 304.4444)

    def test_relative_same_fy_cfo_revenue_not_ttm_mixed(self):
        # Synthetic matching controls, not the user's undisclosed FY revenues.
        d = matching_annual_diagnostics([{"period": "2025-03-31", "cfo": 30.0, "revenue": 100.0},
                                        {"period": "2026-03-31", "cfo": 50.0}])
        self.assertEqual(d["years"][0]["cfo_revenue"], 0.3)
        self.assertIsNone(d["years"][1]["cfo_revenue"])
        self.assertIsNone(d["years"][1]["cfo_minus_capex"])

    def test_relative_current_ratio_requires_same_period(self):
        d = matching_annual_diagnostics([{"period": "2025-03-31", "current_assets": 300},
                                        {"period": "2026-03-31", "current_liabilities": 100}])
        self.assertIsNone(d["current_ratio"])
        d = matching_annual_diagnostics([{"period": "2026-03-31", "current_assets": 300, "current_liabilities": 100}])
        self.assertEqual(d["current_ratio"], 3)

    def test_relative_raw_price_levels_visible_without_verification(self):
        c, _, q = self.demo_company()
        c["actions_coverage_verified"] = False
        c["history_price_basis"] = "unverified"
        p = indicative_price_diagnostics(c, self.demo.history[c["instrument_key"]], [], q["last_price"], self.demo.clock)
        self.assertEqual(p["basis"], "UNVERIFIED_API_LEVELS_INDICATIVE")
        self.assertIsNotNone(p["ma20"])
        self.assertIsNotNone(p["ma50"])
        self.assertIsNotNone(p["average_turnover20_crore"])

    def test_relative_excludes_incomplete_today_candle(self):
        c, _, q = self.demo_company()
        rows = self.demo.history[c["instrument_key"]] + [{"date": str(self.demo.clock.date()), "close": 9999, "high": 9999, "low": 9999, "volume": 1}]
        p = indicative_price_diagnostics(c, rows, [], q["last_price"], self.demo.clock)
        self.assertLess(parse_date(p["last_completed_day"]), self.demo.clock.date())
        self.assertLess(p["ma20"], 9999)

    def test_relative_turnover_uses_raw_price_volume_pair(self):
        c = self.limited_company()
        c["actions_coverage_verified"] = True
        rows = [{"date": str(dt.date(2026, 9, 1)+dt.timedelta(days=i)), "close": 100, "raw_close": 100,
                 "high": 101, "low": 99, "volume": 2000} for i in range(50)]
        adjusted = [{**r, "close": 50, "high": 50.5, "low": 49.5, "volume": 4000} for r in rows]
        p = indicative_price_diagnostics(c, rows, adjusted, 50, dt.datetime(2026, 10, 25, tzinfo=IST))
        self.assertEqual(p["ma20"], 50)
        self.assertEqual(p["average_turnover20_crore"], 100*2000/CRORE)

    def test_relative_short_history_not_zero_levels(self):
        p = indicative_price_diagnostics(self.limited_company(), [], [], 264.4, self.demo.clock)
        self.assertIsNone(p["ma20"])
        self.assertIsNone(p["average_turnover20_crore"])

    def test_relative_company_dump_redacts_secrets_not_public_tokens(self):
        payload = {"Authorization": "secret", "nested": [{"UPSTOX_API_SECRET": "secret2"}],
                   "exchange_token": "1660", "income": {"value": 16.51}}
        cleaned = sanitize_company_dump(payload)
        self.assertEqual(cleaned["Authorization"], "[REDACTED]")
        self.assertEqual(cleaned["nested"][0]["UPSTOX_API_SECRET"], "[REDACTED]")
        self.assertEqual(cleaned["exchange_token"], "1660")

    def test_relative_dump_contains_no_environment_or_account(self):
        self.args.analysis_mode = "upstox-only"
        self.args.dump_upstox = str(self.root / "raw")
        runner = Screener(self.args, {"UPSTOX_ACCESS_TOKEN": "test-secret-never-export"}, self.config, self.state, self.demo)
        runner.load()
        files = list((self.root / "raw").glob("*.json"))
        self.assertEqual(len(files), len(runner.targets))
        for p in files:
            text = p.read_text()
            self.assertNotIn("test-secret-never-export", text)
            data = json.loads(text)
            self.assertTrue(data["company_financials_only"])
            self.assertIn("responses", data)
            self.assertNotIn("account", data)
            self.assertNotIn("env", data)

    def test_relative_mode_skips_account_peer_expansion_and_benchmark(self):
        self.args.analysis_mode = "upstox-only"
        self.args.limit = 1
        original = self.demo.account
        self.demo.account = lambda: (_ for _ in ()).throw(AssertionError("account called"))
        try:
            runner = self.loaded()
            self.assertEqual(len(runner.companies), 1)
            self.assertEqual(runner.benchmark, [])
            runner.cycle_upstox_only(False)
            self.assertIsNone(runner.paper)
            self.assertTrue(all(not r["orders_allowed"] for r in runner.results.values()))
        finally:
            self.demo.account = original

    def test_relative_optional_account_is_read_only_not_exported(self):
        self.args.analysis_mode = "upstox-only"
        self.args.show_account = True
        calls = []
        self.demo.account = lambda: calls.append(True) or {"complete": True, "errors": [], "holdings": [], "positions": [],
                                                          "funds": {"equity": {"available_margin": 211017.23}}}
        runner = self.loaded()
        runner.cycle_upstox_only(False)
        self.assertEqual(len(calls), 1)
        text = (runner.report_dir / "latest_screen.json").read_text()
        self.assertNotIn("211017.23", text)
        self.assertIsNone(runner.paper)

    def test_relative_orders_blocked_programmatically(self):
        self.args.analysis_mode = "upstox-only"
        runner = self.loaded()
        for name in ("execute", "paper_orders"):
            setattr(self.args, name, True)
            with self.assertRaises(DataError):
                runner.cycle_upstox_only(False)
            setattr(self.args, name, False)

    def test_relative_cli_orders_rejected_before_network(self):
        for flags in (["--execute"], ["--paper-orders"], ["--demo", "--analysis-mode", "upstox-only", "--paper-orders"]):
            with self.assertRaises(DataError):
                cli(flags)

    def test_relative_cli_defaults_real_comparison_demo_full(self):
        from unittest.mock import patch
        for demo, expected in ((False, "upstox-only"), (True, "full")):
            state = str(self.root / ("cli-demo.sqlite3" if demo else "cli-real.sqlite3"))
            args = ["--env", str(self.root/"absent-env"), "--state", state, "--quiet"] + (["--demo"] if demo else [])
            with patch(__name__+".Screener") as factory:
                self.assertEqual(cli(args), 0)
                self.assertEqual(factory.call_args.args[0].analysis_mode, expected)
                factory.return_value.run.assert_called_once()

    def test_relative_export_schema_distinct_from_intrinsic(self):
        directory = self.root / "relative-reports"
        r = self.limited_result()
        export_relative_reports(directory, [r], self.demo.clock, {"status": "NORMAL_OPEN"}, False, [])
        data = json.loads((directory / "latest_screen.json").read_text())
        self.assertEqual(data["mode"], "upstox-only")
        self.assertFalse(data["orders_allowed"])
        with (directory / "latest_screen.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertAlmostEqual(float(rows[0]["sector_pe_reference"]), 304.4444)
        self.assertEqual(rows[0]["roe_company"], "28.53")
        self.assertNotIn("fair_value", rows[0])
        self.assertNotIn("health_score", rows[0])

    def test_relative_competitor_wrapper_defensive(self):
        key = "NSE_EQ|INE030A01027"
        self.assertEqual(extract_competitor_keys({"competitors": [{"instrument_key": key}, None, {"instrument_key": key}]}), [key])
        self.assertEqual(extract_competitor_keys("not a list"), [])

    def comparison_http(self, failure: Exception | None = None) -> tuple[Any, list]:
        calls = []
        class FakeHttp:
            config = self.config
            state = self.state
            def request(inner, method, url, **kwargs):
                calls.append((method, url))
                if url.endswith("/competitors"):
                    if "NSE_EQ%7C" in url:
                        if failure:
                            raise failure
                        return {"data": [{"instrument_key": "NSE_EQ|INE030A01027"}]}
                    raise ApiError(400, "Invalid Instrument key", "UDAPI100011")
                return {"data": None}
        client = Upstox(FakeHttp())
        client.instrument_keys_by_isin = {"INE154A01025": "NSE_EQ|INE154A01025"}
        return client, calls

    def test_relative_upstox_no_competitors_request_by_default_config(self):
        client, calls = self.comparison_http()
        client.fetch_competitors = False
        bundle = client.fundamental_bundle("INE154A01025", "consolidated")
        self.assertEqual(bundle["competitors"], [])
        self.assertTrue(all(not url.endswith("/competitors") for _, url in calls))
        self.assertTrue(all(method == "GET" for method, _ in calls))

    def test_competitors_specific_read_fallback_known_real_key(self):
        client, calls = self.comparison_http()
        bundle = client.fundamental_bundle("INE154A01025", "consolidated")
        self.assertEqual(bundle["competitors_status"], "INSTRUMENT_KEY_READ_FALLBACK")
        self.assertEqual(bundle["competitors"][0]["instrument_key"], "NSE_EQ|INE030A01027")
        self.assertEqual(len([u for _, u in calls if u.endswith("/competitors")]), 2)
        self.assertTrue(all(m == "GET" for m, _ in calls))

    def test_competitors_fallback_failure_visible_without_write(self):
        client, calls = self.comparison_http(ApiError(400, "Still invalid", "UDAPI100011"))
        bundle = client.fundamental_bundle("INE154A01025", "consolidated")
        self.assertIsNone(bundle["competitors"])
        self.assertEqual(len(bundle["errors"]), 2)
        self.assertTrue(all(m == "GET" for m, _ in calls))

    def test_competitors_fallback_authentication_not_suppressed(self):
        client, _ = self.comparison_http(AuthenticationError("Expired"))
        with self.assertRaises(AuthenticationError):
            client.fundamental_bundle("INE154A01025", "consolidated")

    def test_competitors_no_known_key_no_fallback(self):
        client, calls = self.comparison_http()
        client.instrument_keys_by_isin = {}
        bundle = client.fundamental_bundle("INE154A01025", "consolidated")
        self.assertIsNone(bundle["competitors"])
        self.assertEqual(len([u for _, u in calls if u.endswith("/competitors")]), 1)

    def test_relative_parse_display_only_aggregates_no_bridge_fabrication(self):
        c = self.limited_company()
        records = {}
        parse_full_statement({"units_in": "crore", "full_statement": [
            {"particular": "Equity Capital", "history": [{"period": "Mar 2026", "value": 100}]},
            {"particular": "Cash (End of the year)", "history": [{"period": "Mar 2026", "value": 20}]},
            {"particular": "Total Liabilities", "history": [{"period": "Mar 2026", "value": 50}]}]}, records)
        row = records["2026-03-31"]
        self.assertEqual(row["equity_total_reported"], 100*CRORE)
        self.assertEqual(row["cash_reported"], 20*CRORE)
        self.assertEqual(row["total_liabilities_reported"], 50*CRORE)
        self.assertNotIn("debt", row)
        self.assertNotIn("share_capital", row)
        self.assertNotIn("cash", row)

    def test_relative_category_labels_case_and_separator_robust(self):
        rows = {}
        parse_categories({"units_in": "crore", "income_statement": [
            {"category": "Net Profit", "history": [{"period": "Mar 2026", "value": 100}]}]},
            "income_statement", {"net_profit": "net_profit"}, rows)
        self.assertEqual(rows["2026-03-31"]["net_profit"], 100*CRORE)

    def test_relative_cycle_dispatch_cannot_enter_legacy_full_path(self):
        self.args.analysis_mode = "upstox-only"
        self.demo.account = lambda: (_ for _ in ()).throw(AssertionError("account called"))
        runner = self.loaded()
        results = runner.cycle(False)
        self.assertTrue(all(r["mode"] == "upstox-only" and not r["orders_allowed"] for r in results.values()))
        self.assertIsNone(runner.paper)

    def test_relative_with_peers_explicitly_expands_only_when_requested(self):
        self.args.analysis_mode = "upstox-only"
        self.args.limit = 1
        self.args.with_peers = True
        runner = self.loaded()
        self.assertGreater(len(runner.companies), len(runner.targets))
        self.assertEqual(runner.benchmark, [])

    def test_relative_quote_timing_missing_and_stale_disclosed(self):
        c = self.limited_company()
        clock = dt.datetime(2026, 10, 15, 12, 17, tzinfo=IST)
        r = self.limited_result(c)
        self.assertEqual(r["quote_timing"], "SNAPSHOT_TIME_MISSING")
        r = relative_stock(c, {"last_price": 264.4, "timestamp": (clock-dt.timedelta(hours=1)).isoformat()}, [], [], clock, self.config)
        self.assertEqual(r["quote_timing"], "SNAPSHOT_STALE")
        self.assertIsNotNone(r["sector_pe_reference"])
        self.assertFalse(r["orders_allowed"])

    def test_relative_quote_timing_recent_snapshot(self):
        c = self.limited_company()
        clock = dt.datetime(2026, 10, 15, 12, 17, tzinfo=IST)
        q = {"last_price": 264.4, "timestamp": clock.isoformat(), "last_trade_time": int(clock.timestamp()*1000)}
        r = relative_stock(c, q, [], [], clock, self.config)
        self.assertEqual(r["quote_timing"], "RECENT_SNAPSHOT")
        self.assertEqual(r["last_trade_age_seconds"], 0)

    def test_relative_fractional_return_not_silently_rescaled(self):
        r = reported_ratios([{"name": "ROE", "company_value": 0.2853, "sector_value": 0.1437}])
        self.assertEqual(r["roe"]["company"], 0.2853)
        self.assertEqual(r["roe"]["unit"], "percent_points")

    def test_relative_capex_unknown_does_not_become_cfo_fcf(self):
        d = matching_annual_diagnostics([{"period": "2026-03-31", "cfo": 100, "capex": None}])
        self.assertIsNone(d["years"][0]["cfo_minus_capex"])
        d = matching_annual_diagnostics([{"period": "2026-03-31", "cfo": 100, "capex": 0}])
        self.assertEqual(d["years"][0]["cfo_minus_capex"], 100)

    def test_env_quotes_comments_equals(self):
        p = self.root / "env.txt"
        p.write_text('UPSTOX_ACCESS_TOKEN="abc=="\nUPSTOX_API_TOKEN=app # comment\nexport UPSTOX_API_SECRET=secret\n')
        result = load_env(p)
        self.assertEqual(result["UPSTOX_ACCESS_TOKEN"], "abc==")
        self.assertEqual(result["UPSTOX_API_TOKEN"], "app")
        self.assertEqual(result["UPSTOX_API_SECRET"], "secret")

    def test_env_no_execution_and_reject_invalid_key(self):
        p = self.root / "env.txt"
        p.write_text('$(touch hacked)=abc\n')
        with self.assertRaises(DataError):
            load_env(p)
        self.assertFalse((self.root / "hacked").exists())

    def test_save_token_preserves_secret_no_duplicate(self):
        p = self.root / "env.txt"
        p.write_text('UPSTOX_API_SECRET=private\nUPSTOX_ACCESS_TOKEN=old\n')
        save_env_token(p, "new")
        save_env_token(p, "newer")
        self.assertEqual(p.read_text().count("UPSTOX_ACCESS_TOKEN="), 1)
        self.assertEqual(load_env(p)["UPSTOX_API_SECRET"], "private")

    def test_redactor(self):
        r = Redactor({"UPSTOX_ACCESS_TOKEN": "a-secret-token", "UPSTOX_API_SECRET": "secret-api"})
        self.assertNotIn("a-secret-token", r("message: a-secret-token secret-api"))
        self.assertNotIn("secret-api", r("message: a-secret-token secret-api"))

    def test_number_zero_not_missing(self):
        self.assertEqual(num("0"), 0)
        self.assertIsNone(positive(0))
        self.assertIsNone(num(float("nan")))
        self.assertEqual(num("(1,000.5)"), -1000.5)

    def test_upside_vs_discount_example(self):
        fair, price = 392.7, 263.2
        self.assertAlmostEqual((fair-price)/price, 0.4920212765957447)
        self.assertAlmostEqual((fair-price)/fair, 0.329768271)

    def test_period_month_end(self):
        self.assertEqual(parse_date("Mar 2025"), dt.date(2025, 3, 31))
        self.assertEqual(parse_date("Feb 2024"), dt.date(2024, 2, 29))

    def test_timestamp_seconds_ms_microseconds(self):
        base = 1770000000
        self.assertEqual(parse_timestamp(base), parse_timestamp(base * 1000))
        self.assertEqual(parse_timestamp(base), parse_timestamp(base * 1_000_000))

    def test_decode_candles_sorts_deduplicates(self):
        rows = [["2025-01-03", 10, 12, 9, 11, 100, 0], ["2025-01-02", 10, 12, 9, 10, 100, 0],
                ["2025-01-03", 10, 12, 9, 11, 100, 0], ["2025-01-04", 10, 8, 9, 11, 100, 0]]
        result = decode_candles(rows, dt.date(2025,1,1), dt.date(2025,1,5))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["date"], "2025-01-02")

    def test_units_crore_except_eps(self):
        rows = {}
        parse_full_statement({"units_in": "crore", "full_statement": [
            {"particular": "Revenue", "history": [{"period": "Mar 2025", "value": 100}]},
            {"particular": "EPS - Diluted", "history": [{"period": "Mar 2025", "value": 12}]}]}, rows)
        self.assertEqual(rows["2025-03-31"]["revenue"], 100 * CRORE)
        self.assertEqual(rows["2025-03-31"]["eps_diluted"], 12)

    def test_unknown_units_rejected(self):
        with self.assertRaises(DataError):
            unit_factor({"units_in": "thousands-unknown"})

    def test_no_investing_to_capex_substitution(self):
        rows = {}
        parse_categories({"units_in": "crore", "cash_flow": [
            {"category": "investing", "history": [{"period": "Mar 2025", "value": -50}]}]},
                         "cash_flow", {"operating": "cfo"}, rows)
        self.assertEqual(rows, {})

    def test_ambiguous_operating_profit_not_ebitda(self):
        rows = {}
        parse_full_statement({"units_in": "crore", "full_statement": [
            {"particular": "Operating Profit", "history": [{"period": "Mar 2025", "value": 10}]}]}, rows)
        self.assertFalse(any("ebitda" in row for row in rows.values()))

    def test_no_liabilities_to_debt_conversion(self):
        inst = self.demo.raw_instruments[1]
        bundle = {"balance": {"units_in": "crore", "history": [{"period": "Mar 2025", "total_asset": 100, "total_liability": 60}]}}
        c = normalize_bundle(inst, bundle, self.config)
        self.assertNotIn("debt", c["annual"][-1])

    def test_supplement_null_does_not_erase(self):
        c, _, _ = self.demo_company()
        original = c["annual"][-1]["revenue"]
        c = merge_supplement(c, {"annual": [{"period": "2026-03-31", "revenue": None}]}, "consolidated")
        self.assertEqual(c["annual"][-1]["revenue"], original)

    def test_supplement_basis_mismatch_blocked(self):
        c, _, _ = self.demo_company()
        with self.assertRaises(DataError):
            merge_supplement(c, {"accounting_basis": "standalone"}, "consolidated")

    def test_supplement_negative_capex_rejected(self):
        p = self.root / "fund.json"
        atomic_json(p, {"currency":"INR", "unit":"rupees", "companies":[
            {"isin":"test", "annual":[{"period":"2025-03-31", "capex":-10}]}]})
        with self.assertRaises(DataError):
            read_supplements(p)

    def test_cfo_operating_interest_fcff(self):
        f, _ = fcff_from_record({"cfo":100,"capex":20,"cash_interest":10,"tax_rate":0.25},
                               {"cfo_interest_classification":"operating"}, self.config)
        self.assertEqual(f, 87.5)

    def test_cfo_financing_interest_tax_shield(self):
        f, _ = fcff_from_record({"cfo":110,"capex":20,"cash_interest":10,"tax_rate":0.25},
                               {"cfo_interest_classification":"financing"}, self.config)
        self.assertEqual(f, 87.5)

    def test_ebit_reinvestment_fcff(self):
        f, _ = fcff_from_record({"ebit":120,"depreciation":20,"capex":20,"change_operating_nwc":2.5,"tax_rate":0.25}, {}, self.config)
        self.assertEqual(f, 87.5)

    def test_fcff_unknown_classification_blocks(self):
        f, reason = fcff_from_record({"cfo":100,"capex":20,"cash_interest":10}, {}, self.config)
        self.assertIsNone(f)
        self.assertIn("classification", reason)

    def test_missing_capex_not_zero(self):
        f, _ = fcff_from_record({"cfo":100,"cash_interest":0,"debt":0,"interest_expense":0}, {}, self.config)
        self.assertIsNone(f)

    def test_dcf_terminal_discount_math(self):
        out = discounted_fcff(100, 0, 0.10, 0.03, 5)
        expected = sum(100/1.1**i for i in range(1,6)) + (100*1.03/(.10-.03))/1.1**5
        self.assertAlmostEqual(out["enterprise_value"], expected)

    def test_dcf_terminal_growth_guard(self):
        with self.assertRaises(DataError):
            discounted_fcff(100, .1, .04, .05)

    def test_negative_growth_not_forced_positive(self):
        rows = [{"period":f"{y}-03-31", "revenue":1000*.8**(y-2020)} for y in range(2020,2024)]
        self.assertLess(growth_rate(rows, "revenue", 3), 0)
        self.assertLess(clip(-.1, self.config["growth_bounds"]), 0)

    def test_bridge_missing_not_zero(self):
        self.assertIsNone(ev_to_equity(100, {"cash":0,"debt":10}))
        self.assertEqual(ev_to_equity(100, {"cash":5,"debt":10,"minority_interest":3,"preferred_equity":2,"non_operating_assets":1}), 91)

    def test_financial_business_dcf_skipped(self):
        out = dcf_model({"sector":"Banks", "industry":"Banking"}, {}, [], [], self.demo.clock, self.config)
        self.assertIsNone(out["value"])

    def test_dcf_demo_complete(self):
        c, ctx, _ = self.demo_company()
        candles, _ = adjusted_candles(c, self.demo.history[c["instrument_key"]], self.demo.clock)
        dcf = dcf_model(c, ctx, candles, self.demo.history[self.demo.index_key], self.demo.clock, self.config)
        self.assertGreater(dcf["value"], 0)
        self.assertEqual(len(dcf["scenarios"]["base"]["projections"]), 5)
        self.assertEqual(len(dcf["sensitivity"]), 9)

    def test_beta_constant_benchmark_unavailable(self):
        rows = []
        date = dt.date(2024, 1, 5)
        for i in range(100):
            rows.append({"date":str(date+dt.timedelta(days=7*i)),"open":100,"high":100,"low":100,"close":100,"volume":1})
        beta, _ = stock_beta(rows, rows, self.demo.clock)
        self.assertIsNone(beta)

    def test_beta_aligned_identical_series_one(self):
        rows = self.demo.history[self.demo.index_key]
        beta, count = stock_beta(rows, rows, self.demo.clock)
        self.assertGreaterEqual(count, 52)
        self.assertAlmostEqual(beta, 1.0)

    def test_beta_short_series_unavailable(self):
        rows = self.demo.history[self.demo.index_key][-90:]
        beta, count = stock_beta(rows, rows, self.demo.clock)
        self.assertIsNone(beta)
        self.assertLess(count, 52)

    def test_corporate_bonus_factor(self):
        actions, warnings = parse_actions([{"name":"Bonus", "expiry_date":"14 Aug 2025", "ratio":"1:1"}])
        self.assertEqual(actions[0]["factor"], 2)
        self.assertFalse(warnings)

    def test_corporate_bare_split_not_guessed(self):
        actions, warnings = parse_actions([{"name":"Split", "expiry_date":"14 Aug 2025", "ratio":"1:5"}])
        self.assertIsNone(actions[0]["factor"])
        self.assertTrue(warnings)

    def test_raw_split_adjustment_price_volume(self):
        c = {"history_price_basis":"raw", "actions_coverage_verified":True,
             "actions":[{"date":"2025-06-01", "factor":2}]}
        row = {"date":"2025-01-01", "open":100,"high":102,"low":98,"close":100,"raw_close":100,"volume":100}
        out, error = adjusted_candles(c, [row], self.demo.clock)
        self.assertIsNone(error)
        self.assertEqual(out[0]["close"], 50)
        self.assertEqual(out[0]["volume"], 200)
        self.assertEqual(out[0]["raw_close"], 100)

    def test_unverified_price_basis_not_silently_adjusted(self):
        out, error = adjusted_candles({"history_price_basis":"unverified"}, [], self.demo.clock)
        self.assertEqual(out, [])
        self.assertIsNotNone(error)

    def test_no_pit_history_no_historical_fair(self):
        c, ctx, _ = self.demo_company()
        c["point_in_time_verified"] = False
        out = historical_model(c, ctx, [], self.demo.clock, self.config)
        self.assertIsNone(out["value"])
        self.assertIn("point-in-time", out["reason"])

    def test_historical_publication_no_lookahead(self):
        c, ctx, _ = self.demo_company()
        c["historical_snapshots"] = [{"available_on":"2025-01-03", "ttm_eps":10,"share_basis_date":"2024-12-31","basis":"as_reported"}]
        candles = [{"date":f"2025-01-0{i}","close":100} for i in range(1,6)]
        config = {**self.config, "minimum_historical_observations":1}
        out = historical_model(c, ctx, candles, self.demo.clock, config)
        self.assertEqual(out["pe_observations"], 2)  # Jan 4 and Jan 5 only.
        self.assertEqual(out["first_observation"], "2025-01-04")

    def test_historical_uses_then_date_eps_not_current(self):
        c, ctx, _ = self.demo_company()
        c["historical_snapshots"] = [{"available_on":"2024-12-31T17:00:00+05:30","ttm_eps":10,"share_basis_date":"2024-12-31","basis":"as_reported"}]
        out = historical_model(c, ctx, [{"date":"2025-01-02","close":100}], self.demo.clock,
                               {**self.config,"minimum_historical_observations":1})
        self.assertEqual(out["median_pe"], 10)
        self.assertAlmostEqual(out["value"], ctx["current"]["eps_diluted"] * 10)

    def test_peers_target_excluded(self):
        runner = self.loaded()
        results = runner.cycle(display=False)
        peer = results["DEMO00000000"]["models"]["peers"]
        self.assertTrue(peer["value"])
        for component in peer["components"].values():
            self.assertNotIn("DEMOVALUE", component.get("peer_symbols", []))

    def test_single_stock_no_peer_fallback(self):
        c, ctx, _ = self.demo_company()
        out = peers_model(c, ctx, {c["isin"]:ctx}, {c["isin"]:c}, self.config)
        self.assertIsNone(out["value"])

    def test_blend_renormalizes_missing_model(self):
        models = {"dcf":{"value":100,"low":90,"high":110}, "peers":{"value":200,"low":180,"high":220}, "historical":unavailable("missing")}
        out = blend_models(models, self.config)
        self.assertEqual(out["count"], 2)
        self.assertAlmostEqual(sum(out["weights"].values()), 1)
        self.assertAlmostEqual(out["value"], (100*.4+200*.35)/.75)

    def test_outlier_quarantine_always_manual_review(self):
        models = {k:{"value":v,"low":v,"high":v} for k,v in zip(("dcf","peers","historical"),(100,105,500))}
        out = blend_models(models, self.config)
        self.assertIn("historical", out["quarantined"])
        self.assertTrue(out["review_required"])

    def test_all_tied_percentiles_neutral(self):
        self.assertEqual(percentile_score(10,[10]*5,True,5), 2.5)
        self.assertEqual(percentile_score(10,[10]*5,False,5), 2.5)

    def test_one_stock_score_not_five(self):
        self.assertIsNone(percentile_score(10,[10],True,5))

    def test_inverted_debt_score(self):
        self.assertEqual(percentile_score(1,[1,2,3,4,5],False,5), 5)
        self.assertEqual(percentile_score(5,[1,2,3,4,5],False,5), 0)

    def test_missing_health_data_not_healthy(self):
        c, _, _ = self.demo_company()
        out = health_scores({c["isin"]:c}, {c["isin"]:{}}, self.config)[c["isin"]]
        self.assertIsNone(out["score"])
        self.assertIsNone(out["quality_score"])

    def test_tick_decimal_rounding(self):
        self.assertEqual(tick_round(263.201,.01,"up"), 263.21)
        self.assertEqual(tick_round(263.209,.01,"down"), 263.20)
        self.assertEqual(tick_round(100.11,.05,"up"), 100.15)

    def test_tick_override_avoids_unit_assumption(self):
        self.assertEqual(instrument_tick({"tick_size":5},{"tick_size_rupees":.01},self.config), .01)

    def test_quote_missing_depth_not_tradable(self):
        _, _, q = self.demo_company()
        q = {**q, "depth":{}}
        check = quote_checks(q,.01,self.demo.clock,self.config)
        self.assertFalse(check["spread_ok"])
        self.assertFalse(check["depth_present"])

    def test_quote_stale_trade_blocks(self):
        _, _, q = self.demo_company()
        q["last_trade_time"] = str(int((self.demo.clock.timestamp()-600)*1000))
        self.assertFalse(quote_checks(q,.01,self.demo.clock,self.config)["fresh"])

    def test_quote_future_timestamp_blocks(self):
        _, _, q = self.demo_company()
        q["timestamp"] = (self.demo.clock+dt.timedelta(minutes=5)).isoformat()
        self.assertFalse(quote_checks(q,.01,self.demo.clock,self.config)["fresh"])

    def test_lower_circuit_locked_blocks(self):
        _, _, q = self.demo_company()
        q["last_price"] = q["lower_circuit_limit"]
        q["depth"] = {"buy":[],"sell":[{"price":q["last_price"],"quantity":100}]}
        self.assertTrue(quote_checks(q,.01,self.demo.clock,self.config)["lower_locked"])

    def test_cas_closed_blocks_normal_segment(self):
        self.assertFalse(tradable_session({"status":"NORMAL_OPEN","cas_eligible_status":{"status":"CTS_CLOSE"}},
                                         {"cas_eligible":True},self.demo.clock))

    def test_non_normal_session_not_actionable(self):
        self.assertFalse(tradable_session({"status":"PRE_OPEN_START"},{},self.demo.clock))

    def test_gtt_payload_api_v3_schema(self):
        plan = {"quantity":10,"instrument_key":"NSE_EQ|TEST","entry":100,"target":150,"stop":90}
        payload = gtt_payload(plan)
        self.assertEqual(payload["type"], "MULTIPLE")
        self.assertEqual(payload["product"], "D")
        self.assertEqual([r["strategy"] for r in payload["rules"]], ["ENTRY","TARGET","STOPLOSS"])
        self.assertTrue(all(r["trigger_type"] == "IMMEDIATE" for r in payload["rules"]))
        self.assertTrue(all(r["market_protection"] == -1 for r in payload["rules"]))

    def test_regular_order_limit_not_market(self):
        p = limit_payload("NSE_EQ|TEST","SELL",10,100,"test")
        self.assertEqual(p["order_type"],"LIMIT")
        self.assertEqual(p["product"],"D")

    def test_duplicate_intent_atomic(self):
        first = self.state.reserve_intent("same","paper","test","TEST","BUY",{})
        second = self.state.reserve_intent("same","paper","test","TEST","BUY",{})
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(self.state.intents()),1)

    def test_missing_account_not_assumed_empty(self):
        view = portfolio_view({"complete":False,"errors":["auth"],"funds":None}, {}, self.demo,{})
        self.assertFalse(view["complete"])
        self.assertIsNone(view["available_margin"])

    def test_unknown_sector_exposure_blocks(self):
        account = self.demo.account()
        account["holdings"] = [{"isin":"unknown","quantity":10,"last_price":100}]
        view = portfolio_view(account,{},self.demo,{})
        self.assertFalse(view["complete"])
        self.assertIn("unknown",view["unknown_sectors"])

    def test_risk_size_respects_caps(self):
        runner = self.loaded()
        result = runner.cycle(display=False)["DEMO00000000"]
        plan = result["plan"]
        self.assertEqual(plan["status"],"PLANNED")
        self.assertLessEqual(plan["rupee_risk"],1_000_000*self.config["risk_fraction_per_trade"]+1e-6)
        self.assertLessEqual(plan["value"],1_000_000*self.config["max_stock_allocation"]+1e-6)

    def test_zero_cash_zero_qty(self):
        runner = self.loaded()
        result = runner.cycle(display=False)["DEMO00000000"]
        view = portfolio_view(self.demo.account(),runner.companies,self.demo,{})
        plan = plan_trade(result,runner.instruments[result["isin"]],view,1_000_000,0,self.config)
        self.assertEqual(plan["quantity"],0)

    def test_sector_cap_zero_room_blocks(self):
        runner = self.loaded()
        result = runner.cycle(display=False)["DEMO00000000"]
        view = portfolio_view(self.demo.account(),runner.companies,self.demo,{})
        view["sector_values"][result["sector"]] = 200000
        plan = plan_trade(result,runner.instruments[result["isin"]],view,1_000_000,1_000_000,self.config)
        self.assertEqual(plan["quantity"],0)

    def test_sellable_excludes_unsettled_pledged_blocked(self):
        self.assertEqual(holding_sellable({"quantity":100,"t1_quantity":10,"collateral_quantity":20,"cnc_used_quantity":5}),65)
        self.assertEqual(holding_sellable({"quantity":1,"t1_quantity":2}),0)

    def test_paper_entry_exit_no_broker_write(self):
        self.args.paper_orders = True
        runner = self.loaded()
        first = runner.cycle(display=False)
        isin = "DEMO00000000"
        self.assertIn(isin,runner.paper.data["positions"])
        count = len(self.state.intents("demo-paper"))
        runner.cycle(display=False)
        self.assertEqual(len(self.state.intents("demo-paper")),count)
        runner.paper.data["positions"][isin]["target"] = first[isin]["price"] - 1
        exits = runner.paper.monitor(first,self.config)
        self.assertEqual(exits[0]["reason"],"FAIR_VALUE_TARGET")
        self.assertEqual(self.demo.mutations,0)

    def test_paper_health_downgrade_exit(self):
        self.args.paper_orders = True
        runner = self.loaded()
        results = runner.cycle(display=False)
        results["DEMO00000000"]["health"]["score"] = 2
        exits = runner.paper.monitor(results,self.config)
        self.assertEqual(exits[0]["reason"],"HEALTH_DOWNGRADE")

    def test_transition_alerts_debounced(self):
        self.assertTrue(self.state.new_alert("one","UNDERVALUED"))
        self.assertFalse(self.state.new_alert("one","UNDERVALUED"))
        self.assertTrue(self.state.new_alert("one","OVERVALUED"))

    def test_cache_ttl(self):
        self.state.put_cache("test",{"number":1})
        self.assertEqual(self.state.cached("test",60),{"number":1})
        self.state.db.execute("UPDATE cache SET created=0 WHERE key='test'")
        self.assertIsNone(self.state.cached("test",1))

    def test_demo_live_flags_rejected(self):
        args = make_parser().parse_args(["--demo","--execute"])
        with self.assertRaises(ScreenerError):
            validate_live_flags(args,{})

    def test_live_disabled_by_default(self):
        args = make_parser().parse_args(["--execute"])
        with self.assertRaises(ScreenerError):
            validate_live_flags(args,{})

    def test_truncated_token_rejected_before_network(self):
        client = Upstox(Http({"UPSTOX_ACCESS_TOKEN":"eyJ"},self.state,self.config))
        with self.assertRaises(AuthenticationError):
            client.validate_token()
        self.assertEqual(client.http.requests,0)

    def test_report_end_to_end_all_phases_models_and_details(self):
        args = make_parser().parse_args(["--demo","--state",str(self.root/'e2e.sqlite3'),"--output",str(self.root/'e2e-reports'),"--top","1"])
        runner = Screener(args,{},self.config,self.state,self.demo)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            runner.run()
        output = buf.getvalue()
        for phase in range(1,6):
            self.assertIn(f"PHASE {phase}/5", output)
        self.assertIn("DCF sensitivity",output)
        self.assertIn("Estimated upside",output)
        row = runner.results["DEMO00000000"]
        self.assertEqual(row["fair"]["count"],3)
        self.assertTrue(row["ready"])
        self.assertGreater(row["health"]["score"],3.5)
        report = json.loads((self.root/'e2e-reports'/'demo'/'latest_screen.json').read_text())
        self.assertTrue(report["demo"])
        self.assertEqual(len(report["results"]),8)
        self.assertEqual(self.demo.mutations,0)

    def test_bad_config_percent_units_rejected(self):
        with self.assertRaises(DataError):
            validate_config({**self.config,"risk_free_rate":7})

    def test_process_lock_duplicate_rejected(self):
        path = self.root/'locked.sqlite3'
        with process_lock(path):
            with self.assertRaises(ScreenerError):
                with process_lock(path):
                    pass
        self.assertFalse(Path(str(path)+'.lock').exists())

    def test_http_post_network_failure_never_retried(self):
        class BrokenOpener:
            calls = 0
            def open(self, req, timeout):
                self.calls += 1
                raise urllib.error.URLError("timeout")
        opener = BrokenOpener()
        http = Http({"UPSTOX_ACCESS_TOKEN":"fake"*40},self.state,self.config,opener=opener,sleeper=lambda _:None)
        http.limiter.wait = lambda:None
        with self.assertRaises(AmbiguousWriteError):
            http.request("POST",API+"/v3/order/gtt/place",payload={})
        self.assertEqual(opener.calls,1)

    def test_http_get_retries_bounded(self):
        class BrokenOpener:
            calls = 0
            def open(self, req, timeout):
                self.calls += 1
                raise urllib.error.URLError("timeout")
        opener = BrokenOpener()
        http = Http({"UPSTOX_ACCESS_TOKEN":"fake"*40},self.state,self.config,opener=opener,sleeper=lambda _:None)
        http.limiter.wait = lambda:None
        with self.assertRaises(ScreenerError):
            http.request("GET",API+"/v2/market/status/NSE")
        self.assertEqual(opener.calls,4)

    def test_http_401_not_retried(self):
        class Unauthorized:
            calls = 0
            def open(self, req, timeout):
                self.calls += 1
                raise urllib.error.HTTPError(req.full_url,401,"unauthorized",{},io.BytesIO(b'{"errors":[{"message":"invalid"}]}'))
        opener = Unauthorized()
        http = Http({"UPSTOX_ACCESS_TOKEN":"fake"*40},self.state,self.config,opener=opener,sleeper=lambda _:None)
        http.limiter.wait = lambda:None
        with self.assertRaises(AuthenticationError):
            http.request("GET",API+"/v2/market/status/NSE")
        self.assertEqual(opener.calls,1)

    def test_quote_mapping_uses_instrument_key_not_dictionary_label(self):
        class FakeHttp:
            state = None
            config = DEFAULT_CONFIG
            def request(self,*a,**k):
                return {"data":{"NSE_EQ:SYMBOL":{"instrument_token":"NSE_EQ|ISIN","last_price":100}}}
        api = Upstox(FakeHttp())
        self.assertIn("NSE_EQ|ISIN",api.quotes(["NSE_EQ|ISIN"]))

    def test_funds_path_and_top_level_order_book_array(self):
        class FakeHttp:
            state = None
            config = DEFAULT_CONFIG
            urls = []
            def request(self,method,url,**kwargs):
                self.urls.append(url)
                if url.endswith('retrieve-all'):
                    return []
                if url.endswith('get-funds-and-margin'):
                    return {"data":{"equity":{"available_margin":0}}}
                return {"data":[]}
        http = FakeHttp()
        account = Upstox(http).account()
        self.assertTrue(account['complete'])
        self.assertIn(API+'/v2/user/get-funds-and-margin',http.urls)
        self.assertEqual(account['orders'],[])

    def test_partial_gtt_fill_not_marked_complete(self):
        payload = {"quantity":10,"rules":[]}
        ident = self.state.reserve_intent('partial','live','NSE_EQ|test','TEST','BUY',payload)
        self.state.set_intent(ident,'ACKNOWLEDGED',{'broker':{'data':{'gtt_order_ids':['test-gtt']}}})
        class FakeClient:
            def get_gtt(self,gid):
                return [{'rules':[{'strategy':'ENTRY','status':'OPEN','order_id':'test-order'},
                                  {'strategy':'TARGET','status':'SCHEDULED'}, {'strategy':'STOPLOSS','status':'SCHEDULED'}]}]
            def order_details(self,oid):
                return {'filled_quantity':3,'status':'open'}
        reconcile_live(FakeClient(),self.state,{'gtt':[],'orders':[]},quiet=True)
        row = self.state.intents('live')[0]
        self.assertEqual(row['status'],'PARTIAL')
        self.assertEqual(row['response']['filled_quantity'],3)

    def test_disappeared_gtt_not_assumed_closed(self):
        ident = self.state.reserve_intent('missing','live','NSE_EQ|test','TEST','BUY',{'quantity':10,'rules':[]})
        self.state.set_intent(ident,'ACKNOWLEDGED',{'broker':{'data':{'gtt_order_ids':['gone']}}})
        class FakeClient:
            def get_gtt(self,gid): return []
        reconcile_live(FakeClient(),self.state,{'gtt':[],'orders':[]},quiet=True)
        self.assertEqual(self.state.intents('live')[0]['status'],'NEEDS_RECONCILIATION')


    def test_reconstructed_date_only_release_keeps_end_of_day(self):
        c, ctx, _ = self.demo_company()
        c["quarterly"] = []
        c["annual"] = [{"period":"2024-12-31","available_on":"2025-01-03","share_basis_date":"2024-12-31","eps_diluted":10}]
        c["historical_snapshots"] = []
        snapshots = build_historical_snapshots(c,self.demo.clock)
        self.assertEqual(parse_timestamp(snapshots[0]["available_on"]).time(),dt.time(23,59,59))
        out = historical_model(c,ctx,[{"date":"2025-01-03","close":100},{"date":"2025-01-06","close":100}],
                               self.demo.clock,{**self.config,"minimum_historical_observations":1})
        self.assertEqual(out["pe_observations"],1)
        self.assertEqual(out["first_observation"],"2025-01-06")

    def test_today_date_only_filing_not_available_at_open(self):
        c = {"annual":[{"period":"2026-09-30","available_on":"2026-10-14","revenue":100}]}
        self.assertEqual(available_rows(c,"annual",self.demo.clock),[])
        after = self.demo.clock + dt.timedelta(days=1)
        self.assertEqual(len(available_rows(c,"annual",after)),1)

    def test_current_share_counts_adjust_for_bonus(self):
        c, ctx, q = self.demo_company()
        c["actions"] = [{"date":"2026-09-01","factor":2,"kind":"bonus"}]
        changed = financial_context(c,q,self.demo.clock,self.config)
        self.assertEqual(changed["shares"],ctx["shares"]*2)
        self.assertEqual(changed["diluted_shares"],ctx["diluted_shares"]*2)
        self.assertIn("factor 2",changed["share_adjustment_note"])

    def test_unknown_action_after_share_snapshot_blocks_count(self):
        c, _, q = self.demo_company()
        c["actions"] = [{"date":"2026-09-01","factor":None,"kind":"rights"}]
        changed = financial_context(c,q,self.demo.clock,self.config)
        self.assertIsNone(changed["shares"])
        self.assertIsNone(changed["diluted_shares"])

    def test_stale_eps_not_excused_by_fresh_revenue_quarter(self):
        c, ctx, _ = self.demo_company()
        ctx["earnings_dates"]["eps_diluted"] = "2024-03-31"
        ok, issues = freshness_checks(c,ctx,self.demo.clock,self.config)
        self.assertFalse(ok)
        self.assertTrue(any("Actual EPS" in issue for issue in issues))

    def test_provider_accounting_basis_mismatch_excluded(self):
        inst = self.demo.raw_instruments[1]
        bundle = {"annual_income":{"type":"standalone","units_in":"crore","income_statement":[
            {"category":"revenue","history":[{"period":"Mar 2026","value":100}]}]}}
        c = normalize_bundle(inst,bundle,self.config,"consolidated")
        self.assertEqual(c["annual"],[])
        self.assertTrue(any("avoid mixing" in warning for warning in c["warnings"]))

    def test_upstox_only_missing_fields_report_not_fabricated(self):
        inst = self.demo.raw_instruments[1]
        c = normalize_bundle(inst,self.demo.fundamental_bundle(inst["isin"],"consolidated"),self.config)
        q = self.demo.quotes([inst["instrument_key"]])[inst["instrument_key"]]
        ctx = financial_context(c,q,self.demo.clock,self.config)
        self.assertIsNone(ctx["shares"])
        out = dcf_model(c,ctx,[],[],self.demo.clock,self.config)
        self.assertIsNone(out["value"])
        self.assertIn("shares",out["reason"])

    def test_string_verification_flag_rejected(self):
        p = self.root/'flags.json'
        atomic_json(p,{"currency":"INR","unit":"rupees","companies":[{"isin":"test","manual_review_complete":"True"}]})
        with self.assertRaises(DataError): read_supplements(p)

    def test_filing_before_period_end_rejected(self):
        p = self.root/'dates.json'
        atomic_json(p,{"currency":"INR","unit":"rupees","companies":[{"isin":"test","annual":[
            {"period":"2026-03-31","available_on":"2026-02-28"}]}]})
        with self.assertRaises(DataError): read_supplements(p)

    def test_duplicate_fiscal_period_rejected(self):
        p = self.root/'duplicate.json'
        atomic_json(p,{"currency":"INR","unit":"rupees","companies":[{"isin":"test","annual":[
            {"period":"2026-03-31"},{"period":"2026-03-31"}]}]})
        with self.assertRaises(DataError): read_supplements(p)

    def test_misspelled_model_weight_rejected(self):
        cfg = {**self.config,"model_weights":{"DCF":1}}
        with self.assertRaises(DataError): validate_config(cfg)

    def test_relative_rank_does_not_override_absolute_bad_cashflow(self):
        ctx = {"annual":[{"cfo":-100},{"cfo":-20},{"cfo":-1}]}
        self.assertFalse(absolute_cash_flow_safety(ctx,self.config))

    def test_debt_free_and_overleveraged_absolute_filters(self):
        good = {"balance":{"debt":0,"cash":1},"current":{}}
        bad = {"balance":{"debt":100,"cash":0},"current":{"ebit":5,"interest_expense":4,"ebitda":8}}
        self.assertTrue(absolute_debt_safety(good,self.config))
        self.assertFalse(absolute_debt_safety(bad,self.config))

    def test_low_peer_scenario_negative_equity_gets_computed_zero(self):
        target = {"isin":"target","sector":"Manufacturing","industry":"Manufacturing","symbol":"TARGET"}
        companies = {"target":target}
        contexts = {}
        for i, multiple in enumerate([1,2,10,11]):
            isin = f"peer{i}"
            companies[isin] = {"isin":isin,"sector":"Manufacturing","industry":"Manufacturing","symbol":isin}
            contexts[isin] = {"tier":"small","market_cap_crore":1000,"ev_ebitda":multiple}
        ctx = {"tier":"small","current":{"ebitda":1},"bridge_known":True,"diluted_shares":1,
               "balance":{"debt":3,"cash":0,"minority_interest":0,"preferred_equity":0,"non_operating_assets":0}}
        contexts["target"] = ctx
        model = peers_model(target,ctx,contexts,companies,{**self.config,"peer_multiple_weights":{"ev_ebitda":1}})
        self.assertGreater(model["value"],0)
        self.assertEqual(model["low"],0)

    def test_inactive_gtt_exits_not_claimed_protective(self):
        ident = self.state.reserve_intent('inactive','live','NSE_EQ|test','TEST','BUY',{'quantity':10,'rules':[]})
        self.state.set_intent(ident,'ACKNOWLEDGED',{'broker':{'data':{'gtt_order_ids':['test-gtt']}}})
        class FakeClient:
            def get_gtt(self,gid):
                return [{'rules':[{'strategy':'ENTRY','status':'COMPLETED','order_id':'o'},
                                  {'strategy':'TARGET','status':'INACTIVE'},{'strategy':'STOPLOSS','status':'INACTIVE'}]}]
            def order_details(self,oid): return {'filled_quantity':10,'status':'complete'}
        reconcile_live(FakeClient(),self.state,{'gtt':[],'orders':[]},quiet=True)
        row = self.state.intents('live')[0]
        self.assertFalse(row['response']['protection_verified'])
        self.assertEqual(row['status'],'FILLED_PROTECTION_UNVERIFIED')

    def test_acknowledged_entry_not_rejected_after_read_failure(self):
        from unittest import mock
        runner = self.loaded()
        result = runner.cycle(display=False)['DEMO00000000']
        plan = result['plan']
        self.args.capital = 1_000_000
        self.args.cash_budget = 1_000_000
        demo = self.demo
        class FakeBroker:
            is_demo = False
            account_calls = 0
            writes = 0
            def quotes(self,keys): return demo.quotes(keys)
            def market(self): return demo.market()
            def account(self):
                self.account_calls += 1
                if self.account_calls == 1: return demo.account()
                raise ScreenerError('simulated read-side failure after acknowledgement')
            def place_gtt(self,payload):
                self.writes += 1
                return {'data':{'gtt_order_ids':['MOCK-ONLY-NOT-A-REAL-ID']}}
        broker = FakeBroker()
        with mock.patch(__name__+'.confirm_mutation',return_value=True), mock.patch(__name__+'.now_ist',return_value=demo.clock), contextlib.redirect_stdout(io.StringIO()):
            execute_entry(broker,self.state,self.args,{},result,plan,runner.instruments[result['isin']],
                          runner.companies,{},runner.adjusted[result['isin']],self.config)
        row = self.state.intents('live')[0]
        self.assertEqual(broker.writes,1)
        self.assertEqual(row['status'],'NEEDS_RECONCILIATION')
        self.assertIn('gtt_order_ids',row['response']['broker']['data'])


    def test_empty_supplement_does_not_erase_new_broker_bonus(self):
        c, _, _ = self.demo_company()
        c["actions"] = [{"date":"2026-09-01","kind":"Bonus issue","factor":2}]
        merged = merge_supplement(c,{"actions":[],"actions_coverage_verified":True},"consolidated")
        self.assertEqual(merged["actions"][0]["factor"],2)

    def test_supplement_corrects_unresolved_split_without_double_counting(self):
        c, _, _ = self.demo_company()
        c["actions"] = [{"date":"2026-09-01","kind":"Stock split","factor":None}]
        merged = merge_supplement(c,{"actions":[{"date":"2026-09-01","kind":"split","factor":2}]},"consolidated")
        self.assertEqual(len(merged["actions"]),1)
        self.assertEqual(merged["actions"][0]["factor"],2)

    def test_review_requires_update_after_new_financial_period(self):
        c, ctx, _ = self.demo_company()
        self.assertTrue(review_is_current(c,ctx,self.demo.clock))
        ctx["quarterly"].append({"period":"2026-09-30"})
        self.assertFalse(review_is_current(c,ctx,self.demo.clock))

    def test_mixed_sector_taxonomy_does_not_understate_external_holdings(self):
        c, _, _ = self.demo_company()
        c["provider_sector"] = "VendorDetailedSector"
        c["sector"] = "UserBroadSector"
        account = self.demo.account()
        account["holdings"] = [{"isin":"external","quantity":10,"last_price":100}]
        view = portfolio_view(account,{c["isin"]:c},self.demo,{})
        self.assertFalse(view["complete"])


    @staticmethod
    def public_listing_regression_fixture():
        # Minimal actual public BOD / alternate-series records observed on
        # 2026-10-15. Public instrument identities, not credentials/account data.
        records = [
            ("ITC", "INE154A01025", "1660", "5582"),
            ("HINDUNILVR", "INE030A01027", "1394", "4765"),
            ("DABUR", "INE016A01026", "772", "4700"),
            ("MARICO", "INE196A01026", "4067", "5645"),
            ("JYOTHYLAB", "INE668F01031", "15146", "15150"),
            ("EMAMILTD", "INE548C01032", "13517", "13520"),
        ]
        master, suspended = [], []
        for symbol, isin, eq_token, be_token in records:
            row = {"segment": "NSE_EQ", "instrument_type": "EQ", "security_type": "NORMAL",
                   "isin": isin, "instrument_key": "NSE_EQ|" + isin,
                   "trading_symbol": symbol, "exchange_token": eq_token}
            master.append(row)
            suspended.append({**row, "instrument_type": "BE", "exchange_token": be_token})
        return master, suspended

    def test_all_six_real_default_symbols_survive_inactive_alternate_series(self):
        master, suspended = self.public_listing_regression_fixture()
        out, counts = build_equity_universe(master, suspended)
        self.assertEqual({r["trading_symbol"] for r in out.values()}, set(DEFAULT_SYMBOLS.split(",")))
        self.assertEqual(counts["eligible_equities"], 6)
        self.assertEqual(counts["excluded_exact_suspensions"], 0)

    def test_plain_isin_blacklist_reproduces_original_bug(self):
        master, suspended = self.public_listing_regression_fixture()
        bad = {r["isin"] for r in suspended}
        self.assertEqual([r for r in master if r["isin"] not in bad], [])
        self.assertEqual(len(build_equity_universe(master, suspended)[0]), 6)

    def test_plain_instrument_key_blacklist_would_also_be_wrong(self):
        master, suspended = self.public_listing_regression_fixture()
        bad = {r["instrument_key"] for r in suspended}
        self.assertEqual([r for r in master if r["instrument_key"] not in bad], [])
        self.assertEqual(len(build_equity_universe(master, suspended)[0]), 6)

    def test_same_key_inactive_af_bl_rl_tl_iq_do_not_suspend_eq(self):
        master, _ = self.public_listing_regression_fixture()
        active = master[0]
        # Same key/ISIN intentionally, alternate series must remain distinct.
        suspended = [{**active, "instrument_type": series} for series in ["AF", "BL", "RL", "TL", "IQ"]]
        out, _ = build_equity_universe([active], suspended)
        self.assertIn(active["isin"], out)

    def test_actual_matching_eq_suspension_is_still_excluded(self):
        master, suspended = self.public_listing_regression_fixture()
        suspended.append(dict(master[0]))
        out, counts = build_equity_universe(master, suspended)
        self.assertNotIn(master[0]["isin"], out)
        self.assertEqual(len(out), 5)
        self.assertEqual(counts["excluded_exact_suspensions"], 1)

    def test_other_exchange_suspension_does_not_suspend_nse(self):
        master, _ = self.public_listing_regression_fixture()
        active = master[0]
        suspended = [{**active, "segment": "BSE_EQ", "instrument_key": "BSE_EQ|" + active["isin"]}]
        self.assertIn(active["isin"], build_equity_universe([active], suspended)[0])

    def test_same_eq_key_old_different_exchange_token_is_not_current_listing(self):
        master, suspended = self.public_listing_regression_fixture()
        active = master[0]
        old = {**suspended[0], "instrument_type": "EQ"}
        self.assertIn(active["isin"], build_equity_universe([active], [old])[0])

    def test_reused_exchange_token_different_security_not_blacklisted(self):
        master, _ = self.public_listing_regression_fixture()
        old = {**master[1], "exchange_token": master[0]["exchange_token"]}
        out, _ = build_equity_universe([master[0]], [old])
        self.assertIn(master[0]["isin"], out)

    def test_tokenless_suspension_matches_only_same_eq_scope_conservatively(self):
        master, _ = self.public_listing_regression_fixture()
        suspended = dict(master[0]); suspended.pop("exchange_token")
        self.assertNotIn(master[0]["isin"], build_equity_universe(master, [suspended])[0])

    def test_tokenless_alternate_series_does_not_blacklist_eq(self):
        master, suspended = self.public_listing_regression_fixture()
        suspended[0].pop("exchange_token")
        self.assertIn(master[0]["isin"], build_equity_universe(master, suspended)[0])

    def test_security_type_pca_is_not_relaxed_by_fix(self):
        master, _ = self.public_listing_regression_fixture()
        candidate = {**master[0], "security_type": "PCA"}
        out, counts = build_equity_universe([candidate], [])
        self.assertFalse(out)
        self.assertEqual(counts["excluded_security_type"], 1)

    def test_listing_token_integer_and_decimal_string_match(self):
        master, _ = self.public_listing_regression_fixture()
        active = {**master[0], "exchange_token": 1660}
        suspended = {**master[0], "exchange_token": "1660.0"}
        self.assertFalse(build_equity_universe([active], [suspended])[0])

    def test_instrument_payload_shape_error_is_explicit(self):
        with self.assertRaises(DataError):
            build_equity_universe({"data": []}, [])

    def test_diagnostics_only_call_public_instruments(self):
        master, suspended = self.public_listing_regression_fixture()
        class PublicOnly:
            is_demo = False
            calls = 0
            def instruments(self):
                self.calls += 1
                return master, suspended
            def validate_token(self):
                raise AssertionError("Diagnostic must not authenticate")
            def account(self):
                raise AssertionError("Diagnostic must not read account")
        client = PublicOnly()
        args = make_parser().parse_args(["--diagnose-instruments", "--symbols", "ITC,DABUR,MARICO",
                                         "--output", str(self.root / "instrument-report")])
        with contextlib.redirect_stdout(io.StringIO()):
            report = diagnose_instruments(client, args)
        self.assertTrue(report["public_data_only"])
        self.assertEqual(client.calls, 1)
        self.assertTrue(all(row["status"] == "FOUND" for row in report["requested_symbols"]))

    def test_real_shape_filter_integrates_with_full_screener_load(self):
        master, suspended = self.public_listing_regression_fixture()
        master.append({"segment": "NSE_INDEX", "name": "Nifty 50", "instrument_key": "NSE_INDEX|Nifty 50"})
        class ListingClient:
            is_demo = False
            loaded = []
            def validate_token(self): return None
            def instruments(self): return master, suspended
            def fundamental_bundle(self, isin, statement_type):
                self.loaded.append(isin)
                return {"errors": [], "fetched_at": now_ist().isoformat()}
            def candles(self, *args): return []
        args = make_parser().parse_args(["--symbols", "ITC,DABUR,MARICO", "--quiet", "--no-peers"])
        client = ListingClient()
        runner = Screener(args, {}, self.config, self.state, client)
        runner.load()
        self.assertEqual({runner.instruments[i]["trading_symbol"] for i in runner.targets}, {"ITC", "DABUR", "MARICO"})
        self.assertEqual(len(client.loaded), 3)


def run_self_tests() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ScreenerTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    try:
        raise SystemExit(cli())
    except KeyboardInterrupt:
        print("\nStopped. Existing broker orders/GTTs were NOT cancelled; verify them in Upstox.", file=sys.stderr)
        raise SystemExit(130)
    except (ScreenerError, OSError, ValueError, sqlite3.Error) as exc:
        try:
            redactor = Redactor(load_env(Path("env.txt")))
        except Exception:
            redactor = lambda value: type(value).__name__
        print("ERROR: " + redactor(exc), file=sys.stderr)
        raise SystemExit(2)
