#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
  UPSTOX VALUE SCREENER  -  3-model fair value + financial health + live signals
==============================================================================
  Phase 1  Auth, instrument master, fundamentals, 5Y candles
  Phase 2  DCF  /  Peer multiples  /  Historical P/E band  ->  Composite FV
  Phase 3  Financial Health Score (0-5, sector-relative percentiles, 5 pillars)
  Phase 4  Live scan (quotes polling) + 4-rule ACTIONABLE BUY filter
  Phase 5  Portfolio check, position sizing, GTT entry/target/stop (DRY-RUN by
           default - orders are only sent with --execute AND typed confirmation)

  Install :  pip install requests numpy rich yfinance
  Run     :  python upstox_value_screener.py              (one full pass)
             python upstox_value_screener.py --live       (then poll during market hours)
             python upstox_value_screener.py --daemon     (8:30 IST pre-market run + live loop, daily)
             python upstox_value_screener.py --detail TCS,INFY   (deep-dive cards)

  env.txt (same folder):
      UPSTOX_ACCESS_TOKEN=...
      UPSTOX_API_TOKEN=...        (API key / client_id)
      UPSTOX_API_SECRET=...
      UPSTOX_REDIRECT_URI=http://127.0.0.1:5000/     (optional, must match your app)

  PRODUCTION CONTROLS (v2): tight-peer valuation only, implausible/outlier models are DROPPED
  (never inflated), >=2 models must independently show upside, absolute quality + data-integrity
  gates, risk-based position sizing, daily order/capital limits, cool-downs, duplicate-order
  ledger, KILL_SWITCH file, pre-order price re-check, audit log (logs/), blocklist.txt.
  Orders are only possible with --execute, inside 09:30-15:10 IST, after typed confirmation.

  NOTE: Upstox has no fundamentals endpoint. Fundamentals (Phase 1.3) come from
  Yahoo Finance via `yfinance` (NSE tickers) and are cached locally.
==============================================================================
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import math
import os
import pickle
import sys
import threading
import uuid
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from urllib.parse import parse_qs, quote, urlencode, urlparse

import numpy as np
import requests

try:
    from rich import box
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.progress import (BarColumn, MofNCompleteColumn, Progress,
                               SpinnerColumn, TextColumn, TimeElapsedColumn)
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency:  pip install rich requests numpy yfinance")

# =============================================================================
#  CONFIGURATION
# =============================================================================
IST = timezone(timedelta(hours=5, minutes=30))
BASE = "https://api.upstox.com"
INSTRUMENT_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
NIFTY_KEY = "NSE_INDEX|Nifty 50"
CR = 1e7
CACHE = Path(".screener_cache")
CACHE.mkdir(exist_ok=True)

UNIVERSE = """360ONE, ABB, ABCAPITAL, ADANIENSOL, ADANIENT, ADANIGREEN, ADANIPORTS, ADANIPOWER, ALKEM, AMBER, AMBUJACEM, ANANDRATHI, ANGELONE, APLAPOLLO, APOLLOHOSP, ASHOKLEY, ASIANPAINT, ASTRAL, ATHERENERG, AUBANK, AUROPHARMA, AXISBANK, BAJAJ-AUTO, BAJAJFINSV, BAJAJHLDNG, BAJFINANCE, BANDHANBNK, BANKBARODA, BANKINDIA, BDL, BEL, BHARATFORG, BHARTIARTL, BHEL, BIOCON, BLUESTARCO, BOSCHLTD, BPCL, BRITANNIA, BSE, CAMS, CANBK, CDSL, CGPOWER, CHOLAFIN, CIPLA, COALINDIA, COCHINSHIP, COFORGE, COLPAL, CONCOR, CROMPTON, CUMMINSIND, DABUR, DELHIVERY, DIVISLAB, DIXON, DLF, DMART, DRREDDY, EICHERMOT, ENRIN, ETERNAL, FEDERALBNK, FORCEMOT, FORTIS, GAIL, GLENMARK, GMRAIRPORT, GODFRYPHLP, GODREJCP, GODREJPROP, GRASIM, GVT&D, HAL, HAVELLS, HCLTECH, HDFCAMC, HDFCBANK, HDFCLIFE, HEROMOTOCO, HINDALCO, HINDPETRO, HINDUNILVR, HINDZINC, HYUNDAI, ICICIBANK, ICICIGI, ICICIPRULI, IDEA, IDFCFIRSTB, IEX, INDHOTEL, INDIANB, INDIGO, INDUSINDBK, INDUSTOWER, INFY, INOXWIND, IOC, IREDA, IRFC, ITC, JINDALSTEL, JIOFIN, JSWENERGY, JSWSTEEL, JUBLFOOD, KALYANKJIL, KAYNES, KEI, KFINTECH, KOTAKBANK, KPITTECH, LAURUSLABS, LICHSGFIN, LICI, LODHA, LT, LTF, LTM, LUPIN, MAHABANK, MANAPPURAM, MANKIND, MARICO, MARUTI, MAXHEALTH, MAZDOCK, MCX, MFSL, MOTHERSON, MOTILALOFS, MPHASIS, MUTHOOTFIN, NAM-INDIA, NATIONALUM, NAUKRI, NBCC, NESTLEIND, NHPC, NMDC, NTPC, NYKAA, OBEROIRLTY, OFSS, OIL, ONGC, PAGEIND, PATANJALI, PAYTM, PERSISTENT, PETRONET, PFC, PGEL, PHOENIXLTD, PIDILITIND, PIIND, PNB, PNBHOUSING, POLICYBZR, POLYCAB, POWERGRID, POWERINDIA, PREMIERENE, PRESTIGE, RADICO, RBLBANK, RECLTD, RELIANCE, RVNL, SAGILITY, SAIL, SBICARD, SBILIFE, SBIN, SHREECEM, SHRIRAMFIN, SIEMENS, SOLARINDS, SONACOMS, SRF, SUNPHARMA, SUPREMEIND, SUZLON, SWIGGY, TATACONSUM, TATAELXSI, TATAPOWER, TATASTEEL, TCS, TECHM, TIINDIA, TITAN, TMPV, TORNTPHARM, TRENT, TVSMOTOR, UJJIVANSFB, ULTRACEMCO, UNIONBANK, UNITDSPR, UNOMINDA, UPL, VBL, VEDL, VMM, VOLTAS, WAAREEENER, WIPRO, YESBANK, ZYDUSLIFE"""
SYMBOLS = [x.strip() for x in UNIVERSE.split(",") if x.strip()]


class CFG:
    # ---- API behaviour
    min_gap = 0.12              # seconds between API calls (~8/s, inside Upstox limits)
    quote_batch = 100           # Upstox allows 500 keys; 100 keeps GET URLs short & safe
    fund_cache_days = 3
    # ---- DCF
    rf, erp = 0.068, 0.060
    beta_lo, beta_hi = 0.6, 1.8
    wacc_lo, wacc_hi = 0.11, 0.155
    g_lo, g_hi = 0.05, 0.20
    terminal_g = 0.045
    taper = 0.90
    fcf_band = 0.40
    default_tax = 0.2517
    financials_skip_dcf = True  # CFO/Debt are not meaningful for banks/NBFCs/insurers
    # ---- Peers
    min_peers = 4
    pe_cap, evebitda_cap, pb_cap = 100.0, 100.0, 50.0
    small_cr, mid_cr = 5_000, 25_000
    # ---- Historical band
    report_lag_days = 75        # results assumed public this many days after FY end
    min_band_obs = 250
    # ---- Blend
    w_dcf, w_peers, w_hist = 0.40, 0.35, 0.25
    out_hi, out_lo = 2.0, 0.4
    # ---- Signal rules
    upside_large_mid, upside_small = 35.0, 45.0
    health_min, cashflow_min = 3.50, 3.00
    liquidity_floor_cr = 5.0
    health_exit = 2.75
    # ---- Portfolio
    max_stock_pct, max_sector_pct = 0.05, 0.20
    target_frac = 0.90
    stop_buffer = 0.99
    entry_mode = "LTP"          # "LTP" or "PREV_HIGH_BREAK"
    # ---- production safety
    consensus_models, consensus_upside = 2, 15.0   # >=2 kept models must each show >=15% upside
    model_sanity_hi, model_sanity_lo = 3.0, 0.30   # a model value outside 0.3x-3x price is dropped
    stale_fy_days = 500
    eps_pat_tol = 0.5
    risk_per_trade_pct = 0.005  # max loss per trade as % of total capital if stop is hit
    min_rr = 2.0
    min_stop_pct, max_stop_pct = 0.015, 0.15
    max_positions = 10
    max_new_orders_per_day = 3
    max_daily_deploy_pct = 0.10
    max_order_value, min_order_value = 100_000, 5_000
    price_drift_max = 0.015     # abort order if live price moved >1.5% from plan
    cooldown_days = 30
    exec_window = ((9, 30), (15, 10))


con = Console(highlight=False)

LOGDIR = Path("logs"); LOGDIR.mkdir(exist_ok=True)
STATE = Path("state"); STATE.mkdir(exist_ok=True)
LEDGER = STATE / "ledger.json"
KILL = Path("KILL_SWITCH")          # create this file to block all order placement instantly
logging.basicConfig(filename=str(LOGDIR / f"screener_{datetime.now():%Y%m%d}.log"), level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("screener")


def audit(event, **kw):
    rec = {"ts": now_ist().isoformat(), "event": event, **kw}
    with (LOGDIR / f"audit_{now_ist():%Y%m%d}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")


def load_ledger():
    try:
        return json.loads(LEDGER.read_text())
    except Exception:
        return []


def save_ledger(led):
    tmp = LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(led, indent=1, default=str))
    os.replace(tmp, LEDGER)


def in_exec_window() -> bool:
    n = now_ist()
    return n.weekday() < 5 and CFG.exec_window[0] <= (n.hour, n.minute) < CFG.exec_window[1]

# =============================================================================
#  SMALL HELPERS
# =============================================================================
def now_ist() -> datetime:
    return datetime.now(IST)


def market_open() -> bool:
    n = now_ist()
    return n.weekday() < 5 and (9, 15) <= (n.hour, n.minute) < (15, 30)


def ok(x) -> bool:
    return x is not None and isinstance(x, (int, float, np.floating)) and math.isfinite(x)


def sdiv(a, b):
    if not ok(a) or not ok(b) or b == 0:
        return None
    r = a / b
    return r if math.isfinite(r) else None


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def tier_of(mcap_cr: float) -> str:
    return "Small" if mcap_cr < CFG.small_cr else ("Mid" if mcap_cr < CFG.mid_cr else "Large")


def fmt_inr(x, d=1):
    return "-" if not ok(x) else f"{x:,.{d}f}"


def fmt_cr(x):
    if not ok(x):
        return "-"
    return f"{x/1000:,.1f}k" if abs(x) >= 1000 else f"{x:,.0f}"


def mk(ok_: bool) -> str:
    return "[bold green]✔[/]" if ok_ else "[bold red]✘[/]"


