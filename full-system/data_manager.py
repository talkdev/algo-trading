# ============ FILE: data_manager.py ============
"""
All market data — fetch, validate, store, serve.
Handles Upstox REST API calls, WebSocket V3 feed,
rolling window maintenance, option chain parsing,
ATM strike identification, and SQLite persistence.
"""

import asyncio
import aiohttp
import sqlite3
import json
import logging
import csv
import os
import numpy as np
import pandas as pd
from collections import deque
from datetime import datetime, date, timedelta
from typing import Optional, Tuple, List, Dict, Any
import pytz
import config

logger = logging.getLogger(__name__)


class AuthenticationError(Exception):
    """Raised when Upstox token is expired or invalid."""
    pass


class ServiceUnavailableError(Exception):
    """Raised when Upstox API is in maintenance."""
    pass


class MaxRetriesError(Exception):
    """Raised when all retry attempts are exhausted."""
    pass


class TokenBucket:
    """Token bucket rate limiter for API calls."""

    def __init__(self, capacity: int, refill_rate: float) -> None:
        """Initialize token bucket with capacity and refill rate."""
        self.capacity = capacity
        self.tokens = float(capacity)
        self.refill_rate = refill_rate
        self._lock = asyncio.Lock()
        self._last_refill_time: float = 0.0

    async def _get_loop_time(self) -> float:
        """Get current event loop time safely."""
        try:
            return asyncio.get_event_loop().time()
        except RuntimeError:
            return 0.0

    async def acquire(self) -> bool:
        """Acquire a token, waiting if necessary."""
        async with self._lock:
            if self._last_refill_time == 0.0:
                self._last_refill_time = await self._get_loop_time()

            now = await self._get_loop_time()
            elapsed = now - self._last_refill_time
            self.tokens = min(
                self.capacity,
                self.tokens + elapsed * self.refill_rate
            )
            self._last_refill_time = now

            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True

            wait_time = (1.0 - self.tokens) / self.refill_rate
            await asyncio.sleep(wait_time)
            self.tokens = 0.0
            return True


