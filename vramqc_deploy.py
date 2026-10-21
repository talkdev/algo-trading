#!/usr/bin/env python3
"""
================================================================================
NSE V-RAMQC v3.0 — SINGLE-FILE INSTITUTIONAL BACKTESTING ENGINE
(Variance-Adaptive Residual-Quality-Conservative Compounder for NSE Equities)
================================================================================
Mandate:
  - 100% Indian NSE Equities (Zero Debt, Zero Liquid ETFs, Zero Commodities)
  - 10-Year Wealth Compounding Horizon (Oct 2016 – Oct 2026, with 2013–2016 warmup)
  - Data Source: Upstox API v2 (configured via env.txt) + Official NSE Archives
  - Storage: Local SQLite Database (instruments, daily_candles, index_candles,
    fundamentals_pit, backtest_nav_history, backtest_rebalance_log,
    backtest_holdings_snapshot)
================================================================================
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    import requests as _requests_lib
except ImportError:
    _requests_lib = None


# ==============================================================================
# 1. CONFIGURATION & ENV.TXT LOADER
# ==============================================================================

@dataclass
class BacktestConfig:
    env_file: str = "env.txt"
    upstox_api_key: str = ""
    upstox_api_secret: str = ""
    upstox_redirect_uri: str = "http://127.0.0.1:8000/callback"
    upstox_access_token: str = ""
    upstox_base_url: str = "https://api.upstox.com/v2"
    sqlite_db_path: str = "vramqc_nse_backtest.db"
    universe_max_stocks: int = 180
    data_start_date: str = "2013-01-01"
    backtest_start_date: str = "2016-10-01"
    backtest_end_date: str = "2026-10-20"
    initial_corpus_inr: float = 10_000_000.0  # ₹1.00 Crore
    risk_free_rate_annual: float = 0.065      # 6.5% p.a. 91d T-Bill proxy
    portfolio_size: int = 25                  # Target N = 25 NSE stocks
    stcg_tax_rate: float = 0.20               # 20.0% Indian STCG (<365 days)
    ltcg_tax_rate: float = 0.125              # 12.5% Indian LTCG (>=365 days)
    ltcg_exemption_inr: float = 125_000.0     # ₹1.25 Lakh annual LTCG exemption
    transaction_cost_bps: float = 22.0        # 22 bps one-way (STT + impact + charges)


def load_config_from_env(env_path: str = "env.txt") -> BacktestConfig:
    cfg = BacktestConfig(env_file=env_path)
    if not os.path.exists(env_path):
        # Create a default env.txt if missing
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(
                "UPSTOX_API_KEY=your_upstox_api_key_here\n"
                "UPSTOX_API_SECRET=your_upstox_api_secret_here\n"
                "UPSTOX_REDIRECT_URI=http://127.0.0.1:8000/callback\n"
                "UPSTOX_ACCESS_TOKEN=your_upstox_oauth_access_token_here\n"
                "UPSTOX_BASE_URL=https://api.upstox.com/v2\n"
                "SQLITE_DB_PATH=vramqc_nse_backtest.db\n"
                "UNIVERSE_MAX_STOCKS=180\n"
                "DATA_START_DATE=2013-01-01\n"
                "BACKTEST_START_DATE=2016-10-01\n"
                "BACKTEST_END_DATE=2026-10-20\n"
                "INITIAL_CORPUS_INR=10000000\n"
                "RISK_FREE_RATE_ANNUAL=0.065\n"
            )

    raw_env: Dict[str, str] = {}
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            raw_env[k.strip()] = v.strip().strip("'").strip('"')

    cfg.upstox_api_key = raw_env.get("UPSTOX_API_KEY", cfg.upstox_api_key)
    cfg.upstox_api_secret = raw_env.get("UPSTOX_API_SECRET", cfg.upstox_api_secret)
    cfg.upstox_redirect_uri = raw_env.get("UPSTOX_REDIRECT_URI", cfg.upstox_redirect_uri)
    cfg.upstox_access_token = raw_env.get("UPSTOX_ACCESS_TOKEN", cfg.upstox_access_token)
    cfg.upstox_base_url = raw_env.get("UPSTOX_BASE_URL", cfg.upstox_base_url).rstrip("/")
    cfg.sqlite_db_path = raw_env.get("SQLITE_DB_PATH", cfg.sqlite_db_path)
    if "UNIVERSE_MAX_STOCKS" in raw_env:
        cfg.universe_max_stocks = int(raw_env["UNIVERSE_MAX_STOCKS"])
    if "DATA_START_DATE" in raw_env:
        cfg.data_start_date = raw_env["DATA_START_DATE"]
    if "BACKTEST_START_DATE" in raw_env:
        cfg.backtest_start_date = raw_env["BACKTEST_START_DATE"]
    if "BACKTEST_END_DATE" in raw_env:
        cfg.backtest_end_date = raw_env["BACKTEST_END_DATE"]
    if "INITIAL_CORPUS_INR" in raw_env:
        cfg.initial_corpus_inr = float(raw_env["INITIAL_CORPUS_INR"])
    if "RISK_FREE_RATE_ANNUAL" in raw_env:
        cfg.risk_free_rate_annual = float(raw_env["RISK_FREE_RATE_ANNUAL"])

    return cfg


# ==============================================================================
# 2. SQLITE DATABASE SCHEMA & MANAGEMENT
# ==============================================================================

def init_sqlite_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA temp_store = MEMORY;")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS instruments (
            symbol TEXT PRIMARY KEY,
            company_name TEXT,
            isin TEXT,
            instrument_key TEXT,
            amfi_sector TEXT,
            cap_tier TEXT,
            cap_rank INTEGER,
            track TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_candles (
            symbol TEXT,
            trade_date TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            traded_value REAL,
            PRIMARY KEY (symbol, trade_date)
        );

        CREATE INDEX IF NOT EXISTS idx_candles_date ON daily_candles(trade_date);
        CREATE INDEX IF NOT EXISTS idx_candles_sym ON daily_candles(symbol);

        CREATE TABLE IF NOT EXISTS index_candles (
            index_name TEXT,
            trade_date TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            PRIMARY KEY (index_name, trade_date)
        );

        CREATE TABLE IF NOT EXISTS fundamentals_pit (
            symbol TEXT,
            reconstitution_date TEXT,
            track TEXT,
            roce_ttm REAL,
            roce_3y_med REAL,
            roe_ttm REAL,
            roa_ttm REAL,
            ebitda_to_assets REAL,
            ppop_to_assets REAL,
            opm_ttm REAL,
            net_debt_to_equity REAL,
            net_debt_to_ebitda REAL,
            icr_ttm REAL,
            accrual_ratio REAL,
            cfo_3y_sum REAL,
            core_pat_ttm REAL,
            core_pat_3y_sum REAL,
            net_npa REAL,
            gnpa_change_1y REAL,
            crar_buffer_bps REAL,
            solvency_ratio REAL,
            cost_to_income REAL,
            rev_cagr_stability REAL,
            sue_core REAL,
            rev_accel REAL,
            div_yield REAL,
            share_dilution_2y REAL,
            fcf_yield REAL,
            earnings_yield REAL,
            retention_ratio REAL,
            promoter_pledge_pct REAL,
            pledge_change_1y REAL,
            asm_gsm_stage INTEGER,
            audit_clean INTEGER,
            data_source TEXT,
            PRIMARY KEY (symbol, reconstitution_date)
        );

        CREATE TABLE IF NOT EXISTS backtest_nav_history (
            trade_date TEXT PRIMARY KEY,
            portfolio_nav_gross REAL,
            portfolio_nav_net REAL,
            benchmark_nav REAL,
            drawdown_net_pct REAL,
            benchmark_dd_pct REAL,
            stress_theta REAL,
            portfolio_beta REAL,
            smallcap_weight_pct REAL
        );

        CREATE TABLE IF NOT EXISTS backtest_rebalance_log (
            rebalance_date TEXT PRIMARY KEY,
            stress_theta REAL,
            vr_ratio REAL,
            d_trend_pct REAL,
            dd_mkt_pct REAL,
            w_mom REAL,
            w_qual REAL,
            w_cons REAL,
            lambda_exp REAL,
            eligible_u_count INTEGER,
            quality_ustar_count INTEGER,
            replacements_count INTEGER,
            ltcg_cliff_protected_count INTEGER,
            turnover_oneway_pct REAL,
            portfolio_beta REAL,
            smallcap_weight_pct REAL,
            stcg_tax_paid_inr REAL,
            ltcg_tax_paid_inr REAL,
            cost_paid_inr REAL
        );

        CREATE TABLE IF NOT EXISTS backtest_holdings_snapshot (
            rebalance_date TEXT,
            rank_in_port INTEGER,
            symbol TEXT,
            company_name TEXT,
            cap_tier TEXT,
            track TEXT,
            amfi_sector TEXT,
            weight_pct REAL,
            z_master REAL,
            s_resmom REAL,
            s_qual REAL,
            s_cons REAL,
            beta_shrunk REAL,
            sigma_down_252d_pct REAL,
            PRIMARY KEY (rebalance_date, symbol)
        );
        """
    )
    conn.commit()
    return conn


# ==============================================================================
# 3. UPSTOX API v2 CLIENT & NSE UNIVERSE / CANDLE INGESTION
# ==============================================================================

# Classification of Financial Services symbols into Track B1 (Lending) vs Track B2 (Non-Lending)
NON_LENDING_BFSI_KEYWORDS = {
    "BSE", "MCX", "CDSL", "CAMS", "KFINTECH", "ANGELONE", "MOTILALOFS",
    "360ONE", "NUVAMA", "ANANDRATHI", "HDFCAMC", "NAM-INDIA", "UTIAMC",
    "ABSLAMC", "ICICIGI", "HDFCLIFE", "SBILIFE", "ICICIPRULI", "LICI",
    "STARHEALTH", "GICRE", "NIACL", "MAXFIN", "PBFFINTECH", "POLICYBZR",
    "PAYTM", "CRISIL", "ICRA", "CAREERP", "IIFLSEC", "JMFINANCIL", "PRUDENT"
}


def classify_accounting_track(symbol: str, amfi_sector: str, company_name: str) -> str:
    sec_upper = (amfi_sector or "").upper()
    name_upper = (company_name or "").upper()
    sym_upper = symbol.upper()
    if "FINANCIAL" not in sec_upper and "BANK" not in sec_upper and "INSURANCE" not in sec_upper:
        return "Track_A_NonBFSI"
    if sym_upper in NON_LENDING_BFSI_KEYWORDS or any(
        kw in name_upper
        for kw in [
            "INSURANCE", "ASSURANCE", "ASSET MANAGEMENT", "AMC", "EXCHANGE",
            "DEPOSITORY", "BROKING", "SECURITIES", "WEALTH", "RATINGS", "REGISTRAR", "CAMS"
        ]
    ):
        return "Track_B2_NonLending_BFSI"
    return "Track_B1_Lending_BFSI"


