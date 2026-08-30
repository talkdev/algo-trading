# -*- coding: utf-8 -*-
"""
================================================================================
 execution.py — merged data_manager.py + order_executor.py + risk_manager.py
================================================================================
   * data_manager.py   : token-bucket rate limiter, Upstox REST client,
                         WebSocket v3 streamer (protobuf), instrument-master
                         cache, DemoProvider synthetic feed, Paper/Live brokers.
   * order_executor.py : entry sequencing (longs first / shorts second),
                         timeouts, partial-fill cancel+abort, exits, regime
                         transition rules A-E, trade_analysis.csv rows.
   * risk_manager.py   : 2% per-trade / 4% combined caps, VIX-scaled stops,
                         daily-loss breaker, kill-switch, margin checks.
================================================================================
"""
import gzip
import json
import math
import os
import random
import re
import statistics
import threading
import time
import uuid
from datetime import datetime, timedelta

import requests

import main as C
from main import IST, now_ist, VirtualClock
from indicators import bs_delta, bs_price, round_to_nearest, round_down, round_up


# =====================================================================
# PART 1 — DATA MANAGER (merged from data_manager.py)
# =====================================================================

import gzip
import json
import math
import os
import random
import re
import statistics
import threading
import time
from datetime import datetime, timedelta

import requests

from indicators import bs_delta, bs_price, round_to_nearest, round_down, round_up

# ============================================================================
# 1. RATE LIMITER
# ============================================================================
class TokenBucket:
    """Thread-safe token bucket: `rate` tokens/sec refill, `burst` capacity."""

    def __init__(self, rate=C.RATE_LIMIT_PER_SEC, burst=C.RATE_LIMIT_BURST):
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._ts = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, n=1, timeout=10.0):
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.burst, self._tokens + (now - self._ts) * self.rate)
                self._ts = now
                if self._tokens >= n:
                    self._tokens -= n
                    return True
                wait = (n - self._tokens) / self.rate
            if time.monotonic() + wait > deadline:
                return False
            time.sleep(min(wait, 0.05))

# ============================================================================
# 2. ERRORS
# ============================================================================
class UpstoxError(Exception):
    pass

class AuthError(UpstoxError):
    pass

class RateLimitError(UpstoxError):
    pass