class DataManager:
    """
    Manages all market data operations including REST API calls,
    WebSocket feed, rolling windows, option chain parsing,
    and SQLite persistence.
    """

    def __init__(self, access_token: str, db_path: str) -> None:
        """Initialize DataManager with access token and database path."""
        self.token = access_token
        self.db_path = db_path
        self.rate_limiter = TokenBucket(
            config.RATE_LIMIT_CAPACITY,
            config.RATE_LIMIT_REFILL_PER_SEC
        )
        self.session: Optional[aiohttp.ClientSession] = None
        self.ws = None
        self.ws_connected = False
        self.ws_last_msg_time: Optional[datetime] = None
        self.kill_switch_triggered = False
        self._ws_decode_errors = 0
        self._subscribed_atm: Optional[float] = None

        # Rolling windows
        self.log_returns: deque = deque(maxlen=252 * 78)
        self.vix_history_20d: deque = deque(maxlen=20)
        self.iv_atm_history: deque = deque(maxlen=config.EDGE_LOOKBACK_DAYS)
        self.iv_rv_spread_history: deque = deque(maxlen=config.EDGE_LOOKBACK_DAYS)
        self.skew_history: deque = deque(maxlen=config.SKEW_LOOKBACK_DAYS)
        self.candles_15m: deque = deque(maxlen=config.ADX_CANDLE_COUNT + 10)
        self.oi_snapshots: deque = deque(maxlen=config.FLOW_WINDOW_MINUTES * 4)
        self.bid_ask_spread_3otm: deque = deque(maxlen=config.SPREAD_LOOKBACK_PERIODS)

        # Live state
        self.spot: Optional[float] = None
        self.prev_spot: Optional[float] = None
        self.vix: Optional[float] = None
        self.prev_vix: Optional[float] = None
        self.option_chain: Dict[float, Dict] = {}
        self.atm_strike: Optional[float] = None
        self.forward_iv: Optional[float] = None
        self.iv_atm: Optional[float] = None
        self.rv_20d: Optional[float] = None
        self.skew: Optional[float] = None
        self.adx: Optional[float] = None
        self.ema_50: Optional[float] = None
        self.ema_slope: Optional[float] = None
        self.net_flow: Optional[float] = None
        self.spread_ratio: Optional[float] = None

        self._data_lock = asyncio.Lock()
        self._IST = pytz.timezone(config.TZ)

    async def initialize(self) -> None:
        """Initialize HTTP session, SQLite, and load historical data."""
        logger.info("DataManager initializing...")
        self.session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            },
            timeout=aiohttp.ClientTimeout(total=30)
        )
        self.init_sqlite()
        state = self.load_state_from_sqlite()
        if state:
            logger.info(f"Loaded existing state from SQLite: regime={state.get('regime', 'N/A')}")
        logger.info("DataManager initialized successfully")

    async def _api_get(self, endpoint: str, params: Dict) -> Dict:
        """Make a rate-limited, retried GET request to Upstox API."""
        await self.rate_limiter.acquire()
        url = config.UPSTOX_BASE_V2 + endpoint
        last_exception = None

        for attempt in range(config.RETRY_MAX_ATTEMPTS):
            try:
                async with self.session.get(url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("data", data)
                    elif resp.status == 429:
                        backoff = min(
                            config.RETRY_BACKOFF_BASE * (2 ** attempt),
                            config.RETRY_MAX_BACKOFF
                        )
                        logger.warning(
                            f"Rate limited on {endpoint}, attempt {attempt + 1}, "
                            f"backoff={backoff:.1f}s"
                        )
                        await asyncio.sleep(backoff)
                        continue
                    elif resp.status == 401:
                        logger.critical(
                            f"Token expired or invalid — endpoint={endpoint}"
                        )
                        raise AuthenticationError("Token expired")
                    elif resp.status == 503:
                        logger.critical(
                            f"API maintenance — endpoint={endpoint}"
                        )
                        raise ServiceUnavailableError("API maintenance")
                    else:
                        body = await resp.text()
                        logger.error(
                            f"API error {resp.status} on {endpoint}: {body[:200]}"
                        )
                        backoff = min(
                            config.RETRY_BACKOFF_BASE * (2 ** attempt),
                            config.RETRY_MAX_BACKOFF
                        )
                        await asyncio.sleep(backoff)
                        continue
            except (AuthenticationError, ServiceUnavailableError):
                raise
            except aiohttp.ClientError as e:
                last_exception = e
                backoff = min(
                    config.RETRY_BACKOFF_BASE * (2 ** attempt),
                    config.RETRY_MAX_BACKOFF
                )
                logger.error(
                    f"Client error on {endpoint} attempt {attempt + 1}: {e}, "
                    f"backoff={backoff:.1f}s"
                )
                await asyncio.sleep(backoff)
            except Exception as e:
                last_exception = e
                backoff = min(
                    config.RETRY_BACKOFF_BASE * (2 ** attempt),
                    config.RETRY_MAX_BACKOFF
                )
                logger.error(
                    f"Unexpected error on {endpoint} attempt {attempt + 1}: {e}"
                )
                await asyncio.sleep(backoff)

        raise MaxRetriesError(
            f"Max retries exhausted for {endpoint}. Last error: {last_exception}"
        )

    async def _api_post(self, endpoint: str, payload: Dict) -> Dict:
        """Make a rate-limited, retried POST request to Upstox API."""
        await self.rate_limiter.acquire()
        url = config.UPSTOX_BASE_V2 + endpoint
        last_exception = None

        for attempt in range(config.RETRY_MAX_ATTEMPTS):
            try:
                async with self.session.post(url, json=payload) as resp:
                    if resp.status in (200, 201):
                        data = await resp.json()
                        return data
                    elif resp.status == 429:
                        backoff = min(
                            config.RETRY_BACKOFF_BASE * (2 ** attempt),
                            config.RETRY_MAX_BACKOFF
                        )
                        logger.warning(
                            f"Rate limited on POST {endpoint}, backoff={backoff:.1f}s"
                        )
                        await asyncio.sleep(backoff)
                        continue
                    elif resp.status == 401:
                        logger.critical("Token expired on POST")
                        raise AuthenticationError("Token expired")
                    elif resp.status == 503:
                        logger.critical("API maintenance on POST")
                        raise ServiceUnavailableError("API maintenance")
                    else:
                        body = await resp.text()
                        logger.error(
                            f"POST error {resp.status} on {endpoint}: {body[:200]}"
                        )
                        backoff = min(
                            config.RETRY_BACKOFF_BASE * (2 ** attempt),
                            config.RETRY_MAX_BACKOFF
                        )
                        await asyncio.sleep(backoff)
                        continue
            except (AuthenticationError, ServiceUnavailableError):
                raise
            except aiohttp.ClientError as e:
                last_exception = e
                backoff = min(
                    config.RETRY_BACKOFF_BASE * (2 ** attempt),
                    config.RETRY_MAX_BACKOFF
                )
                logger.error(f"POST client error on {endpoint}: {e}")
                await asyncio.sleep(backoff)
            except Exception as e:
                last_exception = e
                backoff = min(
                    config.RETRY_BACKOFF_BASE * (2 ** attempt),
                    config.RETRY_MAX_BACKOFF
                )
                logger.error(f"POST unexpected error on {endpoint}: {e}")
                await asyncio.sleep(backoff)

        raise MaxRetriesError(
            f"Max retries exhausted for POST {endpoint}. Last error: {last_exception}"
        )

    async def _api_delete(self, endpoint: str) -> Dict:
        """Make a rate-limited DELETE request to Upstox API."""
        await self.rate_limiter.acquire()
        url = config.UPSTOX_BASE_V2 + endpoint
        last_exception = None

        for attempt in range(config.RETRY_MAX_ATTEMPTS):
            try:
                async with self.session.delete(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("data", data)
                    elif resp.status == 429:
                        backoff = min(
                            config.RETRY_BACKOFF_BASE * (2 ** attempt),
                            config.RETRY_MAX_BACKOFF
                        )
                        await asyncio.sleep(backoff)
                        continue
                    elif resp.status == 401:
                        raise AuthenticationError("Token expired on DELETE")
                    else:
                        body = await resp.text()
                        logger.error(f"DELETE error {resp.status}: {body[:200]}")
                        return {}
            except (AuthenticationError, ServiceUnavailableError):
                raise
            except Exception as e:
                last_exception = e
                backoff = min(
                    config.RETRY_BACKOFF_BASE * (2 ** attempt),
                    config.RETRY_MAX_BACKOFF
                )
                logger.error(f"DELETE error on {endpoint}: {e}")
                await asyncio.sleep(backoff)

        raise MaxRetriesError(
            f"Max retries exhausted for DELETE {endpoint}. Last error: {last_exception}"
        )

    async def fetch_spot_and_vix(self) -> Tuple[Optional[float], Optional[float]]:
        """Fetch current Nifty spot and India VIX from LTP endpoint."""
        try:
            keys = f"{config.INSTRUMENT_NIFTY},{config.INSTRUMENT_VIX}"
            data = await self._api_get(config.EP_LTP, {"instrument_key": keys})

            spot_data = data.get(config.INSTRUMENT_NIFTY, {})
            vix_data = data.get(config.INSTRUMENT_VIX, {})

            new_spot = spot_data.get("last_price")
            new_vix = vix_data.get("last_price")

            if new_spot and new_spot > 0:
                # Validate spike > 5%
                if self.spot and self.spot > 0:
                    change = abs(new_spot / self.spot - 1)
                    if change > 0.05:
                        logger.critical(
                            f"Spot spike detected: {self.spot:.2f} -> {new_spot:.2f} "
                            f"({change * 100:.1f}%) — using previous spot"
                        )
                        new_spot = self.spot

                self.prev_spot = self.spot
                self.spot = float(new_spot)

                if self.prev_spot and self.prev_spot > 0:
                    log_ret = np.log(self.spot / self.prev_spot)
                    self.log_returns.append(log_ret)

            if new_vix and new_vix > 0:
                self.prev_vix = self.vix
                self.vix = float(new_vix)
                self.vix_history_20d.append(self.vix)

            logger.info(
                f"fetch_spot_and_vix: spot={self.spot}, vix={self.vix}"
            )
            return (self.spot, self.vix)

        except Exception as e:
            logger.error(f"fetch_spot_and_vix error: {e}")
            return (self.spot, self.vix)

    async def fetch_option_chain(self, expiry_date: str) -> Dict:
        """Fetch, parse, and filter option chain for given expiry."""
        try:
            params = {
                "instrument_key": config.INSTRUMENT_NIFTY,
                "expiry_date": expiry_date
            }
            data = await self._api_get(config.EP_OPTION_CHAIN, params)

            if not data:
                logger.warning(f"Empty option chain response for expiry={expiry_date}")
                return self.option_chain

            chain_list = data if isinstance(data, list) else data.get("data", [])

            parsed_chain: Dict[float, Dict] = {}

            for item in chain_list:
                strike = float(item.get("strike_price", 0))
                if strike <= 0:
                    continue

                call_opts = item.get("call_options", {})
                put_opts = item.get("put_options", {})

                call_md = call_opts.get("market_data", {})
                put_md = put_opts.get("market_data", {})
                call_greeks = call_opts.get("option_greeks", {})
                put_greeks = put_opts.get("option_greeks", {})

                call_oi = int(call_md.get("oi", 0))
                put_oi = int(put_md.get("oi", 0))
                call_bid = float(call_md.get("bid_price", 0))
                put_bid = float(put_md.get("bid_price", 0))

                # Apply OI filter — widen for ATM
                atm_candidate = self.spot or 0
                is_atm = abs(strike - atm_candidate) <= config.NIFTY_STRIKE_STEP
                min_oi = 10 if is_atm else config.MIN_OI_LOTS

                if call_oi < min_oi and put_oi < min_oi:
                    continue
                if call_bid == 0 and put_bid == 0:
                    continue

                parsed_chain[strike] = {
                    "call": {
                        "instrument_key": call_opts.get("instrument_key", ""),
                        "ltp": float(call_md.get("ltp", 0)),
                        "bid": float(call_md.get("bid_price", 0)),
                        "ask": float(call_md.get("ask_price", 0)),
                        "oi": call_oi,
                        "volume": int(call_md.get("volume", 0)),
                        "iv": float(call_greeks.get("iv", 0)),
                        "delta": float(call_greeks.get("delta", 0)),
                        "gamma": float(call_greeks.get("gamma", 0)),
                        "theta": float(call_greeks.get("theta", 0)),
                        "vega": float(call_greeks.get("vega", 0))
                    },
                    "put": {
                        "instrument_key": put_opts.get("instrument_key", ""),
                        "ltp": float(put_md.get("ltp", 0)),
                        "bid": float(put_md.get("bid_price", 0)),
                        "ask": float(put_md.get("ask_price", 0)),
                        "oi": put_oi,
                        "volume": int(put_md.get("volume", 0)),
                        "iv": float(put_greeks.get("iv", 0)),
                        "delta": float(put_greeks.get("delta", 0)),
                        "gamma": float(put_greeks.get("gamma", 0)),
                        "theta": float(put_greeks.get("theta", 0)),
                        "vega": float(put_greeks.get("vega", 0))
                    }
                }

            if not parsed_chain:
                logger.warning(
                    f"Option chain empty after filtering for expiry={expiry_date}"
                )
                return self.option_chain

            # Merge into main chain
            async with self._data_lock:
                self.option_chain.update(parsed_chain)

            # Identify ATM strike
            if self.spot and self.spot > 0:
                strikes = list(parsed_chain.keys())
                if strikes:
                    raw_atm = min(strikes, key=lambda x: abs(x - self.spot))
                    rounded_atm = (
                        round(raw_atm / config.NIFTY_STRIKE_STEP) * config.NIFTY_STRIKE_STEP
                    )
                    if rounded_atm in self.option_chain:
                        self.atm_strike = rounded_atm
                    else:
                        self.atm_strike = raw_atm
                    logger.info(
                        f"ATM strike identified: {self.atm_strike} "
                        f"(spot={self.spot:.2f})"
                    )

            # Compute IV_ATM
            if self.atm_strike and self.atm_strike in self.option_chain:
                atm_data = self.option_chain[self.atm_strike]
                call_iv = atm_data["call"].get("iv", 0)
                put_iv = atm_data["put"].get("iv", 0)
                if call_iv > 0 and put_iv > 0:
                    self.iv_atm = (call_iv + put_iv) / 2.0
                elif call_iv > 0:
                    self.iv_atm = call_iv
                elif put_iv > 0:
                    self.iv_atm = put_iv
                if self.iv_atm and self.iv_atm > 0:
                    self.iv_atm_history.append(self.iv_atm)
                    logger.info(f"IV_ATM computed: {self.iv_atm:.4f}")

            # Compute Skew (25-delta put IV - 25-delta call IV)
            self._compute_skew()

            # Compute Forward IV
            self._compute_forward_iv(expiry_date)

            return self.option_chain

        except Exception as e:
            logger.error(f"fetch_option_chain error for {expiry_date}: {e}")
            return self.option_chain

    def _compute_skew(self) -> None:
        """Compute 25-delta skew from option chain."""
        try:
            put_25d_strike = self.get_strike_by_delta("put", 0.25)
            call_25d_strike = self.get_strike_by_delta("call", 0.25)

            if put_25d_strike is None or call_25d_strike is None:
                return

            put_iv = self.option_chain.get(put_25d_strike, {}).get("put", {}).get("iv", 0)
            call_iv = self.option_chain.get(call_25d_strike, {}).get("call", {}).get("iv", 0)

            if put_iv > 0 and call_iv > 0:
                self.skew = put_iv - call_iv
                self.skew_history.append(self.skew)
                logger.info(
                    f"Skew computed: {self.skew:.4f} "
                    f"(put_25d_iv={put_iv:.4f}, call_25d_iv={call_iv:.4f})"
                )
        except Exception as e:
            logger.warning(f"Skew computation error: {e}")

    def _compute_forward_iv(self, current_expiry: str) -> None:
        """Compute forward IV from 30-45 DTE expiry."""
        try:
            available_expiries = self.get_available_expiries()
            today = date.today()
            forward_expiry = None

            for exp_str in available_expiries:
                try:
                    exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                    dte = (exp_date - today).days
                    if 30 <= dte <= 45:
                        forward_expiry = exp_str
                        break
                except ValueError:
                    continue

            if forward_expiry and forward_expiry != current_expiry:
                # Find ATM for that expiry
                if self.atm_strike and self.atm_strike in self.option_chain:
                    fwd_data = self.option_chain.get(self.atm_strike, {})
                    fwd_call_iv = fwd_data.get("call", {}).get("iv", 0)
                    fwd_put_iv = fwd_data.get("put", {}).get("iv", 0)
                    if fwd_call_iv > 0 and fwd_put_iv > 0:
                        self.forward_iv = (fwd_call_iv + fwd_put_iv) / 2.0
                        logger.info(
                            f"Forward IV computed: {self.forward_iv:.4f} "
                            f"from expiry={forward_expiry}"
                        )
                        return

            # Fallback to current IV_ATM
            if self.iv_atm and self.iv_atm > 0:
                self.forward_iv = self.iv_atm
                logger.info(f"Forward IV fallback to iv_atm: {self.forward_iv:.4f}")

        except Exception as e:
            logger.warning(f"Forward IV computation error: {e}")
            if self.iv_atm:
                self.forward_iv = self.iv_atm

    async def fetch_historical_candles(
        self,
        instrument_key: str,
        interval: str,
        from_date: str,
        to_date: str
    ) -> List[Dict]:
        """Fetch historical OHLCV candles and update rolling window."""
        try:
            encoded_key = instrument_key.replace("|", "%7C")
            endpoint = f"{config.EP_CANDLE}/{encoded_key}/{interval}/{to_date}/{from_date}"

            data = await self._api_get(endpoint, {})

            candles_raw = []
            if isinstance(data, dict):
                candles_raw = data.get("candles", [])
            elif isinstance(data, list):
                candles_raw = data

            new_candles = []
            for c in candles_raw:
                if len(c) >= 6:
                    try:
                        candle = {
                            "timestamp": str(c[0]),
                            "open": float(c[1]),
                            "high": float(c[2]),
                            "low": float(c[3]),
                            "close": float(c[4]),
                            "volume": int(c[5]),
                            "oi": int(c[6]) if len(c) > 6 else 0
                        }
                        new_candles.append(candle)
                    except (ValueError, IndexError) as e:
                        logger.warning(f"Candle parse error: {e}")
                        continue

            if not new_candles:
                logger.warning(
                    f"No candles returned for {instrument_key} "
                    f"interval={interval} from={from_date} to={to_date}"
                )
                return list(self.candles_15m)

            # Deduplicate by timestamp
            existing_timestamps = {c["timestamp"] for c in self.candles_15m}
            added = 0
            for candle in new_candles:
                if candle["timestamp"] not in existing_timestamps:
                    self.candles_15m.append(candle)
                    existing_timestamps.add(candle["timestamp"])
                    added += 1

            # Sort by timestamp ascending
            sorted_candles = sorted(
                list(self.candles_15m),
                key=lambda x: x["timestamp"]
            )
            self.candles_15m = deque(
                sorted_candles,
                maxlen=config.ADX_CANDLE_COUNT + 10
            )

            logger.info(
                f"Candles updated: added={added}, total={len(self.candles_15m)}"
            )
            return list(self.candles_15m)

        except Exception as e:
            logger.error(f"fetch_historical_candles error: {e}")
            return list(self.candles_15m)

    async def compute_realized_vol(self) -> Optional[float]:
        """Compute 20-day realized volatility from log returns."""
        try:
            min_returns = config.RV_LOOKBACK_DAYS * 78
            if len(self.log_returns) < min_returns:
                logger.info(
                    f"Insufficient log returns for RV: "
                    f"{len(self.log_returns)}/{min_returns}"
                )
                return None

            returns_array = np.array(list(self.log_returns)[-min_returns:])
            rv = float(np.std(returns_array) * np.sqrt(252 * 78))
            self.rv_20d = rv

            if self.iv_atm and self.iv_atm > 0:
                iv_rv_spread = self.iv_atm - rv
                self.iv_rv_spread_history.append(iv_rv_spread)
                logger.info(
                    f"RV computed: rv={rv:.4f}, iv_atm={self.iv_atm:.4f}, "
                    f"spread={iv_rv_spread:.4f}"
                )

            return rv

        except Exception as e:
            logger.error(f"compute_realized_vol error: {e}")
            return None

    def wilders_smooth(self, data: np.ndarray, period: int) -> np.ndarray:
        """Apply Wilder's smoothing to data array."""
        result = np.zeros(len(data))
        if len(data) < period:
            return result
        result[period - 1] = np.mean(data[:period])
        for i in range(period, len(data)):
            result[i] = (result[i - 1] * (period - 1) + data[i]) / period
        return result

    def compute_adx(self, period: int = None) -> Optional[float]:
        """Compute ADX indicator from 15-minute candles using Wilder's method."""
        if period is None:
            period = config.ADX_PERIOD

        if len(self.candles_15m) < period * 2:
            logger.info(
                f"Insufficient candles for ADX: "
                f"{len(self.candles_15m)}/{period * 2}"
            )
            return None

        try:
            candles = list(self.candles_15m)
            highs = np.array([c["high"] for c in candles], dtype=float)
            lows = np.array([c["low"] for c in candles], dtype=float)
            closes = np.array([c["close"] for c in candles], dtype=float)

            n = len(candles)
            tr_arr = np.zeros(n)
            plus_dm_arr = np.zeros(n)
            minus_dm_arr = np.zeros(n)

            for i in range(1, n):
                high_low = highs[i] - lows[i]
                high_pc = abs(highs[i] - closes[i - 1])
                low_pc = abs(lows[i] - closes[i - 1])
                tr_arr[i] = max(high_low, high_pc, low_pc)

                up_move = highs[i] - highs[i - 1]
                down_move = lows[i - 1] - lows[i]

                if up_move > down_move and up_move > 0:
                    plus_dm_arr[i] = up_move
                else:
                    plus_dm_arr[i] = 0.0

                if down_move > up_move and down_move > 0:
                    minus_dm_arr[i] = down_move
                else:
                    minus_dm_arr[i] = 0.0

            smoothed_tr = self.wilders_smooth(tr_arr, period)
            smoothed_pdm = self.wilders_smooth(plus_dm_arr, period)
            smoothed_mdm = self.wilders_smooth(minus_dm_arr, period)

            plus_di = np.where(
                smoothed_tr > 1e-10,
                100.0 * smoothed_pdm / smoothed_tr,
                0.0
            )
            minus_di = np.where(
                smoothed_tr > 1e-10,
                100.0 * smoothed_mdm / smoothed_tr,
                0.0
            )

            di_sum = plus_di + minus_di
            di_diff = np.abs(plus_di - minus_di)
            dx = np.where(di_sum > 1e-10, 100.0 * di_diff / di_sum, 0.0)

            adx_arr = self.wilders_smooth(dx, period)
            self.adx = float(adx_arr[-1])

            logger.info(
                f"ADX computed: {self.adx:.2f} "
                f"(+DI={plus_di[-1]:.2f}, -DI={minus_di[-1]:.2f})"
            )
            return self.adx

        except Exception as e:
            logger.error(f"compute_adx error: {e}")
            return None

    def compute_ema_slope(self) -> Tuple[Optional[float], Optional[float]]:
        """Compute EMA(50) and its 5-bar slope from candle closes."""
        if len(self.candles_15m) < config.EMA_PERIOD + 5:
            logger.info(
                f"Insufficient candles for EMA slope: "
                f"{len(self.candles_15m)}/{config.EMA_PERIOD + 5}"
            )
            return (None, None)

        try:
            closes = np.array(
                [c["close"] for c in self.candles_15m], dtype=float
            )
            ema_series = pd.Series(closes).ewm(
                span=config.EMA_PERIOD, adjust=False
            ).mean()

            self.ema_50 = float(ema_series.iloc[-1])

            ema_prev = float(ema_series.iloc[-6])
            if ema_prev > 1e-10:
                slope = (self.ema_50 - ema_prev) / ema_prev
            else:
                slope = 0.0

            self.ema_slope = slope
            logger.info(
                f"EMA slope computed: ema50={self.ema_50:.2f}, "
                f"slope={self.ema_slope:.6f}"
            )
            return (self.ema_50, self.ema_slope)

        except Exception as e:
            logger.error(f"compute_ema_slope error: {e}")
            return (None, None)

    async def fetch_oi_snapshot(self) -> Dict:
        """Fetch OI snapshot for all chain strikes and compute OI changes."""
        try:
            if not self.option_chain:
                return {}

            strikes = list(self.option_chain.keys())
            prev_snapshot = (
                dict(self.oi_snapshots[-1])
                if self.oi_snapshots
                else {}
            )

            snapshot: Dict[float, Dict] = {}
            chunk_size = 50

            for i in range(0, len(strikes), chunk_size):
                chunk = strikes[i: i + chunk_size]
                instrument_keys = []
                for strike in chunk:
                    call_key = self.option_chain[strike]["call"].get("instrument_key", "")
                    put_key = self.option_chain[strike]["put"].get("instrument_key", "")
                    if call_key:
                        instrument_keys.append(call_key)
                    if put_key:
                        instrument_keys.append(put_key)

                if not instrument_keys:
                    continue

                try:
                    keys_str = ",".join(instrument_keys)
                    data = await self._api_get(
                        config.EP_GREEKS,
                        {"instrument_key": keys_str}
                    )
                    await asyncio.sleep(0.1)

                    for strike in chunk:
                        call_key = self.option_chain[strike]["call"].get("instrument_key", "")
                        put_key = self.option_chain[strike]["put"].get("instrument_key", "")

                        call_data = data.get(call_key, {})
                        put_data = data.get(put_key, {})

                        call_oi = float(call_data.get("oi", 0))
                        put_oi = float(put_data.get("oi", 0))
                        call_delta = float(call_data.get("delta", 0))
                        put_delta = float(put_data.get("delta", 0))

                        prev_call_oi = prev_snapshot.get(strike, {}).get("call_oi", call_oi)
                        prev_put_oi = prev_snapshot.get(strike, {}).get("put_oi", put_oi)

                        call_oi_change = call_oi - prev_call_oi
                        put_oi_change = put_oi - prev_put_oi

                        snapshot[strike] = {
                            "call_oi": call_oi,
                            "put_oi": put_oi,
                            "call_delta": call_delta,
                            "put_delta": put_delta,
                            "call_oi_change": call_oi_change,
                            "put_oi_change": put_oi_change,
                            "call_delta_oi": call_delta * call_oi_change,
                            "put_delta_oi": put_delta * put_oi_change
                        }

                        # Update chain greeks from response
                        if call_data:
                            self.option_chain[strike]["call"]["delta"] = float(call_data.get("delta", self.option_chain[strike]["call"]["delta"]))
                            self.option_chain[strike]["call"]["gamma"] = float(call_data.get("gamma", self.option_chain[strike]["call"]["gamma"]))
                            self.option_chain[strike]["call"]["vega"] = float(call_data.get("vega", self.option_chain[strike]["call"]["vega"]))
                            self.option_chain[strike]["call"]["theta"] = float(call_data.get("theta", self.option_chain[strike]["call"]["theta"]))
                            self.option_chain[strike]["call"]["iv"] = float(call_data.get("iv", self.option_chain[strike]["call"]["iv"]))
                        if put_data:
                            self.option_chain[strike]["put"]["delta"] = float(put_data.get("delta", self.option_chain[strike]["put"]["delta"]))
                            self.option_chain[strike]["put"]["gamma"] = float(put_data.get("gamma", self.option_chain[strike]["put"]["gamma"]))
                            self.option_chain[strike]["put"]["vega"] = float(put_data.get("vega", self.option_chain[strike]["put"]["vega"]))
                            self.option_chain[strike]["put"]["theta"] = float(put_data.get("theta", self.option_chain[strike]["put"]["theta"]))
                            self.option_chain[strike]["put"]["iv"] = float(put_data.get("iv", self.option_chain[strike]["put"]["iv"]))

                except Exception as e:
                    logger.warning(f"OI snapshot chunk error: {e}")
                    continue

            if snapshot:
                self.oi_snapshots.append(snapshot)
                logger.info(f"OI snapshot captured: {len(snapshot)} strikes")

            return snapshot

        except Exception as e:
            logger.error(f"fetch_oi_snapshot error: {e}")
            return {}

    def compute_net_flow(self) -> float:
        """Compute net delta-weighted OI flow over FLOW_WINDOW_MINUTES."""
        try:
            if len(self.oi_snapshots) < 3:
                self.net_flow = 0.0
                return 0.0

            snapshots_to_use = list(self.oi_snapshots)
            net_flow = 0.0

            for snapshot in snapshots_to_use:
                for strike, data in snapshot.items():
                    net_flow += data.get("call_delta_oi", 0.0)
                    net_flow += data.get("put_delta_oi", 0.0)

            self.net_flow = net_flow
            logger.info(f"Net flow computed: {self.net_flow:.4f}")
            return float(net_flow)

        except Exception as e:
            logger.error(f"compute_net_flow error: {e}")
            self.net_flow = 0.0
            return 0.0

    def compute_spread_ratio(self) -> float:
        """Compute bid-ask spread ratio for 3rd OTM put."""
        try:
            if not self.option_chain or not self.atm_strike:
                self.spread_ratio = 1.0
                return 1.0

            strikes_sorted = sorted(self.option_chain.keys())
            puts_below_atm = [s for s in strikes_sorted if s < self.atm_strike]

            if len(puts_below_atm) < config.OTM_STRIKE_OFFSET:
                self.spread_ratio = 1.0
                return 1.0

            third_otm_put = puts_below_atm[-config.OTM_STRIKE_OFFSET]
            put_data = self.option_chain[third_otm_put]["put"]
            ask = put_data.get("ask", 0)
            bid = put_data.get("bid", 0)
            current_spread = ask - bid

            self.bid_ask_spread_3otm.append(current_spread)

            if len(self.bid_ask_spread_3otm) < 3:
                self.spread_ratio = 1.0
                return 1.0

            avg_spread = float(np.mean(list(self.bid_ask_spread_3otm)))
            ratio = current_spread / (avg_spread + 1e-10)
            self.spread_ratio = ratio

            logger.info(
                f"Spread ratio computed: {ratio:.3f} "
                f"(current={current_spread:.2f}, avg={avg_spread:.2f})"
            )
            return float(ratio)

        except Exception as e:
            logger.error(f"compute_spread_ratio error: {e}")
            self.spread_ratio = 1.0
            return 1.0

    def get_strike_by_delta(
        self,
        option_type: str,
        target_delta: float,
        tolerance: float = 0.02
    ) -> Optional[float]:
        """Find strike closest to target delta for given option type."""
        if not self.option_chain:
            logger.warning("get_strike_by_delta: option chain empty")
            return None

        best_strike = None
        best_diff = float("inf")

        for strike, data in self.option_chain.items():
            opt_data = data.get(option_type, {})
            delta = opt_data.get("delta", None)
            if delta is None:
                continue

            if option_type == "put":
                delta_abs = abs(delta)
            else:
                delta_abs = delta

            diff = abs(delta_abs - target_delta)
            if diff < best_diff:
                best_diff = diff
                best_strike = strike

        if best_strike is not None and best_diff <= tolerance:
            return best_strike

        # Widen tolerance
        if best_strike is not None and best_diff <= 0.05:
            logger.warning(
                f"get_strike_by_delta: widened tolerance for "
                f"{option_type} delta={target_delta:.2f}, "
                f"found strike={best_strike} diff={best_diff:.3f}"
            )
            return best_strike

        logger.warning(
            f"get_strike_by_delta: no match for "
            f"{option_type} delta={target_delta:.2f} "
            f"tolerance={tolerance:.2f}"
        )
        return None

    def get_expiry_by_dte(
        self,
        target_dte: int,
        tolerance: int = 5
    ) -> Optional[str]:
        """Find expiry date closest to target DTE."""
        available = self.get_available_expiries()
        if not available:
            logger.warning("get_expiry_by_dte: no expiries available")
            return None

        today = date.today()
        best_expiry = None
        best_diff = float("inf")

        for exp_str in available:
            try:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                dte = (exp_date - today).days
                if dte < target_dte - tolerance:
                    continue
                diff = abs(dte - target_dte)
                if diff < best_diff:
                    best_diff = diff
                    best_expiry = exp_str
            except ValueError:
                continue

        if best_expiry is None:
            logger.warning(
                f"get_expiry_by_dte: no expiry found for "
                f"target_dte={target_dte} tolerance={tolerance}"
            )
        else:
            exp_date = datetime.strptime(best_expiry, "%Y-%m-%d").date()
            dte = (exp_date - today).days
            logger.info(
                f"get_expiry_by_dte: target={target_dte}, "
                f"found={best_expiry} (dte={dte})"
            )

        return best_expiry

    def get_available_expiries(self) -> List[str]:
        """Extract and return sorted list of available expiry dates from chain."""
        if not self.option_chain:
            return []

        expiry_set = set()
        for strike, data in self.option_chain.items():
            call_key = data.get("call", {}).get("instrument_key", "")
            put_key = data.get("put", {}).get("instrument_key", "")

            for key in [call_key, put_key]:
                if not key:
                    continue
                parts = key.split("|")
                if len(parts) >= 4:
                    try:
                        date_str = parts[2]
                        exp_date = datetime.strptime(date_str, "%d%b%Y").date()
                        expiry_set.add(exp_date.strftime("%Y-%m-%d"))
                    except ValueError:
                        pass

        return sorted(list(expiry_set))

    def compute_iv_rank(self) -> float:
        """Compute IV rank as percentile of current IV_ATM in history."""
        if len(self.iv_atm_history) < 10:
            return 50.0

        if self.iv_atm is None:
            return 50.0

        try:
            history = list(self.iv_atm_history)
            iv_high = max(history)
            iv_low = min(history)

            if abs(iv_high - iv_low) < 1e-10:
                return 50.0

            rank = ((self.iv_atm - iv_low) / (iv_high - iv_low)) * 100.0
            return float(max(0.0, min(100.0, rank)))

        except Exception as e:
            logger.error(f"compute_iv_rank error: {e}")
            return 50.0

    def compute_atr(self, period: int = 14) -> Optional[float]:
        """Compute Average True Range from candles."""
        if len(self.candles_15m) < period + 1:
            return None

        try:
            candles = list(self.candles_15m)
            true_ranges = []

            for i in range(1, len(candles)):
                high = candles[i]["high"]
                low = candles[i]["low"]
                prev_close = candles[i - 1]["close"]
                tr = max(
                    high - low,
                    abs(high - prev_close),
                    abs(low - prev_close)
                )
                true_ranges.append(tr)

            atr = float(np.mean(true_ranges[-period:]))
            return atr

        except Exception as e:
            logger.error(f"compute_atr error: {e}")
            return None

    def is_atr_contracting(self, lookback: int = 5) -> bool:
        """Return True if ATR is strictly decreasing over lookback bars."""
        try:
            if len(self.candles_15m) < lookback + 2:
                return False

            candles = list(self.candles_15m)
            atrs = []

            for end in range(len(candles) - lookback, len(candles) + 1):
                if end < 2:
                    continue
                segment = candles[max(0, end - 15):end]
                if len(segment) < 2:
                    continue
                trs = []
                for i in range(1, len(segment)):
                    high = segment[i]["high"]
                    low = segment[i]["low"]
                    prev_close = segment[i - 1]["close"]
                    tr = max(
                        high - low,
                        abs(high - prev_close),
                        abs(low - prev_close)
                    )
                    trs.append(tr)
                if trs:
                    atrs.append(float(np.mean(trs)))

            if len(atrs) < lookback:
                return False

            for i in range(1, len(atrs)):
                if atrs[i] >= atrs[i - 1]:
                    return False

            return True

        except Exception as e:
            logger.error(f"is_atr_contracting error: {e}")
            return False

    async def check_margin(self, legs: List[Dict]) -> Tuple[bool, float]:
        """Check if sufficient margin is available for given legs."""
        if config.PAPER_TRADING_MODE:
            return (True, 0.0)

        try:
            payload = {
                "instruments": [
                    {
                        "instrument_key": leg["instrument_key"],
                        "quantity": leg["quantity"],
                        "transaction_type": leg["transaction_type"],
                        "product": leg.get("product", "D"),
                        "price": leg.get("price", 0)
                    }
                    for leg in legs
                ]
            }

            response = await self._api_post(config.EP_MARGIN, payload)
            data = response.get("data", response)
            required = float(data.get("required_margin", 0))
            available = float(data.get("available_margin", 0))

            if available >= required:
                logger.info(
                    f"Margin OK: required={required:.0f}, available={available:.0f}"
                )
                return (True, required)
            else:
                logger.warning(
                    f"Insufficient margin: required={required:.0f}, "
                    f"available={available:.0f}"
                )
                return (False, required)

        except Exception as e:
            logger.error(f"check_margin error: {e}")
            return (True, 0.0)

    async def start_websocket(self) -> None:
        """Start and manage WebSocket V3 connection with reconnect logic."""
        import websockets

        for attempt in range(config.WS_RECONNECT_ATTEMPTS):
            try:
                logger.info(
                    f"WebSocket connect attempt {attempt + 1}/"
                    f"{config.WS_RECONNECT_ATTEMPTS}"
                )

                # Get authorized redirect URI
                auth_data = await self._api_get(
                    "/feed/market-data-feed/authorize", {}
                )
                ws_url = auth_data.get(
                    "authorizedRedirectUri",
                    config.WS_URL_V3
                )

                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=10
                ) as websocket:
                    self.ws = websocket
                    self.ws_connected = True
                    self._ws_decode_errors = 0
                    logger.info("WebSocket connected successfully")

                    # Build subscription list
                    instrument_keys = self._build_ws_subscription_keys()

                    sub_message = {
                        "guid": f"nifty_algo_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                        "method": "sub",
                        "data": {
                            "mode": config.WS_MODE_LTPC,
                            "instrumentKeys": instrument_keys
                        }
                    }
                    await websocket.send(json.dumps(sub_message))
                    logger.info(
                        f"WebSocket subscribed to {len(instrument_keys)} instruments"
                    )

                    self._subscribed_atm = self.atm_strike

                    async for message in websocket:
                        try:
                            if isinstance(message, bytes):
                                await self._ws_message_handler(message)
                            else:
                                data = json.loads(message)
                                self._process_json_feed(data)
                        except Exception as e:
                            logger.warning(f"WS message processing error: {e}")
                            self._ws_decode_errors += 1
                            if self._ws_decode_errors > 10:
                                logger.error(
                                    "Too many WS decode errors — reconnecting"
                                )
                                break

                        # Check if resubscription needed
                        if (self.atm_strike and self._subscribed_atm and
                                abs(self.atm_strike - self._subscribed_atm) >=
                                2 * config.NIFTY_STRIKE_STEP):
                            new_keys = self._build_ws_subscription_keys()
                            resub_msg = {
                                "guid": f"resub_{datetime.now().strftime('%H%M%S')}",
                                "method": "sub",
                                "data": {
                                    "mode": config.WS_MODE_LTPC,
                                    "instrumentKeys": new_keys
                                }
                            }
                            await websocket.send(json.dumps(resub_msg))
                            self._subscribed_atm = self.atm_strike
                            logger.info(
                                f"Resubscribed WS: new ATM={self.atm_strike}"
                            )

            except Exception as e:
                logger.error(f"WebSocket error on attempt {attempt + 1}: {e}")
                self.ws_connected = False
                if attempt < config.WS_RECONNECT_ATTEMPTS - 1:
                    logger.info(f"Waiting {config.WS_RECONNECT_DELAY_SEC}s before reconnect...")
                    await asyncio.sleep(config.WS_RECONNECT_DELAY_SEC)

        logger.critical("All WebSocket reconnect attempts failed — triggering kill switch")
        self.ws_connected = False
        self.kill_switch_triggered = True

    def _build_ws_subscription_keys(self) -> List[str]:
        """Build list of instrument keys for WebSocket subscription."""
        keys = [config.INSTRUMENT_NIFTY, config.INSTRUMENT_VIX]

        if not self.option_chain or not self.atm_strike:
            return keys

        strikes_sorted = sorted(self.option_chain.keys())
        atm = self.atm_strike

        # ATM ± 10 strikes
        atm_index = min(
            range(len(strikes_sorted)),
            key=lambda i: abs(strikes_sorted[i] - atm)
        )
        low_idx = max(0, atm_index - 10)
        high_idx = min(len(strikes_sorted) - 1, atm_index + 10)
        selected_strikes = strikes_sorted[low_idx: high_idx + 1]

        for strike in selected_strikes:
            call_key = self.option_chain[strike]["call"].get("instrument_key", "")
            put_key = self.option_chain[strike]["put"].get("instrument_key", "")
            if call_key:
                keys.append(call_key)
            if put_key:
                keys.append(put_key)

        return keys[:100]  # WebSocket limit

    async def _ws_message_handler(self, message: bytes) -> None:
        """Decode and process WebSocket protobuf message."""
        try:
            try:
                import MarketDataFeed_pb2 as pb
                feed_response = pb.FeedResponse()
                feed_response.ParseFromString(message)
                for key, feed in feed_response.feeds.items():
                    ltp = feed.ltpc.ltp
                    if ltp > 0:
                        self._update_instrument_ltp(key, ltp)
                    try:
                        if feed.HasField("optionGreeks"):
                            self._update_instrument_greeks(
                                key,
                                feed.optionGreeks.delta,
                                feed.optionGreeks.gamma,
                                feed.optionGreeks.vega,
                                feed.optionGreeks.theta,
                                feed.optionGreeks.iv
                            )
                    except Exception:
                        pass
                self._ws_decode_errors = 0

            except ImportError:
                try:
                    text = message.decode("utf-8")
                    data = json.loads(text)
                    self._process_json_feed(data)
                    self._ws_decode_errors = 0
                except Exception as e:
                    logger.warning(f"WS JSON fallback decode error: {e}")
                    self._ws_decode_errors += 1

        except Exception as e:
            logger.warning(f"WS message handler error: {e}")
            self._ws_decode_errors += 1
            if self._ws_decode_errors > 10:
                logger.error(
                    f"Too many WS decode errors ({self._ws_decode_errors}) — "
                    "scheduling reconnect"
                )
                asyncio.create_task(self._reconnect_websocket())

    def _update_instrument_ltp(self, instrument_key: str, ltp: float) -> None:
        """Update LTP for instrument in live state."""
        self.ws_last_msg_time = datetime.now(pytz.timezone(config.TZ))

        if instrument_key == config.INSTRUMENT_NIFTY:
            if ltp > 0:
                if self.spot and self.spot > 0:
                    change = abs(ltp / self.spot - 1)
                    if change > 0.05:
                        logger.critical(
                            f"WS spot spike: {self.spot:.2f} -> {ltp:.2f} "
                            f"({change * 100:.1f}%) — rejected"
                        )
                        return
                self.prev_spot = self.spot
                self.spot = float(ltp)
                if self.prev_spot and self.prev_spot > 0:
                    log_ret = np.log(self.spot / self.prev_spot)
                    self.log_returns.append(log_ret)

        elif instrument_key == config.INSTRUMENT_VIX:
            if ltp > 0:
                self.prev_vix = self.vix
                self.vix = float(ltp)
                self.vix_history_20d.append(self.vix)

        else:
            parts = instrument_key.split("|")
            if len(parts) >= 5:
                try:
                    strike_str = parts[3]
                    option_type = "call" if parts[4].upper() == "CE" else "put"
                    strike = float(strike_str)
                    if strike in self.option_chain:
                        if ltp > 0:
                            self.option_chain[strike][option_type]["ltp"] = ltp
                        else:
                            logger.warning(
                                f"WS LTP=0 for {instrument_key} — using last known"
                            )
                except (ValueError, KeyError):
                    pass

    def _update_instrument_greeks(
        self,
        instrument_key: str,
        delta: float,
        gamma: float,
        vega: float,
        theta: float,
        iv: float
    ) -> None:
        """Update Greeks for option instrument in chain."""
        parts = instrument_key.split("|")
        if len(parts) >= 5:
            try:
                strike = float(parts[3])
                option_type = "call" if parts[4].upper() == "CE" else "put"
                if strike in self.option_chain:
                    opt = self.option_chain[strike][option_type]
                    opt["delta"] = delta
                    opt["gamma"] = gamma
                    opt["vega"] = vega
                    opt["theta"] = theta
                    opt["iv"] = iv
            except (ValueError, KeyError):
                pass

    def _process_json_feed(self, data: Dict) -> None:
        """Process JSON format WebSocket feed (fallback)."""
        try:
            feeds = data.get("feeds", {})
            for key, feed_data in feeds.items():
                ltpc = feed_data.get("ltpc", {})
                ltp = float(ltpc.get("ltp", 0))
                if ltp > 0:
                    self._update_instrument_ltp(key, ltp)

                greeks = feed_data.get("optionGreeks", {})
                if greeks:
                    self._update_instrument_greeks(
                        key,
                        float(greeks.get("delta", 0)),
                        float(greeks.get("gamma", 0)),
                        float(greeks.get("vega", 0)),
                        float(greeks.get("theta", 0)),
                        float(greeks.get("iv", 0))
                    )
        except Exception as e:
            logger.warning(f"_process_json_feed error: {e}")

    async def _reconnect_websocket(self) -> None:
        """Attempt to reconnect WebSocket after failure."""
        logger.info("Attempting WebSocket reconnect...")
        self.ws_connected = False
        self._ws_decode_errors = 0

        for attempt in range(config.WS_RECONNECT_ATTEMPTS):
            try:
                await asyncio.sleep(config.WS_RECONNECT_DELAY_SEC)
                await self.start_websocket()
                logger.info("WebSocket reconnected successfully")
                return
            except Exception as e:
                logger.error(
                    f"Reconnect attempt {attempt + 1} failed: {e}"
                )

        logger.critical("All WebSocket reconnect attempts failed")
        self.kill_switch_triggered = True

    async def monitor_ws_health(self) -> None:
        """Monitor WebSocket health and trigger kill switch if silent too long."""
        while True:
            try:
                await asyncio.sleep(5)
                if self.ws_last_msg_time is None:
                    continue

                IST = pytz.timezone(config.TZ)
                now = datetime.now(IST)
                if self.ws_last_msg_time.tzinfo is None:
                    ws_time = IST.localize(self.ws_last_msg_time)
                else:
                    ws_time = self.ws_last_msg_time

                elapsed = (now - ws_time).total_seconds()

                if elapsed > config.WS_DOWNTIME_KILL_SWITCH_SEC:
                    logger.critical(
                        f"WebSocket silent for {elapsed:.0f}s — "
                        f"threshold={config.WS_DOWNTIME_KILL_SWITCH_SEC}s — "
                        "triggering kill switch"
                    )
                    self.kill_switch_triggered = True

            except asyncio.CancelledError:
                logger.info("WS health monitor cancelled")
                break
            except Exception as e:
                logger.error(f"monitor_ws_health error: {e}")

    def init_sqlite(self) -> None:
        """Initialize SQLite database with all required tables."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS market_state (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp        TEXT    NOT NULL,
                    spot             REAL,
                    vix              REAL,
                    iv_atm           REAL,
                    rv_20d           REAL,
                    skew             REAL,
                    adx              REAL,
                    ema_50           REAL,
                    composite_score  REAL,
                    regime           TEXT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS regime_history (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp           TEXT    NOT NULL,
                    vol_score           REAL,
                    edge_score          REAL,
                    trend_score         REAL,
                    flow_score          REAL,
                    composite_score     REAL,
                    raw_regime          TEXT,
                    confirmed_regime    TEXT,
                    persistence_count   INTEGER,
                    macro_override      INTEGER DEFAULT 0
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS open_positions (
                    trade_id            TEXT    PRIMARY KEY,
                    strategy_name       TEXT    NOT NULL,
                    regime_at_entry     TEXT,
                    entry_timestamp     TEXT,
                    entry_spot          REAL,
                    entry_vix           REAL,
                    expiry_date         TEXT,
                    legs_json           TEXT,
                    stop_loss           REAL,
                    profit_target       REAL,
                    exit_dte            INTEGER,
                    max_hold_date       TEXT,
                    composite_at_entry  REAL,
                    vol_score           REAL,
                    edge_score          REAL,
                    trend_score         REAL,
                    flow_score          REAL,
                    days_to_expiry      INTEGER,
                    total_credit        REAL,
                    total_debit         REAL,
                    net_premium         REAL,
                    max_risk            REAL,
                    paper_trade         INTEGER DEFAULT 1,
                    status              TEXT    DEFAULT 'OPEN',
                    created_at          TEXT    DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS closed_trades (
                    trade_id                TEXT    PRIMARY KEY,
                    strategy_name           TEXT,
                    regime_at_entry         TEXT,
                    regime_at_exit          TEXT,
                    entry_timestamp         TEXT,
                    exit_timestamp          TEXT,
                    holding_days            REAL,
                    entry_spot              REAL,
                    exit_spot               REAL,
                    entry_vix               REAL,
                    exit_vix                REAL,
                    legs_summary            TEXT,
                    total_credit_received   REAL,
                    total_debit_paid        REAL,
                    net_premium             REAL,
                    max_risk                REAL,
                    realized_pnl            REAL,
                    realized_pnl_percent    REAL,
                    exit_reason             TEXT,
                    slippage_total_points   REAL,
                    transaction_costs       REAL,
                    composite_score_at_entry REAL,
                    vol_score               REAL,
                    edge_score              REAL,
                    trend_score             REAL,
                    flow_score              REAL,
                    days_to_expiry_at_entry INTEGER,
                    expiry_date             TEXT,
                    paper_trade             INTEGER,
                    created_at              TEXT    DEFAULT CURRENT_TIMESTAMP
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
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp   TEXT    NOT NULL,
                    level       INTEGER NOT NULL,
                    trigger     TEXT,
                    action      TEXT,
                    daily_pnl   REAL,
                    drawdown    REAL,
                    regime      TEXT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS order_log (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       TEXT    NOT NULL,
                    trade_id        TEXT,
                    order_id        TEXT,
                    instrument_key  TEXT,
                    action          TEXT,
                    option_type     TEXT,
                    strike          REAL,
                    expiry          TEXT,
                    qty             INTEGER,
                    order_type      TEXT,
                    price           REAL,
                    fill_price      REAL,
                    status          TEXT,
                    slippage        REAL,
                    paper_trade     INTEGER DEFAULT 1
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_open_positions_status
                ON open_positions(status)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_regime_history_timestamp
                ON regime_history(timestamp)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_closed_trades_timestamp
                ON closed_trades(entry_timestamp)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_order_log_trade_id
                ON order_log(trade_id)
            """)

            conn.commit()
            conn.close()
            logger.info(f"SQLite initialized: {self.db_path}")

        except sqlite3.Error as e:
            logger.warning(f"SQLite init error: {e}")

    def save_state_to_sqlite(self, state: Dict) -> None:
        """Write market state to SQLite. Never raises."""
        try:
            conn = sqlite3.connect(self.db_path)
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
                state.get("regime")
            ))
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"save_state_to_sqlite error: {e}")

    def load_state_from_sqlite(self) -> Dict:
        """Load latest market state and open positions from SQLite."""
        result = {}
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute(
                "SELECT * FROM market_state ORDER BY id DESC LIMIT 1"
            )
            row = cursor.fetchone()
            if row:
                cols = [d[0] for d in cursor.description]
                result["market_state"] = dict(zip(cols, row))

            cursor.execute(
                "SELECT * FROM open_positions WHERE status = 'OPEN'"
            )
            rows = cursor.fetchall()
            if rows:
                cols = [d[0] for d in cursor.description]
                result["open_positions"] = [
                    dict(zip(cols, r)) for r in rows
                ]

            cursor.execute(
                "SELECT * FROM score_buffers"
            )
            rows = cursor.fetchall()
            if rows:
                cols = [d[0] for d in cursor.description]
                result["score_buffers"] = [
                    dict(zip(cols, r)) for r in rows
                ]

            conn.close()

        except sqlite3.OperationalError:
            logger.info("No existing state.db — fresh start")
        except sqlite3.Error as e:
            logger.warning(f"load_state_from_sqlite error: {e}")

        return result

    def save_position(self, position_dict: Dict) -> None:
        """Insert or replace position in open_positions table."""
        try:
            conn = sqlite3.connect(self.db_path)
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
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?
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
                position_dict.get("composite_score_at_entry"),
                position_dict.get("vol_score"),
                position_dict.get("edge_score"),
                position_dict.get("trend_score"),
                position_dict.get("flow_score"),
                position_dict.get("days_to_expiry_at_entry"),
                position_dict.get("total_credit_received"),
                position_dict.get("total_debit_paid"),
                position_dict.get("net_premium"),
                position_dict.get("max_risk"),
                1 if position_dict.get("paper_trade") else 0,
                "OPEN"
            ))
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"save_position SQLite error: {e}")

    def close_position(self, trade_id: str, exit_data: Dict) -> None:
        """Mark position closed in SQLite and write to CSV."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute(
                "UPDATE open_positions SET status = 'CLOSED' WHERE trade_id = ?",
                (trade_id,)
            )

            columns = ", ".join(config.TRADE_CSV_COLUMNS)
            placeholders = ", ".join(["?" for _ in config.TRADE_CSV_COLUMNS])
            values = [exit_data.get(col, None) for col in config.TRADE_CSV_COLUMNS]

            cursor.execute(
                f"INSERT OR REPLACE INTO closed_trades ({columns}) "
                f"VALUES ({placeholders})",
                values
            )

            conn.commit()
            conn.close()

            self.write_trade_to_csv(exit_data)
            logger.info(f"Position closed in DB: {trade_id}")

        except sqlite3.Error as e:
            logger.warning(f"close_position SQLite error: {e}")

    def write_trade_to_csv(self, trade_dict: Dict) -> None:
        """Append trade record to CSV file."""
        file_exists = os.path.exists(config.TRADE_CSV)
        try:
            with open(config.TRADE_CSV, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=config.TRADE_CSV_COLUMNS,
                    extrasaction="ignore"
                )
                if not file_exists:
                    writer.writeheader()
                writer.writerow(trade_dict)
            logger.info(
                f"Trade written to CSV: {trade_dict.get('trade_id', 'unknown')}"
            )
        except Exception as e:
            logger.warning(f"CSV write error: {e}")