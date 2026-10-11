"""Minimal Upstox REST client for MCX gold market data and orders."""

from __future__ import annotations

import base64
import gzip
import io
import json
import logging
import os
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from config import ENV_FILE, INSTRUMENT_CACHE, ROOT_DIR, EngineConfig

log = logging.getLogger("gold_news.upstox")

BASE_URL = "https://api.upstox.com"
# V3 order placement (HFT host). Market-data / portfolio remain on api.upstox.com.
ORDER_BASE_URL = "https://api-hft.upstox.com"
MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

# Current Upstox place-order product values (V2/V3 docs).
SUPPORTED_PRODUCTS = frozenset({"I", "D", "MTF"})


class CancelOutcome(str, Enum):
    """Distinct cancellation results — never treat 'inactive' as cancel-without-fill."""
    CONFIRMED_CANCELLED = "confirmed_cancelled"   # cancelled, zero additional fill
    FILLED = "filled"                             # fully executed (stop hit)
    PARTIAL_FILL = "partial_fill"
    REJECTED = "rejected"
    STILL_PENDING = "still_pending"
    UNKNOWN = "unknown"
    NOT_FOUND = "not_found"


@dataclass
class PositionsResult:
    """Successful empty list ≠ API failure."""
    ok: bool
    rows: list[dict]
    error: str = ""


@dataclass
class OrderBookResult:
    """Successful empty book ≠ API failure."""
    ok: bool
    rows: list[dict]
    error: str = ""

# Upstox rejects long 1-minute windows; chunk fetches.
MAX_RANGE_DAYS = {
    "1minute": 28,
    "30minute": 90,
    "day": 300,
    "week": 300,
    "month": 300,
}


class UpstoxAPIError(Exception):
    def __init__(self, status_code: int, payload: Any, url: str):
        self.status_code = status_code
        self.payload = payload
        self.url = url
        detail = payload.get("errors") if isinstance(payload, dict) else payload
        super().__init__(f"HTTP {status_code} from {url}: {detail}")


def load_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not os.path.exists(path):
        return values
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def resolve_credentials(cfg: EngineConfig) -> None:
    """Always load credentials from the repo-root ``env.txt`` only."""
    cfg.env_file = str(ENV_FILE)
    file_values = load_env_file(cfg.env_file)
    merged = dict(os.environ)
    merged.update({k: v for k, v in file_values.items() if v})
    cfg.access_token = merged.get("UPSTOX_ACCESS_TOKEN", "").strip()
    cfg.api_key = merged.get("UPSTOX_API_TOKEN", "") or merged.get("UPSTOX_API_KEY", "")
    cfg.api_secret = merged.get("UPSTOX_API_SECRET", "")
    cfg.redirect_uri = merged.get("UPSTOX_REDIRECT_URI", "")
    if not cfg.access_token:
        raise SystemExit(
            f"No UPSTOX_ACCESS_TOKEN in '{ENV_FILE}' or environment.\n"
            "Run: python gold-trade-engine/main.py --login"
        )


def token_expiry(access_token: str) -> Optional[datetime]:
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        if exp:
            return datetime.fromtimestamp(int(exp), tz=IST)
    except Exception:  # noqa: BLE001
        return None
    return None


def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {"raw": resp.text}