# ============================================================================
# 3. REST CLIENT
# ============================================================================
def fnum(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None

def load_env_file(path):
    """Parse KEY = "value" style files (env.txt / .env)."""
    out = {}
    if not path or not os.path.isfile(path):
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

def parse_candles(payload):
    """[[ts,o,h,l,c,v,oi]...] -> [{dt,o,h,l,c}] ascending."""
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
    """Raw option-chain rows -> sorted [{strike, spot, c:leg, p:leg}]."""
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

class UpstoxClient:
    def __init__(self, token, limiter=None, timeout=15):
        self.h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self.timeout = timeout
        self.limiter = limiter or TokenBucket()
        self._token_key_map = {}

    # ------------------------------------------------------------- transport
    def _request(self, method, url, params=None, json_body=None, retries=4):
        for attempt in range(1, retries + 1):
            if not self.limiter.acquire(1):
                raise RateLimitError("token bucket exhausted")
            try:
                resp = requests.request(method, url, params=params, json=json_body,
                                        headers=self.h, timeout=self.timeout)
            except requests.RequestException as e:
                if attempt == retries:
                    raise UpstoxError(f"network error: {e}")
                time.sleep(C.RETRY_BACKOFF_BASE * attempt)
                continue
            if resp.status_code in (401, 403):
                raise AuthError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code == 429:
                wait = min(C.RETRY_BACKOFF_BASE * (2 ** (attempt - 1)), C.RETRY_MAX_BACKOFF)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                if attempt == retries:
                    raise UpstoxError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                time.sleep(C.RETRY_BACKOFF_BASE * attempt)
                continue
            if resp.status_code != 200:
                raise UpstoxError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        raise UpstoxError("unreachable")

    def _get(self, url, params=None):
        return self._request("GET", url, params=params)

    def _post(self, url, json_body=None):
        return self._request("POST", url, json_body=json_body)

    # ----------------------------------------------------------- market data
    def quotes(self, keys):
        """-> {instrument_key: {last_price, net_change, bid, ask, oi, timestamp}}"""
        if not keys:
            return {}
        data = self._get(f"{C.UPSTOX_BASE_V2}/market-quote/quotes",
                         {"instrument_key": ",".join(keys)})
        return self._keyed_quotes(data.get("data", {}))

    def ltp(self, keys):
        data = self._get(f"{C.UPSTOX_BASE_V2}/market-quote/ltp",
                         {"instrument_key": ",".join(keys)})
        return self._keyed_quotes(data.get("data", {}))

    def _keyed_quotes(self, raw):
        """Upstox returns quotes keyed by instrument_token; remap to keys."""
        tok2key = self._token_key_map
        out = {}
        for token, q in (raw or {}).items():
            key = tok2key.get(str(token))
            if key is None:
                key = self._match_suffix(token)
            if key is None:
                key = str(token)
            depth = q.get("depth") or {}
            buy_l = (depth.get("buy") or [{}])
            sell_l = (depth.get("sell") or [{}])
            out[key] = {
                "last_price": fnum(q.get("last_price")),
                "net_change": fnum(q.get("net_change")),
                "bid": fnum(buy_l[0].get("price")) if buy_l else None,
                "ask": fnum(sell_l[0].get("price")) if sell_l else None,
                "oi": fnum(q.get("oi")),
                "timestamp": q.get("timestamp"),
            }
        return out

    def _match_suffix(self, token):
        for k in self._token_key_map.values():
            if str(token).endswith(k.split("|")[-1].lower()) or \
               str(token).lower() == k.split("|")[-1].lower():
                return k
        return None

    def option_chain(self, underlying_key, expiry_iso):
        data = self._get(f"{C.UPSTOX_BASE_V2}/option/chain",
                         {"instrument_key": underlying_key, "expiry_date": expiry_iso})
        return normalize_chain(data.get("data", []))

    def daily_candles(self, key, days=110):
        to = now_ist().date()
        frm = to - timedelta(days=days)
        data = self._get(f"{C.UPSTOX_BASE_V2}/historical-candle/{key}/day/{to}/{frm}")
        return parse_candles(data)

    def hist_5m(self, key, days=C.HIST_DAYS_5M):
        to = now_ist().date()
        frm = to - timedelta(days=days)
        data = self._get(f"{C.UPSTOX_BASE_V3}/historical-candle/{key}/minutes/5/{to}/{frm}")
        return parse_candles(data)

    def intraday_5m(self, key):
        try:
            data = self._get(f"{C.UPSTOX_BASE_V3}/historical-candle/intraday/{key}/minutes/5")
            return parse_candles(data)
        except UpstoxError:
            return []

    def vix_series(self, days=30):
        try:
            to = now_ist().date()
            frm = to - timedelta(days=days)
            data = self._get(f"{C.UPSTOX_BASE_V2}/historical-candle/{C.KEY_VIX}/day/{to}/{frm}")
            return [b["c"] for b in parse_candles(data)]
        except UpstoxError:
            return []

    # --------------------------------------------------- instrument master
    def instrument_master(self, cache_dir=".", force=False):
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
        resp = requests.get(C.INSTRUMENT_MASTER_URL, headers=headers, timeout=120)
        resp.raise_for_status()
        rows = json.loads(gzip.decompress(resp.content).decode("utf-8"))
        nifty = [x for x in rows if x.get("segment") == "NSE_FO" and x.get("name") == "NIFTY"]
        futs = sorted(({"key": f["instrument_key"], "symbol": f.get("trading_symbol", "NIFTY FUT"),
                        "expiry": ms_to_date(f["expiry"])} for f in nifty
                       if f.get("instrument_type") == "FUT"),
                      key=lambda f: f["expiry"])
        seen = {}
        for o in nifty:
            if o.get("instrument_type") not in ("CE", "PE"):
                continue
            d = ms_to_date(o["expiry"])
            seen[d] = seen.get(d, False) or bool(o.get("weekly"))
        expiries = [{"date": d, "weekly": seen[d]} for d in sorted(seen)]
        # token -> key map for quote remapping
        for o in rows:
            k = o.get("instrument_key")
            t = o.get("instrument_token")
            if k and t:
                self._token_key_map[str(t)] = k
        data = {"date": today, "futs": futs, "expiries": expiries, "source": "instrument master",
                "lot_size": next((int(x.get("lot_size") or C.NIFTY_LOT_SIZE) for x in nifty
                                  if x.get("instrument_type") == "FUT"), C.NIFTY_LOT_SIZE)}
        try:
            with open(cache, "w") as fh:
                json.dump(data, fh)
        except OSError:
            pass
        return data

    # ----------------------------------------------------------- live orders
    def margin_required(self, instruments):
        """POST /v2/charges/margin. instruments: [{'instrument_key', 'quantity',
        'transaction_type', 'product'}] -> required margin ₹ or None on failure."""
        try:
            data = self._post(f"{C.UPSTOX_BASE_V2}/charges/margin",
                              {"instruments": instruments})
            d = data.get("data") or {}
            total = d.get("total") or {}
            return fnum(total.get("required_margin") or total.get("total_margin_required"))
        except UpstoxError:
            return None

    def place_order(self, instrument_key, transaction_type, order_type, quantity,
                    price=None, product="I", validity="DAY", tag=None):
        body = {
            "instrument_token": self._resolve_token(instrument_key),
            "transaction_type": transaction_type,
            "order_type": order_type,
            "quantity": int(quantity),
            "product": product,
            "validity": validity,
            "instrument_key": instrument_key,
        }
        if price is not None and order_type == "LIMIT":
            body["price"] = round(float(price), 2)
        if tag:
            body["tag"] = str(tag)[:20]
        data = self._post(f"{C.UPSTOX_BASE_V2}/order/place", body)
        return (data.get("data") or {}).get("order_id")

    def cancel_order(self, order_id):
        self._request("DELETE", f"{C.UPSTOX_BASE_V2}/order/cancel/{order_id}")

    def order_history(self, order_id):
        data = self._get(f"{C.UPSTOX_BASE_V2}/order/history/{order_id}")
        rows = (data.get("data") or [])
        if not rows:
            return {"status": "UNKNOWN"}
        latest = rows[-1]
        return {"status": (latest.get("status") or "OPEN"),
                "filled_qty": int(latest.get("filled_quantity") or 0),
                "avg_price": fnum(latest.get("average_price")),
                "reject_reason": latest.get("reject_reason")}

    def _resolve_token(self, instrument_key):
        for t, k in self._token_key_map.items():
            if k == instrument_key:
                return t
        return instrument_key

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

# ============================================================================
# 4. WEB-SOCKET V3 STREAMER (minimal protobuf wire decode, mode "ltpc")
# ============================================================================
def _read_varint(buf, pos):
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7

def _decode_ltpc_message(payload: bytes):
    """Minimal protobuf decode of MarketFullFeed{LTPC ltpc=1,...} -> dict."""
    out = {}
    pos = 0
    n = len(payload)
    while pos < n:
        tag, pos = _read_varint(payload, pos)
        field, wire = tag >> 3, tag & 7
        if wire == 0:                       # varint
            val, pos = _read_varint(payload, pos)
            if field == 1:
                out["ltpc"] = val
        elif wire == 1:                     # fixed64 (double)
            val = 0
            for i in range(8):
                val |= payload[pos + i] << (8 * i)
            pos += 8
            import struct
            out[field] = struct.unpack("<d", val.to_bytes(8, "little"))[0]
        elif wire == 2:                     # length-delimited
            length, pos = _read_varint(payload, pos)
            data = payload[pos:pos + length]
            pos += length
            if field == 1:
                out["ltpc_msg"] = data
            else:
                out[field] = data
        elif wire == 5:                     # fixed32
            pos += 4
        else:
            break
    return out

def _decode_feed_response(payload: bytes):
    """FeedResponse { map<string,MarketFullFeed> feeds = 1; int64 currentTs = 2; }.
    Map entries are repeated message field 1 with key (field 1, wire 2) and
    value (field 2, wire 2). Returns {token: ltpc_dict}."""
    feeds = {}
    pos = 0
    n = len(payload)
    while pos < n:
        tag, pos = _read_varint(payload, pos)
        field, wire = tag >> 3, tag & 7
        if wire != 2:
            if wire == 0:
                _, pos = _read_varint(payload, pos)
            elif wire == 1:
                pos += 8
            elif wire == 5:
                pos += 4
            continue
        length, pos = _read_varint(payload, pos)
        data = payload[pos:pos + length]
        pos += length
        if field == 1:                      # map entry
            k, v = None, None
            p2 = 0
            while p2 < len(data):
                t2, p2 = _read_varint(data, p2)
                f2, w2 = t2 >> 3, t2 & 7
                if w2 == 2:
                    l2, p2 = _read_varint(data, p2)
                    d2 = data[p2:p2 + l2]
                    p2 += l2
                    if f2 == 1:
                        k = d2.decode("utf-8", errors="replace")
                    elif f2 == 2:
                        v = d2
                elif w2 == 0:
                    _, p2 = _read_varint(data, p2)
                elif w2 == 1:
                    p2 += 8
                elif w2 == 5:
                    p2 += 4
            if k is not None and v is not None:
                feeds[k] = _decode_ltpc_message(v)
    return feeds

class MarketDataStreamerV3:
    """Background-thread WebSocket consumer for the V3 'ltpc' feed.
    Falls back to REST polling if websocket-client is missing."""

    def __init__(self, token, keys, on_quote, on_disconnect=None):
        self.token = token
        self.keys = list(keys)
        self.on_quote = on_quote
        self.on_disconnect = on_disconnect
        self._latest = {}
        self._thread = None
        self._running = False
        self._ws = None
        self.last_msg = 0.0

    # -- public ---------------------------------------------------------------
    def start(self):
        self._running = True
        try:
            import websocket  # noqa
        except ImportError:
            return False  # caller falls back to REST polling
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def latest(self, instrument_key):
        tok = instrument_key.split("|")[-1].lower()
        best, best_q = None, None
        for t, q in self._latest.items():
            if t.lower() == tok or t.endswith(tok):
                if best is None:
                    best, best_q = t, q
        return best_q

    # -- internals ------------------------------------------------------------
    def _run(self):
        import websocket

        def on_message(ws, message):
            try:
                feeds = _decode_feed_response(message)
                for tok, ltpc in feeds.items():
                    if "ltpc_msg" in ltpc:
                        inner = _decode_ltpc_message(ltpc["ltpc_msg"])
                        # inner fields: 1 ltp(double),2 vol,3 bid,4 ask,5 open,
                        #               6 high,7 low,8 close,9 oi
                        quote = {"last_price": inner.get(1),
                                 "bid": inner.get(3), "ask": inner.get(4),
                                 "oi": inner.get(9)}
                        self._latest[tok] = quote
                        self.last_msg = time.time()
                        self.on_quote(tok, quote)
            except Exception:
                pass

        def on_error(ws, error):
            pass

        def on_close(ws, code, msg):
            if self.on_disconnect:
                self.on_disconnect()

        while self._running:
            try:
                ws = websocket.WebSocketApp(
                    C.WS_URL_V3,
                    header={"Authorization": f"Bearer {self.token}",
                            "Accept": "application/json"},
                    on_message=on_message, on_error=on_error, on_close=on_close)
                self._ws = ws
                ws.run_forever()
            except Exception:
                pass
            if not self._running:
                break
            time.sleep(2.0)
            try:
                self._ws.send(json.dumps({
                    "guid": f"feeder-{int(time.time())}",
                    "method": "sub",
                    "data": {"instrumentKeys": self.keys, "mode": "ltpc"}}))
            except Exception:
                pass
            time.sleep(1.0)

# ============================================================================
# 5. DEMO PROVIDER (offline synthetic feed)
# ============================================================================
class DemoProvider:
    """Same interface as UpstoxClient but fully offline. Scenarios rotate every
    `phase_cycles` cycles: calm uptrend -> range chop -> panic selloff -> mild
    vol crush -> recovery. Uses the injected clock for timestamps."""

    def __init__(self, clock: VirtualClock, phase_cycles=3, seed=11):
        self.clock = clock
        self.phase_cycles = phase_cycles
        self.cycle = 0
        self.rng = random.Random(seed)
        self._oi_state = {}
        self._cum = 0.0
        self._monthly = None
        self._expiries = []
        self.lot_size = C.NIFTY_LOT_SIZE
        self._scen = None
        self._scen_i = 0
        self._spot_level = 24175.65   # smooth-running spot (drifts incrementally)

    # ------------------------------------------------------------- scenarios
    def scenarios(self):
        return [
            dict(name="calm uptrend", vix=11.2, vfwd=12.3, iv=17.5, rv=8.5, adx=31,
                 slope=0.09, dir=1, flow=1, spr_rel=[0.85, 0.90, 0.95], skew=2.2),
            dict(name="range chop", vix=12.0, vfwd=12.15, iv=12.4, rv=11.8, adx=17,
                 slope=0.01, dir=0, flow=0, spr_rel=[1.0, 1.0, 1.0], skew=1.4),
            dict(name="panic selloff", vix=17.5, vfwd=15.2, iv=18.6, rv=21.0, adx=29,
                 slope=-0.12, dir=-1, flow=-1, spr_rel=[1.20, 1.12, 1.05], skew=6.5),
            dict(name="mild vol crush", vix=13.2, vfwd=15.0, iv=14.0, rv=12.0, adx=18,
                 slope=0.01, dir=0, flow=0, spr_rel=[1.0, 1.0, 1.0], skew=1.6),
            dict(name="recovery", vix=13.0, vfwd=13.9, iv=14.2, rv=12.0, adx=27,
                 slope=0.07, dir=1, flow=1, spr_rel=[0.88, 0.92, 0.97], skew=2.8),
        ]

    def scenario(self):
        idx = (self.cycle // self.phase_cycles) % len(self.scenarios())
        self._scen = self.scenarios()[idx]
        self._scen_i = self.cycle % self.phase_cycles
        return self._scen, self._scen_i

    def tick(self):
        self.cycle += 1
        sc, _ = self.scenario()
        # incremental drift so scenario flips don't cause price gaps
        self._spot_level += sc["dir"] * 35
        if sc["dir"] == 0:
            # range scenarios mean-revert gently toward the base level
            self._spot_level += (24175.65 - self._spot_level) * 0.15

    # ----------------------------------------------------------- instruments
    def instruments(self):
        t = self.clock.now().date()
        # weeklies: Thursdays; monthly: a Thursday ~44-50 days out
        weeklies = []
        d = t + timedelta(days=(3 - t.weekday()) % 7)
        if d <= t:
            d += timedelta(days=7)
        while d <= t + timedelta(days=42):
            weeklies.append(d)
            d += timedelta(days=7)
        monthly = None
        for i in range(44, 52):
            cand = t + timedelta(days=i)
            if cand.weekday() == 3:
                monthly = cand
                break
        if monthly is None:
            monthly = t + timedelta(days=49)
        self._monthly = monthly.isoformat()
        expiries = [{"date": d.isoformat(), "weekly": True} for d in weeklies]
        expiries.append({"date": self._monthly, "weekly": False})
        expiries.sort(key=lambda e: e["date"])
        self._expiries = expiries
        return {"futs": [{"key": "NSE_FO|NIFTY|DEMOFUT", "symbol": "NIFTY FUT DEMO",
                          "expiry": self._monthly}],
                "expiries": expiries, "source": "demo",
                "lot_size": self.lot_size}

    # ---------------------------------------------------------------- quotes
    def _spot(self, sc):
        return self._spot_level + self.rng.uniform(-15, 15)

    def quotes(self, keys):
        sc, i = self.scenario()
        spot = self._spot(sc)
        prev_close = 24175.65
        out = {}
        for k in keys:
            ts = self.clock.now().isoformat()
            if k == C.KEY_NIFTY:
                out[k] = {"last_price": spot, "net_change": spot - prev_close,
                          "bid": spot - 0.05, "ask": spot + 0.05, "oi": 0,
                          "ohlc": {"close": prev_close}, "timestamp": ts}
            elif k == C.KEY_VIX:
                out[k] = {"last_price": sc["vix"], "net_change": -0.2,
                          "bid": sc["vix"] - 0.05, "ask": sc["vix"] + 0.05,
                          "oi": 0, "ohlc": {"close": sc["vix"] + 0.2}, "timestamp": ts}
            elif "FUT" in k or k.startswith("NSE_FO"):
                out[k] = {"last_price": spot * 1.0012, "net_change": 5,
                          "bid": spot * 1.001, "ask": spot * 1.0014, "oi": 0,
                          "ohlc": {"close": spot}, "timestamp": ts}
            else:
                out[k] = {"last_price": spot, "net_change": 0, "bid": spot - 0.05,
                          "ask": spot + 0.05, "oi": 0, "ohlc": {"close": spot},
                          "timestamp": ts}
        return out

    def ltp(self, keys):
        q = self.quotes(keys)
        return {k: {"last_price": v.get("last_price"), "timestamp": v.get("timestamp")}
                for k, v in q.items()}

    # ---------------------------------------------------------- option chain
    def _chain_rows(self, expiry):
        sc, i = self.scenario()
        spot = self._spot(sc)
        is_monthly = (self._monthly is not None and expiry >= self._monthly)
        step = 50.0
        lo = math.floor((spot - 2600) / step) * step
        exp_dt = datetime.combine(datetime.strptime(expiry, "%Y-%m-%d").date(),
                                  datetime.min.time())
        exp_dt = exp_dt.replace(hour=15, minute=30, tzinfo=IST)
        T = max((exp_dt - self.clock.now()).total_seconds(), 1.0) / (365 * 24 * 3600)
        rows = []
        self._cum += 18000.0 * sc["flow"]
        for n in range(105):
            k = lo + n * step
            m = (k - spot) / spot
            base_iv = sc["vfwd"] if is_monthly else sc["iv"]
            ivc = base_iv + 900 * m * m
            ivp = ivc + sc["skew"] * 0.25 + max(0.0, -m) * sc["skew"]
            d_c = bs_delta(spot, k, T, ivc, C.RISK_FREE, True)
            d_p = bs_delta(spot, k, T, ivp, C.RISK_FREE, False)
            ltp_c = max(0.05, bs_price(spot, k, T, ivc, C.RISK_FREE, True))
            ltp_p = max(0.05, bs_price(spot, k, T, ivp, C.RISK_FREE, False))
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
                "call_options": {"market_data": {"bid_price": round(ltp_c - 0.4, 2),
                                                 "ask_price": round(ltp_c + 0.4, 2),
                                                 "oi": prev_c, "prev_oi": prev_c - 26000 * sc["flow"],
                                                 "ltp": round(ltp_c, 2)},
                                 "option_greeks": {"delta": round(d_c, 4), "iv": round(ivc, 2)}},
                "put_options": {"market_data": {"bid_price": round(ltp_p - spr_put, 2),
                                                "ask_price": round(ltp_p + spr_put, 2),
                                                "oi": prev_p, "prev_oi": prev_p + 26000 * sc["flow"],
                                                "ltp": round(ltp_p, 2)},
                                "option_greeks": {"delta": round(d_p, 4), "iv": round(ivp, 2)}}})
        return rows

    def option_chain(self, underlying_key, expiry_iso):
        return normalize_chain(self._chain_rows(expiry_iso))

    def quote_for(self, instrument_key):
        """Compute current bid/ask/ltp for one instrument key."""
        parts = instrument_key.split("|")
        try:
            expiry = parts[2]
            strike = float(parts[3])
            otype = parts[4].upper()
        except (IndexError, ValueError):
            return None
        sc, i = self.scenario()
        spot = self._spot(sc)
        is_monthly = (self._monthly is not None and expiry >= self._monthly)
        exp_dt = datetime.combine(datetime.strptime(expiry, "%Y-%m-%d").date(),
                                  datetime.min.time()).replace(hour=15, minute=30, tzinfo=IST)
        T = max((exp_dt - self.clock.now()).total_seconds(), 1.0) / (365 * 24 * 3600)
        m = (strike - spot) / spot
        base_iv = sc["vfwd"] if is_monthly else sc["iv"]
        iv = base_iv + 900 * m * m
        if otype == "PE":
            iv += sc["skew"] * 0.25 + max(0.0, -m) * sc["skew"]
        is_call = otype == "CE"
        d = bs_delta(spot, strike, T, iv, C.RISK_FREE, is_call)
        ltp = max(0.05, bs_price(spot, strike, T, iv, C.RISK_FREE, is_call))
        base = max(60.0, 90000 * math.exp(-((strike - spot) / 300) ** 2))
        return {"last_price": ltp, "bid": max(0.05, ltp - 0.4), "ask": ltp + 0.4,
                "oi": base, "iv": iv, "delta": d, "spot": spot}

    # -------------------------------------------------------------- history
    def daily_candles(self, key, days=110):
        sc, _ = self.scenario()
        out, c = [], 24300.0
        rng = random.Random(3)
        t = self.clock.now()
        n = max(days, 220)
        for i in range(n, 0, -1):
            sig = sc["rv"] / 100 / math.sqrt(252)
            c = c * math.exp(rng.gauss(0.0002, sig if i <= 21 else 0.007))
            d = (t - timedelta(days=i)).replace(hour=0, minute=0, tzinfo=IST)
            out.append({"dt": d, "o": c, "h": c * 1.004, "l": c * 0.996, "c": c})
        return out

    def vix_series(self, days=30):
        sc, _ = self.scenario()
        rng = random.Random(7)
        t = self.clock.now()
        base = sc["vix"]
        series = []
        for i in range(days, 0, -1):
            v = base + rng.gauss(0, 0.35) * (1 - i / (days + 1))
            series.append(max(9.0, v))
        return series

    def hist_5m(self, key, days=C.HIST_DAYS_5M):
        sc, _ = self.scenario()
        spot = self._spot(sc)
        rng = random.Random(5)
        closes = [spot]
        drift = sc["slope"] / 100 * spot / 12
        for i in range(219, -1, -1):
            if sc["dir"] == 0:
                step = 3 * math.sin(i / 25.0) + rng.gauss(0, 26)  # low-ADX chop (~23)
            else:
                step = drift + rng.gauss(0, 4)
            closes.append(closes[-1] - step)
        closes.reverse()
        t0 = self.clock.now().replace(second=0, microsecond=0) - timedelta(minutes=5 * 220)
        return [{"dt": t0 + timedelta(minutes=5 * i), "o": c - 2, "h": c + 4, "l": c - 4,
                 "c": closes[i]} for i, c in enumerate(closes)]

    def intraday_5m(self, key):
        return []

    # ---------------------------------------------------------------- broker
    def margin_required(self, instruments):
        # virtual margin: simple premium-notional model, always 'available'
        total = 0.0
        for leg in instruments:
            q = self.quote_for(leg["instrument_key"])
            px = (q or {}).get("last_price", 0.0) or 0.0
            total += leg.get("quantity", 0) * px
        return max(total * 0.5, 50000.0)

