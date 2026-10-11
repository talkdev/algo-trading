#!/usr/bin/env python3
"""Backtest-only NIFTY 50 / NIFTY BANK futures pairs strategy.

IMPORTANT
---------
* This program is for historical backtesting only. It contains NO order-placement,
  WebSocket, Telegram, or live-trading code.
* It reads only UPSTOX_ACCESS_TOKEN from a private env.txt file. Do not put secrets
  in source code, command-line arguments, logs, or a shared workspace.
* Upstox Plus is required for expired-contract discovery and expired-futures candles.
* OHLC candles cannot reproduce queue position, actual partial fills, or the exact
  tick-by-tick path. Costs are modelled as a user-specified percentage per executed
  side. Intrabar stop and daily-loss checks use a conservative paired-OHLC envelope.
* The Engle-Granger threshold defaults to 0.05. A CLI override is research-only and
  does not establish profitability or statistical significance.
* Detailed console diagnostics plus aligned-front and per-session CSV audits are
  written for troubleshooting. This script does not guarantee profit.

Dependencies
------------
    pip install numpy pandas requests statsmodels

Example (provide your own dates; warm-up data is fetched before test_start):
    python nifty_pairs_backtest.py \
        --start-date 2026-07-01 --end-date 2026-10-09 \
        --env-file env.txt --output-dir backtest_output

Offline smoke test (does not read env.txt or call Upstox):
    python nifty_pairs_backtest.py --synthetic-demo --output-dir /tmp/nifty-demo

The script fetches futures candles only. It does not call any Upstox trading endpoint.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from statsmodels.tsa.stattools import coint

IST = ZoneInfo("Asia/Kolkata")
UPSTOX_BASE = "https://api.upstox.com"
NIFTY_UNDERLYING = "NSE_INDEX|Nifty 50"
BANKNIFTY_UNDERLYING = "NSE_INDEX|Nifty Bank"
BAR_MINUTES = 5


class BacktestError(RuntimeError):
    """Raised for invalid data, API responses, or backtest configuration."""


@dataclass(frozen=True)
class FutureMeta:
    expiry: date
    instrument_key: str
    lot_size: int
    tick_size: float
    trading_symbol: str
    expired: bool


@dataclass
class StrategyConfig:
    lookback_sessions: int = 60
    z_window: int = 50
    # Research knob only. The finalized baseline remains 5%; weakening it is not
    # evidence of cointegration and must be validated walk-forward.
    cointegration_pvalue_threshold: float = 0.05
    entry_z: float = 2.0
    target_z: float = 0.0
    stop_z: float = 3.0
    base_nifty_lots: int = 2
    max_nifty_lots: int = 6
    max_residual_rupees: float = 75_000.0
    margin_pool: float = 1_500_000.0
    max_gross_multiple: float = 4.0
    daily_loss_fraction: float = 0.03
    cost_per_execution_side: float = 0.0005
    latest_entry: dtime = dtime(14, 30)
    flatten_time: dtime = dtime(15, 15)


@dataclass
class OpenPosition:
    side: int                         # +1 long residual; -1 short residual
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    entry_z: float
    alpha: float
    beta: float
    pvalue: float
    nifty_lots: int
    bank_lots: int
    nifty_units: int
    bank_units: int
    nifty_lot_size: int
    bank_lot_size: int
    entry_nifty: float
    entry_bank: float
    entry_fee: float
    entry_gross_notional: float
    residual_exposure: float
    expiry: date


class UpstoxHistoryClient:
    """Read-only Upstox historical-data client. No order APIs are implemented."""

    def __init__(self, access_token: str, cache_dir: Path, timeout: tuple[int, int] = (5, 30)):
        if not access_token or len(access_token) < 20:
            raise BacktestError("UPSTOX_ACCESS_TOKEN is missing or looks invalid.")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        self.timeout = timeout
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = UPSTOX_BASE + path
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                if response.status_code == 401:
                    raise BacktestError(
                        "Upstox rejected the access token (401). Refresh it privately and update env.txt."
                    )
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    wait = min(0.5 * (2 ** attempt), 8.0)
                    time.sleep(wait)
                    continue
                if not response.ok:
                    body = response.text[:500].replace("\n", " ")
                    raise BacktestError(
                        f"Upstox GET {path} failed with HTTP {response.status_code}: {body}"
                    )
                payload = response.json()
                if isinstance(payload, dict) and payload.get("status") == "error":
                    # Do not log request headers or credentials.
                    message = str(payload.get("errors") or payload.get("message") or "API error")[:500]
                    raise BacktestError(f"Upstox API error for {path}: {message}")
                return payload
            except BacktestError:
                raise
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt < 4:
                    time.sleep(min(0.5 * (2 ** attempt), 8.0))
        raise BacktestError(f"Upstox request failed for {path}: {last_error}")

    def get_expiries(self, underlying_key: str) -> list[date]:
        payload = self._get("/v2/expired-instruments/expiries", {"instrument_key": underlying_key})
        values = payload.get("data") or []
        out: list[date] = []
        for value in values:
            try:
                out.append(date.fromisoformat(str(value)[:10]))
            except ValueError:
                continue
        return sorted(set(out))

    @staticmethod
    def _to_meta(record: dict[str, Any], expiry: date, expired: bool) -> FutureMeta | None:
        if str(record.get("instrument_type", "")).upper() not in {"FUT", "FUTIDX"}:
            return None
        key = record.get("instrument_key")
        lot = record.get("lot_size") or record.get("minimum_lot")
        if not key or not lot:
            return None
        try:
            return FutureMeta(
                expiry=expiry,
                instrument_key=str(key),
                lot_size=int(float(lot)),
                tick_size=float(record.get("tick_size") or 0.0),
                trading_symbol=str(record.get("trading_symbol") or record.get("name") or key),
                expired=expired,
            )
        except (TypeError, ValueError):
            return None

    def search_active_future(self, underlying_key: str, query: str, expiry: date | str) -> FutureMeta | None:
        expiry_text = expiry.isoformat() if isinstance(expiry, date) else str(expiry)
        params = {
            "query": query,
            "exchanges": "NSE",
            "segments": "FO",
            "instrument_types": "FUT",
            "expiry": expiry_text,
            "page_number": 1,
            "records": 30,
        }
        payload = self._get("/v2/instruments/search", params)
        records = payload.get("data") or []
        for rec in records:
            try:
                rec_expiry = date.fromisoformat(str(rec.get("expiry"))[:10])
            except (ValueError, TypeError):
                continue
            rec_underlying = str(rec.get("underlying_key") or "")
            if rec_expiry != date.fromisoformat(expiry_text[:10]):
                continue
            if rec_underlying and rec_underlying != underlying_key:
                continue
            meta = self._to_meta(rec, rec_expiry, expired=False)
            if meta:
                return meta
        return None

    def get_expired_future(self, underlying_key: str, expiry: date) -> FutureMeta | None:
        try:
            payload = self._get(
                "/v2/expired-instruments/future/contract",
                {"instrument_key": underlying_key, "expiry_date": expiry.isoformat()},
            )
        except BacktestError as exc:
            # Some option-expiry dates have no futures contract. Do not hide auth/API
            # failures; only treat a clearly absent contract as an empty result.
            text = str(exc).lower()
            if "http 400" in text or "http 404" in text or "no data" in text:
                return None
            raise
        for rec in payload.get("data") or []:
            meta = self._to_meta(rec, expiry, expired=True)
            if meta:
                return meta
        return None

    @staticmethod
    def _month_chunks(start: date, end: date) -> Iterable[tuple[date, date]]:
        cursor = start
        while cursor <= end:
            if cursor.month == 12:
                next_month = date(cursor.year + 1, 1, 1)
            else:
                next_month = date(cursor.year, cursor.month + 1, 1)
            chunk_end = min(end, next_month - timedelta(days=1))
            yield cursor, chunk_end
            cursor = chunk_end + timedelta(days=1)

    @staticmethod
    def _parse_candles(candles: list[list[Any]]) -> pd.DataFrame:
        if not candles:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "oi"])
        rows: list[dict[str, Any]] = []
        for c in candles:
            if len(c) < 5:
                continue
            ts = pd.Timestamp(c[0])
            ts = ts.tz_localize(IST) if ts.tzinfo is None else ts.tz_convert(IST)
            rows.append({
                "bar_start": ts,
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]) if len(c) > 5 and c[5] is not None else np.nan,
                "oi": float(c[6]) if len(c) > 6 and c[6] is not None else np.nan,
            })
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "oi"])
        df = pd.DataFrame(rows).drop_duplicates("bar_start").sort_values("bar_start")
        return df.set_index("bar_start")

    def fetch_contract_history(
        self, meta: FutureMeta, start: date, end: date
    ) -> pd.DataFrame:
        cache_id = hashlib.sha256(
            f"{meta.instrument_key}|{start}|{end}|5m".encode("utf-8")
        ).hexdigest()[:24]
        cache_path = self.cache_dir / f"{cache_id}.csv"
        if cache_path.exists():
            cached = pd.read_csv(cache_path)
            if not cached.empty:
                cached["bar_start"] = pd.to_datetime(cached["bar_start"], utc=True).dt.tz_convert(IST)
                return cached.set_index("bar_start").sort_index()

        frames: list[pd.DataFrame] = []
        encoded_key = quote(meta.instrument_key, safe="")
        for chunk_start, chunk_end in self._month_chunks(start, end):
            if meta.expired:
                path = (
                    f"/v2/expired-instruments/historical-candle/{encoded_key}/"
                    f"5minute/{chunk_end.isoformat()}/{chunk_start.isoformat()}"
                )
            else:
                path = (
                    f"/v3/historical-candle/{encoded_key}/minutes/5/"
                    f"{chunk_end.isoformat()}/{chunk_start.isoformat()}"
                )
            try:
                payload = self._get(path)
            except BacktestError as exc:
                # A contract may not have existed for an early part of the requested
                # range. Continue only for a date-range/empty-history response.
                text = str(exc).lower()
                if "invalid date range" in text or "no candles" in text or "no data" in text:
                    continue
                raise
            data = payload.get("data") or {}
            frames.append(self._parse_candles(data.get("candles") or []))
            time.sleep(0.08)

        frames = [f for f in frames if not f.empty]
        if not frames:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "oi"])
        result = pd.concat(frames).sort_index()
        result = result[~result.index.duplicated(keep="last")]
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        result.reset_index().to_csv(cache_path, index=False)
        return result


def read_access_token(env_path: Path) -> str:
    """Read a local KEY=VALUE file without printing or returning other secrets."""
    if not env_path.exists():
        raise BacktestError(
            f"Credential file not found: {env_path}. Create it privately with "
            "UPSTOX_ACCESS_TOKEN=...; do not put credentials in this script."
        )
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "UPSTOX_ACCESS_TOKEN":
            return value.strip().strip('"').strip("'")
    raise BacktestError(f"UPSTOX_ACCESS_TOKEN was not found in {env_path}.")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected YYYY-MM-DD") from exc


def _data_start_for_warmup(test_start: date, lookback_sessions: int) -> date:
    # ~7/5 calendar days per trading session plus a holiday buffer.
    buffer_days = max(120, math.ceil(lookback_sessions * 1.4) + 30)
    return test_start - timedelta(days=buffer_days)


def discover_contracts(
    api: UpstoxHistoryClient,
    start: date,
    end: date,
) -> tuple[dict[date, FutureMeta], dict[date, FutureMeta]]:
    """Resolve expiry-matched front-month futures for both NSE indices."""
    today = datetime.now(IST).date()
    specs = [
        ("nifty", NIFTY_UNDERLYING, "NIFTY"),
        ("bank", BANKNIFTY_UNDERLYING, "BANKNIFTY"),
    ]
    metas: list[dict[date, FutureMeta]] = []
    date_sets: list[set[date]] = []
    for _, underlying_key, query in specs:
        expiry_dates = set(api.get_expiries(underlying_key))
        # Current/future contract discovery is also queried directly so the last
        # active expiry is present even if the Plus expiry list only returns history.
        for keyword in ("current_month", "next_month", "far_month"):
            try:
                payload = api._get(
                    "/v2/instruments/search",
                    {
                        "query": query,
                        "exchanges": "NSE",
                        "segments": "FO",
                        "instrument_types": "FUT",
                        "expiry": keyword,
                        "page_number": 1,
                        "records": 30,
                    },
                )
                for rec in payload.get("data") or []:
                    try:
                        expiry_dates.add(date.fromisoformat(str(rec.get("expiry"))[:10]))
                    except (TypeError, ValueError):
                        continue
            except BacktestError:
                # Exact-expiry search below remains the fallback.
                pass
        date_sets.append(expiry_dates)
        metas.append({})

    # Both legs must have the exact same expiry. Query only dates in the overlap;
    # this also avoids treating weekly option expiries as futures expiries.
    common = sorted(date_sets[0] & date_sets[1])
    relevant = [d for d in common if start <= d <= end + timedelta(days=45)]
    if not relevant:
        raise BacktestError(
            "No common NIFTY/BANKNIFTY futures expiries were returned. "
            "Check the Upstox Plus entitlement and instrument keys."
        )

    out_n: dict[date, FutureMeta] = {}
    out_b: dict[date, FutureMeta] = {}
    for expiry in relevant:
        for idx, (label, underlying_key, query) in enumerate(specs):
            meta: FutureMeta | None
            if expiry < today:
                meta = api.get_expired_future(underlying_key, expiry)
            else:
                meta = api.search_active_future(underlying_key, query, expiry)
            if meta is None:
                continue
            (out_n if label == "nifty" else out_b)[expiry] = meta
            time.sleep(0.05)

    common_contract_expiries = sorted(set(out_n) & set(out_b))
    out_n = {e: out_n[e] for e in common_contract_expiries}
    out_b = {e: out_b[e] for e in common_contract_expiries}
    if len(common_contract_expiries) < 2:
        raise BacktestError(
            "Could not resolve at least two same-expiry NIFTY/BANKNIFTY futures pairs. "
            "The front-month roll series needs a following contract for roll adjustment."
        )
    return out_n, out_b


def _regular_session_filter(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["bar_end"] = out.index + pd.Timedelta(minutes=BAR_MINUTES)
    out["session_date"] = [ts.date() for ts in out.index]
    # NSE equity-derivatives regular session. Keep only completed five-minute bars.
    out = out[
        (out.index.time >= dtime(9, 15))
        & (out["bar_end"].dt.time <= dtime(15, 40))
    ]
    return out


def _roll_scales(
    histories: dict[date, pd.DataFrame],
    metas: dict[date, FutureMeta],
    leg_name: str,
) -> tuple[dict[date, float], list[str]]:
    expiries = sorted(e for e in metas if e in histories and not histories[e].empty)
    if not expiries:
        raise BacktestError(f"No historical futures candles found for {leg_name}.")
    scales: dict[date, float] = {expiries[-1]: 1.0}
    warnings: list[str] = []
    for i in range(len(expiries) - 2, -1, -1):
        old_exp, new_exp = expiries[i], expiries[i + 1]
        old_df = histories[old_exp]
        new_df = histories[new_exp]
        common = old_df.index.intersection(new_df.index)
        roll_open = pd.Timestamp(datetime.combine(old_exp, dtime(9, 15)), tz=IST)
        refs = common[common < roll_open]
        if len(refs) == 0:
            # Fall back to the last common observation on/before the expiry session.
            roll_close = pd.Timestamp(datetime.combine(old_exp, dtime(15, 40)), tz=IST)
            refs = common[common <= roll_close]
        if len(refs) == 0:
            warnings.append(
                f"{leg_name}: no overlapping old/new contract candle for roll {old_exp}; "
                "used unit adjustment for that roll."
            )
            ratio = 1.0
        else:
            ref_ts = refs[-1]
            old_px = float(old_df.loc[ref_ts, "close"])
            new_px = float(new_df.loc[ref_ts, "close"])
            if old_px <= 0 or new_px <= 0:
                raise BacktestError(f"Non-positive roll reference price for {leg_name} {old_exp}.")
            # Multiplicative back-adjustment is consistent with the log-price model.
            ratio = new_px / old_px
        scales[old_exp] = scales[new_exp] * ratio
    return scales, warnings


def build_front_month_pair(
    metas_n: dict[date, FutureMeta],
    metas_b: dict[date, FutureMeta],
    histories_n: dict[date, pd.DataFrame],
    histories_b: dict[date, pd.DataFrame],
    start: date,
    end: date,
) -> tuple[pd.DataFrame, list[str]]:
    scales_n, warn_n = _roll_scales(histories_n, metas_n, "NIFTY")
    scales_b, warn_b = _roll_scales(histories_b, metas_b, "BANKNIFTY")
    warnings = warn_n + warn_b

    expiry_dates = sorted(set(metas_n) | set(metas_b))
    session_dates: set[date] = set()
    for frame in list(histories_n.values()) + list(histories_b.values()):
        if not frame.empty:
            regular = _regular_session_filter(frame)
            session_dates.update(d for d in regular["session_date"].unique())

    rows: list[dict[str, Any]] = []
    expiries_n = sorted(metas_n)
    expiries_b = sorted(metas_b)
    for session in sorted(d for d in session_dates if start <= d <= end):
        # The contract is rolled at 09:00 on expiry day, so the expiry session maps
        # to the next expiry. No strategy entries are allowed on an expiry date.
        candidates_n = [e for e in expiries_n if e > session]
        candidates_b = [e for e in expiries_b if e > session]
        if not candidates_n or not candidates_b:
            continue
        exp_n, exp_b = candidates_n[0], candidates_b[0]
        if exp_n != exp_b:
            # Strictly enforce same-expiry legs; do not silently substitute.
            continue
        if exp_n not in histories_n or exp_b not in histories_b:
            continue
        nf = _regular_session_filter(histories_n[exp_n])
        bf = _regular_session_filter(histories_b[exp_b])
        nf = nf[nf["session_date"] == session]
        bf = bf[bf["session_date"] == session]
        if nf.empty or bf.empty:
            continue
        # Exact timestamp intersection only. No forward-fill or stale-price synthesis.
        joined = nf.join(bf, how="inner", lsuffix="_n", rsuffix="_b")
        if joined.empty:
            continue
        scale_n = scales_n.get(exp_n, 1.0)
        scale_b = scales_b.get(exp_b, 1.0)
        is_expiry = session in expiry_dates
        meta_n, meta_b = metas_n[exp_n], metas_b[exp_b]
        for ts, r in joined.iterrows():
            rows.append({
                "bar_start": ts,
                "bar_end": ts + pd.Timedelta(minutes=BAR_MINUTES),
                "session_date": session,
                "expiry": exp_n,
                "is_expiry_day": bool(is_expiry),
                "n_open": float(r["open_n"]),
                "n_high": float(r["high_n"]),
                "n_low": float(r["low_n"]),
                "n_close": float(r["close_n"]),
                "b_open": float(r["open_b"]),
                "b_high": float(r["high_b"]),
                "b_low": float(r["low_b"]),
                "b_close": float(r["close_b"]),
                "n_lot_size": meta_n.lot_size,
                "b_lot_size": meta_b.lot_size,
                "n_scale": scale_n,
                "b_scale": scale_b,
                "n_adj_close": float(r["close_n"]) * scale_n,
                "b_adj_close": float(r["close_b"]) * scale_b,
                "nifty_symbol": meta_n.trading_symbol,
                "bank_symbol": meta_b.trading_symbol,
            })
    if not rows:
        raise BacktestError(
            "No synchronized front-month pair bars were built. Check that both legs have "
            "the same expiry dates and that the requested range is covered by Upstox history."
        )
    front = pd.DataFrame(rows).drop_duplicates("bar_start").sort_values("bar_start")
    front = front.set_index("bar_end").sort_index()
    return front, warnings


def fetch_upstox_front_month(
    api: UpstoxHistoryClient,
    test_start: date,
    test_end: date,
    lookback_sessions: int,
) -> tuple[pd.DataFrame, list[str]]:
    data_start = _data_start_for_warmup(test_start, lookback_sessions)
    # The next expiry is required to switch the front-month mapping on expiry day.
    data_end = test_end + timedelta(days=45)
    metas_n, metas_b = discover_contracts(api, data_start, data_end)
    histories_n: dict[date, pd.DataFrame] = {}
    histories_b: dict[date, pd.DataFrame] = {}
    for expiry, meta in metas_n.items():
        histories_n[expiry] = api.fetch_contract_history(meta, data_start, min(data_end, expiry))
    for expiry, meta in metas_b.items():
        histories_b[expiry] = api.fetch_contract_history(meta, data_start, min(data_end, expiry))
    # Trim any empty contract responses before continuous-series construction.
    histories_n = {e: f for e, f in histories_n.items() if not f.empty}
    histories_b = {e: f for e, f in histories_b.items() if not f.empty}
    metas_n = {e: m for e, m in metas_n.items() if e in histories_n}
    metas_b = {e: m for e, m in metas_b.items() if e in histories_b}
    front, warnings = build_front_month_pair(
        metas_n, metas_b, histories_n, histories_b, data_start, test_end
    )
    return front, warnings


def fit_daily_model(
    train: pd.DataFrame,
    pvalue_threshold: float = 0.05,
) -> dict[str, float | bool | int]:
    """OLS(log NIFTY on log BANKNIFTY) plus Engle-Granger diagnostics.

    Model validity uses the requested p-value threshold. The test statistic,
    MacKinnon critical values, fit quality, and return correlation are emitted for
    diagnosis only; they do not independently authorize trades.
    """
    clean = train[["n_adj_close", "b_adj_close"]].replace([np.inf, -np.inf], np.nan).dropna()
    clean = clean[(clean["n_adj_close"] > 0) & (clean["b_adj_close"] > 0)]
    empty: dict[str, float | bool | int] = {
        "alpha": np.nan, "beta": np.nan, "pvalue": 1.0, "valid": False,
        "nobs": int(len(clean)), "coint_statistic": np.nan,
        "critical_1pct": np.nan, "critical_5pct": np.nan, "critical_10pct": np.nan,
        "r_squared": np.nan, "level_correlation": np.nan,
        "return_correlation": np.nan, "residual_std": np.nan,
    }
    if len(clean) < 500:
        return empty
    y = np.log(clean["n_adj_close"].to_numpy(dtype=float))
    x = np.log(clean["b_adj_close"].to_numpy(dtype=float))
    design = np.column_stack([np.ones(len(x)), x])
    alpha, beta = np.linalg.lstsq(design, y, rcond=None)[0]
    residual = y - (alpha + beta * x)
    ss_total = float(np.sum((y - y.mean()) ** 2))
    r_squared = 1.0 - float(np.sum(residual ** 2)) / ss_total if ss_total > 0 else np.nan
    level_corr = float(np.corrcoef(y, x)[0, 1]) if np.std(x) > 0 and np.std(y) > 0 else np.nan
    dy, dx = np.diff(y), np.diff(x)
    return_corr = (
        float(np.corrcoef(dy, dx)[0, 1])
        if len(dx) > 1 and np.std(dx) > 0 and np.std(dy) > 0 else np.nan
    )
    try:
        statistic, pvalue, critical = coint(y, x, trend="c", autolag="aic")
        statistic, pvalue = float(statistic), float(pvalue)
        critical = np.asarray(critical, dtype=float).reshape(-1)
        crit_values = [float(critical[i]) if len(critical) > i else np.nan for i in range(3)]
    except Exception:
        statistic, pvalue = np.nan, 1.0
        crit_values = [np.nan, np.nan, np.nan]
    valid = bool(
        np.isfinite(alpha) and np.isfinite(beta) and np.isfinite(pvalue)
        and pvalue < pvalue_threshold
    )
    return {
        "alpha": float(alpha), "beta": float(beta), "pvalue": pvalue,
        "valid": valid, "nobs": int(len(clean)), "coint_statistic": statistic,
        "critical_1pct": crit_values[0], "critical_5pct": crit_values[1],
        "critical_10pct": crit_values[2], "r_squared": float(r_squared),
        "level_correlation": level_corr, "return_correlation": return_corr,
        "residual_std": float(np.std(residual, ddof=1)),
    }


def choose_lot_size(
    beta: float,
    nifty_price: float,
    bank_price: float,
    nifty_lot_size: int,
    bank_lot_size: int,
    cfg: StrategyConfig,
) -> tuple[int, int, float, float] | None:
    """Size a residual-P&L hedge: bank notional ~= beta * nifty notional."""
    if beta <= 0 or min(nifty_price, bank_price, nifty_lot_size, bank_lot_size) <= 0:
        return None
    max_gross = cfg.max_gross_multiple * cfg.margin_pool
    for qn in range(cfg.base_nifty_lots, cfg.max_nifty_lots + 1):
        target_qb = beta * qn * nifty_lot_size * nifty_price / (bank_lot_size * bank_price)
        qb = int(math.floor(target_qb + 0.5))
        if qb < 1:
            continue
        w_n = qn * nifty_lot_size * nifty_price
        w_b = qb * bank_lot_size * bank_price
        residual = abs(w_b - beta * w_n)
        gross = w_n + w_b
        if residual <= cfg.max_residual_rupees and gross <= max_gross:
            return qn, qb, residual, gross
    return None


def explain_lot_size_rejection(
    beta: float,
    nifty_price: float,
    bank_price: float,
    nifty_lot_size: int,
    bank_lot_size: int,
    cfg: StrategyConfig,
) -> str:
    """Explain why no whole-lot mix satisfies residual and gross caps."""
    if beta <= 0 or min(nifty_price, bank_price, nifty_lot_size, bank_lot_size) <= 0:
        return "invalid_beta_or_price_or_lot"
    max_gross = cfg.max_gross_multiple * cfg.margin_pool
    outcomes: list[tuple[float, bool, bool]] = []
    had_bank_lot = False
    for qn in range(cfg.base_nifty_lots, cfg.max_nifty_lots + 1):
        qb = int(math.floor(beta * qn * nifty_lot_size * nifty_price /
                            (bank_lot_size * bank_price) + 0.5))
        if qb < 1:
            continue
        had_bank_lot = True
        w_n = qn * nifty_lot_size * nifty_price
        w_b = qb * bank_lot_size * bank_price
        residual = abs(w_b - beta * w_n)
        gross = w_n + w_b
        outcomes.append((residual, residual <= cfg.max_residual_rupees, gross <= max_gross))
    if not had_bank_lot:
        return "rounded_bank_lots_below_one"
    # Report the reason for the closest hedge; a diagnostic label, not a sizing change.
    _, residual_ok, gross_ok = min(outcomes, key=lambda item: item[0])
    if not residual_ok and not gross_ok:
        return "residual_and_gross_caps"
    if not residual_ok:
        return "residual_cap"
    if not gross_ok:
        return "gross_cap"
    return "unexpected_no_feasible_mix"


def _spread_extremes(row: pd.Series, alpha: float, beta: float) -> tuple[float, float, float]:
    """Return open, conservative low and conservative high log-residual values."""
    n_open = row["n_open"] * row["n_scale"]
    b_open = row["b_open"] * row["b_scale"]
    n_low = row["n_low"] * row["n_scale"]
    n_high = row["n_high"] * row["n_scale"]
    b_low = row["b_low"] * row["b_scale"]
    b_high = row["b_high"] * row["b_scale"]
    s_open = math.log(n_open) - beta * math.log(b_open) - alpha
    if beta >= 0:
        s_low = math.log(n_low) - beta * math.log(b_high) - alpha
        s_high = math.log(n_high) - beta * math.log(b_low) - alpha
    else:
        s_low = math.log(n_low) - beta * math.log(b_low) - alpha
        s_high = math.log(n_high) - beta * math.log(b_high) - alpha
    return s_open, s_low, s_high


def _gross_pnl(pos: OpenPosition, exit_n: float, exit_b: float) -> float:
    sign_n = pos.side
    sign_b = -pos.side
    return (
        sign_n * pos.nifty_units * (exit_n - pos.entry_nifty)
        + sign_b * pos.bank_units * (exit_b - pos.entry_bank)
    )


def _notional(pos: OpenPosition, px_n: float, px_b: float) -> float:
    return abs(pos.nifty_units * px_n) + abs(pos.bank_units * px_b)


def run_backtest(
    front: pd.DataFrame,
    test_start: date,
    test_end: date,
    cfg: StrategyConfig,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Run an event-driven OHLC backtest; entries use next-bar open to avoid look-ahead."""
    if front.empty:
        raise BacktestError("Backtest input is empty.")
    front = front.sort_index().copy()
    unique_sessions = sorted(set(front["session_date"]))
    test_sessions = [d for d in unique_sessions if test_start <= d <= test_end]
    if not test_sessions:
        raise BacktestError("No bars fall inside the requested out-of-sample date range.")

    equity = float(cfg.margin_pool)
    position: OpenPosition | None = None
    pending_entry: dict[str, Any] | None = None
    pending_exit: dict[str, Any] | None = None
    armed_for_entry = True
    last_z: float | None = None
    trades: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    model_rows: list[dict[str, Any]] = []
    halted_for_day = False

    def close_position(
        pos: OpenPosition,
        when: pd.Timestamp,
        exit_n: float,
        exit_b: float,
        reason: str,
        z_exit: float | None,
    ) -> None:
        nonlocal equity, position, armed_for_entry
        gross = _gross_pnl(pos, exit_n, exit_b)
        exit_fee = cfg.cost_per_execution_side * _notional(pos, exit_n, exit_b)
        net = gross - pos.entry_fee - exit_fee
        equity += net
        trades.append({
            "signal_time": pos.signal_time.isoformat(),
            "entry_time": pos.entry_time.isoformat(),
            "exit_time": when.isoformat(),
            "side": "LONG_SPREAD" if pos.side > 0 else "SHORT_SPREAD",
            "entry_z": pos.entry_z,
            "exit_z": z_exit,
            "beta": pos.beta,
            "cointegration_pvalue": pos.pvalue,
            "expiry": pos.expiry.isoformat(),
            "nifty_lots": pos.nifty_lots,
            "bank_lots": pos.bank_lots,
            "nifty_units": pos.nifty_units,
            "bank_units": pos.bank_units,
            "nifty_lot_size": pos.nifty_lot_size,
            "bank_lot_size": pos.bank_lot_size,
            "entry_nifty": pos.entry_nifty,
            "entry_bank": pos.entry_bank,
            "exit_nifty": float(exit_n),
            "exit_bank": float(exit_b),
            "residual_exposure_rupees": pos.residual_exposure,
            "gross_notional_entry": pos.entry_gross_notional,
            "gross_pnl_rupees": gross,
            "entry_cost_rupees": pos.entry_fee,
            "exit_cost_rupees": exit_fee,
            "net_pnl_rupees": net,
            "exit_reason": reason,
            "equity_after_exit": equity,
        })
        position = None
        # Re-entry is only armed immediately after a mean-crossing take-profit.
        armed_for_entry = (reason == "TAKE_PROFIT")

    for session in test_sessions:
        day_rows = front[front["session_date"] == session].copy().sort_index()
        if day_rows.empty:
            continue
        prior_sessions = [d for d in unique_sessions if d < session]
        train_sessions = prior_sessions[-cfg.lookback_sessions:]
        if len(train_sessions) < cfg.lookback_sessions:
            model = {
                "alpha": np.nan, "beta": np.nan, "pvalue": 1.0, "valid": False, "nobs": 0,
                "coint_statistic": np.nan, "critical_1pct": np.nan,
                "critical_5pct": np.nan, "critical_10pct": np.nan,
                "r_squared": np.nan, "level_correlation": np.nan,
                "return_correlation": np.nan, "residual_std": np.nan,
            }
        else:
            train = front[front["session_date"].isin(train_sessions)]
            model = fit_daily_model(train, cfg.cointegration_pvalue_threshold)
        alpha = float(model["alpha"])
        beta = float(model["beta"])
        pvalue = float(model["pvalue"])
        model_valid = bool(model["valid"])
        model_rows.append({
            "session_date": session.isoformat(),
            "alpha": alpha,
            "beta": beta,
            "cointegration_pvalue": pvalue,
            "cointegration_pvalue_threshold": cfg.cointegration_pvalue_threshold,
            "cointegration_valid": model_valid,
            "coint_statistic": model.get("coint_statistic", np.nan),
            "critical_1pct": model.get("critical_1pct", np.nan),
            "critical_5pct": model.get("critical_5pct", np.nan),
            "critical_10pct": model.get("critical_10pct", np.nan),
            "r_squared": model.get("r_squared", np.nan),
            "level_correlation": model.get("level_correlation", np.nan),
            "return_correlation": model.get("return_correlation", np.nan),
            "residual_std": model.get("residual_std", np.nan),
            "training_sessions": len(train_sessions),
            "training_bars": int(model["nobs"]),
        })

        # Re-express the rolling signal history using this session's locked
        # alpha/beta. For an invalid model, its Z is diagnostic only.
        if np.isfinite(alpha) and np.isfinite(beta):
            residual = np.log(front["n_adj_close"].where(front["n_adj_close"] > 0)) \
                - beta * np.log(front["b_adj_close"].where(front["b_adj_close"] > 0)) - alpha
            mean_prev = residual.rolling(cfg.z_window, min_periods=cfg.z_window).mean().shift(1)
            std_prev = residual.rolling(cfg.z_window, min_periods=cfg.z_window).std(ddof=1).shift(1)
        else:
            residual = pd.Series(np.nan, index=front.index)
            mean_prev = pd.Series(np.nan, index=front.index)
            std_prev = pd.Series(np.nan, index=front.index)

        sod_equity = equity
        daily_start_equity = sod_equity
        daily_halted = False
        pending_entry = None  # Never carry a signal overnight.
        pending_exit = None  # Intraday-only positions.
        previous_bar_end: pd.Timestamp | None = None
        previous_session: date | None = None
        last_z_for_rearm = last_z
        day_pnl_before = equity

        for i, (bar_end, row) in enumerate(day_rows.iterrows()):
            bar_start = pd.Timestamp(row["bar_start"])
            bar_end_ts = pd.Timestamp(bar_end)
            is_expiry_day = bool(row["is_expiry_day"])
            mu = float(mean_prev.loc[bar_end]) if bar_end in mean_prev.index else np.nan
            sigma = float(std_prev.loc[bar_end]) if bar_end in std_prev.index else np.nan
            r_close = float(residual.loc[bar_end]) if bar_end in residual.index else np.nan
            diagnostic_z = np.nan
            if np.isfinite(mu) and np.isfinite(sigma) and sigma > 0 and np.isfinite(r_close):
                diagnostic_z = (r_close - mu) / sigma
            # Keep production/tradable Z blank when the EG gate fails. The
            # counterfactual column below is never used to place backtest trades.
            z_close = diagnostic_z if model_valid else np.nan
            if model_valid and np.isfinite(mu) and np.isfinite(sigma) and sigma > 0:
                s_open, s_low, s_high = _spread_extremes(row, alpha, beta)
                z_open = (s_open - mu) / sigma
                z_low = (s_low - mu) / sigma
                z_high = (s_high - mu) / sigma
            else:
                z_open = z_low = z_high = np.nan

            # If synchronized bars are missing inside the same session, an OHLC
            # backtest cannot observe the path. Exit any open pair at the next
            # available bar open and suppress new entries for the rest of the day.
            intra_day_gap = (
                previous_bar_end is not None
                and previous_session == session
                and bar_start != previous_bar_end
            )
            if intra_day_gap:
                pending_entry = None
                # Without the missing paired bars, avoid taking fresh risk for
                # the rest of the session even when currently flat.
                daily_halted = True
                if position is not None:
                    close_position(position, bar_start, float(row["n_open"]), float(row["b_open"]),
                                   "DATA_GAP_EXIT", z_open if np.isfinite(z_open) else None)

            # Execute a target exit signalled at the prior completed-bar close.
            if pending_exit is not None and position is not None:
                close_position(position, bar_start, float(row["n_open"]), float(row["b_open"]),
                               "TAKE_PROFIT", pending_exit.get("z"))
                pending_exit = None

            # Execute an entry signalled at the prior completed-bar close, at this
            # bar's open. This avoids filling at a price known only after the close.
            if pending_entry is not None and position is None and not daily_halted:
                signal = pending_entry
                pending_entry = None
                contiguous = signal["signal_time"] == bar_start
                entry_time_allowed = bar_start.time() <= cfg.latest_entry
                if (
                    contiguous
                    and entry_time_allowed
                    and not is_expiry_day
                    and model_valid
                    and not daily_halted
                ):
                    px_n, px_b = float(row["n_open"]), float(row["b_open"])
                    sizing = choose_lot_size(
                        beta, px_n, px_b, int(row["n_lot_size"]), int(row["b_lot_size"]), cfg
                    )
                    if sizing is not None:
                        qn, qb, residual_exposure, gross_notional = sizing
                        if signal["side"] > 0:
                            side = 1
                        else:
                            side = -1
                        units_n = qn * int(row["n_lot_size"])
                        units_b = qb * int(row["b_lot_size"])
                        entry_fee = cfg.cost_per_execution_side * gross_notional
                        position = OpenPosition(
                            side=side,
                            signal_time=signal["signal_time"],
                            entry_time=bar_start,
                            entry_z=float(signal["z"]),
                            alpha=alpha,
                            beta=beta,
                            pvalue=pvalue,
                            nifty_lots=qn,
                            bank_lots=qb,
                            nifty_units=units_n,
                            bank_units=units_b,
                            nifty_lot_size=int(row["n_lot_size"]),
                            bank_lot_size=int(row["b_lot_size"]),
                            entry_nifty=px_n,
                            entry_bank=px_b,
                            entry_fee=entry_fee,
                            entry_gross_notional=gross_notional,
                            residual_exposure=residual_exposure,
                            expiry=row["expiry"],
                        )
                        armed_for_entry = False

            # Intrabar stop check. OHLC cannot establish the exact paired path, so
            # low/high use the conservative cross-leg envelope described in the docstring.
            if position is not None and np.isfinite(z_open) and np.isfinite(z_low) and np.isfinite(z_high):
                if position.side > 0:
                    stop_at_open = z_open <= -cfg.stop_z
                    stop_in_bar = z_low <= -cfg.stop_z
                    if stop_at_open:
                        close_position(position, bar_start, float(row["n_open"]), float(row["b_open"]),
                                       "HARD_STOP", z_open)
                    elif stop_in_bar:
                        close_position(position, bar_end_ts, float(row["n_low"]), float(row["b_high"]),
                                       "HARD_STOP_OHLC_ENVELOPE", -cfg.stop_z)
                else:
                    stop_at_open = z_open >= cfg.stop_z
                    stop_in_bar = z_high >= cfg.stop_z
                    if stop_at_open:
                        close_position(position, bar_start, float(row["n_open"]), float(row["b_open"]),
                                       "HARD_STOP", z_open)
                    elif stop_in_bar:
                        close_position(position, bar_end_ts, float(row["n_high"]), float(row["b_low"]),
                                       "HARD_STOP_OHLC_ENVELOPE", cfg.stop_z)

            # Daily loss risk includes unrealized MTM. Since the exact paired
            # intrabar path is unknowable from OHLC, test the most adverse paired
            # envelope and conservatively liquidate there if it breaches the cap.
            loss_limit = daily_start_equity * cfg.daily_loss_fraction
            loss_floor = daily_start_equity - loss_limit
            if position is not None:
                if position.side > 0:
                    adverse_n, adverse_b = float(row["n_low"]), float(row["b_high"])
                else:
                    adverse_n, adverse_b = float(row["n_high"]), float(row["b_low"])
                adverse_mark = (
                    equity
                    + _gross_pnl(position, adverse_n, adverse_b)
                    - position.entry_fee
                    - cfg.cost_per_execution_side * _notional(position, adverse_n, adverse_b)
                )
                if adverse_mark <= loss_floor:
                    close_position(position, bar_end_ts, adverse_n, adverse_b,
                                   "DAILY_LOSS_LIMIT_OHLC_ENVELOPE", None)
                    daily_halted = True
                    pending_entry = None
                    pending_exit = None

            if position is not None and bar_end_ts.time() >= cfg.flatten_time:
                close_position(position, bar_end_ts, float(row["n_close"]), float(row["b_close"]),
                               "TIME_STOP_1515", z_close if np.isfinite(z_close) else None)
                pending_exit = None

            # Net liquidation equity includes the estimated cost to close an open
            # position, making the daily limit and equity curve cost-consistent.
            if position is not None:
                mark = (
                    equity
                    + _gross_pnl(position, float(row["n_close"]), float(row["b_close"]))
                    - position.entry_fee
                    - cfg.cost_per_execution_side * _notional(
                        position, float(row["n_close"]), float(row["b_close"])
                    )
                )
            else:
                mark = equity
            if mark <= loss_floor:
                if position is not None:
                    close_position(position, bar_end_ts, float(row["n_close"]), float(row["b_close"]),
                                   "DAILY_LOSS_LIMIT", z_close if np.isfinite(z_close) else None)
                daily_halted = True
                pending_entry = None
                pending_exit = None

            # Re-entry requires a mean-cross after a non-target exit.
            if np.isfinite(z_close) and position is None and not armed_for_entry:
                if last_z_for_rearm is not None and np.isfinite(last_z_for_rearm):
                    if last_z_for_rearm == 0 or z_close == 0 or last_z_for_rearm * z_close < 0:
                        armed_for_entry = True
                elif abs(z_close) < 1e-12:
                    armed_for_entry = True

            # Mean reversion exit: signal on completed close, fill at next bar open.
            if position is not None and not daily_halted and np.isfinite(z_close):
                target_crossed = (
                    position.side > 0 and z_close >= cfg.target_z
                ) or (
                    position.side < 0 and z_close <= cfg.target_z
                )
                if target_crossed and bar_end_ts.time() < cfg.flatten_time:
                    pending_exit = {"signal_time": bar_end_ts, "z": z_close}

            # Entry signal on completed bars; only the next bar can execute it.
            if (
                position is None
                and pending_entry is None
                and model_valid
                and not daily_halted
                and not is_expiry_day
                and armed_for_entry
                and np.isfinite(z_close)
                and bar_end_ts.time() <= cfg.latest_entry
            ):
                if z_close <= -cfg.entry_z:
                    side = 1  # long residual: buy NIFTY, sell BANKNIFTY
                elif z_close >= cfg.entry_z:
                    side = -1 # short residual: sell NIFTY, buy BANKNIFTY
                else:
                    side = 0
                if side != 0:
                    next_bar_start = None
                    if i + 1 < len(day_rows):
                        next_bar_start = pd.Timestamp(day_rows.iloc[i + 1]["bar_start"])
                    if next_bar_start == bar_end_ts and next_bar_start.time() <= cfg.latest_entry:
                        pending_entry = {"side": side, "z": z_close, "signal_time": bar_end_ts}

            last_z_for_rearm = z_close if np.isfinite(z_close) else last_z_for_rearm
            last_z = last_z_for_rearm
            previous_bar_end = bar_end_ts
            previous_session = session

            # Record net-liquidation mark-to-market at every completed aligned bar.
            if position is not None:
                mark = (
                    equity
                    + _gross_pnl(position, float(row["n_close"]), float(row["b_close"]))
                    - position.entry_fee
                    - cfg.cost_per_execution_side * _notional(
                        position, float(row["n_close"]), float(row["b_close"])
                    )
                )
            else:
                mark = equity
            daily_rows.append({
                "timestamp": bar_end_ts.isoformat(),
                "session_date": session.isoformat(),
                "equity": mark,
                "realized_equity": equity,
                "zscore": z_close if np.isfinite(z_close) else np.nan,
                "diagnostic_zscore_counterfactual": diagnostic_z if np.isfinite(diagnostic_z) else np.nan,
                "cointegration_pvalue": pvalue,
                "cointegration_valid": model_valid,
                "position_side": 0 if position is None else position.side,
                "daily_halted": daily_halted,
                "expiry_day": is_expiry_day,
            })

        # Intraday policy: never carry a position or pending signal overnight.
        if position is not None and not day_rows.empty:
            final_row = day_rows.iloc[-1]
            final_time = pd.Timestamp(day_rows.index[-1])
            close_position(position, final_time, float(final_row["n_close"]), float(final_row["b_close"]),
                           "SESSION_END_FALLBACK", None)
            daily_halted = True
        pending_entry = None
        pending_exit = None
        daily_rows.append({
            "timestamp": pd.Timestamp(datetime.combine(session, dtime(15, 40)), tz=IST).isoformat(),
            "session_date": session.isoformat(),
            "equity": equity,
            "realized_equity": equity,
            "zscore": np.nan,
            "diagnostic_zscore_counterfactual": np.nan,
            "cointegration_pvalue": pvalue,
            "cointegration_valid": model_valid,
            "position_side": 0,
            "daily_halted": daily_halted,
            "expiry_day": bool(day_rows["is_expiry_day"].any()),
        })

    trades_df = pd.DataFrame(trades)
    equity_df = pd.DataFrame(daily_rows)
    models_df = pd.DataFrame(model_rows)
    if not equity_df.empty:
        daily_equity = equity_df.groupby("session_date", as_index=False).tail(1).copy()
        daily_equity["equity"] = pd.to_numeric(daily_equity["equity"], errors="coerce")
        daily_equity = daily_equity.sort_values("session_date")
        returns = daily_equity["equity"].pct_change().dropna()
        sharpe = float(np.sqrt(252) * returns.mean() / returns.std(ddof=1)) if len(returns) > 1 and returns.std(ddof=1) > 0 else 0.0
        peak = daily_equity["equity"].cummax()
        max_dd = float((daily_equity["equity"] / peak - 1.0).min()) if len(daily_equity) else 0.0
    else:
        daily_equity = pd.DataFrame()
        returns = pd.Series(dtype=float)
        sharpe = 0.0
        max_dd = 0.0

    downside = returns[returns < 0]
    sortino = (
        float(np.sqrt(252) * returns.mean() / downside.std(ddof=1))
        if len(downside) > 1 and downside.std(ddof=1) > 0 else 0.0
    )
    bar_equity = pd.to_numeric(equity_df.get("equity", pd.Series(dtype=float)), errors="coerce").dropna()
    bar_peak = bar_equity.cummax()
    max_bar_dd = float((bar_equity / bar_peak - 1.0).min()) if len(bar_equity) else 0.0
    if len(trades_df):
        net_trade_pnl = pd.to_numeric(trades_df["net_pnl_rupees"], errors="coerce").dropna()
        wins = net_trade_pnl[net_trade_pnl > 0]
        losses = net_trade_pnl[net_trade_pnl < 0]
        gross_win = float(wins.sum()) if len(wins) else 0.0
        gross_loss = float(abs(losses.sum())) if len(losses) else 0.0
        profit_factor = gross_win / gross_loss if gross_loss > 0 else None
        entry_times = pd.to_datetime(trades_df["entry_time"], utc=True)
        exit_times = pd.to_datetime(trades_df["exit_time"], utc=True)
        durations = (exit_times - entry_times).dt.total_seconds() / 60.0
        max_loss_streak = 0
        current_loss_streak = 0
        for pnl in net_trade_pnl:
            current_loss_streak = current_loss_streak + 1 if pnl < 0 else 0
            max_loss_streak = max(max_loss_streak, current_loss_streak)
        avg_trade_pnl = float(net_trade_pnl.mean())
        avg_win = float(wins.mean()) if len(wins) else None
        avg_loss = float(losses.mean()) if len(losses) else None
        median_duration = float(durations.median()) if len(durations) else None
    else:
        profit_factor = None
        max_loss_streak = 0
        avg_trade_pnl = avg_win = avg_loss = median_duration = None
    summary = {
        "test_start": test_start.isoformat(),
        "test_end": test_end.isoformat(),
        "initial_equity_rupees": cfg.margin_pool,
        "cointegration_pvalue_threshold": cfg.cointegration_pvalue_threshold,
        "final_equity_rupees": float(equity),
        "net_pnl_rupees": float(equity - cfg.margin_pool),
        "num_trades": int(len(trades_df)),
        "win_rate": float((trades_df["net_pnl_rupees"] > 0).mean()) if len(trades_df) else 0.0,
        "average_net_trade_pnl_rupees": avg_trade_pnl,
        "average_winning_trade_rupees": avg_win,
        "average_losing_trade_rupees": avg_loss,
        "profit_factor_net": profit_factor,
        "median_trade_duration_minutes": median_duration,
        "maximum_consecutive_losses": max_loss_streak,
        "daily_return_sharpe_annualized": sharpe,
        "daily_return_sortino_annualized": sortino,
        "max_daily_drawdown_fraction": max_dd,
        "max_bar_mark_to_market_drawdown_fraction": max_bar_dd,
        "total_cost_rupees": float(
            trades_df[["entry_cost_rupees", "exit_cost_rupees"]].sum().sum()
        ) if len(trades_df) else 0.0,
        "note": "OHLC backtest only; costs approximate fills; not a profitability claim.",
    }
    return trades_df, equity_df, models_df, summary