class UpstoxClient:
    def __init__(self, cfg: EngineConfig):
        self.cfg = cfg
        self.auth_ok = True
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Api-Version": "2.0",
            "Authorization": f"Bearer {cfg.access_token}",
        })
        product = str(cfg.product or "").upper().strip()
        if product not in SUPPORTED_PRODUCTS:
            raise SystemExit(
                f"Unsupported order product '{cfg.product}'. "
                f"Allowed: {sorted(SUPPORTED_PRODUCTS)} (MCX carry → D; intraday → I)."
            )

    def _request(
        self, method: str, path: str, *, params: Optional[dict] = None,
        json_body: Optional[dict] = None, form: Optional[dict] = None,
        base_url: Optional[str] = None, allow_retry: bool = True,
    ) -> Any:
        """
        HTTP helper. Non-idempotent order creation must call with allow_retry=False.
        """
        url = (base_url or BASE_URL) + path
        attempts = max(1, int(self.cfg.rest_retry)) if allow_retry else 1
        last_error: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.request(
                    method, url, params=params, json=json_body, data=form,
                    timeout=self.cfg.rest_timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                log.warning("Network error (%s/%s) %s: %s", attempt, attempts, path, exc)
                if not allow_retry or attempt >= attempts:
                    break
                time.sleep(min(2 ** attempt, 8))
                continue
            if resp.status_code == 401:
                self.auth_ok = False
                raise UpstoxAPIError(401, _safe_json(resp), url)
            if allow_retry and (resp.status_code == 429 or resp.status_code >= 500):
                last_error = UpstoxAPIError(resp.status_code, _safe_json(resp), url)
                time.sleep(min(2 ** attempt, 8))
                continue
            if not resp.ok:
                raise UpstoxAPIError(resp.status_code, _safe_json(resp), url)
            self.auth_ok = True
            return _safe_json(resp)
        raise last_error if last_error else UpstoxAPIError(0, None, url)

    def ltp(self, instrument_key: str) -> Optional[float]:
        data = self._request("GET", "/v2/market-quote/ltp",
                             params={"instrument_key": instrument_key})
        try:
            return float(data["data"][instrument_key]["last_price"])
        except (KeyError, TypeError, ValueError):
            return None

    def quote(self, instrument_key: str) -> Optional[dict]:
        data = self._request("GET", "/v2/market-quote/quotes",
                             params={"instrument_key": instrument_key})
        try:
            return data["data"][instrument_key]
        except (KeyError, TypeError):
            return None

    def bid_ask(self, instrument_key: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """
        Return (bid, ask, ltp) from the order book.

        Missing depth is returned as None — never substituted with LTP.
        Callers must treat None bid/ask as degraded and must not invent a zero spread.
        """
        q = self.quote(instrument_key)
        if not q:
            return None, None, None
        ltp = q.get("last_price")
        depth = q.get("depth") or {}
        buy = (depth.get("buy") or [{}])[0]
        sell = (depth.get("sell") or [{}])[0]
        bid = buy.get("price")
        ask = sell.get("price")
        try:
            bid_f = float(bid) if bid not in (None, 0, 0.0, "0") else None
            ask_f = float(ask) if ask not in (None, 0, 0.0, "0") else None
            ltp_f = float(ltp) if ltp not in (None, "", 0, 0.0) else None
            return bid_f, ask_f, ltp_f
        except (TypeError, ValueError):
            return None, None, None

    def historical(self, instrument_key: str, interval: str,
                   to_date: date, from_date: Optional[date] = None) -> pd.DataFrame:
        to_s = to_date.strftime("%Y-%m-%d")
        key = urllib.parse.quote(instrument_key, safe="")
        if from_date is None:
            path = f"/v2/historical-candle/{key}/{interval}/{to_s}"
        else:
            path = f"/v2/historical-candle/{key}/{interval}/{to_s}/{from_date:%Y-%m-%d}"
        return self._candles_frame(self._request("GET", path))

    def historical_range(self, instrument_key: str, interval: str,
                         to_date: date, from_date: date) -> pd.DataFrame:
        max_days = MAX_RANGE_DAYS.get(interval, 300)
        frames: list[pd.DataFrame] = []
        cur_to = to_date
        while cur_to >= from_date:
            cur_from = max(from_date, cur_to - timedelta(days=max_days - 1))
            try:
                df = self.historical(instrument_key, interval, cur_to, cur_from)
            except UpstoxAPIError as exc:
                log.warning("Upstox rejected %s..%s: %s", cur_from, cur_to, exc)
                df = pd.DataFrame()
            if not df.empty:
                frames.append(df)
            cur_to = cur_from - timedelta(days=1)
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames).sort_index()
        return out[~out.index.duplicated(keep="last")]

    def intraday(self, instrument_key: str, interval: str) -> pd.DataFrame:
        key = urllib.parse.quote(instrument_key, safe="")
        return self._candles_frame(
            self._request("GET", f"/v2/historical-candle/intraday/{key}/{interval}"))

    @staticmethod
    def _candles_frame(payload: Any) -> pd.DataFrame:
        rows = (payload or {}).get("data", {}).get("candles", []) or []
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        cols = ["ts", "open", "high", "low", "close", "volume", "oi"]
        df = pd.DataFrame(rows, columns=cols[:len(rows[0])])
        df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["ts", "close"]).set_index("ts").sort_index()
        keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        return df[keep]

    def place_order(
        self, *, instrument_token: str, transaction_type: str, quantity: int,
        order_type: str = "MARKET", price: float = 0.0,
        trigger_price: float = 0.0, tag: str = "",
        is_amo: bool = False,
    ) -> Optional[str]:
        """
        Place via V3 HFT endpoint. Quantity for MCX commodity = number of lots.

        Never auto-retries: a timeout after acceptance must be reconciled, not resent.
        """
        product = str(self.cfg.product or "D").upper().strip()
        if product not in SUPPORTED_PRODUCTS:
            raise ValueError(f"unsupported product {product}")
        uniq = tag or f"{self.cfg.order_tag}-{uuid.uuid4().hex[:12]}"
        body = {
            "quantity": int(quantity),
            "product": product,
            "validity": self.cfg.validity or "DAY",
            "price": float(price),
            "tag": uniq[:20],  # broker tag length limits
            "instrument_token": instrument_token,
            "order_type": order_type,
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": float(trigger_price),
            "is_amo": bool(is_amo),
            "slice": True,
        }
        mp = getattr(self.cfg, "market_protection", None)
        if mp is not None:
            body["market_protection"] = float(mp)
        # Persist tag for reconcile-by-tag on ambiguous responses
        self._last_order_tag = uniq
        payload = self._request(
            "POST", "/v3/order/place", json_body=body,
            base_url=ORDER_BASE_URL, allow_retry=False,
        )
        data = (payload or {}).get("data") or {}
        order_id = data.get("order_id") or data.get("orderId")
        if not order_id and isinstance(data.get("order_ids"), list) and data["order_ids"]:
            order_id = data["order_ids"][0]
        if not order_id:
            raise UpstoxAPIError(200, payload, "/v3/order/place")
        return str(order_id)

    def place_stop_loss_market(
        self, *, instrument_token: str, transaction_type: str, quantity: int,
        trigger_price: float, tag: str = "",
    ) -> Optional[str]:
        """Broker-side SL-M protective exit (trigger only; market when hit)."""
        return self.place_order(
            instrument_token=instrument_token,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type="SL-M",
            price=0.0,
            trigger_price=float(trigger_price),
            tag=tag or f"SL-{uuid.uuid4().hex[:10]}",
        )

    def cancel_order(self, order_id: str) -> CancelOutcome:
        """
        Request cancel and classify the final state.

        Returns CancelOutcome — callers must NOT treat FILLED as cancelled-without-fill.
        """
        if not order_id:
            return CancelOutcome.NOT_FOUND
        try:
            self._request(
                "DELETE", "/v3/order/cancel",
                params={"order_id": order_id},
                base_url=ORDER_BASE_URL,
                allow_retry=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("cancel_order(%s) request failed: %s", order_id, exc)
        return self.classify_order_state(order_id, attempts=8, sleep_s=0.35)

    def classify_order_state(
        self, order_id: str, *, attempts: int = 6, sleep_s: float = 0.35,
    ) -> CancelOutcome:
        """Poll order details into a CancelOutcome (also used for stop lifecycle)."""
        if not order_id:
            return CancelOutcome.NOT_FOUND
        last_status = ""
        filled_i = 0
        qty_i = 0
        for _ in range(attempts):
            try:
                details = self.order_details(order_id) or {}
            except Exception:  # noqa: BLE001
                time.sleep(sleep_s)
                continue
            if not details:
                time.sleep(sleep_s)
                continue
            last_status = str(
                details.get("status") or details.get("order_status") or ""
            ).lower().replace(" ", "_")
            try:
                filled_i = int(float(
                    details.get("filled_quantity")
                    or details.get("filled_qty")
                    or details.get("traded_quantity")
                    or 0
                ))
            except (TypeError, ValueError):
                filled_i = 0
            try:
                qty_i = int(float(details.get("quantity") or 0))
            except (TypeError, ValueError):
                qty_i = 0
            if last_status in ("cancelled", "canceled", "expired"):
                if filled_i > 0:
                    # Missing order qty with positive fill → unknown, not confirmed full fill
                    if qty_i <= 0:
                        return CancelOutcome.UNKNOWN
                    return (
                        CancelOutcome.FILLED if filled_i >= qty_i
                        else CancelOutcome.PARTIAL_FILL
                    )
                return CancelOutcome.CONFIRMED_CANCELLED
            if last_status in ("rejected", "failed"):
                if filled_i <= 0:
                    return CancelOutcome.REJECTED
                if qty_i <= 0:
                    return CancelOutcome.UNKNOWN
                return (
                    CancelOutcome.FILLED if filled_i >= qty_i
                    else CancelOutcome.PARTIAL_FILL
                )
            if last_status in ("complete", "completed", "filled", "traded"):
                if filled_i <= 0:
                    return CancelOutcome.UNKNOWN
                if qty_i > 0 and filled_i < qty_i:
                    return CancelOutcome.PARTIAL_FILL
                return CancelOutcome.FILLED
            if last_status in (
                "trigger_pending", "open", "pending", "after_market_order_req",
                "modify_pending",
            ):
                time.sleep(sleep_s)
                continue
            time.sleep(sleep_s)
        if last_status in (
            "trigger_pending", "open", "pending", "after_market_order_req",
        ):
            return CancelOutcome.STILL_PENDING
        log.warning("classify_order_state(%s) unresolved status=%s", order_id, last_status)
        return CancelOutcome.UNKNOWN

    def order_details(self, order_id: str) -> dict:
        payload = self._request("GET", "/v2/order/details", params={"order_id": order_id})
        data = (payload or {}).get("data") or {}
        if isinstance(data, dict) and "order" in data:
            return data.get("order") or {}
        return data if isinstance(data, dict) else {}

    def fetch_order_book(self) -> OrderBookResult:
        """Distinguish successful empty book from API failure."""
        try:
            payload = self._request("GET", "/v2/order/retrieve-all")
        except Exception as exc:  # noqa: BLE001
            return OrderBookResult(ok=False, rows=[], error=str(exc))
        data = (payload or {}).get("data")
        if isinstance(data, list):
            return OrderBookResult(ok=True, rows=data)
        return OrderBookResult(ok=False, rows=[], error="order book payload not a list")

    def order_book(self) -> list[dict]:
        """Legacy: raises on failure so callers cannot mistake error for empty."""
        res = self.fetch_order_book()
        if not res.ok:
            raise UpstoxAPIError(0, {"error": res.error}, "/v2/order/retrieve-all")
        return res.rows

    def find_orders_by_tag(self, tag: str) -> list[dict]:
        tag = (tag or "").strip()
        if not tag:
            return []
        res = self.fetch_order_book()
        if not res.ok:
            raise UpstoxAPIError(0, {"error": res.error}, "/v2/order/retrieve-all")
        out = []
        for row in res.rows:
            if str(row.get("tag") or "") == tag or str(row.get("order_tag") or "") == tag:
                out.append(row)
        return out

    def fetch_positions(self) -> PositionsResult:
        """Distinguish successful empty positions from API failure."""
        try:
            payload = self._request("GET", "/v2/portfolio/short-term-positions")
        except Exception as exc:  # noqa: BLE001
            return PositionsResult(ok=False, rows=[], error=str(exc))
        data = (payload or {}).get("data")
        if isinstance(data, list):
            return PositionsResult(ok=True, rows=data)
        # Fallback path — still fail closed if that also errors
        try:
            payload = self._request("GET", "/v2/order/positions")
            data = (payload or {}).get("data")
            if isinstance(data, list):
                return PositionsResult(ok=True, rows=data)
            return PositionsResult(ok=False, rows=[], error="positions payload not a list")
        except Exception as exc:  # noqa: BLE001
            return PositionsResult(ok=False, rows=[], error=str(exc))

    def positions(self) -> list[dict]:
        """Legacy: returns rows only when fetch succeeded; raises on failure."""
        res = self.fetch_positions()
        if not res.ok:
            raise UpstoxAPIError(0, {"error": res.error}, "/v2/portfolio/short-term-positions")
        return res.rows

    def net_position_qty(
        self, instrument_key: str, *, product: str = "",
        all_products: bool = True,
    ) -> Optional[int]:
        """
        Net signed qty for exact instrument_key.

        Prefer documented ``net_quantity`` / ``net_qty`` over raw ``quantity``.
        By default sums ALL products for the instrument (manual/other strategies).
        Returns None when positions cannot be fetched or quantity is malformed.
        Returns 0 only when the API succeeded and no matching row exists.
        """
        res = self.fetch_positions()
        if not res.ok:
            log.warning("positions unavailable: %s", res.error)
            return None
        want_prod = (product or str(self.cfg.product or "")).upper().strip()
        total = 0
        found = False
        for row in res.rows:
            key = str(row.get("instrument_token") or row.get("instrument_key") or "")
            if key != str(instrument_key):
                continue
            if not all_products and want_prod:
                row_prod = str(row.get("product") or row.get("product_type") or "").upper()
                if row_prod and row_prod != want_prod:
                    continue
            found = True
            qty_val = None
            # Prefer net fields (Upstox short-term positions: net after buy/sell offset)
            for fld in ("net_quantity", "net_qty", "quantity"):
                if row.get(fld) is not None and row.get(fld) != "":
                    try:
                        qty_val = int(float(row[fld]))
                        break
                    except (TypeError, ValueError):
                        return None  # malformed → fail closed
            if qty_val is None:
                log.warning("position row missing net quantity for %s", key)
                return None
            total += qty_val
        return total if found else 0

    def get_funds(self) -> Optional[dict]:
        """Broker funds / margin snapshot; None if unavailable."""
        try:
            payload = self._request("GET", "/v2/user/get-funds-and-margin")
        except Exception as exc:  # noqa: BLE001
            log.warning("get_funds failed: %s", exc)
            return None
        data = (payload or {}).get("data")
        return data if isinstance(data, dict) else None

    @staticmethod
    def exchange_code(*, code: str, client_id: str, client_secret: str,
                      redirect_uri: str) -> dict:
        form = {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        resp = requests.post(
            BASE_URL + "/v2/login/authorization/token", data=form,
            headers={"Accept": "application/json", "Api-Version": "2.0"},
            timeout=20,
        )
        return _safe_json(resp)

    @staticmethod
    def login_url(client_id: str, redirect_uri: str) -> str:
        q = urllib.parse.urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
        })
        return f"{BASE_URL}/v2/login/authorization/dialog?{q}"