# ============================================================================
# 6. BROKERS (paper / live)
# ============================================================================
class PaperBroker:
    """Simulated fills from the current feed quote. Slippage model (spec §7):
    core shorts 20 bps, hedges 100 bps + min 1 tick. Longs first, shorts
    second is enforced by the OrderExecutor, not here."""

    def __init__(self, feed, capital=C.INITIAL_CAPITAL, strict_limits=False):
        self.feed = feed
        self.cash = capital
        self.equity = capital
        self.positions = {}          # instrument_key -> {'qty','avg_price'}
        self.order_seq = 0
        self.strict_limits = strict_limits
        self._lock = threading.Lock()

    def margin_available(self):
        return self.cash

    def quote(self, instrument_key):
        if hasattr(self.feed, "quote_for"):
            q = self.feed.quote_for(instrument_key)
        else:
            q = self.feed.quotes([instrument_key]).get(instrument_key, {})
        return q or {}

    def place_order(self, leg: dict, slippage_bps: float = 20.0):
        """leg: {instrument_key, side: BUY|SELL, qty, order_type: LIMIT|MARKET,
        limit_price?, slippage_bps?} -> order dict with status/fill."""
        with self._lock:
            self.order_seq += 1
            oid = f"PAPER-{self.order_seq:06d}"
            q = self.quote(leg["instrument_key"])
            bid, ask, ltp = q.get("bid"), q.get("ask"), q.get("last_price")
            if not ltp:
                return {"order_id": oid, "status": "REJECTED",
                        "reject_reason": "no quote", "filled_qty": 0}
            if bid is None:
                bid = ltp * 0.998
            if ask is None:
                ask = ltp * 1.002
            tick = 0.05
            side = leg["side"]
            if side == "BUY":
                ideal = ask + tick
                fill = ideal * (1 + slippage_bps / 1e4) + tick
                if self.strict_limits and leg.get("order_type") == "LIMIT" \
                        and leg.get("limit_price") is not None and fill > leg["limit_price"]:
                    return {"order_id": oid, "status": "REJECTED",
                            "reject_reason": "limit not filled (slipped past)",
                            "filled_qty": 0, "avg_price": None}
            else:
                ideal = bid - tick
                fill = ideal * (1 - slippage_bps / 1e4) - tick
                if self.strict_limits and leg.get("order_type") == "LIMIT" \
                        and leg.get("limit_price") is not None and fill < leg["limit_price"]:
                    return {"order_id": oid, "status": "REJECTED",
                            "reject_reason": "limit not filled (slipped past)",
                            "filled_qty": 0, "avg_price": None}
            fill = round(fill, 2)
            qty = int(leg["qty"])
            # update book (net qty: negative = short)
            key = leg["instrument_key"]
            cur = self.positions.get(key, {"qty": 0, "avg_price": 0.0})
            if side == "BUY":
                if cur["qty"] >= 0:
                    new_qty = cur["qty"] + qty
                    new_avg = (cur["avg_price"] * cur["qty"] + fill * qty) / new_qty if new_qty else 0.0
                else:  # covering a short
                    new_qty = cur["qty"] + qty
                    if new_qty < 0:
                        new_avg = cur["avg_price"]
                    elif new_qty == 0:
                        new_avg = 0.0
                    else:
                        new_avg = fill  # flipped long at fill price
                self.cash -= fill * qty
            else:  # SELL
                if cur["qty"] <= 0:  # flat or adding to short
                    new_qty = cur["qty"] - qty
                    if new_qty < 0:
                        denom = abs(new_qty)
                        new_avg = ((cur["avg_price"] * abs(cur["qty"]) + fill * qty) / denom
                                   if cur["qty"] < 0 else fill)
                    else:
                        new_avg = 0.0
                else:  # reducing a long
                    new_qty = cur["qty"] - qty
                    if new_qty < 0:
                        new_avg = fill
                    elif new_qty == 0:
                        new_avg = 0.0
                    else:
                        new_avg = cur["avg_price"]
                self.cash += fill * qty
            if new_qty == 0:
                self.positions.pop(key, None)
            else:
                self.positions[key] = {"qty": new_qty, "avg_price": new_avg}
            return {"order_id": oid, "status": "COMPLETE",
                    "filled_qty": qty, "avg_price": fill, "ideal_price": ideal}

    def cancel_order(self, order_id):
        return True

    def order_history(self, order_id):
        return {"status": "COMPLETE"}

    def get_positions(self):
        return list(self.positions.items())