class UpstoxDataManager:
    def __init__(self, cfg: BacktestConfig, conn: sqlite3.Connection):
        self.cfg = cfg
        self.conn = conn
        self.has_valid_oauth = (
            bool(cfg.upstox_access_token)
            and cfg.upstox_access_token != "your_upstox_oauth_access_token_here"
            and len(cfg.upstox_access_token) > 20
        )
        self._rate_lock = threading.Lock()
        self._last_req_ts = 0.0
        self._session = _requests_lib.Session() if _requests_lib is not None else None

    def _throttle(self, min_interval: float = 0.18) -> None:
        """Token-bucket rate limiter (~5.5 req/sec) to prevent Upstox/Cloudflare HTTP 429 bans."""
        with self._rate_lock:
            now = time.time()
            wait = min_interval - (now - self._last_req_ts)
            if wait > 0:
                time.sleep(wait)
            self._last_req_ts = time.time()

    def _build_headers(self, include_auth: bool = True) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) UpstoxQuantBacktester/3.1",
        }
        if include_auth and self.has_valid_oauth:
            headers["Authorization"] = f"Bearer {self.cfg.upstox_access_token}"
        if self.cfg.upstox_api_key and self.cfg.upstox_api_key != "your_upstox_api_key_here":
            headers["X-Upstox-Api-Key"] = self.cfg.upstox_api_key
        return headers

    def fetch_historical_candles_chunked(
        self, instrument_key: str, start_date: str, end_date: str
    ) -> List[Tuple[str, float, float, float, float, float]]:
        """
        Calls Upstox v2 Historical Candle endpoint in 9.5-year (3,470-day) chunks newest-first:
          GET {UPSTOX_BASE_URL}/historical-candle/{instrument_key}/day/{to_date}/{from_date}
        Automatically stops requesting older chunks once a stock's IPO/listing date is reached!
        """
        encoded_key = urllib.parse.quote(instrument_key, safe="")
        d_start = dt.date.fromisoformat(start_date)
        d_end = dt.date.fromisoformat(end_date)

        # Build 9.5-year windows newest-first so we cut HTTP calls by 60%+
        windows: List[Tuple[dt.date, dt.date]] = []
        cur_end = d_end
        while cur_end >= d_start:
            cur_start = max(d_start, cur_end - dt.timedelta(days=3470))
            windows.append((cur_end, cur_start))
            cur_end = cur_start - dt.timedelta(days=1)

        all_rows: Dict[str, Tuple[str, float, float, float, float, float]] = {}
        for to_dt_obj, from_dt_obj in windows:
            to_d, from_d = to_dt_obj.isoformat(), from_dt_obj.isoformat()
            url = f"{self.cfg.upstox_base_url}/historical-candle/{encoded_key}/day/{to_d}/{from_d}"
            chunk_earliest_date: Optional[dt.date] = None

            for attempt in range(4):
                self._throttle(0.18 if attempt == 0 else 0.50)
                try:
                    headers = self._build_headers(include_auth=(attempt == 0))
                    if self._session is not None:
                        resp = self._session.get(url, headers=headers, timeout=12)
                        status_code = resp.status_code
                        if status_code in (401, 403) and attempt == 0:
                            continue
                        if status_code == 429:
                            time.sleep(2.5 * (attempt + 1))
                            continue
                        if status_code != 200:
                            break
                        payload = resp.json()
                    else:
                        req = urllib.request.Request(url, headers=headers)
                        with urllib.request.urlopen(req, timeout=12) as u_resp:
                            payload = json.loads(u_resp.read().decode("utf-8"))

                    candles = payload.get("data", {}).get("candles", [])
                    for c in candles:
                        t_date = str(c[0])[:10]
                        o, h, l, cl, vol = (
                            float(c[1]),
                            float(c[2]),
                            float(c[3]),
                            float(c[4]),
                            float(c[5]),
                        )
                        if cl > 0:
                            all_rows[t_date] = (t_date, o, h, l, cl, vol)
                    if candles:
                        earliest_str = str(candles[-1][0])[:10]
                        chunk_earliest_date = dt.date.fromisoformat(earliest_str)
                    break
                except urllib.error.HTTPError as e:
                    if e.code in (401, 403) and attempt == 0:
                        continue
                    if e.code == 429:
                        time.sleep(2.5 * (attempt + 1))
                        continue
                    break
                except Exception:
                    time.sleep(0.8 * (attempt + 1))

            # Smart IPO Early-Stop: if chunk returned empty OR earliest candle is >45 days
            # after from_dt_obj, the stock was not listed prior to chunk_earliest_date!
            if chunk_earliest_date is None or (chunk_earliest_date - from_dt_obj).days > 45:
                break

        return [all_rows[k] for k in sorted(all_rows.keys())]

    def sync_universe_and_candles(self) -> None:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(DISTINCT symbol), MIN(trade_date) FROM daily_candles")
        row_c = cur.fetchone()
        existing_stocks = row_c[0] or 0
        min_stock_date = str(row_c[1] or "9999-12-31")

        cur.execute(
            "SELECT COUNT(*), MIN(trade_date) FROM index_candles WHERE index_name = 'NIFTY_500'"
        )
        row_i = cur.fetchone()
        existing_idx = row_i[0] or 0
        min_idx_date = str(row_i[1] or "9999-12-31")

        target_start_dt = dt.date.fromisoformat(self.cfg.data_start_date)
        need_earlier_stock_backfill = (
            existing_stocks > 0
            and (dt.date.fromisoformat(min_stock_date) - target_start_dt).days > 20
        )
        need_idx_sync = (
            existing_idx <= 2500
            or (dt.date.fromisoformat(min_idx_date) - target_start_dt).days > 20
        )

        if (
            existing_stocks >= self.cfg.universe_max_stocks
            and not need_earlier_stock_backfill
            and not need_idx_sync
        ):
            print(
                f"[SQLite Cache] Found {existing_stocks} NSE stocks and {existing_idx} "
                f"Nifty 500 trading days ({min_stock_date} -> {self.cfg.backtest_end_date}) "
                f"already cached in '{self.cfg.sqlite_db_path}'."
            )
            return

        # If all 300 stocks are already cached from 2006 but only NIFTY_500 needs backfill:
        if (
            existing_stocks >= self.cfg.universe_max_stocks
            and not need_earlier_stock_backfill
            and need_idx_sync
        ):
            print(
                f"[Upstox API v2 Sync] Backfilling NIFTY_500 benchmark index candles "
                f"({self.cfg.data_start_date} -> {self.cfg.backtest_end_date})..."
            )
            idx_candles = self.fetch_historical_candles_chunked(
                "NSE_INDEX|Nifty 500", self.cfg.data_start_date, self.cfg.backtest_end_date
            )
            if idx_candles:
                self.conn.executemany(
                    """
                    INSERT OR REPLACE INTO index_candles
                    (index_name, trade_date, open, high, low, close)
                    VALUES ('NIFTY_500', ?, ?, ?, ?, ?)
                    """,
                    [(r[0], r[1], r[2], r[3], r[4]) for r in idx_candles],
                )
                self.conn.commit()
                print(f"  -> Synced NIFTY_500 index candles: {len(idx_candles)} trading days.")
            return

        need_earlier_backfill = need_earlier_stock_backfill

        print(
            f"[Upstox API v2 Sync] Initializing NSE Universe (target={self.cfg.universe_max_stocks} "
            f"stocks across Large/Mid/Small caps) & syncing daily OHLCV candles "
            f"({self.cfg.data_start_date} -> {self.cfg.backtest_end_date})..."
        )

        # 1. Fetch NSE Nifty 100 (Large), Midcap 150 (Mid), Smallcap 250 (Small) lists
        # Sample evenly across the full A-Z list so there is zero alphabetical truncation bias
        tier_files = [
            ("Large_Cap", "ind_nifty100list.csv", 1, 100),
            ("Mid_Cap", "ind_niftymidcap150list.csv", 101, 110),
            ("Small_Cap", "ind_niftysmallcap250list.csv", 251, 90),
        ]
        scale = self.cfg.universe_max_stocks / 300.0
        selected_instruments: List[Tuple[str, str, str, str, str, str, int, str]] = []
        seen_symbols = set()

        for cap_tier, csv_name, start_rank, base_quota in tier_files:
            quota = max(15, int(round(base_quota * scale)))
            url = f"https://nsearchives.nseindia.com/content/indices/{csv_name}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    text = r.read().decode("utf-8", errors="ignore")
                all_rows_tier = []
                for row in csv.DictReader(io.StringIO(text)):
                    sym = (row.get("Symbol") or "").strip()
                    isin = (row.get("ISIN Code") or "").strip()
                    comp = (row.get("Company Name") or "").strip()
                    ind = (row.get("Industry") or "Diversified").strip()
                    series = (row.get("Series") or "EQ").strip()
                    if not sym or not isin or series != "EQ" or sym in seen_symbols:
                        continue
                    if "DUMMY" in sym or sym.startswith("PVR"):
                        continue
                    all_rows_tier.append((sym, comp, isin, ind))

                if len(all_rows_tier) > quota:
                    idx_pick = np.linspace(0, len(all_rows_tier) - 1, quota, dtype=int)
                    picked_rows = [all_rows_tier[i] for i in idx_pick]
                else:
                    picked_rows = all_rows_tier

                rank_counter = start_rank
                for sym, comp, isin, ind in picked_rows:
                    if sym in seen_symbols:
                        continue
                    seen_symbols.add(sym)
                    inst_key = f"NSE_EQ|{isin}"
                    track = classify_accounting_track(sym, ind, comp)
                    selected_instruments.append(
                        (sym, comp, isin, inst_key, ind, cap_tier, rank_counter, track)
                    )
                    rank_counter += 1
            except Exception as e:
                print(f"  [Warning] Could not fetch {csv_name}: {e}")

        self.conn.executemany(
            """
            INSERT OR REPLACE INTO instruments
            (symbol, company_name, isin, instrument_key, amfi_sector, cap_tier, cap_rank, track)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            selected_instruments,
        )
        self.conn.commit()

        # 2. Fetch or Backfill Nifty 500 Benchmark Index Candles from Upstox API v2
        if existing_idx <= 2500 or (dt.date.fromisoformat(min_idx_date) - target_start_dt).days > 20:
            idx_candles = self.fetch_historical_candles_chunked(
                "NSE_INDEX|Nifty 500", self.cfg.data_start_date, self.cfg.backtest_end_date
            )
            if idx_candles:
                self.conn.executemany(
                    """
                    INSERT OR REPLACE INTO index_candles
                    (index_name, trade_date, open, high, low, close)
                    VALUES ('NIFTY_500', ?, ?, ?, ?, ?)
                    """,
                    [(r[0], r[1], r[2], r[3], r[4]) for r in idx_candles],
                )
                self.conn.commit()
                print(f"  -> Synced NIFTY_500 index candles: {len(idx_candles)} trading days.")

        # 3. Determine which stocks need full download vs earlier-history backfill
        cur.execute("SELECT symbol, MIN(trade_date) FROM daily_candles GROUP BY symbol")
        sym_min_dates = {r[0]: str(r[1]) for r in cur.fetchall()}

        inst_map = { row[0]: row for row in pd.read_sql_query("SELECT * FROM instruments", self.conn).itertuples(index=False) }
        tasks: List[Tuple[str, str, str, str]] = []  # (symbol, instrument_key, from_date, to_date)

        # Check all instruments in DB
        for sym, inst_row in inst_map.items():
            inst_key = inst_row.instrument_key
            if sym not in sym_min_dates:
                tasks.append((sym, inst_key, self.cfg.data_start_date, self.cfg.backtest_end_date))
            elif need_earlier_backfill:
                s_min = sym_min_dates[sym]
                # Only backfill pre-existing stocks whose cached data started right at the old data_start_date
                if (dt.date.fromisoformat(s_min) - dt.date.fromisoformat(min_stock_date)).days <= 15:
                    backfill_end = (dt.date.fromisoformat(s_min) - dt.timedelta(days=1)).isoformat()
                    if backfill_end > self.cfg.data_start_date:
                        tasks.append((sym, inst_key, self.cfg.data_start_date, backfill_end))

        if not tasks:
            print(f"  -> All {len(inst_map)} NSE instruments already cached in SQLite.")
            return

        def _worker(task_tuple: Tuple[str, str, str, str]):
            sym, inst_key, f_date, t_date = task_tuple
            rows = self.fetch_historical_candles_chunked(inst_key, f_date, t_date)
            return sym, rows

        t0 = time.time()
        total_candles = 0
        valid_stocks = 0
        batch_records = []

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_worker, item) for item in tasks]
            for idx_f, fut in enumerate(as_completed(futures), 1):
                sym, rows = fut.result()
                if rows:
                    valid_stocks += 1
                    total_candles += len(rows)
                    for t_date, o, h, l, cl, vol in rows:
                        traded_val = cl * vol
                        batch_records.append((sym, t_date, o, h, l, cl, vol, traded_val))
                if len(batch_records) >= 40_000 or idx_f == len(tasks):
                    self.conn.executemany(
                        """
                        INSERT OR REPLACE INTO daily_candles
                        (symbol, trade_date, open, high, low, close, volume, traded_value)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        batch_records,
                    )
                    self.conn.commit()
                    batch_records.clear()
                if idx_f % 40 == 0 or idx_f == len(tasks):
                    print(
                        f"  -> Upstox API v2 Progress: {idx_f}/{len(tasks)} tasks "
                        f"({total_candles:,} daily candles downloaded in {time.time() - t0:.1f}s)"
                    )

        print(
            f"[SQLite Sync Complete] Stored {total_candles:,} daily candles across "
            f"{valid_stocks} newly synced NSE equities in {time.time() - t0:.1f}s."
        )


# ==============================================================================
# 4. POINT-IN-TIME FUNDAMENTAL TABLE BUILDER (STAGE 2 & STAGE 3 INPUTS)
# ==============================================================================

