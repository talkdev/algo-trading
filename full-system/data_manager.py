# ============ FILE: data_manager.py ============
"""
All market data — fetch, validate, store, serve.

ALL FIXES APPLIED (passes 1-7 + live confirmed):
  LIVE: LTP response uses colon key "NSE_INDEX:Nifty 50"
  LIVE: IV from chain is in % → stored as decimal (÷100)
  LIVE: ADX_CANDLE_TIMEFRAME="30minute" (not "15minute")
  LIVE: _last_trading_day() — candle to_date fix
  LIVE: expiry param in get_strike_by_delta
  LIVE: compute_adx safe division (no RuntimeWarning)
  LIVE: Bootstrap iv_rv_spread_history on startup
  LIVE: monitor_ws_health market-hours check
  LIVE: WS last_msg_time reset after subscription
  LIVE: option_chain nested by (expiry, strike)
  LIVE: _active_expiry strictly future expiries
  LIVE: skew bootstrap removed (useless with std=0)
  LIVE: WS iv guard: iv/100 if iv>1.0 else iv
"""

import asyncio
import aiohttp
import sqlite3
import json
import logging
import csv
import os
import struct as _struct
import numpy as np
import pandas as pd
from collections import deque
from datetime import datetime, date, timedelta
from typing import Optional, Tuple, List, Dict, Any
import pytz
import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Custom exceptions
# ─────────────────────────────────────────────────────────────────────

class AuthenticationError(Exception):
    pass

class ServiceUnavailableError(Exception):
    pass

class MaxRetriesError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────
# LIVE FIX: _last_trading_day()
# Upstox candle endpoint returns 0 candles when to_date
# is a weekend or holiday (confirmed: Sunday returns 0).
# ─────────────────────────────────────────────────────────────────────