def load_instrument_master(cache_path: str = "") -> list[dict]:
    """
    Load instrument master into the engine folder cache.

    Prefers ``gold-trade-engine/instrument_master_cache.json``. If missing, copies
    from the repo-root cache when present, otherwise downloads into the engine folder.
    """
    path = Path(cache_path) if cache_path else INSTRUMENT_CACHE
    if not path.is_absolute():
        path = INSTRUMENT_CACHE.parent / path
    # Default name always lands in the engine folder (not repo root).
    if path.name == INSTRUMENT_CACHE.name:
        path = INSTRUMENT_CACHE

    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    root_cache = ROOT_DIR / "instrument_master_cache.json"
    if root_cache.exists() and root_cache.resolve() != path.resolve():
        log.info("Seeding instrument cache from %s → %s", root_cache, path)
        data = json.loads(root_cache.read_text(encoding="utf-8"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return data

    log.info("Downloading instrument master → %s", path)
    resp = requests.get(MASTER_URL, timeout=60)
    resp.raise_for_status()
    raw = gzip.GzipFile(fileobj=io.BytesIO(resp.content)).read()
    data = json.loads(raw.decode("utf-8"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def _mcx_gold_candidates(master: list[dict], symbol: str,
                         as_of: date) -> list[tuple[date, dict]]:
    """All unexpired MCX gold futures for ``symbol``, sorted by expiry ascending."""
    symbol = symbol.upper().strip()
    prefix = f"{symbol} FUT"
    candidates: list[tuple[date, dict]] = []
    for row in master:
        if row.get("exchange") != "MCX":
            continue
        if row.get("instrument_type") != "FUT":
            continue
        tsym = str(row.get("trading_symbol", "")).upper()
        if not tsym.startswith(prefix):
            continue
        # Boundary guard: "GOLD FUT" must not accept "GOLDEN…" / "GOLDM FUT".
        rest = tsym[len(prefix):]
        if rest and rest[0] not in (" ",):
            continue
        exp_ms = row.get("expiry")
        if not exp_ms:
            continue
        exp = datetime.fromtimestamp(exp_ms / 1000, tz=UTC).date()
        if exp < as_of:
            continue
        candidates.append((exp, row))
    candidates.sort(key=lambda x: x[0])
    return candidates


def resolve_mcx_gold_future(master: list[dict], symbol: str = "GOLD",
                            as_of: Optional[date] = None,
                            override_key: str = "") -> dict:
    """
    Front-month MCX futures for GOLD / GOLDM / etc.

    Filters by trading_symbol prefix '{NAME} FUT' so GOLDGUINEA is not mistaken
    for GOLD.
    """
    if override_key:
        for row in master:
            if row.get("instrument_key") == override_key:
                # Must resolve to MCX gold futures for the requested symbol.
                tsym = str(row.get("trading_symbol", "")).upper()
                prefix = f"{symbol.upper().strip()} FUT"
                if row.get("exchange") != "MCX" or row.get("instrument_type") != "FUT":
                    raise RuntimeError(
                        f"Override {override_key} is not an MCX FUT "
                        f"(exchange={row.get('exchange')} type={row.get('instrument_type')})"
                    )
                if not tsym.startswith(prefix):
                    raise RuntimeError(
                        f"Override {override_key} symbol {tsym!r} does not match {prefix!r}"
                    )
                return row
        raise RuntimeError(
            f"instrument_key_override {override_key!r} not found in instrument master"
        )

    as_of = as_of or datetime.now(tz=IST).date()
    candidates = _mcx_gold_candidates(master, symbol, as_of)
    if not candidates:
        raise RuntimeError(f"No MCX {symbol} futures found in instrument master")
    return candidates[0][1]


def list_mcx_gold_futures(master: list[dict], symbol: str = "GOLD",
                          as_of: Optional[date] = None,
                          until: Optional[date] = None) -> list[dict]:
    """
    Expiry chain for historical backtests (front → deferred).

    Returns contracts that cover ``[as_of, until]``: the front month active at
    ``as_of`` plus later expiries through ``until``.
    """
    as_of = as_of or datetime.now(tz=IST).date()
    until = until or as_of
    symbol = symbol.upper().strip()
    prefix = f"{symbol} FUT"
    all_rows: list[tuple[date, dict]] = []
    for row in master:
        if row.get("exchange") != "MCX":
            continue
        if row.get("instrument_type") != "FUT":
            continue
        tsym = str(row.get("trading_symbol", "")).upper()
        if not tsym.startswith(prefix):
            continue
        rest = tsym[len(prefix):]
        if rest and rest[0] not in (" ",):
            continue
        exp_ms = row.get("expiry")
        if not exp_ms:
            continue
        exp = datetime.fromtimestamp(exp_ms / 1000, tz=UTC).date()
        all_rows.append((exp, row))
    all_rows.sort(key=lambda x: x[0])
    if not all_rows:
        return []
    # First contract with expiry >= as_of (front at start); if none, last known.
    start_i = 0
    for i, (exp, _) in enumerate(all_rows):
        if exp >= as_of:
            start_i = i
            break
    else:
        start_i = len(all_rows) - 1
    out: list[dict] = []
    for exp, row in all_rows[start_i:]:
        out.append(row)
        if exp >= until:
            break
    return out or [all_rows[start_i][1]]


def stitch_front_month_history(
    client: "UpstoxClient",
    contracts: list[dict],
    interval: str,
    start: date,
    end: date,
    *,
    roll_days_before_expiry: int = 3,
) -> pd.DataFrame:
    """
    Stitch continuous front-month series by rolling ``roll_days_before_expiry``
    calendar days before each contract's expiry into the next.

    Prices are not ratio-adjusted; a roll gap is left as-is so stop/gap logic
    stays honest.
    """
    if not contracts:
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    for i, row in enumerate(contracts):
        key = str(row.get("instrument_key") or "")
        if not key:
            continue
        exp_ms = row.get("expiry")
        if not exp_ms:
            continue
        exp = datetime.fromtimestamp(exp_ms / 1000, tz=UTC).date()
        if i == 0:
            seg_start = start
        else:
            prev_exp = datetime.fromtimestamp(
                contracts[i - 1].get("expiry", exp_ms) / 1000, tz=UTC
            ).date()
            seg_start = max(start, prev_exp - timedelta(days=roll_days_before_expiry))
        if i == len(contracts) - 1:
            seg_end = end
        else:
            seg_end = min(end, exp - timedelta(days=roll_days_before_expiry))
        if seg_end < seg_start:
            continue
        log.info(
            "Front-month segment %s %s .. %s (expiry=%s)",
            row.get("trading_symbol") or key, seg_start, seg_end, exp,
        )
        df = client.historical_range(key, interval, seg_end, seg_start)
        if df.empty:
            continue
        ts0 = pd.Timestamp(seg_start, tz="UTC")
        ts1 = pd.Timestamp(seg_end, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        df = df.loc[(df.index >= ts0) & (df.index <= ts1)]
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames).sort_index()
    return out[~out.index.duplicated(keep="last")]


def resolve_usdinr_future(master: list[dict], as_of: Optional[date] = None,
                          override_key: str = "") -> dict:
    if override_key:
        return {"instrument_key": override_key, "trading_symbol": override_key,
                "lot_size": 1, "name": "USDINR"}
    as_of = as_of or datetime.now(tz=IST).date()
    candidates = []
    for row in master:
        if row.get("name") != "USDINR":
            continue
        if row.get("instrument_type") != "FUT":
            continue
        if row.get("exchange") not in ("NSE", "BSE"):
            continue
        exp_ms = row.get("expiry")
        if not exp_ms:
            continue
        exp = datetime.fromtimestamp(exp_ms / 1000, tz=UTC).date()
        if exp < as_of:
            continue
        # Prefer NSE currency segment
        rank = 0 if row.get("exchange") == "NSE" else 1
        candidates.append((exp, rank, row))
    if not candidates:
        raise RuntimeError("No USDINR futures found")
    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][2]


def update_env_file(path: str, key: str, value: str) -> None:
    """Write credentials only to the repo-root env.txt (path arg is ignored if not root)."""
    target = Path(path).resolve() if path else ENV_FILE
    if target.name != "env.txt" or target.parent.resolve() != ROOT_DIR.resolve():
        log.warning("Redirecting credential write to repo-root env.txt (refused %s)", target)
        target = ENV_FILE
    lines: list[str] = []
    found = False
    if target.exists():
        with target.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip().startswith(f"{key}="):
                    lines.append(f"{key}={value}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f"{key}={value}\n")
    with target.open("w", encoding="utf-8") as fh:
        fh.writelines(lines)