class LiveBroker:
    """Real Upstox orders. Only used when PAPER_TRADING_MODE=False."""

    def __init__(self, client: UpstoxClient):
        self.client = client
        self._lock = threading.Lock()

    def margin_available(self):
        m = self.client.margin_required([{"instrument_key": "NSE_INDEX|Nifty 50",
                                          "quantity": 1, "transaction_type": "SELL",
                                          "product": "I"}])
        return m

    def quote(self, instrument_key):
        q = self.client.quotes([instrument_key]).get(instrument_key, {})
        return q

    def place_order(self, leg: dict, slippage_bps: float = 0.0):
        oid = self.client.place_order(
            leg["instrument_key"], leg["side"], leg.get("order_type", "LIMIT"),
            leg["qty"], price=leg.get("limit_price"), product="I")
        return {"order_id": oid, "status": "PENDING", "filled_qty": 0}

    def cancel_order(self, order_id):
        return self.client.cancel_order(order_id)

    def order_history(self, order_id):
        return self.client.order_history(order_id)

    def get_positions(self):
        try:
            data = self.client._get(f"{C.UPSTOX_BASE_V2}/portfolio/short-term-positions")
            rows = (data.get("data") or [])
            return [(r.get("instrument_key"), {"qty": int(r.get("quantity_m2m") or r.get("quantity") or 0),
                                               "avg_price": fnum(r.get("average_price"))})
                    for r in rows if int(r.get("quantity_m2m") or r.get("quantity") or 0) != 0]
        except UpstoxError:
            return []