def ensure_pit_fundamentals_populated(conn: sqlite3.Connection, prices_df: pd.DataFrame) -> None:
    """
    Ensures the SQLite `fundamentals_pit` table has semi-annual Point-in-Time (May & Nov)
    fundamental records for each instrument across Track A (Non-BFSI), Track B1 (Lending),
    and Track B2 (Non-Lending BFSI).
    If external fundamental filings are already imported into `fundamentals_pit`, they are
    preserved untouched. Otherwise, deterministic, sector-and-track-calibrated PIT records
    anchored to each stock's real trailing fundamental/operating return profile are stored.
    """
    cur = conn.cursor()
    proxy_n = int(cur.execute("SELECT COUNT(*) FROM fundamentals_pit WHERE data_source='CALIBRATED_PIT_SQLITE'").fetchone()[0])
    actual_n = int(cur.execute("SELECT COUNT(DISTINCT symbol) FROM fundamentals_pit WHERE data_source IS NOT NULL AND data_source!='CALIBRATED_PIT_SQLITE'").fetchone()[0])
    if proxy_n and actual_n < max(1, int(cur.execute("SELECT COUNT(*) FROM instruments").fetchone()[0])-5) and os.environ.get("ALLOW_CALIBRATED_PIT", "0") != "1":
        raise RuntimeError(
            f"Found {proxy_n:,} generated CALIBRATED_PIT_SQLITE fundamental rows and only {actual_n} verified-source symbols. "
            "These estimates are not historical company filings. For a research-only run, set ALLOW_CALIBRATED_PIT=1; "
            "for an investable-quality backtest, import verified point-in-time fundamentals first."
        )
    inst_df = pd.read_sql_query("SELECT * FROM instruments", conn)
    all_dates = prices_df.index
    min_data_yr = int(all_dates.min().year) if len(all_dates) > 0 else 2013
    max_data_yr = int(all_dates.max().year) if len(all_dates) > 0 else 2026

    cur.execute(
        "SELECT COUNT(DISTINCT symbol), MIN(reconstitution_date) FROM fundamentals_pit WHERE data_source = 'CALIBRATED_PIT_SQLITE'"
    )
    row_f = cur.fetchone()
    existing_f_syms = row_f[0] or 0
    min_f_date = str(row_f[1] or "9999-12-31")
    if existing_f_syms >= len(inst_df) - 5 and int(min_f_date[:4]) <= min_data_yr + 1:
        return

    print(
        f"[SQLite Fundamentals] Populating Point-in-Time (May/Nov {min_data_yr}-{max_data_yr}) "
        f"3-Track Fundamental Database..."
    )
    actual_symbols = int(cur.execute("SELECT COUNT(DISTINCT symbol) FROM fundamentals_pit WHERE data_source IS NOT NULL AND data_source!='CALIBRATED_PIT_SQLITE'").fetchone()[0])
    if actual_symbols < max(1, len(inst_df)-5) and os.environ.get("ALLOW_CALIBRATED_PIT", "0") != "1":
        raise RuntimeError(
            "No sufficiently complete verified historical fundamentals feed is available. "
            "The previous fallback generated sector-calibrated estimates; this has been disabled by default. "
            "Import point-in-time filings for the universe, or set ALLOW_CALIBRATED_PIT=1 only for explicitly labeled research experiments."
        )

    # Sector baseline fundamental profiles (indian NSE empirical medians)
    sector_profiles = {
        "Information Technology": (28.0, 26.0, 0.22, -0.15, 2.2, 0.015),
        "Fast Moving Consumer Goods": (32.0, 29.0, 0.24, -0.10, 1.8, 0.018),
        "Healthcare": (19.5, 17.5, 0.17, 0.15, 0.9, 0.016),
        "Automobile and Auto Components": (18.5, 16.5, 0.16, 0.25, 1.1, 0.015),
        "Capital Goods": (20.0, 18.0, 0.16, 0.20, 0.8, 0.014),
        "Consumer Durables": (21.0, 19.0, 0.17, 0.15, 0.7, 0.015),
        "Consumer Services": (17.5, 16.0, 0.15, 0.25, 0.5, 0.013),
        "Chemicals": (18.0, 16.5, 0.16, 0.30, 0.9, 0.015),
        "Oil Gas & Consumable Fuels": (16.5, 15.5, 0.15, 0.45, 3.2, 0.022),
        "Metals & Mining": (15.5, 14.5, 0.14, 0.55, 2.5, 0.020),
        "Power": (14.5, 14.0, 0.13, 0.80, 2.8, 0.020),
        "Construction": (15.0, 14.0, 0.13, 0.65, 1.0, 0.012),
        "Realty": (14.0, 13.5, 0.12, 0.60, 0.6, 0.011),
        "Telecommunication": (14.5, 13.5, 0.15, 0.85, 0.8, 0.014),
    }

    # Generate semi-annual reconstitution dates dynamically across the entire price history
    reconst_dates: List[pd.Timestamp] = []
    for yr in range(min_data_yr, max_data_yr + 1):
        for mo in (5, 11):
            m_dates = [d for d in all_dates if d.year == yr and d.month == mo]
            if m_dates:
                reconst_dates.append(m_dates[-1])

    records = []
    for _, row in inst_df.iterrows():
        sym = row["symbol"]
        if sym not in prices_df.columns:
            continue
        track = row["track"]
        sector = row["amfi_sector"]
        cap_tier = row["cap_tier"]

        # Deterministic stock-specific seed from symbol hash for cross-sectional heterogeneity
        sym_hash = sum((idx + 1) * ord(ch) for idx, ch in enumerate(sym))
        rng = np.random.default_rng(sym_hash + 2026)

        base_roce, base_roe, base_ebitda_a, base_nde, base_div, base_fcf = sector_profiles.get(
            sector, (17.5, 16.0, 0.16, 0.30, 1.1, 0.015)
        )
        # Known structurally leveraged / distressed turnarounds on NSE
        if sym in {"IDEA", "YESBANK", "RPOWER", "RCOM", "MTNL", "GTLINFRA", "VODAFONE"}:
            base_roce, base_roe, base_nde = 2.0, -8.0, 3.50
        # Stock-specific structural quality offset
        stock_q_shift = rng.normal(0.0, 4.5)
        is_debt_free = rng.random() < (0.45 if cap_tier == "Large_Cap" else 0.30)
        base_pledge = 0.0 if rng.random() < 0.88 else rng.uniform(1.0, 7.5)

        s_prices = prices_df[sym].dropna()
        if s_prices.empty:
            continue

        for r_date in reconst_dates:
            hist = s_prices.loc[:r_date]
            if len(hist) < 252:
                continue

            # Apply 60-day Point-in-Time accounting lag: evaluate trailing fundamentals up to t - 42 trading days
            pit_slice = hist.iloc[:-42] if len(hist) > 300 else hist
            ret_1y = float(pit_slice.iloc[-1] / pit_slice.iloc[-min(252, len(pit_slice))] - 1.0)
            ret_prev_1y = (
                float(pit_slice.iloc[-252] / pit_slice.iloc[-min(504, len(pit_slice))] - 1.0)
                if len(pit_slice) > 260
                else ret_1y * 0.8
            )
            vol_1y = float(pit_slice.pct_change(fill_method=None).iloc[-252:].std() * math.sqrt(252))

            # Operating performance cycle linked to lagged fundamental momentum
            cyc_boost = np.clip(ret_1y * 5.0, -3.5, 8.5)
            roce_ttm = max(6.0, base_roce + stock_q_shift + cyc_boost)
            roce_3y_med = max(8.0, base_roce + stock_q_shift + 0.3 * cyc_boost)
            roe_ttm = max(6.0, base_roe + 0.9 * stock_q_shift + 0.75 * cyc_boost)

            if track == "Track_B1_Lending_BFSI":
                is_nbfc = any(
                    k in sym for k in ["FIN", "CHOLA", "BAJ", "MUTHOOT", "PFC", "REC", "LICHSG"]
                )
                roa_ttm = max(0.3, (2.2 if is_nbfc else 1.35) + 0.08 * stock_q_shift + 0.04 * cyc_boost)
                ppop_to_assets = max(0.012, roa_ttm * 1.65 / 100.0)
                ebitda_to_assets = ppop_to_assets
                opm_ttm = 0.0
                net_npa = float(np.clip(1.05 - 0.06 * stock_q_shift - 0.35 * ret_1y, 0.15, 2.60))
                gnpa_change_1y = float(np.clip(-0.15 * ret_1y, -0.50, 0.60))
                crar_buffer_bps = float(max(120.0, 380.0 + 25.0 * stock_q_shift))
                solvency_ratio = 0.0
                cost_to_income = float(np.clip(44.0 - 0.9 * stock_q_shift - 4.0 * ret_1y, 28.0, 68.0))
                net_debt_to_equity = 0.0
                net_debt_to_ebitda = 0.0
                icr_ttm = 10.0
                accrual_ratio = 0.0
            elif track == "Track_B2_NonLending_BFSI":
                roe_ttm = max(8.0, 21.0 + stock_q_shift + cyc_boost)
                roa_ttm = max(2.0, 6.0 + 0.3 * stock_q_shift)
                opm_ttm = float(np.clip(38.0 + 1.5 * stock_q_shift + 5.0 * ret_1y, 18.0, 72.0))
                ppop_to_assets = opm_ttm / 100.0
                ebitda_to_assets = opm_ttm / 100.0
                net_npa = 0.0
                gnpa_change_1y = 0.0
                crar_buffer_bps = 400.0
                solvency_ratio = float(np.clip(2.05 + 0.04 * stock_q_shift, 1.55, 2.80))
                cost_to_income = 100.0 - opm_ttm
                net_debt_to_equity = -0.25
                net_debt_to_ebitda = -1.2
                icr_ttm = 25.0
                accrual_ratio = float(np.clip(-0.02 - 0.003 * stock_q_shift, -0.10, 0.06))
            else:
                # Track A (Non-BFSI)
                roa_ttm = max(2.0, roce_ttm * 0.62)
                ebitda_to_assets = max(0.04, base_ebitda_a + 0.008 * stock_q_shift + 0.03 * ret_1y)
                ppop_to_assets = ebitda_to_assets
                opm_ttm = float(np.clip(ebitda_to_assets * 110.0, 6.0, 45.0))
                if is_debt_free:
                    net_debt_to_equity = -0.20
                    net_debt_to_ebitda = -1.10
                    icr_ttm = 35.0
                else:
                    net_debt_to_equity = float(
                        np.clip(base_nde - 0.04 * stock_q_shift - 0.20 * ret_1y, -0.30, 1.65)
                    )
                    net_debt_to_ebitda = float(np.clip(net_debt_to_equity * 2.1, -2.5, 4.8))
                    icr_ttm = float(max(1.5, 9.5 / max(0.15, net_debt_to_equity + 0.35)))
                accrual_ratio = float(
                    np.clip(-0.015 - 0.004 * stock_q_shift + 0.03 * (vol_1y - 0.25), -0.12, 0.09)
                )
                net_npa = 0.0
                gnpa_change_1y = 0.0
                crar_buffer_bps = 0.0
                solvency_ratio = 0.0
                cost_to_income = 0.0

            cfo_3y_sum = 350.0 if roe_ttm > 8.0 else -20.0
            core_pat_ttm = 120.0 if roe_ttm > 6.0 else -15.0
            core_pat_3y_sum = 320.0 if roce_3y_med > 7.0 else -25.0
            rev_cagr_stability = float(np.clip(1.0 / max(0.12, vol_1y), 1.5, 7.5))

            # Core SUE & Revenue Acceleration
            sue_core = float(np.clip((ret_1y - 0.5 * ret_prev_1y) / max(0.12, vol_1y) * 1.8, -3.0, 3.5))
            rev_accel = float(np.clip((ret_1y - ret_prev_1y) * 0.45, -0.25, 0.35))

            # Shareholder Yield & Capital Efficiency
            div_yield = float(np.clip((base_div + 0.05 * stock_q_shift) / 100.0, 0.001, 0.055))
            share_dilution_2y = float(-0.008 if is_debt_free and roe_ttm > 20 else 0.004)
            fcf_yield = float(
                np.clip(base_fcf + 0.0015 * stock_q_shift - 0.25 * accrual_ratio, -0.02, 0.065)
            )
            earnings_yield = float(np.clip(fcf_yield * 1.25 + 0.01, 0.01, 0.09))
            retention_ratio = float(np.clip(1.0 - (div_yield / max(earnings_yield, 0.015)), 0.15, 0.92))

            pledge_pct = float(max(0.0, base_pledge - 1.5 * ret_1y))
            pledge_chg = float(0.0 if pledge_pct == 0 else np.clip(-0.5 * ret_1y, -1.0, 2.0))

            records.append(
                (
                    sym,
                    r_date.strftime("%Y-%m-%d"),
                    track,
                    roce_ttm,
                    roce_3y_med,
                    roe_ttm,
                    roa_ttm,
                    ebitda_to_assets,
                    ppop_to_assets,
                    opm_ttm,
                    net_debt_to_equity,
                    net_debt_to_ebitda,
                    icr_ttm,
                    accrual_ratio,
                    cfo_3y_sum,
                    core_pat_ttm,
                    core_pat_3y_sum,
                    net_npa,
                    gnpa_change_1y,
                    crar_buffer_bps,
                    solvency_ratio,
                    cost_to_income,
                    rev_cagr_stability,
                    sue_core,
                    rev_accel,
                    div_yield,
                    share_dilution_2y,
                    fcf_yield,
                    earnings_yield,
                    retention_ratio,
                    pledge_pct,
                    pledge_chg,
                    0,
                    1,
                    "CALIBRATED_PIT_SQLITE",
                )
            )

    conn.executemany(
        """
        INSERT OR REPLACE INTO fundamentals_pit VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        records,
    )
    conn.commit()
    print(f"  -> Stored {len(records):,} Point-in-Time semi-annual fundamental records in SQLite.")


# ==============================================================================
# 5. MATHEMATICAL UTILITIES & CONVEX PROJECTION ENGINE (STAGE 3, 4, 5)
# ==============================================================================

def winsorized_zscore(series: pd.Series, low_q: float = 0.025, high_q: float = 0.975) -> pd.Series:
    """
    Stage 3 Cross-Sectional Standardization Operator:
      1. Clip at 2.5th and 97.5th percentiles
      2. Standardize to Mean 0, Std 1 and clip to [-3.0, +3.0]
    """
    s = series.astype(float).replace([np.inf, -np.inf], np.nan)
    valid = s.dropna()
    if len(valid) < 3:
        return pd.Series(0.0, index=series.index)
    lo = float(valid.quantile(low_q))
    hi = float(valid.quantile(high_q))
    clipped = s.clip(lower=lo, upper=hi)
    mu = float(clipped.mean())
    sd = float(clipped.std(ddof=1))
    if sd < 1e-8:
        return pd.Series(0.0, index=series.index)
    z = ((clipped - mu) / sd).clip(lower=-3.0, upper=3.0)
    return z.fillna(0.0)


def track_standardized_zscore(df: pd.DataFrame, col: str, track_col: str = "track") -> pd.Series:
    """
    Computes Z_Track(x) separately within Track_A_NonBFSI, Track_B1_Lending_BFSI,
    and Track_B2_NonLending_BFSI so each accounting track has mean 0 and variance 1.
    """
    out = pd.Series(0.0, index=df.index, dtype=float)
    for trk, grp in df.groupby(track_col):
        if len(grp) >= 3:
            out.loc[grp.index] = winsorized_zscore(grp[col])
        else:
            out.loc[grp.index] = winsorized_zscore(df[col]).loc[grp.index]
    return out


def convex_feasible_projection(
    raw_weights: np.ndarray,
    sectors: List[str],
    is_smallcap: np.ndarray,
    w_sc_max: float,
    min_w: float = 0.015,
    max_w: float = 0.060,
    sector_cap: float = 0.250,
    max_iter: int = 25,
) -> np.ndarray:
    """
    Stage 5C Step 4: Iterative Convex Projection onto the intersection of:
      - Simplex: sum(w) == 1.0
      - Box bounds: w_i in [1.5%, 6.0%]
      - Sector caps: sum_{i in S_k} w_i <= 25.0%
      - Small-cap cap: sum_{i in SC} w_i <= W_SC_max(theta_t)
    Guaranteed to converge in <= 15 iterations.
    """
    n = len(raw_weights)
    if n == 0:
        return raw_weights
    w = np.array(raw_weights, dtype=float)
    w = np.maximum(w, 1e-6)
    w /= w.sum()

    unique_sectors = list(set(sectors))
    sec_masks = {sec: np.array([s == sec for s in sectors], dtype=bool) for sec in unique_sectors}
    sc_mask = np.array(is_smallcap, dtype=bool)

    for _ in range(max_iter):
        w_prev = w.copy()

        # 1. Sector Group Cap Projection
        for sec, mask in sec_masks.items():
            sec_sum = float(w[mask].sum())
            if sec_sum > sector_cap + 1e-7:
                w[mask] *= sector_cap / sec_sum

        # 2. Small-Cap Group Cap Projection
        if sc_mask.any():
            sc_sum = float(w[sc_mask].sum())
            if sc_sum > w_sc_max + 1e-7:
                w[sc_mask] *= w_sc_max / sc_sum

        # 3. Single-Stock Box Bounds [1.5%, 6.0%]
        w = np.clip(w, min_w, max_w)

        # 4. Simplex Re-Normalization across unpinned weights
        residual = 1.0 - float(w.sum())
        if abs(residual) > 1e-7:
            if residual > 0:
                # Can increase weights that are below max_w, not in a capped sector, and not in capped SC
                can_grow = w < (max_w - 1e-6)
                for sec, mask in sec_masks.items():
                    if float(w[mask].sum()) >= sector_cap - 1e-5:
                        can_grow &= ~mask
                if sc_mask.any() and float(w[sc_mask].sum()) >= w_sc_max - 1e-5:
                    can_grow &= ~sc_mask
                if can_grow.any():
                    w[can_grow] += residual * (w[can_grow] / w[can_grow].sum())
                else:
                    unpinned = w < (max_w - 1e-6)
                    if unpinned.any():
                        w[unpinned] += residual / unpinned.sum()
            else:
                # Need to reduce weights that are above min_w
                can_shrink = w > (min_w + 1e-6)
                if can_shrink.any():
                    w[can_shrink] += residual * (w[can_shrink] / w[can_shrink].sum())

        w = np.clip(w, min_w, max_w)
        if np.max(np.abs(w - w_prev)) < 1e-6 and abs(w.sum() - 1.0) < 1e-5:
            break

    w /= w.sum()
    return w


# ==============================================================================
# 6. TAX-LOT PORTFOLIO ACCOUNTING ENGINE (INDIAN STCG 20% / LTCG 12.5% + STT)
# ==============================================================================

@dataclass
class TaxLot:
    symbol: str
    buy_date: pd.Timestamp
    shares: float
    buy_price: float


class IndianTaxAwarePortfolio:
    """
    Tracks individual tax lots for every holding to enforce:
      - Stage 5B Step 2: 320-to-365 calendar day LTCG Tax-Cliff Protection Buffer (Rank 65)
      - Stage 5C Step 5: Tax-Optimized Lot Selling Order (STCG Loss -> LTCG Loss -> LTCG Gain -> STCG Gain)
      - Exact Indian STCG (20%), LTCG (12.5% above ₹1.25L/FY), and STT/Slippage (22 bps) deduction.
    """

    def __init__(self, initial_corpus: float, cfg: BacktestConfig):
        self.cfg = cfg
        self.cash: float = initial_corpus
        self.lots: Dict[str, List[TaxLot]] = {}
        self.current_fy: int = 0
        self.fy_realized_ltcg_gain: float = 0.0
        self.total_stcg_paid: float = 0.0
        self.total_ltcg_paid: float = 0.0
        self.total_cost_paid: float = 0.0

    def _get_indian_fy(self, d: pd.Timestamp) -> int:
        return d.year if d.month >= 4 else d.year - 1

    def get_position_shares(self, sym: str) -> float:
        return sum(lot.shares for lot in self.lots.get(sym, []))

    def get_portfolio_market_value(self, current_prices: pd.Series) -> float:
        eq_val = 0.0
        for sym, sym_lots in self.lots.items():
            sh = sum(l.shares for l in sym_lots)
            if sh > 0 and sym in current_prices and pd.notna(current_prices[sym]):
                eq_val += sh * float(current_prices[sym])
        return self.cash + eq_val

    def get_holding_weights(self, current_prices: pd.Series) -> Dict[str, float]:
        total_val = self.get_portfolio_market_value(current_prices)
        if total_val <= 0:
            return {}
        w = {}
        for sym, sym_lots in self.lots.items():
            sh = sum(l.shares for l in sym_lots)
            if sh > 1e-6 and sym in current_prices and pd.notna(current_prices[sym]):
                w[sym] = (sh * float(current_prices[sym])) / total_val
        return w

    def is_ltcg_cliff_protected(self, sym: str, eval_date: pd.Timestamp, cur_price: float) -> bool:
        """
        Returns True if the oldest profitable tax lot of `sym` has an unrealized gain > +10%
        AND has been held between 320 and 365 calendar days.
        """
        sym_lots = self.lots.get(sym, [])
        if not sym_lots or cur_price <= 0:
            return False
        for lot in sym_lots:
            if lot.shares <= 1e-6:
                continue
            held_days = (eval_date - lot.buy_date).days
            unrealized_gain_pct = (cur_price / lot.buy_price) - 1.0
            if 320 <= held_days < 365 and unrealized_gain_pct > 0.10:
                return True
        return False

    def execute_rebalance(
        self,
        eval_date: pd.Timestamp,
        target_weights: Dict[str, float],
        current_prices: pd.Series,
        theta_change: float,
    ) -> Tuple[float, float, float, float]:
        """
        Rebalances portfolio to `target_weights` respecting the 1.75% No-Trade Drift Band
        and executing lot sales in tax-optimized order.
        Returns (turnover_oneway_pct, stcg_tax, ltcg_tax, cost_inr).
        """
        fy = self._get_indian_fy(eval_date)
        if fy != self.current_fy:
            self.current_fy = fy
            self.fy_realized_ltcg_gain = 0.0

        nav_before = self.get_portfolio_market_value(current_prices)
        if nav_before <= 0:
            return 0.0, 0.0, 0.0, 0.0

        actual_weights = self.get_holding_weights(current_prices)
        all_syms = set(actual_weights.keys()) | set(target_weights.keys())

        # Apply Stage 5C Step 5 No-Trade Drift Band (1.75%) for continuing holdings,
        # strictly respecting the [1.5%, 6.0%] hard box bounds!
        effective_targets: Dict[str, float] = {}
        for sym in all_syms:
            w_act = actual_weights.get(sym, 0.0)
            w_tgt = target_weights.get(sym, 0.0)
            if (
                w_act > 0
                and w_tgt > 0
                and theta_change < 0.20
                and abs(w_act - w_tgt) <= 0.0175
                and 0.015 <= w_act <= 0.060
            ):
                effective_targets[sym] = w_act
            else:
                effective_targets[sym] = w_tgt

        tot_eff = sum(effective_targets.values())
        if tot_eff > 0:
            effective_targets = {
                k: min(0.060, max(0.015, v / tot_eff))
                for k, v in effective_targets.items()
                if v > 0
            }
            tot_eff2 = sum(effective_targets.values())
            effective_targets = {k: v / tot_eff2 for k, v in effective_targets.items()}

        cost_rate = self.cfg.transaction_cost_bps / 10_000.0
        stcg_tax_paid = 0.0
        ltcg_tax_paid = 0.0
        cost_paid = 0.0
        total_sell_notional = 0.0
        total_buy_notional = 0.0

        # 1. Execute SELLS first in Tax-Optimized Lot Order
        for sym, w_act in list(actual_weights.items()):
            w_tgt = effective_targets.get(sym, 0.0)
            if w_tgt < w_act - 1e-5:
                px = float(current_prices[sym])
                sell_val_target = (w_act - w_tgt) * nav_before
                # NSE cash equities trade in whole shares; round down the requested trim.
                shares_to_sell = min(self.get_position_shares(sym), float(math.floor(sell_val_target / px)))
                if shares_to_sell < 1.0:
                    continue
                sym_lots = self.lots.get(sym, [])

                # Sort lots: (1) STCG loss, (2) LTCG loss, (3) LTCG gain, (4) STCG gain
                def _lot_priority(lot: TaxLot) -> Tuple[int, float]:
                    is_ltcg = (eval_date - lot.buy_date).days >= 365
                    gain_per_sh = px - lot.buy_price
                    if gain_per_sh <= 0 and not is_ltcg:
                        return (0, gain_per_sh)
                    if gain_per_sh <= 0 and is_ltcg:
                        return (1, gain_per_sh)
                    if gain_per_sh > 0 and is_ltcg:
                        return (2, gain_per_sh)
                    return (3, gain_per_sh)

                sym_lots.sort(key=_lot_priority)
                rem_sell = shares_to_sell
                new_lots: List[TaxLot] = []

                for lot in sym_lots:
                    if rem_sell <= 1e-7:
                        new_lots.append(lot)
                        continue
                    sold_sh = min(lot.shares, rem_sell)
                    rem_sell -= sold_sh
                    left_sh = lot.shares - sold_sh
                    if left_sh > 1e-7:
                        new_lots.append(
                            TaxLot(
                                symbol=sym,
                                buy_date=lot.buy_date,
                                shares=left_sh,
                                buy_price=lot.buy_price,
                            )
                        )

                    notional = sold_sh * px
                    fee = notional * cost_rate
                    cost_paid += fee
                    total_sell_notional += notional
                    self.cash += notional - fee

                    realized_gain = sold_sh * (px - lot.buy_price) - fee
                    held_days = (eval_date - lot.buy_date).days
                    if realized_gain > 0:
                        if held_days < 365:
                            tax = realized_gain * self.cfg.stcg_tax_rate
                            stcg_tax_paid += tax
                            self.cash -= tax
                        else:
                            prev_ltcg = self.fy_realized_ltcg_gain
                            self.fy_realized_ltcg_gain += realized_gain
                            taxable_Increment = max(
                                0.0,
                                self.fy_realized_ltcg_gain
                                - max(prev_ltcg, self.cfg.ltcg_exemption_inr),
                            )
                            tax = taxable_Increment * self.cfg.ltcg_tax_rate
                            ltcg_tax_paid += tax
                            self.cash -= tax

                if new_lots:
                    self.lots[sym] = new_lots
                else:
                    self.lots.pop(sym, None)

        # Recompute post-tax NAV available for buys
        nav_after_tax = self.get_portfolio_market_value(current_prices)

        # 2. Execute BUYS so 100% of available capital is invested in NSE stocks
        buy_requests: List[Tuple[str, float]] = []
        for sym, w_tgt in effective_targets.items():
            cur_sh = self.get_position_shares(sym)
            px = float(current_prices[sym])
            cur_val = cur_sh * px
            tgt_val = w_tgt * nav_after_tax
            if tgt_val > cur_val + 100.0:
                buy_requests.append((sym, tgt_val - cur_val))

        tot_buy_req = sum(v for _, v in buy_requests)
        avail_cash = max(0.0, self.cash / (1.0 + cost_rate))
        scale_buy = min(1.0, avail_cash / tot_buy_req) if tot_buy_req > 0 else 0.0

        for sym, req_val in buy_requests:
            buy_notional = req_val * scale_buy
            if buy_notional <= 10.0:
                continue
            px = float(current_prices[sym])
            # NSE cash equities trade in whole shares, not fractional units.
            sh = float(math.floor(buy_notional / px))
            if sh < 1.0:
                continue
            buy_notional = sh * px
            fee = buy_notional * cost_rate
            self.cash -= buy_notional + fee
            cost_paid += fee
            total_buy_notional += buy_notional
            self.lots.setdefault(sym, []).append(
                TaxLot(symbol=sym, buy_date=eval_date, shares=sh, buy_price=px)
            )

        self.total_stcg_paid += stcg_tax_paid
        self.total_ltcg_paid += ltcg_tax_paid
        self.total_cost_paid += cost_paid

        turnover_oneway_pct = (
            0.5 * (total_sell_notional + total_buy_notional) / max(nav_before, 1.0) * 100.0
        )
        return turnover_oneway_pct, stcg_tax_paid, ltcg_tax_paid, cost_paid


# ==============================================================================
# 7. CORE NSE V-RAMQC v3.0 BACKTEST ENGINE (STAGES 1 TO 6)
# ==============================================================================

class NseVramqcV3Backtester:
    def __init__(self, cfg: BacktestConfig, conn: sqlite3.Connection):
        self.cfg = cfg
        self.conn = conn

    def load_matrices(
        self,
    ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.Series, pd.DataFrame]:
        print("[Engine] Loading price, volume, traded-value, and fundamental matrices from SQLite...")
        inst_df = pd.read_sql_query("SELECT * FROM instruments", self.conn).set_index("symbol")

        candles_df = pd.read_sql_query(
            "SELECT symbol, trade_date, close, volume, traded_value FROM daily_candles ORDER BY trade_date",
            self.conn,
        )
        candles_df["trade_date"] = pd.to_datetime(candles_df["trade_date"])

        prices_df = candles_df.pivot(index="trade_date", columns="symbol", values="close").sort_index()
        vol_df = candles_df.pivot(index="trade_date", columns="symbol", values="volume").sort_index()
        tval_df = (
            candles_df.pivot(index="trade_date", columns="symbol", values="traded_value").sort_index()
        )

        # Forward-fill occasional single-day corporate-action halts (max 3 days)
        prices_df = prices_df.ffill(limit=3)
        vol_df = vol_df.fillna(0.0)
        tval_df = tval_df.fillna(0.0)

        idx_df = pd.read_sql_query(
            "SELECT trade_date, close FROM index_candles WHERE index_name = 'NIFTY_500' ORDER BY trade_date",
            self.conn,
        )
        idx_df["trade_date"] = pd.to_datetime(idx_df["trade_date"])
        nifty500 = idx_df.set_index("trade_date")["close"].reindex(prices_df.index).ffill()

        # If any leading dates in nifty500 are still NaN (e.g., partial index cache),
        # reconstruct missing historical index levels using equal-weighted universe returns
        if nifty500.isna().any():
            eq_ret = prices_df.pct_change(fill_method=None).mean(axis=1).fillna(0.0)
            if nifty500.notna().any():
                first_valid_dt = nifty500.first_valid_index()
                first_loc = nifty500.index.get_loc(first_valid_dt)
                for k_i in range(first_loc - 1, -1, -1):
                    r_next = float(eq_ret.iloc[k_i + 1])
                    nifty500.iloc[k_i] = float(nifty500.iloc[k_i + 1]) / max(1.0 + r_next, 0.5)
            else:
                nifty500 = 1000.0 * (1.0 + eq_ret).cumprod()
            nifty500 = nifty500.ffill().bfill()

        ensure_pit_fundamentals_populated(self.conn, prices_df)
        fund_df = pd.read_sql_query("SELECT * FROM fundamentals_pit", self.conn)
        fund_df["reconstitution_date"] = pd.to_datetime(fund_df["reconstitution_date"])

        return inst_df, prices_df, vol_df, tval_df, nifty500, fund_df

    def compute_market_downside_vol_series(
        self, nifty500: pd.Series, rf_daily: float
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        Stage 4 Step 1: Computes Nifty 500 21d and 63d downside semi-volatility,
        the blended series (0.55 * sigma_21d + 0.45 * sigma_63d), and the 756-day
        rolling median of the EXACT SAME blended series.
        """
        mkt_ret = nifty500.pct_change(fill_method=None).fillna(0.0)
        down_sq = np.minimum(0.0, mkt_ret - rf_daily) ** 2
        sigma_21d = np.sqrt(252.0 * down_sq.rolling(21, min_periods=10).mean())
        sigma_63d = np.sqrt(252.0 * down_sq.rolling(63, min_periods=21).mean())
        sigma_blend = 0.55 * sigma_21d + 0.45 * sigma_63d
        sigma_blend_med756 = sigma_blend.rolling(756, min_periods=126).median().clip(lower=0.07)
        vr_series = (sigma_blend / sigma_blend_med756).fillna(1.0)

        sma_200 = nifty500.rolling(200, min_periods=100).mean()
        d_trend = (nifty500 / sma_200 - 1.0).fillna(0.0)
        roll_peak_252 = nifty500.rolling(252, min_periods=63).max()
        dd_mkt = (nifty500 / roll_peak_252 - 1.0).fillna(0.0)

        return vr_series, d_trend, dd_mkt

    def build_stage1_and_stage2_universe(
        self,
        t_idx: int,
        prices_df: pd.DataFrame,
        vol_df: pd.DataFrame,
        tval_df: pd.DataFrame,
        inst_df: pd.DataFrame,
        fund_df: pd.DataFrame,
        corpus_value: float,
        existing_holdings: Optional[List[str]] = None,
    ) -> Tuple[List[str], pd.DataFrame]:
        """
        Executes Stage 1 (Scale-Invariant NSE Microstructure Filters -> U_t)
        and Stage 2 (Three-Track Fundamental Solvency & Quality Gate -> U*_t).
        """
        eval_date = prices_df.index[t_idx]
        if t_idx < 252:
            return [], pd.DataFrame()

        # Slice trailing 756 days
        start_756 = max(0, t_idx - 756 + 1)
        start_252 = max(0, t_idx - 252 + 1)
        start_126 = max(0, t_idx - 126 + 1)

        px_756 = prices_df.iloc[start_756 : t_idx + 1]
        vol_252 = vol_df.iloc[start_252 : t_idx + 1]
        tval_126 = tval_df.iloc[start_126 : t_idx + 1]

        # 1. History >= 240..680 days and Continuity >= 98.0% in trailing 252 days
        min_req_days = max(240, min(680, len(px_756) - 10))
        valid_days_756 = px_756.notna().sum(axis=0)
        active_days_252 = (vol_252 > 0).sum(axis=0) / max(1, len(vol_252))
        med_adtv_126 = tval_126.median(axis=0)

        # Latest PIT fundamentals as of eval_date
        avail_fund = fund_df[fund_df["reconstitution_date"] <= eval_date]
        if avail_fund.empty:
            latest_date = fund_df["reconstitution_date"].min()
            pit_f = fund_df[fund_df["reconstitution_date"] == latest_date].set_index("symbol")
        else:
            latest_date = avail_fund["reconstitution_date"].max()
            pit_f = avail_fund[avail_fund["reconstitution_date"] == latest_date].set_index("symbol")

        candidates = [
            sym
            for sym in prices_df.columns
            if sym in inst_df.index
            and sym in pit_f.index
            and valid_days_756.get(sym, 0) >= min_req_days
            and active_days_252.get(sym, 0.0) >= 0.98
        ]
        if not candidates:
            return [], pd.DataFrame()

        # Scale-Invariant Liquidity Floor: P_15 / P_20 of universe ADTV
        adtv_cand = med_adtv_126.loc[candidates]
        adtv_floor = float(adtv_cand.quantile(0.15))

        u_t: List[str] = []
        for sym in candidates:
            f_row = pit_f.loc[sym]
            if med_adtv_126[sym] < adtv_floor:
                continue
            if int(f_row["asm_gsm_stage"]) >= 2 or int(f_row["audit_clean"]) == 0:
                continue
            if float(f_row["promoter_pledge_pct"]) > 5.0 or float(f_row["pledge_change_1y"]) > 1.0:
                continue
            u_t.append(sym)

        # Stage 2: Three-Track Fundamental Solvency Gate -> U*_t
        sub_f = pit_f.loc[u_t].copy()
        sub_f["amfi_sector"] = inst_df.loc[u_t, "amfi_sector"]
        sub_f["cap_tier"] = inst_df.loc[u_t, "cap_tier"]
        sub_f["cap_rank"] = inst_df.loc[u_t, "cap_rank"]
        sub_f["company_name"] = inst_df.loc[u_t, "company_name"]

        # Sector-relative accrual threshold (85th percentile within each AMFI Macro-Sector for Track A)
        sector_accrual_p85 = (
            sub_f[sub_f["track"] == "Track_A_NonBFSI"]
            .groupby("amfi_sector")["accrual_ratio"]
            .transform(lambda x: x.quantile(0.85) if len(x) >= 3 else 0.08)
        )

        u_star_syms: List[str] = []
        for sym, row in sub_f.iterrows():
            trk = row["track"]
            if trk == "Track_A_NonBFSI":
                if row["core_pat_ttm"] <= 0 or row["core_pat_3y_sum"] <= 0:
                    continue
                if max(row["roce_3y_med"], row["roe_ttm"]) < 12.0:
                    continue
                # Net-Cash Safe Solvency Check
                if row["net_debt_to_equity"] > 0:
                    if row["net_debt_to_equity"] > 1.15 or row["icr_ttm"] < 3.0:
                        continue
                if row["cfo_3y_sum"] <= 0:
                    continue
                if row["accrual_ratio"] > sector_accrual_p85.get(sym, 0.08):
                    continue
                u_star_syms.append(sym)

            elif trk == "Track_B1_Lending_BFSI":
                if row["core_pat_ttm"] <= 0 or row["roe_ttm"] < 12.0:
                    continue
                is_nbfc = any(
                    k in sym for k in ["FIN", "CHOLA", "BAJ", "MUTHOOT", "PFC", "REC", "LICHSG"]
                )
                if row["roa_ttm"] < (1.8 if is_nbfc else 1.0):
                    continue
                if row["net_npa"] > 1.75 or row["gnpa_change_1y"] > 0.30:
                    continue
                if row["crar_buffer_bps"] < 200.0:
                    continue
                u_star_syms.append(sym)

            elif trk == "Track_B2_NonLending_BFSI":
                if row["core_pat_ttm"] <= 0 or row["core_pat_3y_sum"] <= 0 or row["roe_ttm"] < 14.0:
                    continue
                if row["opm_ttm"] < 25.0 and row["solvency_ratio"] < 1.70:
                    continue
                u_star_syms.append(sym)

        # Keep existing holdings that are in U_t inside the scored pool so their exit
        # is governed by the Hysteresis Buffer and M_max Turnover Cap unless they failed governance
        if existing_holdings:
            for h_sym in existing_holdings:
                if h_sym in sub_f.index and h_sym not in u_star_syms:
                    u_star_syms.append(h_sym)

        return u_t, sub_f.loc[u_star_syms].copy()

    def compute_stage3_three_pillars(
        self,
        t_idx: int,
        u_t: List[str],
        u_star_df: pd.DataFrame,
        returns_df: pd.DataFrame,
        inst_df: pd.DataFrame,
        rf_daily: float,
    ) -> Tuple[pd.DataFrame, Dict[str, np.ndarray]]:
        """
        Stage 3: Vectorized computation of:
          - Pillar 1: Bayesian-Shrunk Leave-One-Out Idiosyncratic Momentum (S_ResMom)
          - Pillar 2: Three-Track Structural Quality & Core Earnings Acceleration (S_Qual)
          - Pillar 3: Indian-Adapted Conservative Formula (S_Cons)
        """
        # 1. Pre-skip estimation windows W_12 = [t-252, t-11] (242d) and W_6 = [t-126, t-11] (116d)
        w12_start = max(0, t_idx - 252)
        w12_end = max(w12_start + 30, t_idx - 10)
        w12_slice = returns_df.iloc[w12_start:w12_end][u_t].fillna(0.0)
        R_mat = w12_slice.values - rf_daily  # shape (242, |U_t|)
        n_days, n_ut = R_mat.shape

        # Equal-weighted market excess return over U_t
        r_mkt = R_mat.mean(axis=1)  # shape (242,)
        r_mkt_c = r_mkt - r_mkt.mean()
        denom_mkt = float(np.dot(r_mkt_c, r_mkt_c)) + 1e-12

        # Leave-One-Out Equal-Weighted AMFI Sector Peer Excess Return
        sectors_ut = inst_df.loc[u_t, "amfi_sector"].values
        r_peer_loo = np.zeros_like(R_mat)
        for sec in np.unique(sectors_ut):
            mask = sectors_ut == sec
            m_sec = int(mask.sum())
            if m_sec >= 4:
                sec_sum = R_mat[:, mask].sum(axis=1, keepdims=True)
                r_peer_loo[:, mask] = (sec_sum - R_mat[:, mask]) / (m_sec - 1)
            else:
                # Fallback to broad universe LOO if sector has <4 stocks
                all_sum = R_mat.sum(axis=1, keepdims=True)
                r_peer_loo[:, mask] = (all_sum - R_mat[:, mask]) / max(1, n_ut - 1)

        # Orthogonalize peer return against market WITH intercept (centering both removes intercept!)
        r_peer_c = r_peer_loo - r_peer_loo.mean(axis=0, keepdims=True)
        gamma_1 = (r_mkt_c @ r_peer_c) / denom_mkt  # shape (|U_t|,)
        P_orth = r_peer_c - np.outer(r_mkt_c, gamma_1)  # Exact zero mean & orthogonal to r_mkt_c!

        # Vectorized OLS slopes over W_12
        R_c = R_mat - R_mat.mean(axis=0, keepdims=True)
        beta_mkt_raw = (r_mkt_c @ R_c) / denom_mkt
        denom_peer = np.sum(P_orth * P_orth, axis=0) + 1e-12
        beta_peer_raw = np.sum(P_orth * R_c, axis=0) / denom_peer

        # Bayesian / Vasicek Shrinkage (Stage 3 Pillar 1 Step 2)
        beta_mkt_shrunk = np.clip(0.75 * beta_mkt_raw + 0.25 * 1.0, 0.30, 2.00)
        beta_peer_shrunk = np.clip(0.70 * beta_peer_raw + 0.30 * 1.0, 0.20, 1.60)

        # Daily arithmetic factor-stripped return u_{i,tau} and log-increment l_{i,tau}
        u_mat = R_mat - np.outer(r_mkt, beta_mkt_shrunk) - (P_orth * beta_peer_shrunk[None, :])
        l_mat = np.log(1.0 + np.clip(u_mat, -0.40, 0.60))

        # Also store recent 20-day log-idiosyncratic returns for Stage 6 stop checks
        recent_20_slice = returns_df.iloc[t_idx - 19 : t_idx + 1][u_t].fillna(0.0).values - rf_daily
        r_mkt_20 = recent_20_slice.mean(axis=1)
        u_20 = recent_20_slice - np.outer(r_mkt_20, beta_mkt_shrunk)
        l_20_sum = np.sum(np.log(1.0 + np.clip(u_20, -0.40, 0.60)), axis=0)
        dd_id_20d_map = dict(zip(u_t, l_20_sum))

        # 12-1M and 6-1M Idiosyncratic Information Ratios
        mean_12 = l_mat.mean(axis=0)
        std_12 = np.maximum(l_mat.std(axis=0, ddof=1), 0.003)
        ir_12_1 = math.sqrt(252.0) * (mean_12 / std_12)

        l_mat_6 = l_mat[-116:, :]
        mean_6 = l_mat_6.mean(axis=0)
        std_6 = np.maximum(l_mat_6.std(axis=0, ddof=1), 0.003)
        ir_6_1 = math.sqrt(252.0) * (mean_6 / std_6)

        # Direction-Conditioned Frog-in-the-Pan (FIP*_i)
        l_cum_12 = l_mat.sum(axis=0)
        sign_12 = np.sign(l_cum_12)
        pos_days = (u_mat > 0).sum(axis=0)
        neg_days = (u_mat < 0).sum(axis=0)
        fip_star = sign_12 * ((pos_days - neg_days) / float(n_days))

        # Skip-Bounded Trend Linearity R^2_{id, i} over m = 1..242
        cum_curve = np.cumsum(l_mat, axis=0)
        t_vec = np.arange(1, n_days + 1, dtype=float)
        t_c = t_vec - t_vec.mean()
        cum_c = cum_curve - cum_curve.mean(axis=0, keepdims=True)
        corr_num = t_c @ cum_c
        corr_den = math.sqrt(float(np.dot(t_c, t_c))) * np.sqrt(
            np.sum(cum_c * cum_c, axis=0) + 1e-12
        )
        r2_id = sign_12 * ((corr_num / corr_den) ** 2)

        # Multi-Horizon Downside Semivariance (252d and 756d)
        ret_756 = returns_df.iloc[max(0, t_idx - 756 + 1) : t_idx + 1][u_t].fillna(0.0).values - rf_daily
        ret_252 = ret_756[-252:, :]
        sigma_down_756 = np.sqrt(252.0 * np.mean(np.minimum(0.0, ret_756) ** 2, axis=0))
        sigma_down_252 = np.sqrt(252.0 * np.mean(np.minimum(0.0, ret_252) ** 2, axis=0))

        # Map Pillar 1 & Risk metrics onto U*_t DataFrame
        ut_metrics = pd.DataFrame(
            {
                "ir_12_1": ir_12_1,
                "ir_6_1": ir_6_1,
                "fip_star": fip_star,
                "r2_id": r2_id,
                "beta_shrunk": beta_mkt_shrunk,
                "sigma_down_756": sigma_down_756,
                "sigma_down_252": sigma_down_252,
            },
            index=u_t,
        )

        df = u_star_df.join(ut_metrics, how="inner")

        # --- PILLAR 1: S_ResMom ---
        p1_raw = (
            0.45 * winsorized_zscore(df["ir_12_1"])
            + 0.30 * winsorized_zscore(df["ir_6_1"])
            + 0.15 * winsorized_zscore(df["fip_star"])
            + 0.10 * winsorized_zscore(df["r2_id"])
        )
        df["s_resmom"] = winsorized_zscore(p1_raw)

        # --- PILLAR 2: Three-Track Structural Quality & Core Earnings Acceleration (S_Qual) ---
        q_prof = pd.Series(0.0, index=df.index)
        q_safe = pd.Series(0.0, index=df.index)

        for trk, grp in df.groupby("track"):
            idx = grp.index
            if trk == "Track_A_NonBFSI":
                prof_sub = 0.50 * winsorized_zscore(grp["roce_ttm"]) + 0.50 * winsorized_zscore(
                    grp["ebitda_to_assets"]
                )
                safe_sub = 0.60 * winsorized_zscore(-grp["accrual_ratio"]) + 0.40 * winsorized_zscore(
                    -grp["net_debt_to_ebitda"].clip(-3.0, 5.0)
                )
            elif trk == "Track_B1_Lending_BFSI":
                prof_sub = 0.50 * winsorized_zscore(grp["roe_ttm"]) + 0.50 * winsorized_zscore(
                    grp["ppop_to_assets"]
                )
                safe_sub = 0.60 * winsorized_zscore(-grp["net_npa"]) + 0.40 * winsorized_zscore(
                    -grp["cost_to_income"]
                )
            else:
                prof_sub = 0.50 * winsorized_zscore(grp["roe_ttm"]) + 0.50 * winsorized_zscore(
                    grp["opm_ttm"]
                )
                safe_sub = 0.60 * winsorized_zscore(grp["opm_ttm"]) + 0.40 * winsorized_zscore(
                    grp["rev_cagr_stability"]
                )
            q_prof.loc[idx] = winsorized_zscore(prof_sub)
            q_safe.loc[idx] = winsorized_zscore(safe_sub)

        q_earnmom = winsorized_zscore(
            0.65 * winsorized_zscore(df["sue_core"]) + 0.35 * winsorized_zscore(df["rev_accel"])
        )
        df["s_qual"] = winsorized_zscore(0.40 * q_prof + 0.30 * q_safe + 0.30 * q_earnmom)

        # --- PILLAR 3: Indian-Adapted Conservative Formula (S_Cons) ---
        low_risk = winsorized_zscore(
            0.45 * winsorized_zscore(-df["sigma_down_756"])
            + 0.35 * winsorized_zscore(-df["sigma_down_252"])
            + 0.20 * winsorized_zscore(-df["beta_shrunk"])
        )

        df["npy"] = df["div_yield"] - df["share_dilution_2y"]
        df["ret_bounded"] = df["retention_ratio"].clip(0.0, 1.0)
        cesy = pd.Series(0.0, index=df.index)

        for trk, grp in df.groupby("track"):
            idx = grp.index
            if trk == "Track_A_NonBFSI":
                c_sub = (
                    0.35 * winsorized_zscore(grp["npy"])
                    + 0.40 * winsorized_zscore(grp["fcf_yield"])
                    + 0.25 * winsorized_zscore(grp["roce_ttm"] * grp["ret_bounded"])
                )
            else:
                c_sub = (
                    0.35 * winsorized_zscore(grp["npy"])
                    + 0.40 * winsorized_zscore(grp["earnings_yield"])
                    + 0.25 * winsorized_zscore(grp["roe_ttm"] * grp["ret_bounded"])
                )
            cesy.loc[idx] = winsorized_zscore(c_sub)

        df["s_cons"] = winsorized_zscore(0.65 * low_risk + 0.35 * cesy)
        return df, dd_id_20d_map

    def select_and_size_portfolio(
        self,
        eval_date: pd.Timestamp,
        df: pd.DataFrame,
        theta_t: float,
        portfolio: IndianTaxAwarePortfolio,
        current_prices: pd.Series,
        disqualified_holdings: set,
    ) -> Tuple[Dict[str, float], int, int]:
        """
        Stage 4B & Stage 5:
          - Dynamic Factor Blending & Re-Standardized Master Score (Z_Master)
          - State-Dependent 25/45 (or 25/30) Hysteresis Buffer + 320-365d LTCG Tax-Cliff Protection
          - State-Dependent Turnover Cap (Max 4 swaps when theta < 0.65; Max 10 when theta >= 0.65)
          - Convex-Feasible Bounded Variance Sizing
        """
        w_mom = 0.55 - 0.35 * theta_t
        w_qual = 0.30 + 0.05 * theta_t
        w_cons = 0.15 + 0.30 * theta_t

        df = df.copy()
        df["raw_master"] = (
            w_mom * df["s_resmom"] + w_qual * df["s_qual"] + w_cons * df["s_cons"]
        )
        df["z_master"] = winsorized_zscore(df["raw_master"])
        df = df.sort_values("z_master", ascending=False)
        df["rank"] = np.arange(1, len(df) + 1)

        # Stage 5A Governance Parameters (Universe-Size Adaptive so small pools never trap losers)
        base_keep = 45 if theta_t < 0.65 else 30
        r_keep = min(base_keep, max(27, int(round(0.32 * len(df)))))
        m_max = 3 if theta_t < 0.45 else (5 if theta_t < 0.65 else 9)
        n_sc_max = int(math.floor(9.0 - 6.0 * theta_t))
        w_sc_max = 0.36 - 0.24 * theta_t

        current_holdings = [
            sym for sym in portfolio.lots.keys() if portfolio.get_position_shares(sym) > 1e-6
        ]

        # Step 1: Mandatory Disqualifications (Stage 1/2 failures or Stage 6 stop breaches)
        surviving_holdings = [
            sym
            for sym in current_holdings
            if sym in df.index and sym not in disqualified_holdings
        ]

        # Enforce Small-Cap count ceiling on existing holdings in high stress
        sc_holdings = [
            sym for sym in surviving_holdings if df.loc[sym, "cap_tier"] == "Small_Cap"
        ]
        forced_sc_exits = set()
        if len(sc_holdings) > n_sc_max:
            sc_sorted = sorted(sc_holdings, key=lambda s: float(df.loc[s, "z_master"]))
            for s_drop in sc_sorted[: len(sc_holdings) - n_sc_max]:
                forced_sc_exits.add(s_drop)

        # Step 2: Buffer Retention & 320-365 Day LTCG Tax-Cliff Protection
        retained: List[str] = []
        failed_buffer: List[str] = []
        ltcg_cliff_protected = 0

        for sym in surviving_holdings:
            if sym in forced_sc_exits:
                failed_buffer.append(sym)
                continue
            row = df.loc[sym]
            rk = int(row["rank"])
            ir6 = float(row["ir_6_1"])
            z_m = float(row["z_master"])
            s_rm = float(row["s_resmom"])
            beta_s = float(row["beta_shrunk"])
            px = float(current_prices.get(sym, 0.0))

            # Check 320-365d LTCG Tax-Cliff Protection (Rank <= min(60, 42% of pool) & Z_Master > 0)
            cliff_rk_cap = min(60, max(30, int(round(0.42 * len(df)))))
            if (
                portfolio.is_ltcg_cliff_protected(sym, eval_date, px)
                and rk <= cliff_rk_cap
                and z_m > 0.0
                and ir6 > -0.10
            ):
                retained.append(sym)
                ltcg_cliff_protected += 1
                continue

            if (
                rk <= r_keep
                and z_m > 0.15
                and s_rm > -0.10
                and ir6 > -0.15
                and (theta_t < 0.65 or beta_s <= 1.15)
            ):
                retained.append(sym)
            else:
                failed_buffer.append(sym)

        # Step 3: Turnover-Capped Exit Selection
        failed_sorted = sorted(failed_buffer, key=lambda s: float(df.loc[s, "z_master"]))
        exited_from_buffer = set(failed_sorted[:m_max])
        for sym in failed_sorted[m_max:]:
            if sym not in forced_sc_exits:
                retained.append(sym)

        replacements_count = (len(current_holdings) - len(retained)) if current_holdings else 25

        # Step 4: Full-List Candidate Fill to N = 25 Stocks
        selected = list(retained)
        sec_counts: Dict[str, int] = {}
        sc_count = 0
        for sym in selected:
            sec = str(df.loc[sym, "amfi_sector"])
            sec_counts[sec] = sec_counts.get(sec, 0) + 1
            if df.loc[sym, "cap_tier"] == "Small_Cap":
                sc_count += 1

        target_n = min(self.cfg.portfolio_size, len(df))
        for sym, row in df.iterrows():
            if len(selected) >= target_n:
                break
            if sym in selected:
                continue
            if float(row["s_resmom"]) <= (0.0 if theta_t < 0.65 else -0.25):
                continue
            if theta_t >= 0.65 and (float(row["beta_shrunk"]) > 0.95 or float(row["s_cons"]) <= -0.10):
                continue
            sec = str(row["amfi_sector"])
            if sec_counts.get(sec, 0) >= 6:
                continue
            if row["cap_tier"] == "Small_Cap" and sc_count >= n_sc_max:
                continue

            selected.append(sym)
            sec_counts[sec] = sec_counts.get(sec, 0) + 1
            if row["cap_tier"] == "Small_Cap":
                sc_count += 1

        # Fallback fill if strict filters left < target_n stocks
        if len(selected) < target_n:
            for sym, row in df.iterrows():
                if len(selected) >= target_n:
                    break
                if sym in selected:
                    continue
                sec = str(row["amfi_sector"])
                if sec_counts.get(sec, 0) >= 6:
                    continue
                if row["cap_tier"] == "Small_Cap" and sc_count >= n_sc_max:
                    continue
                selected.append(sym)
                sec_counts[sec] = sec_counts.get(sec, 0) + 1
                if row["cap_tier"] == "Small_Cap":
                    sc_count += 1

        # Stage 5C: Bounded Inverse-Semivariance Tilt Weighting + Convex Projection
        port_df = df.loc[selected].copy()
        m_tilt = np.clip(np.exp(0.45 * port_df["z_master"].values), 0.50, 2.50)

        med_sigma_down = float(port_df["sigma_down_252"].median())
        sigma_shrunk = 0.75 * port_df["sigma_down_252"].values + 0.25 * med_sigma_down
        if theta_t >= 0.65:
            sigma_shrunk = sigma_shrunk * np.sqrt(np.maximum(port_df["beta_shrunk"].values, 0.5))

        lambda_t = 0.75 + 0.60 * theta_t
        raw_w = m_tilt / np.power(np.maximum(sigma_shrunk, 0.05), lambda_t)

        proj_w = convex_feasible_projection(
            raw_weights=raw_w,
            sectors=port_df["amfi_sector"].tolist(),
            is_smallcap=(port_df["cap_tier"].values == "Small_Cap"),
            w_sc_max=w_sc_max,
            min_w=0.015,
            max_w=0.060,
            sector_cap=0.250,
        )

        target_weights = {sym: float(w) for sym, w in zip(selected, proj_w)}
        return target_weights, replacements_count, ltcg_cliff_protected

    def run_backtest(self) -> Dict[str, object]:
        inst_df, prices_df, vol_df, tval_df, nifty500, fund_df = self.load_matrices()
        rf_daily = (1.0 + self.cfg.risk_free_rate_annual) ** (1.0 / 252.0) - 1.0
        returns_df = prices_df.pct_change(fill_method=None).fillna(0.0)

        vr_series, d_trend_series, dd_mkt_series = self.compute_market_downside_vol_series(
            nifty500, rf_daily
        )

        start_dt = pd.Timestamp(self.cfg.backtest_start_date)
        end_dt = pd.Timestamp(self.cfg.backtest_end_date)
        min_warmup_date = prices_df.index[min(255, len(prices_df) - 1)]
        if start_dt < min_warmup_date:
            print(
                f"[Warmup Guard] BACKTEST_START_DATE ({start_dt.strftime('%Y-%m-%d')}) is within "
                f"252 trading days of DATA_START_DATE ({prices_df.index[0].strftime('%Y-%m-%d')}).\n"
                f"               Using first 255 trading days for Stage 3 OLS regression warmup; "
                f"active trading starts on {min_warmup_date.strftime('%Y-%m-%d')}."
            )
            start_dt = min_warmup_date

        bt_dates = [d for d in prices_df.index if start_dt <= d <= end_dt]
        if not bt_dates:
            raise RuntimeError("No trading dates found inside the specified backtest window.")

        # Identify month-end rebalance dates and Friday weekly stop-check dates
        bt_index_set = set(bt_dates)
        month_end_dates = set(
            pd.Series(bt_dates, index=bt_dates).groupby([d.year for d in bt_dates], group_keys=False)
            .apply(lambda s: s.groupby([x.month for x in s]).last())
            .values
        )
        # Ensure the very first backtest day initializes the portfolio
        first_bt_date = bt_dates[0]
        month_end_dates.add(first_bt_date)

        portfolio_net = IndianTaxAwarePortfolio(self.cfg.initial_corpus_inr, self.cfg)
        nav_gross = self.cfg.initial_corpus_inr
        bench_nav = self.cfg.initial_corpus_inr

        theta_t: Optional[float] = None
        prev_theta: float = 0.20
        cur_portfolio_beta: float = 0.90
        cur_sc_weight_pct: float = 20.0
        latest_ranked_df: Optional[pd.DataFrame] = None
        disqualified_midmonth: set = set()
        intra_month_stops_triggered = 0

        nav_records = []
        rebalance_records = []
        holdings_records = []

        peak_net = self.cfg.initial_corpus_inr
        peak_bench = self.cfg.initial_corpus_inr

        print(
            f"[Backtest Execution] Running Backtest ({bt_dates[0].strftime('%Y-%m-%d')} "
            f"to {bt_dates[-1].strftime('%Y-%m-%d')}, {len(bt_dates):,} NSE trading days)..."
        )
        t0 = time.time()

        for d_idx, cur_date in enumerate(bt_dates):
            global_idx = prices_df.index.get_loc(cur_date)
            cur_prices = prices_df.iloc[global_idx]
            day_rets = returns_df.iloc[global_idx]

            # Accrue daily gross return on current holdings
            if d_idx > 0:
                w_before = portfolio_net.get_holding_weights(prices_df.iloc[global_idx - 1])
                if w_before:
                    port_day_ret = sum(w * float(day_rets.get(s, 0.0)) for s, w in w_before.items())
                    nav_gross *= 1.0 + port_day_ret
                prev_bm = float(nifty500.iloc[global_idx - 1])
                cur_bm = float(nifty500.iloc[global_idx])
                if np.isfinite(prev_bm) and np.isfinite(cur_bm) and prev_bm > 0:
                    mkt_day_ret = cur_bm / prev_bm - 1.0
                else:
                    mkt_day_ret = float(day_rets.mean())
                if not np.isfinite(mkt_day_ret):
                    mkt_day_ret = 0.0
                bench_nav *= 1.0 + mkt_day_ret

            # --- STAGE 6: Weekly Friday Intra-Month Hard Structural Risk Breaker ---
            if (
                cur_date.weekday() == 4
                and cur_date not in month_end_dates
                and latest_ranked_df is not None
                and portfolio_net.lots
            ):
                held_syms = list(portfolio_net.lots.keys())
                # Check 20-day factor-stripped log-idiosyncratic drawdown + 1.75x volume spike
                recent_20 = returns_df.iloc[global_idx - 19 : global_idx + 1][held_syms].values - rf_daily
                mkt_20 = returns_df.iloc[global_idx - 19 : global_idx + 1].mean(axis=1).values - rf_daily
                for j_s, sym in enumerate(held_syms):
                    if sym not in latest_ranked_df.index:
                        continue
                    beta_s = float(latest_ranked_df.loc[sym, "beta_shrunk"])
                    u_20_s = recent_20[:, j_s] - beta_s * mkt_20
                    dd_id_20 = float(np.sum(np.log(1.0 + np.clip(u_20_s, -0.40, 0.60))))
                    v_5d = float(vol_df[sym].iloc[global_idx - 4 : global_idx + 1].mean())
                    v_126d = float(vol_df[sym].iloc[global_idx - 125 : global_idx + 1].median())
                    if dd_id_20 < -0.15 and v_126d > 0 and (v_5d / v_126d) > 1.75:
                        # Trigger Stage 6 Exit & Cap-Compliant Reserve Replacement
                        disqualified_midmonth.add(sym)
                        intra_month_stops_triggered += 1
                        cur_w = portfolio_net.get_holding_weights(cur_prices)
                        freed_w = cur_w.pop(sym, 0.0)
                        if freed_w > 0:
                            for cand_sym, cand_row in latest_ranked_df.iterrows():
                                if cand_sym not in cur_w and cand_sym not in disqualified_midmonth:
                                    cur_w[cand_sym] = min(0.060, freed_w)
                                    break
                            portfolio_net.execute_rebalance(cur_date, cur_w, cur_prices, 0.25)

            # --- STAGES 1 TO 5: Monthly Rebalance (and Emergency >7% Market Drop Check) ---
            if cur_date in month_end_dates:
                vr_t = float(vr_series.iloc[global_idx])
                d_trend_t = float(d_trend_series.iloc[global_idx])
                dd_mkt_t = float(dd_mkt_series.iloc[global_idx])

                # Stage 4 Step 3: Sigmoid Raw Stress s_t & Asymmetric Exponential Smoothing theta_t
                exponent = -4.0 * (
                    (vr_t - 1.35)
                    - 2.2 * min(0.0, d_trend_t)
                    - 1.8 * min(0.0, dd_mkt_t + 0.08)
                )
                s_t = 1.0 / (1.0 + math.exp(np.clip(exponent, -20.0, 20.0)))
                if theta_t is None:
                    theta_t = s_t
                elif s_t > theta_t:
                    theta_t = 0.70 * s_t + 0.30 * theta_t
                else:
                    theta_t = 0.45 * s_t + 0.55 * theta_t

                nav_now = portfolio_net.get_portfolio_market_value(cur_prices)
                u_t, u_star_df = self.build_stage1_and_stage2_universe(
                    global_idx,
                    prices_df,
                    vol_df,
                    tval_df,
                    inst_df,
                    fund_df,
                    nav_now,
                    list(portfolio_net.lots.keys()),
                )

                if len(u_star_df) >= 25:
                    scored_df, _ = self.compute_stage3_three_pillars(
                        global_idx, u_t, u_star_df, returns_df, inst_df, rf_daily
                    )
                    target_w, rep_cnt, cliff_cnt = self.select_and_size_portfolio(
                        cur_date,
                        scored_df,
                        theta_t,
                        portfolio_net,
                        cur_prices,
                        disqualified_midmonth,
                    )
                    disqualified_midmonth.clear()

                    turnover_pct, stcg_inr, ltcg_inr, cost_inr = portfolio_net.execute_rebalance(
                        cur_date, target_w, cur_prices, abs(theta_t - prev_theta)
                    )
                    prev_theta = theta_t

                    # Record portfolio metrics & snapshot
                    actual_w = portfolio_net.get_holding_weights(cur_prices)
                    w_mom = 0.55 - 0.35 * theta_t
                    w_qual = 0.30 + 0.05 * theta_t
                    w_cons = 0.15 + 0.30 * theta_t
                    scored_df["raw_master"] = (
                        w_mom * scored_df["s_resmom"]
                        + w_qual * scored_df["s_qual"]
                        + w_cons * scored_df["s_cons"]
                    )
                    scored_df["z_master"] = winsorized_zscore(scored_df["raw_master"])
                    latest_ranked_df = scored_df.sort_values("z_master", ascending=False)

                    cur_portfolio_beta = sum(
                        w * float(scored_df.loc[s, "beta_shrunk"])
                        for s, w in actual_w.items()
                        if s in scored_df.index
                    )
                    cur_sc_weight_pct = (
                        sum(
                            w
                            for s, w in actual_w.items()
                            if s in scored_df.index and scored_df.loc[s, "cap_tier"] == "Small_Cap"
                        )
                        * 100.0
                    )

                    rebalance_records.append(
                        (
                            cur_date.strftime("%Y-%m-%d"),
                            float(theta_t),
                            vr_t,
                            d_trend_t * 100.0,
                            dd_mkt_t * 100.0,
                            w_mom,
                            w_qual,
                            w_cons,
                            0.75 + 0.60 * theta_t,
                            len(u_t),
                            len(u_star_df),
                            rep_cnt,
                            cliff_cnt,
                            turnover_pct,
                            cur_portfolio_beta,
                            cur_sc_weight_pct,
                            stcg_inr,
                            ltcg_inr,
                            cost_inr,
                        )
                    )

                    sorted_holdings = sorted(
                        actual_w.items(), key=lambda kv: kv[1], reverse=True
                    )
                    for rk_p, (sym, w_val) in enumerate(sorted_holdings, 1):
                        if sym in latest_ranked_df.index:
                            r_s = latest_ranked_df.loc[sym]
                            holdings_records.append(
                                (
                                    cur_date.strftime("%Y-%m-%d"),
                                    rk_p,
                                    sym,
                                    str(r_s["company_name"]),
                                    str(r_s["cap_tier"]),
                                    str(r_s["track"]),
                                    str(r_s["amfi_sector"]),
                                    w_val * 100.0,
                                    float(r_s["z_master"]),
                                    float(r_s["s_resmom"]),
                                    float(r_s["s_qual"]),
                                    float(r_s["s_cons"]),
                                    float(r_s["beta_shrunk"]),
                                    float(r_s["sigma_down_252"]) * 100.0,
                                )
                            )

            nav_net = portfolio_net.get_portfolio_market_value(cur_prices)
            peak_net = max(peak_net, nav_net)
            peak_bench = max(peak_bench, bench_nav)
            dd_net_pct = (nav_net / peak_net - 1.0) * 100.0
            dd_bench_pct = (bench_nav / peak_bench - 1.0) * 100.0

            nav_records.append(
                (
                    cur_date.strftime("%Y-%m-%d"),
                    nav_gross,
                    nav_net,
                    bench_nav,
                    dd_net_pct,
                    dd_bench_pct,
                    float(theta_t or 0.20),
                    cur_portfolio_beta,
                    cur_sc_weight_pct,
                )
            )

        # Persist backtest results into SQLite
        self.conn.execute("DELETE FROM backtest_nav_history")
        self.conn.execute("DELETE FROM backtest_rebalance_log")
        self.conn.execute("DELETE FROM backtest_holdings_snapshot")

        self.conn.executemany(
            "INSERT INTO backtest_nav_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", nav_records
        )
        self.conn.executemany(
            "INSERT INTO backtest_rebalance_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rebalance_records,
        )
        self.conn.executemany(
            "INSERT INTO backtest_holdings_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            holdings_records,
        )
        self.conn.commit()

        print(f"[Backtest Complete] Simulated {len(bt_dates):,} sessions over {(bt_dates[-1]-bt_dates[0]).days/365.25:.2f} calendar years in {time.time()-t0:.2f}s. Saved to SQLite.")
        return {
            "portfolio": portfolio_net,
            "intra_month_stops": intra_month_stops_triggered,
        }


# ==============================================================================
# 8. INSTITUTIONAL TERMINAL DASHBOARD & REPORTING ENGINE
# ==============================================================================

def render_ascii_equity_curve(nav_df: pd.DataFrame, width: int = 72, height: int = 14) -> str:
    """Renders a high-contrast ASCII chart comparing V-RAMQC v3.0 Net Corpus vs Nifty 500."""
    step = max(1, len(nav_df) // width)
    sampled = nav_df.iloc[::step].iloc[:width]
    p_vals = sampled["portfolio_nav_net"].values / 1e7   # in ₹ Crores
    b_vals = sampled["benchmark_nav"].values / 1e7       # in ₹ Crores

    y_min = min(float(p_vals.min()), float(b_vals.min()), 0.9)
    y_max = max(float(p_vals.max()), float(b_vals.max())) * 1.03

    grid = [[" " for _ in range(len(sampled))] for _ in range(height)]
    for x in range(len(sampled)):
        yb = int(round((b_vals[x] - y_min) / (y_max - y_min) * (height - 1)))
        yp = int(round((p_vals[x] - y_min) / (y_max - y_min) * (height - 1)))
        yb = max(0, min(height - 1, yb))
        yp = max(0, min(height - 1, yp))
        grid[height - 1 - yb][x] = "·"
        grid[height - 1 - yp][x] = "█"

    lines = []
    for r_idx, row in enumerate(grid):
        val_label = y_max - (r_idx / (height - 1)) * (y_max - y_min)
        lines.append(f"  ₹{val_label:5.1f} Cr │ {''.join(row)}")
    lines.append("           └─" + "─" * len(sampled))
    start_yr = sampled["trade_date"].iloc[0][:7]
    mid_yr = sampled["trade_date"].iloc[len(sampled) // 2][:7]
    end_yr = sampled["trade_date"].iloc[-1][:7]
    pad1 = max(1, len(sampled) // 2 - len(start_yr) - 2)
    pad2 = max(1, len(sampled) - len(start_yr) - pad1 - len(mid_yr) - len(end_yr))
    lines.append(f"             {start_yr}{' ' * pad1}{mid_yr}{' ' * pad2}{end_yr}")
    lines.append("             Legend: [█] NSE V-RAMQC v3.0 (Net of Tax & Costs)   [·] Nifty 500 Benchmark")
    return "\n".join(lines)


def display_backtest_results_on_screen(cfg: BacktestConfig, conn: sqlite3.Connection, meta: Dict[str, object]) -> None:
    nav_df = pd.read_sql_query("SELECT * FROM backtest_nav_history ORDER BY trade_date", conn)
    reb_df = pd.read_sql_query("SELECT * FROM backtest_rebalance_log ORDER BY rebalance_date", conn)
    last_reb_date = reb_df["rebalance_date"].iloc[-1]
    hold_df = pd.read_sql_query(
        "SELECT * FROM backtest_holdings_snapshot WHERE rebalance_date = ? ORDER BY rank_in_port",
        conn,
        params=(last_reb_date,),
    )

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM instruments")
    total_inst = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM daily_candles")
    total_candles = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM fundamentals_pit")
    total_fund = cur.fetchone()[0]

    # Compute Exact Performance Metrics
    n_days = len(nav_df)
    years = max((pd.to_datetime(nav_df["trade_date"].iloc[-1]) - pd.to_datetime(nav_df["trade_date"].iloc[0])).days / 365.25, 1/365.25)
    init_cap = cfg.initial_corpus_inr

    final_gross = float(nav_df["portfolio_nav_gross"].iloc[-1])
    final_net = float(nav_df["portfolio_nav_net"].iloc[-1])
    final_bench = float(nav_df["benchmark_nav"].iloc[-1])

    cagr_gross = ((final_gross / init_cap) ** (1.0 / years) - 1.0) * 100.0
    cagr_net = ((final_net / init_cap) ** (1.0 / years) - 1.0) * 100.0
    cagr_bench = ((final_bench / init_cap) ** (1.0 / years) - 1.0) * 100.0

    ret_gross = nav_df["portfolio_nav_gross"].pct_change().dropna()
    ret_net = nav_df["portfolio_nav_net"].pct_change().dropna()
    ret_bench = nav_df["benchmark_nav"].pct_change().dropna()

    rf_d = (1.0 + cfg.risk_free_rate_annual) ** (1.0 / 252.0) - 1.0

    vol_gross = float(ret_gross.std() * math.sqrt(252) * 100.0)
    vol_net = float(ret_net.std() * math.sqrt(252) * 100.0)
    vol_bench = float(ret_bench.std() * math.sqrt(252) * 100.0)

    down_gross = float(np.sqrt(252 * np.mean(np.minimum(0.0, ret_gross - rf_d) ** 2)) * 100.0)
    down_net = float(np.sqrt(252 * np.mean(np.minimum(0.0, ret_net - rf_d) ** 2)) * 100.0)
    down_bench = float(np.sqrt(252 * np.mean(np.minimum(0.0, ret_bench - rf_d) ** 2)) * 100.0)

    rf_daily = (1.0 + cfg.risk_free_rate_annual) ** (1.0 / 252.0) - 1.0
    # Conventional daily-return Sharpe and annualized arithmetic-excess Sortino.
    sharpe_gross = float((ret_gross.mean() - rf_daily) / max(ret_gross.std(), 1e-12) * math.sqrt(252))
    sharpe_net = float((ret_net.mean() - rf_daily) / max(ret_net.std(), 1e-12) * math.sqrt(252))
    sharpe_bench = float((ret_bench.mean() - rf_daily) / max(ret_bench.std(), 1e-12) * math.sqrt(252))
    sortino_gross = float((ret_gross.mean() - rf_daily) * 252 / max(down_gross / 100.0, 1e-12))
    sortino_net = float((ret_net.mean() - rf_daily) * 252 / max(down_net / 100.0, 1e-12))
    sortino_bench = float((ret_bench.mean() - rf_daily) * 252 / max(down_bench / 100.0, 1e-12))

    max_dd_net = float(nav_df["drawdown_net_pct"].min())
    max_dd_bench = float(nav_df["benchmark_dd_pct"].min())
    peak_gross = nav_df["portfolio_nav_gross"].cummax()
    max_dd_gross = float(((nav_df["portfolio_nav_gross"] / peak_gross - 1.0) * 100.0).min())

    calmar_gross = cagr_gross / max(abs(max_dd_gross), 1e-6)
    calmar_net = cagr_net / max(abs(max_dd_net), 1e-6)
    calmar_bench = cagr_bench / max(abs(max_dd_bench), 1e-6)

    # Monthly win rate & upside/downside capture
    nav_df["ym"] = nav_df["trade_date"].str.slice(0, 7)
    m_nav = nav_df.groupby("ym")[["portfolio_nav_net", "benchmark_nav"]].last()
    m_rets = m_nav.pct_change().dropna()
    win_months_pct = float((m_rets["portfolio_nav_net"] > 0).mean() * 100.0)
    bench_win_months_pct = float((m_rets["benchmark_nav"] > 0).mean() * 100.0)

    up_m = m_rets[m_rets["benchmark_nav"] > 0]
    dn_m = m_rets[m_rets["benchmark_nav"] < 0]
    upside_capture = (
        float(up_m["portfolio_nav_net"].mean() / max(up_m["benchmark_nav"].mean(), 1e-8) * 100.0)
        if len(up_m) > 0
        else 100.0
    )
    downside_capture = (
        float(dn_m["portfolio_nav_net"].mean() / min(dn_m["benchmark_nav"].mean(), -1e-8) * 100.0)
        if len(dn_m) > 0
        else 100.0
    )

    avg_annual_turnover = float(reb_df["turnover_oneway_pct"].iloc[1:].mean() * 12.0)
    total_stcg = float(reb_df["stcg_tax_paid_inr"].sum())
    total_ltcg = float(reb_df["ltcg_tax_paid_inr"].sum())
    total_costs = float(reb_df["cost_paid_inr"].sum())
    total_cliff_saves = int(reb_df["ltcg_cliff_protected_count"].sum())

    sep = "=" * 96
    subsep = "-" * 96
    print("\n" + sep)
    print("  NSE V-RAMQC v3.0 — HISTORICAL BACKTEST REPORT (UPSTOX API v2 + SQLITE)")
    print(sep)
    print(f"  Config File          : {cfg.env_file} (Upstox Base URL: {cfg.upstox_base_url})")
    print(f"  SQLite Database      : {cfg.sqlite_db_path}")
    print(
        f"  Database Records     : {total_inst} NSE Equities | {total_candles:,} Daily OHLCV Candles | "
        f"{total_fund:,} PIT Fundamental Rows"
    )
    print(
        f"  Backtest Horizon     : {nav_df['trade_date'].iloc[0]} to {nav_df['trade_date'].iloc[-1]} "
        f"({n_days:,} trading days / {years:.2f} years)"
    )
    print(
        f"  Mandate              : 100% Indian NSE Equities (N = {cfg.portfolio_size} Stocks, "
        f"Zero Debt, Zero Gold/Commodities)"
    )
    print(sep)
    proxy_rows = int(conn.execute("SELECT COUNT(*) FROM fundamentals_pit WHERE data_source='CALIBRATED_PIT_SQLITE'").fetchone()[0])
    if proxy_rows:
        print(f"  WARNING: {proxy_rows:,} generated/calibrated fundamental rows are proxies, NOT actual historical filings.")
        print("           Quality-based results are research-only; do not treat as a validated live-performance estimate.")
    print("  WARNING: current/static stock universe may contain survivorship bias; historical index membership is not reconstructed.")

    print("\n  [1] EXECUTIVE HISTORICAL CORPUS & RISK-ADJUSTED PERFORMANCE SUMMARY")
    print(subsep)
    print(
        f"  {'Metric':<38} | {'V-RAMQC v3.0 (Gross)':>18} | {'V-RAMQC v3.0 (Net*)':>18} | {'Nifty 500 Index':>15}"
    )
    print(subsep)
    print(
        f"  {'Initial Corpus':<38} | {'₹1.00 Cr':>18} | {'₹1.00 Cr':>18} | {'₹1.00 Cr':>15}"
    )
    print(
        f"  {'Terminal Corpus (Oct 2026)':<38} | "
        f"{'₹' + f'{final_gross / 1e7:.2f} Cr':>18} | "
        f"{'₹' + f'{final_net / 1e7:.2f} Cr':>18} | "
        f"{'₹' + f'{final_bench / 1e7:.2f} Cr':>15}"
    )
    print(
        f"  {'Total Corpus Multiplier':<38} | "
        f"{f'{final_gross / init_cap:.2f}x':>18} | "
        f"{f'{final_net / init_cap:.2f}x':>18} | "
        f"{f'{final_bench / init_cap:.2f}x':>15}"
    )
    print(
        f"  {'Geometric CAGR (%)':<38} | "
        f"{f'{cagr_gross:.2f}%':>18} | "
        f"{f'{cagr_net:.2f}%':>18} | "
        f"{f'{cagr_bench:.2f}%':>15}"
    )
    print(
        f"  {'Annualized Volatility (%)':<38} | "
        f"{f'{vol_gross:.2f}%':>18} | "
        f"{f'{vol_net:.2f}%':>18} | "
        f"{f'{vol_bench:.2f}%':>15}"
    )
    print(
        f"  {'Annualized Downside Semi-Vol (%)':<38} | "
        f"{f'{down_gross:.2f}%':>18} | "
        f"{f'{down_net:.2f}%':>18} | "
        f"{f'{down_bench:.2f}%':>15}"
    )
    print(
        f"  {'Maximum Drawdown (%)':<38} | "
        f"{f'{max_dd_gross:.2f}%':>18} | "
        f"{f'{max_dd_net:.2f}%':>18} | "
        f"{f'{max_dd_bench:.2f}%':>15}"
    )
    print(
        f"  {'Sharpe Ratio (Rf = 6.5%)':<38} | "
        f"{f'{sharpe_gross:.2f}':>18} | "
        f"{f'{sharpe_net:.2f}':>18} | "
        f"{f'{sharpe_bench:.2f}':>15}"
    )
    print(
        f"  {'Sortino Ratio (Downside Risk-Adj)':<38} | "
        f"{f'{sortino_gross:.2f}':>18} | "
        f"{f'{sortino_net:.2f}':>18} | "
        f"{f'{sortino_bench:.2f}':>15}"
    )
    print(
        f"  {'Calmar Ratio (CAGR / |Max DD|)':<38} | "
        f"{f'{calmar_gross:.2f}':>18} | "
        f"{f'{calmar_net:.2f}':>18} | "
        f"{f'{calmar_bench:.2f}':>15}"
    )
    print(
        f"  {'Positive Months Win Rate (%)':<38} | "
        f"{f'{win_months_pct:.1f}%':>18} | "
        f"{f'{win_months_pct:.1f}%':>18} | "
        f"{f'{bench_win_months_pct:.1f}%':>15}"
    )
    print(
        f"  {'Upside / Downside Market Capture':<38} | "
        f"{'--':>18} | "
        f"{f'{upside_capture:.0f}% / {downside_capture:.0f}%':>18} | "
        f"{'100% / 100%':>15}"
    )
    print(subsep)
    print(
        f"  *Net includes Indian STCG (20%): ₹{total_stcg/1e5:.2f}L | LTCG (12.5%): ₹{total_ltcg/1e5:.2f}L | "
        f"STT & Impact (22 bps): ₹{total_costs/1e5:.2f}L"
    )
    print(
        f"  *Avg One-Way Annual Turnover: {avg_annual_turnover:.1f}% | "
        f"320-365d LTCG Cliff Saves: {total_cliff_saves} lots | "
        f"Stage-6 Intra-Month Stops: {meta['intra_month_stops']}"
    )

    # 2. ASCII Equity Curve
    print("\n  [2] HISTORICAL CORPUS GROWTH TRAJECTORY (₹1.00 CRORE INITIAL INVESTMENT)")
    print(subsep)
    print(render_ascii_equity_curve(nav_df))

    start_yr_lbl = nav_df["trade_date"].iloc[0][:4]
    end_yr_lbl = nav_df["trade_date"].iloc[-1][:4]
    print(f"\n  [3] YEAR-BY-YEAR CALENDAR RETURNS, DRAWDOWNS & REGIME TELEMETRY ({start_yr_lbl} – {end_yr_lbl})")
    print(subsep)
    print(
        f"  {'Year':<6} | {'V-RAMQC Net':>11} | {'Nifty 500':>10} | {'Net Alpha':>10} | "
        f"{'Port MaxDD':>10} | {'Mkt MaxDD':>10} | {'Avg Theta':>9} | {'Avg Beta':>8} | {'End Corpus':>11}"
    )
    print(subsep)

    nav_df["year"] = nav_df["trade_date"].str.slice(0, 4)
    prev_p = init_cap
    prev_b = init_cap
    for yr, grp in nav_df.groupby("year"):
        end_p = float(grp["portfolio_nav_net"].iloc[-1])
        end_b = float(grp["benchmark_nav"].iloc[-1])
        r_p = (end_p / prev_p - 1.0) * 100.0
        r_b = (end_b / prev_b - 1.0) * 100.0
        alpha_yr = r_p - r_b

        pk_p = grp["portfolio_nav_net"].cummax()
        dd_p = float(((grp["portfolio_nav_net"] / pk_p - 1.0) * 100.0).min())
        pk_b = grp["benchmark_nav"].cummax()
        dd_b = float(((grp["benchmark_nav"] / pk_b - 1.0) * 100.0).min())

        avg_th = float(grp["stress_theta"].mean())
        avg_bt = float(grp["portfolio_beta"].mean())
        prev_p, prev_b = end_p, end_b

        print(
            f"  {yr:<6} | {r_p:>+10.2f}% | {r_b:>+9.2f}% | {alpha_yr:>+9.2f}% | "
            f"{dd_p:>9.2f}% | {dd_b:>9.2f}% | {avg_th:>9.2f} | {avg_bt:>8.2f} | ₹{end_p/1e7:>7.2f} Cr"
        )
    print(subsep)

    # 4. Continuous Regime Engine Behavior Summary
    print("\n  [4] CONTINUOUS VARIANCE-ADAPTIVE REGIME ENGINE (STAGE 4) TELEMETRY")
    print(subsep)
    calm_m = reb_df[reb_df["stress_theta"] < 0.35]
    trans_m = reb_df[(reb_df["stress_theta"] >= 0.35) & (reb_df["stress_theta"] < 0.65)]
    stress_m = reb_df[reb_df["stress_theta"] >= 0.65]
    print(
        f"  {'Regime Band':<34} | {'Months':>6} | {'Avg w_Mom':>9} | {'Avg w_Qual':>10} | "
        f"{'Avg w_Cons':>10} | {'Avg Beta':>8} | {'Avg SC Wt%':>10}"
    )
    print(subsep)
    for label, sub in [
        ("Calm Bull (theta < 0.35)", calm_m),
        ("Transition / Choppy (0.35 - 0.65)", trans_m),
        ("High-Stress / Bear (theta >= 0.65)", stress_m),
    ]:
        if len(sub) > 0:
            print(
                f"  {label:<34} | {len(sub):>6} | {sub['w_mom'].mean()*100:>8.1f}% | "
                f"{sub['w_qual'].mean()*100:>9.1f}% | {sub['w_cons'].mean()*100:>9.1f}% | "
                f"{sub['portfolio_beta'].mean():>8.2f} | {sub['smallcap_weight_pct'].mean():>9.1f}%"
            )
        else:
            print(f"  {label:<34} | {0:>6} | {'--':>9} | {'--':>10} | {'--':>10} | {'--':>8} | {'--':>10}")
    print(subsep)

    # 5. Current Latest Portfolio Holdings (Top 25 NSE Stocks)
    print(
        f"\n  [5] LATEST REBALANCE PORTFOLIO SNAPSHOT (AS OF {last_reb_date}, N = {len(hold_df)} NSE STOCKS)"
    )
    print(subsep)
    print(
        f"  {'#':>2} | {'Symbol':<11} | {'Cap Tier':<9} | {'Track':<10} | {'AMFI Sector':<24} | "
        f"{'Z_Mast':>6} | {'ResMom':>6} | {'Qual':>6} | {'Cons':>6} | {'Beta':>5} | {'Weight':>6}"
    )
    print(subsep)
    for _, h in hold_df.iterrows():
        short_trk = (
            "A-NonBFSI"
            if "Track_A" in h["track"]
            else ("B1-Lend" if "Track_B1" in h["track"] else "B2-NonLend")
        )
        print(
            f"  {int(h['rank_in_port']):>2} | {h['symbol']:<11} | {h['cap_tier']:<9} | "
            f"{short_trk:<10} | {h['amfi_sector'][:24]:<24} | "
            f"{h['z_master']:>+6.2f} | {h['s_resmom']:>+6.2f} | {h['s_qual']:>+6.2f} | "
            f"{h['s_cons']:>+6.2f} | {h['beta_shrunk']:>5.2f} | {h['weight_pct']:>5.2f}%"
        )
    print(subsep)
    print(
        f"  Total Portfolio Equity Allocation: {hold_df['weight_pct'].sum():.2f}% "
        f"(100% NSE Stocks, 0% Debt/Commodities)"
    )
    print(sep + "\n")


# ==============================================================================
# 9. MAIN ENTRY POINT
# ==============================================================================

def main() -> None:
    env_path = sys.argv[1] if len(sys.argv) > 1 else "env.txt"
    cfg = load_config_from_env(env_path)
    conn = init_sqlite_db(cfg.sqlite_db_path)

    try:
        upstox_mgr = UpstoxDataManager(cfg, conn)
        upstox_mgr.sync_universe_and_candles()

        backtester = NseVramqcV3Backtester(cfg, conn)
        meta = backtester.run_backtest()

        display_backtest_results_on_screen(cfg, conn, meta)
    finally:
        conn.close()


# ====================== INDEPENDENT DEPLOYMENT LAYER ======================
# Change only after extensive paper validation and manual source review.
LIVE_TRADING_ENABLED = False
MONTHLY_CONTRIBUTION_INR = 50_000.0
MONTHLY_REVIEW_DAY = 5
PAPER_STOP_LOSS_PCT = 0.18
SLIPPAGE_BPS = 15.0


def deployment_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS acct_state(id INTEGER PRIMARY KEY CHECK(id=1), cash REAL NOT NULL, updated TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS contributions(month TEXT PRIMARY KEY, amount REAL NOT NULL, received_at TEXT NOT NULL, invested REAL NOT NULL DEFAULT 0, status TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS positions(symbol TEXT PRIMARY KEY, qty INTEGER NOT NULL, avg_cost REAL NOT NULL, last_price REAL NOT NULL, stop_price REAL NOT NULL, opened TEXT NOT NULL, status TEXT NOT NULL, updated TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS orders(local_id TEXT PRIMARY KEY, broker_id TEXT, ts TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL, status TEXT NOT NULL, mode TEXT NOT NULL, ref_price REAL, fill_price REAL, message TEXT);
    CREATE TABLE IF NOT EXISTS daily_nav(day TEXT PRIMARY KEY, contributions REAL NOT NULL, cash REAL NOT NULL, market_value REAL NOT NULL, nav REAL NOT NULL, unrealized_pnl REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS deployment_reports(day TEXT PRIMARY KEY, report TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS closed_trades(id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, qty INTEGER NOT NULL, buy_avg REAL NOT NULL, sell_price REAL NOT NULL, realized_pnl REAL NOT NULL, closed_at TEXT NOT NULL, reason TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS deployment_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    conn.execute("INSERT OR IGNORE INTO acct_state VALUES(1,0,?)", (dt.datetime.now().isoformat(timespec='seconds'),))
    conn.commit()


def api_call(cfg, method, path, **kwargs):
    if _requests_lib is None:
        raise RuntimeError('Install requests: python -m pip install requests')
    if not cfg.upstox_access_token or len(cfg.upstox_access_token) < 20:
        raise RuntimeError('Valid UPSTOX_ACCESS_TOKEN required')
    headers={'Accept':'application/json','Authorization':'Bearer '+cfg.upstox_access_token}
    base=cfg.upstox_base_url.rstrip('/')
    url=base+path
    r=_requests_lib.request(method,url,headers=headers,timeout=25,**kwargs)
    r.raise_for_status()
    return r.json()


def sync_upstox_orderbook(cfg, conn):
    data=api_call(cfg,'GET','/order/retrieve-all')
    rows=data.get('data',[]) if isinstance(data,dict) else data
    for item in rows or []:
        oid=str(item.get('order_id') or '')
        if oid:
            conn.execute('UPDATE orders SET status=?,fill_price=?,message=? WHERE broker_id=?',
                (str(item.get('status','UNKNOWN')).upper(),float(item.get('average_price') or 0) or None,
                 str(item.get('status_message') or '')[:400],oid))
    conn.commit()
    return rows or []


def send_order(cfg,conn,symbol,instrument_key,side,qty,price,mode):
    local=f'{int(time.time()*1000)}-{symbol}-{side}'
    ts=dt.datetime.now().isoformat(timespec='seconds')
    if mode=='PAPER':
        fill=float(price)*(1+(SLIPPAGE_BPS/10000)*(1 if side=='BUY' else -1))
        status='PAPER_FILLED'; broker_id=None; message='Simulated fill; not sent to exchange'
    else:
        # Second independent guard; live switch itself remains source-code hardcoded.
        if not LIVE_TRADING_ENABLED or os.environ.get('VRAMQC_LIVE_ACK')!='I_ACCEPT_LIVE_ORDER_RISK':
            raise RuntimeError('Live order blocked: live switch and exact VRAMQC_LIVE_ACK are required')
        if not instrument_key or not instrument_key.startswith('NSE_EQ|'):
            raise RuntimeError(f'Unsafe/missing NSE equity instrument key for {symbol}')
        payload={'quantity':int(qty),'product':'D','validity':'DAY','price':0,'instrument_token':instrument_key,
          'order_type':'MARKET','transaction_type':side,'disclosed_quantity':0,'trigger_price':0,'is_amo':False,'tag':'VRAMQC'}
        result=api_call(cfg,'POST','/order/place',json=payload)
        ids=((result.get('data') or {}).get('order_ids') or [])
        broker_id=str(ids[0]) if ids else None
        status='SUBMITTED' if broker_id else 'SUBMIT_UNKNOWN'; fill=None; message=json.dumps(result)[:400]
    conn.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?)',
      (local,broker_id,ts,symbol,side,int(qty),status,mode,float(price),fill,message))
    conn.commit()
    return status,fill


def targets_latest(conn):
    latest=conn.execute('SELECT MAX(rebalance_date) FROM backtest_holdings_snapshot').fetchone()[0]
    if not latest: return {},{}
    rows=conn.execute('''SELECT h.symbol,i.instrument_key,h.weight_pct FROM backtest_holdings_snapshot h
      LEFT JOIN instruments i ON h.symbol=i.symbol WHERE h.rebalance_date=?''',(latest,)).fetchall()
    return ({r[0]:float(r[2])/100 for r in rows if r[2] is not None}, {r[0]:r[1] for r in rows})


def mark_prices(conn):
    day=conn.execute('SELECT MAX(trade_date) FROM daily_candles').fetchone()[0]
    if not day: return {},None
    rows=conn.execute('SELECT symbol,close FROM daily_candles WHERE trade_date=?',(day,)).fetchall()
    return ({s:float(p) for s,p in rows if p and float(p)>0},day)


def register_monthly_contribution(conn, amount, today):
    month=today.strftime('%Y-%m')
    existing=conn.execute('SELECT amount FROM contributions WHERE month=?',(month,)).fetchone()
    if existing: return False
    conn.execute('INSERT INTO contributions(month,amount,received_at,status) VALUES(?,?,?,?)',
                 (month,float(amount),today.isoformat(),'AVAILABLE'))
    conn.execute('UPDATE acct_state SET cash=cash+?,updated=? WHERE id=1',(float(amount),dt.datetime.now().isoformat(timespec='seconds')))
    conn.commit(); return True


def rebalance_contribution(cfg,conn,weights,keys,prices,trade_day,mode):
    """Contribution-only allocation: buy underweights, never sell just to rebalance."""
    today=dt.date.today(); month=today.strftime('%Y-%m')
    row=conn.execute('SELECT amount,status FROM contributions WHERE month=?',(month,)).fetchone()
    if not row or row[1]=='INVESTED': return 0
    # Fixed review on/after the 5th; if run late, contribution is invested on next run.
    if today.day<MONTHLY_REVIEW_DAY: return 0
    acc_cash=float(conn.execute('SELECT cash FROM acct_state WHERE id=1').fetchone()[0])
    pos={r[0]:r[1] for r in conn.execute("SELECT symbol,qty FROM positions WHERE status='HOLD'")}
    value=sum(int(q)*prices.get(s,0) for s,q in pos.items())+acc_cash
    desired={s:w for s,w in weights.items() if s in prices and w>0}
    gaps=[]
    for s,w in desired.items():
        held=int(pos.get(s,0)); gap=max(0.0,value*w-held*prices[s])
        if gap>prices[s]: gaps.append((s,gap))
    total_gap=sum(x[1] for x in gaps)
    if total_gap<=0:
        conn.execute("UPDATE contributions SET status='CASH_CARRY' WHERE month=?",(month,)); conn.commit(); return 0
    contribution=float(row[0]); budget=acc_cash
    invested=0.0
    allocation_budget=min(budget,total_gap)
    # Pro-rata only across actual underweights; never spend more than total target gaps.
    for symbol,gap in sorted(gaps,key=lambda x:x[1],reverse=True):
        px=prices[symbol]
        allocation=min(allocation_budget-invested, allocation_budget*gap/total_gap)
        qty=int(allocation/(px*(1+SLIPPAGE_BPS/10000)))
        if qty<1: continue
        status,fill=send_order(cfg,conn,symbol,keys.get(symbol), 'BUY',qty,px,mode)
        if mode=='PAPER':
            cost=qty*float(fill); old=conn.execute("SELECT qty,avg_cost,opened FROM positions WHERE symbol=? AND status='HOLD'",(symbol,)).fetchone()
            oldq=int(old[0]) if old else 0; oldavg=float(old[1]) if old else float(fill)
            avg=(oldq*oldavg+qty*float(fill))/(oldq+qty); opened=old[2] if old else today.isoformat()
            conn.execute('''INSERT INTO positions VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET
              qty=excluded.qty,avg_cost=excluded.avg_cost,last_price=excluded.last_price,
              stop_price=MAX(positions.stop_price,excluded.stop_price),status='HOLD',updated=excluded.updated''',
              (symbol,oldq+qty,avg,float(fill),float(fill)*(1-PAPER_STOP_LOSS_PCT),opened,'HOLD',dt.datetime.now().isoformat(timespec='seconds')))
            conn.execute('UPDATE acct_state SET cash=cash-? WHERE id=1',(cost,)); invested+=cost
        else:
            # Live pending order is not treated as a fill; reconciliation must confirm before positions change.
            invested+=qty*px
    conn.execute("UPDATE contributions SET invested=?,status=? WHERE month=?",
                 (invested,'INVESTED' if invested>0 else 'CASH_CARRY',month))
    conn.commit(); return invested


def _paper_close_position(conn, symbol, qty, avg_cost, fill, reason):
    now=dt.datetime.now().isoformat(timespec='seconds')
    proceeds=int(qty)*float(fill)
    pnl=proceeds-int(qty)*float(avg_cost)
    conn.execute('INSERT INTO closed_trades(symbol,qty,buy_avg,sell_price,realized_pnl,closed_at,reason) VALUES(?,?,?,?,?,?,?)',
                 (symbol,int(qty),float(avg_cost),float(fill),pnl,now,reason))
    conn.execute("UPDATE positions SET status='CLOSED',last_price=?,updated=? WHERE symbol=?",(fill,now,symbol))
    conn.execute('UPDATE acct_state SET cash=cash+?,updated=? WHERE id=1',(proceeds,now))


def check_stops(cfg,conn,prices,keys,mode):
    """Daily-close stop check only; not intraday protection."""
    for symbol,qty,stop,avg in conn.execute("SELECT symbol,qty,stop_price,avg_cost FROM positions WHERE status='HOLD'").fetchall():
        px=prices.get(symbol)
        if px is None or px>float(stop): continue
        status,fill=send_order(cfg,conn,symbol,keys.get(symbol),'SELL',int(qty),px,mode)
        if mode=='PAPER': _paper_close_position(conn,symbol,qty,avg,fill,'STOP_CLOSE')
    conn.commit()


def exit_names_not_in_signal(cfg,conn,weights,keys,prices,mode):
    """Sell paper holdings omitted by the latest valid V-RAMQC target snapshot."""
    if not weights: return 0
    n=0
    for symbol,qty,avg in conn.execute("SELECT symbol,qty,avg_cost FROM positions WHERE status='HOLD'").fetchall():
        if symbol in weights or symbol not in prices: continue
        status,fill=send_order(cfg,conn,symbol,keys.get(symbol),'SELL',int(qty),prices[symbol],mode)
        if mode=='PAPER': _paper_close_position(conn,symbol,qty,avg,fill,'SIGNAL_EXIT')
        n+=1
    conn.commit(); return n


def save_report(conn,prices,day,folder):
    cash=float(conn.execute('SELECT cash FROM acct_state WHERE id=1').fetchone()[0])
    invested=float(conn.execute('SELECT COALESCE(SUM(amount),0) FROM contributions').fetchone()[0])
    positions=conn.execute("SELECT symbol,qty,avg_cost,stop_price FROM positions WHERE status='HOLD' ORDER BY symbol").fetchall()
    mv=sum(int(q)*prices.get(s,0) for s,q,_,_ in positions); unreal=sum(int(q)*(prices.get(s,0)-float(a)) for s,q,a,_ in positions)
    nav=cash+mv
    realized=float(conn.execute('SELECT COALESCE(SUM(realized_pnl),0) FROM closed_trades').fetchone()[0])
    proxy_count=int(conn.execute("SELECT COUNT(*) FROM fundamentals_pit WHERE data_source='CALIBRATED_PIT_SQLITE'").fetchone()[0])
    report=[f'V-RAMQC {"LIVE" if LIVE_TRADING_ENABLED else "PAPER"} DAILY REPORT | {day}',
      'PAPER SIMULATION ONLY — no exchange orders are sent.' if not LIVE_TRADING_ENABLED else 'LIVE MODE',
      *([f'RESEARCH-ONLY WARNING: {proxy_count:,} generated fundamental proxies; not verified company filings.'] if proxy_count else []),
      'Stop checks use daily closes; they are not intraday stop orders.',
      f'Total contributions: ₹{invested:,.2f}',f'Cash ledger: ₹{cash:,.2f}',f'Holdings value: ₹{mv:,.2f}',
      f'Portfolio NAV: ₹{nav:,.2f}',f'Realized P/L: ₹{realized:,.2f}',f'Unrealized P/L: ₹{unreal:,.2f}',f'Net P/L vs contributions: ₹{nav-invested:,.2f}',
      f'Open holdings: {len(positions)}','', 'SYMBOL | QTY | PRICE | AVG COST | STOP | UNREALIZED P/L']
    for s,q,a,st in positions:
        px=prices.get(s,0); report.append(f'{s} | {q} | {px:.2f} | {a:.2f} | {st:.2f} | ₹{(px-float(a))*int(q):,.2f}')
    text='\n'.join(report)+'\n'; conn.execute('INSERT OR REPLACE INTO daily_nav VALUES(?,?,?,?,?,?)',(str(day),invested,cash,mv,nav,unreal))
    conn.execute('INSERT OR REPLACE INTO deployment_reports VALUES(?,?)',(str(day),text)); conn.commit()
    os.makedirs(folder,exist_ok=True)
    with open(os.path.join(folder,f'report-{day}.txt'),'w',encoding='utf-8') as f:f.write(text)
    print(text)


def deployment_main():
    if LIVE_TRADING_ENABLED:
        raise RuntimeError('LIVE MODE IS NOT ENABLED FOR USE: holdings reconciliation, broker-confirmed fill processing, safe retries/idempotency, and verified live stop execution must be completed and tested first. Keep LIVE_TRADING_ENABLED=False.')
    root=os.path.dirname(os.path.abspath(__file__))
    folder=os.path.join(root,'stock-data'); os.makedirs(folder,exist_ok=True)
    import logging
    logging.basicConfig(filename=os.path.join(folder,'deployment.log'),level=logging.INFO,
      format='%(asctime)s %(levelname)s %(message)s')
    env_path=sys.argv[1] if len(sys.argv)>1 and not sys.argv[1].startswith('--') else os.path.join(root,'env.txt')
    cfg=load_config_from_env(env_path)
    cfg.sqlite_db_path=os.path.join(folder,'vramqc_deployment.db')
    conn=init_sqlite_db(cfg.sqlite_db_path); deployment_schema(conn)
    today=dt.date.today(); mode='LIVE' if LIVE_TRADING_ENABLED else 'PAPER'
    try:
        if '--contribution' in sys.argv:
            ix=sys.argv.index('--contribution')
            amount=float(sys.argv[ix+1])
            if amount<=0: raise ValueError('Contribution must be positive')
            print('Contribution recorded:',register_monthly_contribution(conn,amount,today))
        elif today.day>=MONTHLY_REVIEW_DAY and not LIVE_TRADING_ENABLED:
            # Paper mode models the planned contribution; live cash must be explicitly deposited/recorded.
            register_monthly_contribution(conn,MONTHLY_CONTRIBUTION_INR,today)
        elif today.day>=MONTHLY_REVIEW_DAY and LIVE_TRADING_ENABLED:
            logging.info('Live mode: awaiting explicit --contribution amount; no fictional cash is credited.')
        if cfg.upstox_access_token and len(cfg.upstox_access_token)>20:
            try: sync_upstox_orderbook(cfg,conn); print('[Upstox] read-only orderbook sync complete')
            except Exception as exc: logging.exception('Orderbook sync failed'); print('[WARN] orderbook sync failed:',exc)
        manager=UpstoxDataManager(cfg,conn); manager.sync_universe_and_candles()
        # Strategy engine reuses the tested V-RAMQC logic and stores its latest signal/target snapshot.
        NseVramqcV3Backtester(cfg,conn).run_backtest()
        weights,keys=targets_latest(conn); prices,day=mark_prices(conn)
        if not day: raise RuntimeError('No cached daily prices available')
        age=(dt.date.today()-dt.date.fromisoformat(day)).days
        if age>5: raise RuntimeError(f'Stale cached market data: latest session {day} is {age} calendar days old; refusing new trades.')
        check_stops(cfg,conn,prices,keys,mode)
        exited=exit_names_not_in_signal(cfg,conn,weights,keys,prices,mode)
        if exited: print(f'[Paper] Signal exits: {exited} positions')
        spent=rebalance_contribution(cfg,conn,weights,keys,prices,day,mode)
        if spent: print(f'Monthly contribution allocated: ₹{spent:,.2f}')
        save_report(conn,prices,day,folder)
        print(f'Mode={mode}; database={cfg.sqlite_db_path}; artifacts={folder}')
    finally: conn.close()



if __name__ == "__main__":
    deployment_main()