def bar(v, mx=5.0, w=10) -> str:
    if not ok(v):
        return "[dim]n/a[/]"
    n = int(round(clamp(v / mx, 0, 1) * w))
    col = "green" if v >= 3.5 else ("yellow" if v >= 2.75 else "red")
    return f"[{col}]{'█' * n}[/][dim]{'░' * (w - n)}[/] {v:.2f}"


def pct_style(v: float) -> str:
    if v >= 35:
        return "bold green"
    if v >= 10:
        return "green"
    if v >= -10:
        return "yellow"
    return "red"


def phase(title: str, sub: str = ""):
    con.print()
    con.print(Rule(f"[bold white on blue] {title} [/]" + (f"  [dim]{sub}[/]" if sub else ""),
                   style="blue"))


def progress() -> Progress:
    return Progress(SpinnerColumn(), TextColumn("[cyan]{task.description}"), BarColumn(),
                    MofNCompleteColumn(), TimeElapsedColumn(), console=con, transient=True)


# =============================================================================
#  ENV + UPSTOX CLIENT
# =============================================================================
def load_env(path: str) -> dict:
    env = {}
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("UPSTOX_ACCESS_TOKEN", "UPSTOX_API_TOKEN", "UPSTOX_API_SECRET", "UPSTOX_REDIRECT_URI"):
        if not env.get(k) and os.environ.get(k):
            env[k] = os.environ[k]
    return env


def save_env_token(path: str, token: str):
    p = Path(path)
    lines = p.read_text().splitlines() if p.exists() else []
    done = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith("UPSTOX_ACCESS_TOKEN"):
            lines[i] = f"UPSTOX_ACCESS_TOKEN={token}"
            done = True
    if not done:
        lines.append(f"UPSTOX_ACCESS_TOKEN={token}")
    p.write_text("\n".join(lines) + "\n")


