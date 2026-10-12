#!/usr/bin/env python3
"""
Gold / Macro Dashboard — refreshes every 60 seconds.

Data integrity layers (applied in order):
  1. Contract-chain filter — future-dated only, deduped by instrument_key,
     within a max-expiry window (default 60 days).
  2. Plausibility range — hard numeric bounds per instrument. Rejects
     obviously wrong values (e.g. USD/INR 95.95) regardless of source.
  3. Reference cross-check (fail-closed) — Upstox futures must agree with a
     yfinance reference within tolerance. If the reference cannot be
     fetched, the Upstox contract is REJECTED (not accepted-by-default).
  4. Kind validation — spot/futures require positive price; only
     Upstox-sourced futures require expiry metadata.
  5. Age validation — kind-aware thresholds. yfinance falls back to `daily`
     kind when intraday 1m data is unavailable, so daily-close age is
     compared against the daily limit, not the futures/spot limit.

Run:                  python monitor.py
Verbose HTTP:         python monitor.py --debug
Refresh instruments:  python monitor.py --refresh-instruments
"""

from __future__ import annotations

import gzip
import json
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

try:
    import yfinance as yf
    _HAS_YF = True
except ImportError:
    _HAS_YF = False

try:
    from cme_fedwatch import get_probabilities as fedwatch_probs
    _HAS_FEDWATCH = True
except ImportError:
    _HAS_FEDWATCH = False


# ═════════════════════════════════════════════════════════════════════════════
# Configuration
# ═════════════════════════════════════════════════════════════════════════════
REFRESH_SECONDS = 60
HTTP_TIMEOUT = 20
# Prefer local env.txt; fall back to repo-root env.txt (same as main engine).
_ENV_LOCAL = Path(__file__).with_name("env.txt")
_ENV_ROOT = Path(__file__).resolve().parent.parent / "env.txt"
ENV_FILE = _ENV_LOCAL if _ENV_LOCAL.exists() else _ENV_ROOT

CACHE_DIR = Path.home() / ".cache" / "upstox_instruments"
CACHE_MAX_AGE = timedelta(hours=24)

UPSTOX_ASSETS = "https://assets.upstox.com/market-quote/instruments/exchange"
INSTRUMENT_FILES = ["NSE.json.gz", "MCX.json.gz", "BSE.json.gz", "complete.json.gz"]
UPSTOX_API = "https://api.upstox.com"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFII10"

DEBUG = "--debug" in sys.argv
FORCE_REFRESH = "--refresh-instruments" in sys.argv

MAX_EXPIRY_DAYS = 60

MAX_AGE = {
    "spot": 300,             # 5 min
    "futures": 3600,         # 1 h
    "daily": 7 * 86400,      # 7 days
    "expectation": 3 * 86400,
    "unknown": 86400,
}


# ═════════════════════════════════════════════════════════════════════════════
# Env
# ═════════════════════════════════════════════════════════════════════════════
def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        sys.exit(f"[fatal] env file not found: {path}")
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