# ============================================================================
# 7. FEED FACADE
# ============================================================================
class Feed:
    """Unified interface over UpstoxClient or DemoProvider + streamer."""
    def __init__(self, client, streamer=None):
        self.client = client
        self.streamer = streamer

    def quotes(self, keys):
        return self.client.quotes(keys)

    def ltp(self, keys):
        return self.client.ltp(keys)

    def option_chain(self, underlying_key, expiry_iso):
        return self.client.option_chain(underlying_key, expiry_iso)

    def daily_candles(self, key, days=110):
        return self.client.daily_candles(key, days)

    def hist_5m(self, key, days=C.HIST_DAYS_5M):
        return self.client.hist_5m(key, days)

    def intraday_5m(self, key):
        return self.client.intraday_5m(key)

    def vix_series(self, days=30):
        if hasattr(self.client, "vix_series"):
            return self.client.vix_series(days)
        return []

    def quote_for(self, instrument_key):
        if hasattr(self.client, "quote_for"):
            return self.client.quote_for(instrument_key)
        return self.quotes([instrument_key]).get(instrument_key, {})

    def tick(self):
        if hasattr(self.client, "tick"):
            self.client.tick()

# =====================================================================
# PART 2 — ORDER EXECUTOR (merged from order_executor.py)
# =====================================================================

import json
import time
import uuid
from datetime import datetime

EXIT_PROFIT = "PROFIT_TARGET"
EXIT_STOP = "STOP_LOSS"
EXIT_TIME = "TIME_EXIT"
EXIT_REGIME = "REGIME_CHANGE"
EXIT_MANUAL = "MANUAL"
EXIT_EXPIRY = "EXPIRY_SQUAREOFF"
EXIT_DAILY_LOSS = "DAILY_LOSS"
EXIT_KILL = "KILL_SWITCH"

def est_transaction_costs(legs):
    """Estimated ₹ costs: brokerage + STT + exchange + SEBI + stamp + GST."""
    brokerage = C.COST_BROKERAGE_OPTION * max(len(legs), 1)
    prem = sum(l.get("premium_value", 0.0) for l in legs)
    stt = prem * C.COST_STT_OPTION_SELL_PCT
    exch = prem * C.COST_EXCHANGE_PCT
    sebi = prem * C.COST_SEBI_PCT
    stamp = prem * C.COST_STAMP_PCT
    gst = (brokerage + exch + sebi) * C.COST_GST_PCT
    return brokerage + stt + exch + sebi + stamp + gst