class Upstox:
    def __init__(self, env: dict):
        self.env = env
        self.s = requests.Session()
        self.lock = threading.Lock()
        self.last = 0.0
        self.set_token(env.get("UPSTOX_ACCESS_TOKEN", ""))

    def set_token(self, t: str):
        self.token = t
        self.s.headers.update({"Authorization": f"Bearer {t}", "Accept": "application/json"})

    def request(self, method: str, path: str, retries: int = 4, **kw):
        url = path if path.startswith("http") else BASE + path
        r = None
        for a in range(retries):
            with self.lock:
                wait = self.last + CFG.min_gap - time.time()
                if wait > 0:
                    time.sleep(wait)
                self.last = time.time()
            try:
                r = self.s.request(method, url, timeout=30, **kw)
            except requests.RequestException:
                if a == retries - 1:
                    raise
                time.sleep(1.5 * (a + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 * (a + 1))
                continue
            return r
        return r

    def profile(self):
        try:
            r = self.request("GET", "/v2/user/profile")
            if r is not None and r.status_code == 200:
                return r.json().get("data", {})
        except requests.RequestException:
            pass
        return None


# =============================================================================
#  DATA MODEL
# =============================================================================
class Stock:
    def __init__(self, symbol, key, isin, name):
        self.symbol, self.key, self.isin, self.name = symbol, key, isin, name
        self.fund = None
        self.c = None            # daily candles dict
        self.w = None            # weekly candles dict
        self.q = None            # latest quote
        self.m = {}              # live metrics (ltp, mcap, multiples ...)
        self.v = {}              # valuation outputs
        self.h = {}              # health outputs
        self.sig = None          # signal outputs
        self.issues = []
        self.flags = []          # data-integrity flags (block buys)

    @property
    def sector(self):
        return (self.fund or {}).get("sector", "Unclassified")

    @property
    def is_fin(self):
        return "financial" in self.sector.lower()


# =============================================================================
#  PHASE 1
# =============================================================================
def step_1_1_auth(api: Upstox, env_path: str, interactive: bool = True):
    phase("PHASE 1.1  ·  AUTHENTICATION", "Upstox OAuth2")
    prof = api.profile()
    if prof:
        con.print(Panel.fit(
            f"[green]✔ Access token valid[/]   user: [bold]{prof.get('user_name', '?')}[/]   "
            f"id: {prof.get('user_id', '?')}   broker: {prof.get('broker', 'UPSTOX')}",
            border_style="green"))
        return
    con.print("[yellow]Access token missing/expired (Upstox tokens expire daily ~3:30 AM IST). "
              "Starting OAuth2 login…[/]")
    if not interactive:
        raise RuntimeError("Access token expired - run once interactively to log in again.")
    key, secret = api.env.get("UPSTOX_API_TOKEN"), api.env.get("UPSTOX_API_SECRET")
    redir = api.env.get("UPSTOX_REDIRECT_URI", "http://127.0.0.1:5000/")
    if not (key and secret):
        sys.exit("UPSTOX_API_TOKEN / UPSTOX_API_SECRET missing in env.txt")
    url = f"{BASE}/v2/login/authorization/dialog?" + urlencode(
        {"client_id": key, "redirect_uri": redir, "response_type": "code"})
    con.print(f"\n1) Open this URL and log in:\n   [link={url}]{url}[/link]")
    raw = input("2) Paste the 'code' (or the whole redirected URL): ").strip()
    code = parse_qs(urlparse(raw).query).get("code", [raw])[0]
    r = requests.post(f"{BASE}/v2/login/authorization/token",
                      headers={"Accept": "application/json",
                               "Content-Type": "application/x-www-form-urlencoded"},
                      data={"code": code, "client_id": key, "client_secret": secret,
                            "redirect_uri": redir, "grant_type": "authorization_code"}, timeout=30)
    if r.status_code != 200 or "access_token" not in r.json():
        sys.exit(f"Token exchange failed: {r.status_code} {r.text[:300]}")
    tok = r.json()["access_token"]
    api.set_token(tok)
    save_env_token(env_path, tok)
    con.print("[green]✔ New access token stored in env.txt[/]")


def step_1_2_universe(api: Upstox, limit: int | None):
    phase("PHASE 1.2  ·  MASTER STOCK UNIVERSE", "Upstox NSE instrument master (EQ / NSE_EQ)")
    f = CACHE / f"instruments_{now_ist():%Y%m%d}.json"
    if f.exists():
        data = json.loads(f.read_text())
    else:
        r = requests.get(INSTRUMENT_URL, timeout=90)
        r.raise_for_status()
        data = json.loads(gzip.decompress(r.content))
        f.write_text(json.dumps(data))
    eq = {}
    for x in data:
        if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ":
            eq.setdefault(x.get("trading_symbol"), x)
    stocks, missing = [], []
    for sym in SYMBOLS:
        x = eq.get(sym)
        if not x:
            missing.append(sym)
            continue
        key = x["instrument_key"]
        stocks.append(Stock(sym, key, x.get("isin") or key.split("|")[-1], x.get("name", sym)))
    bl = Path("blocklist.txt")
    if bl.exists():
        blocked = {x.strip().upper() for x in bl.read_text().replace(",", "\n").split() if x.strip()}
        hit = [s.symbol for s in stocks if s.symbol in blocked]
        stocks = [s for s in stocks if s.symbol not in blocked]
        if hit:
            con.print(f"[yellow]Blocklist applied (excluded): {', '.join(hit)}[/]")
    if limit:
        stocks = stocks[:limit]
    con.print(f"Active NSE EQ instruments in master: [bold]{len(eq):,}[/]   "
              f"Target universe: [bold]{len(SYMBOLS)}[/]   Matched: [bold green]{len(stocks)}[/]")
    if missing:
        con.print(f"[yellow]Not found in Upstox master ({len(missing)}):[/] {', '.join(missing)}")
    return stocks, missing


# ---- 1.3 fundamentals --------------------------------------------------------
def _row(df, names):
    if df is None or getattr(df, "empty", True):
        return []
    for n in names:
        if n in df.index:
            return [None if (v is None or v != v) else float(v) for v in df.loc[n].values]
    return []


def _ttm(df, names):
    v = _row(df, names)[:4]
    return sum(v) if len(v) == 4 and all(x is not None for x in v) else None


def fetch_fundamental(sym: str):
    import yfinance as yf
    tk = yf.Ticker(f"{sym}.NS")
    fin, cf, bs, qf = tk.financials, tk.cashflow, tk.balance_sheet, tk.quarterly_financials
    if fin is None or fin.empty:
        return None
    try:
        info = tk.info or {}
    except Exception:
        info = {}
    f = {"sector": info.get("sector") or "Unclassified",
         "industry": info.get("industry") or info.get("sector") or "Unclassified"}
    f["fy_dates"] = [c.strftime("%Y-%m-%d") for c in fin.columns]
    f["rev"] = _row(fin, ["Total Revenue", "Operating Revenue"])
    f["ebitda"] = _row(fin, ["EBITDA", "Normalized EBITDA"])
    f["ebit"] = _row(fin, ["EBIT", "Operating Income"])
    f["pat"] = _row(fin, ["Net Income", "Net Income Common Stockholders"])
    f["eps"] = _row(fin, ["Diluted EPS", "Basic EPS"])
    f["interest"] = _row(fin, ["Interest Expense", "Interest Expense Non Operating"])
    f["pretax"] = _row(fin, ["Pretax Income"])
    f["tax"] = _row(fin, ["Tax Provision"])
    f["cfo"] = _row(cf, ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"])
    f["capex"] = [abs(x) if x is not None else None
                  for x in _row(cf, ["Capital Expenditure", "Purchase Of PPE"])]
    f["bs_dates"] = [c.strftime("%Y-%m-%d") for c in bs.columns] if bs is not None and not bs.empty else []
    f["equity_hist"] = _row(bs, ["Stockholders Equity", "Common Stock Equity"])
    f["debt"] = (_row(bs, ["Total Debt"]) or [None])[0]
    f["cash"] = (_row(bs, ["Cash And Cash Equivalents",
                           "Cash Cash Equivalents And Short Term Investments"]) or [None])[0]
    f["cur_assets"] = (_row(bs, ["Current Assets"]) or [None])[0]
    f["cur_liab"] = (_row(bs, ["Current Liabilities"]) or [None])[0]
    f["tot_assets"] = (_row(bs, ["Total Assets"]) or [None])[0]
    sh = info.get("sharesOutstanding") or (_row(fin, ["Diluted Average Shares"]) or [None])[0]
    f["shares"] = float(sh) if sh else None
    # TTM from last 4 quarters, fallback = latest fiscal year
    def pick(q_names, ann):
        t = _ttm(qf, q_names)
        return t if t is not None else (ann[0] if ann else None)
    f["ttm_rev"] = pick(["Total Revenue", "Operating Revenue"], f["rev"])
    f["ttm_ebitda"] = pick(["EBITDA", "Normalized EBITDA"], f["ebitda"])
    f["ttm_ebit"] = pick(["EBIT", "Operating Income"], f["ebit"])
    f["ttm_pat"] = pick(["Net Income", "Net Income Common Stockholders"], f["pat"])
    f["ttm_eps"] = pick(["Diluted EPS", "Basic EPS"], f["eps"])
    f["ttm_interest"] = pick(["Interest Expense", "Interest Expense Non Operating"], f["interest"])
    qp = _row(qf, ["Net Income", "Net Income Common Stockholders"])
    f["q_pat_yoy"] = (qp[0] - qp[4]) / abs(qp[4]) if len(qp) >= 5 and qp[0] is not None \
        and qp[4] not in (None, 0) else None
    return f


def step_1_3_fundamentals(stocks, refresh: bool):
    phase("PHASE 1.3  ·  FUNDAMENTAL ACCOUNTING RECORD", "matched by ISIN / symbol · cached locally")
    path = CACHE / "fundamentals.json"
    cache = {}
    if path.exists() and not refresh:
        try:
            cache = json.loads(path.read_text())
        except Exception:
            cache = {}
    fresh_after = time.time() - CFG.fund_cache_days * 86400
    todo = [s for s in stocks if not (s.isin in cache and cache[s.isin].get("_ts", 0) > fresh_after
                                      and cache[s.isin].get("data"))]
    con.print(f"Cached: [green]{len(stocks) - len(todo)}[/]   To fetch from Yahoo Finance: [cyan]{len(todo)}[/]")
    if todo:
        try:
            import yfinance  # noqa: F401
        except ImportError:
            sys.exit("pip install yfinance   (needed for fundamentals)")
        with progress() as pr:
            t = pr.add_task("Fundamentals", total=len(todo))
            with ThreadPoolExecutor(8) as ex:
                futs = {ex.submit(fetch_fundamental, s.symbol): s for s in todo}
                for fu in as_completed(futs):
                    s = futs[fu]
                    try:
                        cache[s.isin] = {"_ts": time.time(), "data": fu.result()}
                    except Exception as e:  # noqa: BLE001
                        cache[s.isin] = {"_ts": time.time(), "data": None, "err": str(e)[:80]}
                    pr.advance(t)
        path.write_text(json.dumps(cache))
    for s in stocks:
        s.fund = (cache.get(s.isin) or {}).get("data")
        if not s.fund:
            s.issues.append("no fundamentals")
    ok_n = sum(1 for s in stocks if s.fund)
    con.print(f"Fundamentals available for [bold green]{ok_n}[/] / {len(stocks)} stocks")


# ---- 1.4 historical candles --------------------------------------------------
def fetch_candles(api: Upstox, key: str, interval: str, frm: str, to: str):
    r = api.request("GET", f"/v2/historical-candle/{quote(key, safe='')}/{interval}/{to}/{frm}")
    if r is None or r.status_code != 200:
        return None
    cs = (r.json().get("data") or {}).get("candles") or []
    if not cs:
        return None
    d = np.array([str(x[0])[:10] for x in cs])
    a = np.array([[x[1], x[2], x[3], x[4], x[5]] for x in cs], dtype=float)
    o = np.argsort(d, kind="stable")
    d, a = d[o], a[o]
    return {"d": d, "o": a[:, 0], "h": a[:, 1], "l": a[:, 2], "c": a[:, 3], "v": a[:, 4]}


def to_weekly(c):
    keys, h, l, cl = [], [], [], []
    last = None
    for i, ds in enumerate(c["d"]):
        k = date.fromisoformat(str(ds)).isocalendar()[:2]
        if k != last:
            keys.append(k); h.append(c["h"][i]); l.append(c["l"][i]); cl.append(c["c"][i]); last = k
        else:
            h[-1] = max(h[-1], c["h"][i]); l[-1] = min(l[-1], c["l"][i]); cl[-1] = c["c"][i]
    return {"k": keys, "h": np.array(h), "l": np.array(l), "c": np.array(cl)}


def step_1_4_candles(api: Upstox, stocks, use_weekly_api: bool, no_cache: bool):
    phase("PHASE 1.4  ·  HISTORICAL MARKET DATA", "Upstox Historical Candle API · 5Y daily + Nifty 50")
    to = now_ist().strftime("%Y-%m-%d")
    frm = (now_ist() - timedelta(days=365 * 5 + 15)).strftime("%Y-%m-%d")
    cpath = CACHE / f"candles_{to}.pkl"
    store = {}
    if cpath.exists() and not no_cache:
        try:
            store = pickle.loads(cpath.read_bytes())
        except Exception:
            store = {}
    keys = [NIFTY_KEY] + [s.key for s in stocks]
    todo = [k for k in keys if k not in store]
    con.print(f"Cached series: [green]{len(keys) - len(todo)}[/]   To download: [cyan]{len(todo)}[/]")
    if todo:
        with progress() as pr:
            t = pr.add_task("Daily candles", total=len(todo))
            with ThreadPoolExecutor(6) as ex:
                futs = {ex.submit(fetch_candles, api, k, "day", frm, to): k for k in todo}
                for fu in as_completed(futs):
                    try:
                        store[futs[fu]] = fu.result()
                    except Exception:
                        store[futs[fu]] = None
                    pr.advance(t)
        cpath.write_bytes(pickle.dumps(store))
    nifty = store.get(NIFTY_KEY)
    if nifty is None:
        sys.exit("Could not download Nifty 50 candles - check token / network.")
    nifty_w = None
    if use_weekly_api:
        wk = fetch_candles(api, NIFTY_KEY, "week", frm, to)
        nifty_w = to_weekly(wk) if wk else None
    nifty_w = nifty_w or to_weekly(nifty)
    for s in stocks:
        s.c = store.get(s.key)
        if s.c is None or len(s.c["c"]) < 260:
            s.issues.append("short/no candles")
            if s.c is not None and len(s.c["c"]) < 60:
                s.c = None
            continue
        s.w = to_weekly(s.c)   # weekly bars are exact aggregates of the daily bars
    n_ok = sum(1 for s in stocks if s.c is not None)
    con.print(f"Candle history for [bold green]{n_ok}[/] / {len(stocks)} stocks · "
              f"Nifty bars: {len(nifty['c']):,} · last bar {nifty['d'][-1]}")
    return nifty, nifty_w


# =============================================================================
#  QUOTES
# =============================================================================
def fetch_quotes(api: Upstox, keys):
    out = {}
    for i in range(0, len(keys), CFG.quote_batch):
        chunk = keys[i:i + CFG.quote_batch]
        r = api.request("GET", "/v2/market-quote/quotes", params={"instrument_key": ",".join(chunk)})
        if r is None or r.status_code != 200:
            continue
        for k, v in (r.json().get("data") or {}).items():
            out[v.get("instrument_token") or k] = v
    return out


def apply_quotes(stocks, quotes):
    for s in stocks:
        s.q = quotes.get(s.key)


# =============================================================================
#  PHASE 2  -  FAIR VALUE ENGINE
# =============================================================================
def cagr(series, yrs):
    if not series or len(series) <= yrs:
        return None
    a, b = series[0], series[yrs]
    if not ok(a) or not ok(b) or a <= 0 or b <= 0:
        return None
    return (a / b) ** (1 / yrs) - 1


def calc_beta(sw, bw):
    sc, bc = dict(zip(sw["k"], sw["c"])), dict(zip(bw["k"], bw["c"]))
    ks = [k for k in sw["k"] if k in bc][-105:]      # ~2 years of weekly closes
    if len(ks) < 40:
        return None
    a = np.array([sc[k] for k in ks]); b = np.array([bc[k] for k in ks])
    ra, rb = np.diff(a) / a[:-1], np.diff(b) / b[:-1]
    var = np.var(rb, ddof=1)
    return float(np.cov(ra, rb, ddof=1)[0, 1] / var) if var > 0 else None


def algo_a_dcf(s: Stock, bw):
    f, m = s.fund, s.m
    out = {"fv": None, "why": "", "kind": "DCF"}
    beta = calc_beta(s.w, bw) if s.w else None
    beta_c = clamp(beta if beta is not None else 1.0, CFG.beta_lo, CFG.beta_hi)
    ke = CFG.rf + beta_c * CFG.erp
    if s.is_fin:      # banks / NBFC / insurers: justified P/B = (ROE-g)/(Ke-g)
        out["kind"] = "RI-PB"
        eq = (f.get("equity_hist") or [None])[0]
        pat = f.get("ttm_pat")
        if not (ok(eq) and eq > 0 and ok(pat) and pat > 0 and f.get("shares")):
            out["why"] = "N/A (financial: needs positive equity & earnings)"
            return out
        roe = clamp(pat / eq, 0.08, 0.25)
        g = min(0.05, ke - 0.04)
        pb = (roe - g) / (ke - g)
        fv = pb * eq / f["shares"]
        out.update(fv=fv if fv > 0 else None, beta=beta, beta_c=beta_c, ke=ke, wacc=ke, g=g,
                   base=pat, base_src="TTM PAT", tv_share=None, roe=roe, pb=pb)
        return out
    n = min(3, len(f["cfo"]), len(f["capex"]))
    fcfs = [f["cfo"][i] - f["capex"][i] for i in range(n)
            if f["cfo"][i] is not None and f["capex"][i] is not None]
    if not fcfs or not f.get("shares"):
        out["why"] = "N/A (no cash-flow data)"
        return out
    latest, avg = fcfs[0], float(np.mean(fcfs))
    if latest > 0 and avg > 0 and abs(latest - avg) / avg <= CFG.fcf_band:
        base, src = latest, "latest FCF"
    else:
        base, src = avg, "3Y avg FCF"
    if base <= 0:
        out["why"] = "N/A (FCF negative)"
        return out
    debt = f.get("debt") or 0.0
    interest = (f.get("interest") or [None])[0]
    kd = clamp(interest / debt, 0.06, 0.14) if ok(interest) and debt > 0 and interest > 0 else 0.095
    pt, tx = (f.get("pretax") or [None])[0], (f.get("tax") or [None])[0]
    tax = clamp(tx / pt, 0.15, 0.30) if ok(pt) and ok(tx) and pt > 0 else CFG.default_tax
    wd = debt / (debt + m["mcap"]) if m["mcap"] > 0 else 0
    wacc = clamp(ke * (1 - wd) + kd * (1 - tax) * wd, CFG.wacc_lo, CFG.wacc_hi)
    gs = [cagr(f["rev"], 3), cagr(f["rev"], 5), cagr(f["ebitda"], 3), cagr(f["ebitda"], 5)]
    gv = [g for g in gs if g is not None]
    g = clamp(float(np.mean(gv)) if gv else 0.08, CFG.g_lo, CFG.g_hi)
    rates = [g, g, g, g * CFG.taper, g * CFG.taper ** 2]
    fcf, pv, flows = base, 0.0, []
    for t, r in enumerate(rates, 1):
        fcf *= 1 + r
        flows.append(fcf)
        pv += fcf / (1 + wacc) ** t
    tv = flows[-1] * (1 + CFG.terminal_g) / (wacc - CFG.terminal_g)
    ev = pv + tv / (1 + wacc) ** 5
    eq = ev + (f.get("cash") or 0.0) - debt
    fv = eq / f["shares"]
    out.update(fv=fv if fv > 0 else None, beta=beta, beta_c=beta_c, ke=ke, wacc=wacc, g=g,
               base=base, base_src=src, ev=ev, tv_share=(tv / (1 + wacc) ** 5) / ev if ev else None)
    if fv <= 0:
        out["why"] = "N/A (negative equity value)"
    return out


def tier_group_keys(s: Stock):
    ind = (s.fund or {}).get("industry")
    if not ind or ind == "Unclassified":
        return []
    return [("industry+tier", (ind, s.m["tier"])), ("industry", (ind,))]


def algo_b_peers(s: Stock, groups):
    f, m = s.fund, s.m
    res = {"fv": None, "implied": {}, "medians": {}, "level": {}, "n": {}}
    metrics = [("pe", CFG.pe_cap), ("pb", CFG.pb_cap)]
    if not s.is_fin:
        metrics.insert(1, ("evebitda", CFG.evebitda_cap))
    for name, cap in metrics:
        for lvl, gk in tier_group_keys(s):
            peers = [p.m[name] for p in groups.get((lvl, gk), []) if p is not s
                     and ok(p.m.get(name)) and 0 < p.m[name] <= cap]
            if len(peers) >= CFG.min_peers:
                res["medians"][name] = float(median(peers)); res["level"][name] = lvl
                res["n"][name] = len(peers)
                break
    sh = f.get("shares")
    if "pe" in res["medians"] and ok(f.get("ttm_eps")) and f["ttm_eps"] > 0:
        res["implied"]["P/E"] = f["ttm_eps"] * res["medians"]["pe"]
    if "evebitda" in res["medians"] and ok(f.get("ttm_ebitda")) and f["ttm_ebitda"] > 0 and sh:
        res["implied"]["EV/EBITDA"] = (f["ttm_ebitda"] * res["medians"]["evebitda"]
                                       + (f.get("cash") or 0) - (f.get("debt") or 0)) / sh
    if "pb" in res["medians"] and ok(m.get("bvps")) and m["bvps"] > 0:
        res["implied"]["P/B"] = m["bvps"] * res["medians"]["pb"]
    vals = [v for v in res["implied"].values() if ok(v) and v > 0]
    res["fv"] = float(np.mean(vals)) if vals else None
    return res


def algo_c_hist(s: Stock):
    f, m, c = s.fund, s.m, s.c
    out = {"fv": None, "pe_med": None, "pe_p25": None, "pe_p75": None, "pb_med": None, "obs": 0}
    if not ok(f.get("ttm_eps")) or f["ttm_eps"] <= 0:
        return out
    lag = timedelta(days=CFG.report_lag_days)
    eps_pts = sorted((date.fromisoformat(d) + lag).isoformat() + "|" + repr(e)
                     for d, e in zip(f["fy_dates"], f["eps"]) if e is not None)
    if not eps_pts:
        return out
    av = np.array([p.split("|")[0] for p in eps_pts]); ev = np.array([float(p.split("|")[1]) for p in eps_pts])
    idx = np.searchsorted(av, c["d"], side="right") - 1
    msk = idx >= 0
    eps_d = np.where(msk, ev[np.clip(idx, 0, None)], np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        pe = c["c"] / eps_d
    pe = pe[(eps_d > 0) & np.isfinite(pe) & (pe < 500)]
    if len(pe) >= CFG.min_band_obs:
        p25, p50, p75 = np.percentile(pe, [25, 50, 75])
        out.update(pe_med=float(p50), pe_p25=float(p25), pe_p75=float(p75), obs=len(pe),
                   fv=float(f["ttm_eps"] * p50))
    if f.get("equity_hist") and f.get("shares") and f.get("bs_dates"):
        bp = sorted((date.fromisoformat(d) + lag).isoformat() + "|" + repr(e / f["shares"])
                    for d, e in zip(f["bs_dates"], f["equity_hist"]) if e)
        if bp:
            bav = np.array([p.split("|")[0] for p in bp]); bv = np.array([float(p.split("|")[1]) for p in bp])
            bi = np.searchsorted(bav, c["d"], side="right") - 1
            bvd = np.where(bi >= 0, bv[np.clip(bi, 0, None)], np.nan)
            with np.errstate(divide="ignore", invalid="ignore"):
                pb = c["c"] / bvd
            pb = pb[(bvd > 0) & np.isfinite(pb)]
            if len(pb) >= CFG.min_band_obs:
                out["pb_med"] = float(np.median(pb))
    return out


def blend(dcf, peers, hist, ltp):
    raw = {"DCF": dcf, "Peers": peers, "Hist": hist}
    res = {"cfv": None, "used": {}, "kept": {}, "flags": [], "median": None}
    avail = {}
    for k, v in raw.items():
        if not ok(v) or v <= 0:
            continue
        if v > CFG.model_sanity_hi * ltp or v < CFG.model_sanity_lo * ltp:
            res["flags"].append(f"{k} dropped (₹{v:,.0f} = {v / ltp:.1f}x price, implausible)")
            continue
        avail[k] = v
    if len(avail) < 2:
        res["flags"].append("fewer than 2 credible models")
        return res
    med = float(median(avail.values()))
    res["median"] = med
    keep = {}
    for k, v in avail.items():
        if v > CFG.out_hi * med or v < CFG.out_lo * med:
            res["flags"].append(f"{k} dropped as outlier ({v / med:.1f}x median)")
        else:
            keep[k] = v
    if len(keep) < 2:
        res["flags"].append("models disagree - no composite")
        return res
    w = {"DCF": CFG.w_dcf, "Peers": CFG.w_peers, "Hist": CFG.w_hist}
    tw = sum(w[k] for k in keep)
    res["cfv"] = sum(w[k] * keep[k] for k in keep) / tw
    res["used"] = {k: w[k] / tw for k in keep}
    res["kept"] = keep
    return res


def integrity(s: Stock):
    f, m, c = s.fund, s.m, s.c
    fl = []
    fy = (f.get("fy_dates") or [None])[0]
    if not fy:
        fl.append("no fiscal-year date")
    elif (now_ist().date() - date.fromisoformat(fy)).days > CFG.stale_fy_days:
        fl.append("stale financials")
    if len(f.get("rev") or []) < 3:
        fl.append("<3 yrs history")
    eps, pat, sh = f.get("ttm_eps"), f.get("ttm_pat"), f.get("shares")
    if ok(eps) and ok(pat) and sh and pat != 0 and abs(eps * sh - pat) / abs(pat) > CFG.eps_pat_tol:
        fl.append("EPS vs PAT/shares mismatch")
    rv = f.get("rev") or []
    if len(rv) >= 2 and ok(rv[0]) and ok(rv[1]) and rv[1] > 0 and abs(rv[0] / rv[1] - 1) > 0.6:
        fl.append("revenue jump >60% (restructure/demerger?)")
    if (now_ist().date() - date.fromisoformat(str(c["d"][-1]))).days > 7:
        fl.append("stale price history")
    if abs(m["ltp"] / float(c["c"][-1]) - 1) > 0.25:
        fl.append("price gap >25% vs last close")
    return fl


def phase2(stocks, nifty_w):
    phase("PHASE 2  ·  THREE-MODEL FAIR VALUE ENGINE", "DCF/RI · tight-peer multiples · historical P/E band")
    valid = []
    for s in stocks:
        if not s.fund or s.c is None or not s.q:
            continue
        f = s.fund
        ltp = float(s.q.get("last_price") or 0) or float(s.c["c"][-1])
        if ltp <= 0 or not f.get("shares"):
            s.issues.append("no price/shares")
            continue
        m = s.m
        m["ltp"] = ltp
        m["mcap"] = ltp * f["shares"]
        m["mcap_cr"] = m["mcap"] / CR
        m["tier"] = tier_of(m["mcap_cr"])
        m["ev"] = m["mcap"] + (f.get("debt") or 0) - (f.get("cash") or 0)
        m["bvps"] = sdiv((f["equity_hist"] or [None])[0], f["shares"])
        m["pe"] = sdiv(ltp, f.get("ttm_eps"))
        m["evebitda"] = None if s.is_fin else sdiv(m["ev"], f.get("ttm_ebitda"))
        m["pb"] = sdiv(ltp, m["bvps"])
        s.flags = integrity(s)
        valid.append(s)
    clean = [s for s in valid if not s.flags]       # suspect data never contaminates peer medians
    groups = {}
    for s in clean:
        for lvl, gk in tier_group_keys(s):
            groups.setdefault((lvl, gk), []).append(s)
    with progress() as pr:
        t = pr.add_task("Valuing", total=len(valid))
        for s in valid:
            a = algo_a_dcf(s, nifty_w)
            b = algo_b_peers(s, groups)
            c = algo_c_hist(s)
            bl = blend(a["fv"], b["fv"], c["fv"], s.m["ltp"])
            s.v = {"A": a, "B": b, "C": c, "blend": bl, "cfv": bl["cfv"]}
            pr.advance(t)
    n_cfv = sum(1 for s in valid if s.v.get("cfv"))
    con.print(f"Stocks valued: [bold]{len(valid)}[/]   composite FV (>=2 credible, agreeing models): "
              f"[bold green]{n_cfv}[/]   data-integrity flagged: [yellow]{sum(1 for s in valid if s.flags)}[/]   "
              f"(DCF/RI {sum(1 for s in valid if s.v['A']['fv'])}, Peers {sum(1 for s in valid if s.v['B']['fv'])}, "
              f"Hist {sum(1 for s in valid if s.v['C']['fv'])})")
    return valid


# =============================================================================
#  PHASE 3  -  FINANCIAL HEALTH SCORE
# =============================================================================
def pct_rank(vals: dict, higher_better=True):
    items = [(k, v) for k, v in vals.items() if ok(v)]
    n = len(items)
    if n == 0:
        return {}
    if n == 1:
        return {items[0][0]: 0.5}
    arr = np.array([v for _, v in items])
    out = {}
    for k, v in items:
        p = ((arr < v).sum() + ((arr == v).sum() - 1) / 2) / (n - 1)
        out[k] = p if higher_better else 1 - p
    return out


BUCKETS = {
    "Cash Flow":     [("CFO/EBITDA", True), ("FCF/Sales", True), ("Int. Coverage", True)],
    "Profitability": [("ROCE", True), ("ROE", True), ("Op Margin", True), ("Margin Stability", True)],
    "Growth":        [("Rev CAGR 3Y", True), ("EPS CAGR 3Y", True), ("Qtr PAT YoY", True)],
    "Solvency":      [("Debt/Equity", False), ("NetDebt/EBITDA", False), ("Current Ratio", True)],
    "Value+Mom":     [("Earnings Yield", True), ("Return 6M", True), ("Return 12M", True)],
}


def raw_ratios(s: Stock):
    f, m, c = s.fund, s.m, s.c
    g = lambda k: (f.get(k) or [None])[0]  # noqa: E731
    cfo, capex, ebitda, rev, ebit = g("cfo"), g("capex"), g("ebitda"), g("rev"), g("ebit")
    interest = f.get("ttm_interest")
    fcf = cfo - capex if ok(cfo) and ok(capex) else None
    r = {}
    r["CFO/EBITDA"] = sdiv(cfo, ebitda) if ok(ebitda) and ebitda > 0 else None
    r["FCF/Sales"] = sdiv(fcf, rev)
    ebit_t = f.get("ttm_ebit")
    r["Int. Coverage"] = (min(ebit_t / interest, 50.0) if ok(ebit_t) and ok(interest) and interest > 0
                          else (50.0 if ok(ebit_t) and ebit_t > 0 else None))
    ta, cl = f.get("tot_assets"), f.get("cur_liab")
    r["ROCE"] = sdiv(ebit_t, ta - cl) if ok(ta) and ok(cl) and ta - cl > 0 else None
    eq = (f["equity_hist"] or [None])[0]
    r["ROE"] = sdiv(f.get("ttm_pat"), eq) if ok(eq) and eq > 0 else None
    r["Op Margin"] = sdiv(ebit_t, f.get("ttm_rev"))
    mg = [sdiv(a, b) for a, b in zip(f["ebit"][:5], f["rev"][:5])]
    mg = [x for x in mg if x is not None]
    r["Margin Stability"] = -float(np.std(mg)) if len(mg) >= 3 else None
    r["Rev CAGR 3Y"] = cagr(f["rev"], 3)
    r["EPS CAGR 3Y"] = cagr(f["eps"], 3)
    r["Qtr PAT YoY"] = f.get("q_pat_yoy")
    debt = f.get("debt")
    r["Debt/Equity"] = sdiv(debt, eq) if ok(eq) and eq > 0 and ok(debt) else None
    nd = (debt or 0) - (f.get("cash") or 0)
    r["NetDebt/EBITDA"] = sdiv(nd, ebitda) if ok(ebitda) and ebitda > 0 and ok(debt) else None
    r["Current Ratio"] = sdiv(f.get("cur_assets"), f.get("cur_liab"))
    if s.is_fin:
        r["Earnings Yield"] = sdiv(f.get("ttm_pat"), m["mcap"])
        r["Debt/Equity"] = r["NetDebt/EBITDA"] = r["Current Ratio"] = None   # not meaningful
    else:
        r["Earnings Yield"] = sdiv(ebit_t, m["ev"]) if m["ev"] > 0 else None
    cl_ = c["c"]
    r["Return 6M"] = m["ltp"] / cl_[-126] - 1 if len(cl_) > 126 else None
    r["Return 12M"] = m["ltp"] / cl_[-252] - 1 if len(cl_) > 252 else None
    return r


def phase3(valid):
    phase("PHASE 3  ·  FINANCIAL HEALTH SCORE", "5 pillars · sector-relative percentile → 0-5")
    raws = {s.symbol: raw_ratios(s) for s in valid}
    by_sector = {}
    for s in valid:
        by_sector.setdefault(s.sector, []).append(s.symbol)
    metric_score = {s.symbol: {} for s in valid}
    fallback_used = 0
    for bucket, mets in BUCKETS.items():
        for name, hb in mets:
            uni = pct_rank({k: v[name] for k, v in raws.items()}, hb)
            for sec, syms in by_sector.items():
                have = [x for x in syms if ok(raws[x][name])]
                sp = pct_rank({x: raws[x][name] for x in have}, hb) if len(have) >= 5 else None
                for x in syms:
                    if not ok(raws[x][name]):
                        continue
                    p = sp[x] if sp is not None else uni.get(x)
                    if sp is None:
                        fallback_used += 1
                    metric_score[x][name] = 5.0 * p
    for s in valid:
        bs = {}
        for bucket, mets in BUCKETS.items():
            sc = [metric_score[s.symbol][n] for n, _ in mets if n in metric_score[s.symbol]]
            if sc:
                bs[bucket] = float(np.mean(sc))
        s.h = {"raw": raws[s.symbol], "metric": metric_score[s.symbol], "buckets": bs,
               "score": float(np.mean(list(bs.values()))) if len(bs) >= 4 else None,
               "cf": bs.get("Cash Flow")}
    scored = [s.h["score"] for s in valid if s.h["score"] is not None]
    con.print(f"Scored [bold]{len(scored)}[/] stocks · mean {np.mean(scored):.2f} · "
              f"≥3.50: [green]{sum(1 for x in scored if x >= 3.5)}[/] · "
              f"<2.75: [red]{sum(1 for x in scored if x < 2.75)}[/]   "
              f"[dim](universe-wide ranking used where a sector had <5 data points: {fallback_used} metric-cells)[/]")


# =============================================================================
#  PHASE 4  -  SIGNAL ENGINE
# =============================================================================
def structure(c, q, ltp, is_open):
    d, H, L, C, V = c["d"], c["h"], c["l"], c["c"], c["v"]
    today = now_ist().strftime("%Y-%m-%d")
    if is_open:
        n = int(np.searchsorted(d, today, side="left"))
        qlow = float(((q or {}).get("ohlc") or {}).get("low") or 0)
        sess_low = min(qlow, ltp) if qlow > 0 else ltp
        ref = today
    else:                      # market closed -> evaluate the last completed session
        n = len(d) - 1
        sess_low = float(L[-1])
        ref = str(d[-1])
    if n < 25:
        return None
    Hp, Lp, Cp, Vp, dp = H[:n], L[:n], C[:n], V[:n], d[:n]
    low20 = float(Lp[-20:].min())
    prev_close = float(Cp[-1])
    rd = date.fromisoformat(ref)
    pk = (rd - timedelta(days=rd.weekday() + 1)).isocalendar()[:2]
    pw = [float(Hp[i]) for i in range(max(0, n - 15), n)
          if date.fromisoformat(str(dp[i])).isocalendar()[:2] == pk]
    pw_high = max(pw) if pw else None
    new_low = min(sess_low, ltp) < low20
    reclaim = (ltp > prev_close) or (pw_high is not None and ltp > pw_high)
    return {"low20": low20, "prev_close": prev_close, "prev_high": float(Hp[-1]), "pw_high": pw_high,
            "new_low": new_low, "confirm": reclaim,
            "avg_val_cr": float(np.mean(Cp[-20:] * Vp[-20:]) / CR)}


def scan_stock(s: Stock, q, is_open):
    cfv = s.v.get("cfv") if s.v else None
    health = s.h.get("score") if s.h else None
    if not cfv or health is None or s.c is None:
        s.sig = None
        return
    ltp = float((q or {}).get("last_price") or 0) or s.m["ltp"]
    up = (cfv - ltp) / ltp * 100
    disc = (cfv - ltp) / cfv * 100
    thr = CFG.upside_small if s.m["tier"] == "Small" else CFG.upside_large_mid
    st = structure(s.c, q, ltp, is_open)
    lc = float((q or {}).get("lower_circuit_limit") or 0)
    locked = lc > 0 and ltp <= lc * 1.001
    kept = s.v["blend"].get("kept", {})
    agree = sum(1 for v in kept.values() if (v - ltp) / ltp * 100 >= CFG.consensus_upside)
    r1 = up >= thr and agree >= CFG.consensus_models
    f, raw = s.fund, s.h["raw"]
    fail = []
    if s.flags:
        fail.append("data: " + ", ".join(s.flags))
    pat = f.get("ttm_pat")
    if not ok(pat) or pat <= 0:
        fail.append("loss-making (TTM PAT<=0)")
    roe = raw.get("ROE")
    if ok(roe) and roe < 0.08:
        fail.append(f"ROE {roe:.0%} < 8%")
    if not s.is_fin:
        ic, de = raw.get("Int. Coverage"), raw.get("Debt/Equity")
        cfo0 = (f.get("cfo") or [None])[0]
        if ok(ic) and ic < 1.5:
            fail.append(f"interest cover {ic:.1f}x < 1.5")
        if ok(de) and de > 2.5:
            fail.append(f"D/E {de:.1f} > 2.5")
        if ok(cfo0) and cfo0 <= 0:
            fail.append("negative operating cash flow")
    r2 = (health >= CFG.health_min and (s.h.get("cf") or 0) >= CFG.cashflow_min and not fail)
    r3 = (not locked) and st is not None and st["avg_val_cr"] >= CFG.liquidity_floor_cr
    r4 = st is not None and (not st["new_low"]) and st["confirm"]
    s.sig = {"ltp": ltp, "upside": up, "disc": disc, "thr": thr, "rules": (r1, r2, r3, r4),
             "locked": locked, "st": st, "health": health, "cf": s.h.get("cf"), "cfv": cfv,
             "agree": agree, "r2_fail": fail}
    s.sig["verdict"] = verdict(s.sig)


def verdict(sig):
    r1, r2, r3, r4 = sig["rules"]
    up = sig["upside"]
    if r1 and r2 and r3 and r4:
        return ("ACTIONABLE BUY", "bold black on green")
    if r1 and r2 and r3:
        return ("UNDERVALUED · WAIT (price structure)", "bold yellow")
    if r1 and r2:
        return ("UNDERVALUED · BLOCKED (liquidity/circuit)", "yellow")
    if r1:
        return ("VALUE-TRAP RISK (quality/data gate)", "bold red")
    if up >= 15:
        return ("MODERATELY UNDERVALUED", "green")
    if up >= -10:
        return ("FAIRLY VALUED", "white")
    return ("OVERVALUED", "red")


def swing_low(s: Stock, ltp):
    L = s.w["l"]
    n = len(L)
    piv = [float(L[i]) for i in range(max(2, n - 30), n - 2)
           if L[i] < min(L[i - 2], L[i - 1], L[i + 1], L[i + 2])]
    cand = [p for p in piv if p < ltp]
    if cand:
        return cand[-1]
    m = float(L[-12:].min())
    return m if m < ltp else ltp * 0.9


def scan_all(valid, quotes, is_open):
    for s in valid:
        scan_stock(s, quotes.get(s.key), is_open)
    return [s for s in valid if s.sig]


# =============================================================================
#  DISPLAY
# =============================================================================
def banner(env_path, universe_n):
    t = Text()
    t.append("UPSTOX  VALUE  SCREENER\n", style="bold white")
    t.append("3-Model Fair Value  ·  Financial Health  ·  4-Rule Signal Filter  ·  Portfolio Sizing\n",
             style="cyan")
    t.append(f"{now_ist():%A %d %b %Y  %H:%M:%S IST}   ·   universe {universe_n}   ·   "
             f"market {'OPEN' if market_open() else 'CLOSED'}", style="dim")
    con.print(Panel(t, border_style="bright_blue", box=box.DOUBLE, padding=(1, 4)))


def market_panel(quotes):
    q = quotes.get(NIFTY_KEY)
    if not q:
        return
    ltp = q.get("last_price")
    chg = q.get("net_change")
    pct = (chg / (ltp - chg) * 100) if ok(chg) and ok(ltp) and ltp - chg else None
    col = "green" if (chg or 0) >= 0 else "red"
    con.print(Panel.fit(f"NIFTY 50   [bold]{fmt_inr(ltp, 2)}[/]   "
                        f"[{col}]{fmt_inr(chg, 2)}  ({pct:+.2f}%)[/]" if pct is not None
                        else f"NIFTY 50   {fmt_inr(ltp, 2)}", border_style="blue"))


def rules_str(r):
    return " ".join(mk(x) for x in r)


def table_buys(buys, plans):
    t = Table(title="[bold green]★ SIGNIFICANTLY UNDERVALUED (ACTIONABLE BUY) ★[/]", box=box.HEAVY_EDGE,
              border_style="green", header_style="bold white on dark_green", expand=False)
    for c, j in [("#", "right"), ("Symbol", "left"), ("Sector", "left"), ("Cap", "left"),
                 ("LTP ₹", "right"), ("Fair ₹", "right"), ("Upside", "right"), ("Health", "left"),
                 ("Rules 1-4", "center"), ("Entry ₹", "right"), ("Target ₹", "right"),
                 ("Stop ₹", "right"), ("R:R", "right"), ("Qty", "right")]:
        t.add_column(c, justify=j)
    for i, s in enumerate(buys, 1):
        sg = s.sig
        p = plans.get(s.symbol, {})
        entry, tgt, stp = p.get("entry"), p.get("target"), p.get("stop")
        rr = (tgt - entry) / (entry - stp) if entry and stp and entry > stp else None
        t.add_row(str(i), f"[bold]{s.symbol}[/]", s.sector[:18], f"{s.m['tier']} {fmt_cr(s.m['mcap_cr'])}Cr",
                  fmt_inr(sg["ltp"]), fmt_inr(sg["cfv"]), f"[bold green]{sg['upside']:+.1f}%[/]",
                  bar(sg["health"], w=6), rules_str(sg["rules"]), fmt_inr(entry), fmt_inr(tgt),
                  fmt_inr(stp), f"{rr:.1f}" if rr else "-", str(p.get("qty", "-")))
    return t


def table_ranked(title, rows, border="cyan"):
    t = Table(title=title, box=box.SIMPLE_HEAVY, border_style=border, header_style="bold cyan")
    for c, j in [("Symbol", "left"), ("Cap", "left"), ("LTP", "right"), ("DCF", "right"),
                 ("Peers", "right"), ("Hist", "right"), ("Fair", "right"), ("Upside", "right"),
                 ("Disc", "right"), ("Health", "left"), ("CF", "right"), ("R1-R4", "center"),
                 ("Verdict", "left")]:
        t.add_column(c, justify=j)
    for s in rows:
        sg, v = s.sig, s.v
        lab, sty = sg["verdict"]
        t.add_row(f"[bold]{s.symbol}[/]", f"{s.m['tier'][0]}·{fmt_cr(s.m['mcap_cr'])}", fmt_inr(sg["ltp"]),
                  fmt_inr(v["A"]["fv"], 0), fmt_inr(v["B"]["fv"], 0), fmt_inr(v["C"]["fv"], 0),
                  fmt_inr(sg["cfv"], 0), f"[{pct_style(sg['upside'])}]{sg['upside']:+.1f}%[/]",
                  f"{sg['disc']:+.0f}%", bar(sg["health"], w=6),
                  f"{sg['cf']:.1f}" if ok(sg["cf"]) else "-", rules_str(sg["rules"]), f"[{sty}]{lab}[/]")
    return t


def table_sectors(scanned):
    sec = {}
    for s in scanned:
        sec.setdefault(s.sector, []).append(s)
    t = Table(title="Sector Heat-map", box=box.SIMPLE_HEAVY, border_style="magenta", header_style="bold magenta")
    for c, j in [("Sector", "left"), ("#", "right"), ("Avg Health", "left"), ("Median Upside", "right"),
                 ("Undervalued≥R1", "right"), ("BUY", "right")]:
        t.add_column(c, justify=j)
    for k, v in sorted(sec.items(), key=lambda kv: -float(np.median([x.sig["upside"] for x in kv[1]]))):
        mu = float(np.median([x.sig["upside"] for x in v]))
        t.add_row(k, str(len(v)), bar(float(np.mean([x.sig["health"] for x in v])), w=8),
                  f"[{pct_style(mu)}]{mu:+.1f}%[/]", str(sum(1 for x in v if x.sig["rules"][0])),
                  f"[bold green]{sum(1 for x in v if all(x.sig['rules']))}[/]")
    return t


def card(s: Stock):
    sg, v, h, m, f = s.sig, s.v, s.h, s.m, s.fund
    A, B, C, bl = v["A"], v["B"], v["C"], v["blend"]
    lab, sty = sg["verdict"]
    L = []
    L.append(f"[bold]{s.name}[/]  ({s.symbol} · {s.isin})   {s.sector} / {f['industry']}")
    L.append(f"LTP [bold]₹{sg['ltp']:,.2f}[/]   Fair [bold]₹{sg['cfv']:,.2f}[/]   Upside "
             f"[{pct_style(sg['upside'])}]{sg['upside']:+.1f}%[/] (need ≥{sg['thr']:.0f}%)   "
             f"Discount {sg['disc']:+.1f}%   Tier {m['tier']} (₹{m['mcap_cr']:,.0f} Cr)")
    L.append("")
    L.append("[bold cyan]Fair value models[/]")
    if A["fv"] and A.get("kind") == "RI-PB":
        L.append(f"  RI-P/B ₹{A['fv']:,.0f}   justified P/B {A['pb']:.2f}x  ROE {A['roe']:.1%}  Ke={A['ke']:.1%}  g={A['g']:.1%}")
    elif A["fv"]:
        L.append(f"  DCF    ₹{A['fv']:,.0f}   β={A.get('beta') if A.get('beta') is None else round(A['beta'], 2)}"
                 f"→{A['beta_c']:.2f}  Ke={A['ke']:.1%}  WACC={A['wacc']:.1%}  g={A['g']:.1%}  "
                 f"base FCF ₹{A['base'] / CR:,.0f}Cr ({A['base_src']})  TV share {A['tv_share']:.0%}")
    else:
        L.append(f"  DCF    [dim]{A['why']}[/]")
    if B["fv"]:
        imp = "  ".join(f"{k} ₹{x:,.0f}" for k, x in B["implied"].items())
        med = "  ".join(f"{k.upper()} {x:.1f}x({B['level'][k]},n={B['n'][k]})" for k, x in B["medians"].items())
        L.append(f"  Peers  ₹{B['fv']:,.0f}   implied: {imp}")
        L.append(f"         peer medians: {med}")
    else:
        L.append("  Peers  [dim]N/A (insufficient peers / data)[/]")
    if C["fv"]:
        L.append(f"  Hist   ₹{C['fv']:,.0f}   own P/E median {C['pe_med']:.1f}x "
                 f"(IQR {C['pe_p25']:.1f}-{C['pe_p75']:.1f}x, {C['obs']} days)   now {m['pe']:.1f}x"
                 if ok(m.get("pe")) else f"  Hist   ₹{C['fv']:,.0f}")
    else:
        L.append("  Hist   [dim]N/A (EPS<=0 or short history)[/]")
    L.append("  Blend  " + "  ".join(f"{k} {w:.0%}" for k, w in bl["used"].items())
             + ("   [yellow]" + "; ".join(bl["flags"]) + "[/]" if bl["flags"] else ""))
    L.append("")
    if s.flags:
        L.append("[bold red]DATA FLAGS: " + "; ".join(s.flags) + "[/]")
        L.append("")
    L.append(f"[bold cyan]Financial Health  {bar(h['score'])}[/]")
    for b, sc in h["buckets"].items():
        L.append(f"  {b:<14}{bar(sc)}")
    L.append("")
    st = sg["st"] or {}
    r1, r2, r3, r4 = sg["rules"]
    L.append("[bold cyan]Rules[/]")
    L.append(f"  {mk(r1)} R1 Margin of safety   upside {sg['upside']:+.1f}% vs ≥{sg['thr']:.0f}% · models showing ≥"
             f"{CFG.consensus_upside:.0f}% upside: {sg['agree']}/{len(v['blend'].get('kept', {}))} (need {CFG.consensus_models})")
    cfs = f"{sg['cf']:.2f}" if ok(sg["cf"]) else "n/a"
    L.append(f"  {mk(r2)} R2 Quality            health {sg['health']:.2f} (≥{CFG.health_min}) · cash-flow {cfs} "
             f"(≥{CFG.cashflow_min})" + ("  [red]" + "; ".join(sg["r2_fail"]) + "[/]" if sg["r2_fail"] else ""))
    L.append(f"  {mk(r3)} R3 Liquidity/circuit  20d avg value ₹{st.get('avg_val_cr', 0):,.1f} Cr "
             f"(≥{CFG.liquidity_floor_cr}) · lower-circuit lock: {'YES' if sg['locked'] else 'no'}")
    L.append(f"  {mk(r4)} R4 Price structure   20d low {st.get('low20', 0):,.1f} · new 20d low: "
             f"{'YES' if st.get('new_low') else 'no'} · prev close {st.get('prev_close', 0):,.1f} · "
             f"prev-week high {fmt_inr(st.get('pw_high'))} · reclaim: {'yes' if st.get('confirm') else 'no'}")
    return Panel("\n".join(L), title=f"[{sty}] {s.symbol}  ·  {lab} [/]", border_style="green" if all(sg["rules"]) else "cyan",
                 padding=(1, 2))


def render_scan(scanned, plans, args, quotes, is_open):
    market_panel(quotes)
    if not is_open:
        con.print("[dim]Market closed → rules evaluated on the last completed session. "
                  "R4 is most meaningful with --live during market hours.[/]")
    buys = sorted([s for s in scanned if all(s.sig["rules"])], key=lambda s: -s.sig["upside"])
    k = Table.grid(padding=(0, 3))
    k.add_row(f"Scanned [bold]{len(scanned)}[/]",
              f"≥R1 undervalued [bold]{sum(1 for s in scanned if s.sig['rules'][0])}[/]",
              f"Quality pass [bold]{sum(1 for s in scanned if s.sig['rules'][1])}[/]",
              f"[bold green]ACTIONABLE BUY {len(buys)}[/]")
    con.print(Panel(k, title="Decision Summary", border_style="green" if buys else "yellow"))
    if buys:
        con.print(table_buys(buys, plans))
    else:
        con.print(Panel("[yellow]No stock passes all four rules right now. Patience is a position.[/]",
                        border_style="yellow"))
    wl = sorted([s for s in scanned if s.sig["rules"][0] and s.sig["rules"][1] and not all(s.sig["rules"])],
                key=lambda s: -s.sig["upside"])[:10]
    if wl:
        con.print(table_ranked("WATCHLIST — cheap & high quality, waiting on liquidity / price structure", wl, "yellow"))
    top = sorted(scanned, key=lambda s: -s.sig["upside"])[:args.top]
    con.print(table_ranked(f"TOP {args.top} BY ESTIMATED UPSIDE (all verdicts)", top))
    traps = sorted([s for s in scanned if s.sig["rules"][0] and not s.sig["rules"][1]],
                   key=lambda s: -s.sig["upside"])[:8]
    if traps:
        con.print(table_ranked("⚠ VALUE-TRAP RISK — big discount but health below threshold", traps, "red"))
    con.print(table_sectors(scanned))
    return buys


def data_quality(stocks, missing):
    bad = [s for s in stocks if s.issues or s.flags or not s.v or not s.v.get("cfv")]
    if not bad and not missing:
        return
    t = Table(title="Data-quality report (excluded / partial)", box=box.SIMPLE, header_style="bold yellow")
    t.add_column("Symbol"); t.add_column("Reason", overflow="fold")
    for s in bad[:70]:
        why = ", ".join(s.issues + s.flags) or ("; ".join(s.v["blend"]["flags"]) if s.v else "not valued")
        t.add_row(s.symbol, why)
    for m_ in missing:
        t.add_row(m_, "not in Upstox NSE_EQ master")
    con.print(t)
    if len(bad) > 70:
        con.print(f"[dim]…and {len(bad) - 70} more[/]")


# =============================================================================
#  PHASE 5  -  PORTFOLIO + EXECUTION
# =============================================================================
def plan_orders(api: Upstox, args, buys, by_sym):
    ctx = {"notes": [], "holdings_ok": False, "funds_ok": False}
    holdings, avail = [], None
    try:
        r = api.request("GET", "/v2/portfolio/long-term-holdings")
        if r is not None and r.status_code == 200:
            holdings = r.json().get("data") or []
            ctx["holdings_ok"] = True
        else:
            ctx["notes"].append(f"Holdings API unavailable ({getattr(r, 'status_code', '?')})")
        r = api.request("GET", "/v2/user/get-funds-and-margin", params={"segment": "SEC"})
        if r is not None and r.status_code == 200:
            avail = float(r.json()["data"]["equity"]["available_margin"])
            ctx["funds_ok"] = True
        else:
            ctx["notes"].append("Funds API unavailable (offline ~12:00-5:30 AM IST)")
    except Exception as e:  # noqa: BLE001
        ctx["notes"].append(f"Portfolio fetch failed: {e}")
        log.exception("portfolio fetch")
    hold_val, held, sec_val = 0.0, {}, {}
    for h in holdings:
        sym = h.get("trading_symbol")
        val = float(h.get("quantity") or 0) * float(h.get("last_price") or 0)
        hold_val += val
        held[sym] = held.get(sym, 0) + val
        sec = by_sym[sym].sector if sym in by_sym else "Other"
        sec_val[sec] = sec_val.get(sec, 0) + val
    if args.capital:
        total = args.capital
        cash = avail if avail is not None else max(total - hold_val, 0)
    else:
        cash = avail if avail is not None else 0.0
        total = cash + hold_val
    ctx.update(total=total, cash=cash, hold_val=hold_val, n_hold=len(holdings))
    ctx["exits"] = [(sym, by_sym[sym].h["score"]) for sym in held if sym in by_sym and by_sym[sym].h
                    and by_sym[sym].h.get("score") is not None and by_sym[sym].h["score"] < CFG.health_exit]
    led = [e for e in load_ledger() if e.get("status") in ("PLACED", "SUBMITTING", "UNKNOWN")]
    today = now_ist().date()
    dd = lambda e: date.fromisoformat(e["date"])  # noqa: E731
    today_n = sum(1 for e in led if dd(e) == today)
    deployed = sum(e["qty"] * e["entry"] for e in led if dd(e) == today)
    cooling = {e["symbol"] for e in led if (today - dd(e)).days < CFG.cooldown_days}
    budget = CFG.max_daily_deploy_pct * total - deployed
    n_pos = len({h.get("trading_symbol") for h in holdings})
    plans, new_n = {}, 0
    for s in buys:
        sg = s.sig
        ltp = sg["ltp"]
        entry = ltp if CFG.entry_mode == "LTP" or not sg["st"] else max(ltp, sg["st"]["prev_high"])
        target = CFG.target_frac * sg["cfv"]
        stop = swing_low(s, ltp) * CFG.stop_buffer
        if stop > entry * (1 - CFG.min_stop_pct):
            stop = entry * (1 - CFG.min_stop_pct)           # stops tighter than 1.5% just get noise-hit
        risk = entry - stop
        stop_pct = risk / entry
        rr = (target - entry) / risk if risk > 0 else 0
        why = []
        if total <= 0:
            why.append("capital unknown (use --capital)")
        if stop_pct > CFG.max_stop_pct:
            why.append(f"stop {stop_pct:.0%} away > max {CFG.max_stop_pct:.0%}")
        if rr < CFG.min_rr:
            why.append(f"R:R {rr:.1f} < {CFG.min_rr}")
        if s.symbol in cooling:
            why.append("already ordered (cool-down)")
        if n_pos + new_n >= CFG.max_positions:
            why.append("max positions reached")
        if today_n + new_n >= CFG.max_new_orders_per_day:
            why.append("daily order limit reached")
        sector_room = CFG.max_sector_pct * total - sec_val.get(s.sector, 0)
        cap_alloc = min(CFG.max_stock_pct * total - held.get(s.symbol, 0), sector_room, cash, budget,
                        CFG.max_order_value)
        qty_cap = int(cap_alloc // entry) if cap_alloc > 0 else 0
        qty_risk = int((CFG.risk_per_trade_pct * total) // risk) if risk > 0 else 0
        qty = min(qty_cap, qty_risk)
        if not why:
            if qty < 1:
                why.append(f"1 share ₹{entry:,.0f} exceeds limits (alloc ₹{max(cap_alloc, 0):,.0f}, risk-qty {qty_risk})")
            elif qty * entry < CFG.min_order_value:
                why.append(f"order ₹{qty * entry:,.0f} < min ₹{CFG.min_order_value:,.0f}")
        if why:
            qty = 0
        else:
            cost = qty * entry
            cash -= cost; budget -= cost; new_n += 1
            sec_val[s.sector] = sec_val.get(s.sector, 0) + cost
            held[s.symbol] = held.get(s.symbol, 0) + cost
        plans[s.symbol] = {"qty": qty, "entry": entry, "target": target, "stop": stop, "rr": rr,
                           "stop_pct": stop_pct, "why": why, "stock": s}
    return plans, ctx


def render_plan(plans, ctx, args):
    phase("PHASE 5  ·  PORTFOLIO CHECK & EXECUTION PLAN",
          "[bold red]LIVE ORDERS ARMED (--execute)[/]" if args.execute else "DRY-RUN (no orders will be sent)")
    for n in ctx["notes"]:
        con.print(f"[yellow]{n}[/]")
    con.print(Panel.fit(
        f"Total capital [bold]₹{ctx['total']:,.0f}[/]   Available cash [bold]₹{ctx['cash']:,.0f}[/]   "
        f"Holdings ₹{ctx['hold_val']:,.0f} ({ctx['n_hold']} positions)\n"
        f"Limits: {CFG.max_stock_pct:.0%}/stock · {CFG.max_sector_pct:.0%}/sector · risk {CFG.risk_per_trade_pct:.2%}/trade · "
        f"≤{CFG.max_new_orders_per_day} orders & ≤{CFG.max_daily_deploy_pct:.0%} capital/day · R:R ≥ {CFG.min_rr}",
        border_style="blue"))
    if ctx["exits"]:
        con.print(Panel(f"[bold red]EXIT TRIGGER (health < {CFG.health_exit}):[/] " +
                        ", ".join(f"{a} ({b:.2f})" for a, b in ctx["exits"]), border_style="red"))
    if not plans:
        return
    t = Table(title="Execution Plan (CNC delivery · GTT multi-leg)", box=box.ROUNDED, border_style="green",
              header_style="bold green")
    for c, j in [("Symbol", "left"), ("Qty", "right"), ("Entry", "right"), ("Target", "right"), ("Stop", "right"),
                 ("Capital ₹", "right"), ("Max loss ₹", "right"), ("R:R", "right"), ("Status", "left")]:
        t.add_column(c, justify=j)
    for sym, p in plans.items():
        q = p["qty"]
        t.add_row(f"[bold]{sym}[/]", str(q), fmt_inr(p["entry"]), fmt_inr(p["target"]), fmt_inr(p["stop"]),
                  f"{q * p['entry']:,.0f}", f"{q * (p['entry'] - p['stop']):,.0f}", f"{p['rr']:.1f}",
                  "[green]ready[/]" if q else "[yellow]SKIP: " + "; ".join(p["why"]) + "[/]")
    con.print(t)
    con.print(f"[dim]Extra exit rule: sell if a future quarterly update drops Financial Health < {CFG.health_exit}.[/]")


def place_orders(api: Upstox, args, plans, ctx):
    live = {k: p for k, p in plans.items() if p["qty"] > 0}
    if not live:
        con.print("[yellow]Nothing eligible to place.[/]")
        return
    if KILL.exists():
        con.print("[bold red]KILL_SWITCH file present - order placement blocked.[/]")
        audit("blocked", reason="kill switch")
        return
    if not in_exec_window():
        con.print(f"[yellow]Outside execution window "
                  f"{CFG.exec_window[0][0]:02d}:{CFG.exec_window[0][1]:02d}-{CFG.exec_window[1][0]:02d}:"
                  f"{CFG.exec_window[1][1]:02d} IST (Mon-Fri). Nothing sent.[/]")
        audit("blocked", reason="outside window")
        return
    if not ctx.get("holdings_ok"):
        con.print("[bold red]Holdings could not be verified - refusing to place orders.[/]")
        audit("blocked", reason="holdings unverified")
        return
    if args.no_confirm and not args.i_accept_risk:
        con.print("[red]--no-confirm requires --i-accept-risk.[/]")
        return
    total_cost = sum(p["qty"] * p["entry"] for p in live.values())
    max_loss = sum(p["qty"] * (p["entry"] - p["stop"]) for p in live.values())
    con.print(Panel(f"[bold red]LIVE ORDER PLACEMENT[/]  {len(live)} GTT order(s) · capital ₹{total_cost:,.0f} · "
                    f"max loss at stops ≈ ₹{max_loss:,.0f}\n"
                    "[dim]Stops can gap through in fast markets; GTT triggers are not guaranteed fills. "
                    "Brokerage, taxes and slippage are not modelled.[/]", border_style="red"))
    if not args.no_confirm:
        if input(f"Type  PLACE {len(live)}  to send: ").strip() != f"PLACE {len(live)}":
            con.print("[yellow]Cancelled - nothing sent.[/]")
            audit("cancelled")
            return
    led = load_ledger()
    today = str(now_ist().date())
    for sym, p in live.items():
        if KILL.exists():
            con.print("[bold red]KILL_SWITCH detected - stopping.[/]")
            break
        s = p["stock"]
        if any(e["symbol"] == sym and e["date"] == today and e["status"] != "FAILED" for e in led):
            con.print(f"[yellow]{sym}: already ordered today - skipped (duplicate guard)[/]")
            continue
        q = fetch_quotes(api, [s.key]).get(s.key)
        ltp = float((q or {}).get("last_price") or 0)
        lc = float((q or {}).get("lower_circuit_limit") or 0)
        if ltp <= 0:
            con.print(f"[red]{sym}: no fresh quote - skipped[/]"); audit("skip", symbol=sym, reason="no quote"); continue
        drift = abs(ltp / p["entry"] - 1)
        if drift > CFG.price_drift_max or (lc > 0 and ltp <= lc * 1.001):
            con.print(f"[yellow]{sym}: price moved {drift:.1%} from plan (or circuit) - skipped[/]")
            audit("skip", symbol=sym, reason="drift/circuit", ltp=ltp, planned=p["entry"])
            continue
        rec = {"id": uuid.uuid4().hex[:8], "symbol": sym, "date": today, "ts": now_ist().isoformat(),
               "qty": p["qty"], "entry": p["entry"], "target": p["target"], "stop": p["stop"], "status": "SUBMITTING"}
        led.append(rec)
        save_ledger(led)                                  # written BEFORE sending: a crash can never double-order
        entry_trig = "IMMEDIATE" if CFG.entry_mode == "LTP" else "ABOVE"
        body = {"type": "MULTIPLE", "quantity": p["qty"], "product": "D", "instrument_token": s.key,
                "transaction_type": "BUY",
                "rules": [{"strategy": "ENTRY", "trigger_type": entry_trig, "trigger_price": round(p["entry"], 2)},
                          {"strategy": "TARGET", "trigger_type": "IMMEDIATE", "trigger_price": round(p["target"], 2)},
                          {"strategy": "STOPLOSS", "trigger_type": "IMMEDIATE", "trigger_price": round(p["stop"], 2)}]}
        audit("order_submit", **rec, body=body)
        try:
            r = api.request("POST", "/v3/order/gtt/place", retries=1, json=body)   # never auto-retry a POST
        except Exception as e:  # noqa: BLE001
            rec["status"], rec["err"] = "UNKNOWN", str(e)
            save_ledger(led); audit("order_unknown", id=rec["id"], err=str(e))
            con.print(f"[bold red]{sym}: network error - order state UNKNOWN. Check Upstox app before retrying.[/]")
            continue
        txt = (r.text if r is not None else "")[:400]
        if r is None or r.status_code >= 500:
            rec["status"] = "UNKNOWN"
        elif r.status_code in (200, 201):
            rec["status"] = "PLACED"
        else:
            rec["status"] = "FAILED"
        rec["resp"] = txt
        save_ledger(led)
        audit("order_result", id=rec["id"], status=rec["status"], code=getattr(r, "status_code", None), resp=txt)
        col = {"PLACED": "green", "FAILED": "red", "UNKNOWN": "bold red"}[rec["status"]]
        con.print(f"[{col}]{rec['status']}[/] {sym} x{p['qty']}  ({getattr(r, 'status_code', '?')}) {txt[:160]}")


def export_csv(scanned):
    f = Path(f"screener_results_{now_ist():%Y%m%d_%H%M}.csv")
    with f.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "sector", "tier", "mcap_cr", "ltp", "fv_dcf", "fv_peers", "fv_hist", "fair_value",
                    "upside_pct", "discount_pct", "health", "cashflow_pillar", "R1", "R2", "R3", "R4", "verdict"])
        for s in sorted(scanned, key=lambda x: -x.sig["upside"]):
            sg, v = s.sig, s.v
            w.writerow([s.symbol, s.sector, s.m["tier"], round(s.m["mcap_cr"]), round(sg["ltp"], 2),
                        *(round(x, 1) if x else "" for x in (v["A"]["fv"], v["B"]["fv"], v["C"]["fv"], sg["cfv"])),
                        round(sg["upside"], 1), round(sg["disc"], 1), round(sg["health"], 2),
                        round(sg["cf"], 2) if ok(sg["cf"]) else "", *[int(x) for x in sg["rules"]], sg["verdict"][0]])
    con.print(f"[dim]Full results saved → {f}[/]")


# =============================================================================
#  ORCHESTRATION
# =============================================================================
def run_pipeline(args, force_live=False):
    if args.max_stock_pct:
        CFG.max_stock_pct = args.max_stock_pct
    if args.risk_pct:
        CFG.risk_per_trade_pct = args.risk_pct
    audit("run_start", execute=args.execute, live=args.live, capital=args.capital)
    env = load_env(args.env)
    api = Upstox(env)
    banner(args.env, len(SYMBOLS))
    if args.execute:
        con.print(Panel("[bold red]EXECUTE MODE - real orders can be sent (09:30-15:10 IST, after confirmation).[/]\n"
                        "Create a file named KILL_SWITCH in this folder to block all orders instantly.",
                        border_style="red"))
    step_1_1_auth(api, args.env, interactive=not args.daemon)
    stocks, missing = step_1_2_universe(api, args.limit)
    step_1_3_fundamentals(stocks, args.refresh_fund)
    nifty, nifty_w = step_1_4_candles(api, stocks, args.weekly_api, args.no_cache)
    keys = [s.key for s in stocks] + [NIFTY_KEY]
    quotes = fetch_quotes(api, keys)
    apply_quotes(stocks, quotes)
    if not any(s.q for s in stocks):
        con.print("[yellow]No quotes returned - falling back to last close prices.[/]")
    valid = phase2(stocks, nifty_w)
    phase3(valid)
    by_sym = {s.symbol: s for s in valid}

    phase("PHASE 4  ·  SCAN & SIGNAL GENERATION", "Live discount · Estimated upside · 4-rule trigger + safety gates")
    is_open = market_open()
    scanned = scan_all(valid, quotes, is_open)
    buys = sorted([s for s in scanned if all(s.sig["rules"])], key=lambda s: -s.sig["upside"])
    audit("scan", scanned=len(scanned), buys=[s.symbol for s in buys])
    plans, ctx = plan_orders(api, args, buys, by_sym)
    render_scan(scanned, plans, args, quotes, is_open)
    render_plan(plans, ctx, args)
    for s in buys[:5]:
        con.print(card(s))
    for sym in [x.strip().upper() for x in (args.detail or "").split(",") if x.strip()]:
        if sym in by_sym and by_sym[sym].sig:
            con.print(card(by_sym[sym]))
        else:
            con.print(f"[yellow]{sym}: no valuation available[/]")
    if args.execute:
        place_orders(api, args, plans, ctx)
    elif any(p["qty"] for p in plans.values()):
        con.print("[dim]Dry-run only. Re-run with --execute inside market hours to place these as GTT orders.[/]")
    data_quality(stocks, missing)
    export_csv(scanned)
    con.print(Panel("[bold]Legend[/]  R1 upside ≥35% (L/M) / ≥45% (Small) AND ≥2 credible models each ≥+15% · "
                    "R2 health ≥3.50, cash-flow ≥3.00 and absolute gates (profitable, ROE≥8%, cover≥1.5x, D/E≤2.5, CFO>0, clean data) · "
                    "R3 no lower-circuit & 20d avg value ≥ ₹5 Cr · R4 no new 20-day low + higher close / prev-week-high reclaim\n"
                    "[dim]Screening output only - not investment advice. Verify fundamentals independently before acting.[/]",
                    border_style="dim"))
    if args.live or force_live:
        live_loop(api, valid, args, by_sym)


def live_loop(api, valid, args, by_sym):
    phase("LIVE MODE", f"polling every {args.interval}s until 15:30 IST · Ctrl-C to stop")
    keys = [s.key for s in valid] + [NIFTY_KEY]
    try:
        while True:
            n = now_ist()
            if n.weekday() >= 5 or (n.hour, n.minute) >= (15, 30):
                con.print("[dim]Market closed - live loop finished.[/]")
                return
            if (n.hour, n.minute) < (9, 15):
                time.sleep(min(30, args.interval))
                continue
            q = fetch_quotes(api, keys)
            scanned = scan_all(valid, q, True)
            buys = sorted([s for s in scanned if all(s.sig["rules"])], key=lambda s: -s.sig["upside"])
            if sys.stdout.isatty():
                con.clear()
            con.print(Rule(f"[bold]LIVE {now_ist():%H:%M:%S}[/]  ·  {len(buys)} actionable", style="green"))
            market_panel(q)
            if buys:
                con.print(table_buys(buys, {}))
            near = sorted([s for s in scanned if s.sig["rules"][0] and s.sig["rules"][1] and not all(s.sig["rules"])],
                          key=lambda s: -s.sig["upside"])[:8]
            if near:
                con.print(table_ranked("Watchlist", near, "yellow"))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        con.print("\n[dim]Stopped.[/]")


def daemon(args):
    last_run = None
    while True:
        n = now_ist()
        t = n.replace(hour=8, minute=30, second=0, microsecond=0)
        if last_run == n.date() or n > t + timedelta(hours=7):
            t += timedelta(days=1)
        while t.weekday() >= 5:
            t += timedelta(days=1)
        if t > n:
            con.print(f"[dim]Sleeping until {t:%a %d %b %H:%M} IST…[/]")
            time.sleep((t - n).total_seconds())
        last_run = now_ist().date()
        try:
            run_pipeline(args, force_live=True)
        except Exception as e:  # noqa: BLE001
            con.print(f"[red]Run failed: {e}[/]"); log.exception("daemon run failed")


def parse_args():
    p = argparse.ArgumentParser(description="Upstox value screener (production-hardened)")
    p.add_argument("--env", default="env.txt")
    p.add_argument("--live", action="store_true", help="poll quotes during market hours after the full pass")
    p.add_argument("--daemon", action="store_true", help="run daily at 08:30 IST (analysis only; never auto-orders)")
    p.add_argument("--interval", type=int, default=120, help="live polling seconds (default 120)")
    p.add_argument("--execute", action="store_true", help="enable real GTT order placement (typed confirmation)")
    p.add_argument("--no-confirm", action="store_true", help="skip typed confirmation (needs --i-accept-risk)")
    p.add_argument("--i-accept-risk", action="store_true")
    p.add_argument("--capital", type=float, default=0, help="override total capital for sizing (₹)")
    p.add_argument("--max-stock-pct", type=float, default=0, help="e.g. 0.05 = 5%% of capital per stock")
    p.add_argument("--risk-pct", type=float, default=0, help="e.g. 0.005 = risk 0.5%% of capital per trade")
    p.add_argument("--detail", default="", help="comma list of symbols for deep-dive cards")
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--limit", type=int, default=0, help="only first N stocks (testing)")
    p.add_argument("--refresh-fund", action="store_true", help="ignore fundamentals cache")
    p.add_argument("--no-cache", action="store_true", help="ignore candle cache")
    p.add_argument("--weekly-api", action="store_true", help="fetch Nifty weekly candles from API instead of resampling")
    return p.parse_args()


if __name__ == "__main__":
    a = parse_args()
    try:
        daemon(a) if a.daemon else run_pipeline(a)
    except KeyboardInterrupt:
        con.print("\n[dim]Interrupted.[/]")