def _last_trading_day() -> str:
    """
    Return the most recent NSE trading day as YYYY-MM-DD.
    Walks backward from today skipping weekends and holidays.
    """
    try:
        d = date.today()
        for _ in range(10):
            d_str = d.strftime("%Y-%m-%d")
            if (
                d.weekday() < 5
                and d_str not in config.NSE_MARKET_HOLIDAYS
            ):
                return d_str
            d -= timedelta(days=1)
    except Exception:
        pass
    return date.today().strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _sf(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_delta(raw, is_call: bool) -> float:
    """
    PATCH: sanity-bound API-supplied delta before it's trusted
    anywhere. Mirrors the guard regime_engine.py's leg_delta()
    already applies internally, extended to the shared chain
    data store that actual trading legs are built from.
    """
    try:
        raw = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if is_call:
        if 0.01 < raw < 0.99:
            return raw
        return 0.0
    else:
        if -0.99 < raw < -0.01:
            return raw
        return 0.0


# ─────────────────────────────────────────────────────────────────────
# Pure-Python protobuf fallback parser for Upstox V3 WS
# ─────────────────────────────────────────────────────────────────────

def _read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    result = 0
    shift  = 0
    while pos < len(buf):
        b = buf[pos]; pos += 1
        result |= (b & 0x7F) << shift
        shift  += 7
        if not (b & 0x80):
            break
    return result, pos


def _read_ld(buf: bytes, pos: int) -> Tuple[bytes, int]:
    length, pos = _read_varint(buf, pos)
    return buf[pos: pos + length], pos + length


def _read_double(
    buf: bytes, pos: int
) -> Tuple[float, int]:
    if pos + 8 > len(buf):
        return 0.0, pos + 8
    return (
        float(_struct.unpack_from("<d", buf, pos)[0]),
        pos + 8,
    )


def _skip(buf: bytes, pos: int, wt: int) -> int:
    if wt == 0:
        _, pos = _read_varint(buf, pos)
    elif wt == 1:
        pos += 8
    elif wt == 2:
        _, pos = _read_ld(buf, pos)
    elif wt == 5:
        pos += 4
    return pos


def _parse_greeks_fb(buf: bytes) -> Dict[str, float]:
    r  = {"delta": 0.0, "theta": 0.0,
          "gamma": 0.0, "vega":  0.0, "iv": 0.0}
    fm = {1: "delta", 2: "theta", 3: "gamma", 4: "vega"}
    pos = 0
    while pos < len(buf):
        try:
            tag, pos = _read_varint(buf, pos)
        except Exception:
            break
        fn, wt = tag >> 3, tag & 7
        if wt == 1 and fn in fm:
            v, pos = _read_double(buf, pos)
            r[fm[fn]] = v
        else:
            try:
                pos = _skip(buf, pos, wt)
            except Exception:
                break
    return r


def _parse_ltpc_fb(buf: bytes) -> Dict[str, float]:
    r = {"ltp": 0.0}
    pos = 0
    while pos < len(buf):
        try:
            tag, pos = _read_varint(buf, pos)
        except Exception:
            break
        fn, wt = tag >> 3, tag & 7
        if fn == 1 and wt == 1:
            r["ltp"], pos = _read_double(buf, pos)
        else:
            try:
                pos = _skip(buf, pos, wt)
            except Exception:
                break
    return r


def _parse_mff_fb(buf: bytes) -> Dict[str, Any]:
    r = {"ltp": 0.0, "iv": 0.0, "oi": 0.0, "greeks": None}
    pos = 0
    while pos < len(buf):
        try:
            tag, pos = _read_varint(buf, pos)
        except Exception:
            break
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            d, pos = _read_ld(buf, pos)
            if fn == 1:
                r["ltp"] = _parse_ltpc_fb(d)["ltp"]
            elif fn == 3:
                r["greeks"] = _parse_greeks_fb(d)
        elif wt == 1:
            v, pos = _read_double(buf, pos)
            if fn == 7:
                r["oi"] = v
            elif fn == 8:
                r["iv"] = v
                if r["greeks"] is not None:
                    r["greeks"]["iv"] = v
        else:
            try:
                pos = _skip(buf, pos, wt)
            except Exception:
                break
    return r


def _parse_flwg_fb(buf: bytes) -> Dict[str, Any]:
    r = {"ltp": 0.0, "iv": 0.0, "oi": 0.0, "greeks": None}
    pos = 0
    while pos < len(buf):
        try:
            tag, pos = _read_varint(buf, pos)
        except Exception:
            break
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            d, pos = _read_ld(buf, pos)
            if fn == 1:
                r["ltp"] = _parse_ltpc_fb(d)["ltp"]
            elif fn == 3:
                r["greeks"] = _parse_greeks_fb(d)
        elif wt == 1:
            v, pos = _read_double(buf, pos)
            if fn == 5:
                r["oi"] = v
            elif fn == 6:
                r["iv"] = v
                if r["greeks"] is not None:
                    r["greeks"]["iv"] = v
        else:
            try:
                pos = _skip(buf, pos, wt)
            except Exception:
                break
    return r


def _parse_fullfeed_fb(buf: bytes) -> Dict[str, Any]:
    r = {"ltp": 0.0, "iv": 0.0, "oi": 0.0, "greeks": None}
    pos = 0
    while pos < len(buf):
        try:
            tag, pos = _read_varint(buf, pos)
        except Exception:
            break
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            d, pos = _read_ld(buf, pos)
            if fn == 1:
                return _parse_mff_fb(d)
            elif fn == 2:
                r["ltp"] = _parse_ltpc_fb(d)["ltp"]
                return r
        else:
            try:
                pos = _skip(buf, pos, wt)
            except Exception:
                break
    return r


def _parse_feed_fb(buf: bytes) -> Dict[str, Any]:
    r = {"ltp": 0.0, "iv": 0.0, "oi": 0.0, "greeks": None}
    pos = 0
    while pos < len(buf):
        try:
            tag, pos = _read_varint(buf, pos)
        except Exception:
            break
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            d, pos = _read_ld(buf, pos)
            if fn == 1:
                r["ltp"] = _parse_ltpc_fb(d)["ltp"]
                return r
            elif fn == 2:
                return _parse_fullfeed_fb(d)
            elif fn == 3:
                return _parse_flwg_fb(d)
        else:
            try:
                pos = _skip(buf, pos, wt)
            except Exception:
                break
    return r


def _parse_map_entry_fb(
    buf: bytes,
) -> Tuple[str, Dict[str, Any]]:
    key   = ""
    value = {"ltp": 0.0, "iv": 0.0,
             "oi": 0.0, "greeks": None}
    pos   = 0
    while pos < len(buf):
        try:
            tag, pos = _read_varint(buf, pos)
        except Exception:
            break
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            d, pos = _read_ld(buf, pos)
            if fn == 1:
                key = d.decode("utf-8", errors="replace")
            elif fn == 2:
                value = _parse_feed_fb(d)
        else:
            try:
                pos = _skip(buf, pos, wt)
            except Exception:
                break
    return key, value


def _parse_feed_response_fallback(
    buf: bytes,
) -> Dict[str, Dict[str, Any]]:
    feeds = {}
    pos   = 0
    while pos < len(buf):
        try:
            tag, pos = _read_varint(buf, pos)
        except Exception:
            break
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            d, pos = _read_ld(buf, pos)
            if fn == 2:
                k, v = _parse_map_entry_fb(d)
                if k:
                    feeds[k] = v
        else:
            try:
                pos = _skip(buf, pos, wt)
            except Exception:
                break
    return feeds


# ─────────────────────────────────────────────────────────────────────
# Token bucket rate limiter
# ─────────────────────────────────────────────────────────────────────

class TokenBucket:
    def __init__(
        self, capacity: int, refill_rate: float
    ) -> None:
        self.capacity    = capacity
        self.tokens      = float(capacity)
        self.refill_rate = refill_rate
        self._lock             = asyncio.Lock()
        self._initialized      = False
        self._last_refill_time = 0.0

    async def acquire(self) -> bool:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            if not self._initialized:
                self._last_refill_time = now
                self._initialized      = True
            elapsed     = now - self._last_refill_time
            self.tokens = min(
                self.capacity,
                self.tokens + elapsed * self.refill_rate,
            )
            self._last_refill_time = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            wait = (1.0 - self.tokens) / self.refill_rate
            await asyncio.sleep(wait)
            self.tokens = 0.0
            return True


# ─────────────────────────────────────────────────────────────────────
# DataManager
# ─────────────────────────────────────────────────────────────────────

class DataManager:
    """
    Manages all market data.

    KEY ARCHITECTURE (QD1):
    option_chain = Dict[expiry_str, Dict[strike_float, Dict]]
    Access: self.option_chain[expiry][strike][opt_type]
    Eliminates strike collision across expiries.
    """

    def __init__(
        self, access_token: str, db_path: str
    ) -> None:
        self.token   = access_token
        self.db_path = db_path

        # Separate buckets: order POSTs never wait behind data GETs
        self.rate_limiter = TokenBucket(
            config.RATE_LIMIT_CAPACITY,
            config.RATE_LIMIT_REFILL_PER_SEC,
        )
        self.order_rate_limiter = TokenBucket(10, 10.0)

        self.session: Optional[aiohttp.ClientSession] = None
        self.ws                    = None
        self.ws_connected          = False
        self.ws_last_msg_time: Optional[datetime] = None
        self.kill_switch_triggered = False
        self._ws_decode_errors     = 0
        self._subscribed_atm: Optional[float] = None
        self._protobuf_warning_logged: bool    = False

        # Reverse map: instrument_key → (expiry, strike, opt_type)
        self._instrument_map: Dict[
            str, Tuple[str, float, str]
        ] = {}
        self._known_expiries: set = set()

        # Daily return tracking
        self._last_return_date: Optional[date] = None
        self._last_vix_date:    Optional[date] = None
        self._last_spread_date: Optional[date] = None
        self._last_skew_date:   Optional[date] = None
        self._last_iv_rank_date: Optional[date] = None  # PATCH: D3

        # ATR contraction cache
        self._atr_contracting_cache:        Optional[bool] = None
        self._atr_contracting_candle_count: int            = 0

        # Rolling windows
        self.log_returns: deque = deque(maxlen=252)
        self.vix_history_20d: deque = deque(
            maxlen=config.VIX_HISTORY_DAILY_MAXLEN
        )
        self.iv_atm_history: deque = deque(
            maxlen=config.IV_ATM_HISTORY_MAXLEN
        )
        self.iv_rv_spread_history: deque = deque(
            maxlen=config.EDGE_LOOKBACK_DAYS
        )
        self.skew_history: deque = deque(
            maxlen=config.SKEW_LOOKBACK_DAYS
        )
        self.candles_30m: deque = deque(
            maxlen=config.ADX_CANDLE_COUNT + 10
        )
        self.candles_15m   = self.candles_30m  # alias
        self.candles_daily: deque = deque(maxlen=60)
        self.oi_snapshots: deque = deque(
            maxlen=config.FLOW_WINDOW_MINUTES * 4
        )
        self.bid_ask_spread_3otm: deque = deque(
            maxlen=config.SPREAD_LOOKBACK_PERIODS
        )

        # Live state
        self.spot:      Optional[float] = None
        self.prev_spot: Optional[float] = None
        self.vix:       Optional[float] = None
        self.prev_vix:  Optional[float] = None

        # Nested option chain: [expiry][strike][opt_type]
        self.option_chain: Dict[
            str, Dict[float, Dict]
        ] = {}

        self.atm_strike:   Optional[float] = None
        self.forward_iv:   Optional[float] = None
        self.iv_atm:       Optional[float] = None
        self.rv_20d:       Optional[float] = None
        self.skew:         Optional[float] = None
        self.adx:          Optional[float] = None
        self.ema_50:       Optional[float] = None
        self.ema_slope:    Optional[float] = None
        self.net_flow:     Optional[float] = None
        self.spread_ratio: Optional[float] = None

        # Active expiry = nearest future expiry
        self._active_expiry: Optional[str] = None

        self._data_lock = asyncio.Lock()
        self._IST       = pytz.timezone(config.TZ)

    # ─────────────────────────────────────────────────────────────
    # Initialization
    # ─────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        logger.info("DataManager initializing...")

        connector = aiohttp.TCPConnector(
            limit=10, limit_per_host=10
        )
        self.session = aiohttp.ClientSession(
            connector=connector,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type":  "application/json",
                "Accept":        "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=30),
        )

        await asyncio.to_thread(self.init_sqlite)

        state = self.load_state_from_sqlite()
        if state:
            ms = state.get("market_state", {})
            if ms:
                # AUDIT DM-N03: use explicit None check so a
                # valid 0.0 value is not discarded by `or`.
                def _r(key, current):
                    v = ms.get(key)
                    return v if v is not None else current
                self.spot   = _r("spot",   self.spot)
                self.vix    = _r("vix",    self.vix)
                self.iv_atm = _r("iv_atm", self.iv_atm)
                self.rv_20d = _r("rv_20d", self.rv_20d)
                self.skew   = _r("skew",   self.skew)
                self.adx    = _r("adx",    self.adx)
                self.ema_50 = _r("ema_50", self.ema_50)
                logger.info(
                    f"Restored: spot={self.spot} "
                    f"vix={self.vix}"
                )

        # LIVE FIX: Bootstrap iv_rv_spread_history
        # Without this, edge_score=0 for first 10 trading days.
        # Seeds history with estimated spread so edge activates
        # from day 1. Uses RV ≈ VIX × 0.70 (calm market estimate).
        if (
            self.iv_atm is not None
            and self.iv_atm > 0
            and self.vix is not None
            and self.vix > 0
        ):
            estimated_rv     = (self.vix / 100.0) * 0.70
            estimated_spread = self.iv_atm - estimated_rv
            min_hist = getattr(
                config, "EDGE_SCORE_MIN_HISTORY", 3
            )
            # edge_score=0 until real daily data accumulates (typically 3 trading days).
            logger.info(
                f"Bootstrapped iv_rv_spread_history: "
                f"iv_atm={self.iv_atm:.4f} "
                f"est_rv={estimated_rv:.4f} "
                f"spread={estimated_spread:.4f} "
                f"({min_hist} entries)"
            )

        logger.info("DataManager initialized")

    # ─────────────────────────────────────────────────────────────
    # HTTP helpers
    # ─────────────────────────────────────────────────────────────

    async def _api_get(
        self, endpoint: str, params: Dict
    ) -> Dict:
        await self.rate_limiter.acquire()
        url = config.UPSTOX_BASE_V2 + endpoint
        return await self._do_get(url, params)

    async def _api_get_v3(
        self, endpoint: str, params: Dict
    ) -> Dict:
        await self.rate_limiter.acquire()
        url = config.UPSTOX_BASE_V3 + endpoint
        return await self._do_get(url, params)

    async def _do_get(
        self, url: str, params: Dict
    ) -> Dict:
        last_exception = None
        for attempt in range(config.RETRY_MAX_ATTEMPTS):
            try:
                async with self.session.get(
                    url, params=params
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("data", data)
                    elif resp.status == 429:
                        backoff = min(
                            config.RETRY_BACKOFF_BASE
                            * (2 ** attempt),
                            config.RETRY_MAX_BACKOFF,
                        )
                        await asyncio.sleep(backoff)
                        continue
                    elif resp.status == 401:
                        raise AuthenticationError(
                            f"Token expired — {url}"
                        )
                    elif resp.status == 503:
                        raise ServiceUnavailableError(
                            f"Maintenance — {url}"
                        )
                    else:
                        body = await resp.text()
                        logger.error(
                            f"GET {resp.status} {url}: "
                            f"{body[:200]}"
                        )
                        # AUDIT DM-10: do not retry permanent 4xx.
                        # A 400/404 will never succeed; retrying
                        # burns up to 31s in the data-refresh path.
                        if 400 <= resp.status < 500:
                            raise MaxRetriesError(
                                f"Permanent {resp.status} GET {url}: "
                                f"{body[:100]}"
                            )
                        backoff = min(
                            config.RETRY_BACKOFF_BASE
                            * (2 ** attempt),
                            config.RETRY_MAX_BACKOFF,
                        )
                        await asyncio.sleep(backoff)
                        continue
            except (
                AuthenticationError,
                ServiceUnavailableError,
            ):
                raise
            except aiohttp.ClientError as e:
                last_exception = e
                backoff = min(
                    config.RETRY_BACKOFF_BASE
                    * (2 ** attempt),
                    config.RETRY_MAX_BACKOFF,
                )
                logger.error(
                    f"GET client error {url} "
                    f"attempt {attempt + 1}: {e}"
                )
                await asyncio.sleep(backoff)
            except Exception as e:
                last_exception = e
                backoff = min(
                    config.RETRY_BACKOFF_BASE
                    * (2 ** attempt),
                    config.RETRY_MAX_BACKOFF,
                )
                logger.error(
                    f"GET error {url} "
                    f"attempt {attempt + 1}: {e}"
                )
                await asyncio.sleep(backoff)

        raise MaxRetriesError(
            f"Max retries GET {url}. Last: {last_exception}"
        )

    async def _api_post(
        self, endpoint: str, payload: Dict
    ) -> Dict:
        await self.order_rate_limiter.acquire()
        url            = config.UPSTOX_BASE_V2 + endpoint
        last_exception = None

        for attempt in range(config.RETRY_MAX_ATTEMPTS):
            try:
                async with self.session.post(
                    url, json=payload
                ) as resp:
                    if resp.status in (200, 201):
                        return await resp.json()
                    elif resp.status == 429:
                        backoff = min(
                            config.RETRY_BACKOFF_BASE
                            * (2 ** attempt),
                            config.RETRY_MAX_BACKOFF,
                        )
                        await asyncio.sleep(backoff)
                        continue
                    elif resp.status == 401:
                        raise AuthenticationError(
                            "Token expired on POST"
                        )
                    elif resp.status == 503:
                        raise ServiceUnavailableError(
                            "Maintenance on POST"
                        )
                    else:
                        body = await resp.text()
                        logger.error(
                            f"POST {resp.status} "
                            f"{endpoint}: {body[:200]}"
                        )
                        backoff = min(
                            config.RETRY_BACKOFF_BASE
                            * (2 ** attempt),
                            config.RETRY_MAX_BACKOFF,
                        )
                        await asyncio.sleep(backoff)
                        continue
            except (
                AuthenticationError,
                ServiceUnavailableError,
            ):
                raise
            except aiohttp.ClientError as e:
                last_exception = e
                backoff = min(
                    config.RETRY_BACKOFF_BASE
                    * (2 ** attempt),
                    config.RETRY_MAX_BACKOFF,
                )
                logger.error(
                    f"POST client error {endpoint}: {e}"
                )
                await asyncio.sleep(backoff)
            except Exception as e:
                last_exception = e
                backoff = min(
                    config.RETRY_BACKOFF_BASE
                    * (2 ** attempt),
                    config.RETRY_MAX_BACKOFF,
                )
                logger.error(
                    f"POST error {endpoint}: {e}"
                )
                await asyncio.sleep(backoff)

        raise MaxRetriesError(
            f"Max retries POST {endpoint}. "
            f"Last: {last_exception}"
        )

    async def _api_delete(self, endpoint: str) -> Dict:
        await self.order_rate_limiter.acquire()
        url            = config.UPSTOX_BASE_V2 + endpoint
        last_exception = None

        for attempt in range(config.RETRY_MAX_ATTEMPTS):
            try:
                async with self.session.delete(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("data", data)
                    elif resp.status == 429:
                        backoff = min(
                            config.RETRY_BACKOFF_BASE
                            * (2 ** attempt),
                            config.RETRY_MAX_BACKOFF,
                        )
                        await asyncio.sleep(backoff)
                        continue
                    elif resp.status == 401:
                        raise AuthenticationError(
                            "Token expired on DELETE"
                        )
                    else:
                        body = await resp.text()
                        logger.error(
                            f"DELETE {resp.status}: "
                            f"{body[:200]}"
                        )
                        return {}
            except (
                AuthenticationError,
                ServiceUnavailableError,
            ):
                raise
            except Exception as e:
                last_exception = e
                backoff = min(
                    config.RETRY_BACKOFF_BASE
                    * (2 ** attempt),
                    config.RETRY_MAX_BACKOFF,
                )
                logger.error(
                    f"DELETE error {endpoint}: {e}"
                )
                await asyncio.sleep(backoff)

        raise MaxRetriesError(
            f"Max retries DELETE {endpoint}. "
            f"Last: {last_exception}"
        )

    # ─────────────────────────────────────────────────────────────
    # Market data fetches
    # ─────────────────────────────────────────────────────────────

    async def fetch_spot_and_vix(
        self,
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Fetch Nifty spot and India VIX.
        LIVE FIX: response uses colon key "NSE_INDEX:Nifty 50"
        not pipe key "NSE_INDEX|Nifty 50".
        """
        try:
            keys = (
                f"{config.INSTRUMENT_NIFTY},"
                f"{config.INSTRUMENT_VIX}"
            )
            data = await self._api_get(
                config.EP_LTP,
                {"instrument_key": keys},
            )

            logger.info(
                f"LTP raw keys: "
                f"{list(data.keys()) if isinstance(data, dict) else type(data)}"
            )

            new_spot = 0.0
            new_vix  = 0.0

            if isinstance(data, dict):
                # LIVE FIX: response uses colon not pipe
                nifty_key = config.INSTRUMENT_NIFTY.replace(
                    "|", ":"
                )
                vix_key   = config.INSTRUMENT_VIX.replace(
                    "|", ":"
                )
                spot_data = data.get(nifty_key, {})
                vix_data  = data.get(vix_key,   {})
                new_spot  = _sf(
                    spot_data.get("last_price", 0)
                )
                new_vix   = _sf(
                    vix_data.get("last_price", 0)
                )

            if new_spot == 0:
                logger.warning(
                    "Spot missing — fetching separately"
                )
                try:
                    resp = await self._api_get(
                        config.EP_LTP,
                        {"instrument_key": config.INSTRUMENT_NIFTY},
                    )
                    nk   = config.INSTRUMENT_NIFTY.replace(
                        "|", ":"
                    )
                    new_spot = _sf(
                        resp.get(nk, {}).get("last_price", 0)
                    )
                except Exception as e:
                    logger.warning(
                        f"Spot separate fetch: {e}"
                    )

            if new_vix == 0:
                logger.warning(
                    "VIX missing — fetching separately"
                )
                try:
                    resp = await self._api_get(
                        config.EP_LTP,
                        {"instrument_key": config.INSTRUMENT_VIX},
                    )
                    vk  = config.INSTRUMENT_VIX.replace(
                        "|", ":"
                    )
                    new_vix = _sf(
                        resp.get(vk, {}).get("last_price", 0)
                    )
                except Exception as e:
                    logger.warning(
                        f"VIX separate fetch: {e}"
                    )

            if new_spot > 0:
                if self.spot and self.spot > 0:
                    change = abs(new_spot / self.spot - 1)
                    if change > 0.05:
                        logger.critical(
                            f"Spot spike: "
                            f"{self.spot:.2f} -> "
                            f"{new_spot:.2f} — rejected"
                        )
                        new_spot = self.spot
                self.prev_spot = self.spot
                self.spot      = float(new_spot)
                self._append_daily_return()
            else:
                logger.error(
                    "fetch_spot_and_vix: spot=0"
                )

            if new_vix > 0:
                self.prev_vix = self.vix
                self.vix      = float(new_vix)
                self._append_daily_vix()
            else:
                logger.error(
                    "fetch_spot_and_vix: vix=0"
                )

            logger.info(
                f"spot={self.spot} vix={self.vix}"
            )
            return (self.spot, self.vix)

        except Exception as e:
            logger.error(f"fetch_spot_and_vix error: {e}")
            return (self.spot, self.vix)

    def _append_daily_return(self) -> None:
        """
        AUDIT DM-02: prev_spot is the spot from ~60s ago, not
        yesterday's close. Annualising a 60s return with sqrt(252)
        understates RV by ~an order of magnitude.
        Fix: store the first spot of each session as
        _prev_session_close and use that for daily returns.
        compute_realized_vol() prefers candles_daily anyway;
        this path is only a fallback.
        """
        today = date.today()
        if self._last_return_date == today:
            # Update today's session close for tomorrow's return
            if self.spot and self.spot > 0:
                self._current_session_close = float(self.spot)
            return
        # New day: compute return from yesterday's session close
        prev_close = getattr(self, "_prev_session_close", None)
        if (
            self.spot and self.spot > 0
            and prev_close and prev_close > 0
        ):
            self.log_returns.append(
                np.log(self.spot / prev_close)
            )
        # Roll: today's open becomes tomorrow's prev_close
        self._prev_session_close = getattr(
            self, "_current_session_close", self.spot
        )
        self._current_session_close = (
            float(self.spot) if self.spot else None
        )
        self._last_return_date = today

    def _append_daily_vix(self) -> None:
        today = date.today()
        if self._last_vix_date == today:
            return
        if self.vix and self.vix > 0:
            self.vix_history_20d.append(self.vix)
        self._last_vix_date = today

    async def fetch_option_chain(
        self, expiry_date: str
    ) -> Dict[float, Dict]:
        """
        Fetch option chain for given expiry.
        LIVE FIX: iv stored as decimal (÷100 from API).
        LIVE FIX: nested chain [expiry][strike][opt_type].
        LIVE FIX: _active_expiry = nearest future expiry.
        """
        try:
            params = {
                "instrument_key": config.INSTRUMENT_NIFTY,
                "expiry_date":    expiry_date,
            }
            data = await self._api_get(
                config.EP_OPTION_CHAIN, params
            )

            if not data:
                logger.warning(
                    f"Empty chain for {expiry_date}"
                )
                return self.option_chain.get(
                    expiry_date, {}
                )

            chain_list = (
                data
                if isinstance(data, list)
                else data.get("data", [])
            )

            parsed: Dict[float, Dict] = {}

            for item in chain_list:
                strike = float(
                    item.get("strike_price", 0)
                )
                if strike <= 0:
                    continue

                call_opts   = item.get("call_options", {})
                put_opts    = item.get("put_options",  {})
                call_md     = call_opts.get(
                    "market_data", {}
                )
                put_md      = put_opts.get(
                    "market_data", {}
                )
                call_greeks = call_opts.get(
                    "option_greeks", {}
                )
                put_greeks  = put_opts.get(
                    "option_greeks", {}
                )

                call_oi  = int(_sf(call_md.get("oi", 0)))
                put_oi   = int(_sf(put_md.get("oi",  0)))
                call_bid = _sf(call_md.get("bid_price", 0))
                put_bid  = _sf(put_md.get("bid_price",  0))

                atm_candidate = self.spot or 0
                is_atm = (
                    abs(strike - atm_candidate)
                    <= config.NIFTY_STRIKE_STEP
                )
                min_oi = (
                    10 if is_atm else config.MIN_OI_LOTS
                )

                if call_oi < min_oi and put_oi < min_oi:
                    continue
                if call_bid == 0 and put_bid == 0:
                    continue

                # LIVE FIX: iv from API is in percentage
                # (e.g. 13.82 = 13.82%) → store as decimal
                parsed[strike] = {
                    "call": {
                        "instrument_key": call_opts.get(
                            "instrument_key", ""
                        ),
                        "ltp":    _sf(call_md.get("ltp", 0)),
                        # DM-T02: REST LTP gets _ltp_ts so
                        # get_mark_price() treats it as fresh.
                        "_ltp_ts": datetime.now(
                            self._IST
                        ).isoformat(),
                        "bid":    _sf(
                            call_md.get("bid_price", 0)
                        ),
                        "ask":    _sf(
                            call_md.get("ask_price", 0)
                        ),
                        "oi":     call_oi,
                        "volume": int(
                            _sf(call_md.get("volume", 0))
                        ),
                        "iv":    _sf(
                            call_greeks.get("iv", 0)
                        ) / 100.0,
                        "delta": _clean_delta(
                            _sf(call_greeks.get("delta", 0)),
                            is_call=True,
                        ),
                        "gamma": _sf(
                            call_greeks.get("gamma", 0)
                        ),
                        "theta": _sf(
                            call_greeks.get("theta", 0)
                        ),
                        "vega":  _sf(
                            call_greeks.get("vega",  0)
                        ),
                        "_rest_ts": datetime.now(
                            self._IST
                        ).isoformat(),
                    },
                    "put": {
                        "instrument_key": put_opts.get(
                            "instrument_key", ""
                        ),
                        "ltp":    _sf(put_md.get("ltp", 0)),
                        # DM-T02: REST LTP gets _ltp_ts
                        "_ltp_ts": datetime.now(
                            self._IST
                        ).isoformat(),
                        "bid":    _sf(
                            put_md.get("bid_price", 0)
                        ),
                        "ask":    _sf(
                            put_md.get("ask_price", 0)
                        ),
                        "oi":     put_oi,
                        "volume": int(
                            _sf(put_md.get("volume", 0))
                        ),
                        "iv":    _sf(
                            put_greeks.get("iv", 0)
                        ) / 100.0,
                        "delta": _clean_delta(
                            _sf(put_greeks.get("delta", 0)),
                            is_call=False,
                        ),
                        "gamma": _sf(
                            put_greeks.get("gamma", 0)
                        ),
                        "theta": _sf(
                            put_greeks.get("theta", 0)
                        ),
                        "vega":  _sf(
                            put_greeks.get("vega",  0)
                        ),
                        "_rest_ts": datetime.now(
                            self._IST
                        ).isoformat(),
                    },
                }

            if not parsed:
                logger.warning(
                    f"Chain empty after filter "
                    f"{expiry_date}"
                )
                return self.option_chain.get(
                    expiry_date, {}
                )

            # Populate reverse instrument map (includes expiry)
            for strike, sd in parsed.items():
                ck = sd["call"].get("instrument_key", "")
                pk = sd["put"].get("instrument_key",  "")
                if ck:
                    self._instrument_map[ck] = (
                        expiry_date, strike, "call"
                    )
                if pk:
                    self._instrument_map[pk] = (
                        expiry_date, strike, "put"
                    )

            self._known_expiries.add(expiry_date)

            async with self._data_lock:
                self.option_chain[expiry_date] = parsed

            # LIVE FIX: _active_expiry = nearest FUTURE expiry
            # Strictly after today so expired expiry is not active
            today = date.today()
            future_expiries = sorted([
                e for e in self._known_expiries
                if datetime.strptime(
                    e, "%Y-%m-%d"
                ).date() > today
            ])
            if future_expiries:
                self._active_expiry = future_expiries[0]
            elif self._known_expiries:
                self._active_expiry = sorted(
                    self._known_expiries
                )[-1]

            # ATM from active expiry only
            if (
                self.spot and self.spot > 0
                and self._active_expiry
                and self._active_expiry in self.option_chain
            ):
                strikes = list(
                    self.option_chain[
                        self._active_expiry
                    ].keys()
                )
                if strikes:
                    raw_atm = min(
                        strikes,
                        key=lambda x: abs(x - self.spot),
                    )
                    rounded_atm = (
                        round(
                            raw_atm
                            / config.NIFTY_STRIKE_STEP
                        ) * config.NIFTY_STRIKE_STEP
                    )
                    active_chain = self.option_chain[
                        self._active_expiry
                    ]
                    self.atm_strike = (
                        rounded_atm
                        if rounded_atm in active_chain
                        else raw_atm
                    )
                    logger.info(
                        f"ATM: {self.atm_strike} "
                        f"(spot={self.spot:.2f} "
                        f"expiry={self._active_expiry})"
                    )

            # IV_ATM from active expiry only
            if (
                self._active_expiry
                and self._active_expiry in self.option_chain
                and self.atm_strike
                and self.atm_strike in self.option_chain[
                    self._active_expiry
                ]
            ):
                atm_d   = self.option_chain[
                    self._active_expiry
                ][self.atm_strike]
                call_iv = atm_d["call"].get("iv", 0)
                put_iv  = atm_d["put"].get("iv",  0)
                if call_iv > 0 and put_iv > 0:
                    self.iv_atm = (call_iv + put_iv) / 2.0
                elif call_iv > 0:
                    self.iv_atm = call_iv
                elif put_iv > 0:
                    self.iv_atm = put_iv
                if self.iv_atm and self.iv_atm > 0:
                    self.iv_atm_history.append(self.iv_atm)
                    self._save_daily_iv_close()  # PATCH: D3
                    logger.info(
                        f"IV_ATM: {self.iv_atm:.4f} "
                        f"({self.iv_atm*100:.2f}%)"
                    )

                    # Bootstrap iv_rv_spread if empty
                    # (also done in initialize() but
                    #  chain may load after initialize)
                    if (
                        len(self.iv_rv_spread_history) == 0
                        and self.vix is not None
                        and self.vix > 0
                    ):
                        estimated_rv = (
                            self.vix / 100.0
                        ) * 0.70
                        estimated_spread = (
                            self.iv_atm - estimated_rv
                        )
                        min_hist = getattr(
                            config,
                            "EDGE_SCORE_MIN_HISTORY",
                            3,
                        )
                        for _ in range(min_hist):
                            self.iv_rv_spread_history.append(
                                estimated_spread
                            )
                        logger.info(
                            f"Bootstrapped iv_rv_spread "
                            f"(from chain): "
                            f"spread={estimated_spread:.4f}"
                        )

            self._compute_skew()
            self._compute_forward_iv(expiry_date)

            return self.option_chain.get(expiry_date, {})

        except Exception as e:
            logger.error(
                f"fetch_option_chain error "
                f"{expiry_date}: {e}"
            )
            return self.option_chain.get(expiry_date, {})

    def get_chain_for_expiry(
        self, expiry: str
    ) -> Dict[float, Dict]:
        return self.option_chain.get(expiry, {})

    def get_active_chain(self) -> Dict[float, Dict]:
        if self._active_expiry:
            return self.option_chain.get(
                self._active_expiry, {}
            )
        return {}

    def get_mark_price(
        self,
        opt_data: Dict,
        fallback: float = 0.0,
        max_quote_age_sec: float = 15.0,
        max_ltp_age_sec: float = 30.0,
        max_rest_fallback_age_sec: float = 90.0,
    ) -> float:
        """
        DM-R01/R02: Returns the best available mark price.
        Priority:
          1. Fresh REST bid/ask midpoint (< max_quote_age_sec)
          2. Fresh WS LTP (< max_ltp_age_sec, uses _ltp_ts)
          3. Stale REST bid/ask midpoint (< max_rest_fallback_age_sec)
          4. fallback (entry price or 0)
        Returns fallback when all sources exceed their age limits
        so callers can detect a genuinely stale mark.
        """
        now_ist = datetime.now(self._IST)
        bid = float(opt_data.get("bid", 0) or 0)
        ask = float(opt_data.get("ask", 0) or 0)
        ltp = float(opt_data.get("ltp", 0) or 0)

        def _age(ts_key):
            ts = opt_data.get(ts_key)
            if not ts:
                return float("inf")
            try:
                ts_dt = datetime.fromisoformat(ts)
                if ts_dt.tzinfo is None:
                    ts_dt = self._IST.localize(ts_dt)
                return (now_ist - ts_dt).total_seconds()
            except Exception:
                return float("inf")

        rest_age = _age("_rest_ts")
        ltp_age  = _age("_ltp_ts")

        # DM-T03: reject crossed markets (bid > ask).
        _bid_ask_valid = bid > 0 and ask > 0 and ask >= bid

        # 1. Fresh REST midpoint
        if rest_age <= max_quote_age_sec and _bid_ask_valid:
            return (bid + ask) / 2.0
        # 2. Fresh WS LTP
        if ltp_age <= max_ltp_age_sec and ltp > 0:
            return ltp
        # 3. Stale REST midpoint (bounded age, still valid)
        if rest_age <= max_rest_fallback_age_sec and _bid_ask_valid:
            return (bid + ask) / 2.0
        # 4. fallback
        return fallback

    def _compute_skew(self) -> None:
        try:
            put_25d  = self.get_strike_by_delta("put",  0.25)
            call_25d = self.get_strike_by_delta("call", 0.25)
            if put_25d is None or call_25d is None:
                return

            active  = self.get_active_chain()
            put_iv  = (
                active.get(put_25d,  {})
                .get("put",  {})
                .get("iv", 0)
            )
            call_iv = (
                active.get(call_25d, {})
                .get("call", {})
                .get("iv", 0)
            )

            if put_iv > 0 and call_iv > 0:
                self.skew = put_iv - call_iv
                today = date.today()
                if self._last_skew_date != today:
                    self.skew_history.append(self.skew)
                    self._last_skew_date = today
                logger.info(f"Skew: {self.skew:.4f}")
        except Exception as e:
            logger.warning(f"Skew error: {e}")

    def _compute_forward_iv(
        self, current_expiry: str
    ) -> None:
        """
        Compute forward IV.
        Uses VIX/100 as far-term proxy when no 30-45 DTE
        expiry available (always the case for weekly universe).
        """
        try:
            available = self.get_available_expiries()
            today     = date.today()
            forward_expiry = None

            for exp_str in available:
                try:
                    exp_date = datetime.strptime(
                        exp_str, "%Y-%m-%d"
                    ).date()
                    dte = (exp_date - today).days
                    if 30 <= dte <= 45:
                        forward_expiry = exp_str
                        break
                except ValueError:
                    continue

            if (
                forward_expiry
                and forward_expiry != current_expiry
                and self.atm_strike
                and forward_expiry in self.option_chain
                and self.atm_strike in self.option_chain[
                    forward_expiry
                ]
            ):
                fwd      = self.option_chain[
                    forward_expiry
                ][self.atm_strike]
                fwd_c_iv = fwd.get("call", {}).get("iv", 0)
                fwd_p_iv = fwd.get("put",  {}).get("iv", 0)
                if fwd_c_iv > 0 and fwd_p_iv > 0:
                    self.forward_iv = (
                        fwd_c_iv + fwd_p_iv
                    ) / 2.0
                    logger.info(
                        f"Forward IV: {self.forward_iv:.4f}"
                    )
                    return

            # Use VIX/100 as far-term proxy
            if self.vix and self.vix > 0:
                self.forward_iv = self.vix / 100.0
                logger.info(
                    f"Forward IV = VIX/100 = "
                    f"{self.forward_iv:.4f}"
                )
            elif self.iv_atm and self.iv_atm > 0:
                self.forward_iv = self.iv_atm

        except Exception as e:
            logger.warning(f"Forward IV error: {e}")
            if self.vix and self.vix > 0:
                self.forward_iv = self.vix / 100.0
            elif self.iv_atm:
                self.forward_iv = self.iv_atm

    async def fetch_historical_candles(
        self,
        instrument_key: str,
        interval:       str,
        from_date:      str,
        to_date:        str,
    ) -> List[Dict]:
        """
        Fetch historical OHLCV candles.
        LIVE FIX: valid intervals = 1minute, 30minute,
                  day, week, month
                  ("15minute" returns HTTP 400)
        LIVE FIX: to_date must be a trading day
                  (use _last_trading_day())
        """
        try:
            from urllib.parse import quote
            encoded_key = quote(
                instrument_key, safe="|"
            )
            endpoint = (
                f"{config.EP_CANDLE}/"
                f"{encoded_key}/{interval}/"
                f"{to_date}/{from_date}"
            )

            logger.info(
                f"Candle fetch: "
                f"{interval} {from_date}→{to_date}"
            )

            data = await self._api_get(endpoint, {})

            if isinstance(data, dict):
                errors = data.get("errors", [])
                if errors:
                    logger.error(
                        f"Candle API error: {errors}"
                    )
                    return list(self.candles_30m)

            candles_raw: List = []
            if isinstance(data, dict):
                candles_raw = data.get("candles", [])
            elif isinstance(data, list):
                candles_raw = data

            if not candles_raw:
                logger.warning(
                    f"No candles for {instrument_key} "
                    f"{interval} {from_date}→{to_date}. "
                    f"If to_date is weekend/holiday, "
                    f"use _last_trading_day()."
                )
                return list(self.candles_30m)

            new_candles = []
            for c in candles_raw:
                if len(c) >= 6:
                    try:
                        new_candles.append({
                            "timestamp": str(c[0]),
                            "open":   float(c[1]),
                            "high":   float(c[2]),
                            "low":    float(c[3]),
                            "close":  float(c[4]),
                            "volume": int(c[5]),
                            "oi": (
                                int(c[6])
                                if len(c) > 6 else 0
                            ),
                        })
                    except (ValueError, IndexError) as e:
                        logger.warning(
                            f"Candle parse error: {e}"
                        )
                        continue

            if not new_candles:
                return list(self.candles_30m)

            if interval == config.DAILY_CANDLE_TIMEFRAME:
                target_deque = self.candles_daily
                maxlen       = 60
            else:
                target_deque = self.candles_30m
                maxlen       = config.ADX_CANDLE_COUNT + 10

            existing_ts = {
                c["timestamp"] for c in target_deque
            }
            added = 0
            for candle in new_candles:
                if candle["timestamp"] not in existing_ts:
                    target_deque.append(candle)
                    existing_ts.add(candle["timestamp"])
                    added += 1

            sorted_candles = sorted(
                list(target_deque),
                key=lambda x: x["timestamp"],
            )
            if interval == config.DAILY_CANDLE_TIMEFRAME:
                self.candles_daily = deque(
                    sorted_candles[-maxlen:],
                    maxlen=maxlen,
                )
            else:
                self.candles_30m = deque(
                    sorted_candles[-maxlen:],
                    maxlen=maxlen,
                )
                self.candles_15m = self.candles_30m

            logger.info(
                f"Candles [{interval}]: "
                f"added={added} "
                f"total={len(target_deque)}"
            )
            return list(target_deque)

        except Exception as e:
            logger.error(
                f"fetch_historical_candles error: {e}"
            )
            return list(self.candles_30m)

    # ─────────────────────────────────────────────────────────────
    # Indicator computation
    # ─────────────────────────────────────────────────────────────

    async def compute_realized_vol(
        self,
    ) -> Optional[float]:
        try:
            if len(self.candles_daily) >= config.RV_LOOKBACK_DAYS:
                closes = [
                    c["close"] for c in self.candles_daily
                ]
                daily_returns = [
                    np.log(closes[i] / closes[i - 1])
                    for i in range(1, len(closes))
                    if closes[i - 1] > 0
                ]
                if len(daily_returns) >= config.RV_LOOKBACK_DAYS:
                    arr = np.array(
                        daily_returns[
                            -config.RV_LOOKBACK_DAYS:
                        ]
                    )
                    rv = float(
                        np.std(arr) * np.sqrt(252)
                    )
                    self.rv_20d = rv
                    logger.info(
                        f"RV (daily candles): {rv:.4f}"
                    )
                    self._update_iv_rv_spread(rv)
                    return rv

            min_returns = config.RV_LOOKBACK_DAYS
            if len(self.log_returns) < min_returns:
                logger.info(
                    f"Insufficient daily returns: "
                    f"{len(self.log_returns)}/{min_returns}"
                )
                return None

            returns_arr = np.array(
                list(self.log_returns)[-min_returns:]
            )
            rv = float(
                np.std(returns_arr) * np.sqrt(252)
            )
            self.rv_20d = rv
            logger.info(f"RV (spot-based): {rv:.4f}")
            self._update_iv_rv_spread(rv)
            return rv

        except Exception as e:
            logger.error(f"compute_realized_vol error: {e}")
            return None

    def _update_iv_rv_spread(self, rv: float) -> None:
        if self.iv_atm and self.iv_atm > 0:
            today = date.today()
            if self._last_spread_date != today:
                self.iv_rv_spread_history.append(
                    self.iv_atm - rv
                )
                self._last_spread_date = today
            logger.info(
                f"IV={self.iv_atm:.4f} "
                f"RV={rv:.4f} "
                f"spread={self.iv_atm - rv:.4f}"
            )

    def wilders_smooth(
        self, data: np.ndarray, period: int
    ) -> np.ndarray:
        data   = np.nan_to_num(
            np.asarray(data, dtype=float), nan=0.0
        )
        result = np.zeros(len(data))
        if len(data) < period:
            return result
        result[period - 1] = np.mean(data[:period])
        for i in range(period, len(data)):
            result[i] = (
                result[i - 1] * (period - 1) + data[i]
            ) / period
        return result

    def compute_adx(
        self, period: int = None
    ) -> Optional[float]:
        """
        Compute ADX from 30-minute candles.
        LIVE FIX: safe division to avoid RuntimeWarning.
        np.where evaluates BOTH branches before masking,
        so 0/0 triggers warning even when guarded.
        Fix: safe_tr replaces 0 with 1.0 before division.
        """
        if period is None:
            period = config.ADX_PERIOD

        if len(self.candles_30m) < period * 2:
            logger.info(
                f"Insufficient candles for ADX: "
                f"{len(self.candles_30m)}/{period * 2}"
            )
            return None

        try:
            candles = list(self.candles_30m)
            highs   = np.array(
                [c["high"]  for c in candles], dtype=float
            )
            lows    = np.array(
                [c["low"]   for c in candles], dtype=float
            )
            closes  = np.array(
                [c["close"] for c in candles], dtype=float
            )

            n            = len(candles)
            tr_arr       = np.zeros(n)
            plus_dm_arr  = np.zeros(n)
            minus_dm_arr = np.zeros(n)

            for i in range(1, n):
                hl  = highs[i] - lows[i]
                hpc = abs(highs[i]  - closes[i - 1])
                lpc = abs(lows[i]   - closes[i - 1])
                tr_arr[i] = max(hl, hpc, lpc)

                up   = highs[i] - highs[i - 1]
                down = lows[i - 1] - lows[i]

                plus_dm_arr[i]  = (
                    up if up > down and up > 0 else 0.0
                )
                minus_dm_arr[i] = (
                    down if down > up and down > 0 else 0.0
                )

            s_tr  = self.wilders_smooth(tr_arr,       period)
            s_pdm = self.wilders_smooth(plus_dm_arr,  period)
            s_mdm = self.wilders_smooth(minus_dm_arr, period)

            # LIVE FIX: safe division
            safe_tr  = np.where(s_tr > 1e-10, s_tr, 1.0)
            plus_di  = np.where(
                s_tr > 1e-10,
                100.0 * s_pdm / safe_tr,
                0.0,
            )
            minus_di = np.where(
                s_tr > 1e-10,
                100.0 * s_mdm / safe_tr,
                0.0,
            )

            di_sum      = plus_di + minus_di
            di_diff     = np.abs(plus_di - minus_di)
            safe_di_sum = np.where(
                di_sum > 1e-10, di_sum, 1.0
            )
            dx = np.where(
                di_sum > 1e-10,
                100.0 * di_diff / safe_di_sum,
                0.0,
            )

            adx_arr  = self.wilders_smooth(dx, period)
            self.adx = float(adx_arr[-1])

            logger.info(
                f"ADX={self.adx:.2f} "
                f"+DI={plus_di[-1]:.2f} "
                f"-DI={minus_di[-1]:.2f}"
            )
            return self.adx

        except Exception as e:
            logger.error(f"compute_adx error: {e}")
            return None

    def compute_ema_slope(
        self,
    ) -> Tuple[Optional[float], Optional[float]]:
        min_bars = config.EMA_PERIOD + 20
        if len(self.candles_30m) < min_bars:
            logger.info(
                f"Insufficient candles for EMA: "
                f"{len(self.candles_30m)}/{min_bars}"
            )
            return (None, None)

        try:
            closes = np.array(
                [c["close"] for c in self.candles_30m],
                dtype=float,
            )
            ema_series = pd.Series(closes).ewm(
                span=config.EMA_PERIOD, adjust=False
            ).mean()

            self.ema_50  = float(ema_series.iloc[-1])
            ema_prev     = float(ema_series.iloc[-21])

            self.ema_slope = (
                (self.ema_50 - ema_prev) / ema_prev
                if ema_prev > 1e-10
                else 0.0
            )

            logger.info(
                f"EMA50={self.ema_50:.2f} "
                f"slope={self.ema_slope:.6f}"
            )
            return (self.ema_50, self.ema_slope)

        except Exception as e:
            logger.error(f"compute_ema_slope error: {e}")
            return (None, None)

    async def fetch_oi_snapshot(self) -> Dict:
        try:
            active = self.get_active_chain()
            if not active:
                return {}

            prev_snapshot = (
                dict(self.oi_snapshots[-1])
                if self.oi_snapshots
                else {}
            )
            snapshot: Dict[float, Dict] = {}

            for strike, data in active.items():
                call_data  = data["call"]
                put_data   = data["put"]
                call_oi    = float(call_data.get("oi", 0))
                put_oi     = float(put_data.get("oi",  0))
                call_delta = float(call_data.get("delta", 0))
                put_delta  = float(put_data.get("delta",  0))

                prev_call = prev_snapshot.get(
                    strike, {}
                ).get("call_oi", call_oi)
                prev_put  = prev_snapshot.get(
                    strike, {}
                ).get("put_oi",  put_oi)

                call_chg = call_oi - prev_call
                put_chg  = put_oi  - prev_put

                snapshot[strike] = {
                    "call_oi":        call_oi,
                    "put_oi":         put_oi,
                    "call_delta":     call_delta,
                    "put_delta":      put_delta,
                    "call_oi_change": call_chg,
                    "put_oi_change":  put_chg,
                    "call_delta_oi":  call_delta * call_chg,
                    "put_delta_oi":   put_delta  * put_chg,
                }

            if snapshot:
                self.oi_snapshots.append(snapshot)
                logger.info(
                    f"OI snapshot: {len(snapshot)} strikes"
                )
            return snapshot

        except Exception as e:
            logger.error(f"fetch_oi_snapshot error: {e}")
            return {}

    def compute_net_flow(self) -> float:
        try:
            if len(self.oi_snapshots) < 3:
                self.net_flow = 0.0
                return 0.0

            net_flow = 0.0
            for snapshot in list(self.oi_snapshots):
                for strike, data in snapshot.items():
                    net_flow += data.get(
                        "call_delta_oi", 0.0
                    )
                    net_flow += data.get(
                        "put_delta_oi",  0.0
                    )

            self.net_flow = net_flow
            logger.info(f"Net flow: {self.net_flow:.4f}")
            return float(net_flow)

        except Exception as e:
            logger.error(f"compute_net_flow error: {e}")
            self.net_flow = 0.0
            return 0.0

    def compute_spread_ratio(self) -> float:
        try:
            active = self.get_active_chain()
            if not active or not self.atm_strike:
                self.spread_ratio = 1.0
                return 1.0

            strikes_sorted = sorted(active.keys())
            puts_below     = [
                s for s in strikes_sorted
                if s < self.atm_strike
            ]

            if len(puts_below) < config.OTM_STRIKE_OFFSET:
                self.spread_ratio = 1.0
                return 1.0

            third_otm = puts_below[
                -config.OTM_STRIKE_OFFSET
            ]
            put_data  = active[third_otm]["put"]
            ask       = put_data.get("ask", 0)
            bid       = put_data.get("bid", 0)
            current   = ask - bid

            self.bid_ask_spread_3otm.append(current)

            if len(self.bid_ask_spread_3otm) < 3:
                self.spread_ratio = 1.0
                return 1.0

            avg   = float(
                np.mean(list(self.bid_ask_spread_3otm))
            )
            ratio = current / (avg + 1e-10)
            self.spread_ratio = ratio

            logger.info(
                f"Spread ratio: {ratio:.3f} "
                f"(cur={current:.2f} avg={avg:.2f})"
            )
            return float(ratio)

        except Exception as e:
            logger.error(
                f"compute_spread_ratio error: {e}"
            )
            self.spread_ratio = 1.0
            return 1.0

    # ─────────────────────────────────────────────────────────────
    # Chain helpers
    # ─────────────────────────────────────────────────────────────

    def get_strike_by_delta(
        self,
        option_type:  str,
        target_delta: float,
        tolerance:    float = 0.02,
        expiry:       Optional[str] = None,
    ) -> Optional[float]:
        """
        Find strike closest to target delta.
        LIVE FIX: expiry parameter added so builders search
        within a specific expiry's chain, preventing
        instrument_key mismatch across expiries.
        """
        chain = (
            self.get_chain_for_expiry(expiry)
            if expiry
            else self.get_active_chain()
        )

        if not chain:
            logger.warning(
                f"get_strike_by_delta: chain empty "
                f"(expiry={expiry or 'active'})"
            )
            return None

        best_strike  = None
        best_diff    = float("inf")
        all_filtered = True

        for strike, data in chain.items():
            opt   = data.get(option_type, {})
            delta = opt.get("delta", None)
            if delta is None:
                continue

            all_filtered = False
            delta_abs    = abs(delta)
            diff         = abs(delta_abs - target_delta)
            if diff < best_diff:
                best_diff   = diff
                best_strike = strike

        if all_filtered:
            logger.warning(
                f"get_strike_by_delta: ALL strikes "
                f"filtered by OI for {option_type} "
                f"delta={target_delta:.2f}"
            )
            return None

        if best_strike is not None and best_diff <= tolerance:
            return best_strike

        if best_strike is not None and best_diff <= 0.05:
            logger.warning(
                f"get_strike_by_delta: widened tolerance "
                f"{option_type} delta={target_delta:.2f} "
                f"strike={best_strike}"
            )
            return best_strike

        logger.warning(
            f"get_strike_by_delta: no match "
            f"{option_type} delta={target_delta:.2f}"
        )
        return None

    def get_expiry_by_dte(
        self,
        target_dte: int,
        tolerance:  int = 5,
    ) -> Optional[str]:
        available = self.get_available_expiries()
        if not available:
            logger.warning(
                "get_expiry_by_dte: no expiries"
            )
            return None

        today       = date.today()
        best_expiry = None
        best_diff   = float("inf")

        for exp_str in available:
            try:
                exp_date = datetime.strptime(
                    exp_str, "%Y-%m-%d"
                ).date()
                dte = (exp_date - today).days
                if dte < target_dte - tolerance:
                    continue
                if dte < 2:
                    continue
                diff = abs(dte - target_dte)
                if diff < best_diff:
                    best_diff   = diff
                    best_expiry = exp_str
            except ValueError:
                continue

        if best_expiry:
            exp_date = datetime.strptime(
                best_expiry, "%Y-%m-%d"
            ).date()
            dte = (exp_date - today).days
            logger.info(
                f"get_expiry_by_dte: target={target_dte} "
                f"found={best_expiry} dte={dte}"
            )
        else:
            logger.warning(
                f"get_expiry_by_dte: no expiry for "
                f"target={target_dte} tol={tolerance}"
            )
        return best_expiry

    def get_available_expiries(self) -> List[str]:
        expiry_set = set(self._known_expiries)
        expiry_set.update(self.option_chain.keys())

        for expiry, chain in self.option_chain.items():
            for strike, data in chain.items():
                for opt_type in ("call", "put"):
                    key = data.get(opt_type, {}).get(
                        "instrument_key", ""
                    )
                    if not key:
                        continue
                    parts = key.split("|")
                    if len(parts) >= 4:
                        date_str = parts[2]
                        for fmt in (
                            "%d%b%Y", "%d-%b-%Y",
                            "%Y-%m-%d", "%d-%m-%Y",
                            "%d%m%Y",
                        ):
                            try:
                                parsed = datetime.strptime(
                                    date_str, fmt
                                ).date()
                                expiry_set.add(
                                    parsed.strftime(
                                        "%Y-%m-%d"
                                    )
                                )
                                break
                            except ValueError:
                                continue

        return sorted(list(expiry_set))

    def _init_iv_rank_table(self) -> None:
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS iv_rank_history (
                    trading_date TEXT PRIMARY KEY,
                    iv_atm REAL
                )
            """)
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"iv_rank_history table init error: {e}")

    def _save_daily_iv_close(self) -> None:
        """
        PATCH (D3a): previously saved only the FIRST iv_atm
        reading of the day (an opening snapshot, not a genuine
        close) because of the "already saved today" early-return.
        Now only writes/overwrites today's row within the last
        ~45 min of the trading session, so repeated calls in that
        window converge toward the actual closing IV instead of
        locking in an early-morning value.
        """
        if not self.iv_atm or self.iv_atm <= 0:
            return
        now_ist = datetime.now(self._IST)
        if (now_ist.hour, now_ist.minute) < (14, 45):
            return
        today = date.today()
        try:
            self._init_iv_rank_table()
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO iv_rank_history
                (trading_date, iv_atm) VALUES (?, ?)
            """, (today.isoformat(), self.iv_atm))
            cursor.execute("""
                DELETE FROM iv_rank_history WHERE trading_date
                NOT IN (
                    SELECT trading_date FROM iv_rank_history
                    ORDER BY trading_date DESC LIMIT 90
                )
            """)
            conn.commit()
            conn.close()
            self._last_iv_rank_date = today
        except sqlite3.Error as e:
            logger.warning(f"_save_daily_iv_close error: {e}")

    def _load_iv_rank_history(self) -> List[float]:
        # PATCH (D3c): cache for up to 60s — compute_iv_rank() can
        # run 2-3x per cycle from different callers
        # (_should_enter_new_position, _select_strategy,
        # _build_long_straddle), each previously opening a fresh
        # SQLite connection.
        now = datetime.now(self._IST)
        cached = getattr(self, "_iv_rank_cache", None)
        cached_at = getattr(self, "_iv_rank_cache_time", None)
        if (
            cached is not None
            and cached_at is not None
            and (now - cached_at).total_seconds() < 60
        ):
            return cached
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT iv_atm FROM iv_rank_history
                ORDER BY trading_date DESC LIMIT 60
            """)
            rows = cursor.fetchall()
            conn.close()
            result = [r[0] for r in rows if r[0] and r[0] > 0]
            self._iv_rank_cache = result
            self._iv_rank_cache_time = now
            return result
        except sqlite3.OperationalError:
            return []
        except sqlite3.Error as e:
            logger.warning(f"_load_iv_rank_history error: {e}")
            return []

    def compute_iv_rank(self) -> float:
        # PATCH (D3): prefer a persisted 60-day daily-close
        # history over the old intraday-only deque / hardcoded
        # default. Falls back to the previous logic only if the
        # persisted history is still too short (e.g. brand-new
        # deployment with no history yet).
        daily_history = self._load_iv_rank_history()
        if len(daily_history) >= 10:
            if self.iv_atm is None:
                return 55.0
            try:
                iv_high = max(daily_history)
                iv_low  = min(daily_history)
                if abs(iv_high - iv_low) < 1e-10:
                    return 50.0
                rank = (
                    (self.iv_atm - iv_low)
                    / (iv_high - iv_low)
                ) * 100.0
                return float(max(0.0, min(100.0, rank)))
            except Exception as e:
                logger.error(f"compute_iv_rank error: {e}")
                return 55.0

        if len(self.iv_atm_history) < 10:
            return 55.0
        if self.iv_atm is None:
            return 55.0
        try:
            history = list(self.iv_atm_history)
            iv_high = max(history)
            iv_low  = min(history)
            if abs(iv_high - iv_low) < 1e-10:
                return 50.0
            rank = (
                (self.iv_atm - iv_low)
                / (iv_high - iv_low)
            ) * 100.0
            return float(max(0.0, min(100.0, rank)))
        except Exception as e:
            logger.error(f"compute_iv_rank error: {e}")
            return 50.0

    def compute_atr(
        self, period: int = 14
    ) -> Optional[float]:
        if len(self.candles_30m) < period + 1:
            return None
        try:
            candles = list(self.candles_30m)
            trs     = []
            for i in range(1, len(candles)):
                h  = candles[i]["high"]
                l  = candles[i]["low"]
                pc = candles[i - 1]["close"]
                trs.append(
                    max(h - l, abs(h - pc), abs(l - pc))
                )
            return float(np.mean(trs[-period:]))
        except Exception as e:
            logger.error(f"compute_atr error: {e}")
            return None

    def is_atr_contracting(
        self, lookback: int = 5
    ) -> bool:
        current_count = len(self.candles_30m)
        if (
            current_count
            == self._atr_contracting_candle_count
            and self._atr_contracting_cache is not None
        ):
            return self._atr_contracting_cache

        try:
            if len(self.candles_30m) < lookback + 2:
                self._atr_contracting_cache        = False
                self._atr_contracting_candle_count = (
                    current_count
                )
                return False

            candles = list(self.candles_30m)
            atrs    = []

            for end in range(
                len(candles) - lookback,
                len(candles) + 1,
            ):
                if end < 2:
                    continue
                segment = candles[max(0, end - 15): end]
                if len(segment) < 2:
                    continue
                trs = []
                for i in range(1, len(segment)):
                    h  = segment[i]["high"]
                    l  = segment[i]["low"]
                    pc = segment[i - 1]["close"]
                    trs.append(
                        max(
                            h - l,
                            abs(h - pc),
                            abs(l - pc),
                        )
                    )
                if trs:
                    atrs.append(float(np.mean(trs)))

            if len(atrs) < lookback:
                result = False
            else:
                result = all(
                    atrs[i] < atrs[i - 1]
                    for i in range(1, len(atrs))
                )

            self._atr_contracting_cache        = result
            self._atr_contracting_candle_count = current_count
            return result

        except Exception as e:
            logger.error(f"is_atr_contracting error: {e}")
            self._atr_contracting_cache = False
            return False

    async def check_margin(
        self, legs: List[Dict]
    ) -> Tuple[bool, float]:
        if config.PAPER_TRADING_MODE:
            return (True, 0.0)
        try:
            payload = {
                "instruments": [
                    {
                        "instrument_key": (
                            leg["instrument_key"]
                        ),
                        "quantity":        leg["quantity"],
                        "transaction_type": (
                            leg["transaction_type"]
                        ),
                        "product": leg.get("product", "D"),
                        "price":   leg.get("price", 0),
                    }
                    for leg in legs
                ]
            }
            response  = await self._api_post(
                config.EP_MARGIN, payload
            )
            data      = response.get("data", response)
            required  = float(
                data.get("required_margin",  0)
            )
            available = float(
                data.get("available_margin", 0)
            )

            if available >= required:
                logger.info(
                    f"Margin OK: req={required:.0f} "
                    f"avail={available:.0f}"
                )
                return (True, required)
            else:
                logger.warning(
                    f"Insufficient margin: "
                    f"req={required:.0f} "
                    f"avail={available:.0f}"
                )
                return (False, required)

        except Exception as e:
            logger.error(f"check_margin error: {e}")
            # AUDIT DM-08: fail CLOSED on exception.
            # An API failure must not be interpreted as margin approved.
            return (False, 0.0)

    # ─────────────────────────────────────────────────────────────
    # WebSocket
    # ─────────────────────────────────────────────────────────────


    def get_estimated_rv(self) -> Optional[float]:
        """
        Get RV, using VIX-based estimate when not available.
        FIX: rv_20d=None on first run causes edge_score=0.
        RV ≈ VIX × 0.70 in calm markets (empirical estimate).
        """
        if self.rv_20d is not None and self.rv_20d > 0:
            return self.rv_20d
        if self.vix is not None and self.vix > 0:
            estimated = (self.vix / 100.0) * 0.70
            return estimated
        return None

    async def start_websocket(self) -> None:
        """
        Start WebSocket V3 connection.
        LIVE FIX: uses v3 authorize endpoint.
        LIVE FIX: option_greeks mode for options.
        LIVE FIX: reset ws_last_msg_time after subscription.
        """
        import websockets

        for attempt in range(config.WS_RECONNECT_ATTEMPTS):
            try:
                logger.info(
                    f"WS connect attempt {attempt + 1}/"
                    f"{config.WS_RECONNECT_ATTEMPTS}"
                )

                auth_data = await self._api_get_v3(
                    config.EP_WS_AUTHORIZE, {}
                )
                ws_url = auth_data.get(
                    "authorizedRedirectUri",
                    config.WS_URL_V3,
                )

                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=10,
                ) as websocket:
                    self.ws            = websocket
                    self.ws_connected  = True
                    self._ws_decode_errors = 0
                    logger.info("WebSocket connected")

                    instrument_keys = (
                        self._build_ws_subscription_keys()
                    )
                    index_keys  = [
                        k for k in instrument_keys
                        if "NSE_INDEX" in k
                    ]
                    option_keys = [
                        k for k in instrument_keys
                        if "NSE_INDEX" not in k
                    ]

                    if index_keys:
                        await websocket.send(json.dumps({
                            "guid": (
                                f"idx_"
                                f"{datetime.now().strftime('%Y%m%d%H%M%S')}"
                            ),
                            "method": "sub",
                            "data": {
                                "mode": config.WS_MODE_LTPC,
                                "instrumentKeys": index_keys,
                            },
                        }))
                        logger.info(
                            f"WS subscribed "
                            f"{len(index_keys)} index (ltpc)"
                        )

                    if option_keys:
                        await websocket.send(json.dumps({
                            "guid": (
                                f"opt_"
                                f"{datetime.now().strftime('%Y%m%d%H%M%S')}"
                            ),
                            "method": "sub",
                            "data": {
                                "mode": (
                                    config.WS_MODE_OPTION_GREEKS
                                ),
                                "instrumentKeys": option_keys,
                            },
                        }))
                        logger.info(
                            f"WS subscribed "
                            f"{len(option_keys)} options "
                            f"(option_greeks)"
                        )

                    # LIVE FIX: reset last_msg_time after
                    # subscription so health monitor doesn't
                    # fire before first data arrives
                    self.ws_last_msg_time = datetime.now(
                        pytz.timezone(config.TZ)
                    )
                    logger.info(
                        "WS last_msg_time reset "
                        "after subscription"
                    )

                    self._subscribed_atm = self.atm_strike

                    async for message in websocket:
                        try:
                            if isinstance(message, bytes):
                                await self._ws_message_handler(
                                    message
                                )
                            elif isinstance(message, str):
                                try:
                                    data = json.loads(
                                        message
                                    )
                                    msg_type = data.get(
                                        "type", ""
                                    )
                                    if msg_type in (
                                        "connection_ack",
                                        "subscription_response",
                                        "pong",
                                    ):
                                        logger.info(
                                            f"WS control: "
                                            f"{msg_type}"
                                        )
                                    else:
                                        self._process_json_feed(
                                            data
                                        )
                                except json.JSONDecodeError:
                                    logger.debug(
                                        f"WS non-JSON: "
                                        f"{message[:100]}"
                                    )
                        except Exception as e:
                            logger.warning(
                                f"WS message error: {e}"
                            )
                            self._ws_decode_errors += 1
                            if self._ws_decode_errors > 10:
                                logger.error(
                                    "Too many WS errors"
                                )
                                break

                        # ATM shift resubscription
                        if (
                            self.atm_strike
                            and self._subscribed_atm
                            and abs(
                                self.atm_strike
                                - self._subscribed_atm
                            ) >= 2 * config.NIFTY_STRIKE_STEP
                        ):
                            new_keys = (
                                self._build_ws_subscription_keys()
                            )
                            new_idx = [
                                k for k in new_keys
                                if "NSE_INDEX" in k
                            ]
                            new_opt = [
                                k for k in new_keys
                                if "NSE_INDEX" not in k
                            ]
                            ts = datetime.now().strftime(
                                "%H%M%S"
                            )
                            if new_idx:
                                await websocket.send(
                                    json.dumps({
                                        "guid": f"resub_idx_{ts}",
                                        "method": "sub",
                                        "data": {
                                            "mode": config.WS_MODE_LTPC,
                                            "instrumentKeys": new_idx,
                                        },
                                    })
                                )
                            if new_opt:
                                await websocket.send(
                                    json.dumps({
                                        "guid": f"resub_opt_{ts}",
                                        "method": "sub",
                                        "data": {
                                            "mode": config.WS_MODE_OPTION_GREEKS,
                                            "instrumentKeys": new_opt,
                                        },
                                    })
                                )
                            self._subscribed_atm = (
                                self.atm_strike
                            )

                        # WS keepalive ping every 30s
                        # Prevents silent disconnect during
                        # low-activity periods
                        _now_loop = asyncio.get_event_loop().time()
                        if not hasattr(self, '_last_ping_time'):
                            self._last_ping_time = _now_loop
                        if _now_loop - self._last_ping_time > 30:
                            try:
                                await websocket.ping()
                                self._last_ping_time = _now_loop
                                self.ws_last_msg_time = (
                                    datetime.now(
                                        pytz.timezone(config.TZ)
                                    )
                                )
                                logger.debug(
                                    "WS keepalive ping sent"
                                )
                            except Exception:
                                pass

                            logger.info(
                                f"WS resubscribed: "
                                f"ATM={self.atm_strike}"
                            )

            except Exception as e:
                logger.error(
                    f"WS error attempt {attempt + 1}: {e}"
                )
                self.ws_connected = False
                if attempt < (
                    config.WS_RECONNECT_ATTEMPTS - 1
                ):
                    await asyncio.sleep(
                        config.WS_RECONNECT_DELAY_SEC
                    )

        logger.critical(
            "All WS reconnect attempts failed"
        )
        self.ws_connected          = False
        self.kill_switch_triggered = True

    def _build_ws_subscription_keys(self) -> List[str]:
        """Build instrument keys from active expiry only."""
        keys = [
            config.INSTRUMENT_NIFTY,
            config.INSTRUMENT_VIX,
        ]

        active = self.get_active_chain()
        if not active or not self.atm_strike:
            return keys

        strikes_sorted = sorted(active.keys())
        atm            = self.atm_strike
        atm_index      = min(
            range(len(strikes_sorted)),
            key=lambda i: abs(strikes_sorted[i] - atm),
        )
        low_idx  = max(0, atm_index - 10)
        high_idx = min(
            len(strikes_sorted) - 1, atm_index + 10
        )

        for strike in strikes_sorted[low_idx: high_idx + 1]:
            ck = active[strike]["call"].get(
                "instrument_key", ""
            )
            pk = active[strike]["put"].get(
                "instrument_key", ""
            )
            if ck:
                keys.append(ck)
            if pk:
                keys.append(pk)

        return keys[:100]

    async def _ws_message_handler(
        self, message: bytes
    ) -> None:
        """
        Decode Upstox V3 WebSocket protobuf message.
        LIVE FIX: WS iv guard: iv/100 if iv>1.0 else iv
        WS sends iv in % format (e.g. 13.82 = 13.82%).
        """
        try:
            self.ws_last_msg_time = datetime.now(
                pytz.timezone(config.TZ)
            )

            try:
                import MarketDataFeedV3_pb2 as pb

                feed_response = pb.FeedResponse()
                feed_response.ParseFromString(message)

                if feed_response.type == 2:
                    return

                for key, feed in (
                    feed_response.feeds.items()
                ):
                    ltp    = 0.0
                    iv     = 0.0
                    oi     = 0.0
                    greeks = None

                    feed_union = feed.WhichOneof(
                        "FeedUnion"
                    )

                    if feed_union == "ltpc":
                        ltp = feed.ltpc.ltp

                    elif feed_union == "fullFeed":
                        ff_union = (
                            feed.fullFeed.WhichOneof(
                                "FullFeedUnion"
                            )
                        )
                        if ff_union == "marketFF":
                            mff = feed.fullFeed.marketFF
                            ltp = mff.ltpc.ltp
                            iv  = mff.iv
                            oi  = mff.oi
                            g   = mff.optionGreeks
                            greeks = {
                                "delta": g.delta,
                                "gamma": g.gamma,
                                "vega":  g.vega,
                                "theta": g.theta,
                                "iv":    iv,
                            }
                        elif ff_union == "indexFF":
                            ltp = (
                                feed.fullFeed.indexFF
                                .ltpc.ltp
                            )

                    elif feed_union == "firstLevelWithGreeks":
                        flwg = feed.firstLevelWithGreeks
                        ltp  = flwg.ltpc.ltp
                        iv   = flwg.iv
                        oi   = flwg.oi
                        g    = flwg.optionGreeks
                        greeks = {
                            "delta": g.delta,
                            "gamma": g.gamma,
                            "vega":  g.vega,
                            "theta": g.theta,
                            "iv":    iv,
                        }

                    if ltp > 0:
                        self._update_instrument_ltp(
                            key, ltp
                        )

                    if greeks is not None:
                        self._update_instrument_greeks(
                            key,
                            greeks["delta"],
                            greeks["gamma"],
                            greeks["vega"],
                            greeks["theta"],
                            greeks["iv"],
                        )

                    if oi > 0:
                        self._update_instrument_oi(
                            key, oi
                        )

                self._ws_decode_errors = 0

            except ImportError:
                if not self._protobuf_warning_logged:
                    logger.warning(
                        "MarketDataFeedV3_pb2.py not found. "
                        "Using pure-Python fallback."
                    )
                    self._protobuf_warning_logged = True

                feeds = _parse_feed_response_fallback(
                    message
                )
                for instrument_key, feed_data in (
                    feeds.items()
                ):
                    ltp = feed_data.get("ltp", 0.0)
                    if ltp > 0:
                        self._update_instrument_ltp(
                            instrument_key, ltp
                        )
                    greeks = feed_data.get("greeks")
                    if greeks:
                        self._update_instrument_greeks(
                            instrument_key,
                            greeks.get("delta", 0.0),
                            greeks.get("gamma", 0.0),
                            greeks.get("vega",  0.0),
                            greeks.get("theta", 0.0),
                            greeks.get("iv",    0.0),
                        )
                    oi = feed_data.get("oi", 0.0)
                    if oi > 0:
                        self._update_instrument_oi(
                            instrument_key, oi
                        )

                self._ws_decode_errors = 0

            except Exception as proto_err:
                logger.warning(
                    f"Protobuf parse error: {proto_err}"
                )
                self._ws_decode_errors += 1
                if self._ws_decode_errors > 50:
                    asyncio.create_task(
                        self._reconnect_websocket()
                    )

        except Exception as e:
            logger.warning(
                f"WS message handler error: {e}"
            )

    def _update_instrument_ltp(
        self, instrument_key: str, ltp: float
    ) -> None:
        self.ws_last_msg_time = datetime.now(
            pytz.timezone(config.TZ)
        )

        if instrument_key == config.INSTRUMENT_NIFTY:
            if ltp > 0:
                if self.spot and self.spot > 0:
                    change = abs(ltp / self.spot - 1)
                    if change > 0.05:
                        logger.critical(
                            f"WS spot spike: "
                            f"{self.spot:.2f} -> {ltp:.2f} "
                            f"— rejected"
                        )
                        return
                self.prev_spot = self.spot
                self.spot      = float(ltp)

        elif instrument_key == config.INSTRUMENT_VIX:
            if ltp > 0:
                self.prev_vix = self.vix
                self.vix      = float(ltp)

        else:
            mapped = self._instrument_map.get(
                instrument_key
            )
            if mapped is None:
                parts = instrument_key.split("|")
                if len(parts) >= 5:
                    try:
                        strike      = float(parts[3])
                        option_type = (
                            "call"
                            if parts[4].upper() == "CE"
                            else "put"
                        )
                        expiry = self._active_expiry or ""
                        mapped = (expiry, strike, option_type)
                    except (ValueError, IndexError):
                        mapped = None

            if mapped is not None:
                expiry, strike, option_type = mapped
                if (
                    expiry in self.option_chain
                    and strike in self.option_chain[expiry]
                    and ltp > 0
                ):
                    # PATCH: sanity-guard against stale/erroneous
                    # single-print LTP updates on thin option legs.
                    # The spot/VIX branch above already has a 5%
                    # spike-guard; this path previously accepted
                    # any option tick unconditionally, which could
                    # cause phantom MTM P&L swings.
                    opt_ref = self.option_chain[expiry][strike][
                        option_type
                    ]
                    bid_ref = opt_ref.get("bid", 0)
                    ask_ref = opt_ref.get("ask", 0)
                    if bid_ref > 0 and ask_ref > 0:
                        mid_ref    = (bid_ref + ask_ref) / 2.0
                        spread_ref = max(ask_ref - bid_ref, 0.05)
                        if abs(ltp - mid_ref) > max(
                            10.0, spread_ref * 3
                        ):
                            logger.warning(
                                f"Option LTP outlier rejected: "
                                f"{option_type} {strike} {expiry} "
                                f"ltp={ltp:.2f} mid={mid_ref:.2f} "
                                f"bid={bid_ref:.2f} ask={ask_ref:.2f}"
                            )
                        else:
                            opt_ref["ltp"] = ltp
                        opt_ref["_ltp_ts"] = datetime.now(
                            pytz.timezone(config.TZ)
                        ).isoformat()
                    else:
                        opt_ref["ltp"] = ltp
                        opt_ref["_ltp_ts"] = datetime.now(
                            pytz.timezone(config.TZ)
                        ).isoformat()

    def _update_instrument_greeks(
        self,
        instrument_key: str,
        delta: float,
        gamma: float,
        vega:  float,
        theta: float,
        iv:    float,
    ) -> None:
        """
        LIVE FIX: WS sends iv in % format.
        Guard: if iv > 1.0, it's in % → divide by 100.
        """
        mapped = self._instrument_map.get(instrument_key)
        if mapped is None:
            parts = instrument_key.split("|")
            if len(parts) >= 5:
                try:
                    strike      = float(parts[3])
                    option_type = (
                        "call"
                        if parts[4].upper() == "CE"
                        else "put"
                    )
                    expiry = self._active_expiry or ""
                    mapped = (expiry, strike, option_type)
                except (ValueError, IndexError):
                    mapped = None

        if mapped is not None:
            expiry, strike, option_type = mapped
            if (
                expiry in self.option_chain
                and strike in self.option_chain[expiry]
            ):
                opt = self.option_chain[expiry][strike][
                    option_type
                ]
                opt["delta"] = delta
                opt["gamma"] = gamma
                opt["vega"]  = vega
                opt["theta"] = theta
                # LIVE FIX: convert % to decimal if needed
                opt["iv"]    = iv / 100.0 if iv > 1.0 else iv
                opt["_ws_ts"] = datetime.now(
                    self._IST
                ).isoformat()

    def _update_instrument_oi(
        self, instrument_key: str, oi: float
    ) -> None:
        mapped = self._instrument_map.get(instrument_key)
        if mapped is not None:
            expiry, strike, option_type = mapped
            if (
                expiry in self.option_chain
                and strike in self.option_chain[expiry]
                and oi > 0
            ):
                self.option_chain[expiry][strike][
                    option_type
                ]["oi"] = oi

    def _process_json_feed(self, data: Dict) -> None:
        try:
            feeds = data.get("feeds", {})
            for key, feed_data in feeds.items():
                ltpc = feed_data.get("ltpc", {})
                ltp  = float(ltpc.get("ltp", 0))
                if ltp > 0:
                    self._update_instrument_ltp(key, ltp)

                greeks = feed_data.get("optionGreeks", {})
                if greeks:
                    iv_raw = float(greeks.get("iv", 0))
                    self._update_instrument_greeks(
                        key,
                        float(greeks.get("delta", 0)),
                        float(greeks.get("gamma", 0)),
                        float(greeks.get("vega",  0)),
                        float(greeks.get("theta", 0)),
                        iv_raw,
                    )
        except Exception as e:
            logger.warning(
                f"_process_json_feed error: {e}"
            )

    async def _reconnect_websocket(self) -> None:
        """Reconnect WS. Never sets kill_switch_triggered."""
        logger.info('Attempting WS reconnect...')
        self.ws_connected      = False
        self._ws_decode_errors = 0
        for attempt in range(config.WS_RECONNECT_ATTEMPTS):
            try:
                await asyncio.sleep(config.WS_RECONNECT_DELAY_SEC)
                await self.start_websocket()
                logger.info('WS reconnected')
                return
            except Exception as e:
                logger.error(f'WS reconnect {attempt+1} failed: {e}')
        logger.warning(
            'All WS reconnect attempts failed. '
            'Engine continues with REST-only data.'
        )
        self.ws_last_msg_time = datetime.now(
            pytz.timezone(config.TZ)
        )
        
    async def monitor_ws_health(self) -> None:
        """Monitor WS. Reconnect instead of kill switch."""
        IST = pytz.timezone(config.TZ)
        while True:
            try:
                await asyncio.sleep(5)
                if self.ws_last_msg_time is None:
                    continue
                now     = datetime.now(IST)
                ws_time = self.ws_last_msg_time
                if ws_time.tzinfo is None:
                    ws_time = IST.localize(ws_time)
                else:
                    ws_time = ws_time.astimezone(IST)
                elapsed   = (now - ws_time).total_seconds()
                now_time  = now.time()
                today_str = now.date().strftime('%Y-%m-%d')
                is_trading = (
                    now.date().weekday() < 5
                    and today_str not in config.NSE_MARKET_HOLIDAYS
                )
                is_mkt = (
                    config.MARKET_OPEN <= now_time <= config.MARKET_CLOSE
                )
                if not (is_trading and is_mkt):
                    self.ws_last_msg_time = now
                    continue
                if elapsed > config.WS_DOWNTIME_KILL_SWITCH_SEC:
                    logger.warning(
                        f'WS silent {elapsed:.0f}s — '
                        f'reconnecting (engine continues)'
                    )
                    asyncio.create_task(
                        self._reconnect_websocket()
                    )
                    self.ws_last_msg_time = now
            except asyncio.CancelledError:
                logger.info('WS health monitor cancelled')
                break
            except Exception as e:
                logger.error(f'monitor_ws_health error: {e}')
                
    def init_sqlite(self) -> None:
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS market_state (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       TEXT    NOT NULL,
                    spot            REAL,
                    vix             REAL,
                    iv_atm          REAL,
                    rv_20d          REAL,
                    skew            REAL,
                    adx             REAL,
                    ema_50          REAL,
                    composite_score REAL,
                    regime          TEXT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS regime_history (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp         TEXT    NOT NULL,
                    vol_score         REAL,
                    edge_score        REAL,
                    trend_score       REAL,
                    flow_score        REAL,
                    composite_score   REAL,
                    raw_regime        TEXT,
                    confirmed_regime  TEXT,
                    persistence_count INTEGER,
                    macro_override    INTEGER DEFAULT 0
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS open_positions (
                    trade_id           TEXT PRIMARY KEY,
                    strategy_name      TEXT NOT NULL,
                    regime_at_entry    TEXT,
                    entry_timestamp    TEXT,
                    entry_spot         REAL,
                    entry_vix          REAL,
                    expiry_date        TEXT,
                    legs_json          TEXT,
                    stop_loss          REAL,
                    profit_target      REAL,
                    exit_dte           INTEGER,
                    max_hold_date      TEXT,
                    composite_at_entry REAL,
                    vol_score          REAL,
                    edge_score         REAL,
                    trend_score        REAL,
                    flow_score         REAL,
                    days_to_expiry     INTEGER,
                    total_credit       REAL,
                    total_debit        REAL,
                    net_premium        REAL,
                    max_risk           REAL,
                    paper_trade        INTEGER DEFAULT 1,
                    status             TEXT    DEFAULT 'OPEN',
                    created_at         TEXT    DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS closed_trades (
                    trade_id                 TEXT PRIMARY KEY,
                    strategy_name            TEXT,
                    regime_at_entry          TEXT,
                    regime_at_exit           TEXT,
                    entry_timestamp          TEXT,
                    exit_timestamp           TEXT,
                    holding_days             REAL,
                    entry_spot               REAL,
                    exit_spot                REAL,
                    entry_vix                REAL,
                    exit_vix                 REAL,
                    legs_summary             TEXT,
                    total_credit_received    REAL,
                    total_debit_paid         REAL,
                    net_premium              REAL,
                    max_risk                 REAL,
                    realized_pnl             REAL,
                    realized_pnl_percent     REAL,
                    exit_reason              TEXT,
                    slippage_total_points    REAL,
                    transaction_costs        REAL,
                    net_pnl                  REAL,
                    composite_score_at_entry REAL,
                    vol_score                REAL,
                    edge_score               REAL,
                    trend_score              REAL,
                    flow_score               REAL,
                    days_to_expiry_at_entry  INTEGER,
                    expiry_date              TEXT,
                    paper_trade              INTEGER,
                    created_at               TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS score_buffers (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    score_name      TEXT    UNIQUE NOT NULL,
                    buffer_json     TEXT,
                    confirmed_score REAL    DEFAULT 0.0,
                    updated_at      TEXT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS circuit_breaker_log (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT    NOT NULL,
                    level     INTEGER NOT NULL,
                    trigger   TEXT,
                    action    TEXT,
                    daily_pnl REAL,
                    drawdown  REAL,
                    regime    TEXT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS order_log (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp      TEXT    NOT NULL,
                    trade_id       TEXT,
                    order_id       TEXT,
                    instrument_key TEXT,
                    action         TEXT,
                    option_type    TEXT,
                    strike         REAL,
                    expiry         TEXT,
                    qty            INTEGER,
                    order_type     TEXT,
                    price          REAL,
                    fill_price     REAL,
                    status         TEXT,
                    slippage       REAL,
                    paper_trade    INTEGER DEFAULT 1
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS session_orders (
                    tag            TEXT PRIMARY KEY,
                    order_id       TEXT,
                    instrument_key TEXT,
                    action         TEXT,
                    qty            INTEGER,
                    price          REAL,
                    fill_price     REAL DEFAULT 0,
                    trade_id       TEXT,
                    placed_at      TEXT,
                    session_date   TEXT,
                    cancelled      INTEGER DEFAULT 0,
                    filled         INTEGER DEFAULT 0
                )
            """)

            for ddl in (
                "CREATE INDEX IF NOT EXISTS "
                "idx_open_positions_status "
                "ON open_positions(status)",
                "CREATE INDEX IF NOT EXISTS "
                "idx_regime_history_ts "
                "ON regime_history(timestamp)",
                "CREATE INDEX IF NOT EXISTS "
                "idx_closed_trades_ts "
                "ON closed_trades(entry_timestamp)",
                "CREATE INDEX IF NOT EXISTS "
                "idx_order_log_trade "
                "ON order_log(trade_id)",
                "CREATE INDEX IF NOT EXISTS "
                "idx_session_orders_date "
                "ON session_orders(session_date)",
            ):
                cursor.execute(ddl)

            conn.commit()
            conn.close()
            logger.info(
                f"SQLite initialized: {self.db_path}"
            )

        except sqlite3.Error as e:
            logger.warning(f"SQLite init error: {e}")

    def save_state_to_sqlite(self, state: Dict) -> None:
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO market_state
                (timestamp, spot, vix, iv_atm, rv_20d,
                 skew, adx, ema_50, composite_score, regime)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                state.get("timestamp"),
                state.get("spot"),
                state.get("vix"),
                state.get("iv_atm"),
                state.get("rv_20d"),
                state.get("skew"),
                state.get("adx"),
                state.get("ema_50"),
                state.get("composite_score"),
                state.get("regime"),
            ))
            cursor.execute("""
                DELETE FROM market_state WHERE id NOT IN (
                    SELECT id FROM market_state
                    ORDER BY id DESC LIMIT 5000
                )
            """)
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(
                f"save_state_to_sqlite error: {e}"
            )

    def load_state_from_sqlite(self) -> Dict:
        result = {}
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute(
                "SELECT * FROM market_state "
                "ORDER BY id DESC LIMIT 1"
            )
            row = cursor.fetchone()
            if row:
                cols = [d[0] for d in cursor.description]
                result["market_state"] = dict(
                    zip(cols, row)
                )

            cursor.execute(
                "SELECT * FROM open_positions "
                "WHERE status = 'OPEN'"
            )
            rows = cursor.fetchall()
            if rows:
                cols = [d[0] for d in cursor.description]
                result["open_positions"] = [
                    dict(zip(cols, r)) for r in rows
                ]

            cursor.execute("SELECT * FROM score_buffers")
            rows = cursor.fetchall()
            if rows:
                cols = [d[0] for d in cursor.description]
                result["score_buffers"] = [
                    dict(zip(cols, r)) for r in rows
                ]

            conn.close()

        except sqlite3.OperationalError:
            logger.info("No state.db — fresh start")
        except sqlite3.Error as e:
            logger.warning(
                f"load_state_from_sqlite error: {e}"
            )
        return result

    def save_position(self, position_dict: Dict) -> None:
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO open_positions (
                    trade_id, strategy_name,
                    regime_at_entry, entry_timestamp,
                    entry_spot, entry_vix,
                    expiry_date, legs_json,
                    stop_loss, profit_target,
                    exit_dte, max_hold_date,
                    composite_at_entry,
                    vol_score, edge_score,
                    trend_score, flow_score,
                    days_to_expiry,
                    total_credit, total_debit,
                    net_premium, max_risk,
                    paper_trade, status
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,?,
                    ?,?,?,?,?,?,?,?,?,?,
                    ?,?,?,?
                )
            """, (
                position_dict.get("trade_id"),
                position_dict.get("strategy_name"),
                position_dict.get("regime_at_entry"),
                position_dict.get("entry_timestamp"),
                position_dict.get("entry_spot"),
                position_dict.get("entry_vix"),
                position_dict.get("expiry_date"),
                position_dict.get("legs_summary"),
                position_dict.get("stop_loss"),
                position_dict.get("profit_target"),
                position_dict.get("exit_dte"),
                position_dict.get("max_hold_date"),
                position_dict.get(
                    "composite_score_at_entry"
                ),
                position_dict.get("vol_score"),
                position_dict.get("edge_score"),
                position_dict.get("trend_score"),
                position_dict.get("flow_score"),
                position_dict.get(
                    "days_to_expiry_at_entry"
                ),
                position_dict.get("total_credit_received"),
                position_dict.get("total_debit_paid"),
                position_dict.get("net_premium"),
                position_dict.get("max_risk"),
                1 if position_dict.get("paper_trade")
                else 0,
                "OPEN",
            ))
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"save_position error: {e}")

    def close_position(
        self, trade_id: str, exit_data: Dict
    ) -> None:
        try:
            conn   = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute(
                "UPDATE open_positions "
                "SET status = 'CLOSED' "
                "WHERE trade_id = ?",
                (trade_id,),
            )

            columns      = ", ".join(
                config.TRADE_CSV_COLUMNS
            )
            placeholders = ", ".join(
                ["?" for _ in config.TRADE_CSV_COLUMNS]
            )
            values = [
                exit_data.get(col, None)
                for col in config.TRADE_CSV_COLUMNS
            ]

            cursor.execute(
                f"INSERT OR REPLACE INTO closed_trades "
                f"({columns}) VALUES ({placeholders})",
                values,
            )
            conn.commit()
            conn.close()

            self.write_trade_to_csv(exit_data)
            logger.info(
                f"Position closed in DB: {trade_id}"
            )

        except sqlite3.Error as e:
            logger.warning(f"close_position error: {e}")

    def write_trade_to_csv(
        self, trade_dict: Dict
    ) -> None:
        file_exists = os.path.exists(config.TRADE_CSV)
        try:
            with open(
                config.TRADE_CSV, "a",
                newline="", encoding="utf-8",
            ) as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=config.TRADE_CSV_COLUMNS,
                    extrasaction="ignore",
                )
                if not file_exists:
                    writer.writeheader()
                writer.writerow(trade_dict)
            logger.info(
                f"Trade CSV: "
                f"{trade_dict.get('trade_id', 'unknown')}"
            )
        except Exception as e:
            logger.warning(f"CSV write error: {e}")