class OrderExecutor:
    def __init__(self, ctx):
        self.ctx = ctx                      # feed, broker, clock, state, loggers, risk
        self.open_trades = {}               # trade_id -> trade dict (in-memory mirror)
        self._load_open_trades()

    # ------------------------------------------------------------ persistence
    def _load_open_trades(self):
        for pos in self.ctx.state.get_open_positions():
            tid = pos["trade_id"]
            trade = {
                "trade_id": tid,
                "strategy_name": pos["strategy_name"],
                "regime_at_entry": pos["regime_at_entry"],
                "entry_ts": pos["entry_ts"],
                "entry_spot": pos["entry_spot"],
                "entry_vix": pos["entry_vix"],
                "expiry_date": pos["expiry_date"],
                "days_to_expiry": pos["days_to_expiry"],
                "lot_size": pos["lot_size"] or C.NIFTY_LOT_SIZE,
                "legs": json.loads(pos["legs_json"] or "[]"),
                "meta": json.loads(pos["meta_json"] or "{}"),
                "status": pos["status"],
                "scores": {},
            }
            self.open_trades[tid] = trade

    def _save_position(self, trade):
        self.ctx.state.upsert_position({
            "trade_id": trade["trade_id"],
            "strategy_name": trade["strategy_name"],
            "regime_at_entry": trade["regime_at_entry"],
            "status": trade.get("status", "OPEN"),
            "legs_json": json.dumps(trade["legs"]),
            "meta_json": json.dumps(trade.get("meta", {})),
            "entry_ts": trade["entry_ts"],
            "entry_spot": trade.get("entry_spot"),
            "entry_vix": trade.get("entry_vix"),
            "expiry_date": trade.get("expiry_date"),
            "days_to_expiry": trade.get("days_to_expiry"),
            "lot_size": trade.get("lot_size", C.NIFTY_LOT_SIZE),
        })

    def _checkpoint(self, reason="fill"):
        snap = {"spot": self.ctx.last_spot, "vix": self.ctx.last_vix,
                "reason": reason, "open_trades": list(self.open_trades)}
        self.ctx.state.checkpoint(snap, reason=reason)

    # ------------------------------------------------------------- entry
    def execute_entry(self, plan, env, regime, scores=None):
        """Execute a TradePlan. Returns trade_id or None on abort."""
        if not self.ctx.risk.margin_ok(plan):
            self.ctx.loggers.audit("ENTRY_ABORTED", strategy=plan.strategy_name, reason="margin")
            return None

        trade_id = str(uuid.uuid4())
        trade = {
            "trade_id": trade_id,
            "strategy_name": plan.strategy_name,
            "regime_at_entry": regime,
            "entry_ts": self.ctx.clock.now().isoformat(),
            "entry_spot": env.spot,
            "entry_vix": env.vix,
            "expiry_date": plan.expiry_date,
            "days_to_expiry": plan.days_to_expiry,
            "lot_size": plan.lot_size,
            "legs": [{"instrument_key": l.instrument_key, "side": l.side,
                      "qty": l.qty, "kind": l.kind, "role": l.role,
                      "status": "PENDING", "order_id": None, "fill_price": None}
                     for l in plan.legs],
            "meta": {**dict(plan.meta), "max_risk": plan.max_risk_points,
                     "risk_rupees": plan.max_risk_points * plan.lot_size},
            "meta_json": json.dumps({**plan.meta, "max_risk": plan.max_risk_points,
                                     "risk_rupees": plan.max_risk_points * plan.lot_size}),
            "status": "OPEN",
            "scores": scores or {},
            "exit_rules": plan.exit_rules,
        }
        self.open_trades[trade_id] = trade
        self._save_position(trade)
        self.ctx.loggers.audit("ENTRY_STARTED", trade_id=trade_id,
                               strategy=plan.strategy_name, regime=regime,
                               legs=len(plan.legs), spot=env.spot, vix=env.vix)

        # ---- sequence: longs first, shorts second ---------------------------
        long_legs = [l for l in plan.legs if l.kind == "long"]
        short_legs = [l for l in plan.legs if l.kind == "short"]
        ordered = long_legs + short_legs

        deadline = time.monotonic() + C.ORDER_FILL_TIMEOUT
        for leg in ordered:
            if time.monotonic() > deadline:
                self._abort_entry(trade_id, "global timeout")
                return None
            order = self._place_one(leg, trade_id)
            if order is None:
                self._abort_entry(trade_id, "order placement failed")
                return None
            if order["status"] in ("REJECTED",):
                self._abort_entry(trade_id, f"rejected: {order.get('reject_reason')}")
                return None
            if order.get("filled_qty", 0) < leg.qty:
                self._abort_entry(trade_id, f"partial fill {order.get('filled_qty')}/{leg.qty}")
                return None
            self._mark_filled(trade, leg, order)
            self._checkpoint("fill")

        trade["status"] = "OPEN"
        self._save_position(trade)
        gross_credit = sum(l.get("fill_price", 0) * l["qty"] for l in trade["legs"] if l["side"] == "SELL")
        debit = sum(l.get("fill_price", 0) * l["qty"] for l in trade["legs"] if l["side"] == "BUY")
        if trade["strategy_name"] in self.CREDIT_STRATEGIES:
            credit = gross_credit - debit          # net credit received
        else:
            credit = gross_credit
        trade["meta"]["entry_credit"] = round(credit, 2)
        trade["meta"]["entry_debit"] = round(debit, 2)
        self._save_position(trade)
        self.ctx.loggers.audit("ENTRY_COMPLETE", trade_id=trade_id,
                               strategy=plan.strategy_name, credit=round(credit, 2),
                               debit=round(debit, 2))
        return trade_id

    def _place_one(self, leg, trade_id):
        """Place one leg with proper order type/price. -> order dict."""
        q = self.ctx.broker.quote(leg.instrument_key)
        bid, ask = q.get("bid"), q.get("ask")
        tick = 0.05
        order_type = "LIMIT"
        limit_price = None
        if leg.side == "BUY":
            limit_price = (ask + tick) if ask is not None else None
        else:
            limit_price = (bid - tick) if bid is not None else None
        req = {"instrument_key": leg.instrument_key, "side": leg.side,
               "qty": leg.qty, "order_type": order_type,
               "limit_price": limit_price, "slippage_bps": leg.slippage_bps}
        try:
            order = self.ctx.broker.place_order(req, slippage_bps=leg.slippage_bps)
        except Exception as e:
            self.ctx.loggers.audit("ORDER_ERROR", trade_id=trade_id, leg=leg.instrument_key,
                                   error=str(e))
            return None
        # live-mode polling
        if order["status"] == "PENDING":
            order = self._await_fill(order["order_id"], leg, trade_id)
        order["leg_key"] = leg.instrument_key
        return order

    def _await_fill(self, order_id, leg, trade_id):
        deadline = time.monotonic() + C.CORE_FILL_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(0.2)
            try:
                st = self.ctx.broker.order_history(order_id)
            except Exception as e:
                self.ctx.loggers.audit("ORDER_POLL_ERROR", order_id=order_id, error=str(e))
                continue
            if st["status"] in ("COMPLETE", "TRADED"):
                return {"order_id": order_id, "status": "COMPLETE",
                        "filled_qty": st.get("filled_qty", leg.qty),
                        "avg_price": st.get("avg_price")}
            if st["status"] in ("REJECTED", "CANCELLED"):
                return {"order_id": order_id, "status": "REJECTED",
                        "reject_reason": st.get("reject_reason")}
        return {"order_id": order_id, "status": "REJECTED", "reject_reason": "fill timeout"}

    def _mark_filled(self, trade, leg, order):
        for l in trade["legs"]:
            if l["instrument_key"] == leg.instrument_key:
                l["status"] = "COMPLETE"
                l["order_id"] = order.get("order_id")
                l["fill_price"] = order.get("avg_price")
        self.ctx.state.upsert_order({
            "order_id": order.get("order_id") or str(uuid.uuid4()),
            "trade_id": trade["trade_id"], "leg_key": leg.instrument_key,
            "instrument_key": leg.instrument_key, "side": leg.side,
            "qty": leg.qty, "filled_qty": order.get("filled_qty", leg.qty),
            "avg_price": order.get("avg_price"),
            "status": order.get("status", "COMPLETE"),
            "placed_ts": datetime.now().isoformat(), "updated_ts": datetime.now().isoformat()})

    def _abort_entry(self, trade_id, reason):
        trade = self.open_trades.pop(trade_id, None)
        if trade:
            # cancel any pending legs (best effort)
            for l in trade["legs"]:
                if l["order_id"] and l["status"] in ("PENDING",):
                    try:
                        self.ctx.broker.cancel_order(l["order_id"])
                    except Exception:
                        pass
            self.ctx.state.conn.execute("DELETE FROM positions WHERE trade_id=?", (trade_id,))
            self.ctx.state.conn.commit()
            # restore any fills already taken (paper broker holds them)
            for l in trade["legs"]:
                if l.get("fill_price") is not None and l["status"] == "COMPLETE":
                    self._reverse_fill(l)
        self.ctx.loggers.audit("ENTRY_ABORTED", trade_id=trade_id, reason=reason)

    def _reverse_fill(self, leg):
        """Undo a completed leg (abort path)."""
        q = self.ctx.broker.quote(leg["instrument_key"])
        px = q.get("last_price") or leg.get("fill_price") or 0
        side = "SELL" if leg["side"] == "BUY" else "BUY"
        try:
            self.ctx.broker.place_order({"instrument_key": leg["instrument_key"],
                                         "side": side, "qty": leg["qty"],
                                         "order_type": "MARKET"}, slippage_bps=100.0)
        except Exception:
            pass
        leg["status"] = "REVERSED"

    # ------------------------------------------------------------- monitor
    def manage_positions(self, res, env, now):
        """Run per-cycle checks: stops, profit targets, time exits, expiry
        square-off, daily-loss breaker. Returns list of closed trade ids."""
        closed = []
        for tid in list(self.open_trades):
            trade = self.open_trades.get(tid)
            if not trade or trade.get("status") != "OPEN":
                continue
            reason = self._check_trade(trade, res, env, now)
            if reason:
                self.close_trade(tid, reason)
                closed.append(tid)
        return closed

    def _check_trade(self, trade, res, env, now):
        rules = trade.get("exit_rules", {})
        spot = res.spot
        # ---- static spot stop (short straddle) ----------------------------
        if rules.get("static_stop_pct"):
            entry_spot = trade.get("entry_spot") or spot
            up = entry_spot * (1 + rules["static_stop_pct"])
            dn = entry_spot * (1 - rules["static_stop_pct"])
            if spot >= up or spot <= dn:
                return EXIT_STOP
        # ---- premium-based stop / profit target ---------------------------
        prem = self._current_premium(trade)
        entry_credit = trade["meta"].get("entry_credit", 0.0)
        entry_debit = trade["meta"].get("entry_debit", 0.0)
        is_credit = trade["strategy_name"] in ("ATM_STRADDLE_45D", "WIDE_IRON_CONDOR",
                                               "BULL_PUT_BEAR_CALL", "RATIO_SPREAD_1x2")
        if is_credit and entry_credit > 0:
            if prem <= entry_credit * rules.get("profit_target_pct", C.PROFIT_TARGET_PCT):
                return EXIT_PROFIT
            be = trade.get("meta", {}).get("be_stop")
            if be:
                if prem >= entry_credit:
                    return EXIT_STOP            # breakeven stop (Rule D)
            elif prem >= entry_credit * (1 + self.ctx.risk.stop_pct_for_vix(trade.get("entry_vix") or 15)):
                return EXIT_STOP                # premium rose past stop % of credit
        else:
            if entry_debit > 0:
                mult = rules.get("profit_target_mult")
                if mult and prem >= entry_debit * mult:
                    return EXIT_PROFIT
                mult = rules.get("stop_loss_mult")
                if mult and prem <= entry_debit * mult:
                    return EXIT_STOP
        # ---- time exits -----------------------------------------------------
        dte = (datetime.strptime(trade["expiry_date"], "%Y-%m-%d").date() - now.date()).days \
            if trade.get("expiry_date") else 0
        if rules.get("time_exit_days") and dte <= rules["time_exit_days"]:
            return EXIT_TIME
        if rules.get("hold_days"):
            entry_dt = datetime.fromisoformat(trade["entry_ts"])
            if (now - entry_dt).days >= rules["hold_days"] and now.time() >= C.TIME_EXIT_NORMAL:
                return EXIT_TIME
        # ---- expiry day square-off -------------------------------------------
        if C.is_expiry_day(now.date()) and now.time() >= C.EXPIRY_SQUARE_OFF:
            return EXIT_EXPIRY
        return None

    CREDIT_STRATEGIES = ("ATM_STRADDLE_45D", "WIDE_IRON_CONDOR",
                         "BULL_PUT_BEAR_CALL", "RATIO_SPREAD_1x2")

    def _current_premium(self, trade):
        """Current position value in premium points*lots.

        Credit strategies (sold premium): remaining liability = short value
        minus long value.  Debit strategies (bought premium): position value =
        sum of long (+ short) leg values.  Closed legs are excluded."""
        short_v = long_v = 0.0
        for l in trade["legs"]:
            if l.get("fill_price") is None or l.get("status") in ("CLOSED", "CLOSED_PARTIAL"):
                continue
            q = self.ctx.broker.quote(l["instrument_key"])
            px = q.get("last_price") or l["fill_price"]
            if l["side"] == "SELL":
                short_v += px * l["qty"]
            else:
                long_v += px * l["qty"]
        if trade["strategy_name"] in self.CREDIT_STRATEGIES:
            return short_v - long_v
        return short_v + long_v

    # ------------------------------------------------------------- exits
    def close_trade(self, trade_id, reason, market=True):
        trade = self.open_trades.get(trade_id)
        if not trade:
            return None
        # shorts first (buy back), then longs (sell)
        ordered = [l for l in trade["legs"] if l["side"] == "SELL"] + \
                  [l for l in trade["legs"] if l["side"] == "BUY"]
        realized = 0.0
        ideal = 0.0
        slip = 0.0
        for l in ordered:
            if l.get("fill_price") is None or l.get("status") in ("CLOSED", "CLOSED_PARTIAL"):
                continue
            q = self.ctx.broker.quote(l["instrument_key"])
            bid, ask = q.get("bid"), q.get("ask")
            close_side = "BUY" if l["side"] == "SELL" else "SELL"
            close_px = None
            if market or bid is None or ask is None:
                close_px = q.get("last_price") or l["fill_price"]
            else:
                close_px = (ask + 0.05) if close_side == "BUY" else (bid - 0.05)
            try:
                order = self.ctx.broker.place_order(
                    {"instrument_key": l["instrument_key"], "side": close_side,
                     "qty": l["qty"], "order_type": "MARKET" if market else "LIMIT",
                     "limit_price": None}, slippage_bps=100.0)
                avg = order.get("avg_price") or close_px
            except Exception:
                avg = close_px
            diff = (avg - l["fill_price"]) * (1 if l["side"] == "SELL" else -1)
            realized += diff * l["qty"]
            ideal += ((close_px if close_side == "SELL" else close_px) - l["fill_price"]) \
                * (1 if l["side"] == "SELL" else -1) * l["qty"]
            l["status"] = "CLOSED"
            l["close_price"] = avg
        slip = realized - ideal
        pnl_points = realized / (trade["lot_size"] or C.NIFTY_LOT_SIZE)
        costs = est_transaction_costs([{**l, "premium_value": (l.get("fill_price") or 0) * l["qty"]}
                                       for l in trade["legs"]])
        pnl_rupees = realized * (trade["lot_size"] or C.NIFTY_LOT_SIZE) - costs
        self.ctx.risk.add_day_pnl(pnl_points)
        self._write_trade_row(trade, reason, pnl_rupees, pnl_points, slip, costs)
        self.ctx.state.conn.execute(
            "UPDATE positions SET status=? WHERE trade_id=?", ("CLOSED", trade_id))
        self.ctx.state.conn.commit()
        self._checkpoint("close")
        self.ctx.loggers.audit("TRADE_CLOSED", trade_id=trade_id, reason=reason,
                               pnl_points=round(pnl_points, 2), pnl_rupees=round(pnl_rupees, 2))
        self.open_trades.pop(trade_id, None)
        return pnl_points

    def _write_trade_row(self, trade, reason, pnl_rupees, pnl_points, slip, costs):
        sc = trade.get("scores") or {}
        exit_ts = self.ctx.clock.now()
        try:
            entry_dt = datetime.fromisoformat(trade["entry_ts"])
        except (TypeError, ValueError):
            entry_dt = exit_ts
        self.ctx.loggers.trade_csv.append({
            "trade_id": trade["trade_id"],
            "strategy_name": trade["strategy_name"],
            "regime_at_entry": trade["regime_at_entry"],
            "regime_at_exit": self.ctx.last_regime or "",
            "entry_timestamp": trade["entry_ts"],
            "exit_timestamp": exit_ts.isoformat(),
            "holding_days": max((exit_ts - entry_dt).total_seconds(), 0.0) / 86400.0,
            "entry_spot": trade.get("entry_spot"),
            "exit_spot": self.ctx.last_spot,
            "entry_vix": trade.get("entry_vix"),
            "exit_vix": self.ctx.last_vix,
            "legs_summary": json.dumps([{"instrument": l["instrument_key"],
                                         "side": l["side"], "qty": l["qty"],
                                         "entry_price": l.get("fill_price"),
                                         "exit_price": l.get("close_price")}
                                        for l in trade["legs"]]),
            "total_credit_received": trade["meta"].get("entry_credit", 0.0),
            "total_debit_paid": trade["meta"].get("entry_debit", 0.0),
            "net_premium": (trade["meta"].get("entry_credit", 0.0) -
                            trade["meta"].get("entry_debit", 0.0)),
            "max_risk": trade["meta"].get("max_risk", 0.0),
            "realized_pnl": round(pnl_points, 2),
            "realized_pnl_percent": round(pnl_rupees / self.ctx.risk.capital * 100.0, 4),
            "exit_reason": reason,
            "slippage_total_points": round(slip / (trade["lot_size"] or C.NIFTY_LOT_SIZE), 2),
            "transaction_costs": round(costs, 2),
            "composite_score_at_entry": sc.get("composite"),
            "vol_score": sc.get("vol"), "edge_score": sc.get("edge"),
            "trend_score": sc.get("trend"), "flow_score": sc.get("flow"),
            "days_to_expiry_at_entry": trade.get("days_to_expiry"),
            "expiry_date": trade.get("expiry_date"),
            "paper_trade": C.PAPER_TRADING_MODE,
        })

    # ------------------------------------------------------- transitions A-E
    def apply_transitions(self, old_regime, new_regime, env):
        """Rules A-E (spec §6). Called when the confirmed regime changes."""
        if self.ctx.clock.now().time() >= C.TIME_LAST_IGNORE:
            self.ctx.loggers.audit("TRANSITION_SKIPPED", reason="after 14:45 IST (Rule E)")
            return
        shorts = [l for l in (ll for t in self.open_trades.values()
                              for ll in t["legs"]) if l["side"] == "SELL"]
        if new_regime == C.REGIME_STRONG_BUY:
            # Rule A: flatten ALL shorts immediately (market), keep longs 1 day.
            # For fully-short trades close the whole trade (REGIME_CHANGE exit);
            # for mixed trades flatten the short legs and hold the longs.
            for tid in list(self.open_trades):
                t = self.open_trades.get(tid)
                if not t:
                    continue
                if any(l["side"] == "SELL" for l in t["legs"]):
                    if all(l["side"] == "SELL" for l in t["legs"]):
                        self.close_trade(tid, EXIT_REGIME, market=True)
                    else:
                        self._flatten_shorts(tid)
                    self.ctx.loggers.audit("RULE_A", trade_id=tid)
            return
        if old_regime == C.REGIME_STRONG_SELL and new_regime == C.REGIME_MILD_SELL:
            # Rule B: close 50% of shorts (limit), tighten remaining to BE
            for tid in list(self.open_trades):
                t = self.open_trades.get(tid)
                if not t:
                    continue
                self._close_half_shorts(tid)
                self.ctx.loggers.audit("RULE_B", trade_id=tid)
            return
        if old_regime == C.REGIME_MILD_SELL and new_regime == C.REGIME_NEUTRAL:
            for tid in list(self.open_trades):
                t = self.open_trades.get(tid)
                if not t:
                    continue
                self._close_half_shorts(tid)
                self.ctx.loggers.audit("RULE_B", trade_id=tid)
            return
        if old_regime == C.REGIME_MILD_SELL and new_regime == C.REGIME_BUY:
            # Rule C: don't close shorts — buy 0.20-delta OTM puts = 100% notional
            self._hedge_shorts_with_puts(env)
            self.ctx.loggers.audit("RULE_C", env=env.regime)
            return
        if new_regime in (C.REGIME_NEUTRAL, C.REGIME_BUY) and \
                old_regime in (C.REGIME_STRONG_SELL, C.REGIME_MILD_SELL):
            # Rule D-ish: tighten stops to breakeven on remaining shorts
            self._breakeven_stops()
            return
        if old_regime == new_regime:
            # Rule D: same broad category -> breakeven stops
            self._breakeven_stops()

    def _flatten_shorts(self, tid):
        trade = self.open_trades[tid]
        for l in trade["legs"]:
            if l["side"] == "SELL" and l.get("fill_price") is not None:
                try:
                    self.ctx.broker.place_order(
                        {"instrument_key": l["instrument_key"], "side": "BUY",
                         "qty": l["qty"], "order_type": "MARKET"}, slippage_bps=100.0)
                    l["status"] = "CLOSED_PARTIAL"
                except Exception:
                    pass

    def _close_half_shorts(self, tid):
        trade = self.open_trades[tid]
        for l in trade["legs"]:
            if l["side"] == "SELL" and l.get("fill_price") is not None:
                half = max(1, l["qty"] // 2)
                if l["qty"] - half > 0:
                    try:
                        self.ctx.broker.place_order(
                            {"instrument_key": l["instrument_key"], "side": "BUY",
                             "qty": half, "order_type": "LIMIT",
                             "limit_price": self.ctx.broker.quote(l["instrument_key"]).get("bid")},
                            slippage_bps=100.0)
                        l["qty"] -= half
                    except Exception:
                        pass

    def _hedge_shorts_with_puts(self, env):
        """Rule C: buy 0.20-delta OTM puts covering 100% of short notional."""
        shorts = [l for t in self.open_trades.values() for l in t["legs"]
                  if l["side"] == "SELL"]
        if not shorts:
            return
        put_strike = env.strike_by_delta("PE", 0.20)
        if not put_strike:
            return
        total_qty = sum(l["qty"] for l in shorts)
        key = f"NSE_FO|NIFTY|{env.near_expiry}|{put_strike:.0f}|PE"
        q = self.ctx.broker.quote(key)
        if not q.get("last_price"):
            return
        try:
            self.ctx.broker.place_order({"instrument_key": key, "side": "BUY",
                                         "qty": total_qty, "order_type": "LIMIT",
                                         "limit_price": q.get("ask") + 0.05},
                                        slippage_bps=100.0)
        except Exception:
            pass

    def _breakeven_stops(self):
        # In paper mode stops are checked against entry credit; mark trades
        # so premium-stop threshold moves to the entry value (breakeven).
        for t in self.open_trades.values():
            t["meta"]["be_stop"] = True

    # ------------------------------------------------------------- emergency
    def square_off_all(self, reason=EXIT_MANUAL):
        for tid in list(self.open_trades):
            self.close_trade(tid, reason, market=True)

    def open_short_count(self):
        return sum(1 for t in self.open_trades.values()
                   for l in t["legs"] if l["side"] == "SELL")

# =====================================================================
# PART 3 — RISK MANAGER (merged from risk_manager.py)
# =====================================================================

import math

class RiskManager:
    def __init__(self, broker, state, loggers=None, capital=C.INITIAL_CAPITAL):
        self.broker = broker
        self.state = state
        self.loggers = loggers
        self.capital = capital
        self.day_pnl_points = 0.0
        self.day_key = None

    # ------------------------------------------------------------------ days
    def _roll_day(self, clock):
        day = clock.now().date().isoformat()
        if self.day_key != day:
            self.day_key = day
            self.day_pnl_points = 0.0

    def add_day_pnl(self, points):
        self.day_pnl_points += points

    def daily_loss_breached(self) -> bool:
        return self.day_pnl_points <= C.MAX_DAILY_LOSS

    # ----------------------------------------------------------------- stops
    def stop_pct_for_vix(self, vix):
        """VIX-scaled stop percent on premium: base 30% @ VIX 14, clamp 18-40%."""
        pct = C.SL_BASE_PERCENT * (C.SL_REFERENCE_VIX / max(vix, 5.0))
        return max(C.SL_MIN_PERCENT, min(C.SL_MAX_PERCENT, pct))

    # ---------------------------------------------------------------- sizing
    def suggest_lots(self, plan, existing_risk_rupees=0.0):
        """Lots = floor(2% capital / (max_risk_points_per_lot * lot_size)),
        clamped by the 4% combined-risk cap. Never below 1 if plan is viable.
        `plan` must be built with lots=1 so max_risk_points is per-lot."""
        per_lot_risk_rupees = max(plan.max_risk_points, 1.0) * plan.lot_size
        risk_cap = C.MAX_RISK_PER_TRADE * self.capital
        lots = math.floor(risk_cap / per_lot_risk_rupees)
        # undefined-risk (ratio spread) trades capped at 1%
        if plan.strategy_name in ("RATIO_SPREAD_1x2",):
            lots = min(lots, math.floor(0.01 * self.capital / per_lot_risk_rupees))
        combined_cap = C.MAX_COMBINED_RISK * self.capital
        while lots > 0 and existing_risk_rupees + lots * per_lot_risk_rupees > combined_cap:
            lots -= 1
        return max(lots, 1)

    # ----------------------------------------------------------- margin check
    def margin_ok(self, plan) -> bool:
        # rough premium-notional margin requirement (paper model)
        notional = 0.0
        for leg in plan.legs:
            q = self.broker.quote(leg.instrument_key)
            notional += leg.qty * (q.get("last_price") or 0.0)
        required = max(notional * 0.5, 50_000.0)
        avail = self.broker.margin_available()
        ok = avail is None or required <= avail
        if self.loggers and not ok:
            self.loggers.audit("margin_insufficient", required=required, available=avail,
                               strategy=plan.strategy_name)
        return ok

    # --------------------------------------------------------- gamma exposure
    @staticmethod
    def gamma_ratio(open_shorts, spot, capital=C.INITIAL_CAPITAL):
        """Proxy for gamma exposure vs the combined-risk limit: dollar delta of
        short book / (4% of capital). 1.0 == limit."""
        dollar_delta = sum(abs(s.get("delta", 0.0) or 0.0) * s.get("qty", 0) * spot
                           for s in open_shorts)
        limit = C.MAX_COMBINED_RISK * capital
        return dollar_delta / limit if limit else 0.0

    # ------------------------------------------------------------- kill switch
    def kill_switch(self, reason):
        if self.loggers:
            self.loggers.audit("KILL_SWITCH", reason=reason)