def collect_run_diagnostics(
    front: pd.DataFrame,
    equity: pd.DataFrame,
    models: pd.DataFrame,
    cfg: StrategyConfig,
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Create detailed non-trading diagnostics and per-session audit rows."""
    if models.empty:
        return {"diagnostic_test_sessions": 0, "diagnostic_valid_model_sessions": 0}, pd.DataFrame()

    model_df = models.copy()
    model_df["session_date"] = model_df["session_date"].astype(str)
    model_valid = model_df["cointegration_valid"].astype(str).str.lower().eq("true")
    pvalues = pd.to_numeric(model_df["cointegration_pvalue"], errors="coerce")
    betas = pd.to_numeric(model_df["beta"], errors="coerce")
    beta_changes = betas.diff().abs().dropna()

    bars = equity.copy()
    bars["session_date"] = bars["session_date"].astype(str)
    bars["local_ts"] = pd.to_datetime(bars["timestamp"], utc=True).dt.tz_convert(IST)
    bars["z_live"] = pd.to_numeric(bars.get("zscore", np.nan), errors="coerce")
    bars["z_counterfactual"] = pd.to_numeric(
        bars.get("diagnostic_zscore_counterfactual", np.nan), errors="coerce"
    )
    bars["model_valid_bool"] = bars["cointegration_valid"].astype(str).str.lower().eq("true")
    bars["expiry_bool"] = bars["expiry_day"].astype(str).str.lower().eq("true")
    bars["entry_time_ok"] = bars["local_ts"].dt.strftime("%H:%M") <= cfg.latest_entry.strftime("%H:%M")
    bars["eligible_close"] = bars["entry_time_ok"] & ~bars["expiry_bool"]
    bars["diag_long"] = bars["eligible_close"] & (bars["z_counterfactual"] <= -cfg.entry_z)
    bars["diag_short"] = bars["eligible_close"] & (bars["z_counterfactual"] >= cfg.entry_z)
    bars["counterfactual_invalid_model"] = (
        bars["model_valid_bool"] == False
    ) & (bars["diag_long"] | bars["diag_short"])
    bars["live_long"] = bars["eligible_close"] & bars["model_valid_bool"] & (bars["z_live"] <= -cfg.entry_z)
    bars["live_short"] = bars["eligible_close"] & bars["model_valid_bool"] & (bars["z_live"] >= cfg.entry_z)

    # Check next-bar continuity and sizing only for otherwise-valid live signal
    # closes. These are diagnostics, not trade fills or extra signal criteria.
    start_lookup: dict[tuple[str, pd.Timestamp], pd.Series] = {}
    for _, row in front.iterrows():
        key = (str(row["session_date"]), pd.Timestamp(row["bar_start"]))
        start_lookup[key] = row
    beta_by_day = dict(zip(model_df["session_date"], betas))

    def sizing_counts(candidates: pd.DataFrame) -> dict[str, int]:
        counts = {
            "candidate_closes": int(len(candidates)), "contiguous_next_bar": 0,
            "size_feasible": 0, "size_rejected": 0, "no_next_bar": 0,
            "size_reject_residual_cap": 0, "size_reject_gross_cap": 0,
            "size_reject_residual_and_gross": 0, "size_reject_other": 0,
        }
        for _, signal in candidates.iterrows():
            session_key = str(signal["session_date"])
            signal_time = pd.Timestamp(signal["local_ts"])
            next_row = start_lookup.get((session_key, signal_time))
            if next_row is None or signal_time.time() > cfg.latest_entry:
                counts["no_next_bar"] += 1
                continue
            counts["contiguous_next_bar"] += 1
            beta = float(beta_by_day.get(session_key, np.nan))
            if not np.isfinite(beta):
                counts["size_rejected"] += 1
                continue
            sized = choose_lot_size(
                beta, float(next_row["n_open"]), float(next_row["b_open"]),
                int(next_row["n_lot_size"]), int(next_row["b_lot_size"]), cfg,
            )
            if sized is None:
                counts["size_rejected"] += 1
                reason = explain_lot_size_rejection(
                    beta, float(next_row["n_open"]), float(next_row["b_open"]),
                    int(next_row["n_lot_size"]), int(next_row["b_lot_size"]), cfg,
                )
                key = {
                    "residual_cap": "size_reject_residual_cap",
                    "gross_cap": "size_reject_gross_cap",
                    "residual_and_gross_caps": "size_reject_residual_and_gross",
                }.get(reason, "size_reject_other")
                counts[key] += 1
            else:
                counts["size_feasible"] += 1
        return counts

    live_candidates = bars[bars["live_long"] | bars["live_short"]]
    raw_candidates = bars[bars["diag_long"] | bars["diag_short"]]
    live_size = sizing_counts(live_candidates)
    raw_size = sizing_counts(raw_candidates)

    z_stats = bars.groupby("session_date")["z_counterfactual"].agg(
        diagnostic_z_min="min", diagnostic_z_max="max"
    ).reset_index()
    count_fields = [
        "diag_long", "diag_short", "counterfactual_invalid_model", "live_long", "live_short"
    ]
    counts = bars.groupby("session_date")[count_fields].sum().reset_index()
    session_diag = model_df.merge(z_stats, on="session_date", how="left").merge(
        counts, on="session_date", how="left"
    )
    session_diag = session_diag.rename(columns={
        "diag_long": "counterfactual_or_live_long_closes",
        "diag_short": "counterfactual_or_live_short_closes",
        "counterfactual_invalid_model": "invalid_model_signal_closes",
        "live_long": "valid_model_long_closes",
        "live_short": "valid_model_short_closes",
    })

    session_counts = front.groupby("session_date").size()
    gaps_over_5m = 0
    for _, frame in front.groupby("session_date"):
        differences = frame.sort_index().index.to_series().diff().dropna()
        gaps_over_5m += int((differences > pd.Timedelta(minutes=5)).sum())
    expiry_by_session = front.groupby("session_date")["expiry"].first().sort_index()
    expiry_switches = int((expiry_by_session != expiry_by_session.shift()).sum() - 1) if len(expiry_by_session) else 0
    log_n = np.log(front["n_adj_close"].where(front["n_adj_close"] > 0))
    log_b = np.log(front["b_adj_close"].where(front["b_adj_close"] > 0))
    r_n = log_n.groupby(front["session_date"]).diff()
    r_b = log_b.groupby(front["session_date"]).diff()
    return_corr = float(r_n.corr(r_b)) if r_n.notna().sum() > 2 and r_b.notna().sum() > 2 else np.nan

    summary = {
        "diagnostic_test_sessions": int(len(model_df)),
        "diagnostic_valid_model_sessions": int(model_valid.sum()),
        "diagnostic_invalid_model_sessions": int((~model_valid).sum()),
        "diagnostic_cointegration_threshold": cfg.cointegration_pvalue_threshold,
        "diagnostic_pvalue_min": float(pvalues.min()) if pvalues.notna().any() else None,
        "diagnostic_pvalue_median": float(pvalues.median()) if pvalues.notna().any() else None,
        "diagnostic_pvalue_max": float(pvalues.max()) if pvalues.notna().any() else None,
        "diagnostic_beta_min": float(betas.min()) if betas.notna().any() else None,
        "diagnostic_beta_median": float(betas.median()) if betas.notna().any() else None,
        "diagnostic_beta_max": float(betas.max()) if betas.notna().any() else None,
        "diagnostic_beta_median_abs_day_change": float(beta_changes.median()) if len(beta_changes) else None,
        "diagnostic_counterfactual_z_signal_closes": int((bars["diag_long"] | bars["diag_short"]).sum()),
        "diagnostic_invalid_model_signal_closes": int(bars["counterfactual_invalid_model"].sum()),
        "diagnostic_valid_model_signal_closes": int((bars["live_long"] | bars["live_short"]).sum()),
        "diagnostic_live_signal_contiguous_next_bars": live_size["contiguous_next_bar"],
        "diagnostic_live_signal_size_feasible": live_size["size_feasible"],
        "diagnostic_live_signal_size_rejected": live_size["size_rejected"],
        "diagnostic_live_signal_size_reject_residual_cap": live_size["size_reject_residual_cap"],
        "diagnostic_live_signal_size_reject_gross_cap": live_size["size_reject_gross_cap"],
        "diagnostic_live_signal_size_reject_residual_and_gross": live_size["size_reject_residual_and_gross"],
        "diagnostic_live_signal_size_reject_other": live_size["size_reject_other"],
        "diagnostic_live_signal_no_next_bar": live_size["no_next_bar"],
        "diagnostic_counterfactual_size_feasible_if_gate_ignored": raw_size["size_feasible"],
        "diagnostic_counterfactual_size_rejected_if_gate_ignored": raw_size["size_rejected"],
        "diagnostic_counterfactual_size_reject_residual_cap": raw_size["size_reject_residual_cap"],
        "diagnostic_counterfactual_size_reject_gross_cap": raw_size["size_reject_gross_cap"],
        "diagnostic_counterfactual_size_reject_residual_and_gross": raw_size["size_reject_residual_and_gross"],
        "diagnostic_front_sessions": int(len(session_counts)),
        "diagnostic_bars_per_session_min": int(session_counts.min()) if len(session_counts) else 0,
        "diagnostic_bars_per_session_median": float(session_counts.median()) if len(session_counts) else 0.0,
        "diagnostic_bars_per_session_max": int(session_counts.max()) if len(session_counts) else 0,
        "diagnostic_intrasession_gaps_over_5m": int(gaps_over_5m),
        "diagnostic_distinct_expiries": int(front["expiry"].nunique()),
        "diagnostic_expiry_switches": expiry_switches,
        "diagnostic_same_session_log_return_correlation": return_corr,
    }
    return summary, session_diag


def print_run_diagnostics(
    diagnostics: dict[str, Any],
    session_diag: pd.DataFrame,
    cfg: StrategyConfig,
) -> None:
    print("\n" + "=" * 96)
    print("BACKTEST TROUBLESHOOTING / MODEL DIAGNOSTICS (not trading recommendations)")
    print("=" * 96)
    print(json.dumps(diagnostics, indent=2, allow_nan=True))
    if diagnostics.get("diagnostic_valid_model_sessions", 0) == 0:
        print(
            "BLOCKER: no session passed the Engle-Granger gate. Under this configuration, "
            "the actual strategy cannot enter; sizing and execution are downstream."
        )
    invalid_signal_closes = int(diagnostics.get("diagnostic_invalid_model_signal_closes", 0) or 0)
    if invalid_signal_closes:
        print(
            f"NOTE: {invalid_signal_closes} OLS-residual threshold closes occurred on "
            "sessions where cointegration failed. These are counterfactual diagnostics; "
            "they were NOT traded."
        )
    beta_min = diagnostics.get("diagnostic_beta_min")
    beta_max = diagnostics.get("diagnostic_beta_max")
    if beta_min is not None and beta_max is not None and beta_max - beta_min > 0.5:
        print("WARNING: large beta range; investigate relationship stability and contract-roll inputs.")
    if session_diag.empty:
        return
    columns = [
        "session_date", "training_bars", "alpha", "beta", "cointegration_pvalue",
        "coint_statistic", "critical_5pct", "r_squared", "return_correlation",
        "cointegration_valid", "diagnostic_z_min", "diagnostic_z_max",
        "invalid_model_signal_closes", "valid_model_long_closes", "valid_model_short_closes",
    ]
    columns = [c for c in columns if c in session_diag.columns]
    display = session_diag[columns].copy()
    for col in display.columns:
        if pd.api.types.is_numeric_dtype(display[col]):
            numeric = pd.to_numeric(display[col], errors="coerce")
            if col == "cointegration_pvalue":
                display[col] = numeric.map(lambda value: f"{value:.3e}" if pd.notna(value) else "NaN")
            else:
                display[col] = numeric.round(5)
    print("\nPer-session calibration and counterfactual signal audit:")
    with pd.option_context("display.max_rows", None, "display.max_columns", None, "display.width", 240):
        print(display.to_string(index=False, na_rep="NaN"))
    print("\nInterpretation:")
    print(f"  Baseline EG gate: p-value < {cfg.cointegration_pvalue_threshold:g}")
    print(f"  Entry thresholds: Z <= {-cfg.entry_z:g} / Z >= {cfg.entry_z:g}; target Z={cfg.target_z:g}; stop +/-{cfg.stop_z:g}")
    print(f"  Size gate: NIFTY lots {cfg.base_nifty_lots}-{cfg.max_nifty_lots}; residual <= Rs {cfg.max_residual_rupees:,.0f}; gross <= Rs {cfg.margin_pool * cfg.max_gross_multiple:,.0f}")
    print("  Counterfactual signals are never executed while the cointegration gate is false.")


def synthetic_front(n_sessions: int = 150, bars_per_session: int = 77, seed: int = 7) -> tuple[pd.DataFrame, date, date]:
    """Deterministic synthetic series for code smoke testing; not market data."""
    rng = np.random.default_rng(seed)
    weekdays: list[date] = []
    d = date(2025, 1, 1)
    while len(weekdays) < n_sessions:
        if d.weekday() < 5:
            weekdays.append(d)
        d += timedelta(days=1)
    rows: list[dict[str, Any]] = []
    log_bank = math.log(20_000.0)
    residual = 0.0
    beta = 0.72
    # Synthetic index levels are chosen only to exercise the configured 65/30
    # hedge sizing in this software smoke test; they are not market observations.
    alpha = math.log(8_547.0) - beta * math.log(20_000.0)
    step = 0
    for day in weekdays:
        start = datetime.combine(day, dtime(9, 15), tzinfo=IST)
        for j in range(bars_per_session):
            ts = pd.Timestamp(start + timedelta(minutes=5 * j))
            log_bank += rng.normal(0.0, 0.0008)
            residual = 0.88 * residual + rng.normal(0.0, 0.0009) + 0.0015 * math.sin(step / 55.0)
            log_n = alpha + beta * log_bank + residual
            bank_close = math.exp(log_bank)
            nifty_close = math.exp(log_n)
            bank_range = rng.uniform(0.0001, 0.0006)
            nifty_range = rng.uniform(0.0001, 0.0006)
            rows.append({
                "bar_start": ts,
                "bar_end": ts + pd.Timedelta(minutes=5),
                "session_date": day,
                "expiry": date(2099, 12, 31),
                "is_expiry_day": False,
                "n_open": nifty_close * (1 + rng.normal(0, 0.00015)),
                "n_high": nifty_close * (1 + nifty_range),
                "n_low": nifty_close * (1 - nifty_range),
                "n_close": nifty_close,
                "b_open": bank_close * (1 + rng.normal(0, 0.00015)),
                "b_high": bank_close * (1 + bank_range),
                "b_low": bank_close * (1 - bank_range),
                "b_close": bank_close,
                "n_lot_size": 65,
                "b_lot_size": 30,
                "n_scale": 1.0,
                "b_scale": 1.0,
                "n_adj_close": nifty_close,
                "b_adj_close": bank_close,
                "nifty_symbol": "SYNTHETIC-NIFTY-FUT",
                "bank_symbol": "SYNTHETIC-BANK-FUT",
            })
            step += 1
    front = pd.DataFrame(rows).set_index("bar_end").sort_index()
    return front, weekdays[90], weekdays[-1]


def write_outputs(
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    models: pd.DataFrame,
    summary: dict[str, Any],
    front: pd.DataFrame,
    session_diagnostics: pd.DataFrame,
    output_dir: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    trades.to_csv(output_dir / "trades.csv", index=False)
    equity.to_csv(output_dir / "equity_curve.csv", index=False)
    models.to_csv(output_dir / "daily_models.csv", index=False)
    front_export = front.copy()
    front_export.index.name = "bar_end"
    front_export.reset_index().to_csv(output_dir / "aligned_front_month.csv", index=False)
    session_diagnostics.to_csv(output_dir / "session_diagnostics.csv", index=False)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"\nWrote: {output_dir / 'trades.csv'}")
    print(f"Wrote: {output_dir / 'equity_curve.csv'}")
    print(f"Wrote: {output_dir / 'daily_models.csv'}")
    print(f"Wrote: {output_dir / 'aligned_front_month.csv'}")
    print(f"Wrote: {output_dir / 'session_diagnostics.csv'}")
    print(f"Wrote: {output_dir / 'summary.json'}")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backtest-only NIFTY/BANKNIFTY futures pairs strategy.")
    parser.add_argument("--env-file", default="env.txt", help="Private KEY=VALUE file; only UPSTOX_ACCESS_TOKEN is read.")
    parser.add_argument("--start-date", type=_parse_date, help="First out-of-sample date (YYYY-MM-DD).")
    parser.add_argument("--end-date", type=_parse_date, help="Last out-of-sample date (YYYY-MM-DD).")
    parser.add_argument("--lookback-sessions", type=int, default=60, help="EOD calibration lookback in trading sessions.")
    parser.add_argument("--z-window", type=int, default=50, help="Number of previous completed aligned bars for Z-score.")
    parser.add_argument(
        "--cointegration-pvalue-threshold", type=float, default=0.05,
        help="Research override for EG p-value gate (baseline 0.05; changing it is a strategy change, not proof of profitability).",
    )
    parser.add_argument("--entry-z", type=float, default=2.0)
    parser.add_argument("--target-z", type=float, default=0.0)
    parser.add_argument("--stop-z", type=float, default=3.0)
    parser.add_argument("--initial-capital", type=float, default=1_500_000.0)
    parser.add_argument("--max-gross-multiple", type=float, default=4.0)
    parser.add_argument("--daily-loss-fraction", type=float, default=0.03)
    parser.add_argument("--cost-per-side", type=float, default=0.0005,
                        help="Cost fraction per execution side per leg; 0.0005 = 0.05%.")
    parser.add_argument("--output-dir", default="backtest_output")
    parser.add_argument("--cache-dir", default="backtest_cache")
    parser.add_argument("--synthetic-demo", action="store_true", help="Run an offline synthetic smoke test.")
    parser.add_argument("--quiet-diagnostics", action="store_true", help="Suppress the detailed console diagnostics (CSV reports are still written).")
    return parser


def main() -> int:
    args = make_parser().parse_args()
    cfg = StrategyConfig(
        lookback_sessions=args.lookback_sessions,
        z_window=args.z_window,
        cointegration_pvalue_threshold=args.cointegration_pvalue_threshold,
        entry_z=args.entry_z,
        target_z=args.target_z,
        stop_z=args.stop_z,
        margin_pool=args.initial_capital,
        max_gross_multiple=args.max_gross_multiple,
        daily_loss_fraction=args.daily_loss_fraction,
        cost_per_execution_side=args.cost_per_side,
    )
    if cfg.lookback_sessions < 2 or cfg.z_window < 2:
        raise BacktestError("lookback-sessions and z-window must be >= 2.")
    if not 0.0 < cfg.cointegration_pvalue_threshold <= 1.0:
        raise BacktestError("--cointegration-pvalue-threshold must be in (0, 1].")

    if args.synthetic_demo:
        front, start, end = synthetic_front()
        test_start = args.start_date or start
        test_end = args.end_date or end
        warnings = ["Synthetic data only: this run is a software smoke test, not market evidence."]
    else:
        if args.start_date is None or args.end_date is None:
            raise BacktestError("For Upstox history, provide both --start-date and --end-date.")
        if args.start_date > args.end_date:
            raise BacktestError("--start-date must be on/before --end-date.")
        token = read_access_token(Path(args.env_file))
        api = UpstoxHistoryClient(token, Path(args.cache_dir))
        front, warnings = fetch_upstox_front_month(api, args.start_date, args.end_date, cfg.lookback_sessions)
        test_start, test_end = args.start_date, args.end_date

    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    print(f"Aligned bars: {len(front):,}; sessions: {front['session_date'].nunique():,}")
    print("No forward-fill is used. Historical entries fill at next-bar open; no orders are sent.")
    trades, equity, models, summary = run_backtest(front, test_start, test_end, cfg)
    summary["aligned_bars"] = int(len(front))
    summary["aligned_sessions"] = int(front["session_date"].nunique())
    summary["lookback_sessions"] = cfg.lookback_sessions
    summary["z_window_bars"] = cfg.z_window
    summary["entry_latest_ist"] = cfg.latest_entry.strftime("%H:%M")
    summary["flatten_ist"] = cfg.flatten_time.strftime("%H:%M")
    diagnostics, session_diagnostics = collect_run_diagnostics(front, equity, models, cfg)
    summary.update(diagnostics)
    write_outputs(
        trades, equity, models, summary, front, session_diagnostics, Path(args.output_dir)
    )
    if not args.quiet_diagnostics:
        print_run_diagnostics(diagnostics, session_diagnostics, cfg)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BacktestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