# ═════════════════════════════════════════════════════════════════════════════
# Instrument cache
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class InstrumentCache:
    by_symbol: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    loaded_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def _download_one(self, filename: str) -> list[dict[str, Any]] | None:
        local = CACHE_DIR / filename
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        needs_download = FORCE_REFRESH or not local.exists()
        if not needs_download:
            age = datetime.now(timezone.utc) - datetime.fromtimestamp(
                local.stat().st_mtime, tz=timezone.utc
            )
            if age > CACHE_MAX_AGE:
                needs_download = True

        if needs_download:
            url = f"{UPSTOX_ASSETS}/{filename}"
            if DEBUG:
                print(f"[DEBUG] downloading {url}")
            try:
                r = requests.get(url, timeout=HTTP_TIMEOUT)
                r.raise_for_status()
                local.write_bytes(r.content)
            except Exception as exc:
                self.errors.append(f"{filename}: download failed ({type(exc).__name__})")
                if local.exists():
                    return self._parse(local)
                return None

        return self._parse(local)

    def _parse(self, path: Path) -> list[dict[str, Any]]:
        try:
            raw = gzip.decompress(path.read_bytes())
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except Exception as exc:
            self.errors.append(f"{path.name}: parse failed ({type(exc).__name__})")
            return []

    def load(self) -> None:
        for filename in INSTRUMENT_FILES:
            rows = self._download_one(filename)
            if not rows:
                continue
            self.loaded_files.append(filename)
            for row in rows:
                for key_field in ("trading_symbol", "name", "short_name"):
                    val = row.get(key_field)
                    if not val:
                        continue
                    self.by_symbol.setdefault(str(val).strip().upper(), []).append(row)
            if DEBUG:
                print(f"[DEBUG] {filename}: {len(rows):,} rows indexed")

    def find_futures(
        self,
        symbol_prefix: str,
        segment: str,
        instrument_type: str = "FUT",
        max_n: int = 3,
        max_expiry_days: int = MAX_EXPIRY_DAYS,
    ) -> list[dict[str, Any]]:
        """Return up to N nearest future-dated contracts whose trading_symbol
        STARTS WITH symbol_prefix (strict prefix, not substring match)."""
        prefix = symbol_prefix.upper()
        now = datetime.now(timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        cutoff_ms = int((now + timedelta(days=max_expiry_days)).timestamp() * 1000)

        by_key: dict[str, tuple[int, dict[str, Any]]] = {}
        for rows in self.by_symbol.values():
            for row in rows:
                if row.get("segment") != segment:
                    continue
                if instrument_type and row.get("instrument_type") != instrument_type:
                    continue

                # Strict prefix match on trading_symbol (not name/short_name)
                ts = str(row.get("trading_symbol") or "").strip().upper()
                if not ts.startswith(prefix):
                    continue

                ikey = row.get("instrument_key")
                if not ikey or ikey in by_key:
                    continue

                try:
                    exp = int(row.get("expiry", 0))
                except (TypeError, ValueError):
                    continue
                if exp <= now_ms or exp > cutoff_ms:
                    continue

                by_key[ikey] = (exp, row)

        ordered = sorted(by_key.values(), key=lambda x: x[0])
        return [row for _, row in ordered[:max_n]]


# ═════════════════════════════════════════════════════════════════════════════
# Upstox REST client
# ═════════════════════════════════════════════════════════════════════════════
class UpstoxClient:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        self.last_error: str = ""

    def _call(self, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
        try:
            r = self.session.get(url, params=params, timeout=HTTP_TIMEOUT)
            if DEBUG:
                print(f"\n[DEBUG] GET {r.url}")
                print(f"[DEBUG] status={r.status_code}")
                print(f"[DEBUG] body={r.text[:600]}\n")
            if r.status_code != 200:
                try:
                    errs = r.json().get("errors") or []
                    self.last_error = (
                        f"HTTP {r.status_code}: {errs[0].get('errorCode')}"
                        if errs else f"HTTP {r.status_code}"
                    )
                except Exception:
                    self.last_error = f"HTTP {r.status_code}"
                return None
            return r.json()
        except requests.RequestException as exc:
            self.last_error = f"{type(exc).__name__}"
            return None

    def ltp(self, keys: list[str]) -> dict[str, dict[str, Any]]:
        if not keys:
            return {}
        body = self._call(f"{UPSTOX_API}/v3/market-quote/ltp",
                          {"instrument_key": ",".join(keys)})
        if not body or body.get("status") != "success":
            return {}
        out: dict[str, dict[str, Any]] = {}
        for resp_key, v in (body.get("data") or {}).items():
            out[resp_key] = v
            token = v.get("instrument_token")
            if token:
                out[token] = v
                out[token.replace("|", ":")] = v
        return out


# ═════════════════════════════════════════════════════════════════════════════
# Observation model
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class Observation:
    name: str
    value: str
    change: str = ""
    source: str = ""
    source_ts: str = ""
    error: str = ""
    numeric: float | None = None
    kind: str = "unknown"
    age_seconds: float | None = None
    valid: bool = False
    reject_reason: str = ""
    meta: dict[str, str] = field(default_factory=dict)


@dataclass
class Snapshot:
    ts_utc: str = ""
    observations: list[Observation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ═════════════════════════════════════════════════════════════════════════════
# Validation
# ═════════════════════════════════════════════════════════════════════════════
def validate(obs: Observation) -> Observation:
    if obs.error:
        obs.valid = False
        obs.reject_reason = obs.error
        return obs

    if obs.kind in ("spot", "futures"):
        if obs.numeric is None:
            obs.valid = False
            obs.reject_reason = "no numeric value"
            return obs
        if obs.numeric <= 0:
            obs.valid = False
            obs.reject_reason = f"rejected zero/negative price ({obs.numeric})"
            return obs

    # Expiry metadata only enforced for Upstox-sourced futures.
    if obs.kind == "futures" and obs.source.startswith("upstox:"):
        if "expiry" not in obs.meta:
            obs.valid = False
            obs.reject_reason = "upstox futures without expiry metadata"
            return obs

    if obs.age_seconds is not None:
        limit = MAX_AGE.get(obs.kind, MAX_AGE["unknown"])
        if obs.age_seconds > limit:
            obs.valid = False
            obs.reject_reason = (
                f"stale: age {_fmt_age(obs.age_seconds)} > "
                f"limit {_fmt_age(limit)}"
            )
            return obs

    obs.valid = True
    return obs


def _fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


# ═════════════════════════════════════════════════════════════════════════════
# Formatting helpers
# ═════════════════════════════════════════════════════════════════════════════
def _fmt_num(x: Any) -> str:
    if x is None:
        return "—"
    if isinstance(x, (int, float)):
        if abs(x) >= 1000:
            return f"{x:,.2f}"
        if abs(x) >= 1:
            return f"{x:.4f}"
        return f"{x:.6f}"
    return str(x)


def _pct(last: float, prev: float) -> str:
    if not prev:
        return ""
    return f"{'+' if (last - prev) >= 0 else ''}{(last - prev) / prev * 100:.3f}%"


def _fmt_expiry_ms(expiry_ms: Any) -> str:
    try:
        ms = int(expiry_ms)
        if ms > 10_000_000_000:
            ms = ms / 1000
        return datetime.fromtimestamp(ms, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return "?"


# ═════════════════════════════════════════════════════════════════════════════
# yfinance — intraday first, daily fallback with kind adjusted
# ═════════════════════════════════════════════════════════════════════════════
def _yf_fetch_last(ticker: str, interval: str, period: str) -> tuple[float, float, float] | None:
    """Return (last_close, prev_close, age_seconds) or None on failure."""
    if not _HAS_YF:
        return None
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period=period, interval=interval, auto_adjust=False)
        if hist.empty:
            return None
        last = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else last
        dt = hist.index[-1].to_pydatetime()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        return last, prev, age
    except Exception:
        return None


def fetch_yf(
    ticker: str,
    name: str,
    kind_hint: str = "spot",
    label: str | None = None,
    prefer_intraday: bool = True,
) -> Observation:
    """Fetch from yfinance. Tries 1-minute intraday first (if prefer_intraday),
    then daily. The observation's `kind` reflects whichever fetch succeeded:
    intraday → kind_hint (spot/futures), daily → 'daily'."""
    if not _HAS_YF:
        return Observation(name=name, value="—", source="yfinance",
                           error="pip install yfinance", kind=kind_hint)

    src = f"yf:{ticker}" if label is None else f"yf:{ticker} ({label})"

    # 1. Intraday 1-minute
    if prefer_intraday:
        res = _yf_fetch_last(ticker, interval="1m", period="1d")
        if res is not None:
            last, prev, age = res
            # Only accept intraday if fresh enough for the kind hint
            if age <= MAX_AGE.get(kind_hint, 3600):
                obs = Observation(
                    name=name,
                    value=f"{last:,.4f}" if last >= 100 else f"{last:.4f}",
                    change=_pct(last, prev),
                    source=f"{src} [1m]",
                    source_ts=datetime.fromtimestamp(
                        time.time() - age, tz=timezone.utc
                    ).strftime("%H:%M:%S"),
                    numeric=last,
                    kind=kind_hint,
                    age_seconds=age,
                    meta={"ticker": ticker, "resolution": "1m"},
                )
                return validate(obs)
            if DEBUG:
                print(f"[DEBUG] {name} {ticker} 1m age {_fmt_age(age)} > "
                      f"limit {_fmt_age(MAX_AGE.get(kind_hint, 3600))} — "
                      f"falling back to daily")

    # 2. Daily fallback — kind becomes 'daily'
    res = _yf_fetch_last(ticker, interval="1d", period="5d")
    if res is None:
        return Observation(name=name, value="—", source=src,
                           error="empty history", kind=kind_hint)

    last, prev, age = res
    obs = Observation(
        name=name,
        value=f"{last:,.4f}" if last >= 100 else f"{last:.4f}",
        change=_pct(last, prev),
        source=f"{src} [1d]",
        source_ts=datetime.fromtimestamp(
            time.time() - age, tz=timezone.utc
        ).strftime("%Y-%m-%d"),
        numeric=last,
        kind="daily",                       # reflects actual resolution
        age_seconds=age,
        meta={"ticker": ticker, "resolution": "1d"},
    )
    return validate(obs)


# ═════════════════════════════════════════════════════════════════════════════
# Reference + Upstox chain
# ═════════════════════════════════════════════════════════════════════════════
def fetch_reference(ticker: str) -> float | None:
    """Latest yfinance price for cross-check. Intraday 1m preferred, daily fallback."""
    res = _yf_fetch_last(ticker, interval="1m", period="1d")
    if res is not None:
        return res[0]
    res = _yf_fetch_last(ticker, interval="1d", period="5d")
    if res is not None:
        return res[0]
    return None


def fetch_upstox_chain(
    client: UpstoxClient,
    name: str,
    contract_rows: list[dict[str, Any]],
    fallback_yf: str,
    fallback_label: str,
    fallback_kind: str,
    plausibility_min: float,
    plausibility_max: float,
    reference_price: float | None,
    tolerance_pct: float,
) -> Observation:
    """Try contracts; accept only if plausible AND (if required) within
    tolerance of the reference. Fail-closed: if a reference is required but
    missing, no contract is accepted."""

    tried: list[str] = []
    reasons: list[str] = []

    for row in contract_rows:
        key = row.get("instrument_key")
        if not key or key in tried:
            continue
        tried.append(key)
        quotes = client.ltp([key])
        q = quotes.get(key) or quotes.get(key.replace("|", ":"))

        if not q:
            reasons.append(f"{key}:empty")
            continue

        lp = q.get("last_price")
        if not isinstance(lp, (int, float)) or lp <= 0:
            reasons.append(f"{key}:nonpositive({lp})")
            continue

        # Hard plausibility range
        if not (plausibility_min <= lp <= plausibility_max):
            reasons.append(
                f"{key}:out_of_range({lp}, {plausibility_min}-{plausibility_max})"
            )
            if DEBUG:
                print(f"[DEBUG] {name} {key}: {lp} outside plausibility "
                      f"[{plausibility_min}, {plausibility_max}] — reject")
            continue

        # Fail-closed reference cross-check
        if tolerance_pct > 0:
            if reference_price is None:
                reasons.append(f"{key}:no_reference_available")
                if DEBUG:
                    print(f"[DEBUG] {name} {key}: reference unavailable — "
                          f"fail-closed reject")
                continue
            deviation = abs(lp - reference_price) / reference_price
            if deviation > tolerance_pct:
                reasons.append(
                    f"{key}:deviation({deviation*100:.2f}%>"
                    f"{tolerance_pct*100:.2f}%)"
                )
                if DEBUG:
                    print(f"[DEBUG] {name} {key}: {lp} vs ref "
                          f"{reference_price} = {deviation*100:.2f}% > "
                          f"tol {tolerance_pct*100:.2f}% — reject")
                continue

        # Accept
        ts_str = ""
        age: float | None = None
        ltt = q.get("ltt") or q.get("timestamp")
        if isinstance(ltt, (int, float)) and ltt > 0:
            try:
                sec = ltt / 1000 if ltt > 10_000_000_000 else ltt
                dt = datetime.fromtimestamp(sec, tz=timezone.utc)
                ts_str = dt.strftime("%H:%M:%S")
                age = (datetime.now(timezone.utc) - dt).total_seconds()
            except Exception:
                pass

        cp = q.get("cp")
        obs = Observation(
            name=name,
            value=_fmt_num(lp),
            change=_pct(lp, cp) if isinstance(cp, (int, float)) and cp else "",
            source=f"upstox:{key}",
            source_ts=ts_str,
            numeric=float(lp),
            kind="futures",
            age_seconds=age,
            meta={
                "instrument_key": key,
                "expiry": _fmt_expiry_ms(row.get("expiry")),
                "symbol": str(row.get("trading_symbol") or ""),
                "rejected_before": ",".join(reasons),
            },
        )
        if reference_price:
            obs.meta["reference"] = f"{reference_price:.4f}"
        return validate(obs)

    # Nothing accepted — fallback
    if DEBUG:
        print(f"[DEBUG] {name}: no contract accepted. Reasons: {reasons}. "
              f"Falling back to {fallback_yf}")
    obs = fetch_yf(
        fallback_yf, name,
        kind_hint=fallback_kind,
        label=fallback_label or fallback_kind,
        prefer_intraday=True,
    )
    obs.meta["upstox_tried"] = ",".join(tried)
    obs.meta["upstox_rejections"] = ";".join(reasons)
    obs.meta["fallback"] = "upstox chain rejected"
    return obs


# ═════════════════════════════════════════════════════════════════════════════
# FRED / FedWatch
# ═════════════════════════════════════════════════════════════════════════════
def fetch_fred_real_yield() -> Observation:
    try:
        r = requests.get(FRED_CSV_URL, timeout=HTTP_TIMEOUT,
                         headers={"User-Agent": "monitor/1.0"})
        r.raise_for_status()
        lines = [ln for ln in r.text.splitlines() if ln.strip()]
        if len(lines) < 2:
            return Observation(name="US10Y_REAL", value="—", source="FRED",
                               error="empty csv", kind="daily")
        last = lines[-1].split(",")
        if len(last) < 2 or last[1] in (".", "", "NA"):
            return Observation(name="US10Y_REAL", value="—", source="FRED",
                               error="latest N/A", kind="daily")
        val = float(last[1])
        obs = Observation(
            name="US10Y_REAL",
            value=f"{val:.2f}%",
            source="FRED:DFII10",
            source_ts=last[0],
            numeric=val,
            kind="daily",
            meta={"series": "DFII10", "unit": "percent"},
        )
        try:
            dt = datetime.fromisoformat(last[0]).replace(tzinfo=timezone.utc)
            obs.age_seconds = (datetime.now(timezone.utc) - dt).total_seconds()
        except Exception:
            pass
        return validate(obs)
    except Exception as exc:
        return Observation(name="US10Y_REAL", value="—", source="FRED",
                           error=f"{type(exc).__name__}", kind="daily")


def fetch_fedwatch() -> Observation:
    if not _HAS_FEDWATCH:
        return Observation(name="FED_WATCH", value="—", source="cme-fedwatch",
                           error="pip install cme-fedwatch", kind="expectation")
    try:
        data = fedwatch_probs("next")
        meetings = data.get("meetings") or []
        if not meetings:
            return Observation(name="FED_WATCH", value="—",
                               source="cme-fedwatch", error="no meetings",
                               kind="expectation")
        m = meetings[0]
        probs = m.get("probabilities") or {}
        if not probs:
            return Observation(name="FED_WATCH", value="—",
                               source="cme-fedwatch", error="no probs",
                               kind="expectation")
        label, prob = max(probs.items(), key=lambda kv: float(kv[1]))
        return validate(Observation(
            name="FED_WATCH",
            value=f"{label} @ {float(prob):.1f}%",
            source="cme-fedwatch",
            source_ts=str(m.get("date", "")),
            numeric=float(prob),
            kind="expectation",
            meta={"meeting": str(m.get("date", "")),
                  "contract": str(m.get("contract", ""))},
        ))
    except Exception as exc:
        return Observation(name="FED_WATCH", value="—",
                           source="cme-fedwatch",
                           error=f"{type(exc).__name__}", kind="expectation")


# ═════════════════════════════════════════════════════════════════════════════
# Rendering
# ═════════════════════════════════════════════════════════════════════════════
RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
GREEN = "\033[32m"; RED = "\033[31m"; YELLOW = "\033[33m"; CYAN = "\033[36m"
BG_GREEN = "\033[42m\033[30m"; BG_RED = "\033[41m\033[97m"; BG_YELLOW = "\033[43m\033[30m"


def _paint(change: str) -> str:
    if not change:
        return ""
    if change.startswith("+"):
        return f"{GREEN}{change}{RESET}"
    if change.startswith("-"):
        return f"{RED}{change}{RESET}"
    return change


def _status_icon(o: Observation) -> str:
    if o.error:
        return f"{RED}✗{RESET}"
    if not o.valid:
        return f"{YELLOW}!{RESET}"
    return f"{GREEN}✓{RESET}"


def _kind_tag(o: Observation) -> str:
    color = {
        "spot": CYAN, "futures": CYAN,
        "daily": YELLOW, "expectation": YELLOW,
    }.get(o.kind, DIM)
    return f"{color}[{o.kind}]{RESET}"


def render_status_banner(snap: Snapshot) -> str:
    live = [o for o in snap.observations if o.kind in ("spot", "futures")]
    if not live:
        return f"  {BG_RED} HOLD — no live data rows {RESET}"
    valid_live = [o for o in live if o.valid and not o.error]
    invalid_live = [o for o in live if not o.valid or o.error]
    ratio = len(valid_live) / len(live)
    if ratio == 1.0:
        return (f"  {BG_GREEN} TRADEABLE — "
                f"{len(valid_live)}/{len(live)} live rows valid {RESET}")
    if ratio >= 0.5:
        return (f"  {BG_YELLOW} PARTIAL — {len(invalid_live)} live row(s) invalid, "
                f"signals disabled {RESET}")
    return (f"  {BG_RED} HOLD — only {len(valid_live)}/{len(live)} live rows "
            f"valid {RESET}")


def render(snap: Snapshot) -> str:
    width = 108
    line = "─" * width
    out: list[str] = []
    out.append(f"{BOLD}╔{'═' * width}╗{RESET}")
    out.append(f"{BOLD}║{'GOLD / MACRO DASHBOARD'.center(width)}║{RESET}")
    out.append(f"{BOLD}╚{'═' * width}╝{RESET}")
    out.append(f"  {DIM}Observation time (UTC): {snap.ts_utc}{RESET}")
    out.append("")
    out.append(render_status_banner(snap))
    out.append("")
    out.append(
        f"  {BOLD}{'St':<2} {'Indicator':<14} {'Value':>16}  {'Change':>10}  "
        f"{'Kind':<14} {'Source':<34} {'Info':<24}{RESET}"
    )
    out.append(f"  {line}")

    for o in snap.observations:
        st = _status_icon(o)
        kind_tag = _kind_tag(o)

        if o.error:
            val = f"{RED}{'ERROR':>16}{RESET}"
            change = ""
        else:
            val = f"{o.value[:16]:>16}"
            change = _paint(o.change[:10])

        if o.valid:
            bits: list[str] = []
            if o.age_seconds is not None:
                bits.append(f"age {_fmt_age(o.age_seconds)}")
            if "expiry" in o.meta:
                bits.append(f"exp {o.meta['expiry']}")
            info_col = f"{DIM}{' '.join(bits)[:24]}{RESET}"
        else:
            reason = o.reject_reason or o.error or "invalid"
            info_col = f"{RED}{reason[:24]}{RESET}"

        out.append(
            f"  {st}  {o.name[:14]:<14} {val}  {change:>10}  "
            f"{kind_tag:<14} {o.source[:34]:<34} {info_col}"
        )

    out.append(f"  {line}")
    for n in snap.notes:
        out.append(f"  {YELLOW}• {n}{RESET}")
    out.append("")
    out.append(f"  {DIM}Legend: ✓ valid  ! invalid  ✗ error   "
               f"[spot]/[futures]/[daily]/[expectation]{RESET}")
    out.append(f"  {DIM}Refreshing every {REFRESH_SECONDS}s — Ctrl+C to stop{RESET}")
    out.append("")
    return "\n".join(out)


# ═════════════════════════════════════════════════════════════════════════════
# Routing
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class Route:
    name: str
    upstox_chain: list[dict[str, Any]] = field(default_factory=list)
    yf_ticker: str = ""
    yf_kind: str = "spot"
    yf_label: str = ""
    plausibility_min: float = 0.0
    plausibility_max: float = 1e12
    reference_ticker: str = ""
    reference_tolerance_pct: float = 0.0


@dataclass
class Routing:
    routes: list[Route] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def resolve(cache: InstrumentCache) -> Routing:
    r = Routing()

    # ── USD/INR ───────────────────────────────────────────────────────────
    # NSE currency derivatives, strict prefix "USDINR" on trading_symbol.
    chain = cache.find_futures("USDINR", segment="NCD_FO",
                               instrument_type="FUT", max_n=3)
    if chain:
        r.routes.append(Route(
            name="USDINR",
            upstox_chain=chain,
            yf_ticker="INR=X",
            yf_kind="spot",
            yf_label="forex fallback",
            plausibility_min=70.0,
            plausibility_max=92.0,
            reference_ticker="INR=X",
            reference_tolerance_pct=0.03,
        ))
        exp = _fmt_expiry_ms(chain[0].get("expiry"))
        sym = chain[0].get("trading_symbol", "?")
        r.notes.append(f"USDINR: {len(chain)} NCD_FO contracts, front={sym} exp={exp}")
    else:
        r.routes.append(Route(
            name="USDINR",
            yf_ticker="INR=X",
            yf_kind="spot",
            yf_label="forex",
        ))
        r.notes.append("USDINR: no NCD_FO futures — using yfinance INR=X")

    # ── MCX Gold ──────────────────────────────────────────────────────────
    chain = cache.find_futures("GOLD", segment="MCX_FO",
                               instrument_type="FUT", max_n=3)
    if chain:
        r.routes.append(Route(
            name="MCX_GOLD",
            upstox_chain=chain,
            yf_ticker="GC=F",
            yf_kind="futures",
            yf_label="COMEX proxy",
            plausibility_min=50000.0,
            plausibility_max=300000.0,
        ))
        exp = _fmt_expiry_ms(chain[0].get("expiry"))
        sym = chain[0].get("trading_symbol", "?")
        r.notes.append(f"MCX_GOLD: {len(chain)} MCX_FO contracts, front={sym} exp={exp}")
    else:
        r.routes.append(Route(
            name="MCX_GOLD",
            yf_ticker="GC=F",
            yf_kind="futures",
            yf_label="COMEX proxy",
        ))
        r.notes.append("MCX_GOLD: no MCX_FO futures — using yfinance GC=F")

    # ── XAU/USD (labeled proxy) ───────────────────────────────────────────
    r.routes.append(Route(
        name="XAUUSD",
        yf_ticker="GC=F",
        yf_kind="futures",
        yf_label="COMEX proxy",
    ))
    r.notes.append(
        "XAUUSD: Yahoo's XAUUSD=X spot ticker is delisted; using GC=F "
        "(COMEX gold futures). Intraday 1m preferred; daily fallback."
    )

    # ── DXY ───────────────────────────────────────────────────────────────
    r.routes.append(Route(
        name="DXY",
        yf_ticker="DX-Y.NYB",
        yf_kind="daily",
        yf_label="daily close",
    ))
    r.notes.append(
        "DXY: yfinance provides daily closes only — the change shown is the "
        "daily change, not an intraday move."
    )

    r.routes.append(Route(name="US10Y_REAL", yf_kind="daily"))
    r.routes.append(Route(name="FED_WATCH", yf_kind="expectation"))

    return r


# ═════════════════════════════════════════════════════════════════════════════
# Snapshot assembly
# ═════════════════════════════════════════════════════════════════════════════
def build_snapshot(client: UpstoxClient, routing: Routing) -> Snapshot:
    snap = Snapshot(ts_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    references: dict[str, float | None] = {}
    for route in routing.routes:
        if route.reference_ticker and route.reference_ticker not in references:
            references[route.reference_ticker] = fetch_reference(route.reference_ticker)
            if DEBUG:
                refv = references[route.reference_ticker]
                print(f"[DEBUG] reference {route.reference_ticker} = {refv}")

    for route in routing.routes:
        if route.upstox_chain:
            ref_val = references.get(route.reference_ticker) if route.reference_ticker else None
            snap.observations.append(fetch_upstox_chain(
                client=client,
                name=route.name,
                contract_rows=route.upstox_chain,
                fallback_yf=route.yf_ticker,
                fallback_label=route.yf_label or route.yf_kind,
                fallback_kind=route.yf_kind,
                plausibility_min=route.plausibility_min,
                plausibility_max=route.plausibility_max,
                reference_price=ref_val,
                tolerance_pct=route.reference_tolerance_pct,
            ))
            continue

        if route.name == "US10Y_REAL":
            snap.observations.append(fetch_fred_real_yield())
        elif route.name == "FED_WATCH":
            snap.observations.append(fetch_fedwatch())
        elif route.yf_ticker:
            prefer = route.yf_kind != "daily"
            snap.observations.append(fetch_yf(
                ticker=route.yf_ticker,
                name=route.name,
                kind_hint=route.yf_kind,
                label=route.yf_label or None,
                prefer_intraday=prefer,
            ))

    snap.notes.append(
        "Validation: plausibility range enforced on Upstox futures; "
        "reference cross-check is fail-closed; yfinance intraday→daily "
        "fallback adjusts the kind label."
    )
    snap.notes.append(
        "DXY and US10Y_REAL are daily series — their values do not change "
        "intraday. FED_WATCH updates once per business day."
    )
    return snap


# ═════════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════════
_running = True


def _sigint(signum, frame):  # noqa: ARG001
    global _running
    _running = False
    print(f"\n{YELLOW}[stopping] Ctrl+C — finishing current cycle…{RESET}")


def main() -> None:
    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)

    env = load_env(ENV_FILE)
    token = env.get("UPSTOX_ACCESS_TOKEN", "")
    if not token:
        sys.exit("[fatal] UPSTOX_ACCESS_TOKEN missing from env.txt")

    os.system("cls" if os.name == "nt" else "clear")

    print(f"{DIM}[init] loading Upstox instrument files (cached 24h)…{RESET}")
    cache = InstrumentCache()
    cache.load()
    if cache.loaded_files:
        print(f"{DIM}  loaded: {', '.join(cache.loaded_files)}{RESET}")
    else:
        print(f"{YELLOW}  no instrument files — falling back to yfinance{RESET}")
    for e in cache.errors:
        print(f"{YELLOW}  warn: {e}{RESET}")

    print(f"{DIM}[init] resolving indicator routes…{RESET}")
    routing = resolve(cache)
    for route in routing.routes:
        if route.upstox_chain:
            keys = [c["instrument_key"] for c in route.upstox_chain]
            tol = f" tol={route.reference_tolerance_pct*100:.1f}%" if route.reference_tolerance_pct else ""
            rng = f" [{route.plausibility_min:g}-{route.plausibility_max:g}]"
            print(f"{DIM}  {route.name:<12} → {GREEN}upstox{RESET}{DIM}  "
                  f"chain={keys}  ref={route.reference_ticker or '—'}{tol}{rng}  "
                  f"fallback={route.yf_ticker}{RESET}")
        else:
            tgt = route.yf_ticker or route.name.lower()
            print(f"{DIM}  {route.name:<12} → {CYAN}{tgt}{RESET}{DIM}  "
                  f"[{route.yf_kind}]{RESET}")
    print()
    for n in routing.notes:
        print(f"{DIM}  • {n}{RESET}")
    time.sleep(2.0)

    client = UpstoxClient(token)

    while _running:
        cycle_start = time.monotonic()
        try:
            snap = build_snapshot(client, routing)
        except Exception as exc:
            snap = Snapshot(
                ts_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                observations=[Observation(
                    name="SYSTEM", value="—", source="internal",
                    error=f"{type(exc).__name__}: {exc}", kind="unknown",
                )],
            )

        if os.name == "nt":
            os.system("cls")
        else:
            sys.stdout.write("\033[H\033[J")
        sys.stdout.write(render(snap))
        sys.stdout.flush()

        elapsed = time.monotonic() - cycle_start
        remaining = max(0.0, REFRESH_SECONDS - elapsed)
        while remaining > 0 and _running:
            chunk = min(0.5, remaining)
            time.sleep(chunk)
            remaining -= chunk

    print(f"\n{BOLD}Goodbye.{RESET}")


if __name__ == "__main__":
    main()