#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 T S M O M   —   Time Series Momentum on the Nifty ETF Universe
================================================================================
 Single-file, start-to-end implementation of the TSMOM algorithm.

 WHAT THIS PROGRAM DOES
 ----------------------
  Live-only ETF momentum SIP. Upstox delivery holdings are the portfolio
  source of truth; local tables are journals/mirrors, never a trading wallet.

  * INVEST_PER_DAY is a STRICT buy-notional cap per IST calendar day, shared
    by every buy and every run. Zero disables all buys. Sells, rotation,
    queued exit slices and risk exits still run at zero.
  * Portfolio value = sum(Upstox holding quantity x Upstox last_price),
    including unsettled delivery positions, excluding broker cash and
    instruments outside this ETF universe. Cash is read separately only to
    check affordability and to measure account-level drawdown.
  * Each run refreshes broker holdings/cash/orders, checks exits, updates the
    monthly basket when due, then spends only the unused daily SIP allowance
    on eligible ETFs. Target weights guide purchases; they are NOT a lump-sum
    entitlement. There is no autofill, budget lift, cash sweep or reinvestment
    of sale proceeds outside the SIP cap.
  * DAILY_EXIT_GUARD is permanently True. Momentum/DMA exits are checked every
    run; the monthly rebalance rotates/sells and all buying uses the SIP path.
  * Re-runs top up the same day's allowance. Accepted but unfilled orders
    reserve budget until filled/cancelled; an ambiguous submission is never
    blindly retried. Broker-read failures stop orders (fail closed).
  * Buys use buffered LIMIT orders. Outside regular NSE hours the client marks
    orders AMO for the broker to queue; actual fills still require the exchange
    to accept and execute them. A fresh quote is required while the market is
    open; closed-market orders reference the latest downloaded close.
  * The existing y/n live-order confirmation is retained. Non-interactive
    shells cannot place orders.

 CONFIGURATION / USAGE
 ---------------------
  Edit INVEST_PER_DAY below (rupees of buy notional; statutory charges need
  additional available cash). Set it to 0 to pause buying without pausing exits.
  Put UPSTOX_ACCESS_TOKEN / UPSTOX_API_TOKEN / UPSTOX_API_SECRET in ./env.txt.

  python etf-momentum-screener.py          # scan, update, live SIP cycle, status
  python etf-momentum-screener.py trade    # live SIP cycle (asks y/n)
  python etf-momentum-screener.py live     # alias for trade
  python etf-momentum-screener.py status   # Upstox-backed portfolio + journal
  python etf-momentum-screener.py monitor  # quotes + account risk check
  python etf-momentum-screener.py reconcile
  python etf-momentum-screener.py selftest # offline tests with fake clients

  Generated files live under ./stock-data/ (tsmom.db, logs/, cache/, reports/).
  Standard library only.

 DATA NOTES (honest methodology, per the rulebook)
 -------------------------------------------------
  * Adjusted close:  price_daily.adj_close = close x cumulative factor from
    the corporate_actions table (splits/bonus/dividends loaded per symbol).
    Upstox daily candles are raw/unadjusted; the pipeline refuses to use
    raw prices for signals whenever adjustment factors exist. If no factors
    are loaded for a symbol, raw close is used as a documented proxy and
    this is flagged as a limitation.
  * Dividends: correction factors loaded as corporate actions (note 'DIV')
    restate prices to a total-return scale; dividend CASH is not separately
    ledgered. With raw Upstox prices dividends are absent entirely, so
    price-only momentum is understated for distributing funds.
  * iNAV filter: executed when nav_data rows exist (|close/NAV - 1| vs the
    0.40% / 0.75% bands, latest NAV on/before the date, max 4 days old);
    'GLOBAL INDICES' ETFs use the separate GLOBAL_ETF_PREMIUM_CAP (20%) —
    since 2022/2024 the SEBI/RBI overseas-investment freeze has stopped new
    unit creation, so those six trade at REAL premiums (measured 2-22% norm,
    31-111% blow-off Sep-Oct-2026); premiums above the cap are buy-BANNED
    (BSE investor caution late-Sep-2026; price bands anchor to T-1 NAV from
    1-Apr-2027), never mislabelled as corrupt data.  Only a >300% gap or a
    >20% one-step NAV jump is treated as a broken mapping.
    the daily update / 'load-data' AUTO-CREATE stock-data/nav/*.csv from
    ONE bulk Groww CSV call when absent (AMFI NAVAll endpoints + mfapi
    name-search as fallbacks; ambiguous name matches are refused, never
    guessed), with mfapi history backfill throttled against HTTP-429 walls.
    Fetch locks: once a day after the
    first success; after a failure, retried only after NAV_FAIL_RETRY_HOURS
    so the daily run never churns the network. Diagnostics name each
    failing stage so silent breakage shows up in the console. Otherwise
    the iNAV check is skipped, loudly.  TLS: if verification fails on an
    AV/proxy-MITM box, the NAV fetcher retries ONCE unverified with a loud
    note (_nav_auto_get) — public reference data only; quotes/credentials/
    Upstox never relax.  Scheme codes are ISIN-verified and built in
    (NAV_SCHEME_CODES, all 111 ETFs) — direct download URLs for every ETF
    are listed in nav_urls.txt in this folder; the NAV pipeline works even
    when the bulk feeds are down.
  * Spreads: real bid-ask history builds up from daily 'monitor' quote
    snapshots into the quotes table; until then a liquidity-tiered proxy is
    used (recalibrated 3/9/15/28 bps, x1.4 for non-broad funds).
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
import datetime as dt
import gzip
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

# ==============================================================================
# SECTION 1 — CONFIGURATION (the full rulebook, all in one place)
# Production defaults below are the adopted rulebook values.  Several were
# measured on real Upstox data before the offline research harness was retired;
# the rationale is kept inline.  This is a live-only engine.
# ==============================================================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "stock-data"
DB_PATH = DATA_DIR / "tsmom.db"
LOG_DIR = DATA_DIR / "logs"
CACHE_DIR = DATA_DIR / "cache"
REPORT_DIR = DATA_DIR / "reports"
ENV_PATH = BASE_DIR / "env.txt"

# --- Live execution -----------------------------------------------------------
LIVE_ORDER_TYPE = "LIMIT"          # buys are price-capped; exits use MARKET
LIVE_LIMIT_BUFFER = 0.002          # maximum buy price = reference + 0.2%

# --- Signal parameters (Step 2/3) ---------------------------------------------
LOOKBACK = 252                     # momentum lookback in trading sessions
SKIP = 21                          # skip the most recent 21 sessions
ENTRY_THRESHOLD = 0.02             # enter when momentum > +2%
EXIT_THRESHOLD = 0.00              # exit when momentum <= 0%
MIN_HISTORY_BARS = 252             # a fund needs >= 252 sessions to signal

# --- Risk / sizing parameters (Step 4/6) --------------------------------------
VOL_WINDOW = 60                    # volatility & covariance window
COV_WINDOW = 60
MAX_HOLDINGS = 5                   # ranking versions B and C (user 2026-10-19: 5 ETFs)
POS_CAP_ABS = 0.25                 # absolute ceiling 25% per ETF
POS_CAP_LIQ_FRAC = 0.15            # 15% of p25 daily traded value
VOL_TARGET = 0.15                  # 15% annualised portfolio volatility.
                                   # [TESTED 2026-10-27 round 3: 8% -> CAGR 11.71%
                                   # (-1.6pt); 20% -> same CAGR, worse MaxDD/Sharpe;
                                   # 15% is the plateau top. Drawdown triggers scale
                                   # from this: review 15% / halve 22.5% / cash 30%]
LIQ_WINDOW = 60                    # liquidity measurement window
LIQ_PCTL = 25.0                    # 25th percentile of daily traded value
SPREAD_WINDOW = 60
SPREAD_PCTL = 90.0                 # 90th percentile spread
DEFAULT_SPREAD_BPS_BROAD = 5.0     # proxy when no quote history exists yet
DEFAULT_SPREAD_BPS_OTHER = 10.0
IMPACT_BASE_BPS = 2.0              # impact floor ...
IMPACT_SQRT_COEF = 25.0            # ... plus 25 bps x sqrt(participation)
SPREAD_MAX_BPS = 25.0              # reject trades whose p90 spread exceeds this
INAV_DEV_BROAD = 0.0040            # 0.40% broad-market iNAV band
INAV_DEV_SECTOR = 0.0075           # 0.75% midcap/sectoral iNAV band
# --- NAV auto-create (user ask 2026-10-27): when stock-data/nav/*.csv is
# absent the code builds it itself — ISINs come from the journal's
# instrument_key (NSE_EQ|INF…), the latest NAV from ONE bulk Groww CSV call
# (user direction 2026-10-27: the per-scheme mfapi storm hit HTTP 429 rate
# limits), with AMFI NAVAll + mfapi kept as offline fallbacks.
NAV_AUTO_FETCH = True            # create-if-absent in the daily update path
NAV_HISTORY_DAYS = 400           # calendar days of NAV history kept per fund
NAV_MAX_STALE_DAYS = 4           # NAV older than this is not a valid iNAV reference
INAV_LAG_CUSHION = 0.0025        # +0.25%/day widens the band for NAV publication lag
                                 # (audit F6 v3: a one-day market move is drift, not premium)
INAV_CORRUPT_DEV = 3.00          # |close/NAV - 1| beyond 300% — or a >20% one-step
                                 # NAV jump — can ONLY be a broken scheme mapping
                                 # (scale/split/wrong code): excluded as CORRUPT DATA
                                 # with a navdiag hint (audit v4 P1 at v7 truth label)
GLOBAL_ETF_PREMIUM_CAP = 0.20    # 'GLOBAL INDICES' buys banned above a +20% market-
                                 # vs-NAV premium.  MEASURED (28-Oct-2026), NOT theory:
                                 # SEBI/RBI overseas ceilings ($7bn industry, $1bn per
                                 # AMC, $300mn per AMC in overseas ETFs) exhausted since
                                 # 2022/2024 -> no new units can be created -> the six
                                 # India-listed international ETFs trade at REAL
                                 # premiums.  52-wk norm 2-22% (VRO tables); Sep-Oct-26
                                 # blow-off 31-111% (VRO 14-Oct: FANG+ +59.5%, S&P500
                                 # Top50 +58.9%, HKTECH +31.1%, MON100 +15.3%, HSBeES
                                 # +4.6%; Mint 9-Sep: Q50 +96%; DSIJ 24-Oct: up to
                                 # +111.8%).  BSE formally cautioned (late-Sep-2026):
                                 # "abrupt fall in price ... on enhancement of overseas
                                 # investment limits or on migration of the base price
                                 # for ETF price bands to the T-1 NAV effective
                                 # 1-Apr-2027" (SEBI circular 15-Jun-2026).  0.00 bans
                                 # ANY premium; 99.0 disables the guard (NOT advised
                                 # before Apr-2027).  Domestic funds keep the 0.40/
                                 # 0.75% bands — APs create units freely, premiums
                                 # stay under 1%.
                                 # CAP VALUE measured 28-Oct-2026 night (external
                                 # audit lever 3, R20 variants): banning the 4
                                 # blow-offs (what 0.20 does today) costs -0.48pp
                                 # CAGR vs full tradability (11.98 vs 12.46) —
                                 # cheap insurance.  Tightening to ~8% would ALSO
                                 # ban MON100 (+15%, stable ~12% 52-wk avg, deepest
                                 # liquidity): measured -2.76pp CAGR (9.70) — NOT
                                 # worth it; a ~13% premium snap on one <=25% seat
                                 # is a ~3% one-time hit the monthly trend exits
                                 # can ride out, vs a permanent -2.3pp/yr.
                                 # 0.20 stays.
NAV_FETCH_BUDGET_SEC = 600       # wall-clock budget for the history downloads
                                 # (111 backfills at the throttled pace need ~4-5 min
                                 #  on the first complete pass)
GROWW_NAVALL_URL = "https://groww.in/v1/api/nav/v1/all_nav_latest.csv"  # bulk latest NAV
MIN_GROWW_ROWS = 1000            # accept a Groww parse only if it is the full dump
AMFI_NAVALL_URLS = [             # AMFI daily NAV snapshots, unioned in fallback order
    "https://www.amfiindia.com/spages/NAVAll.txt",
    "https://portal.amfiindia.com/spages/NAVAll.txt",
    "https://www.amfiindia.com/spages/NAVOpen.txt",
    "https://portal.amfiindia.com/spages/NAVClose.txt",
]
MFAPI_HISTORY_URL = "https://api.mfapi.in/mf/{code}"
MFAPI_SEARCH_URL = "https://api.mfapi.in/mf/search?q={q}"
NAV_FAIL_RETRY_HOURS = 4         # a FAILED auto-create retries after 4h (not tomorrow)
NAV_SSL_RELAX = False            # manual override only: the NAV fetcher now retries
                                 #   CERTIFICATE_VERIFY_FAILED over an unverified channel
                                 #   automatically (_nav_auto_get, public reference data
                                 #   only). Set True only to force that always. Quotes,
                                 #   credentials, Upstox NEVER relax TLS.
                                 # break cert verification ("Basic Constraints of CA cert
                                 # not marked critical" — the user's 2026-10-28 box).
                                 # True = NAV bulk fetches skip TLS verification.
                                 # NAV is PUBLIC reference data; the broker feed is
                                 # NEVER relaxed (Upstox calls keep full verification).
# --- Built-in scheme codes (user-provided fix 2026-10-28) ---------------
# The 111 ETFs below were ISIN-matched against the user's AMFI NAVAll snapshot
# (dated 27-Oct-2026) — no name-guessing, no search storm.  This map is tried
# BEFORE the mfapi name search, so on a box where Groww is 404 and AMFI was
# unparsable, NAV CSVs still build: history/latest come straight from
# https://api.mfapi.in/mf/{code}.  The full URL list for every ETF is also
# saved as nav_urls.txt in the repo root.  Verify freshness
# periodically with: diff of scheme names vs the latest NAVAll dump.
NAV_SCHEME_CODES = {  # symbol: mfapi scheme code (AMFI-verified, 27-Oct-2026)
    "ABSLPSE": "152620",
    "ALPHA": "149397",
    "ALPHAETF": "152134",
    "ALPL30IETF": "148456",
    "AONETMMQ50": "153957",
    "AONETOTAL": "153332",
    "AUTOIETF": "149463",
    "BANK10ADD": "154256",
    "BANKNIFTY1": "133122",
    "BFSI": "149105",
    "CEMNTGROWW": "154476",
    "CHEMICAL": "153934",
    "COMMOIETF": "151207",
    "CONS": "150492",
    "CONSUMER": "153191",
    "CPSEETF": "140107",
    "DEFENCE": "154182",
    "DIVIDEND": "154061",
    "DIVOPPBEES": "128639",
    "EBBETF0430": "147849",
    "ECAPINSURE": "153180",
    "ELM250": "153841",
    "EMULTIMQ": "152981",
    "EQUAL200": "153379",
    "ESG": "148572",
    "EVINDIA": "152715",
    "FINIETF": "150879",
    "FLEXIADD": "153874",
    "FMCGIETF": "149072",
    "GROWWHOSPI": "154199",
    "GROWWLOVOL": "153583",
    "GROWWN200": "153329",
    "GROWWNET": "153625",
    "GROWWPOWER": "153744",
    "GROWWRAIL": "153229",
    "HDFCBSE500": "151376",
    "HDFCGROWTH": "150621",
    "HDFCQUAL": "150620",
    "HDFCSENSEX": "135854",
    "HDFCSML250": "151375",
    "HEALTHCARE": "154169",
    "HEALTHY": "149262",
    "HNGSNGBEES": "140095",
    "HYBRIDETF": "154497",
    "ICICIB22": "141957",
    "INFRA": "154181",
    "INFRAIETF": "150526",
    "INSUREIETF": "154503",
    "ITBEES": "148408",
    "LARGEMID50": "154626",
    "LIQUIDCASE": "152327",
    "LOWVOL1": "150000",
    "LTGILTBEES": "139496",
    "MAFANG": "148927",
    "MAHKTECH": "149379",
    "MAKEINDIA": "149786",
    "MASPTOP50": "149169",
    "METALIETF": "152769",
    "MIDBANKADD": "154528",
    "MIDCAP": "149757",
    "MIDCAPETF": "149918",
    "MIDQ50ADD": "149403",
    "MIDSELIETF": "139519",
    "MIDSMALL": "152634",
    "MNC": "150531",
    "MOCAPITAL": "153406",
    "MODEFENCE": "152846",
    "MOENERGY": "153931",
    "MOHEALTH": "150445",
    "MOINFRA": "153564",
    "MOM100": "114456",
    "MOMENTUM30": "153904",
    "MOMENTUM50": "152874",
    "MOMIDMTM": "153694",
    "MOMMIDCAP": "154471",
    "MON100": "114984",
    "MONIFTY500": "152106",
    "MONQ50": "149438",
    "MOQUALITY": "150520",
    "MOREALTY": "152541",
    "MOSERVICE": "154033",
    "MOTOUR": "153618",
    "MOVALUE": "150517",
    "MSCI360": "154366",
    "MULTICAP": "152811",
    "NEXT50IETF": "144587",
    "NIFTY100EW": "153315",
    "NIFTYBEES": "140084",
    "NIFTYQLITY": "150497",
    "NV20IETF": "139455",
    "NXT100CASE": "154764",
    "OILIETF": "152748",
    "PHARMABEES": "149008",
    "PSUBANKADD": "151888",
    "PVTBANIETF": "147530",
    "SBIBPB": "153435",
    "SBINEQWETF": "152749",
    "SECLEDBETA": "154698",
    "SELECTIPO": "153384",
    "SHARIABEES": "140094",
    "SMALLCAP": "152455",
    "SML100CASE": "153837",
    "SNXT30BEES": "153569",
    "TATAGOLD": "152284",
    "TATSILV": "152285",
    "TOP100CASE": "152667",
    "TOP10ADD": "152812",
    "TOP15IETF": "153617",
    "TOP20": "154060",
    "VAL30IETF": "152932",
    "VALUE": "154299",
}

NAV_CALL_SLEEP_SEC = 1.2         # throttle between per-scheme calls (429 storms)
NAV_MAX_429 = 3                  # consecutive HTTP 429s -> stop hammering the mirror

# audit v13 #1 (2026-10-30): symbols the Upstox instrument master matched by
# NAME for 109/111 entries but missed these two despite both being plain "EQ"
# NSE listings (SBI BSE PSU Bank listed 24-Mar-2025, SBI Nifty50 Equal Weight
# listed 22-Jul-2024).  We keep their ISINs so verification can fall back to
# an ISIN match; both ISINs from public exchange listings.
ETF_KNOWN_ISIN = {
    "SBIBPB": "INF200KB1712",
    "SBINEQWETF": "INF200KB1282",
}

# --- Cash & tax (Step 8) -------------------------------------------------------
STCG_RATE = 0.20                   # <= 12 months
LTCG_RATE = 0.125                  # > 12 months, above exemption
LTCG_EXEMPTION = 125_000.0         # Rs 1.25 lakh per financial year

# --- Drawdown risk controls (Step 9) ------------------------------------------
# Drawdown thresholds are SCALED from the vol target (audit sec 3.5):
# dd_thresholds(vol_target) -> (review, severe, extreme)
REENTRY_BAND = 0.05                # re-enter when within 5% of prior peak

# --- Statutory charges & slippage (Step 7) ------------------------------------
# ETF delivery reality (audit-corrected):
#   STT: 0.001% on the SELL side only for equity ETFs; NONE on buy; and no STT
#        at all for Gold / Liquid / Gilt ETFs (and some international ETFs).
#   Stamp duty: 0.015% on the BUYER for delivery (NSE). Verify against your
#        contract note if your broker charges differently.
#   Exchange 0.00297%, SEBI 0.0001%, IPFT 0.0001%, GST 18% on brokerage+exchange+SEBI.
CHARGES = {
    "brokerage_pct": 0.0005,       # 0.05% per order ...
    "brokerage_cap": 20.0,         # ... capped at Rs 20 (discount broker)
    "stt_buy_pct": 0.0,            # no STT on ETF purchase
    "stt_sell_pct": 0.00001,       # 0.001% sell side (equity ETFs only)
    "stamp_buy_pct": 0.00015,      # 0.015% on buy (delivery)
    "exchange_pct": 0.0000297,     # 0.00297%
    "sebi_pct": 0.000001,          # 0.0001%
    "ipft_pct": 0.000001,          # 0.0001%
    "gst_pct": 0.18,               # 18% on brokerage+exchange+SEBI
    "dp_sell_flat": 15.93,         # demat (DP) charge, SELL side only: flat Rs
                                   # 13.50 + 18% GST per scrip per day (discount-
                                   # broker schedule; audit v13 #4; verify your
                                   # plan vs contract note — ledger #103)
}
STT_EXEMPT_CATEGORIES = {"COMMODITY", "DEBT"}   # gold/silver/liquid/gilt: no STT

# --- Tax by asset class (audit #7): equity rates are NOT universal -------------
# EQUITY ETFs only:            STCG 20% (<=12m), LTCG 12.5% over Rs 1.25 L (>12m).
# HYBRID / GLOBAL INDICES:     <65% domestic equity -> post-2023 debt-fund style,
#                              taxed at SLAB rates (see TAX_RULES); sources
#                              conflict on detail — confirm with your CA.
#                              (F11 v3 fixed this comment, which had wrongly
#                              implied equity rates for Hybrid/Global.)
# GOLD/SILVER (COMMODITY):   STCG at slab (<=12m), LTCG 12.5% NO indexation, NO
#                            Rs 1.25 L exemption (>12m).
# DEBT (liquid/gilt/bond):   always at slab rate (specified mutual funds).
# GLOBAL: sources conflict (12 vs 24 months, slab vs 12.5%) -> configured
#          CONSERVATIVELY below (slab short-term, 12.5% long-term, NO
#          Rs 1.25 L exemption).  CONFIRM WITH A CA before filing.
# Audit 2.7 (conservative where sources conflict — CONFIRM WITH A CA):
#   GLOBAL: slab STCG, 12.5% LTCG, NO 1.25 L exemption (some sources differ)
#   HYBRID: 50:50 fund is NOT >65% Indian equity -> non-equity treatment (slab)
CESS_RATE = 0.04                   # health & education cess on the tax amount
TAX_RULES = {
    "EQUITY":    {"stcg": 0.20,   "ltcg": 0.125, "exempt": 125_000.0, "months": 12},
    "GLOBAL":    {"stcg": 0.30,   "ltcg": 0.125, "exempt": 0.0,       "months": 12},
    "HYBRID":    {"stcg": 0.30,   "ltcg": 0.30,  "exempt": 0.0,       "months": 0},
    "COMMODITY": {"stcg": 0.30,   "ltcg": 0.125, "exempt": 0.0,       "months": 12},
    "DEBT":      {"stcg": 0.30,   "ltcg": 0.30,  "exempt": 0.0,       "months": 0},
}


INTEG_MAX_DAILY = 0.40         # series-integrity gate ceilings (F2.6 candidates —
INTEG_MAX_VOL = 1.50           #   tightened settings are run as variants, not
INTEG_MAX_MOM = 3.00           #   silently adopted)

# --- Audit fixes: cash proxies, selection quality, risk release ----------------
# Liquid/overnight/gilt/bond funds are CASH PROXIES: they win rankings on tiny
# vol and eat a top-3 slot, so they are excluded from the ranked pool (audit #3).
CASH_PROXY_SYMBOLS = {"LIQUIDCASE", "LTGILTBEES", "EBBETF0430"}
ONE_PER_INDEX_FAMILY = True        # original rulebook rule
PAIRWISE_CORR_CAP = 0.70           # skip candidate if 120-day correlation with a
                                   #   selected ETF exceeds the cap (0 disables).
                                   #   Measured 0.80 -> 0.70 on real data: better
                                   #   CAGR/Sharpe/MaxDD across windows incl. OOS.
CORR_WINDOW = 120                  # correlation lookback window in sessions
RISK_RELEASE_DAYS = 92             # release 15%/20% risk locks after ~3 months even
                                   #   without price recovery (peak resets)

# --- Tuning knobs (each has a default = current/conservative behaviour) --------
NO_TRADE_BAND = 0.20               # skip rebalancing a position when |tgt-have| /
                                   #   max(tgt,have) < band (exits ALWAYS trade)
MIN_TRADE_VALUE = 2_000.0          # skip dust trades; brokerage makes them poor value
MIN_LIQ_WEIGHT = 0.10              # audit 2.6: 15% x p25 must allow at least this
                                   #   fraction of portfolio equity, else no ranking
VOL_BLEND_252 = 0.30               # 0..1 weight of the 252d vol blended into 60d vol.
                                   #   Measured: 0.30 lifts CAGR/Sharpe with flat
                                   #   MaxDD and beat 0.50 on Sharpe/MaxDD.
REBALANCE_FREQ = "M"               # "M" monthly or "Q" quarterly (month-ends only)

# --- Strict daily SIP ---------------------------------------------------------
# Buy notional only, in rupees, per IST calendar day. All runs and all buy
# reasons share this cap. Charges require extra cash but do not consume the
# investment allowance. Unfilled accepted orders reserve their worst-case
# notional until broker reconciliation. Never lifted to fill a target plan.
INVEST_PER_DAY = 5000
SIP_MODE = True                    # fixed: rebalance sells/rotates; SIP buys
SIP_BASKET_KEY = "sip_basket"      # meta key: comma-separated selected symbols
SIP_BASKET_DATE_KEY = "sip_basket_date"   # meta key: signal date of the basket
SIP_TARGETS_KEY = "sip_targets"    # meta key: JSON {symbol: target_weight}
DEPLOY_DAY_KEY = "deploy_day"      # meta key: JSON {date, deployed, orders}
DEPLOY_SKIP_KEY = "deploy_hard_blocks"  # meta key: JSON {symbol: consecutive hard blocks}
DEPLOY_HARD_BLOCK_DAYS = 3         # audit v4 §8.7: after this many days in a row
                                   #   failing a HARD check (broken price data or
                                   #   iNAV premium block) the name drops out of the
                                   #   stored plan until the month-end re-plan —
                                   #   stops burning budget rows on e.g. a data-corrupt
                                   #   MONQ50. Trend-guard parks do NOT count.
STALE_REFRESH_DAYS = 5             # user choice 2026-10-28 (audit v4 §6): if the
                                   #   basket is older than this many MARKET DAYS,
                                   #   re-derive it from today's data before buying
SIP_RISK_PAUSE = ("SEVERE", "EXTREME")   # daily buys paused in these risk states
QUOTE_MAX_DEV = 0.03               # warn on a large move; never substitute a
                                   #   stale close for a fresh live quote
SIP_TREND_GUARD = True             # daily buy pauses for a name whose EXIT rules
                                   #   currently fire (momentum <= exit, or below
                                   #   the 200-DMA) — never accumulate what the
                                   #   strategy itself would exit
# Permanent live safety rule: check every holding's exit rules on every run.
# Historical R12A results remain in AUDIT_LEDGER.md; this is a user-locked
# safety choice, not a performance-optimized optional knob.
DAILY_EXIT_GUARD = True

# --- Drawdown thresholds SCALE WITH THE VOL TARGET (audit: tuning section) -----
DD_REVIEW_MULT = 1.00              # e.g. 15% target -> 15% review
DD_SEVERE_MULT = 1.50              # ...            -> 22.5% halve
DD_EXTREME_MULT = 2.00             # ...            -> 30% cash

def dd_thresholds(vol_target: float = None):
    vt = VOL_TARGET if vol_target is None else vol_target
    return (DD_REVIEW_MULT * vt, DD_SEVERE_MULT * vt, DD_EXTREME_MULT * vt)


# --- Upstox API ----------------------------------------------------------------
UPSTOX_BASE = "https://api.upstox.com/v2"  # already versioned; paths below omit /v2
UPSTOX_INSTRUMENTS = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
HTTP_SLEEP = 0.35                  # politeness delay between API calls
FETCH_CHUNK_DAYS = 1000            # historical fetch chunk; auto-halves on error
HTTP_RETRIES = 3

# ==============================================================================
# SECTION 2 — SMALL UTILITIES: dates, numbers, Indian money formatting
# ==============================================================================

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TODAY = dt.datetime.now(IST).date()


def now_iso() -> str:
    return dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return dt.datetime.now(IST).date().isoformat()


def parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s[:10])


def date_str(d: dt.date) -> str:
    return d.isoformat()


def month_key(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def financial_year(d: dt.date) -> str:
    """Indian FY: Apr-Mar.  2026-04-01 .. 2027-03-31 -> 'FY2027'."""
    y = d.year if d.month >= 4 else d.year - 1
    return f"FY{y + 1}"


def fmt_inr(x, decimals: int = 2) -> str:
    """Indian digit grouping: 1234567.5 -> '12,34,567.50' (no symbol)."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    neg = x < 0
    x = abs(x)
    s = f"{x:,.{decimals}f}"
    # convert 1,234,567.89 -> 12,34,567.89
    if "," in s:
        whole, frac = s.split(".", 1) if "." in s else (s, "")
        parts = whole.split(",")
        if len(parts) >= 2:
            head = parts[0]
            tail = ",".join(parts[1:])
            whole = f"{head},{tail}"
            # regroup: first group 3 digits? Actually str already did 3s.
            # Rebuild properly:
        # proper Indian grouping from raw integer
        sign = "-" if neg else ""
        ip = int(math.floor(x))
        fp = x - ip
        ip_s = str(ip)
        if len(ip_s) > 3:
            last3 = ip_s[-3:]
            rest = ip_s[:-3]
            groups = []
            while len(rest) > 2:
                groups.insert(0, rest[-2:])
                rest = rest[:-2]
            if rest:
                groups.insert(0, rest)
            ip_s = ",".join(groups) + "," + last3
        out = sign + ip_s
        if decimals > 0:
            out += f"{fp:,.{decimals}f}"[1:]
        return out
    return ("-" if neg else "") + s


def fmt_rs(x, decimals: int = 2) -> str:
    return "Rs " + fmt_inr(x, decimals)


def fmt_cr(x) -> str:
    """Short human money: 123456789 -> 'Rs 12.35 Cr'."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    a = abs(x)
    sign = "-" if x < 0 else ""
    if a >= 1e7:
        return f"{sign}Rs {a/1e7:,.2f} Cr"
    if a >= 1e5:
        return f"{sign}Rs {a/1e5:,.2f} L"
    return f"{sign}Rs {a:,.0f}"


def fmt_pct(x, decimals: int = 2, signed: bool = True) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    sign = "+" if (signed and x > 0) else ""
    return f"{sign}{x*100:.{decimals}f}%"


def fmt_num(x, decimals: int = 2) -> str:
    try:
        return f"{float(x):,.{decimals}f}"
    except (TypeError, ValueError):
        return str(x)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def safe_div(a, b, default=0.0):
    try:
        if b == 0:
            return default
        return a / b
    except Exception:
        return default


def percentile(values, p):
    """Linear-interpolation percentile (p in 0..100). None-safe."""
    vals = sorted(v for v in values if v is not None)
    n = len(vals)
    if n == 0:
        return None
    if n == 1:
        return vals[0]
    k = (n - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return vals[int(k)]
    return vals[f] + (vals[c] - vals[f]) * (k - f)


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs, ddof: int = 1):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n <= ddof:
        return 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - ddof)
    return math.sqrt(var)


# ==============================================================================
# SECTION 3 — CONSOLE: clean, colourful, easy language
# ==============================================================================

class _C:
    """ANSI colour codes (auto-disabled when not a terminal)."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    GREY = "\033[90m"
    BG_GREEN = "\033[42m"
    BG_RED = "\033[41m"
    BG_BLUE = "\033[44m"


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_use_color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
if os.environ.get("FORCE_COLOR"):
    _use_color = True


def paint(text: str, *codes) -> str:
    if not _use_color:
        return text
    return "".join(codes) + str(text) + _C.RESET


def vlen(s: str) -> int:
    return len(_ANSI_RE.sub("", str(s)))


class Printer:
    """All console output goes through here."""

    def raw(self, text: str = ""):
        print(text)

    def line(self, text: str = ""):
        print(text)

    def banner(self):
        w = 78
        self.raw()
        self.raw(paint("  " + "=" * (w - 4), _C.CYAN, _C.BOLD))
        self.raw(paint("  ||  T S M O M   -   Time Series Momentum on the Nifty ETF Universe", _C.CYAN, _C.BOLD))
        self.raw(paint("  ||  LIVE SIP Edition  |  Upstox Holdings  |  SQLite Journal", _C.CYAN, _C.BOLD))
        self.raw(paint("  " + "=" * (w - 4), _C.CYAN, _C.BOLD))
        self.raw(paint(f"  Today: {parse_date(today_str()).strftime('%A, %d %B %Y')}   Mode: LIVE TRADING",
                     _C.RED, _C.BOLD))
        self.raw()

    def section(self, title: str, icon: str = ""):
        self.raw()
        t = f"  {icon}  {title}  " if icon else f"  {title}  "
        bar = "─" * max(8, 78 - vlen(t))
        self.raw(paint(t + bar, _C.BLUE, _C.BOLD))

    def sub(self, title: str):
        self.raw(paint(f"  -- {title} " + "-" * max(4, 60 - vlen(title)), _C.GREY))

    def ok(self, msg: str):
        self.raw("  " + paint("[ OK ]", _C.GREEN, _C.BOLD) + " " + msg)

    def info(self, msg: str):
        self.raw("  " + paint("[INFO]", _C.CYAN, _C.BOLD) + " " + msg)

    def warn(self, msg: str):
        self.raw("  " + paint("[WARN]", _C.YELLOW, _C.BOLD) + " " + msg)

    def err(self, msg: str):
        self.raw("  " + paint("[FAIL]", _C.RED, _C.BOLD) + " " + msg)

    def note(self, msg: str):
        self.raw("  " + paint("[NOTE]", _C.MAGENTA, _C.BOLD) + " " + msg)

    def kv(self, key, val, key_color=_C.GREY, val_color=_C.WHITE):
        self.raw("  " + paint(f"{str(key):<28}", key_color) + " : " + paint(str(val), val_color))

    def table(self, headers, rows, aligns=None, row_color=None, header_color=_C.CYAN):
        """Pretty table. row_color: fn(rowidx, row) -> colour code or None."""
        ncols = len(headers)
        aligns = aligns or (["<"] * ncols)
        # compute widths
        widths = [vlen(str(h)) for h in headers]
        for r in rows:
            for i, cell in enumerate(r):
                widths[i] = max(widths[i], vlen(str(cell)))
        def fmt_row(cells, color=None):
            out = []
            for i, cell in enumerate(cells):
                s = str(cell)
                pad = widths[i] - vlen(s)
                if aligns[i] == ">":
                    s = " " * pad + s
                else:
                    s = s + " " * pad
                out.append(paint(s, color) if color else s)
            return "  " + "  ".join(out) + "  "
        sep = "  " + "  ".join("─" * (w + 2) for w in widths) + "  "
        self.raw(paint("  " + "  ".join("─" * (w + 2) for w in widths) + "  ", _C.GREY))
        self.raw(fmt_row(headers, header_color))
        self.raw(paint(sep, _C.GREY))
        for idx, r in enumerate(rows):
            col = row_color(idx, r) if row_color else None
            self.raw(fmt_row(r, col))
        self.raw(paint("  " + "  ".join("─" * (w + 2) for w in widths) + "  ", _C.GREY))

    def decision(self, emoji, headline, detail, color=_C.WHITE):
        self.raw("  " + paint(f"{emoji}  {headline}", color, _C.BOLD))
        if detail:
            self.raw("      " + paint(detail, _C.GREY))


P = Printer()


# ==============================================================================
# SECTION 4 — LOGGING (everything under stock-data/logs/)
# ==============================================================================

_LOG_FILE = None


def setup_logging() -> Path:
    global _LOG_FILE
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_FILE = LOG_DIR / f"tsmom_{TODAY.strftime('%Y%m%d')}_{dt.datetime.now(IST).strftime('%H%M%S')}.log"
    with open(_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{now_iso()}  === TSMOM session started ===\n")
    return _LOG_FILE


def log(msg: str, level: str = "INFO"):
    line = f"{now_iso()}  {level:<5}  {msg}"
    if _LOG_FILE:
        try:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def save_report(name: str, text: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    p = REPORT_DIR / name
    p.write_text(text, encoding="utf-8")
    return p


# ==============================================================================
# SECTION 5 — ENV.TXT LOADER (Upstox API keys)
# ==============================================================================

def load_env(path: Path = ENV_PATH) -> dict:
    """Read KEY=VALUE lines from env.txt. Comments (#) and blanks allowed."""
    env = {}
    if not path.exists():
        raise FileNotFoundError(
            f"env.txt not found at {path}. It must contain "
            "UPSTOX_ACCESS_TOKEN, UPSTOX_API_TOKEN, UPSTOX_API_SECRET."
        )
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        env[k] = v
    required = ["UPSTOX_ACCESS_TOKEN", "UPSTOX_API_TOKEN", "UPSTOX_API_SECRET"]
    missing = [k for k in required if not env.get(k)]
    if missing:
        raise ValueError(f"env.txt is missing keys: {', '.join(missing)}")
    return env


# ==============================================================================
# SECTION 6 — SQLITE DATABASE (stock-data/tsmom.db)
# ==============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS etf_universe (
    symbol TEXT PRIMARY KEY,
    name TEXT, category TEXT, index_family TEXT,
    instrument_key TEXT, first_seen TEXT, last_seen TEXT,
    status TEXT DEFAULT 'ACTIVE', broad INTEGER DEFAULT 0,
    notes TEXT);
CREATE TABLE IF NOT EXISTS discovery_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, discovered_on TEXT, source TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS etf_pit (
    symbol TEXT, as_of TEXT, present INTEGER,
    PRIMARY KEY (symbol, as_of));
CREATE TABLE IF NOT EXISTS price_daily (
    symbol TEXT, date TEXT,
    open REAL, high REAL, low REAL, close REAL, adj_close REAL,
    volume REAL, traded_value REAL,
    PRIMARY KEY (symbol, date));
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, ts TEXT, ltp REAL, bid REAL, ask REAL, spread_bps REAL);
CREATE TABLE IF NOT EXISTS corporate_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, ex_date TEXT, factor REAL, note TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ux_corporate_actions
    ON corporate_actions(symbol, ex_date, note);
CREATE TABLE IF NOT EXISTS nav_data (
    symbol TEXT, date TEXT, nav REAL, PRIMARY KEY (symbol, date));
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT, symbol TEXT, strategy TEXT,
    momentum REAL, vol60 REAL, liq_p25 REAL, spread_p90 REAL,
    decision TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, signal_as_of TEXT, exec_date TEXT,
    symbol TEXT, side TEXT, qty REAL, price REAL, value REAL,
    slippage REAL, charges REAL, status TEXT, mode TEXT,
    order_type TEXT, reason TEXT,
    broker_order_id TEXT, reserved_value REAL, filled_qty REAL DEFAULT 0,
    limit_price REAL);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    qty REAL, avg_cost REAL, last_price REAL DEFAULT 0, updated TEXT);
CREATE TABLE IF NOT EXISTS tax_lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, buy_date TEXT, buy_price REAL, qty REAL,
    sell_date TEXT, sell_price REAL, pnl REAL, gain_type TEXT, tax REAL);
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    date TEXT PRIMARY KEY,
    cash REAL, holdings_value REAL, equity REAL,
    invested_frac REAL, peak REAL, drawdown REAL, mode TEXT);
CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, level TEXT, drawdown REAL, action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, phase TEXT, symbol TEXT, decision TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS pending_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_as_of TEXT, exec_after TEXT, symbol TEXT, side TEXT,
    target_qty REAL, reason TEXT, status TEXT DEFAULT 'PENDING',
    created TEXT);
"""


class Db:
    """Thin sqlite3 wrapper. All state of the strategy lives here."""

    def __init__(self, path: Path = DB_PATH):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        # Additive migration for existing live journals. Never reset holdings,
        # cash or orders when upgrading the single-file engine.
        order_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(orders)")}
        for name, decl in (("broker_order_id", "TEXT"), ("reserved_value", "REAL"),
                           ("filled_qty", "REAL DEFAULT 0"), ("limit_price", "REAL")):
            if name not in order_cols:
                self.conn.execute(f"ALTER TABLE orders ADD COLUMN {name} {decl}")
        pos_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(positions)")}
        if "last_price" not in pos_cols:
            self.conn.execute("ALTER TABLE positions ADD COLUMN last_price REAL DEFAULT 0")
        # Legacy accepted buys consume today's cap until their broker outcome is
        # known. Historical rows cannot create a new live position.
        self.conn.execute("UPDATE orders SET reserved_value=value WHERE mode='LIVE' "
                          "AND side='BUY' AND reserved_value IS NULL AND status IN "
                          "('FILLED','PENDING','UNCONFIRMED','SUBMITTING')")
        self.conn.commit()

    @contextmanager
    def transaction(self):
        """Cross-process atomic budget reservation; network calls stay outside."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def execute(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def query(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def meta_get(self, key, default=None):
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def meta_set(self, key, value):
        self.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, str(value)))

    # ---- price helpers -------------------------------------------------
    def upsert_prices(self, symbol, bars):
        """bars: iterable of dicts with date/open/high/low/close/adj_close/volume/traded_value"""
        self.conn.executemany(
            "INSERT INTO price_daily(symbol,date,open,high,low,close,adj_close,volume,traded_value) "
            "VALUES(:symbol,:date,:open,:high,:low,:close,:adj_close,:volume,:traded_value) "
            "ON CONFLICT(symbol,date) DO UPDATE SET "
            "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
            "adj_close=excluded.adj_close, volume=excluded.volume, traded_value=excluded.traded_value",
            list(bars))
        self.conn.commit()

    def price_series(self, symbol):
        """Ascending list of rows (date, adj_close, close, traded_value)."""
        rows = self.query(
            "SELECT date, adj_close, close, traded_value, volume FROM price_daily "
            "WHERE symbol=? ORDER BY date", (symbol,))
        return rows

    def symbols_with_prices(self):
        return [r["symbol"] for r in self.query(
            "SELECT DISTINCT symbol FROM price_daily ORDER BY symbol")]

    def last_price(self, symbol):
        r = self.one("SELECT close, adj_close, date FROM price_daily WHERE symbol=? "
                     "ORDER BY date DESC LIMIT 1", (symbol,))
        return r if r else None

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


# ==============================================================================
# SECTION 7 — THE ETF UNIVERSE (static master list of 111 funds)
# ==============================================================================
# (symbol, name, category, index_family)
# Names cleaned of HTML entities. 'index_family' enforces one-ETF-per-index.

ETF_UNIVERSE = [
    ('EBBETF0430', 'Nifty BHARAT Bond', 'DEBT', 'Bond'),
    ('EQUAL200', 'BSE 200 Equal Weight', 'EQUITY', 'BSE 200 Equal Weight'),
    ('HDFCBSE500', 'HDFC S&P BSE 500 ETF', 'EQUITY', 'BSE 500'),
    ('DIVIDEND', 'BSE 500 Dividend Leaders', 'EQUITY', 'BSE 500 Dividend Leaders 50'),
    ('ICICIB22', 'S&P BSE BHARAT 22 index', 'EQUITY', 'BSE BHARAT 22'),
    ('ECAPINSURE', 'BSE Capital Markets & Insurance Total Return Index', 'EQUITY', 'BSE Capital Markets & Insurance'),
    ('MOVALUE', 'Motilal Oswal S&P BSE Enhanced Value ETF', 'EQUITY', 'BSE Enhanced Value'),
    ('MOHEALTH', 'Motilal Oswal S&P BSE Healthcare ETF', 'EQUITY', 'BSE Healthcare'),
    ('GROWWHOSPI', 'BSE Hospitals Index', 'EQUITY', 'BSE Hospitals'),
    ('DEFENCE', 'BSE India Defence', 'EQUITY', 'BSE India Defence'),
    ('MOINFRA', 'BSE India Infrastructure Total Return Index', 'EQUITY', 'BSE India Infrastructure'),
    ('SECLEDBETA', 'BSE India Sector Leaders', 'EQUITY', 'BSE India Sector Leaders TRI'),
    ('INSUREIETF', 'BSE Insurance Index', 'EQUITY', 'BSE Insurance'),
    ('LARGEMID50', 'BSE LargeMid (60:40) Stable Dividend 50 TRI', 'EQUITY', 'BSE LargeMid (60:40) Stable Dividend 50'),
    ('MOMMIDCAP', 'BSE Midcap 150 Momentum 30 TRI', 'EQUITY', 'BSE Midcap 150 Momentum 30'),
    ('MIDSELIETF', 'S&P BSE Midcap Select Index', 'EQUITY', 'BSE Midcap Select'),
    ('MIDBANKADD', 'BSE MidSmall Private Banks Index', 'EQUITY', 'BSE MidSmall Private Banks'),
    ('GROWWPOWER', 'BSE Power Index - TRI', 'EQUITY', 'BSE Power'),
    ('SBIBPB', 'BSE PSU Bank Index TRI', 'EQUITY', 'BSE PSU Bank'),
    ('MOQUALITY', 'Motilal Oswal S&P BSE Quality ETF', 'EQUITY', 'BSE Quality'),
    ('SELECTIPO', 'BSE Select IPO Index', 'EQUITY', 'BSE Select IPO'),
    ('HDFCSENSEX', 'SENSEX', 'EQUITY', 'BSE Sensex'),
    ('SNXT30BEES', 'BSE Sensex Next 30 Index', 'EQUITY', 'BSE Sensex Next 30'),
    ('BANK10ADD', 'BSE Top 10 Banks Index (TRI)', 'EQUITY', 'BSE Top 10 Banks'),
    ('HYBRIDETF', 'Nifty200 Momentum 30 Plus 8-13 yr G-Sec 50:50 TRI', 'Hybrid', 'Equity/Debt'),
    ('MAFANG', 'NYSE FANG+ Total Return Index', 'GLOBAL INDICES', 'GLOBAL INDICES'),
    ('MASPTOP50', 'S&P 500 Top 50 Total Return Index', 'GLOBAL INDICES', 'GLOBAL INDICES'),
    ('MON100', 'Nasdaq100', 'GLOBAL INDICES', 'GLOBAL INDICES'),
    ('MONQ50', 'Nasdaq Q-50 Total Return Index', 'GLOBAL INDICES', 'GLOBAL INDICES'),
    ('MAHKTECH', 'Hang Seng TECH Total Return Index', 'GLOBAL INDICES', 'GLOBAL INDICES'),
    ('HNGSNGBEES', 'Hang Seng Index', 'GLOBAL INDICES', 'GLOBAL INDICES'),
    ('TATAGOLD', 'Tata Gold Exchange Traded Fund', 'COMMODITY', 'GOLD'),
    ('LTGILTBEES', 'NIFTY 8-13 yr G-Sec Index', 'DEBT', 'GSECS/GILT'),
    ('MSCI360', 'MSCI India Index', 'EQUITY', 'MSCI India'),
    ('TOP100CASE', 'Zerodha Nifty 100 ETF', 'EQUITY', 'Nifty 100'),
    ('NIFTY100EW', 'Nifty 100 Equal Weight Index', 'EQUITY', 'Nifty 100 Equal Weight'),
    ('ESG', 'NIFTY100 ESG SECTOR LEADERS', 'EQUITY', 'Nifty 100 ESG Sector Leaders'),
    ('LOWVOL1', 'Kotak Nifty 100 Low Vol 30 ETF', 'EQUITY', 'NIFTY 100 Low Volatility 30'),
    ('HDFCQUAL', 'HDFC NIFTY100 Quality 30 ETF', 'EQUITY', 'Nifty 100 Quality 30'),
    ('GROWWN200', 'Nifty 200 Index - TRI', 'EQUITY', 'Nifty 200'),
    ('ALPHAETF', 'Mirae Asset Nifty 200 Alpha 30 ETF', 'EQUITY', 'Nifty 200 Alpha 30'),
    ('MOMENTUM30', 'Nifty 200 Momentum 30 Index', 'EQUITY', 'Nifty 200 Momentum 30'),
    ('NIFTYQLITY', 'Aditya Birla Sun Life Nifty 200 Quality 30 ETF', 'EQUITY', 'Nifty 200 Quality 30'),
    ('VAL30IETF', 'Nifty200 Value 30 Index', 'EQUITY', 'Nifty 200 Value 30'),
    ('NIFTYBEES', 'Nifty 50', 'EQUITY', 'Nifty 50'),
    ('SBINEQWETF', 'Nifty50 Equal Weight', 'EQUITY', 'Nifty 50 Equal Weight'),
    ('SHARIABEES', 'Shariah', 'EQUITY', 'Nifty 50 Shariah'),
    ('NV20IETF', 'Nifty50 Value 20', 'EQUITY', 'Nifty 50 Value 20'),
    ('MONIFTY500', 'Motilal Oswal Nifty 500 ETF', 'EQUITY', 'Nifty 500'),
    ('FLEXIADD', 'Nifty500 Flexicap Quality 30 TRI', 'EQUITY', 'Nifty 500 Flexicap Quality 30'),
    ('HEALTHCARE', 'Nifty 500 Healthcare Index', 'EQUITY', 'Nifty 500 Healthcare'),
    ('GROWWLOVOL', 'Nifty 500 Low Volatility 50 Index TRI', 'EQUITY', 'Nifty 500 Low Volatility 50'),
    ('MOMENTUM50', 'Nifty 500 Momentum 50 TRI', 'EQUITY', 'Nifty 500 Momentum 50'),
    ('MULTICAP', 'Nifty500 Multicap 50:25:25 Index', 'EQUITY', 'Nifty 500 Multicap 50:25:25'),
    ('EMULTIMQ', 'Nifty500 Multicap Momentum Quality 50 TRI', 'EQUITY', 'Nifty 500 Multicap Momentum Quality 50'),
    ('VALUE', 'Nifty 500 Value 50', 'EQUITY', 'Nifty 500 Value 50'),
    ('ALPHA', 'NIFTY Alpha 50 Index', 'EQUITY', 'NIFTY Alpha 50'),
    ('ALPL30IETF', 'Nifty Alpha Low-Volatility 30 Index', 'EQUITY', 'Nifty Alpha Low-Volatility 30'),
    ('AUTOIETF', 'Nifty Auto Index', 'EQUITY', 'Nifty Auto'),
    ('BANKNIFTY1', 'Nifty Bank', 'EQUITY', 'Nifty Bank'),
    ('MOCAPITAL', 'Nifty Capital Market TRI', 'EQUITY', 'Nifty Capital Market'),
    ('CEMNTGROWW', 'Nifty Cements Index TRI', 'EQUITY', 'Nifty Cements'),
    ('CHEMICAL', 'Nifty Chemicals Index (TRI)', 'EQUITY', 'Nifty Chemicals'),
    ('COMMOIETF', 'ICICI Prudential Nifty Commodities ETF', 'EQUITY', 'Nifty Commodities'),
    ('CPSEETF', 'CPSE ETF', 'EQUITY', 'Nifty CPSE'),
    ('DIVOPPBEES', 'Nifty Dividend Opportunities 50 TRI', 'EQUITY', 'Nifty Dividend Opportunities 50'),
    ('MOENERGY', 'Nifty Energy Index', 'EQUITY', 'Nifty Energy'),
    ('EVINDIA', 'Nifty EV and New Age Automotive TRI', 'EQUITY', 'Nifty EV and New Age Automotive'),
    ('BFSI', 'Nifty Financial Services Index', 'EQUITY', 'Nifty Financial Services'),
    ('FINIETF', 'ICICI Pru Nifty Financial Services Ex-Bank ETF', 'EQUITY', 'Nifty Financial Services Ex-Bank'),
    ('FMCGIETF', 'Nifty FMCG Index', 'EQUITY', 'Nifty FMCG'),
    ('HDFCGROWTH', 'HDFC NIFTY Growth Sectors 15 ETF', 'EQUITY', 'Nifty Growth Sectors 15'),
    ('HEALTHY', 'Nifty Healthcare TRI', 'EQUITY', 'Nifty Healthcare'),
    ('CONS', 'Kotak Nifty India Consumption ETF', 'EQUITY', 'Nifty India Consumption'),
    ('MODEFENCE', 'Nifty India Defence TRI', 'EQUITY', 'Nifty India Defence'),
    ('INFRA', 'Nifty India Infrastructure & Logistics', 'EQUITY', 'Nifty India Infrastructure & Logistics'),
    ('GROWWNET', 'Nifty India Internet Index', 'EQUITY', 'Nifty India Internet'),
    ('MAKEINDIA', 'Nifty India Manufacturing TRI', 'EQUITY', 'Nifty India Manufacturing'),
    ('CONSUMER', 'Nifty India New Age Consumption', 'EQUITY', 'Nifty India New Age Consumption'),
    ('GROWWRAIL', 'Nifty India Railways PSU Index', 'EQUITY', 'Nifty India Railways PSU'),
    ('MOTOUR', 'Nifty India Tourism TRI', 'EQUITY', 'Nifty India Tourism'),
    ('INFRAIETF', 'ICICI Prudential Nifty Infrastructure ETF', 'EQUITY', 'Nifty Infrastructure'),
    ('ITBEES', 'Nifty IT TRI', 'EQUITY', 'Nifty IT'),
    ('ELM250', 'Nifty LargeMidcap 250 TRI', 'EQUITY', 'Nifty LargeMidcap 250'),
    ('METALIETF', 'Nifty Metal Index', 'EQUITY', 'Nifty Metal'),
    ('MOM100', 'Nifty Midcap 100', 'EQUITY', 'Nifty Midcap 100'),
    ('MIDCAPETF', 'Mirae Asset Nifty Midcap 150 ETF', 'EQUITY', 'Nifty Midcap 150'),
    ('MOMIDMTM', 'Nifty Midcap 150 Momentum 50 TRI', 'EQUITY', 'Nifty Midcap 150 Momentum 50'),
    ('MIDQ50ADD', 'Nifty Midcap 150 Quality 50 Index', 'EQUITY', 'Nifty Midcap 150 Quality 50'),
    ('MIDCAP', 'Nifty Midcap 50 Index', 'EQUITY', 'NIFTY Midcap 50'),
    ('MIDSMALL', 'Mirae Asset Nifty MidSmallcap400 MQ 100 ETF', 'EQUITY', 'Nifty MidSmallcap400 Momentum Quality 100'),
    ('MNC', 'Kotak Nifty MNC ETF', 'EQUITY', 'Nifty MNC'),
    ('NXT100CASE', 'Nifty Next 100', 'EQUITY', 'Nifty Next 100'),
    ('NEXT50IETF', 'Nifty Next 50', 'EQUITY', 'Nifty Next 50'),
    ('OILIETF', 'Nifty Oil & Gas Index', 'EQUITY', 'Nifty Oil & Gas'),
    ('PHARMABEES', 'Nifty Pharma TRI', 'EQUITY', 'Nifty Pharma'),
    ('PVTBANIETF', 'Nifty Private Bank Index', 'EQUITY', 'Nifty Private Bank'),
    ('ABSLPSE', 'Aditya Birla Sun Life Nifty PSE ETF', 'EQUITY', 'Nifty PSE'),
    ('PSUBANKADD', 'DSP Nifty PSU Bank ETF', 'EQUITY', 'Nifty PSU Bank'),
    ('MOREALTY', 'Motilal Oswal Nifty Realty ETF', 'EQUITY', 'Nifty Realty'),
    ('MOSERVICE', 'Nifty Services Sector TRI', 'EQUITY', 'Nifty Services Sector'),
    ('HDFCSML250', 'HDFC NIFTY Smallcap 250 ETF', 'EQUITY', 'Nifty Smallcap 250'),
    ('SMALLCAP', 'Mirae Asset Nifty Smallcap 250 MQ 100 ETF', 'EQUITY', 'Nifty Smallcap 250 Momentum Quality 100'),
    ('SML100CASE', 'Nifty Smallcap 100 Index', 'EQUITY', 'Nifty Smallcap100'),
    ('TOP10ADD', 'Nifty Top 10 Equal Weight Index', 'EQUITY', 'Nifty Top 10 Equal Weight'),
    ('TOP15IETF', 'Nifty Top 15 Equal Weight Index', 'EQUITY', 'Nifty Top 15 Equal Weight'),
    ('TOP20', 'Nifty Top 20 Equal Weight', 'EQUITY', 'Nifty Top 20 Equal Weight'),
    ('AONETOTAL', 'Nifty Total Market Index', 'EQUITY', 'Nifty Total Market'),
    ('AONETMMQ50', 'Nifty Total Market Momentum Quality 50 TRI', 'EQUITY', 'Nifty Total Market Momentum Quality 50'),
    ('LIQUIDCASE', 'Zerodha Nifty 1D Rate Liquid ETF', 'DEBT', 'Overnight ETFs and Liquid ETF'),
    ('TATSILV', 'Tata Silver Exchange Traded Fund', 'COMMODITY', 'SILVER'),
]

# Broad-market ETFs get the tighter 0.40% iNAV band.
BROAD_FAMILIES = {'Nifty 50', 'Nifty Next 50', 'Nifty Bank', 'Nifty 50 Equal Weight'}


def universe_map():
    return {sym: {"name": nm, "category": cat, "index_family": fam}
            for sym, nm, cat, fam in ETF_UNIVERSE}


# ==============================================================================
# SECTION 8 — UPSTOX API CLIENT (stdlib urllib only)
# ==============================================================================

class UpstoxError(Exception):
    pass


class UpstoxClient:
    """Minimal Upstox v2 REST client: instrument master, candles, quotes, orders."""

    def __init__(self, env: dict):
        self.access_token = env.get("UPSTOX_ACCESS_TOKEN", "")
        self.api_key = env.get("UPSTOX_API_TOKEN", "")
        self.api_secret = env.get("UPSTOX_API_SECRET", "")

    # ---------- low-level HTTP ----------
    def _request(self, url: str, method: str = "GET", body=None, timeout=30, retry: bool = True):
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            # Account/order v2 APIs require this header as well as the URL prefix.
            "Api-Version": "2.0",
            # Cloudflare blocks python-urllib's default signature (error 1010)
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36"),
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        last_err = None
        attempts = HTTP_RETRIES if retry else 1
        for attempt in range(attempts):
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read()
                    return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as e:
                payload = ""
                try:
                    payload = e.read().decode("utf-8", "ignore")[:400]
                except Exception:
                    pass
                if e.code == 401:
                    raise UpstoxError(
                        "Upstox rejected the access token (401). Tokens expire daily. "
                        "Please put a fresh UPSTOX_ACCESS_TOKEN in env.txt. "
                        f"Server said: {payload}")
                if e.code == 429:
                    time.sleep(5.0 * (attempt + 1))   # rate limit: real backoff
                    last_err = e
                    if attempt == attempts - 1:
                        raise UpstoxError(
                            "RATE_LIMIT: Upstox is rate-limiting requests (HTTP 429). "
                            "Wait a few minutes and rerun; the data already downloaded "
                            "is kept and only the missing part will be fetched.")
                    continue
                if e.code >= 500:
                    time.sleep(1.5 * (attempt + 1))
                    last_err = e
                    continue
                raise UpstoxError(f"Upstox HTTP {e.code}: {payload}")
            except Exception as e:  # network down, DNS, timeout...
                last_err = e
                time.sleep(1.0 * (attempt + 1))
        raise UpstoxError(f"Upstox request failed after {attempts} tries: {last_err}")

    # ---------- instrument master (discovery) ----------
    def download_instrument_master(self) -> list:
        """Download NSE instrument master (gzipped JSON). Cached in stock-data/cache."""
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        gz_path = CACHE_DIR / "NSE.json.gz"
        json_path = CACHE_DIR / "NSE.json"
        if json_path.exists() and (time.time() - json_path.stat().st_mtime) < 12 * 3600:
            return json.loads(json_path.read_text(encoding="utf-8"))
        if gz_path.exists() and (time.time() - gz_path.stat().st_mtime) < 12 * 3600:
            raw = gzip.decompress(gz_path.read_bytes())
            instruments = json.loads(raw.decode("utf-8"))
            json_path.write_text(json.dumps(instruments), encoding="utf-8")
            return instruments
        req = urllib.request.Request(
            UPSTOX_INSTRUMENTS,
            headers={"Accept": "application/gzip",
                     "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/126.0.0.0 Safari/537.36")})
        with urllib.request.urlopen(req, timeout=60) as resp:
            gz_bytes = resp.read()
        gz_path.write_bytes(gz_bytes)
        raw = gzip.decompress(gz_bytes)
        instruments = json.loads(raw.decode("utf-8"))
        json_path.write_text(json.dumps(instruments), encoding="utf-8")
        return instruments

    # ---------- historical candles ----------
    def historical_daily(self, instrument_key: str, from_date: dt.date, to_date: dt.date) -> list:
        """
        Fetch daily candles. Returns ascending [{date,open,high,low,close,volume}].
        Chunks the range; auto-halves the chunk if the API rejects it.
        """
        out = []
        seen = set()

        def fetch_chunk(f0: dt.date, t0: dt.date, depth: int = 0):
            if f0 > t0 or depth > 6:
                return
            key = urllib.parse.quote(instrument_key, safe="")
            to_s = t0.isoformat()
            from_s = f0.isoformat()
            # Upstox path order: /historical-candle/{key}/{interval}/{toDate}/{fromDate}
            for interval in ("1day", "day"):
                url = (f"{UPSTOX_BASE}/historical-candle/{key}/{interval}/{to_s}/{from_s}")
                try:
                    resp = self._request(url)
                    break
                except UpstoxError as e:
                    if "RATE_LIMIT" in str(e) or "401" in str(e) or "access token" in str(e).lower():
                        raise               # global problem -> abort the whole refresh
                    if interval == "day":
                        if "HTTP 4" in str(e) and depth < 6:
                            mid = f0 + (t0 - f0) / 2
                            fetch_chunk(f0, mid, depth + 1)
                            fetch_chunk(mid + dt.timedelta(days=1), t0, depth + 1)
                            return
                        return
                    continue
            else:
                return
            if not isinstance(resp, dict) or resp.get("status") != "success":
                return
            candles = (resp.get("data") or {}).get("candles") or []
            for c in candles:
                try:
                    ts = c[0]
                    if isinstance(ts, (int, float)):
                        d = dt.datetime.fromtimestamp(ts / 1000.0, dt.timezone.utc).date()
                    else:
                        d = parse_date(str(ts))
                    dk = d.isoformat()
                    if dk in seen:
                        continue
                    seen.add(dk)
                    o, h, l, cl, vol = float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5] or 0)
                    out.append({"date": dk, "open": o, "high": h, "low": l,
                                "close": cl, "volume": vol})
                except Exception:
                    continue

        step = dt.timedelta(days=FETCH_CHUNK_DAYS)
        cur = from_date
        while cur <= to_date:
            end = min(cur + step - dt.timedelta(days=1), to_date)
            fetch_chunk(cur, end)
            time.sleep(HTTP_SLEEP)
            cur = end + dt.timedelta(days=1)
        out.sort(key=lambda r: r["date"])
        return out

    # ---------- market quotes ----------
    def ltp(self, instrument_keys: list) -> dict:
        keys = ",".join(instrument_keys)
        url = f"{UPSTOX_BASE}/market-quote/ltp?instrument_key={urllib.parse.quote(keys, safe=',|')}"
        resp = self._request(url)
        out = {}
        for k, v in (resp.get("data") or {}).items():
            out[k] = float(v.get("last_price", 0.0))
        return out

    def quote(self, instrument_key: str) -> dict:
        """Best-effort quote: ltp, bid, ask. Missing fields -> None."""
        url = f"{UPSTOX_BASE}/market-quote/quotes?instrument_key={urllib.parse.quote(instrument_key, safe='|')}"
        resp = self._request(url)
        data = resp.get("data") or {}
        out = {"ltp": None, "bid": None, "ask": None, "ts": None}
        for _k, v in data.items():
            if not isinstance(v, dict):
                continue
            out["ltp"] = float(v.get("last_price") or 0) or None
            depth = v.get("depth") or {}
            buy = (depth.get("buy") or [{}])
            sell = (depth.get("sell") or [{}])
            if buy and isinstance(buy[0], dict) and buy[0].get("price"):
                out["bid"] = float(buy[0]["price"])
            if sell and isinstance(sell[0], dict) and sell[0].get("price"):
                out["ask"] = float(sell[0]["price"])
            out["ts"] = (v.get("timestamp") or v.get("last_trade_time")
                         or v.get("last_trade_timestamp"))
            break
        return out

    def order_details(self, order_id: str) -> dict:
        """Broker-side order status/fill for live reconciliation."""
        return self._request(f"{UPSTOX_BASE}/order/details?order_id={urllib.parse.quote(order_id)}")

    def positions(self) -> list:
        """Today's Upstox positions. Errors propagate (never fake a flat book)."""
        resp = self._request(f"{UPSTOX_BASE}/portfolio/short-term-positions")
        if resp.get("status") not in (None, "success"):
            raise UpstoxError(f"positions API: {resp.get('status')}")
        data = resp.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("positions"), list):
            return data["positions"]
        raise UpstoxError("malformed positions response")

    def funds_available(self):
        """Available equity margin; unavailable/invalid data is an error."""
        resp = self._request(f"{UPSTOX_BASE}/user/get-funds-and-margin?segment=SEC")
        if resp.get("status") not in (None, "success"):
            raise UpstoxError(f"funds API: {resp.get('status')}")
        data = resp.get("data") or {}
        eq = data.get("equity") or data.get("SEC") or data
        if not isinstance(eq, dict):
            raise UpstoxError("malformed funds response")
        for key in ("available_margin", "availableMargin", "available"):
            if eq.get(key) is not None:
                value = float(eq[key])
                if not math.isfinite(value) or value < 0:
                    break
                return value
        raise UpstoxError("equity available_margin missing or invalid")

    def holdings(self):
        """Upstox delivery holdings. Only an explicit empty list means flat."""
        resp = self._request(f"{UPSTOX_BASE}/portfolio/long-term-holdings")
        if resp.get("status") not in (None, "success"):
            raise UpstoxError(f"holdings API: {resp.get('status')}")
        data = resp.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("holdings"), list):
            return data["holdings"]
        raise UpstoxError("malformed holdings response")

    def orders_book(self):
        """Today's broker order book; errors propagate to the fail-closed sync."""
        resp = self._request(f"{UPSTOX_BASE}/order/retrieve-all")
        if resp.get("status") not in (None, "success"):
            raise UpstoxError(f"order book API: {resp.get('status')}")
        data = resp.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("orders"), list):
            return data["orders"]
        raise UpstoxError("malformed order book response")

    # ---------- live order submission (single attempt; no blind retry) ----------
    def place_order(self, symbol: str, instrument_key: str, side: str,
                    qty: int, price: float, order_type: str = "MARKET",
                    is_amo: bool = False) -> dict:
        body = {
            "quantity": int(qty),
            "product": "D",
            "validity": "DAY",
            "price": float(price) if order_type != "MARKET" else 0.0,
            "tag": f"tsmom-{TODAY.isoformat()}",
            "instrument_token": instrument_key,
            "order_type": order_type,
            "transaction_type": side.upper(),
            "disclosed_quantity": 0,
            "trigger_price": 0.0,
            "is_amo": bool(is_amo),
        }
        # Never retry an order submission: a lost response may mean the broker
        # accepted it. Broker reconciles the reserved UNCONFIRMED ticket instead.
        return self._request(f"{UPSTOX_BASE}/order/place", method="POST", body=body, retry=False)


# ==============================================================================
# SECTION 9 — ETF SCANNER & DISCOVERY (Step 1: point-in-time universe)
# ==============================================================================

class Scanner:
    def __init__(self, db: Db, client=None):
        self.db = db
        self.client = client

    def sync_universe(self, source: str = "static-list") -> list:
        """
        Register every ETF in the universe table. The FIRST time a symbol is
        seen its discovery date is stored and printed on the console.
        Returns list of newly discovered symbols.
        """
        umap = universe_map()
        today = today_str()
        newly = []
        P.section("ETF SCANNER — UNIVERSE DISCOVERY", "\U0001F50D")
        P.info(f"Checking {len(umap)} ETFs from the master universe list...")
        for sym, info in umap.items():
            row = self.db.one("SELECT * FROM etf_universe WHERE symbol=?", (sym,))
            broad = 1 if info["index_family"] in BROAD_FAMILIES else 0
            if row is None:
                self.db.execute(
                    "INSERT INTO etf_universe(symbol,name,category,index_family,instrument_key,"
                    "first_seen,last_seen,status,broad,notes) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (sym, info["name"], info["category"], info["index_family"],
                     f"NSE_EQ|{sym}", today, today, "ACTIVE", broad, f"source={source}"))
                self.db.execute(
                    "INSERT INTO discovery_log(symbol,discovered_on,source,detail) VALUES(?,?,?,?)",
                    (sym, today, source, f"first registration: {info['name']}"))
                newly.append(sym)
            else:
                self.db.execute(
                    "UPDATE etf_universe SET name=?, category=?, index_family=?, last_seen=?, broad=? "
                    "WHERE symbol=?",
                    (info["name"], info["category"], info["index_family"], today, broad, sym))
        if newly:
            # audit v8 (2026-10-28): a fresh install printed all 111 rows and
            # drowned the console — cap the table, the full list is one
            # command away
            P.ok(f"{len(newly)} NEW ETFs discovered today.")
            rows = []
            for sym in newly[:12]:
                info = umap[sym]
                rows.append([sym, info["name"][:38], info["category"],
                             paint(today, _C.GREEN, _C.BOLD)])
            P.table(["Symbol", "Name", "Category", "Discovery Date"], rows,
                    aligns=["<", "<", "<", ">"])
            if len(newly) > 12:
                P.note(f"... and {len(newly) - 12} more — full list with "
                       f"dates:  python etf-momentum-screener.py scan")
        else:
            P.info("No new ETFs today — every fund was already known to the database.")
        return newly

    def verify_against_upstox(self, instruments: list) -> dict:
        """Match symbols to the live NSE instrument master; return symbol -> key."""
        by_symbol = {}
        by_isin = {}
        for it in instruments:
            sym = (it.get("trading_symbol") or "").strip()
            if not sym:
                continue
            seg = (it.get("segment") or "")
            exch = (it.get("exchange") or "")
            if exch == "NSE" and seg in ("EQ", "NSE_EQ"):
                by_symbol[sym] = it.get("instrument_key")
                isin = (it.get("isin") or "").strip()
                if isin:
                    by_isin[isin] = it.get("instrument_key")
        found = 0
        isin_matched = []
        for sym, _info in universe_map().items():
            key = by_symbol.get(sym)
            # audit v13 #1: a few real NSE "EQ" listings lose the exact-name
            # match (master naming/segment quirks).  Fall back to their ISIN.
            if not key and sym in ETF_KNOWN_ISIN:
                key = by_isin.get(ETF_KNOWN_ISIN[sym])
                if key:
                    isin_matched.append(sym)
            if key:
                found += 1
                self.db.execute("UPDATE etf_universe SET instrument_key=? WHERE symbol=?", (key, sym))
        if isin_matched:
            P.info("Matched via ISIN (plain symbol lookup missed these): "
                   + ", ".join(sorted(isin_matched)))
        return by_symbol

    def show_universe_table(self):
        """Full scanner output with discovery dates."""
        rows = self.db.query(
            "SELECT symbol,name,category,index_family,first_seen,status,broad FROM etf_universe "
            "ORDER BY category, symbol")
        P.section("SCANNED ETF LIST (with discovery dates)", "\U0001F4CB")
        out = []
        for r in rows:
            out.append([r["symbol"], r["name"][:40], r["category"],
                        r["index_family"][:26],
                        paint(r["first_seen"], _C.CYAN),
                        r["status"]])
        def rc(i, row):
            return _C.GREEN if row[4].find(_C.GREEN) >= 0 else None
        P.table(["Symbol", "Name", "Category", "Index Family", "Discovered On", "Status"],
                out, aligns=["<", "<", "<", "<", ">", "<"])
        P.info(f"Total ETFs tracked: {len(rows)}")
        return rows

    def show_universe_summary(self):
        """One-glance universe status for the daily run.  The full 111-row
        listing stays available via: python etf-momentum-screener.py scan"""
        rows = self.db.query(
            "SELECT category, COUNT(*) AS n FROM etf_universe GROUP BY category")
        total = sum(r["n"] for r in rows)
        parts = ", ".join(f"{r['n']} {str(r['category']).lower()}"
                          for r in sorted(rows, key=lambda r: -r["n"]))
        first = self.db.one("SELECT MIN(first_seen) AS f FROM etf_universe")
        P.kv("ETFs tracked", f"{total}  ({parts})")
        if first and first["f"]:
            P.kv("Universe journaled since", first["f"])
            # survivorship-journal freshness check: a fresh 'first_seen' with
            # much older price history means the journal was rebuilt (e.g. DB
            # recreated) and discovery dates no longer prove point-in-time
            # membership for long histories
            oldest_px = self.db.one("SELECT MIN(date) AS d FROM price_daily")
            # audit v8 (2026-10-28): the old text claimed the journal "was
            # rebuilt; discovery dates reset" — it never rebuilds anything,
            # and on a fresh install the youth is NORMAL.  Say the true thing,
            # once a week (it fired on every daily run otherwise).
            # audit v11 P1-1: this summary runs BEFORE the day's price download,
            # so on a true fresh install price_daily is EMPTY and the old
            # oldest_px condition silently skipped the note.  A journal whose
            # first_seen IS today, with no prices yet, is the fresh-install
            # signature — emit the same honesty note from the journal side.
            _olin = oldest_px and oldest_px["d"]
            _mismatch = (_olin and first["f"] > oldest_px["d"]
                         and (parse_date(first["f"]) - parse_date(oldest_px["d"])).days > 30)
            _fresh = (not _olin) and first["f"] == today_str()
            if _mismatch or _fresh:
                _ln = self.db.meta_get("last_journal_note", "")
                try:
                    _emit = (not _ln) or (parse_date(today_str())
                                          - parse_date(_ln)).days >= 7
                except Exception:
                    _emit = True
                if _emit:
                    _pxf = (f"while price history runs back to {oldest_px['d']}"
                            if _olin else
                            "(prices are being downloaded in this very first run)")
                    P.note(f"Heads-up (weekly): the discovery journal only started "
                           f"{first['f']} {_pxf} — perfectly normal on a fresh install "
                           f"or after a data restore.  Point-in-time membership "
                           f"only becomes reliable once the journal has aged about "
                           f"a year; live trading is unaffected.  Tip: when backing "
                           f"up, copy the whole "
                           f"stock-data folder together (prices + journal).")
                    self.db.meta_set("last_journal_note", today_str())
        P.note("Full listing with discovery dates:  python etf-momentum-screener.py scan")


# ==============================================================================
# SECTION 10 — DATA MANAGER (download / cache price history)
# ==============================================================================

def _http_get(url: str, timeout: int = 25, verify: bool = True):
    """(text, error_string) GET for public endpoints (stdlib).
    Browser-grade headers: AMFI's front-end has refused bare-script clients in
    the past, and the user's 2026-10-27 run failed with a blank message — the
    error string is KEPT so a console paste pinpoints the failing stage.
    verify=False exists ONLY for the opt-in NAV_SSL_RELAX knob (AV/proxy MITM
    breaking cert verification on the user's box; NAV is public reference data).
    """
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/126.0.0.0 Safari/537.36",
            "Accept": "text/plain, text/csv, application/json, */*",
            "Accept-Language": "en-IN,en;q=0.9",
            "Referer": "https://www.amfiindia.com/"})
        kwargs = {"timeout": timeout}
        if not verify:
            import ssl as _ssl
            kwargs["context"] = _ssl._create_unverified_context()
        with urllib.request.urlopen(req, **kwargs) as resp:
            raw = resp.read()
        try:
            return raw.decode("utf-8"), ""
        except UnicodeDecodeError:
            return raw.decode("latin-1", errors="replace"), ""
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"


_NAV_TLS_DOWNGRADE_NOTED = [False]   # one console note per process on fallback

def _nav_auto_get(url: str, timeout: int = 25):
    """TLS-resilient fetcher for PUBLIC NAV reference data ONLY (Groww/AMFI
    bulk dumps — never quotes, never credentials, never Upstox).  On
    CERTIFICATE_VERIFY_FAILED (corporate AV/proxy MITM, the user's 2026-10-28
    box) it retries ONCE over an unverified channel and says so loudly;
    NAV_SSL_RELAX=True still forces the always-off legacy behavior.  Every
    other failure stays a hard error.  Fixes the 'NAV download must be fixed'
    issue without any flag juggling."""
    txt, err = _http_get(url, timeout)
    if txt:
        return txt, err
    if "CERTIFICATE_VERIFY_FAILED" in (err or ""):
        txt2, err2 = _http_get(url, timeout, verify=False)
        if txt2 and not _NAV_TLS_DOWNGRADE_NOTED[0]:
            _NAV_TLS_DOWNGRADE_NOTED[0] = True
            # user direction 2026-10-28 (audit v8): LOG ONLY — the AV/proxy TLS
            # interception on this box is permanent, so the console warning was
            # pure noise.  Safety facts unchanged: the downgrade applies ONLY to
            # free public NAV reference data (guarded by the INAV_CORRUPT_DEV
            # 300% / NAV-scale-jump tiers); quotes, orders, credentials NEVER
            # downgrade.  Root fix: stop the AV's TLS interception (or give
            # Python its root certificate).
            log("NAV fetch: TLS certificate verification failed (AV/proxy "
                "intercepts TLS on this box) — retried ONCE over an unverified "
                "channel; public NAV reference data only, corrupt-tier gate "
                "(>300% / NAV scale jump) still applies; quotes/orders/creds "
                "remain fully verified. Console silent per user direction.")
        return txt2, err2
    return txt, err


def fetch_amfi_navall(http_get=None):
    """(isin_map, diagnostics).  Walks AMFI_NAVALL_URLS, UNIONS parsed schemes
    (first URL wins per ISIN), rejects HTML-shaped responses (bot-block error
    pages) and empty bodies — every failure is named in the diagnostics
    string."""
    get = http_get or _http_get
    amfi, diag = {}, []
    for url in AMFI_NAVALL_URLS:
        txt, err = get(url, timeout=25)
        if not txt:
            diag.append(f"{url.split('/')[2]}{url[-11:]} -> {err or 'empty response'}")
            continue
        if "<" in txt[:200]:
            diag.append(f"{url.split('/')[2]}{url[-11:]} -> returned an HTML page, not the "
                        f"text snapshot ({len(txt)} chars — bot-block or layout change)")
            continue
        rows = parse_amfi_navall(txt)
        if not rows:
            diag.append(f"{url.split('/')[2]}{url[-11:]} -> {len(txt)} chars parsed to 0 "
                        f"scheme rows (layout changed?)")
            continue
        for isin, hit in rows.items():
            amfi.setdefault(isin, hit)
    return amfi, ";  ".join(diag)


def parse_groww_navall(text: str):
    """Groww all_nav_latest.csv -> (by_isin, by_name) or (None, None).

    Header aliases are sniffed (exact column names are not in the sandbox's
    reach): isin/code/name/nav/date columns are matched against common
    aliases; delimiter detected per line.  Rows without a float NAV are
    dropped.  (None, None) when the body is not clearly the full dump
    (< MIN_GROWW_ROWS data rows) so a bot-block HTML page can never look
    like a successful download.  Dates accept ISO, DD-MM-YYYY, DD-Mon-YYYY,
    DD/MM/YYYY and epoch s/ms; rows with NO parseable date are SKIPPED —
    external audit F6 (2026-10-28 v3): stamping 'today' fabricates freshness
    and lets a stale NAV cheat the staleness guard."""
    if not text or "<" in text[:200]:
        return None, None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < MIN_GROWW_ROWS:
        return None, None
    delim = ";" if lines[0].count(";") > lines[0].count(",") else ","
    hdr = [h.strip().lower().replace("_", " ") for h in lines[0].split(delim)]
    ALIAS = {
        "isin": {"isin"},
        "code": {"scheme code", "code", "scheme_code", "schemecode"},
        "name": {"scheme name", "name", "scheme", "fund name", "scheme_name"},
        "nav": {"nav", "net asset value", "nav value", "navvalue"},
        "date": {"date", "nav date", "as on", "asof", "as of", "navdate"},
    }
    col = {}
    for key, aliases in ALIAS.items():
        for i, h in enumerate(hdr):
            if h in aliases:
                col[key] = i
                break
    if "nav" not in col or ("isin" not in col and "name" not in col):
        return None, None
    by_isin, by_name = {}, {}
    for ln in lines[1:]:
        f = [x.strip().strip('"') for x in ln.split(delim)]
        try:
            nav = float(f[col["nav"]].replace(",", ""))
        except (ValueError, IndexError):
            continue
        d = ""
        if "date" in col and col["date"] < len(f):
            raw = f[col["date"]]
            for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y", "%d/%m/%Y"):
                try:
                    d = date_str(dt.datetime.strptime(raw, fmt).date())
                    break
                except ValueError:
                    continue
            else:
                try:
                    ts = float(raw) / (1000.0 if len(raw) > 10 else 1.0)
                    d = date_str(dt.datetime.fromtimestamp(ts, dt.timezone.utc).date())
                except (ValueError, OSError, OverflowError):
                    d = ""
        if not d:
            continue                    # audit F6: never fabricate a NAV date
        rec = {"code": f[col["code"]] if "code" in col and col["code"] < len(f) else "",
               "name": f[col["name"]] if "name" in col and col["name"] < len(f) else "",
               "nav": nav, "date": d}
        if "isin" in col and col["isin"] < len(f):
            isin = f[col["isin"]].split("/")[0].strip().upper()
            if re.fullmatch(r"INF[A-Z0-9]{9}", isin):
                by_isin[isin] = rec
        if rec["name"]:
            by_name.setdefault(rec["name"].lower(), rec)
    if len(by_isin) + len(by_name) < MIN_GROWW_ROWS:
        return None, None
    return by_isin, by_name


def groww_guess_code(symbol, name, by_name, by_token):
    """Name-token match against the already-downloaded Groww dump (NO extra
    HTTP).  Conservative like mfapi_guess_code: exactly ONE candidate must
    cover all name tokens; ambiguous hits return None (missing NAV beats a
    WRONG NAV)."""
    want = set(_nav_tokens(name)) or set(_nav_tokens(symbol))
    if not want:
        return None
    hits = []
    for key, rec in by_name.items():
        toks = set(_nav_tokens(key))
        if want <= toks and rec not in hits:
            hits.append((key, rec))
    if len(hits) == 1:
        return hits[0][1]
    return None


def fetch_groww_navall(http_get=None):
    """(by_isin, by_name, diagnostics) — ONE bulk GET replaces the per-scheme
    storm that hit mfapi's HTTP-429 wall on the user's 2026-10-27 box."""
    get = http_get or _http_get
    txt, err = get(GROWW_NAVALL_URL, timeout=25)
    host = GROWW_NAVALL_URL.split("/")[2]
    if not txt:
        return None, None, f"{host}/all_nav_latest.csv -> {err or 'empty response'}"
    by_isin, by_name = parse_groww_navall(txt)
    if by_isin is None:
        return None, None, (f"{host}/all_nav_latest.csv -> {len(txt)} chars parsed to "
                            f"< {MIN_GROWW_ROWS} scheme rows (bot-block or layout change)")
    return by_isin, by_name, ""


def nav_map_row(symbol: str) -> dict:
    """Full nav_urls.txt row for a symbol: {code, isin, name} (editable mapping
    layer, user direction 2026-10-28).  Used by the absurd-iNAV diagnostics so
    a wrong mapping is self-explanatory (audit v6 #1)."""
    sym = (symbol or "").strip().upper()
    for fpath in (BASE_DIR / "nav_urls.txt", CACHE_DIR.parent / "nav_urls.txt"):
        try:
            txt = Path(fpath).read_text("utf-8", errors="replace")
        except Exception:
            continue
        for line in txt.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [x.strip() for x in line.split(";")]
            if len(parts) >= 2 and parts[0].upper() == sym:
                return {"code": parts[1],
                        "isin": parts[2] if len(parts) > 2 else "",
                        "name": parts[3] if len(parts) > 3 else ""}
    return {}


def nav_scheme_codes_effective(path=None) -> dict:
    """SYMBOL -> mfapi scheme code: the built-in NAV_SCHEME_CODES overlaid with
    any EXTRA lines the user adds to stock-data/nav_urls.txt (user request
    2026-10-28: keep the file read from the stock-data folder so a new ETF can
    be added there without touching the script).  Lines are
    SYMBOL;CODE;ISIN;NAME;URL;URL — '#' comments skipped; an override line's
    code wins over the built-in map for the same symbol."""
    codes = dict(NAV_SCHEME_CODES)
    p = Path(path) if path else (CACHE_DIR.parent / "nav_urls.txt")
    for fpath in (BASE_DIR / "nav_urls.txt", p):
        try:
            txt = Path(fpath).read_text("utf-8", errors="replace")
        except Exception:
            continue
        for line in txt.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [x.strip() for x in line.split(";")]
            if len(parts) >= 2 and parts[0] and parts[1].isdigit():
                codes[parts[0].upper()] = parts[1]
    return codes


def write_nav_urls_listing(path=None) -> None:
    """(Re)write stock-data/nav_urls.txt from the effective code map — the
    user's editable copy lives in the stock-data folder (never overwritten:
    once it exists the user's edits are the source of truth for extras)."""
    p = Path(path) if path else (CACHE_DIR.parent / "nav_urls.txt")
    if p.exists():
        return
    try:
        codes = nav_scheme_codes_effective()
        lines = ["# Direct NAV URLs for every tracked ETF — add a new ETF as a row\n"
                 "# SYMBOL;SCHEME_CODE and the NAV pipeline picks it up automatically.\n"
                 "# FORMAT: SYMBOL;CODE;ISIN;HISTORY URL;LATEST URL\n"]
        for sym in sorted(codes):
            c = codes[sym]
            lines.append(f"{sym};{c};;https://api.mfapi.in/mf/{c};"
                         f"https://api.mfapi.in/mf/{c}/latest")
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def mfapi_search(query: str, http_get=None, probe=None) -> list:
    """api.mfapi.in/mf/search?q=... -> [(scheme_code, scheme_name)].
    probe (mutable dict): fail-streak bookkeeping — 'fail' incremented on a
    transport error (>5 stops the caller's search loop), reset on clean HTTP."""
    get = http_get or _http_get
    txt, _e = get(MFAPI_SEARCH_URL.format(q=urllib.parse.quote(query or "")),
                  timeout=15)
    if not txt:
        if probe is not None:
            probe["fail"] = probe.get("fail", 0) + 1
            probe["err"] = _e
        return []
    try:
        payload = json.loads(txt)
    except ValueError:
        return []
    if probe is not None:
        probe["fail"] = 0           # clean transport, even when 0 schemes match
    out = []
    if isinstance(payload, list):
        for r in payload:
            try:
                out.append((str(r["schemeCode"]), str(r["schemeName"])))
            except (KeyError, TypeError):
                continue
    return out


_NAV_STOP = {"etf", "index", "fund", "the", "of", "and", "limited", "ltd"}


def _nav_tokens(s: str) -> list:
    return [t for t in re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split()
            if t and t not in _NAV_STOP]


def mfapi_guess_code(symbol: str, name: str, http_get=None, probe=None):
    """Scheme-code guess when the ISIN is NOT in any bulk snapshot.
    Deliberately conservative: the search must return exactly ONE candidate
    whose name covers every query token (unambiguous full cover) — otherwise
    return None and leave the file absent.  A missing NAV beats a WRONG NAV
    (a mis-mapped series would arm the iNAV filter against the wrong fund).
    `probe` (mutable dict) records TRANSPORT failures vs clean no-hits so the
    caller can circuit-break when the mirror is down instead of attempting
    one search storm per symbol (offline sandbox burn: 333 wasted calls)."""
    base = " ".join(_nav_tokens(name)) or symbol.lower()
    for q in (f"{base} etf", base, symbol.lower()):
        if probe is not None and probe.get("fail", 0) >= 5:
            return None
        cands = mfapi_search(q, http_get, probe=probe)
        seen = {}
        qt = set(_nav_tokens(q))
        for code, sname in cands:
            if qt and qt <= set(_nav_tokens(sname)) and code not in seen:
                seen[code] = sname
        if len(seen) == 1:
            return next(iter(seen.items()))
        time.sleep(HTTP_SLEEP)
    return None


def isin_of(instrument_key: str) -> str:
    """'NSE_EQ|INF769K01010' -> 'INF769K01010'; '' when the key carries no
    mutual-fund ISIN (Indian MF ISINs are INF + 9 alnum)."""
    tail = (instrument_key or "").split("|")[-1].strip().upper()
    return tail if re.fullmatch(r"INF[A-Z0-9]{9}", tail) else ""


def parse_amfi_navall(text: str) -> dict:
    """AMFI NAVAll.txt -> {isin: {code, name, nav, date}}.
    Handles BOTH layouts: the legacy 8-column row
    Code;ISIN(Div/Growth);ISIN(Div Reinvest);Name;NAV;Repurchase;Sale;Date
    and the NEW layout AMFI shipped by Oct-2026 with Plan/Option inserted —
    Code;ISIN(...);ISIN(...);Name;Plan;Option;NAV;Date  (the user's 2026-10-28
    run: 1.5 MB fetched, 0 rows parsed — every row failed float('Direct Plan')).
    NAV is probed at position 4 first, then 6; the date is always the LAST
    field.  Fund-house / category header lines fail the numeric-code or ISIN
    checks and are skipped."""
    out = {}
    for line in (text or "").splitlines():
        f = [x.strip() for x in line.split(";")]
        if len(f) < 6 or not f[0].isdigit():
            continue
        isin = f[1].split("/")[0].strip().upper()
        if not re.fullmatch(r"INF[A-Z0-9]{9}", isin):
            continue
        nav = None
        for idx in (4, 6):
            if idx < len(f):
                try:
                    nav = float(f[idx])
                    break
                except ValueError:
                    continue
        if nav is None:
            continue
        d = ""
        for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
            try:
                d = date_str(dt.datetime.strptime(f[-1], fmt).date())
                break
            except (ValueError, TypeError):
                continue
        if d:
            out[isin] = {"code": f[0], "name": f[3], "nav": nav, "date": d}
    return out


def parse_mfapi_history(payload: dict, min_date: str) -> list:
    """api.mfapi.in/mf/{code} JSON -> ascending unique [(YYYY-MM-DD, nav)]
    on/after min_date.  Payload rows are {'date': 'DD-MM-YYYY', 'nav': str},
    most recent first; junk rows are dropped."""
    if not isinstance(payload, dict):
        return []
    dd = {}
    for r in (payload.get("data") or []):
        try:
            d = date_str(dt.datetime.strptime(str(r["date"]).strip(), "%d-%m-%Y").date())
            dd[d] = float(r["nav"])
        except (KeyError, ValueError, TypeError):
            continue
    return sorted((d, nav) for d, nav in dd.items() if d >= min_date)


def _nav_retry_allowed(db: Db, folder=None) -> bool:
    """Success locks the rest of the day; failure cools off for
    NAV_FAIL_RETRY_HOURS — the 2026-10-27 one-attempt-per-day lock left the
    user unable to retry after a transient upstream failure.  2026-10-28 fix:
    the 'ok' lock is only real when the nav folder actually covers the whole
    ACTIVE universe — a day that created 8 of 111 files is NOT success
    (the user's box kept repeating the same thin 8/111 run all day)."""
    if db.meta_get("nav_autofetch_ok", "") == today_str():
        try:
            nav_dir = Path(folder) if folder else (CACHE_DIR.parent / "nav")
            have = {p.stem for p in nav_dir.glob("*.csv")}
            need = {r["symbol"] for r in db.query(
                "SELECT symbol FROM etf_universe WHERE status='ACTIVE'")}
            if have >= need:
                return False          # genuinely done for today
        except Exception:
            return False
        # partial coverage — fall through and re-attempt today
    fts = db.meta_get("nav_autofetch_fail", "")
    if fts:
        try:
            return (dt.datetime.now(IST).replace(tzinfo=None)
                    - dt.datetime.fromisoformat(fts)).total_seconds() > NAV_FAIL_RETRY_HOURS * 3600
        except Exception:
            return True
    return True


def ensure_nav_csvs(db: Db, folder=None, force: bool = False, http_get=None) -> dict:
    """Create stock-data/nav/{SYMBOL}.csv (symbol,date,nav) for active universe
    funds whose file is missing (force=True rewrites all).  Never clobbers
    user-supplied files.

    Resolution pipeline (user direction 2026-10-27 — the 111-call mfapi storm
    hit HTTP 429 rate limits on the user's box; Groww's bulk CSV is ONE call;
    user fix 2026-10-28 — the built-in ISIN-verified NAV_SCHEME_CODES map now
    short-circuits resolution entirely before any search):
      1. journal instrument_key (NSE_EQ|INF…) -> ISIN
      2. Groww bulk latest-NAV CSV  -> scheme code + latest NAV   [primary]
         (name-token match inside the same dump when the ISIN is absent)
      3. AMFI daily snapshot(s)     -> scheme code + latest NAV   [bulk fallback]
      4. NAV_SCHEME_CODES static map (AMFI/ISIN-verified 2026-10-28) -> code
      5. mfapi name search          -> scheme code                [last resort]
      6. mfapi history              -> daily NAV rows (NAV_HISTORY_DAYS),
         throttled + aborted on HTTP-429 so a sick mirror costs seconds,
         never minutes — the latest-NAV row is written FIRST, so a 429 wall
         can never leave a file empty.
    Diagnostics for every upstream failure are returned in 'diag'; the symbol
    -> scheme-code resolution is cached in stock-data/nav/_nav_map.json for
    review.  http_get is injectable for tests.  NAV_SSL_RELAX (AV/proxy MITM
    on the user's Windows box, 2026-10-28) selects the unverified fetcher;
    otherwise _nav_auto_get auto-falls back on CERTIFICATE_VERIFY_FAILED."""
    get = http_get or (lambda u, timeout=25: _http_get(u, timeout, verify=False)
                       if NAV_SSL_RELAX else _nav_auto_get(u, timeout))
    folder = Path(folder) if folder else (CACHE_DIR.parent / "nav")
    folder.mkdir(parents=True, exist_ok=True)
    uni = {r["symbol"]: r for r in db.query(
        "SELECT symbol, name, instrument_key FROM etf_universe "
        "WHERE status='ACTIVE' ORDER BY symbol")}
    targets = []
    for sym, r in uni.items():
        fp = folder / f"{sym}.csv"
        if force or not fp.exists():
            targets.append((sym, isin_of(r["instrument_key"]), fp))
    if not targets:
        return {"files": 0, "todo": 0, "failed": [], "diag": ""}
    g_isin, g_name, gdiag = fetch_groww_navall(get)
    diags = [gdiag] if gdiag else []
    amfi, adiag = {}, ""
    if not (g_isin or g_name):
        amfi, adiag = fetch_amfi_navall(get)   # only pay for AMFI when Groww is down
        if adiag:
            diags.append(adiag)
    min_date = date_str(parse_date(today_str()) - dt.timedelta(days=NAV_HISTORY_DAYS))
    t0 = time.time()
    rate_limited = False
    consec_429 = 0
    probe = {"fail": 0, "nagged": False, "aborted": False, "done_note": False, "err": ""}
    ok_syms, failed, via_search, via_groww, via_static, mapping = [], [], [], [], [], {}
    navcodes = nav_scheme_codes_effective()   # built-in map + user stock-data overlay
    for sym, isin, fp in targets:
        if time.time() - t0 > NAV_FETCH_BUDGET_SEC:
            failed.append(sym)
            continue
        # first full pass over 111 funds takes a few minutes — show life
        if len(ok_syms) % 25 == 0 and len(ok_syms) and not probe.get(f"p{len(ok_syms)}"):
            probe[f"p{len(ok_syms)}"] = True
            P.note(f"NAV bootstrap progress: {len(ok_syms)}/{len(targets)} funds done…")
        hit = (g_isin or {}).get(isin) if isin else None
        code, latest_nav, latest_date, source, gname = None, None, "", "", ""
        if hit:
            code, latest_nav, latest_date, source = hit["code"], hit["nav"], hit["date"], "groww-isin"
        elif isin and isin in amfi:
            hitA = amfi[isin]
            code, latest_nav, latest_date, source = hitA["code"], hitA["nav"], hitA["date"], "amfi-isin"
        elif sym in navcodes:
            # user-provided fix (2026-10-28): AMFI/ISIN-verified static map
            # (plus anything in stock-data/nav_urls.txt) — no live lookup
            # storm, no guessing.  Latest NAV is missing from this path, so
            # history backfill becomes the data source below.
            code, source = navcodes[sym], "static-map"
        else:
            guess = (groww_guess_code(sym, uni[sym]["name"], g_name, g_isin)
                     if g_name else None)
            if guess:
                code, latest_nav, latest_date, gname, source = (
                    guess["code"], guess["nav"], guess["date"], guess["name"], "groww-name")
            else:
                if probe["fail"] == 0 and not probe["nagged"] and not (g_isin or g_name or amfi):
                    probe["nagged"] = True
                if probe["fail"] < 5:
                    guess2 = mfapi_guess_code(sym, uni[sym]["name"], get, probe=probe)
                    if guess2:
                        code, gname, source = guess2[0], guess2[1], "mfapi-search"
                elif not probe["aborted"]:
                    probe["aborted"] = True
        if probe["aborted"] and not probe["done_note"]:
            probe["done_note"] = True
            diags.append("mfapi search unreachable (transport errors x5+) — remaining "
                         "name lookups skipped this run")
        if not code and not latest_nav:
            failed.append(sym)
            continue
        rows = {}
        if latest_nav and latest_date:
            rows[latest_date] = latest_nav
        if code and not rate_limited and time.time() - t0 <= NAV_FETCH_BUDGET_SEC:
            txt, e = get(MFAPI_HISTORY_URL.format(code=code), timeout=15)
            if txt:
                try:
                    rows.update(dict(parse_mfapi_history(json.loads(txt), min_date)))
                    consec_429 = 0
                except ValueError:
                    pass
            elif e and "429" in e:
                consec_429 += 1
                if consec_429 >= NAV_MAX_429:
                    rate_limited = True
                    diags.append(f"mfapi history -> HTTP 429 x{NAV_MAX_429} consecutive "
                                 f"(rate-limited — latest-NAV rows kept, history backfill "
                                 f"skipped; re-run tomorrow or later)")
                time.sleep(NAV_CALL_SLEEP_SEC)
            else:
                consec_429 = 0
                if e:
                    time.sleep(NAV_CALL_SLEEP_SEC)
        if not rows:
            failed.append(sym)
            continue
        with open(fp, "w", newline="", encoding="utf-8") as f:
            wcsv = csv.writer(f)
            wcsv.writerow(["symbol", "date", "nav"])
            for d, nav in sorted(rows.items()):
                wcsv.writerow([sym, d, f"{nav:.4f}"])
        ok_syms.append(sym)
        if source == "mfapi-search":
            via_search.append(sym)
        if source == "static-map":
            via_static.append(sym)
        if source.startswith("groww"):
            via_groww.append(sym)
        mapping[sym] = {"isin": isin, "code": code,
                        "scheme": hit["name"] if hit else (amfi.get(isin, {}).get("name") or gname),
                        "source": source}
        time.sleep(HTTP_SLEEP)
    try:
        with open(folder / "_nav_map.json", "w", encoding="utf-8") as f:
            json.dump(mapping, f, indent=1)
    except Exception:
        pass
    return {"files": len(ok_syms), "todo": len(targets), "failed": failed,
            "via_search": len(via_search), "via_groww": len(via_groww),
            "via_static": len(via_static),
            "diag": ";  ".join(d for d in diags if d),
            "error": "" if (g_isin or amfi or ok_syms) else "no NAV source reachable"}


def refresh_nav_csvs(db: Db, folder=None, http_get=None) -> int:
    """Daily one-call NAV top-up: merge the bulk dump's LATEST NAV row into
    existing stock-data/nav/*.csv files.

    Bootstrapped files used to freeze on creation day (user run 2026-10-28 —
    the iNAV filter compared a moving market against a frozen NAV).  Bulk
    sources only (Groww; AMFI when Groww is down) so this costs ONE network
    call; symbols not in the cached _nav_map.json mapping are left for the
    next ensure_nav_csvs() pass.  Returns the number of files advanced."""
    get = http_get or (lambda u, timeout=25: _http_get(u, timeout, verify=False)
                       if NAV_SSL_RELAX else _nav_auto_get(u, timeout))
    folder = Path(folder) if folder else (CACHE_DIR.parent / "nav")
    try:
        mapping = json.loads((folder / "_nav_map.json").read_text("utf-8"))
    except Exception:
        return 0
    if not isinstance(mapping, dict) or not mapping:
        return 0
    g_isin, _g_name, _gdiag = fetch_groww_navall(get)
    bulk = g_isin or {}
    if not bulk:
        bulk, _ = fetch_amfi_navall(get)
    if not bulk:
        # 2026-10-28 user fix: when BOTH bulk feeds fail (Groww 404, AMFI
        # mangled), top up from mfapi per-scheme /latest using the mapped
        # codes — the endpoint confirmed working on the user's box.
        latest = {}
        _codes = nav_scheme_codes_effective()
        for sym, m in mapping.items():
            code = m.get("code") or _codes.get(sym)
            if not code:
                continue
            txt, _e = get(MFAPI_HISTORY_URL.format(code=code) + "/latest", timeout=15)
            if not txt:
                continue
            try:
                rows = parse_mfapi_history(json.loads(txt), "0000-00-00")
            except ValueError:
                rows = []
            if rows:
                d, nav = rows[-1]
                latest[sym] = {"nav": nav, "date": d}
            time.sleep(NAV_CALL_SLEEP_SEC)
        if not latest:
            return 0
        n = 0
        for sym, rec in latest.items():
            fp = folder / f"{sym}.csv"
            if not fp.exists():
                continue
            try:
                rows = {}
                with open(fp, newline="", encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        rows[r["date"]] = float(r["nav"])
            except Exception:
                continue
            if rows.get(rec["date"]) == rec["nav"]:
                continue
            rows[rec["date"]] = rec["nav"]
            try:
                with open(fp, "w", newline="", encoding="utf-8") as f:
                    wcsv = csv.writer(f)
                    wcsv.writerow(["symbol", "date", "nav"])
                    for dd_, nn in sorted(rows.items()):
                        wcsv.writerow([sym, dd_, f"{nn:.4f}"])
                n += 1
            except Exception:
                continue
        return n
    by_code = {}
    for isin, rec in bulk.items():
        c = rec.get("code")
        if c and c not in by_code:
            by_code[c] = rec
    n = 0
    for sym, m in mapping.items():
        fp = folder / f"{sym}.csv"
        if not fp.exists():
            continue
        rec = bulk.get(m.get("isin") or "") or by_code.get(m.get("code") or "")
        if not rec or not rec.get("nav") or not rec.get("date"):
            continue
        try:
            nav_val = float(rec["nav"])
        except (TypeError, ValueError):
            continue
        try:
            rows = {}
            with open(fp, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    rows[r["date"]] = float(r["nav"])
        except Exception:
            continue
        if rows.get(rec["date"]) is not None and abs(rows[rec["date"]] - nav_val) < 1e-9:
            continue                      # already current at this date
        rows[rec["date"]] = nav_val
        try:
            with open(fp, "w", newline="", encoding="utf-8") as f:
                wcsv = csv.writer(f)
                wcsv.writerow(["symbol", "date", "nav"])
                for d, nav in sorted(rows.items()):
                    wcsv.writerow([sym, d, f"{nav:.4f}"])
            n += 1
        except Exception:
            continue
    return n


class DataManager:
    def __init__(self, db: Db, client: UpstoxClient):
        self.db = db
        self.client = client

    def series(self, symbol: str, as_of: str, limit: int = 252,
               field: str = "adj_close") -> list:
        """Ascending rows of (date, field) up to as_of — live-path helper
        used by the bear-regime check."""
        rows = self.db.query(
            f"SELECT date, {field} AS v FROM price_daily WHERE symbol=? AND date<=? "
            f"ORDER BY date DESC LIMIT ?", (symbol, as_of, limit))
        return list(reversed(rows))

    def adj_factor(self, symbol: str, date: str) -> float:
        """Cumulative adjustment factor from corporate_actions (default 1.0)."""
        rows = self.db.query(
            "SELECT factor FROM corporate_actions WHERE symbol=? AND ex_date<=? ORDER BY ex_date",
            (symbol, date))
        f = 1.0
        for r in rows:
            f *= float(r["factor"])
        return f

    def update_symbol(self, symbol: str, years: int = 11, force: bool = False) -> int:
        """
        Fetch missing daily bars for one symbol into price_daily. Returns bars stored.
        force=True (audit 2026-10-19): PURGE the symbol's stored bars first and
        re-download the full history — the only way to heal a series that mixes
        synthetic demo rows with real ones (a merged splice can never be trusted,
        and the freshness shortcut would otherwise say 'already up to date').
        """
        row = self.db.one("SELECT instrument_key FROM etf_universe WHERE symbol=?", (symbol,))
        key = row["instrument_key"] if row else f"NSE_EQ|{symbol}"
        to_date = TODAY
        from_date = to_date - dt.timedelta(days=int(years * 365.25))
        if force:
            self.db.execute("DELETE FROM price_daily WHERE symbol=?", (symbol,))
        else:
            last = self.db.one("SELECT MAX(date) AS d FROM price_daily WHERE symbol=?", (symbol,))
            if last and last["d"]:
                ld = parse_date(last["d"])
                # audit v12 P0-2: skip only when the stored last bar IS the
                # last completed NSE session (weekday minus holidays, today
                # only after 16:00).  The old 3-calendar-day shortcut skipped
                # Mon-Wed bars on every Fri/weekend run, while the caller
                # counted the skip as "current" — silently stale by 2 bars.
                if ld.isoformat() >= last_completed_session():
                    return 0
                from_date = ld - dt.timedelta(days=5)   # small overlap, upsert handles it
        try:
            bars = self.client.historical_daily(key, from_date, to_date)
        except UpstoxError as e:
            msg = str(e)
            if ("401" in msg or "access token" in msg.lower()
                    or "RATE_LIMIT" in msg):
                raise            # global problem -> abort the whole refresh once
            P.warn(f"{symbol}: cannot download history ({e})")
            log(f"{symbol}: download failed: {e}", "WARN")
            return 0
        if not bars:
            return 0
        rows = []
        for b in bars:
            f = self.adj_factor(symbol, b["date"])
            close = b["close"]
            rows.append({
                "symbol": symbol, "date": b["date"],
                # audit #9: OHLC stored RAW (the traded price); only adj_close
                # carries the adjustment factor.  Never mix scales.
                "open": b["open"], "high": b["high"],
                "low": b["low"], "close": close,
                "adj_close": close * f, "volume": b["volume"],
                "traded_value": close * b["volume"],
            })
        self.db.upsert_prices(symbol, rows)
        return len(rows)

    def recompute_adjustments(self, symbol: str = None) -> int:
        """
        Rebuild adj_close = raw close x cumulative factor for stored bars.
        Must run whenever corporate actions change — audit bug #6: previously
        only NEW bars were adjusted and history kept stale factors.
        """
        syms = [symbol] if symbol else [r["symbol"] for r in self.db.query(
            "SELECT DISTINCT symbol FROM price_daily")]
        n = 0
        for s in syms:
            rows = self.db.query("SELECT date, close FROM price_daily WHERE symbol=? ORDER BY date", (s,))
            if not rows:
                continue
            for r in rows:
                f = self.adj_factor(s, r["date"])
                self.db.execute(
                    "UPDATE price_daily SET adj_close=? WHERE symbol=? AND date=?",
                    (r["close"] * f, s, r["date"]))
                n += 1
        return n

    def update_all(self, years: int = 11, only: list = None, force: bool = False,
                   progress: bool = True) -> dict:
        P.section("MARKET DATA — DOWNLOAD & REFRESH", "\U0001F4E5")
        symbols = only or [r["symbol"] for r in self.db.query(
            "SELECT symbol FROM etf_universe WHERE status='ACTIVE' ORDER BY symbol")]
        stats = {"updated": 0, "skipped": 0, "failed": 0, "bars": 0, "stale": 0}
        total = len(symbols)
        if not progress:
            P.info(f"Checking {total} symbols against Upstox in quiet mode — "
                   f"per-symbol progress: python etf-momentum-screener.py update")
        for i, sym in enumerate(symbols, 1):
            try:
                n = self.update_symbol(sym, years=years, force=force)
            except UpstoxError as e:
                P.err(f"Stopping data refresh: {e}")
                if "RATE_LIMIT" in str(e):
                    P.note("Already-downloaded bars are safely stored in the database. "
                           "Rerun later — only the missing tail will be fetched.")
                else:
                    P.note("Update UPSTOX_ACCESS_TOKEN in env.txt (it expires daily) and rerun.")
                break
            if n > 0:
                stats["updated"] += 1
                stats["bars"] += n
                if progress:
                    P.raw(f"  {paint('[{}/{}]'.format(i, total), _C.GREY)} "
                          f"{sym:<12} downloaded {n:<5} bars   "
                          f"{paint('ok', _C.GREEN)}")
            else:
                st = self.db.one("SELECT COUNT(*) AS c, MAX(date) AS d FROM "
                                 "price_daily WHERE symbol=?", (sym,))
                if st and st["c"]:
                    if st["d"] and st["d"] >= last_completed_session():
                        stats["skipped"] += 1   # genuinely current
                        if progress:
                            P.raw(f"  {paint('[{}/{}]'.format(i, total), _C.GREY)} "
                                  f"{sym:<12} already up to date ({st['c']} bars)   "
                                  f"{paint('skip', _C.YELLOW)}")
                    else:
                        # audit v12 P0-2: the feed returned nothing new while
                        # the stored bar is BEHIND the last completed session
                        # — that is stale, not 'current'.  Say so.
                        stats["stale"] += 1
                        P.raw(f"  {paint('[{}/{}]'.format(i, total), _C.GREY)} "
                              f"{sym:<12} STALE — last bar {st['d']}, feed gave "
                              f"nothing new   {paint('stale', _C.RED)}")
                else:
                    stats["failed"] += 1
                    P.raw(f"  {paint('[{}/{}]'.format(i, total), _C.GREY)} "
                          f"{sym:<12} NO DATA (will be excluded)   "
                          f"{paint('missing', _C.RED)}")
        _summ = (f"Data refresh done: {stats['updated']} updated, "
                 f"{stats['skipped']} current, {stats['failed']} without data, ")
        if stats["stale"]:
            _summ += (f"{stats['stale']} STALE (feed gave no new bars — check "
                      f"Upstox/network; analysis below reads those stale prices), ")
        P.ok(_summ + f"{stats['bars']:,} bars written.")
        self.db.meta_set("last_data_update", now_iso())
        return stats

    def load_corporate_actions(self) -> int:
        """
        Load stock-data/corporate-actions/*.csv  (symbol,ex_date,factor,note).
        Idempotent (audit 2.2): UNIQUE(symbol,ex_date,note) + INSERT OR REPLACE —
        loading the same file twice can NEVER compound factors.

        FACTOR CONVENTION (differs from the usual pre-split convention!):
        factors restate prices FROM THE EX-DATE ONWARD into today's scale.
        A 1:2 split (price halves at the ex-date) therefore needs factor 2.0,
        so that adj_close = raw x 2.0 for dates >= ex-date makes the series
        continuous with today's prices.  note='DIV...' marks distributions
        (slab haircut applies on reinvestment).
        """
        folder = CACHE_DIR.parent / "corporate-actions"
        folder.mkdir(parents=True, exist_ok=True)
        n = 0
        # audit 2.2: remove any legacy duplicate rows first
        self.db.execute(
            "DELETE FROM corporate_actions WHERE id NOT IN "
            "(SELECT MIN(id) FROM corporate_actions GROUP BY symbol, ex_date, note)")
        for fp in sorted(folder.glob("*.csv")):
            with open(fp, newline="", encoding="utf-8") as f:
                for row in csv.reader(f):
                    if not row or row[0].strip().upper() == "SYMBOL" or len(row) < 3:
                        continue
                    sym, ex, factor = row[0].strip(), row[1].strip(), float(row[2])
                    note = row[3].strip() if len(row) > 3 else ""
                    self.db.execute(
                        "INSERT OR REPLACE INTO corporate_actions(symbol,ex_date,factor,note) "
                        "VALUES(?,?,?,?)",
                        (sym, ex, factor, note))
                    n += 1
        if n:
            self.recompute_adjustments()
        return n

    def load_nav_data(self, folder=None) -> int:
        """Load stock-data/nav/*.csv  (symbol,date,nav) into nav_data — enables the
        real iNAV premium/discount filter (audit bug #6).  folder override is
        used by the selftests; production callers leave it None."""
        folder = Path(folder) if folder else (CACHE_DIR.parent / "nav")
        folder.mkdir(parents=True, exist_ok=True)
        n = 0
        for fp in sorted(folder.glob("*.csv")):
            with open(fp, newline="", encoding="utf-8") as f:
                for row in csv.reader(f):
                    if not row or row[0].strip().upper() == "SYMBOL" or len(row) < 3:
                        continue
                    sym, d, nav = row[0].strip(), row[1].strip(), float(row[2])
                    self.db.execute(
                        "INSERT INTO nav_data(symbol,date,nav) VALUES(?,?,?) "
                        "ON CONFLICT(symbol,date) DO UPDATE SET nav=excluded.nav",
                        (sym, d, nav))
                    n += 1
        return n

    def coverage_table(self):
        rows = self.db.query(
            "SELECT symbol, MIN(date) AS f, MAX(date) AS l, COUNT(*) AS n "
            "FROM price_daily GROUP BY symbol ORDER BY symbol")
        out = []
        for r in rows:
            n = r["n"]
            tag = "ready" if n >= MIN_HISTORY_BARS else ("thin" if n >= 60 else "insufficient")
            out.append([r["symbol"], r["f"], r["l"], fmt_num(n, 0), tag])
        P.section("PRICE DATA COVERAGE", "\U0001F4C8")
        def rc(i, row):
            return _C.GREEN if row[4] == "ready" else (_C.YELLOW if row[4] == "thin" else _C.RED)
        P.table(["Symbol", "From", "To", "Bars", "Signal-ready?"], out,
                aligns=["<", "<", "<", ">", "<"], row_color=rc)
        return rows

    def coverage_summary(self):
        """One-glance coverage status for the daily run.  The per-symbol table
        stays available via: python etf-momentum-screener.py update"""
        rows = self.db.query(
            "SELECT symbol, MIN(date) AS f, MAX(date) AS l, COUNT(*) AS n "
            "FROM price_daily GROUP BY symbol")
        ready = sum(1 for r in rows if r["n"] >= MIN_HISTORY_BARS)
        thin = sorted(r["symbol"] for r in rows if 60 <= r["n"] < MIN_HISTORY_BARS)
        insuff = sorted(r["symbol"] for r in rows if r["n"] < 60)
        latest = self.db.one("SELECT MAX(date) AS d FROM price_daily")
        P.section("PRICE DATA COVERAGE", "\U0001F4C8")
        P.kv("Last price bar on", (latest["d"] if latest and latest["d"] else "n/a"))
        P.kv("Signal-ready", f"{ready} of {len(rows)} ETFs "
             f"({MIN_HISTORY_BARS}+ bars of history)", val_color=_C.GREEN)
        if thin:
            P.kv("Thin history",
                 f"{len(thin)} funds — excluded from signals until they mature: "
                 + (", ".join(thin[:6]) + (f" +{len(thin)-6} more" if len(thin) > 6 else "")),
                 val_color=_C.YELLOW)
        if insuff:
            P.kv("Insufficient history",
                 f"{len(insuff)} funds — too new to trade: "
                 + (", ".join(insuff[:6]) + (f"+{len(insuff)-6} more" if len(insuff) > 6 else "")),
                 val_color=_C.RED)
        P.note("Per-symbol coverage table:  python etf-momentum-screener.py update")
        return rows


# ==============================================================================
# SECTION 11 — MOMENTUM SIGNAL (Step 2) & THRESHOLDS (Step 3)
# ==============================================================================

def momentum_at(adj: list, i: int, lookback: int = LOOKBACK, skip: int = SKIP):
    """
    M = P_adj(t-skip) / P_adj(t-lookback) - 1     (trading-session offsets)
    adj: ascending list of adjusted closes, i = index of signal date.
    Returns None when history is insufficient.
    """
    if i - skip < 0 or i - lookback < 0:
        return None
    if i - lookback >= len(adj) or i - skip >= len(adj):
        return None
    p1 = adj[i - skip]
    p0 = adj[i - lookback]
    if p0 is None or p1 is None or p0 == 0:
        return None
    return p1 / p0 - 1.0


def signal_momentum(adj: list, i: int, lookback: int = LOOKBACK, skip: int = SKIP):
    """Production momentum signal: the single LOOKBACK/SKIP 12-1 rule."""
    return momentum_at(adj, i, lookback, skip)


def market_sessions_to(db, frm: str, to: str) -> int:
    """COUNT of downloaded market sessions in (frm, to] — audit v10: the old
    calendar-days x 5/7 guess mis-counted holidays and long weekends."""
    r = db.one("SELECT COUNT(DISTINCT date) AS n FROM price_daily "
               "WHERE date>? AND date<=?", (frm, to))
    return int((r or {"n": 0})["n"] or 0)


def market_open_now(now: dt.datetime = None) -> bool:
    """Regular NSE cash session in IST; holidays come from the repo calendar."""
    now = now or dt.datetime.now(IST).replace(tzinfo=None)
    return (now.weekday() < 5 and now.date().isoformat() not in _nse_holidays()
            and dt.time(9, 15) <= now.time() < dt.time(15, 30))


def live_order_ok(context: str = "") -> bool:
    """2026-10-29 user direction: the --confirm CLI flag is GONE — live order
    placement asks a runtime y/n instead.  A human at a terminal must type 'y'
    each run; cron/non-interactive shells cannot confirm, so real orders are
    refused there (data/refresh/analysis still run).  Anything else is treated
    as NO — real money must never ride on a default."""
    if not sys.stdin.isatty():
        P.err("LIVE MODE but no interactive console — real orders REFUSED this "
              "run (analysis only).  Run from a terminal to place orders.")
        return False
    P.warn("LIVE MODE — REAL ORDERS WILL BE SENT TO UPSTOX."
           + (f"  {context}" if context else ""))
    try:
        ans = input("  Type y to allow REAL orders today (anything else = "
                    "analysis only): ")
    except (EOFError, KeyboardInterrupt):
        ans = ""
    return ans.strip().lower() == "y"


def _nse_holidays() -> set:
    try:
        hp = BASE_DIR / "nse_holidays.json"
        if hp.exists():
            return set(json.loads(hp.read_text()))
    except Exception:
        pass
    return set()


def last_completed_session(now: dt.datetime = None) -> str:
    """Last NSE session whose daily bar is downloadable: today itself only on
    a market day AFTER ~16:00 IST, else the previous weekday minus known
    holidays.  audit v12 P0-2 (2026-10-29 live audit): the update path used a
    3-calendar-day freshness shortcut, so a Friday run skipped Wednesday/
    Thursday bars forever and then labelled them 'current'."""
    hols = _nse_holidays()
    now = now or dt.datetime.now(IST).replace(tzinfo=None)
    d = now.date()
    if d.weekday() >= 5 or d.isoformat() in hols or now.time() < dt.time(16, 0):
        d -= dt.timedelta(days=1)
    while d.weekday() >= 5 or d.isoformat() in hols:
        d -= dt.timedelta(days=1)
    return d.isoformat()


def prev_completed_session(iso_day: str) -> str:
    """The NSE session BEFORE iso_day (weekends + known holidays skipped).
    Companion to last_completed_session — used for the one-session grace
    window on live order freshness (audit v14 P1)."""
    hols = _nse_holidays()
    d = parse_date(iso_day) - dt.timedelta(days=1)
    while d.weekday() >= 5 or d.isoformat() in hols:
        d -= dt.timedelta(days=1)
    return d.isoformat()


def sessions_to_calendar(today: str, target: str) -> int:
    """Weekdays minus known NSE holidays strictly in (today, target] — the
    CALENDAR count, independent of the price table.  Audit v11 P0-1
    (2026-10-28): the v10 helper market_sessions_to() counts downloaded bars,
    which can NEVER contain a future date, so a start-of-day run always saw 0
    sessions left and stale-signal age was understated. The strict live SIP
    cap no longer uses deployment pacing, but signal freshness still needs a
    calendar-based market-day count."""
    hols = set()
    try:
        hp = BASE_DIR / "nse_holidays.json"
        if hp.exists():
            hols = set(json.loads(hp.read_text()))
    except Exception:
        pass
    t = parse_date(today); n = parse_date(target)
    cnt = 0
    d = t + dt.timedelta(days=1)
    while d <= n:
        if d.weekday() < 5 and d.isoformat() not in hols:
            cnt += 1
        d += dt.timedelta(days=1)
    return cnt


def live_buy_order_px(px: float):
    """(order_type, maximum price) for a live SIP buy."""
    if LIVE_ORDER_TYPE == "LIMIT":
        return "LIMIT", round(float(px) * (1.0 + LIVE_LIMIT_BUFFER), 2)
    return "MARKET", float(px)


def sip_orphan_should_sell(sym: str, target: dict, chosen_syms: set,
                           sip_mode: bool) -> bool:
    """Held symbol missing from the target map -> orphan sell.  audit v15 P0-2:
    in SIP_MODE the map omits chosen names by design, so a held+CHOSEN holding
    must be KEPT (the live console had the rebalance dumping its own #1/#2
    fresh picks).  Cash sleeves are never orphans (they ARE the parking)."""
    if sym in target or sym in CASH_PROXY_SYMBOLS:
        return False
    if sip_mode and sym in chosen_syms:
        return False
    return True


def signal_closeable(exits_outstanding: int, exits_failed: list) -> bool:
    """A month-end signal may seal (`last_trade_signal` written) only once its
    exits are physically done — not sliced/queued AND not broker-failed.
    audit v15 P0-3: an EDIS-rejected exit used to seal the signal anyway and
    was never retried."""
    return exits_outstanding == 0 and not exits_failed


def day_slices(gaps: dict, budget: float, caps: dict = None) -> dict:
    """Split today's deployment budget across plan gaps — exactly ONE slice
    (=> one order) per symbol.  Audit v5 #3 (2026-10-28): the old 3-pass loop
    gave each name ~1/3 of its share per pass whenever gaps were bigger than
    the budget, fragmenting every day into up to 3 orders per ETF — 3x
    per-order charges, and it defeated the 15%-of-p25 liquidity cap because
    that cap is written per-ORDER (3 same-day orders tripled the intended
    exposure; the console showed 9 orders for 3 ETFs).
    Each pass computes ALL shares off one frozen (remaining, pool) snapshot
    (fairness: no order-of-iteration bias), clips at gaps and per-day caps,
    drops filled/cap-exhausted names, and redistributes only the clipped
    slack. Whatever cannot fill waits for a later run or the next IST day."""
    todo = {s: float(g) for s, g in (gaps or {}).items() if g >= MIN_TRADE_VALUE}
    caps = {s: max(0.0, min(float(c), todo[s]))
            for s, c in (caps or {}).items() if c is not None and s in todo}
    out = {}
    remaining = min(float(budget), sum(todo.values()))
    while remaining >= MIN_TRADE_VALUE and todo:
        pool = sum(todo.values())
        if pool <= 0:
            break
        applied = 0.0
        for s in sorted(todo):                     # deterministic pass
            share = min(todo[s], remaining * todo[s] / pool)
            if s in caps:
                share = min(share, caps[s] - out.get(s, 0.0))
            share = max(0.0, share)
            if share < MIN_TRADE_VALUE:
                continue
            out[s] = out.get(s, 0.0) + share
            todo[s] -= share
            applied += share
        remaining -= applied
        # filled names and cap-exhausted names leave the pool before the
        # next redistribution pass
        for s in list(todo):
            room = caps.get(s, float("inf")) - out.get(s, 0.0)
            if todo[s] < MIN_TRADE_VALUE or room < MIN_TRADE_VALUE:
                del todo[s]
        if applied < MIN_TRADE_VALUE:
            # audit v11 P0-2 (2026-10-28 console audit): when the daily budget
            # split across N plan names drops EVERY share under MIN_TRADE_VALUE
            # (5000/3 = 1667 on the 2026-10-28 run), the loop applied nothing
            # and bought ZERO into a half-empty Rs 5.3 L plan.  Concentrate the
            # whole remainder on the single largest-gap name instead — one
            # order, one round-trip (audit v5 #3's one-order-per-day and the
            # min-trade floor both stay intact).  Tomorrow's run then fills the
            # next-largest gap; the basket completes over a few runs.
            if todo and remaining >= MIN_TRADE_VALUE:
                for _big in sorted(todo, key=lambda s: -todo[s]):
                    _room = caps.get(_big, float("inf")) - out.get(_big, 0.0)
                    _alloc = min(todo[_big], remaining, _room)
                    if _alloc >= MIN_TRADE_VALUE:
                        out[_big] = out.get(_big, 0.0) + _alloc
                        todo[_big] -= _alloc
                        remaining -= _alloc
                        applied = _alloc
                        break
            if applied < MIN_TRADE_VALUE:
                break                            # dust remainder stops
    return {s: v for s, v in out.items() if v >= MIN_TRADE_VALUE}


def deploy_budget_remaining(meta_val: str, today: str, amount: float):
    """Legacy journal display helper. Live reservations are authoritative in
    the orders table; this reads the old summary only for migration reports."""
    try:
        d = json.loads(meta_val or "{}")
    except Exception:
        d = {}
    already = float(d.get("deployed", 0.0)) if d.get("date") == today else 0.0
    return already, max(0.0, float(amount or 0.0) - already)


def exit_rules_fire(adj):
    """The strategy's per-holding exit trigger as a pure function over an
    adjusted-close list (the R12A daily-exit kernel): momentum <= EXIT_THRESHOLD
    at the last bar, or the last
    close below its 200-DMA.  False when the history is too short to judge."""
    if not adj or len(adj) < LOOKBACK + 1:
        return False
    t = len(adj) - 1
    mom = signal_momentum(adj, t)
    if mom is not None and mom <= EXIT_THRESHOLD:
        return True
    if len(adj) >= 200:
        seg = adj[-200:]
        if adj[t] < sum(seg) / len(seg):
            return True
    return False


def daily_returns(closes: list) -> list:
    out = []
    for i in range(1, len(closes)):
        a, b = closes[i - 1], closes[i]
        if a and b and a > 0:
            out.append(b / a - 1.0)
    return out


def row_adj(r):
    """
    adj_close from a price row of ANY shape (audit hotfix): the live
    stats_at() passes raw SQLite rows (date, adj_close, close, traded_value) —
    never assume a fixed column count.
    """
    try:
        return r["adj_close"]
    except (TypeError, KeyError, IndexError, AttributeError):
        return r[1]


def series_integrity_issue(rows) -> str:
    """
    Data-integrity gate (audit 2026-10-19).  Flags a price series as UNTRUSTABLE
    when it shows signs of a synthetic/real splice or unadjusted corporate action:
      - any daily |move| > 40% in the trailing 260 bars (an unleveraged ETF
        cannot legitimately do this; a splice/split can),
      - trailing 60-day annualised vol > 150%,
      - |12-1 momentum| > 300%.
    Accepts raw values or price rows (any column shape).  Returns '' when sane.
    Ceilings live in INTEG_MAX_DAILY/VOL/MOM (external audit F2.6 — harness
    variants test tighter gates; defaults stay until measured).
    """
    vals = []
    for r in rows:
        try:
            v = float(r) if isinstance(r, (int, float)) else float(row_adj(r))
        except Exception:
            continue
        vals.append(v)
    seg = vals[-260:]
    if len(seg) < 30:
        return ""
    for i in range(1, len(seg)):
        if seg[i - 1] and abs(seg[i] / seg[i - 1] - 1.0) > INTEG_MAX_DAILY:
            return ("implausible daily price jump — series looks spliced or "
                    "unadjusted")
    rets = [seg[i] / seg[i - 1] - 1.0 for i in range(1, len(seg)) if seg[i - 1]]
    w = rets[-60:]
    if len(w) >= 30:
        m = sum(w) / len(w)
        vol = math.sqrt(sum((x - m) ** 2 for x in w) / (len(w) - 1)) * math.sqrt(252.0)
        if vol > INTEG_MAX_VOL:
            return f"implausible volatility ({vol:.0%} annualised) — series looks spliced"
    if len(seg) >= 253 and seg[-253]:
        mom = seg[-22] / seg[-253] - 1.0
        if abs(mom) > INTEG_MAX_MOM:
            return f"implausible 12-1 momentum ({mom:+.0%}) — series looks spliced"
    return ""


def date_returns(rows) -> dict:
    """{date: simple return} from rows of (date, adj_close[, ...]). Works with
    sqlite Rows, tuples or dicts — always date-aligned (audit bug #9)."""
    out = {}
    prev = None
    for r in rows:
        try:
            d = r["date"] if isinstance(r, sqlite3.Row) or isinstance(r, dict) else r[0]
            a = r["adj_close"] if isinstance(r, sqlite3.Row) or isinstance(r, dict) else r[1]
        except Exception:
            continue
        if prev is not None and prev and a and prev > 0:
            out[d] = a / prev - 1.0
        prev = a
    return out


def aligned_return_matrix(series_by_sym: dict, window: int):
    """Date-aligned return matrix for covariance work (audit bug #9)."""
    rets = {s: date_returns(rows) for s, rows in series_by_sym.items()}
    common = set.intersection(*[set(r.keys()) for r in rets.values()]) if rets else set()
    dates = sorted(common)[-window:]
    syms = list(series_by_sym.keys())
    X = [[rets[s][d] for s in syms] for d in dates]
    return X, syms, dates


def annualized_vol(closes: list, window: int = VOL_WINDOW) -> float:
    """
    Annualised volatility (audit 3.2): the short-window estimate, optionally
    blended with the 252-day estimate as
        vol = sqrt((1-w) * v60^2 + w * v252^2),   w = VOL_BLEND_252.
    Blending damps vol spikes that make the engine whipsaw.
    """
    rets = daily_returns(closes)
    v60 = stdev(rets[-window:], ddof=1) * math.sqrt(252.0)
    if VOL_BLEND_252 > 0 and len(rets) >= 252 + 1:
        v252 = stdev(rets[-252:], ddof=1) * math.sqrt(252.0)
        return math.sqrt((1.0 - VOL_BLEND_252) * v60 ** 2
                         + VOL_BLEND_252 * v252 ** 2)
    return v60


def month_end_indices(dates: list) -> list:
    """Indices of the last trading session of each month."""
    out = []
    cur_key = None
    for i, d in enumerate(dates):
        k = month_key(parse_date(d))
        if k != cur_key:
            cur_key = k
            out.append(i)  # tentatively the first day of month
        else:
            out[-1] = i    # keep moving to the last day of the month
    return out


def last_month_end_date(all_dates: list, today: Optional[str] = None):
    """
    Last COMPLETED month-end session (audit bug #1, tightened 2.9):
      - if the newest bar belongs to the CURRENT calendar month (vs `today`),
        that month is still in progress and must not signal;
      - a truncated final month is dropped only when the last bar is earlier
        than (the month's expected last weekday - 3 days) — never merely
        because the bar lands on the 24th (holidays!).
    `today` makes the function date-independent (selftests pass their own).
    """
    if not all_dates:
        return None
    idx = month_end_indices(all_dates)
    if not idx:
        return all_dates[-1]
    last_bar = parse_date(all_dates[-1])
    today_d = parse_date(today) if today else parse_date(today_str())
    current_month = month_key(last_bar) == month_key(today_d)
    import calendar as _cal
    last_day = _cal.monthrange(last_bar.year, last_bar.month)[1]
    exp = dt.date(last_bar.year, last_bar.month, last_day)
    while exp.weekday() >= 5:
        exp -= dt.timedelta(days=1)         # expected last weekday of month
    truncated = last_bar < exp - dt.timedelta(days=3)
    if len(idx) >= 2 and (current_month or truncated):
        idx = idx[:-1]           # final month still in progress / data ends mid-month
    return all_dates[idx[-1]]


def decide(momentum: float, holding: bool, entry: float = ENTRY_THRESHOLD,
           exit_thr: float = EXIT_THRESHOLD):
    """Step 3 threshold table -> (decision, reason)."""
    if momentum is None:
        return "SKIPPED", "not enough price history (needs 252 sessions)"
    if holding:
        if momentum > exit_thr:
            return "HOLD", f"trend still positive ({fmt_pct(momentum)}) — keep holding"
        return "EXIT", f"trend turned negative ({fmt_pct(momentum)} <= 0%) — sell"
    else:
        if momentum > entry:
            return "ELIGIBLE_BUY", f"uptrend confirmed ({fmt_pct(momentum)} > {fmt_pct(entry)}) — buy candidate"
        return "CASH", f"trend too weak ({fmt_pct(momentum)} <= {fmt_pct(entry)}) — stay in cash"


# --- Daily SIP helpers (user request 2026-10-19) -------------------------------

def derisk_sell_qty(qty: int, level: str) -> int:
    """Units to SELL when a risk trigger fires: SEVERE -> half, EXTREME -> all.
    REVIEW never sells.  A 1-unit position cannot be halved -> 0 (noted)."""
    qty = int(qty)
    if qty <= 0:
        return 0
    if level == "EXTREME":
        return qty
    if level == "SEVERE":
        return qty // 2
    return 0


def sip_buys_allowed(state: str) -> bool:
    """Daily SIP buys pause while the risk engine is in SEVERE/EXTREME."""
    return state not in SIP_RISK_PAUSE


def signal_is_stale(db, as_of: str, today: str = None, limit: int = None):
    """(stale?, age_in_market_days) — audit v5 #2 / v6 #2 (2026-10-28):
    a signal (or stored basket) older than STALE_REFRESH_DAYS market sessions
    must be RE-DERIVED from today's data, never executed late.  One shared
    helper: run(), run_daily() and deploy_day() used to count market days in
    three slightly different ways."""
    lim = STALE_REFRESH_DAYS if limit is None else limit
    if not as_of or not lim:
        return False, 0
    today = today or today_str()
    row = db.one("SELECT COUNT(DISTINCT date) AS n FROM price_daily "
                 "WHERE date>? AND date<=?", (as_of, today))
    age = int((row or {"n": 0})["n"] or 0)
    return age > lim, age


def nav_last_jump(db, symbol: str) -> float:
    """|last two stored NAVs ratio - 1| (0.0 when fewer than two rows) —
    a >20% one-step jump means the NAV series changed scale (mapping broke),
    which no real NAV ever does.  Shared by collect_stats and navdiag."""
    rr = db.query("SELECT nav FROM nav_data WHERE symbol=? "
                  "ORDER BY date DESC LIMIT 2", (symbol,))
    if len(rr) == 2 and rr[1]["nav"]:
        return abs(rr[0]["nav"] / rr[1]["nav"] - 1.0)
    return 0.0


def inav_tier(dev, nav_jump: float = 0.0, limit=None) -> str:
    """Classify a signed close-vs-NAV deviation (audit v7, 28-Oct-2026,
    measured): 'corrupt'  = broken scheme mapping (|dev| beyond 300% or a
    >20% one-step NAV scale jump) — the only case excluded as bad data;
    'premium' = dev above the allowed band — a REAL market premium (SEBI
    overseas-unit freeze on global ETFs), buy-banned but never mislabelled;
    'ok' = inside the band (discounts are never blocked — underpaying is fine)."""
    if dev is None:
        return "unknown"
    if abs(nav_jump or 0.0) > 0.20 or abs(dev) > INAV_CORRUPT_DEV:
        return "corrupt"
    if limit is not None and dev > limit:
        return "premium"
    return "ok"


def next_month_end_hint(today: Optional[str] = None) -> str:
    """Best-effort date of the month-end trading session of the CURRENT month
    (calendar last day adjusted for weekends and known NSE holidays) — i.e.
    when the next rebalance signal will be computed."""
    import calendar as _cal
    t = parse_date(today) if today else parse_date(today_str())
    last_day = _cal.monthrange(t.year, t.month)[1]
    d = dt.date(t.year, t.month, last_day)
    hols = set()
    try:
        hp = BASE_DIR / "nse_holidays.json"
        if hp.exists():
            hols = set(json.loads(hp.read_text()))
    except Exception:
        pass
    while d.weekday() >= 5 or date_str(d) in hols:
        d -= dt.timedelta(days=1)
    return date_str(d)


def quote_session_date(ts) -> Optional[str]:
    """Best-effort 'YYYY-MM-DD' from a quote timestamp (ISO string or epoch)."""
    if ts is None:
        return None
    s = str(ts).strip()
    if not s:
        return None
    if s.replace(".", "", 1).isdigit():
        try:
            x = float(s)
            if x > 1e12:                       # epoch milliseconds
                x /= 1000.0
            return date_str(dt.datetime.fromtimestamp(x, IST).date())
        except Exception:
            return None
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return None


def quote_is_today(ts, today: str) -> Optional[bool]:
    """True/False when the quote timestamp is readable; None = unknown format."""
    d = quote_session_date(ts)
    if d is None:
        return None
    return d == today


# ==============================================================================
# SECTION 12 — LEDOIT-WOLF SHRINKAGE COVARIANCE (constant-correlation target)
# ==============================================================================

def mean_vector(X):
    n = len(X[0]) if X else 0
    return [sum(row[j] for row in X) / len(X) for j in range(n)] if X and n else []


def demean(X):
    mu = mean_vector(X)
    return [[row[j] - mu[j] for j in range(len(mu))] for row in X]


def sample_cov(X, ddof: int = 0):
    """X: T x N (already demeaned for ddof=0 MLE form)."""
    T = len(X)
    N = len(X[0]) if T else 0
    S = [[0.0] * N for _ in range(N)]
    if T == 0:
        return S
    for t in range(T):
        x = X[t]
        for i in range(N):
            xi = x[i]
            if xi == 0.0:
                continue
            for j in range(i, N):
                S[i][j] += xi * x[j]
    den = T if ddof == 0 else max(1, T - 1)
    for i in range(N):
        for j in range(i, N):
            S[i][j] /= den
            S[j][i] = S[i][j]
    return S


def ledoit_wolf_cc(X):
    """
    Ledoit-Wolf-style shrinkage of the sample covariance toward a
    CONSTANT-CORRELATION target (Ledoit & Wolf 2003/2004).

    X: T x N matrix of demeaned daily returns.
    Returns (Sigma_shrunk, delta, cond_estimate).

    Shrinkage intensity (plug-in form of the LW intensity):
        delta* = sum_ij Var_hat(s_ij)  /  sum_ij (s_ij - f_ij)^2 ,  clamped to [0,1]
        Var_hat(s_ij) = (1/T) * mean_t ( x_ti x_tj - s_ij )^2
    Target F:  f_ii = s_ii,  f_ij = rbar * sqrt(s_ii s_jj)

    HONESTY NOTE (audit #9): this is the plug-in LW intensity — it omits the
    rho correction term of the full LW2004 estimator.  For the small N used
    here (<= a few holdings) the difference is immaterial, but this is not a
    byte-for-byte reproduction of the paper's formula.
    """
    T = len(X)
    N = len(X[0]) if T else 0
    if T == 0 or N == 0:
        return [[0.0] * N for _ in range(N)], 1.0, 1.0
    S = sample_cov(X, ddof=0)
    # average correlation
    num = 0.0
    den = 0
    for i in range(N):
        for j in range(i + 1, N):
            di, dj = S[i][i], S[j][j]
            if di > 0 and dj > 0:
                num += S[i][j] / math.sqrt(di * dj)
                den += 1
    rbar = (num / den) if den else 0.0
    rbar = clamp(rbar, -0.99, 0.99)
    # target matrix F
    F = [[0.0] * N for _ in range(N)]
    for i in range(N):
        for j in range(N):
            if i == j:
                F[i][j] = S[i][i]
            else:
                di, dj = S[i][i], S[j][j]
                F[i][j] = rbar * math.sqrt(max(di, 0.0) * max(dj, 0.0))
    # plug-in variance of cov entries and distance to target
    pi = 0.0
    for t in range(T):
        x = X[t]
        for i in range(N):
            for j in range(N):
                d = x[i] * x[j] - S[i][j]
                pi += d * d
    pi /= float(T * T)          # sum_ij Var_hat(s_ij)
    dist = 0.0
    for i in range(N):
        for j in range(N):
            dd = S[i][j] - F[i][j]
            dist += dd * dd
    delta = clamp(pi / dist, 0.0, 1.0) if dist > 0 else 1.0
    Sigma = [[(1.0 - delta) * S[i][j] + delta * F[i][j] for j in range(N)] for i in range(N)]
    cond = condition_number(Sigma) if N <= 20 else diag_condition_proxy(Sigma)
    return Sigma, delta, cond


def diag_condition_proxy(Sigma):
    d = [Sigma[i][i] for i in range(len(Sigma)) if Sigma[i][i] > 0]
    return (max(d) / min(d)) if d else 1.0


def condition_number(Sigma):
    """Jacobi eigenvalue condition number (symmetric matrices, N small)."""
    n = len(Sigma)
    A = [row[:] for row in Sigma]
    for sweep in range(50):
        off = 0.0
        for i in range(n):
            for j in range(i + 1, n):
                off += A[i][j] * A[i][j]
        if off < 1e-18:
            break
        for p in range(n - 1):
            for q in range(p + 1, n):
                if abs(A[p][q]) < 1e-15:
                    continue
                theta = (A[q][q] - A[p][p]) / (2.0 * A[p][q])
                t = math.copysign(1.0, theta) / (abs(theta) + math.sqrt(theta * theta + 1.0))
                c = 1.0 / math.sqrt(t * t + 1.0)
                s = t * c
                for k in range(n):
                    akp, akq = A[k][p], A[k][q]
                    A[k][p] = c * akp - s * akq
                    A[k][q] = s * akp + c * akq
                for k in range(n):
                    apk, aqk = A[p][k], A[q][k]
                    A[p][k] = c * apk - s * aqk
                    A[q][k] = s * apk + c * aqk
    ev = [A[i][i] for i in range(n)]
    lo = min(ev)
    hi = max(ev)
    if lo <= 0:
        return float("inf")
    return hi / lo


def portfolio_vol(weights: list, Sigma) -> float:
    n = len(weights)
    v = 0.0
    for i in range(n):
        wi = weights[i]
        if wi == 0:
            continue
        for j in range(n):
            v += wi * weights[j] * Sigma[i][j]
    return math.sqrt(max(v, 0.0))


# ==============================================================================
# SECTION 13 — FILTERS (Step 4: liquidity, spread, iNAV)
# ==============================================================================

class FilterEngine:
    def __init__(self, db: Db):
        self.db = db

    def liquidity_p25(self, symbol: str, as_of: str) -> float:
        rows = self.db.query(
            "SELECT traded_value FROM price_daily WHERE symbol=? AND date<=? "
            "ORDER BY date DESC LIMIT ?", (symbol, as_of, LIQ_WINDOW))
        vals = [r["traded_value"] for r in rows]
        p = percentile(vals, LIQ_PCTL)
        return p if p is not None else 0.0

    def spread_p90(self, symbol: str, as_of: str) -> float:
        """90th-percentile observed spread in bps; proxy default if no quotes.
        NOTE (audit #9): ts is a datetime string — compare on the DATE part so
        same-day quotes are not excluded."""
        rows = self.db.query(
            "SELECT spread_bps FROM quotes WHERE symbol=? AND substr(ts,1,10)<=? "
            "ORDER BY ts DESC LIMIT ?", (symbol, as_of, SPREAD_WINDOW))
        vals = [r["spread_bps"] for r in rows if r["spread_bps"] is not None]
        if vals:
            # audit 3.8: trust the observed distribution only with >= 60
            # quote snapshots; with fewer, be conservative (worst seen, but
            # never better than the liquidity-tier proxy)
            liq = self.liquidity_p25(symbol, as_of)
            r0 = self.db.one("SELECT broad FROM etf_universe WHERE symbol=?", (symbol,))
            proxy = estimate_spread_bps(liq, bool(r0["broad"]) if r0 else False)
            if len(vals) >= 60:
                p = percentile(vals, SPREAD_PCTL)
                return p if p is not None else max(vals)
            return max(max(vals), proxy)
        r = self.db.one("SELECT broad FROM etf_universe WHERE symbol=?", (symbol,))
        broad = bool(r["broad"]) if r else False
        return DEFAULT_SPREAD_BPS_BROAD if broad else DEFAULT_SPREAD_BPS_OTHER

    def inav_deviation(self, symbol: str, as_of: str, price: float = None):
        """SIGNED market/NAV - 1 (positive = premium) + NAV staleness, as
        (deviation, nav_gap_days) or (None, None) when unavailable.

        external audit F6 (2026-10-28 v3):
        - SIGNED because blocking a DISCOUNT blocks a good buy (the gate exists
          to stop OVERPAYING, so only premiums are gated).
        - The NAV date can lag the reference day (end-of-day publication), so
          the comparison band needs a lag cushion — callers add
          INAV_LAG_CUSHION x gap_days, and gap_days is returned for that.
        - With price=None the reference is the close ON as_of, falling back to
          the latest close on/before as_of (live runs happen before today's
          bar is downloaded — exact-date matching disabled the filter live)."""
        r = self.db.one("SELECT nav, date FROM nav_data WHERE symbol=? AND date<=? "
                        "ORDER BY date DESC LIMIT 1", (symbol, as_of))
        if price is None:
            # raw close — what a buyer actually pays and the only series on the
            # same scale as the published NAV (audit v5 #1).  NB: the audit's
            # hypothesis that adj_close is dividend-inflated (~1.6x) was
            # MEASURED FALSE on this dataset (adj_close == close for all 111
            # symbols; no adjustments on file) — the residual absurd gaps are
            # stay untouched: adj_close and NAV must never be compared
            # should an adjustment ever load.  Residual absurd gaps are
            # NAV-side mapping faults, handled by the INAV_CORRUPT_DEV tier.
            p = self.db.one("SELECT close FROM price_daily "
                            "WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 1",
                            (symbol, as_of))
        else:
            p = {"close": price}
        if not r or not p or not r["nav"] or not p["close"]:
            return None, None
        try:
            gap = max(0, (parse_date(as_of) - parse_date(r["date"])).days)
            if gap > NAV_MAX_STALE_DAYS:
                return None, None
        except Exception:
            return None, None
        return (float(p["close"]) / float(r["nav"]) - 1.0), gap

    def inav_premium_block(self, symbol: str, as_of: str, price: float = None):
        """(blocked, dev, effective_limit) — pre-buy iNAV check with the NAV
        publication-lag cushion.  Extracted so both buy paths (rebalance gate
        and the SIP tranche buys) and the selftests use one rule (audit F1 v3)."""
        dev, gap = self.inav_deviation(symbol, as_of, price=price)
        if dev is None:
            return False, None, None
        lim = self.inav_limit(symbol) + INAV_LAG_CUSHION * min(gap or 0, NAV_MAX_STALE_DAYS)
        return (dev is not None and dev > lim), dev, lim

    def inav_limit(self, symbol: str) -> float:
        # audit v7 (28-Oct-2026): the six 'GLOBAL INDICES' ETFs cannot keep the
        # domestic 0.40/0.75% bands — the SEBI/RBI overseas-investment freeze
        # has blocked NEW unit creation since 2022/2024, so they trade at REAL
        # premiums (measured norm 2-22%, blow-off 31-111% Sep-Oct-2026).  They
        # get their own cap; domestic funds still arbitrate at <1%.
        r = self.db.one("SELECT broad, category FROM etf_universe WHERE symbol=?",
                        (symbol,))
        if r and (r["category"] or "").upper() == "GLOBAL INDICES":
            return GLOBAL_ETF_PREMIUM_CAP
        return INAV_DEV_BROAD if (r and r["broad"]) else INAV_DEV_SECTOR

    def tradable_enough(self, liq_p25: float, equity: float) -> bool:
        """
        Pre-ranking liquidity filter (audit 2.6, supersedes bug #4 rule):
        a full sleeve (15% x p25) must represent at least MIN_LIQ_WEIGHT of
        equity — a RELATIVE gate that scales with the portfolio.
        """
        return ((POS_CAP_LIQ_FRAC * liq_p25) / max(equity, 1.0)) >= MIN_LIQ_WEIGHT

    def correlation(self, rows_a, rows_b, window: int = CORR_WINDOW):
        """
        Date-aligned Pearson correlation of daily returns over the window
        (audit bug #9: never correlate tail-sliced series — align on dates).
        """
        ret_a = date_returns(rows_a[-(window + 1):])
        ret_b = date_returns(rows_b[-(window + 1):])
        common = sorted(set(ret_a) & set(ret_b))
        if len(common) < 20:
            return 0.0
        xa = [ret_a[d] for d in common]
        xb = [ret_b[d] for d in common]
        ma, mb = mean(xa), mean(xb)
        num = sum((a - ma) * (b - mb) for a, b in zip(xa, xb))
        da = math.sqrt(sum((a - ma) ** 2 for a in xa))
        db = math.sqrt(sum((b - mb) ** 2 for b in xb))
        return safe_div(num, da * db, 0.0)

    def check_execution(self, symbol: str, as_of: str, intended_value: float):
        """
        Full pre-trade gate. Returns (ok, reason, info-dict).
        A failure should be DEFERRED one session and rechecked, then skipped.
        """
        info = {}
        p25 = self.liquidity_p25(symbol, as_of)
        info["liq_p25"] = p25
        liq_limit = POS_CAP_LIQ_FRAC * p25
        info["liq_limit"] = liq_limit
        if intended_value > liq_limit:
            return False, (f"order value {fmt_cr(intended_value)} exceeds 15% of daily traded "
                           f"value p25 ({fmt_cr(liq_limit)})"), info
        spread = self.spread_p90(symbol, as_of)
        info["spread_p90"] = spread
        if spread > SPREAD_MAX_BPS:
            return False, (f"bid-ask spread is {spread:.1f} bps — wider than the "
                           f"{SPREAD_MAX_BPS:.0f} bps limit; skipping rather than "
                           f"paying up"), info
        dev, gap = self.inav_deviation(symbol, as_of)
        info["inav_dev"] = dev
        if dev is not None:
            lim = self.inav_limit(symbol) + INAV_LAG_CUSHION * min(gap or 0, NAV_MAX_STALE_DAYS)
            info["inav_limit"] = lim
            # signed: only a PREMIUM above the band is gated (buying a discount
            # is favorable); the cushion forgives NAV publication lag
            if dev > lim:
                return False, (f"price is {fmt_pct(dev)} ABOVE iNAV "
                               f"(limit {fmt_pct(lim)} including a {gap}-day "
                               f"lag cushion) — would be overpaying"), info
        return True, "all checks passed", info


# ==============================================================================
# SECTION 14 — PORTFOLIO CONSTRUCTION (Steps 5 & 6)
# ==============================================================================

class PortfolioBuilder:
    def __init__(self, db: Db, filters: FilterEngine):
        self.db = db
        self.filters = filters

    def stats_at(self, symbol: str, as_of: str, lookback: int, skip: int, entry: float, exit_thr: float):
        rows = self.db.query(
            "SELECT date, adj_close, close, traded_value FROM price_daily "
            "WHERE symbol=? AND date<=? ORDER BY date", (symbol, as_of))
        if len(rows) < max(MIN_HISTORY_BARS, lookback + 5):
            return None
        adj = [r["adj_close"] for r in rows]
        closes = [r["close"] for r in rows]
        i = len(rows) - 1
        mom = signal_momentum(adj, i, lookback=lookback, skip=skip)
        vol = annualized_vol(adj, VOL_WINDOW)
        liq = percentile([r["traded_value"] for r in rows[-LIQ_WINDOW:]], LIQ_PCTL) or 0.0
        return {
            "symbol": symbol, "date": rows[-1]["date"], "momentum": mom,
            "vol": vol, "liq_p25": liq, "close": closes[-1],
            "adj": adj, "rows": rows,
        }

    def rank_metric(self, st) -> float:
        """Ranking key: raw momentum (missing momentum sorts last)."""
        return st["momentum"] if st["momentum"] is not None else -9.0

    def select(self, stats_list: list, holding_symbols: set,
               entry: float = ENTRY_THRESHOLD, exit_thr: float = EXIT_THRESHOLD,
               equity: float = 0.0):
        """
        Step 5 selection — the live ranked-momentum basket: top MAX_HOLDINGS
        by momentum among ETFs that pass the trend gate.
        Filters applied before ranking (audit bugs #3/#4):
          - cash-proxy ETFs (liquid/overnight/gilt/bond) are never ranked
          - a full position (15% x p25) must be >= MIN_LIQ_WEIGHT of equity
          - pairwise 120-day correlation cap against already-chosen ETFs
          - one ETF per index family (rulebook rule)
        Incumbency: governed by the rotation rule — incumbents fall out only
        when their momentum rank decays beyond the rotation window (the old
        bounded bonus knob was retired; it was dead under rotation, F8 v3).
        """
        gate_eq = max(float(equity or 0.0), 1.0)  # empty SIP book: no capital seed
        self.last_pool = None   # stashed for dropped_reasons() console display
        decisions = []
        for st in stats_list:
            # audit 2026-10-19: never trade a series that fails the integrity
            # gate — held names EXIT (risk first), others are blocked outright
            issue = series_integrity_issue(st.get("rows") or [])
            if issue:
                if st["symbol"] in holding_symbols:
                    dec, reason = ("EXIT",
                                   f"data integrity: {issue} — exit and block re-entry")
                else:
                    dec, reason = ("BLOCKED", f"data integrity: {issue}")
                decisions.append((st, dec, reason))
                continue
            dec, reason = decide(st["momentum"], st["symbol"] in holding_symbols,
                                 entry=entry, exit_thr=exit_thr)
            if st["symbol"] in CASH_PROXY_SYMBOLS and dec in ("ELIGIBLE_BUY", "HOLD"):
                dec, reason = ("CASH_SLEEVE",
                               "cash-proxy fund (liquid/gilt/bond) — kept out of "
                               "the ranked pool; used as the cash sleeve instead")
            else:
                if st.get("rows"):
                    # audit 2.5: overlay on ENTRIES and EXITS in the live path too
                    # (hotfix: rows may be 4-col SQLite rows, not (date, adj) pairs)
                    seg = [row_adj(r) for r in st["rows"]][-200:]
                    below = bool(seg) and seg[-1] < sum(seg) / len(seg)
                    if below and st["symbol"] in holding_symbols and dec in ("HOLD",):
                        dec, reason = "EXIT", ("exit overlay: price below the "
                                               "200-day average")
                    elif below and dec == "ELIGIBLE_BUY":
                        dec, reason = ("SKIPPED",
                                       "entry overlay: price below the 200-day average")
                # liquidity gate ALWAYS applies to entries (hotfix: the overlay
                # branch used to be an elif that swallowed this check)
                if dec == "ELIGIBLE_BUY" and not self.filters.tradable_enough(
                        st.get("liq_p25", 0.0), gate_eq):
                    dec, reason = ("SKIPPED",
                                   f"too illiquid for portfolio scale: 15% of daily traded "
                                   f"value ({fmt_cr(POS_CAP_LIQ_FRAC * st.get('liq_p25', 0.0))}) "
                                   f"is under {fmt_pct(MIN_LIQ_WEIGHT)} of equity")
                # audit v7 (28-Oct-2026): premium guard — flagged in
                # collect_stats by the shared inav_tier rule.  Banned from NEW
                # buys with the REAL reason (a market premium that can snap
                # shut), never mislabelled corrupt.  HOLDs are NOT force-sold:
                # the trend exit still rules, with a loud note attached.
                if dec == "ELIGIBLE_BUY" and st.get("premium_over"):
                    _pd, _pl = st["premium_over"]
                    dec, reason = ("SKIPPED",
                                   f"premium guard: price is {fmt_pct(_pd)} above the "
                                   f"fund's real worth (cap {fmt_pct(_pl)}) — SEBI "
                                   f"overseas-freeze premium, can crash overnight; "
                                   f"unlocks under the cap")
                elif dec == "HOLD" and st.get("premium_over"):
                    _pd, _pl = st["premium_over"]
                    reason += (f"  (rides a {fmt_pct(_pd)} premium over real worth — "
                               f"can compress overnight; trend exit still rules)")
            decisions.append((st, dec, reason))
        exits = [st for st, dec, _ in decisions if dec == "EXIT"]
        holds = [st for st, dec, _ in decisions if dec == "HOLD"]
        eligible = [st for st, dec, _ in decisions if dec == "ELIGIBLE_BUY"]

        pool = [st for st in (eligible + holds) if st["momentum"] is not None
                and st["symbol"] not in CASH_PROXY_SYMBOLS
                and self.filters.tradable_enough(st.get("liq_p25", 0.0), gate_eq)]
        pool.sort(key=self.rank_metric, reverse=True)
        self.last_pool = list(pool)
        chosen = self._pick_with_incumbency(pool, holding_symbols, MAX_HOLDINGS)
        chosen = self._apply_rotation(pool, holding_symbols, MAX_HOLDINGS)
        return {"chosen": chosen, "decisions": decisions, "exits": exits,
                "holds": holds, "eligible": eligible}

    def _pick_with_incumbency(self, ranked: list, holding_symbols: set, k: int) -> list:
        """Rank order + family/correlation caps.

        The rotation rule (_apply_rotation) IS the incumbency mechanism: held
        ETFs ranked inside the top-N keep their seat, stale ones are replaced.
        This strict rank-order pick is the fallback the rotation pass overrides;
        the name is kept so call sites stay stable."""
        rest = list(ranked)
        out = self._corr_cap(rest, k)
        for st in rest:
            if len(out) >= k:
                break
            fam = universe_map().get(st["symbol"], {}).get("index_family", st["symbol"])
            if ONE_PER_INDEX_FAMILY and any(
                    universe_map().get(s["symbol"], {}).get("index_family") == fam for s in out):
                continue
            cand = out + [st]
            if self._corr_ok(cand):
                out.append(st)
        return out

    def _corr_cap(self, pool: list, k: int) -> list:
        out = []
        for st in pool:            # caller supplies rank order (audit 2.4)
            if len(out) >= k:
                break
            fam = universe_map().get(st["symbol"], {}).get("index_family", st["symbol"])
            if ONE_PER_INDEX_FAMILY and any(
                    universe_map().get(s["symbol"], {}).get("index_family") == fam for s in out):
                continue
            cand = out + [st]
            if self._corr_ok(cand):
                out.append(st)
        return out

    def _corr_ok(self, chosen: list) -> bool:
        """Pairwise 120-day correlation cap (audit sec 2.2). PAIRWISE_CORR_CAP=0 disables."""
        if PAIRWISE_CORR_CAP <= 0 or len(chosen) < 2:
            return True
        new = chosen[-1]
        for other in chosen[:-1]:
            c = self.filters.correlation(new.get("rows", []), other.get("rows", []),
                                         window=CORR_WINDOW)
            if abs(c) > PAIRWISE_CORR_CAP:
                return False
        return True

    def _corr_offender(self, chosen: list):
        """Worst correlation-cap clash for the LAST element — (symbol, corr) or
        None.  Used only to EXPLAIN drops on the console."""
        if PAIRWISE_CORR_CAP <= 0 or len(chosen) < 2:
            return None
        new = chosen[-1]
        worst = None
        for other in chosen[:-1]:
            c = self.filters.correlation(new.get("rows", []), other.get("rows", []),
                                         window=CORR_WINDOW)
            if abs(c) > PAIRWISE_CORR_CAP and (worst is None or abs(c) > abs(worst[1])):
                worst = (other["symbol"], c)
        return worst

    def dropped_reasons(self, chosen: list, limit: int = 12) -> list:
        """Why top-ranked candidates lost their seat (external audit 2026-10-27:
        'MON100 dropped, most likely by the correlation cap — an inference,
        because the script does not print the reason').  [(rank, symbol, mom,
        reason)].  Approximate: evaluated against the FINAL basket."""
        pool = self.last_pool or []
        chosen_syms = {s["symbol"] for s in chosen}
        out = []
        for pos, st in enumerate(pool[:limit], start=1):
            if st["symbol"] in chosen_syms:
                continue
            parts = []
            if ONE_PER_INDEX_FAMILY:
                fam = universe_map().get(st["symbol"], {}).get("index_family", st["symbol"])
                seat = next((s["symbol"] for s in chosen
                             if universe_map().get(s["symbol"], {}).get("index_family") == fam),
                            None)
                if seat:
                    parts.append(f"one-per-family — {seat} carries the '{fam}' family")
            off = self._corr_offender(chosen + [st])
            if off:
                parts.append(f"correlation {off[1]:+.2f} with {off[0]} breaches "
                             f"the {PAIRWISE_CORR_CAP:.2f} cap")
            if not parts:
                parts.append(f"basket full at {MAX_HOLDINGS} seats (rotation keeps "
                             f"incumbents ranked <= 5)")
            out.append((pos, st["symbol"], st.get("momentum"), "; ".join(parts)))
        return out

    def _apply_rotation(self, pool, holding_symbols, k):
        """
        Audit bug #9: rotation can now BOTH drop stale incumbents AND admit
        replacements.  Held ETFs ranked beyond 5 are replaced while a stronger
        eligible ETF exists; held ETFs ranked <= 5 keep their seat.
        Correlation/family caps otherwise arbitrate WITH INCUMBENT PRIORITY
        (external audit F3.2).
        """
        ranked = sorted(pool, key=self.rank_metric, reverse=True)
        rank_pos = {st["symbol"]: i + 1 for i, st in enumerate(ranked)}
        kept = [st for st in ranked
                if st["symbol"] in holding_symbols and rank_pos.get(st["symbol"], 99) <= 5]
        out = list(kept)
        for st in ranked:
            if len(out) >= k:
                break
            if st in out:
                continue
            fam = universe_map().get(st["symbol"], {}).get("index_family", st["symbol"])
            if ONE_PER_INDEX_FAMILY and any(
                    universe_map().get(s["symbol"], {}).get("index_family") == fam for s in out):
                continue
            if self._corr_ok(out + [st]):
                out.append(st)
        return out

    def size(self, chosen: list, equity: float, risk_scale: float = 1.0):
        """
        Step 6 sizing:
          1. raw weights — inverse-volatility across the chosen basket
          2. dynamic cap  w_i <= min(25%, 15% * p25_value / equity)
          3. NO upward re-normalisation — remainder stays in cash
          4. scale invested fraction so portfolio vol <= VOL_TARGET
          5. accept lower risk if unreachable; never leverage
        Returns dict with weights, vol, cond numbers, invested fraction.
        """
        if not chosen:
            return {"weights": {}, "invested": 0.0, "port_vol": 0.0, "delta": None,
                    "cond": 1.0, "vol_reachable": False, "capped": {}}
        syms = [s["symbol"] for s in chosen]
        n = len(syms)
        # 1) raw weights — inverse-volatility (Step 6)
        inv = [1.0 / (s["vol"] if s["vol"] and s["vol"] > 0 else 1e-6) for s in chosen]
        ssum = sum(inv) or 1.0
        raw = [x / ssum for x in inv]
        # 2) dynamic caps
        caps = {}
        w = []
        for i, s in enumerate(chosen):
            cap = min(POS_CAP_ABS, POS_CAP_LIQ_FRAC * s["liq_p25"] / equity if equity > 0 else 0.0)
            caps[s["symbol"]] = cap
            w.append(min(raw[i], cap))
        # 3) no upward renormalisation
        # 4) covariance of selected, 60-day DATE-ALIGNED demeaned returns
        #    (audit bug #9: tail-sliced series misalign on missing bars)
        series_by_sym = {s["symbol"]: s.get("rows", []) for s in chosen}
        X, _syms, _dates = aligned_return_matrix(series_by_sym, COV_WINDOW)
        T = len(X)
        if T < 20:
            Sigma = [[0.0] * n for _ in range(n)]
            for i in range(n):
                Sigma[i][i] = (chosen[i]["vol"] or 0.0) ** 2
            delta, cond = 0.0, 1.0
        else:
            Xd = demean(X)
            Sigma, delta, cond = ledoit_wolf_cc(Xd)
        raw_vol = portfolio_vol(w, Sigma) * math.sqrt(252.0)   # DAILY cov -> ANNUAL vol
        target_vol = VOL_TARGET * risk_scale
        if raw_vol <= 0:
            s_scale = 1.0
        elif raw_vol <= target_vol:
            s_scale = 1.0
        else:
            s_scale = target_vol / raw_vol
        s_scale = clamp(s_scale, 0.0, 1.0)
        weights = {syms[i]: w[i] * s_scale for i in range(n)}
        port_vol = portfolio_vol([weights[s] for s in syms], Sigma) * math.sqrt(252.0)
        reachable = raw_vol >= target_vol - 1e-9
        return {
            "weights": weights,
            "invested": sum(weights.values()),
            "port_vol": port_vol,
            "unlevered_vol": raw_vol,
            "delta": delta,
            "cond": cond,
            "vol_reachable": reachable,
            "capped": caps,
            "symbols": syms,
            "Sigma": Sigma,
            "risk_scale": risk_scale,
        }


# ==============================================================================
# SECTION 15 — STATUTORY COSTS & SLIPPAGE (Step 7)
# ==============================================================================

def calc_charges(side: str, value: float, category: str = "EQUITY") -> dict:
    """Full Indian statutory charge stack for ETF delivery (audit-corrected)."""
    c = CHARGES
    brokerage = min(value * c["brokerage_pct"], c["brokerage_cap"])
    stt = value * (c["stt_sell_pct"] if side.upper() == "SELL" else c["stt_buy_pct"])
    if category and category.upper() in {x.upper() for x in STT_EXEMPT_CATEGORIES}:
        stt = 0.0          # Gold/Liquid/Gilt ETFs: no STT (audit #8)
    stamp = value * c["stamp_buy_pct"] if side.upper() == "BUY" else 0.0
    exchange = value * c["exchange_pct"]
    sebi = value * c["sebi_pct"]
    ipft = value * c["ipft_pct"]
    gst = c["gst_pct"] * (brokerage + exchange + sebi)
    # audit v13 #4 (2026-10-30): the depository takes a flat fee every time
    # shares leave the demat account — sells were previously FREE of it.
    dp = c.get("dp_sell_flat", 0.0) if side.upper() == "SELL" else 0.0
    total = brokerage + stt + stamp + exchange + sebi + ipft + gst + dp
    return {"brokerage": brokerage, "stt": stt, "stamp": stamp,
            "exchange": exchange, "sebi": sebi, "ipft": ipft, "gst": gst,
            "dp": dp, "total": total}


def estimate_spread_bps(liq_p25: float, broad: bool = False) -> float:
    """
    Liquidity-tiered spread proxy (bps) used until real quote history exists.
    Thin ETFs trade meaningfully worse than liquid broad-market ones.

    External audit F2.3 (2026-10-27): mid-tier Indian ETF spreads run 10-25 bps
    in reality; these recalibrated tiers replace the old too-tight 5/10/20 proxy.
    """
    if liq_p25 >= 5e7:      # >= Rs 5 Cr/day p25
        base = 3.0
    elif liq_p25 >= 5e6:    # >= Rs 50 L/day
        base = 9.0
    elif liq_p25 >= 5e5:    # >= Rs 5 L/day
        base = 15.0
    else:
        base = 28.0
    return base * (1.0 if broad else 1.4)


def impact_bps(order_value: float, liq_p25: float, daily_vol: float = 0.01) -> float:
    """
    Square-root market-impact model scaled by participation AND volatility
    (audit 3.6): impact ~ sqrt(participation) * (daily_vol / 1%).
    A calm ETF (0.5% daily vol) costs half the impact of the 1% reference.
    """
    participation = safe_div(order_value, max(liq_p25, 1.0), 1.0)
    vol_factor = max(daily_vol, 1e-4) / 0.01
    return ((IMPACT_BASE_BPS + IMPACT_SQRT_COEF * math.sqrt(min(participation, 1.0)))
            * vol_factor)


def slippage_price(side: str, ref_price: float, spread_bps: float,
                   spread_mult: float = 1.0,
                   order_value: float = 0.0, liq_p25: float = 0.0,
                   daily_vol: float = 0.01) -> float:
    """
    Executable price (audit 3.5):
      MARKET     — cross the spread: half spread x mult + sqrt-impact
      LIMIT_MID  — rest at mid: NO spread crossing, impact only
    """
    impact = (impact_bps(order_value, liq_p25, daily_vol) / 10000.0
              if liq_p25 else IMPACT_BASE_BPS / 10000.0)
    half = (spread_bps * spread_mult) / 2.0 / 10000.0
    slip = half + impact
    if side.upper() == "BUY":
        return ref_price * (1.0 + slip)
    return ref_price * (1.0 - slip)


# ==============================================================================
# SECTION 16 — TAX HELPERS & TAX LOTS (Step 8, audit-corrected)
# ==============================================================================

def gain_class(category: str) -> str:
    """Netting bucket (audit #7; external audit 2026-10-27 F2.5: HYBRID and
    GLOBAL INDICES get their OWN buckets — the aggregation settle used to tax
    them at EQUITY rates, understating tax on every held hybrid/global lot)."""
    cat = (category or "EQUITY").upper()
    if cat in ("COMMODITY",):
        return "COMMODITY"
    if cat in ("DEBT",):
        return "DEBT"
    if cat == "GLOBAL INDICES":
        return "GLOBAL"
    if cat == "HYBRID":
        return "HYBRID"
    return "EQUITY"


def tax_rules_for(category: str) -> dict:
    cat = (category or "EQUITY").upper()
    if cat == "GLOBAL INDICES":
        cat = "GLOBAL"
    if cat == "HYBRID":
        cat = "HYBRID"
    return TAX_RULES.get(cat, TAX_RULES["EQUITY"])


def held_more_than_months(buy_date, sell_date, months: int) -> bool:
    """
    Anniversary test (audit #7): 'more than N months' means strictly after the
    N-th monthly anniversary of the purchase date.  Buy 2025-01-20 and sell
    2026-01-25 IS more than 12 months; sell 2026-01-15 is not.
    """
    if months <= 0:
        return True
    import calendar
    b = parse_date(buy_date) if isinstance(buy_date, str) else buy_date
    s = parse_date(sell_date) if isinstance(sell_date, str) else sell_date
    y = b.year + (b.month - 1 + months) // 12
    m = (b.month - 1 + months) % 12 + 1
    day = min(b.day, calendar.monthrange(y, m)[1])   # clamp e.g. 31 Jan + 1m
    return s > dt.date(y, m, day)


def ltcg_anniversary(buy_date, months: int = 12) -> "dt.date":
    """The N-month anniversary date used by the holding-period test."""
    import calendar
    b = parse_date(buy_date) if isinstance(buy_date, str) else buy_date
    y = b.year + (b.month - 1 + months) // 12
    m = (b.month - 1 + months) % 12 + 1
    return dt.date(y, m, min(b.day, calendar.monthrange(y, m)[1]))


def tax_on_gain(category: str, gain_type: str, gain: float, exempt_remaining: float):
    """Tax for one realised gain with per-class rules + 4% cess. (tax, exempt_used)."""
    rules = tax_rules_for(category)
    if gain <= 0:
        return 0.0, 0.0
    if gain_type == "STCG":
        return gain * rules["stcg"] * (1.0 + CESS_RATE), 0.0
    use = min(gain, exempt_remaining, rules["exempt"])
    taxable = max(0.0, gain - use)
    return taxable * rules["ltcg"] * (1.0 + CESS_RATE), use


class TaxEngine:
    """FIFO tax lots: STCG 20% (<=12 months), LTCG 12.5% above Rs 1.25 L/FY."""

    def __init__(self, db: Db):
        self.db = db

    def record_buy(self, symbol: str, buy_date: str, price: float, qty: float):
        self.db.execute(
            "INSERT INTO tax_lots(symbol,buy_date,buy_price,qty) VALUES(?,?,?,?)",
            (symbol, buy_date, price, qty))

    def record_sell(self, symbol: str, sell_date: str, sell_price: float, qty: float) -> dict:
        """FIFO lot match (audit 3.7); writes realised lots.
        Returns {realized_pnl, tax, lt_pnl, st_pnl}."""
        remaining = qty
        realized = 0.0
        st_pnl = 0.0
        lt_pnl = 0.0
        cat_row = self.db.one("SELECT category FROM etf_universe WHERE symbol=?", (symbol,))
        category = cat_row["category"] if cat_row else "EQUITY"
        order = "buy_date, id"   # FIFO
        lots = self.db.query(
            "SELECT * FROM tax_lots WHERE symbol=? AND qty>0 AND sell_date IS NULL "
            f"ORDER BY {order}", (symbol,))
        for lot in lots:
            if remaining <= 0:
                break
            take = min(remaining, lot["qty"])
            pnl = (sell_price - lot["buy_price"]) * take
            # audit #7: anniversary test, not calendar-month subtraction
            is_lt = held_more_than_months(lot["buy_date"], sell_date,
                                          tax_rules_for(category)["months"])
            gain_type = "LTCG" if is_lt else "STCG"
            if gain_type == "LTCG":
                lt_pnl += pnl
            else:
                st_pnl += pnl
            self.db.execute(
                "INSERT INTO tax_lots(symbol,buy_date,buy_price,qty,sell_date,sell_price,pnl,gain_type,tax) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (symbol, lot["buy_date"], lot["buy_price"], take, sell_date,
                 sell_price, pnl, gain_type, 0.0))
            self.db.execute("UPDATE tax_lots SET qty=qty-? WHERE id=?", (take, lot["id"]))
            remaining -= take
            realized += pnl
        rules = tax_rules_for(category)
        tax = (max(0.0, st_pnl) * rules["stcg"]
               + max(0.0, lt_pnl) * rules["ltcg"]) * (1.0 + CESS_RATE)
        return {"realized_pnl": realized, "tax": tax, "st_pnl": st_pnl, "lt_pnl": lt_pnl}

    def year_tax_summary(self) -> dict:
        """
        Per-FY, per-class tax with LOSS NETTING (audit #7): losses offset gains
        inside the same class, the Rs 1.25 L exemption applies only to
        equity-like LTCG, and commodity/debt use their own rates.
        """
        rows = self.db.query(
            "SELECT l.sell_date AS sell_date, l.gain_type AS gain_type, l.pnl AS pnl, "
            "l.symbol AS symbol, u.category AS category FROM tax_lots l "
            "LEFT JOIN etf_universe u ON u.symbol=l.symbol "
            "WHERE l.sell_date IS NOT NULL")
        buckets = {}   # (fy, class) -> {"st":0,"lt":0}
        for r in rows:
            fy = financial_year(parse_date(r["sell_date"]))
            cls = gain_class(r["category"] or "EQUITY")
            d = buckets.setdefault((fy, cls), {"st": 0.0, "lt": 0.0})
            if r["gain_type"] == "STCG":
                d["st"] += r["pnl"] or 0.0
            else:
                d["lt"] += r["pnl"] or 0.0
        by_fy = {}
        carry = {}    # cls -> {"st","lt"} loss carry-forward (audit 2.7)
        for (fy, cls), d in sorted(buckets.items()):
            row = by_fy.setdefault(fy, {"stcg": 0.0, "ltcg": 0.0, "tax": 0.0,
                                        "by_class": {}})
            rules = TAX_RULES["EQUITY"] if cls == "EQUITY" else (
                TAX_RULES["COMMODITY"] if cls == "COMMODITY" else
                # audit v14 P2 (2026-10-30): GLOBAL/HYBRID/DEBT reporting used
                # slab for BOTH legs — GLOBAL LTCG must be the configured
                # 12.5%, not 30%.  Use the per-class rules everywhere.
                tax_rules_for({"GLOBAL": "GLOBAL INDICES"}.get(cls, cls)))
            st_rate, lt_rate, exempt = rules["stcg"], rules["ltcg"], rules["exempt"]
            st_net, lt_net = d["st"], d["lt"]
            c = carry.get(cls)
            if c:   # carried losses: ST first vs ST then LT; LT vs LT only
                if c["st"] < 0:
                    use = min(-c["st"], max(0.0, st_net)); st_net -= use; c["st"] += use
                    if c["st"] < 0:
                        use2 = min(-c["st"], max(0.0, lt_net)); lt_net -= use2; c["st"] += use2
                if c["lt"] < 0:
                    use = min(-c["lt"], max(0.0, lt_net)); lt_net -= use; c["lt"] += use
            if st_net < 0 and lt_net > 0:     # STCL vs LTCG (audit 2.7)
                use = min(-st_net, lt_net)
                lt_net -= use
                st_net += use
            tax = (max(0.0, st_net) * st_rate
                   + max(0.0, lt_net - exempt) * lt_rate) * (1.0 + CESS_RATE)
            row["stcg"] += st_net
            row["ltcg"] += lt_net
            row["tax"] += tax
            row["by_class"][cls] = {"stcg": st_net, "ltcg": lt_net, "tax": tax}
            if st_net < 0 or lt_net < 0:
                carry[cls] = {"st": min(st_net, 0.0), "lt": min(lt_net, 0.0)}
        return by_fy


# ==============================================================================
# SECTION 17 — LIVE UPSTOX BROKER (holdings truth + SIP reservations)
# ==============================================================================

# Upstox order statuses meaning "still working at the broker" — never send a
# second order while one of these is open (user direction 2026-10-28)
BROKER_OPEN_STATUSES = {
    "open", "trigger pending", "pending", "validation pending",
    "put order req received", "open pending", "modify pending",
    "modify validation pending", "after market order req received",
    "amo req received",
    # audit v14 P1 (2026-10-30): a partially-filled order still has the
    # remainder WORKING — missing it here let the next run duplicate it.
    "partially filled", "partially filled and open",
}


class Broker:
    """Live Upstox delivery broker. The DB mirrors broker truth and journals
    orders/reservations; it is never an alternative trading account."""

    ACTIVE_STATUSES = ("SUBMITTING", "PENDING", "UNCONFIRMED")

    def __init__(self, db: Db, tax: TaxEngine, client=None):
        self.db = db
        self.tax = tax
        self.client = client
        self._live_book = None
        self._live_open = {}
        self._cash_synced = False

    # ----- account state -----
    def sync_live_cash(self) -> bool:
        """Read available cash. Failure invalidates order permission; cached
        cash may be displayed but must never size a new order."""
        self._cash_synced = False
        try:
            if self.client is None:
                raise RuntimeError("no Upstox client")
            avail = float(self.client.funds_available())
            if not math.isfinite(avail) or avail < 0:
                raise ValueError("invalid available margin")
        except Exception as e:
            P.err(f"Could not read Upstox available cash ({e}) — orders disabled.")
            return False
        ledger = self.cash()
        if abs(avail - ledger) > 1.0:
            P.note(f"Cash synced from Upstox: {fmt_cr(ledger)} -> {fmt_cr(avail)}.")
        self.set_cash(avail)
        self._cash_synced = True
        return True

    def cash(self) -> float:
        v = self.db.meta_get("cash_balance")
        try:
            return max(0.0, float(v)) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def set_cash(self, v: float):
        self.db.meta_set("cash_balance", float(v))
        self.db.meta_set("cash", float(v))

    def _book_symbol(self, raw: str, instrument_key: str = "") -> str:
        sym = (raw or "").strip().upper()
        if sym and self.db.one("SELECT symbol FROM etf_universe WHERE symbol=?", (sym,)):
            return sym
        if instrument_key:
            row = self.db.one("SELECT symbol FROM etf_universe WHERE instrument_key=?",
                              (instrument_key,))
            if row:
                return row["symbol"]
            isin = isin_of(instrument_key)
            if isin:
                row = self.db.one(
                    "SELECT symbol FROM etf_universe WHERE instrument_key LIKE ?",
                    (f"%|{isin}",))
                if row:
                    return row["symbol"]
        return sym

    def _row_symbol(self, r: dict) -> str:
        return self._book_symbol(
            r.get("trading_symbol") or r.get("tradingsymbol") or r.get("symbol") or "",
            r.get("instrument_token") or r.get("instrument_key") or "")

    @staticmethod
    def _number(value, default=0.0) -> float:
        try:
            n = float(value)
            return n if math.isfinite(n) else default
        except (TypeError, ValueError):
            return default

    def sync_live_book(self) -> bool:
        """Fetch holdings, unsettled delivery positions, orders and cash before
        changing the mirror. Any unreadable source invalidates trading permission.
        Portfolio marks come from Upstox last_price (close_price fallback)."""
        self._live_book = None
        self._live_open = {}
        self._cash_synced = False
        try:
            if self.client is None:
                raise RuntimeError("no Upstox client")
            hold_rows = self.client.holdings()
            pos_rows = self.client.positions()
            order_rows = self.client.orders_book()
            if not all(isinstance(x, list) for x in (hold_rows, pos_rows, order_rows)):
                raise ValueError("malformed broker portfolio response")
        except Exception as e:
            P.err(f"Cannot read the Upstox book ({e}) — REFUSING to trade blind. "
                  "Check the token/network and rerun.")
            return False

        known = {r["symbol"] for r in self.db.query("SELECT symbol FROM etf_universe")}
        truth, foreign = {}, []
        try:
            for r in hold_rows:
                if not isinstance(r, dict):
                    raise ValueError("malformed holding row")
                sym = self._row_symbol(r)
                if not sym:
                    continue
                if sym not in known:
                    foreign.append(sym)
                    continue
                if str(r.get("product") or "D").upper() != "D":
                    continue
                # Upstox documents quantity as the TOTAL holding qty. t1_quantity
                # is a disclosure subset, not an additive second bucket; adding it
                # would double-count unsettled holdings.
                qty = self._number(r.get("quantity"))
                avg = self._number(r.get("average_price", r.get("avg_price")))
                mark = self._number(r.get("last_price")) or self._number(r.get("close_price"))
                if qty < 0 or avg < 0:
                    raise ValueError(f"invalid holding quantity/cost for {sym}")
                if qty > 0 and mark <= 0:
                    raise ValueError(f"no Upstox market price for holding {sym}")
                if qty > 0:
                    truth[sym] = {"qty": qty, "avg_cost": avg, "last_price": mark}

            # Aggregate today's net delivery fills before merging, so partial
            # sells and multiple rows cannot overwrite one another's cost basis.
            positions = {}
            for r in pos_rows:
                if not isinstance(r, dict):
                    raise ValueError("malformed position row")
                sym = self._row_symbol(r)
                if not sym or sym not in known:
                    continue
                if str(r.get("product") or "D").upper() != "D":
                    continue
                bought = self._number(r.get("buy_quantity", r.get("day_buy_quantity")))
                sold = self._number(r.get("sell_quantity", r.get("day_sell_quantity")))
                net = bought - sold if ("buy_quantity" in r or "day_buy_quantity" in r) \
                    else self._number(r.get("quantity"))
                bpx = self._number(r.get("buy_price", r.get("day_buy_price")))
                mark = self._number(r.get("last_price")) or self._number(r.get("close_price")) or bpx
                p = positions.setdefault(sym, {"net": 0.0, "bought": 0.0, "cost": 0.0, "mark": 0.0})
                p["net"] += net
                p["bought"] += max(0.0, bought)
                p["cost"] += max(0.0, bought) * bpx
                p["mark"] = mark or p["mark"]
            for sym, p in positions.items():
                if p["net"] == 0:
                    continue
                t = truth.setdefault(sym, {"qty": 0.0, "avg_cost": 0.0, "last_price": p["mark"]})
                new_q = t["qty"] + p["net"]
                if p["bought"] > 0 and p["cost"] > 0:
                    t["avg_cost"] = ((t["qty"] * t["avg_cost"] + p["cost"]) / new_q) \
                        if new_q > 0 else p["cost"] / p["bought"]
                t["qty"] = new_q
                t["last_price"] = p["mark"] or t["last_price"]
                if t["qty"] <= 0:
                    truth.pop(sym, None)
                elif t["last_price"] <= 0:
                    raise ValueError(f"no Upstox market price for position {sym}")

            openq = {}
            for r in order_rows:
                if not isinstance(r, dict):
                    raise ValueError("malformed order row")
                sym = self._row_symbol(r)
                if not sym or sym not in known:
                    continue
                st = str(r.get("status") or "").strip().lower()
                if st not in BROKER_OPEN_STATUSES:
                    continue
                pend = self._number(r.get("quantity")) - self._number(r.get("filled_quantity"))
                side = str(r.get("transaction_type") or "").strip().upper()
                if pend > 0 and side in ("BUY", "SELL"):
                    openq.setdefault(sym, {"BUY": 0.0, "SELL": 0.0})[side] += pend
        except Exception as e:
            P.err(f"Invalid Upstox portfolio data ({e}) — no orders placed.")
            return False

        if not self.sync_live_cash():
            return False
        existing = {r["symbol"]: r["qty"] for r in
                    self.db.query("SELECT symbol, qty FROM positions WHERE qty>0")}
        vanished = sorted(s for s in existing if s not in truth)
        with self.db.transaction():
            for sym in vanished:
                self.db.conn.execute("DELETE FROM positions WHERE symbol=?", (sym,))
            for sym, t in truth.items():
                self.db.conn.execute(
                    "INSERT INTO positions(symbol,qty,avg_cost,last_price,updated) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET "
                    "qty=excluded.qty, avg_cost=excluded.avg_cost, "
                    "last_price=excluded.last_price, updated=excluded.updated",
                    (sym, t["qty"], t["avg_cost"], t["last_price"], now_iso()))
        self._live_book = {s: dict(t) for s, t in truth.items()}
        self._live_open = openq
        self.db.meta_set("live_open_orders", json.dumps(openq))
        if vanished:
            P.warn("Journal mirror cleaned to match Upstox: " + ", ".join(vanished[:8]))
        newat = [s for s in truth if s not in existing]
        if newat:
            P.note("Adopted Upstox holdings: "
                   + ", ".join(f"{s} x{truth[s]['qty']:.0f}" for s in newat[:8]))
        for s in newat:
            lots = self.db.one("SELECT COUNT(*) AS c FROM tax_lots WHERE symbol=? "
                               "AND qty>0 AND sell_date IS NULL", (s,))
            if not lots or not lots["c"]:
                t = truth[s]
                self.db.execute("INSERT INTO tax_lots(symbol,buy_date,buy_price,qty) "
                                "VALUES(?,?,?,?)", (s, today_str(), t["avg_cost"], t["qty"]))
                unk = set(json.loads(self.db.meta_get("tax_age_unknown", "[]")))
                unk.add(s)
                self.db.meta_set("tax_age_unknown", json.dumps(sorted(unk)))
                P.warn(f"{s}: buy age unknown; created a conservative tax lot dated today.")
        if foreign:
            P.note("Non-universe broker holdings are untouched and excluded from this ETF portfolio: "
                   + ", ".join(sorted(set(foreign))[:8]))
        if openq:
            P.note("Open broker orders: " + ", ".join(
                f"{s} BUY {d['BUY']:.0f}/SELL {d['SELL']:.0f}"
                for s, d in sorted(openq.items())[:8]))
        P.ok(f"Upstox book synced: {len(truth)} ETF holding(s), market value "
             f"{fmt_cr(self.holdings_value())}; cash {fmt_cr(self.cash())} kept separate.")
        return True

    def live_book_ok(self) -> bool:
        return self.client is not None and self._live_book is not None and self._cash_synced

    def _active_rows(self, symbol: str = None, side: str = None):
        sql = "SELECT * FROM orders WHERE mode='LIVE' AND status IN ('SUBMITTING','PENDING','UNCONFIRMED')"
        args = []
        if symbol:
            sql += " AND symbol=?"; args.append(symbol)
        if side:
            sql += " AND side=?"; args.append(side.upper())
        return self.db.query(sql, args)

    def live_open_qty(self, sym: str, side: str) -> float:
        side = side.upper()
        broker_qty = float((self._live_open.get(sym) or {}).get(side, 0.0))
        local_qty = sum(max(0.0, float(r["qty"]) - float(r["filled_qty"] or 0))
                        for r in self._active_rows(sym, side))
        return max(broker_qty, local_qty)

    def sellable_qty(self, sym: str) -> float:
        h = self.holdings().get(sym)
        return max(0.0, float(h["qty"]) - self.live_open_qty(sym, "SELL")) if h else 0.0

    def holdings(self) -> dict:
        if self._live_book is None:
            self.sync_live_book()
        return {s: dict(t) for s, t in (self._live_book or {}).items()}

    def holdings_value(self, prices: dict = None) -> float:
        """Current ETF portfolio value. The optional historical-price argument
        is accepted for call compatibility but never overrides broker marks."""
        return sum(h["qty"] * h.get("last_price", 0.0) for h in self.holdings().values())

    def equity(self, prices: dict = None) -> float:
        """SIP portfolio equity = holdings market value, NOT cash + holdings."""
        return self.holdings_value()

    def account_equity(self) -> float:
        """Account-level risk measure only; selling to cash is not a crash."""
        return self.cash() + self.holdings_value()

    # ----- strict SIP budget / live order flow -----
    def sip_committed(self, day: str = None) -> float:
        day = day or today_str()
        row = self.db.one("SELECT COALESCE(SUM(reserved_value),0) AS v FROM orders "
                          "WHERE exec_date=? AND side='BUY' AND mode='LIVE'", (day,))
        return float(row["v"] or 0.0)

    def sip_remaining(self, day: str = None) -> float:
        return max(0.0, float(INVEST_PER_DAY) - self.sip_committed(day))

    def _insert_submission(self, symbol, side, qty, price, signal_as_of, reason,
                           order_type, limit_price, reserved):
        """Atomically reserve budget and journal BEFORE the network call. A
        crash after submission leaves a blocking reservation, never free budget."""
        day = today_str()
        with self.db.transaction():
            if side == "BUY":
                used = float(self.db.conn.execute(
                    "SELECT COALESCE(SUM(reserved_value),0) FROM orders "
                    "WHERE exec_date=? AND side='BUY' AND mode='LIVE'", (day,)).fetchone()[0])
                if INVEST_PER_DAY <= 0 or reserved > INVEST_PER_DAY - used + 1e-8:
                    return None
            cur = self.db.conn.execute(
                "INSERT INTO orders(ts,signal_as_of,exec_date,symbol,side,qty,price,value,"
                "slippage,charges,status,mode,order_type,reason,reserved_value,filled_qty,limit_price) "
                "VALUES(?,?,?,?,?,?,?,?,0,0,'SUBMITTING','LIVE',?,?,?,?,?)",
                (now_iso(), signal_as_of, day, symbol, side, qty, price, reserved if side == "BUY" else 0.0,
                 order_type, reason, reserved if side == "BUY" else 0.0, 0, limit_price))
            return cur.lastrowid

    def execute(self, symbol: str, side: str, qty: float, ref_price: float,
                signal_as_of: str = "", reason: str = "", spread_bps: float = 8.0,
                spread_mult: float = 1.0, order_type: str = "MARKET",
                limit_px: float = None) -> dict:
        side = str(side).upper()
        qty = int(qty)
        ref_price = self._number(ref_price)
        if qty <= 0 or ref_price <= 0 or side not in ("BUY", "SELL"):
            return {"status": "REJECTED", "reason": "invalid side, quantity or price"}
        if not self.live_book_ok():
            return {"status": "REJECTED", "reason": "Upstox holdings/cash not verified"}
        if self.live_open_qty(symbol, side) > 0:
            return {"status": "REJECTED", "reason": f"an {side} order is already open/unconfirmed for {symbol}"}
        if side == "SELL" and qty > self.sellable_qty(symbol):
            return {"status": "REJECTED", "reason": "quantity exceeds Upstox sellable holdings"}
        if side == "BUY":
            order_type, limit_px = live_buy_order_px(ref_price)
            if order_type != "LIMIT":
                return {"status": "REJECTED", "reason": "strict SIP cap requires LIMIT buys"}
            reserved = round(qty * float(limit_px), 8)
            if reserved < MIN_TRADE_VALUE:
                return {"status": "REJECTED", "reason": "buy below MIN_TRADE_VALUE"}
            if reserved > self.sip_remaining() + 1e-8:
                return {"status": "REJECTED", "reason": "daily SIP allowance exhausted"}
            need = reserved + calc_charges("BUY", reserved)["total"] * 1.10
            if self.cash() < need:
                return {"status": "REJECTED", "reason": "insufficient Upstox cash for order and charges"}
            send_px = float(limit_px)
        else:
            order_type, send_px, reserved = "MARKET", 0.0, 0.0
        row_id = self._insert_submission(symbol, side, qty, limit_px if side == "BUY" else ref_price,
                                         signal_as_of, reason, order_type,
                                         send_px if side == "BUY" else None, reserved)
        if row_id is None:
            return {"status": "REJECTED", "reason": "daily SIP allowance exhausted"}
        key_row = self.db.one("SELECT instrument_key FROM etf_universe WHERE symbol=?", (symbol,))
        key = (key_row["instrument_key"] if key_row else "") or ""
        if not key:
            self.db.execute("UPDATE orders SET status='FAILED',reserved_value=0 WHERE id=?", (row_id,))
            return {"status": "FAILED", "reason": f"no verified NSE instrument key for {symbol}"}
        oid = ""
        try:
            resp = self.client.place_order(symbol, key, side, qty, send_px, order_type,
                                           is_amo=not market_open_now())
            oid = str((resp.get("data") or {}).get("order_id") or "")
            if not oid:
                raise RuntimeError("broker returned no order_id")
            self.db.execute("UPDATE orders SET broker_order_id=?,status='UNCONFIRMED' WHERE id=?",
                            (oid, row_id))
        except Exception as e:
            msg = str(e)
            explicit_reject = ("Upstox HTTP 4" in msg or "UDAPI" in msg)
            status = "FAILED" if explicit_reject else "UNCONFIRMED"
            self.db.execute("UPDATE orders SET status=?,reserved_value=? WHERE id=?",
                            (status, 0.0 if explicit_reject else reserved, row_id))
            P.err(f"{side} {symbol}: {msg}. " + ("Broker rejected the order." if explicit_reject else
                  "Submission outcome unknown; budget stays reserved. Reconcile before retrying."))
            if side == "SELL" and "EDIS" in msg.upper():
                P.err("Delivery sells need Upstox CDSL EDIS/TPIN authorisation (or DDPI).")
            return {"status": status, "reason": msg, "note": msg, "qty": qty,
                    "price": ref_price, "value": 0.0, "reserved": reserved}
        try:
            det = self.client.order_details(oid)
            result = self._update_order_from_details(self.db.one("SELECT * FROM orders WHERE id=?", (row_id,)),
                                                     (det or {}).get("data") or {})
        except Exception as e:
            result = {"status": "UNCONFIRMED", "price": send_px if side == "BUY" else ref_price,
                      "value": 0.0, "filled_qty": 0, "note": f"order {oid} accepted; fill verification failed: {e}"}
        result.update({"qty": qty, "mode": "LIVE", "reason": reason, "reserved": reserved,
                       "broker_order_id": oid})
        log(f"ORDER LIVE {side} {symbol} x{qty} id={oid} status={result['status']}")
        return result

    def _update_order_from_details(self, row, d: dict) -> dict:
        """Apply only the NEW cumulative filled quantity. Re-polling a partial
        fill and then its completion never double-counts cash, lots or budget."""
        if not isinstance(d, dict) or not d:
            raise ValueError("empty order details")
        bs = str(d.get("status") or "").strip().lower()
        status = ("FILLED" if bs in ("complete", "filled") else
                  "PENDING" if bs in BROKER_OPEN_STATUSES or bs == "queued" else
                  "REJECTED" if bs in ("rejected", "cancelled", "expired") else "UNCONFIRMED")
        ordered = int(row["qty"])
        filled = int(self._number(d.get("filled_quantity"), ordered if status == "FILLED" else 0))
        filled = max(0, min(ordered, filled))
        avg = self._number(d.get("average_price"))
        note = f"broker {bs or 'unknown'}; filled {filled}/{ordered}"
        if filled > 0 and avg <= 0:
            status = "UNCONFIRMED"
            note += "; broker average fill price missing"
        already = int(row["filled_qty"] or 0)
        delta = max(0, filled - already)
        if delta and avg > 0:
            category = universe_map().get(row["symbol"], {}).get("category", "EQUITY")
            charges = calc_charges(row["side"], avg * delta, category)
            self._apply_fill(row["symbol"], row["side"], delta, avg, avg * delta, charges)
        value = avg * filled if avg > 0 else 0.0
        limit_price = self._number(row["limit_price"], self._number(row["price"]))
        active = status in self.ACTIVE_STATUSES
        reserved = (value + max(0, ordered - filled) * limit_price) if active else value
        if row["side"] != "BUY":
            reserved = 0.0
        category = universe_map().get(row["symbol"], {}).get("category", "EQUITY")
        charges_total = calc_charges(row["side"], value, category)["total"] if value else 0.0
        self.db.execute("UPDATE orders SET status=?,filled_qty=?,price=?,value=?,charges=?,reserved_value=? WHERE id=?",
                        (status, filled, avg if avg > 0 else row["price"], value, charges_total, reserved, row["id"]))
        if status == "REJECTED":
            P.warn(f"{row['symbol']} order {row['broker_order_id'] or row['id']} {bs}; "
                   f"unfilled budget released, any confirmed partial fill retained.")
        return {"status": status, "price": avg if avg > 0 else self._number(row["price"]),
                "value": value, "filled_qty": filled, "note": note,
                "charges": calc_charges(row["side"], value, category)}

    def _apply_fill(self, symbol, side, qty, px, value, charges):
        """Provisional same-run mirror; the next Upstox sync remains authoritative."""
        t = (self._live_book or {}).get(symbol, {"qty": 0.0, "avg_cost": 0.0, "last_price": px})
        if side == "BUY":
            nq = t["qty"] + qty
            t["avg_cost"] = ((t["qty"] * t["avg_cost"] + qty * px) / nq) if nq else px
            t["qty"] = nq
            self.tax.record_buy(symbol, today_str(), px, qty)
            self.set_cash(self.cash() - value - charges["total"])
        else:
            sell_qty = min(qty, t["qty"])
            self.tax.record_sell(symbol, today_str(), px, sell_qty)
            t["qty"] = max(0.0, t["qty"] - sell_qty)
            self.set_cash(self.cash() + value - charges["total"])
        t["last_price"] = px
        if self._live_book is not None:
            if t["qty"] > 0:
                self._live_book[symbol] = t
            else:
                self._live_book.pop(symbol, None)
        if t["qty"] > 0:
            self.db.execute("INSERT INTO positions(symbol,qty,avg_cost,last_price,updated) VALUES(?,?,?,?,?) "
                            "ON CONFLICT(symbol) DO UPDATE SET qty=excluded.qty,avg_cost=excluded.avg_cost,"
                            "last_price=excluded.last_price,updated=excluded.updated",
                            (symbol, t["qty"], t["avg_cost"], px, now_iso()))
        else:
            self.db.execute("DELETE FROM positions WHERE symbol=?", (symbol,))

    def print_order_ticket(self, res: dict, symbol: str, name: str, side: str):
        status = res.get("status")
        if status == "FILLED":
            P.decision("🟢" if side == "BUY" else "🔴",
                       f"{'BOUGHT' if side == 'BUY' else 'SOLD'} {res.get('filled_qty', res.get('qty', 0))} "
                       f"units of {symbol} ({name}) at Rs {res.get('price', 0):.2f}",
                       f"value {fmt_rs(res.get('value', 0))} | estimated charges "
                       f"{fmt_rs((res.get('charges') or {}).get('total', 0))} | LIVE",
                       _C.GREEN if side == "BUY" else _C.RED)
        elif status in self.ACTIVE_STATUSES:
            P.info(f"{side} {symbol}: sent to Upstox, awaiting fill. "
                   f"{fmt_rs(res.get('reserved', 0))} reserved against today's SIP allowance. "
                   + (res.get("note") or ""))
        else:
            P.warn(f"{side} {symbol} not executed: {res.get('note') or res.get('reason') or status}")

# ==============================================================================
# SECTION 18 — RISK ENGINE (Step 9: drawdown triggers & re-entry)
# ==============================================================================

class RiskEngine:
    def __init__(self, db: Db, vol_target: float = None):
        self.db = db
        self.vol_target = float(vol_target if vol_target is not None else VOL_TARGET)
        self.state = db.meta_get("risk_state", "NORMAL")
        self.peak = float(db.meta_get("equity_peak", 0.0))
        self.halved = db.meta_get("risk_halved", "0") == "1"
        self.all_cash = db.meta_get("risk_all_cash", "0") == "1"
        self.trigger_date = db.meta_get("risk_trigger_date", "")
        self.trigger_counts = {
            "REVIEW": int(db.meta_get("risk_count_REVIEW", "0")),
            "SEVERE": int(db.meta_get("risk_count_SEVERE", "0")),
            "EXTREME": int(db.meta_get("risk_count_EXTREME", "0")),
        }

    def _save(self):
        self.db.meta_set("risk_state", self.state)
        self.db.meta_set("equity_peak", self.peak)
        self.db.meta_set("risk_halved", "1" if self.halved else "0")
        self.db.meta_set("risk_all_cash", "1" if self.all_cash else "0")
        self.db.meta_set("risk_trigger_date", self.trigger_date)
        for k, v in self.trigger_counts.items():
            self.db.meta_set(f"risk_count_{k}", v)

    def update(self, equity: float, context: str = "") -> dict:
        """Daily monitoring of portfolio drawdown."""
        events = []
        # Legacy journals may contain a fixed-capital or simulated peak. Rebase
        # once to the verified live account, never interpret that as a crash.
        # Risk uses cash + holdings so an ordinary sale is not a drawdown;
        # SIP allocation itself uses holdings market value only.
        _mode_now = "LIVE_SIP"
        _mode_stored = self.db.meta_get("equity_peak_mode", "")
        if _mode_stored != _mode_now:
            _legacy_meta_peak = float(self.db.meta_get("equity_peak", 0.0) or 0.0)
            if _mode_stored or _legacy_meta_peak > 0:
                P.warn(f"Legacy risk peak rebased to live account equity {fmt_cr(equity)}.")
                self.peak = max(equity, 0.0)
                self.halved = False
                self.all_cash = False
                self.state = "NORMAL"
                self.trigger_date = ""
                self.db.meta_set("risk_peak_rebased_date", today_str())
            self.db.meta_set("equity_peak_mode", _mode_now)
            self._save()
        if equity > self.peak:
            self.peak = equity
        dd = 1.0 - equity / self.peak if self.peak > 0 else 0.0
        # --- release rules (audit bug #5) ---
        # (a) classic: recover to within REENTRY_BAND of prior peak
        # (b) TIME-BASED: after RISK_RELEASE_DAYS the lock is released at the
        #     next review even without price recovery — sitting in cash cannot
        #     heal a drawdown, and the old peak would re-trigger instantly —
        #     so the peak is RESET to current equity on time-based release.
        if self.halved or self.all_cash:
            released = False
            reason = ""
            if dd <= REENTRY_BAND:
                released, reason = True, (
                    f"equity recovered to within {fmt_pct(REENTRY_BAND)} of "
                    f"peak {fmt_rs(self.peak)}")
            elif self.trigger_date:
                try:
                    trig = parse_date(self.trigger_date)
                    if (parse_date(today_str()) - trig).days >= RISK_RELEASE_DAYS:
                        released = True
                        self.peak = equity          # reset the slate
                        reason = (f"{RISK_RELEASE_DAYS} days elapsed since the "
                                  f"trigger — time-based release; peak reset to "
                                  f"{fmt_rs(equity)} so the rule can fire again "
                                  f"on a NEW decline")
                except Exception:
                    pass
            if released:
                self.halved = False
                self.all_cash = False
                self.state = "NORMAL"
                self.trigger_date = ""
                self.db.execute(
                    "INSERT INTO risk_events(ts,level,drawdown,action,detail) VALUES(?,?,?,?,?)",
                    (now_iso(), "RECOVERY", dd, "restore full sizing", reason))
                events.append(("RECOVERY", f"Risk limits released — {reason}."))
                # peak may have been reset — recompute dd so the trigger block
                # below cannot re-lock the account on the OLD drawdown
                dd = 1.0 - equity / self.peak if self.peak > 0 else 0.0
        dd_review, dd_severe, dd_extreme = dd_thresholds(self.vol_target)
        if dd >= dd_extreme and not self.all_cash:
            self.all_cash = True
            self.state = "EXTREME"
            self.trigger_date = today_str()
            self.trigger_counts["EXTREME"] += 1
            self.db.execute(
                "INSERT INTO risk_events(ts,level,drawdown,action,detail) VALUES(?,?,?,?,?)",
                (now_iso(), "EXTREME", dd, "move to 100% cash",
                 f"drawdown {fmt_pct(dd)} breached {fmt_pct(dd_extreme)}"))
            events.append(("EXTREME", f"Portfolio fell {fmt_pct(dd)} from peak — moving 100% to cash "
                                      f"(protection rule, not a guaranteed stop-loss)."))
        elif dd >= dd_severe and not self.halved and not self.all_cash:
            self.halved = True
            self.state = "SEVERE"
            self.trigger_date = today_str()
            self.trigger_counts["SEVERE"] += 1
            self.db.execute(
                "INSERT INTO risk_events(ts,level,drawdown,action,detail) VALUES(?,?,?,?,?)",
                (now_iso(), "SEVERE", dd, "halve target invested fraction",
                 f"drawdown {fmt_pct(dd)} breached {fmt_pct(dd_severe)}"))
            events.append(("SEVERE", f"Portfolio fell {fmt_pct(dd)} from peak — target invested "
                                     f"amount is HALVED until recovery."))
        elif dd >= dd_review and self.state == "NORMAL":
            self.state = "REVIEW"
            self.trigger_counts["REVIEW"] += 1
            self.db.execute(
                "INSERT INTO risk_events(ts,level,drawdown,action,detail) VALUES(?,?,?,?,?)",
                (now_iso(), "REVIEW", dd, "risk review",
                 f"drawdown {fmt_pct(dd)} breached {fmt_pct(dd_review)}; holdings documented"))
            events.append(("REVIEW", f"Portfolio fell {fmt_pct(dd)} from peak — risk review triggered."))
        self.db.meta_set("risk_last_eval", today_str())
        self._save()
        return {"drawdown": dd, "peak": self.peak, "state": self.state,
                "halved": self.halved, "all_cash": self.all_cash, "events": events}

    def risk_scale(self) -> float:
        if self.all_cash:
            return 0.0
        if self.halved:
            return 0.5
        return 1.0

    def snapshot(self, date: str, cash: float, holdings_value: float, equity: float):
        inv = safe_div(holdings_value, equity, 0.0)
        dd = 1.0 - equity / self.peak if self.peak > 0 else 0.0
        self.db.execute(
            "INSERT INTO portfolio_snapshots(date,cash,holdings_value,equity,invested_frac,peak,drawdown,mode) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET cash=excluded.cash, holdings_value=excluded.holdings_value, "
            "equity=excluded.equity, invested_frac=excluded.invested_frac, peak=excluded.peak, "
            "drawdown=excluded.drawdown, mode=excluded.mode",
            (date, cash, holdings_value, equity, inv, self.peak, dd,
             "LIVE"))


# ==============================================================================
# SECTION 19 — TRADING CYCLE (live SIP: signal -> size -> execute)
# ==============================================================================

def resolve_pending_live_orders(db: Db, broker: "Broker") -> int:
    """Re-poll every unresolved live submission. Missing/ambiguous outcomes
    remain blocking; cumulative partial fills are applied exactly once."""
    if broker.client is None:
        return 0
    done = 0
    for r in db.query("SELECT * FROM orders WHERE mode='LIVE' AND status IN "
                      "('SUBMITTING','PENDING','UNCONFIRMED') ORDER BY id"):
        oid = r["broker_order_id"]
        if not oid:
            match = re.search(r"live order id ([A-Za-z0-9_-]+)", r["reason"] or "")
            oid = match.group(1) if match else ""
        if not oid:
            P.warn(f"Unconfirmed {r['side']} {r['symbol']} submission #{r['id']} has no broker id; "
                   "budget remains reserved. Check Upstox and reconcile manually before retrying.")
            continue
        try:
            det = broker.client.order_details(oid)
            d = (det or {}).get("data") or {}
            if isinstance(d, list):
                d = next((x for x in d if str(x.get("order_id")) == str(oid)), {})
            before = (r["status"], int(r["filled_qty"] or 0))
            res = broker._update_order_from_details(r, d)
            if (res["status"], int(res.get("filled_qty", 0))) != before:
                done += 1
                P.info(f"Order {oid} {r['symbol']}: {res['status']} ({res.get('note', '')}).")
        except Exception as e:
            P.note(f"Order {oid} {r['symbol']}: re-check failed ({e}); reservation retained.")
    return done


class TradingCycle:
    def __init__(self, db: Db):
        self.db = db
        try:
            env = load_env()
        except Exception:
            env = {"UPSTOX_ACCESS_TOKEN": "", "UPSTOX_API_TOKEN": "", "UPSTOX_API_SECRET": ""}
        self.client = UpstoxClient(env)
        self.data = DataManager(db, self.client)   # history helpers (regime, sleeve)
        self.filters = FilterEngine(db)
        self.builder = PortfolioBuilder(db, self.filters)
        self.tax = TaxEngine(db)
        self.broker = Broker(db, self.tax, self.client)
        self.risk = RiskEngine(db)

    # ------------------------------------------------------------------ #
    def latest_prices(self) -> dict:
        prices = {}
        for r in self.db.query(
                "SELECT p.symbol AS symbol, p.close AS close FROM price_daily p WHERE p.date = "
                "(SELECT MAX(date) FROM price_daily WHERE symbol=p.symbol)"):
            prices[r["symbol"]] = r["close"]
        return prices

    def last_month_end(self) -> str:
        rows = self.db.query("SELECT DISTINCT date FROM price_daily ORDER BY date")
        dates = [r["date"] for r in rows]
        if not dates:
            return today_str()
        return last_month_end_date(dates) or dates[-1]

    # ---------------- Daily SIP machinery (user request 2026-10-19) ----------
    def sip_basket(self) -> list:
        """The currently selected ETFs (the daily buy list).  Stored at each
        monthly rebalance; derived once from the current signal if missing."""
        raw = self.db.meta_get(SIP_BASKET_KEY, "") or ""
        syms = [s.strip() for s in raw.split(",") if s.strip()]
        if syms:
            return syms
        as_of = self.last_month_end()
        stats = self.collect_stats(as_of)
        if not stats:
            return []
        sel = self.builder.select(stats, set(self.broker.holdings().keys()),
                                  equity=self.broker.equity(self.latest_prices()))
        syms = [s["symbol"] for s in sel["chosen"]]
        if syms:
            self.db.meta_set(SIP_BASKET_KEY, ",".join(syms))
            self.db.meta_set(SIP_BASKET_DATE_KEY, as_of)
            P.info(f"SIP basket initialized from the {as_of} signal: {', '.join(syms)}")
        return syms

    def sip_targets(self, basket: list, equity: float) -> dict:
        """Sized target weights {symbol: weight} for the SIP basket, produced by
        the standard sizing engine (inverse-vol -> caps -> covariance vol target).
        Stored at the monthly rebalance; re-derived from the last month-end
        signal on demand for journals written by older versions."""
        raw = self.db.meta_get(SIP_TARGETS_KEY, "") or ""
        out = {}
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    out = {k: float(v) for k, v in parsed.items() if k in basket}
            except Exception:
                out = {}
        if basket and len(out) == len(basket):
            return out
        as_of = self.last_month_end()
        stats = self.collect_stats(as_of)
        hold_syms = set(self.broker.holdings().keys())
        # audit v14 P1 (2026-10-30): the re-derivation used to SIZE the stored
        # basket symbols directly, skipping selection filters (iNAV premium
        # block, 200-DMA overlay, correlation cap, one-per-index-family).  A
        # legacy journal could therefore size towards an ETF the selector
        # would have blocked.  Re-run the real selection and intersect.
        sel = self.builder.select(stats, hold_syms, equity=equity)
        sel_syms = {c["symbol"] for c in (sel.get("chosen") if isinstance(sel, dict) else sel or [])}
        chosen = [s for s in stats if s["symbol"] in basket
                  and s["symbol"] in sel_syms]
        dropped = sorted(set(basket) - {s["symbol"] for s in chosen})
        if dropped:
            P.warn("Stored plan names failed today's selection filters and are "
                   "excluded from sizing: " + ", ".join(dropped))
        if not chosen:
            return {}
        sized = self.builder.size(chosen, equity, risk_scale=1.0)
        out = {k: v for k, v in sized["weights"].items() if k in basket}
        if out:
            self.db.meta_set(SIP_TARGETS_KEY, json.dumps(out))
            P.info(f"SIP target weights derived from the {as_of} signal: "
                   + ", ".join(f"{k} {fmt_pct(v, 1)}" for k, v in
                               sorted(out.items(), key=lambda kv: -kv[1])))
        return out

    def _live_quotes(self, symbols):
        """Live LTP / spread / session-day flags from Upstox quotes.
        Returns (ltp_map, spread_map, today_flag_map).  Empty maps offline —
        callers then fall back to the latest close, honestly labelled."""
        qmap, qspread, tflag = {}, {}, {}
        symbols = [s for s in dict.fromkeys(symbols) if s]
        if not symbols:
            return qmap, qspread, tflag
        try:
            client = UpstoxClient(load_env())
        except Exception as e:
            P.note(f"Live quotes unavailable ({e}) — fills use the latest close.")
            return qmap, qspread, tflag
        for sym in symbols:
            key_row = self.db.one(
                "SELECT instrument_key FROM etf_universe WHERE symbol=?", (sym,))
            key = (key_row["instrument_key"] if key_row else "") or f"NSE_EQ|{sym}"
            try:
                q = client.quote(key)
                if q.get("ltp"):
                    qmap[sym] = float(q["ltp"])
                    if q.get("bid") and q.get("ask"):
                        qspread[sym] = min(
                            abs(float(q["ask"]) - float(q["bid"])) / qmap[sym] * 10000.0,
                            SPREAD_MAX_BPS)
                    tflag[sym] = quote_is_today(q.get("ts"), today_str())
                time.sleep(HTTP_SLEEP)
            except Exception as e:
                P.warn(f"quote {sym} failed ({e})")
        return qmap, qspread, tflag

    def sell_fraction(self, sym: str, price: float, level: str, as_of: str,
                      reason: str) -> dict:
        """Risk de-risk sell: SEVERE halves the position, EXTREME liquidates it.
        Large orders are sliced to 15% of p25 daily value per session; the
        remainder is queued in pending_trades (same rule as exit slicing)."""
        h = self.broker.holdings().get(sym)
        if not h or h["qty"] <= 0:
            return {"status": "SKIP", "reason": "no position"}
        # user direction 2026-10-28: qty already committed to an open sell
        # order at the broker is not sellable a second time (live truth)
        avail = max(0.0, h["qty"] - self.broker.live_open_qty(sym, "SELL"))
        if avail <= 0:
            return {"status": "SKIP",
                    "reason": "all units already queued in an open broker sell order"}
        qty = derisk_sell_qty(avail, level)
        if qty <= 0:
            return {"status": "SKIP",
                    "reason": f"{avail:.0f} sellable unit(s) too small to {level.lower()}"}
        if not price or price <= 0:
            price = h["avg_cost"]
        p25 = self.filters.liquidity_p25(sym, as_of)
        max_qty = int(POS_CAP_LIQ_FRAC * p25 / price) if price > 0 else 0
        qty_now, remain = qty, 0
        if max_qty > 0 and qty > max_qty:
            qty_now, remain = max_qty, qty - max_qty
            self.db.execute(
                "INSERT INTO pending_trades(signal_as_of,exec_after,symbol,side,"
                "target_qty,reason,status,created) VALUES(?,?,?,?,?,?,?,?)",
                (as_of, today_str(), sym, "SELL", remain,
                 f"de-risk slice ({level}) — remainder queued", "PENDING", now_iso()))
            P.warn(f"SELL {sym}: {qty_now} of {qty} units today (liquidity slice); "
                   f"{remain} queued for the next session.")
        res = self.broker.execute(sym, "SELL", qty_now, price, as_of,
                                  reason=reason, spread_bps=8.0)
        self.broker.print_order_ticket(res, sym,
                                       universe_map().get(sym, {}).get("name", sym),
                                       "SELL")
        return res

    def apply_derisk(self, risk_ctx: dict, prices: dict) -> int:
        """Execute the risk engine's verdicts as real sells (audit 2026-10-19
        daily-SIP mode): SEVERE sells half ONCE when it fires; EXTREME sells
        everything and keeps the book empty while the lock is on."""
        fired = {lv for lv, _ in (risk_ctx.get("events") or [])}
        n = 0
        if "SEVERE" in fired and "EXTREME" not in fired:
            P.section("SAFETY SELLING — portfolio down badly; halving every holding", "\U0001F6E1")
            for sym in sorted(self.broker.holdings()):
                # audit v5 #4: the cash sleeve is already the safest asset —
                # halving/selling it buys nothing but charges
                if sym in CASH_PROXY_SYMBOLS:
                    continue
                self.sell_fraction(sym, prices.get(sym, 0.0), "SEVERE", today_str(),
                                   "safety rule: portfolio fell far from its peak — sell half")
                n += 1
        if "EXTREME" in fired or (self.risk.all_cash and self.broker.holdings()):
            if "EXTREME" in fired:
                P.section("SAFETY SELLING — portfolio fell too far; moving fully to cash", "\U0001F6E1")
            for sym in sorted(self.broker.holdings()):
                if sym in CASH_PROXY_SYMBOLS:
                    continue        # the sleeve IS the "fully to cash" state
                self.sell_fraction(sym, prices.get(sym, 0.0), "EXTREME", today_str(),
                                   "safety rule: portfolio fell too far — sell everything")
                n += 1
        return n

    def consume_pending_sells(self, prices: dict) -> int:
        """Next-session worker for queued SELL remainders (sliced exits/de-risks).
        Stands down while the monthly signal is still OPEN — in that window the
        rebalance re-drive owns the exits and must not be doubled up."""
        rows = self.db.query(
            "SELECT * FROM pending_trades WHERE status='PENDING' AND side='SELL' "
            "AND symbol<>'*' AND exec_after < ?", (today_str(),))
        if not rows:
            return 0
        as_of = self.last_month_end()
        if self.db.meta_get("last_trade_signal", "") != as_of:
            return 0                      # open signal: run() re-drives exits
        n = 0
        for r in rows:
            sym = r["symbol"]
            h = self.broker.holdings().get(sym)
            if not h or h["qty"] <= 0:
                self.db.execute("UPDATE pending_trades SET status='DONE' WHERE id=?",
                                (r["id"],))
                continue
            qty = min(int(r["target_qty"]), int(h["qty"]))
            price = prices.get(sym) or h["avg_cost"]
            res = self.broker.execute(sym, "SELL", qty, price, today_str(),
                                      reason=r["reason"] or "queued exit remainder")
            self.broker.print_order_ticket(res, sym,
                                           universe_map().get(sym, {}).get("name", sym),
                                           "SELL")
            left = int(r["target_qty"]) - qty
            if left <= 0:
                self.db.execute("UPDATE pending_trades SET status='DONE' WHERE id=?",
                                (r["id"],))
            else:
                self.db.execute("UPDATE pending_trades SET target_qty=? WHERE id=?",
                                (left, r["id"]))
            n += 1
        return n

    def deploy_day(self) -> int:
        """Spend only the unused IST-day SIP allowance on eligible ETFs.

        Target weights determine relative routing, not a lump-sum entitlement.
        The allowance is shared across reruns and reserved before submission.
        Sale proceeds and idle broker cash never create extra buying power.
        """
        P.section("TODAY'S SIP — strict daily allowance", "\U0001F4E6")
        if INVEST_PER_DAY <= 0:
            P.info("INVEST_PER_DAY=0: buying is paused. Exit checks, rotation and "
                   "risk sells still run; sale proceeds remain as broker cash.")
            return 0
        if not self.broker.live_book_ok():
            P.warn("No SIP buys: Upstox holdings/cash are not verified.")
            return 0
        remaining = self.broker.sip_remaining()
        committed = self.broker.sip_committed()
        P.kv("Daily SIP cap", fmt_rs(INVEST_PER_DAY))
        P.kv("Already filled / reserved today", fmt_rs(committed))
        P.kv("Available to invest this run", fmt_rs(remaining))
        if remaining < MIN_TRADE_VALUE:
            P.info(f"Unused allowance is below the {fmt_rs(MIN_TRADE_VALUE)} minimum "
                   "order (or the cap is exhausted). No additional buys.")
            return 0

        basket = self.sip_basket()
        if not basket:
            P.warn("No eligible ETF basket yet; no SIP buys.")
            return 0
        sig_date = self.db.meta_get(SIP_BASKET_DATE_KEY, "")
        if sig_date and signal_is_stale(self.db, sig_date)[0]:
            P.note("ETF list is stale — refreshing selection and executing exits before buying.")
            try:
                self.run(force_exec=True, as_of_override=today_str(), quiet=True)
            except Exception as e:
                P.warn(f"Fresh selection failed ({e}); no buys from the stale list.")
                return 0
            basket = self.sip_basket()
            sig_date = self.db.meta_get(SIP_BASKET_DATE_KEY, "") or sig_date
            if not basket or signal_is_stale(self.db, sig_date)[0]:
                P.warn("No fresh eligible basket; SIP buying paused.")
                return 0
        P.kv("ETFs to buy into", ", ".join(basket))
        if sig_date:
            P.kv("List chosen on", sig_date)
        if not sip_buys_allowed(self.risk.state):
            P.warn(f"Risk state {self.risk.state}: new SIP buys paused.")
            return 0

        prices = self.latest_prices()
        eq0 = self.broker.holdings_value()
        holdings0 = self.broker.holdings()
        targets = self.sip_targets(basket, eq0)
        if not targets:
            P.warn("No eligible target weights; no SIP buys.")
            return 0
        qmap, qspread, tflag = self._live_quotes(basket)
        market_open = market_open_now()
        if not market_open:
            P.note("Outside regular NSE hours: eligible orders are marked AMO and "
                   "reference the latest downloaded close. Upstox/exchange acceptance "
                   "and execution are still required; this is not a simulated fill.")
        try:
            hard_blocks = json.loads(self.db.meta_get(DEPLOY_SKIP_KEY, "") or "{}")
            if not isinstance(hard_blocks, dict):
                hard_blocks = {}
        except Exception:
            hard_blocks = {}
        plan, parked, hard_dropped = {}, [], []
        for sym in basket:
            try:
                raw = self.data.series(sym, today_str(), 300, "adj_close") or []
                adj = [float(row_adj(r)) for r in raw]
                issue = series_integrity_issue(adj)
            except Exception:
                adj, issue = [], "price history unreadable"
            if issue:
                P.warn(f"Not buying {sym}: {issue}")
                hard_blocks[sym] = int(hard_blocks.get(sym, 0)) + 1
                continue
            if SIP_TREND_GUARD and exit_rules_fire(adj):
                P.warn(f"Not buying {sym}: current momentum/DMA exit rules fire.")
                parked.append(sym)
                continue
            if int(hard_blocks.get(sym, 0)) >= DEPLOY_HARD_BLOCK_DAYS:
                hard_dropped.append(sym)
                continue
            if targets.get(sym, 0.0) <= 0:
                continue
            px = qmap.get(sym) if market_open and tflag.get(sym) is True else prices.get(sym)
            if market_open and tflag.get(sym) is not True:
                P.warn(f"Not buying {sym}: a fresh same-day Upstox quote is required while market is open.")
                continue
            if not px or px <= 0:
                P.warn(f"Not buying {sym}: no valid reference price.")
                continue
            if qmap.get(sym) and prices.get(sym):
                dev = qmap[sym] / prices[sym] - 1.0
                if abs(dev) > QUOTE_MAX_DEV:
                    P.warn(f"{sym}: live quote differs {fmt_pct(dev)} from last close; "
                           "using the live quote, subject to the premium gate.")
            plan[sym] = {"px": float(px)}

        # Route the contribution by relative target weights. Projecting the
        # portfolio by the unused allowance lets an empty book start AND lets
        # an already-balanced book keep growing on later SIP days.
        weight_sum = sum(targets[s] for s in plan)
        if weight_sum <= 0:
            P.warn("No buyable ETF has a positive target weight.")
            return 0
        projected = eq0 + remaining
        gaps = {}
        for sym, item in plan.items():
            desired = targets[sym] / weight_sum * projected
            current = holdings0.get(sym, {}).get("qty", 0.0) * item["px"]
            gaps[sym] = max(0.0, desired - current)
        caps = {}
        for sym in plan:
            p25 = self.filters.liquidity_p25(sym, today_str()) or 0.0
            if p25 > 0:
                caps[sym] = POS_CAP_LIQ_FRAC * p25
        slices = day_slices(gaps, remaining, caps)
        if not slices:
            P.info("No allocatable SIP slice after target gaps, liquidity caps and minimum-ticket checks.")
            slices = {}
        submitted = filled = 0
        for sym, slice_val in sorted(slices.items(), key=lambda kv: (-kv[1], kv[0])):
            left = self.broker.sip_remaining()
            if left < MIN_TRADE_VALUE:
                break
            if self.broker.live_open_qty(sym, "BUY") > 0:
                P.info(f"{sym}: an earlier buy is still open/unconfirmed; not stacking another order.")
                continue
            px = plan[sym]["px"]
            order_type, limit_px = live_buy_order_px(px)
            qty = int(min(slice_val, left) / limit_px)
            reserved = qty * limit_px
            if qty <= 0 or reserved < MIN_TRADE_VALUE:
                continue
            ok, why, _ = self.filters.check_execution(sym, today_str(), reserved)
            if ok:
                blocked, dev, lim = self.filters.inav_premium_block(sym, today_str(), price=px)
                if blocked:
                    ok = False
                    why = (f"buy price is {fmt_pct(dev)} above NAV/fair value "
                           f"(allowed {fmt_pct(lim)})")
            if not ok:
                P.warn(f"Not buying {sym}: {why}")
                if "NAV" in why or "fair value" in why:
                    hard_blocks[sym] = int(hard_blocks.get(sym, 0)) + 1
                self.db.execute(
                    "INSERT INTO orders(ts,signal_as_of,exec_date,symbol,side,qty,price,value,"
                    "slippage,charges,status,mode,order_type,reason) "
                    "VALUES(?,?,?,?,?,?,?,?,0,0,'SKIPPED_FILTER','LIVE','LIMIT',?)",
                    (now_iso(), today_str(), today_str(), sym, "BUY", qty, px, reserved, why))
                continue
            spread = qspread.get(sym)
            if spread is None:
                broad = self.db.one("SELECT broad FROM etf_universe WHERE symbol=?", (sym,))
                spread = estimate_spread_bps(self.filters.liquidity_p25(sym, today_str()),
                                             broad=bool(broad and broad["broad"]))
            res = self.broker.execute(sym, "BUY", qty, px, today_str(),
                                      reason=f"SIP from {fmt_rs(INVEST_PER_DAY)} daily cap",
                                      spread_bps=spread, order_type=order_type, limit_px=limit_px)
            self.broker.print_order_ticket(res, sym, universe_map().get(sym, {}).get("name", sym), "BUY")
            if res.get("status") in ("FILLED", "PENDING", "UNCONFIRMED", "SUBMITTING"):
                submitted += 1
                filled += res.get("status") == "FILLED"
                hard_blocks.pop(sym, None)

        try:
            self.db.meta_set(DEPLOY_SKIP_KEY, json.dumps({s: n for s, n in hard_blocks.items() if n > 0}))
            offenders = sorted(s for s in set(hard_blocks) | set(hard_dropped)
                               if int(hard_blocks.get(s, 0)) >= DEPLOY_HARD_BLOCK_DAYS)
            if offenders:
                tk = json.loads(self.db.meta_get(SIP_TARGETS_KEY, "") or "{}")
                for s in offenders:
                    tk.pop(s, None)
                self.db.meta_set(SIP_TARGETS_KEY, json.dumps(tk))
                P.warn("Dropped until re-plan after repeated hard blocks: " + ", ".join(offenders))
        except Exception as e:
            P.note(f"Could not update hard-block journal: {e}")
        # Summary only; orders.reserved_value is the atomic source of truth.
        self.db.meta_set(DEPLOY_DAY_KEY, json.dumps({
            "date": today_str(), "deployed": self.broker.sip_committed(),
            "orders": self.db.one("SELECT COUNT(*) AS n FROM orders WHERE exec_date=? "
                                  "AND side='BUY' AND mode='LIVE' AND status IN "
                                  "('FILLED','PENDING','UNCONFIRMED','SUBMITTING')", (today_str(),))["n"]}))
        P.ok(f"SIP run: {submitted} order(s) submitted, {filled} fully filled. "
             f"Today's filled + reserved notional: {fmt_rs(self.broker.sip_committed())} "
             f"of {fmt_rs(INVEST_PER_DAY)}; unused {fmt_rs(self.broker.sip_remaining())}.")
        if parked:
            P.info("Trend guard paused buys in: " + ", ".join(parked))
        return submitted

    def daily_exit_guard(self):
        """Exit a holding the DAY its rules fire instead of waiting for the
        month-end rebalance. Historical R12A measured a return tradeoff; this
        live safety guard is permanently enabled by user direction.
        This guard is locked on for the live SIP engine."""
        P.section("DAILY EXIT GUARD — quit falling ETFs today, not at month-end", "\U0001F6E1")
        prices = self.latest_prices()
        fired = 0
        for sym, h in list(self.broker.holdings().items()):
            qty = float((h or {}).get("qty", 0.0) or 0.0)
            if qty <= 0:
                continue
            # audit v5 #4: the idle-cash sleeve is exempt — it IS the cash
            if sym in CASH_PROXY_SYMBOLS:
                continue
            try:
                raw = self.data.series(sym, today_str(), 300, "adj_close") or []
                adj = [float(r) if isinstance(r, (int, float)) else float(row_adj(r))
                       for r in raw]
            except Exception:
                continue
            if not exit_rules_fire(adj):
                continue
            px = prices.get(sym)
            if not px:
                continue
            # user direction 2026-10-28: respect units already queued in an
            # open broker sell order (live truth) — never double-sell
            qty = min(qty, self.broker.sellable_qty(sym))
            if qty <= 0:
                P.info(f"{sym}: a sell order is already working at the broker.")
                continue
            res = self.broker.execute(
                sym, "SELL", qty, px, today_str(),
                reason="EXIT-GUARD: trend broke before the month-end check")
            self.broker.print_order_ticket(
                res, sym, universe_map().get(sym, {}).get("name", sym), "SELL")
            if res.get("status") == "FILLED":
                fired += 1
        if fired:
            P.warn(f"EXIT-GUARD sold {fired} holding(s) whose trend broke today "
                   f"(did not wait for month-end).")
        else:
            P.ok("EXIT-GUARD: everything held is still trending up — nothing to "
                 "sell today.")
        return fired

    def run_daily(self, force_exec: bool = False, final_status: bool = True) -> float:
        """Live SIP cycle. Zero allowance disables buys only, never exit logic.
        Returns the Upstox ETF portfolio market value (cash excluded)."""
        P.section("LIVE SIP RUN — exits, rebalance, then capped buying", "\U0001F4C5")
        P.info(f"Today (IST): {today_str()} | Daily buy cap: {fmt_rs(INVEST_PER_DAY)}")
        if self.db.meta_get("data_origin") == "SYNTHETIC":
            P.err("Synthetic price database: refusing all live orders. Run update --force-data first.")
            return 0.0
        dp_max = self.db.one("SELECT MAX(date) AS d FROM price_daily")["d"]
        lcs = last_completed_session()
        if not dp_max or dp_max < prev_completed_session(lcs):
            P.err(f"Price data stale (newest {dp_max or 'none'}, last completed {lcs}). "
                  "Run update before trading; no orders placed.")
            return 0.0
        if 0 < INVEST_PER_DAY < MIN_TRADE_VALUE:
            P.warn(f"INVEST_PER_DAY={fmt_rs(INVEST_PER_DAY)} is below the "
                   f"{fmt_rs(MIN_TRADE_VALUE)} minimum order: no buys can fit. Exits still run.")
        try:
            resolve_pending_live_orders(self.db, self.broker)
        except Exception as e:
            P.warn(f"Pending-order reconciliation failed ({e}); unresolved reservations remain blocking.")
        if not self.broker.sync_live_book():
            P.err("Broker truth unavailable — analysis only, no orders.")
            return 0.0

        prices = self.latest_prices()
        account_eq = self.broker.account_equity()
        hv = self.broker.holdings_value()
        ctx = self.risk.update(account_eq, "daily")
        for level, msg in ctx["events"]:
            P.warn(f"SAFETY ALERT ({level}): {msg}")
        self.apply_derisk(ctx, prices)
        self.consume_pending_sells(prices)
        as_of = self.last_month_end()
        if force_exec or self.db.meta_get("last_trade_signal", "") != as_of:
            stale, age = (False, 0) if force_exec else signal_is_stale(self.db, as_of)
            if stale:
                P.info(f"Month-end signal {as_of} is {age} sessions old; deriving a fresh basket.")
                self.run(force_exec=True, as_of_override=today_str(), quiet=True)
            else:
                P.info(f"Rebalance check {as_of}: rotate/exit holdings; all buys use the SIP cap.")
                self.run(force_exec=force_exec)
        else:
            P.ok(f"Rebalance {as_of} already handled; next check after {next_month_end_hint()}.")
        self.daily_exit_guard()
        self.deploy_day()
        for _ in range(2):
            try:
                if not resolve_pending_live_orders(self.db, self.broker):
                    break
            except Exception:
                break
            time.sleep(3)
        # Broker marks/quantities win over provisional same-run fill mirrors.
        if not self.broker.sync_live_book():
            P.warn("Post-run broker refresh failed; displayed mirror may be provisional. No further orders.")
        cash = self.broker.cash()
        hv = self.broker.holdings_value()
        account_eq = cash + hv
        self.risk.snapshot(today_str(), cash, hv, account_eq)
        if final_status:
            P.section("UPSTOX ETF PORTFOLIO", "\U0001F4B0")
            P.kv("Holdings market value (portfolio)", fmt_cr(hv), val_color=_C.GREEN)
            P.kv("Available broker cash (separate)", fmt_cr(cash))
            P.kv("SIP cap / committed today", f"{fmt_rs(INVEST_PER_DAY)} / {fmt_rs(self.broker.sip_committed())}")
            P.kv("Risk state", self.risk.state)
            P.note("Drawdown protection uses account equity (cash + ETFs), so selling to cash is not mistaken for a crash.")
        log(f"live SIP done: portfolio={hv:.2f} cash={cash:.2f} sip_committed={self.broker.sip_committed():.2f}")
        return hv

    def collect_stats(self, as_of: str, lookback: int = LOOKBACK, skip: int = SKIP,
                      entry: float = ENTRY_THRESHOLD, exit_thr: float = EXIT_THRESHOLD):
        stats = []
        prem_notes = []
        umap = universe_map()
        for row in self.db.query("SELECT symbol FROM etf_universe WHERE status='ACTIVE' ORDER BY symbol"):
            sym = row["symbol"]
            st = self.builder.stats_at(sym, as_of, lookback, skip, entry, exit_thr)
            if st is None:
                continue
            # audit v7 (28-Oct-2026, MEASURED): a big close-vs-NAV gap on the six
            # GLOBAL-INDICES ETFs is a REAL premium (SEBI/RBI overseas freeze —
            # no new units since 2022/2024; VRO/DSIJ/Mint/BSE all measured it),
            # NOT corrupt data.  Three tiers in one classifier (inav_tier):
            #   corrupt (>300% gap or >20% one-step NAV jump) -> excluded, the
            #     mapping is broken — full diagnostics + navdiag hint.
            #   premium (above the category cap: 0.40/0.75% domestic, 20%
            #     global) -> stays VISIBLE in the scan with a buy-ban flag;
            #     select() marks it SKIPPED with the real reason.
            #   ok -> untouched.
            try:
                _dev, _gap = self.filters.inav_deviation(sym, as_of)
                if _dev is not None:
                    _lim = (self.filters.inav_limit(sym)
                            + INAV_LAG_CUSHION * min(_gap or 0, NAV_MAX_STALE_DAYS))
                    _tier = inav_tier(_dev, nav_last_jump(self.db, sym), _lim)
                    if _tier == "corrupt":
                        _news = self.db.one(
                            "SELECT close FROM price_daily WHERE symbol=? AND date<=? "
                            "ORDER BY date DESC LIMIT 1", (sym, as_of))
                        _msg = (f"{sym}: EXCLUDED — price is {fmt_pct(_dev)} vs its "
                                f"NAV; that gap is corrupt data (check the price "
                                f"series and nav mapping), not a real premium.")
                        _row = nav_map_row(sym)
                        if _row:
                            _navlast = self.db.one(
                                "SELECT nav, date FROM nav_data WHERE symbol=? "
                                "ORDER BY date DESC LIMIT 1", (sym,))
                            _msg += (f"  [scheme {_row.get('code','?')} "
                                     f"'{_row.get('name','?')[:34]}'"
                                     + (f" ISIN {_row.get('isin')}" if _row.get("isin") else "")
                                     + (f"; NAV {fmt_rs(_navlast['nav'])} {_navlast['date']}"
                                        if _navlast else "")
                                     + (f" vs close {fmt_rs(_news['close'])}" if _news else "")
                                     + f" — verify: python etf-momentum-screener.py navdiag {sym}]")
                        P.err(_msg)
                        continue
                    if _tier == "premium":
                        st["premium_over"] = (_dev, _lim)
                        prem_notes.append((sym, _dev, _lim))
            except Exception:
                pass
            st["name"] = umap.get(sym, {}).get("name", sym)
            st["category"] = umap.get(sym, {}).get("category", "")
            st["index_family"] = umap.get(sym, {}).get("index_family", sym)
            st["spread_bps"] = self.filters.spread_p90(sym, as_of)
            stats.append(st)
        if prem_notes:
            # audit v9 (2026-10-28): split the note by cause — the SEBI-freeze
            # story and the 20% cap belong ONLY to global ETFs; a domestic name
            # over its ~1% band is a normal market-maker gap, and quiet re-picks
            # print no table, so never promise one
            _globs = [(s, d) for s, d, _l in prem_notes
                      if (umap.get(s, {}).get("category", "") or "").upper()
                      == "GLOBAL INDICES"]
            _doms = [(s, d, l) for s, d, l in prem_notes
                     if (umap.get(s, {}).get("category", "") or "").upper()
                     != "GLOBAL INDICES"]
            if _globs:
                P.note("Premium guard (real market premium, NOT bad data): "
                       + ", ".join(f"{s} {fmt_pct(d)}" for s, d in _globs)
                       + " — these global ETFs trade ABOVE what they are really "
                         "worth because SEBI's overseas-investment freeze stopped "
                         "new units (BSE warned late-Sep-2026: the premium can snap "
                         "shut overnight; price bands anchor to NAV from "
                         "1-Apr-2027).  Buys stay banned above the 20% cap and "
                         "resume automatically when the premium cools under it.")
            if _doms:
                P.note("Premium guard: "
                       + ", ".join(f"{s} {fmt_pct(d)} over fair value "
                                   f"(its band is {fmt_pct(l)})"
                                   for s, d, l in _doms)
                       + " — buying paused until the gap cools; market makers "
                         "close such small premiums fast, so overpaying today "
                         "locks in a small loss.  It buys automatically once back "
                         "inside the band.")
        return stats

    # ------------------------------------------------------------------ #
    def print_signal_console(self, selection: dict, as_of: str):
        P.section(f"MONTH-END CHECK — which ETFs qualify as of {as_of}", "\U0001F4AC")
        chosen_syms = {s["symbol"] for s in selection["chosen"]}
        rows = []
        def dec_color(dec):
            return {"ELIGIBLE_BUY": _C.GREEN, "HOLD": _C.CYAN, "EXIT": _C.RED,
                    "CASH": _C.GREY, "SKIPPED": _C.YELLOW}.get(dec, _C.WHITE)
        ordered = sorted(selection["decisions"],
                         key=lambda t: ({"ELIGIBLE_BUY": 0, "HOLD": 1, "EXIT": 2,
                                         "CASH": 3, "SKIPPED": 4}.get(t[1], 9),
                                        -(t[0]["momentum"] or -9)))
        for st, dec, reason in ordered:
            mom = st["momentum"]
            rows.append([
                st["symbol"],
                st["name"][:26],
                fmt_pct(mom) if mom is not None else "n/a",
                fmt_pct(st["vol"] or 0, 1, signed=False)
                + (" ⚑" if vol_plausibility(st["symbol"], st["vol"]) else ""),
                fmt_cr(st["liq_p25"]),
                paint(dec, dec_color(dec)),
                "★ SELECTED" if st["symbol"] in chosen_syms else "",
                # audit v6 #4 (2026-10-28): the bare word "SKIPPED" read as a
                # bug; say WHY inline (the reasons were only printed in the
                # recap below the table, and easy to miss)
                (reason or "")[:46],
            ])
        P.table(["Symbol", "Name", "1Y Trend", "Risk (vol)", "Daily Traded (p25)",
                 "Decision", "", "Why"],
                rows, aligns=["<", "<", ">", ">", ">", "<", "<", "<"])
        flagged_vol = [st["symbol"] for st, _d, _r in ordered
                       if vol_plausibility(st["symbol"], st["vol"])]
        if flagged_vol:
            P.note(f"⚑ annualized-vol print beyond the category plausibility "
                   f"ceiling: {', '.join(flagged_vol)} — likely an unadjusted "
                   f"corporate action or spliced series. Sizing already shrinks "
                   f"these weights; verify the data before trusting the signal.")
        # easy-language recap
        P.sub("What this means in plain words")
        for st, dec, reason in ordered:
            if dec == "ELIGIBLE_BUY":
                P.decision("\U0001F7E2", f"{st['symbol']}: BUY candidate",
                           f"{st['name']} — {reason}", _C.GREEN)
            elif dec == "HOLD":
                P.decision("\U0001F535", f"{st['symbol']}: keep holding",
                           f"{st['name']} — {reason}", _C.CYAN)
            elif dec == "EXIT":
                P.decision("\U0001F534", f"{st['symbol']}: SELL signal",
                           f"{st['name']} — {reason}", _C.RED)
            elif dec == "SKIPPED":
                P.decision("⚪", f"{st['symbol']}: cannot trade", reason, _C.YELLOW)
        # observability (external audit F1, 2026-10-27): the family rule and
        # the correlation cap drop higher-ranked candidates SILENTLY (paste:
        # MON100 lost its seat to MONQ50 with no printed reason).  Say why.
        drops = self.builder.dropped_reasons(selection["chosen"])
        if drops:
            P.sub("Why higher-ranked candidates were left out")
            for pos, sym, mom, why in drops[:5]:
                mom_s = fmt_pct(mom) if mom is not None else "n/a"
                P.info(f"  rank #{pos:<2} {sym:<12} {mom_s:>8}  — {why}")

    def print_portfolio_plan(self, sized: dict, equity: float):
        P.sub("The plan — how much goes into each ETF")
        if not sized["weights"]:
            P.info("Everything stays in cash this month — no ETF qualifies.")
            return
        rows = []
        for sym, w in sorted(sized["weights"].items(), key=lambda kv: -kv[1]):
            cap = sized["capped"].get(sym, POS_CAP_ABS)
            # audit v4 #8: say WHY the weight was capped. When cap == POS_CAP_ABS
            # the binder is the 25% one-name rule, NOT thin trading — the old
            # "thinly traded" label wrongly blamed liquidity for large funds.
            _hit = w / max(sized.get("risk_scale", 1.0), 1e-9) >= cap - 1e-9
            if not _hit:
                _why = ""
            elif cap >= POS_CAP_ABS - 1e-9:
                _why = "capped: max 25% per ETF"
            else:
                _why = "capped: thinly traded"
            rows.append([sym, fmt_pct(w, 2), fmt_cr(w * equity),
                         fmt_pct(cap, 2), _why])
        P.table(["ETF", "Planned share", "Planned amount", "Trading limit", "Note"], rows,
                aligns=["<", ">", ">", ">", "<"])
        P.kv("Current ETF portfolio basis", fmt_cr(equity))
        P.note("Weights guide relative SIP routing. They do not reserve broker cash "
               "or authorize purchases above INVEST_PER_DAY.")
        if SIP_MODE:
            # external audit (transcript audit 2026-10-27 §2/§7/§8): the sized
            # plan is NOT the deployable plan when the SIP trend guard parks a
            # name — state the reachable number explicitly, next to the target
            _parked_plan, _dep_val = [], 0.0
            for _sym, _w in sized["weights"].items():
                if _w <= 0:
                    continue
                _m, _above, _v = _quick_trend(self.db, _sym)
                # audit v4 §8.3: unverifiable trend (<200 bars) must not count
                # as buyable — treat None like False
                if _above is not True:
                    _parked_plan.append(_sym)
                else:
                    _dep_val += _w * equity
            if _parked_plan:
                P.kv("Buyable today (still trending up)",
                     f"{fmt_cr(_dep_val)} ({fmt_pct(_dep_val / max(equity, 1.0))} "
                     f"of your money) — PARKED while falling: {', '.join(_parked_plan)}")
            elif sized["weights"]:
                P.kv("Buyable today (still trending up)",
                     "the full plan — nothing is falling")
        P.kv("Expected portfolio risk", f"{fmt_pct(sized['port_vol'], 2, signed=False)} per year "
                                        f"(target {fmt_pct(VOL_TARGET, 0, signed=False)})")
        if sized.get("delta") is not None:
            P.kv("Ledoit-Wolf shrinkage", f"{sized['delta']:.3f} toward constant correlation")
            if sized["delta"] >= 0.98:
                # external audit F6: delta pinned at 1.000 = sample correlations
                # fully discarded — typically ONE outlier series (the ⚑-flagged
                # ones) inflating dispersion; risk estimate is lower-confidence
                P.note("Shrinkage saturated — sample correlations fully discarded, so "
                       "the risk estimate is lower-confidence until the ⚑-flagged "
                       "data is verified.")
        if sized.get("cond") is not None:
            P.kv("Covariance condition number", f"{sized['cond']:,.1f}")
        if not sized.get("vol_reachable", True):
            P.note(f"The {fmt_pct(VOL_TARGET, 0, signed=False)} risk target could not be fully "
                   f"reached inside the caps — the rulebook says: accept the "
                   f"LOWER risk, never add leverage.")

    # ------------------------------------------------------------------ #
    def _current_risk_scale(self, as_of: str) -> float:
        """Audit v4 P2 (2026-10-28): one volatility-scale rule for BOTH sizing
        paths — drawdown-trigger scale AND the bear-regime halving (benchmark
        below its 200-day average).  SIP-mode sizing used to pass
        risk_scale=1.0 unconditionally, so live books ignored the bear halving."""
        risk_scale = self.risk.risk_scale()
        # audit 3.5: halve the vol target while the broad market is below
        # its 200-day average (explicit rule, tested as a hypothesis)
        bench = None
        for cand in ("NIFTYBEES", "HDFCSENSEX", "MONIFTY500"):
            rows = self.data.series(cand, as_of, 210, "adj_close")
            if rows and len(rows) >= 200:
                bench = [r[1] for r in rows]
                break
        if bench and bench[-1] < sum(bench[-200:]) / 200.0:
            risk_scale *= 0.5
            P.info("bear regime (benchmark below its 200-day average) — "
                   "risk budget halved")
        return risk_scale

    def run(self, force_exec: bool = False,
            as_of_override: str = None, quiet: bool = False):
        # audit v5 #11: quiet=True suppresses the full signal/plan tables —
        # used for in-run re-picks (stale-list refresh) whose panel was
        # otherwise printed twice, once for the discarded basket and again
        # for the fresh one.  Orders, journal and meta writes are NOT
        # affected; only the two biggest console panels are replaced by a
        # one-line summary.
        P.section("TSMOM TRADING CYCLE", "\U0001F680")
        P.info("Mode: LIVE TRADING | Rebalance: sell/rotate only; buys share the SIP cap")
        self.broker.holdings()
        if not self.broker.live_book_ok():
            P.err("Broker truth unavailable — rebalance halted; no orders.")
            return 0.0
        all_dates = [r["date"] for r in self.db.query(
            "SELECT DISTINCT date FROM price_daily ORDER BY date")]
        # audit v4 §6 (user choice: refresh): deploy_day may force a RE-DERIVED
        # basket at today's date when the stored list is too stale
        as_of = as_of_override or (last_month_end_date(all_dates) if all_dates else today_str())
        if as_of_override:
            P.info(f"Signal date: {as_of} (re-derived fresh — stale list discarded)")
        else:
            P.info(f"Signal date (last COMPLETED month-end): {as_of}")
        if (REBALANCE_FREQ == "Q" and not as_of_override
                and parse_date(as_of).month not in (3, 6, 9, 12)):
            P.ok(f"Quarterly rebalance mode: {as_of} is not a quarter-end — "
                 f"nothing to do this month.")
            return self.broker.equity(self.latest_prices())

        # ---- audit bug #1: never rebalance twice on the same signal ----
        if self.db.meta_get("last_trade_signal") == as_of and not force_exec:
            P.ok(f"The {as_of} month-end rebalance has already been executed. "
                 f"Nothing to do until the next month-end.")
            P.note("Tip: 'python etf-momentum-screener.py monitor' updates quotes and risk daily.")
            return self.broker.equity(self.latest_prices())

        # audit v6 #2 (2026-10-28): a DUE signal older than the staleness limit
        # is re-derived from TODAY instead of executed late — enforced here in
        # run() itself so EVERY entry path (run_daily's pre-check is just the
        # fast path; `trade`, deploy re-picks) shares one rule and one counter.
        if (as_of_override is None) and not force_exec:
            _stale, _sage = signal_is_stale(self.db, as_of)
            if _stale:
                P.info(f"The {as_of} signal is {_sage} market-days old "
                       f"(limit {STALE_REFRESH_DAYS}) — re-deriving the list and "
                       f"plan from TODAY's data instead of running a dead cycle.")
                return self.run(force_exec=True, as_of_override=today_str(), quiet=True)

        # ---- execution calendar (audit bug #1): signal at month-end, orders
        #      on the NEXT trading session; queue until then ----
        exec_date = None
        for d in all_dates:
            if d > as_of:
                exec_date = d
                break
        if exec_date is None:
            cand = parse_date(as_of) + dt.timedelta(days=1)
            while cand.weekday() >= 5:
                cand += dt.timedelta(days=1)
            exec_date = date_str(cand)
        today = today_str()
        if today < exec_date and not force_exec:
            dup = self.db.one(
                "SELECT id FROM pending_trades WHERE status='PENDING' AND symbol='*' "
                "AND signal_as_of=?", (as_of,))
            if not dup:   # audit 2.9: no duplicate queue rows
                self.db.execute(
                    "INSERT INTO pending_trades(signal_as_of,exec_after,symbol,side,target_qty,reason,status,created) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (as_of, exec_date, "*", "REBALANCE", 0,
                     f"signal computed {as_of}; waiting for next session {exec_date}",
                     "PENDING", now_iso()))
            P.warn(f"Signals are ready, but the rules say orders go out on the NEXT "
                   f"session ({exec_date}). Queued in 'pending_trades'.")
            P.note(f"Run 'python etf-momentum-screener.py trade' on {exec_date} (or use --force-exec "
                   f"to fill now at the latest price).")
            return self.broker.equity(self.latest_prices())
        if today > exec_date:
            P.warn(f"Note: this is a LATE execution (planned {exec_date}, today {today}) — "
                   f"fills use the latest available price.")
        self.db.execute("UPDATE pending_trades SET status='DONE' WHERE status='PENDING' "
                        "AND symbol='*'")   # sliced SELL remainders survive for the daily worker
        eq_before = self.broker.equity()  # ETF market value, no cash or fixed seed
        _account0 = self.broker.account_equity()
        risk_ctx = self.risk.update(_account0, "cycle-start")
        for level, msg in risk_ctx["events"]:
            P.warn(f"RISK [{level}]: {msg}")
        if risk_ctx["all_cash"]:
            P.warn("Risk rules currently force 100% CASH — new buys are blocked until recovery.")
        stats = self.collect_stats(as_of)
        if not stats:
            P.err("No ETF has enough price history yet. Run: python etf-momentum-screener.py update")
            return
        holding_syms = set(self.broker.holdings().keys())
        selection = self.builder.select(stats, holding_syms,
                                        equity=self.broker.equity(self.latest_prices()))
        if quiet:
            P.info(f"Fresh basket picked quietly: "
                   f"{', '.join(s['symbol'] for s in selection['chosen']) or '(none — all cash)'} "
                   f"({len(selection['decisions'])} names scanned).")
        else:
            self.print_signal_console(selection, as_of)
        if SIP_MODE:
            # user 2026-10-19: rebalance SELLS exits only; the portfolio is
            # built by the daily SIP.  The chosen set is the daily buy list.
            self.db.meta_set(SIP_BASKET_KEY,
                             ",".join(s["symbol"] for s in selection["chosen"]))
            self.db.meta_set(SIP_BASKET_DATE_KEY, as_of)
            # Store relative routing weights. They never authorize a lump-sum
            # purchase; every contribution is limited by INVEST_PER_DAY.
            sized = self.builder.size(selection["chosen"], eq_before,
                                      risk_scale=self._current_risk_scale(as_of))
            self.db.meta_set(SIP_TARGETS_KEY, json.dumps(sized["weights"]))
            self.db.meta_set(DEPLOY_SKIP_KEY, "{}")   # fresh plan = fresh chances
            if sized["weights"] and not quiet:
                self.print_portfolio_plan(sized, eq_before)
            P.info(f"This rebalance only sells/rotates. Relative weights route the "
                   f"strict {fmt_rs(INVEST_PER_DAY)} daily SIP allowance among eligible ETFs.")
            P.kv("ETFs picked for this month",
                 ", ".join(s["symbol"] for s in selection["chosen"]) or "(none)")
            # audit §3: say when the whole basket is thematic (no broad index
            # qualifies) — the caps make it LOOK diversified; it is not
            try:
                _broads = {r["symbol"] for r in self.db.query(
                    "SELECT symbol FROM etf_universe WHERE broad=1 AND status='ACTIVE'")}
                _ch = [s["symbol"] for s in selection["chosen"]]
                if _ch and not any(s in _broads for s in _ch):
                    P.note("Basket concentration: every broad-market index is "
                           "below the momentum threshold today, so the basket is "
                           "100% sectoral/thematic/global — more correlated in a "
                           "correction than the family+correlation caps suggest.  "
                           "The resulting FEWER-than-5 names are deliberate: the "
                           "0.70 correlation cap keeps only the least-related "
                           "trends, and re-measurement on 28-Oct-2026 (0.60: "
                           "10.47% CAGR, 0.75: 11.47%) confirmed 0.70 beats both "
                           "(11.80->12.46 with the vol blend) — the narrow basket "
                           "is the bear-market price you pay for crash control.")
            except Exception:
                pass

        # ---- build target basket (execute at the LATEST traded price) ----
        latest_px = self.latest_prices()
        target = {}
        # audit v15 P0-2 (2026-10-30): in SIP_MODE the target map never got
        # the CHOSEN symbols, so every held-and-still-picked ETF fell into
        # the orphan loop below with weight 0 -> the rebalance SOLD its own
        # #1/#2 picks (the live console showed SELL PHARMABEES/MOCAPITAL
        # minutes after re-picking them).  Held-and-chosen is kept, always.
        _chosen_syms = {st["symbol"] for st in selection["chosen"]}
        # exits are mandatory sells
        for st in selection["exits"]:
            target.setdefault(st["symbol"], {"weight": 0.0,
                                             "close": latest_px.get(st["symbol"], st["close"]),
                                             "spread_bps": st["spread_bps"], "name": st["name"]})
            target[st["symbol"]]["weight"] = 0.0
        # audit bug #2: EVERY held symbol must appear in the target map — a held
        # ETF that fell out of the top-3 (or lost its data) is sold, so the
        # portfolio never silently accumulates orphans.
        for sym, h in self.broker.holdings().items():
            if sym not in target:
                # audit v5 #4 (sleeve) + v15 P0-2 (SIP kept-pick) live inside
                # sip_orphan_should_sell — one source of truth for both.
                if not sip_orphan_should_sell(sym, target, _chosen_syms, SIP_MODE):
                    continue
                px = latest_px.get(sym) or h["avg_cost"]
                target[sym] = {"weight": 0.0, "close": px,
                               "spread_bps": self.filters.spread_p90(sym, as_of),
                               "name": universe_map().get(sym, {}).get("name", sym)}
        for sym in target:
            if not target[sym].get("close") or target[sym]["close"] <= 0:
                h = self.broker.holdings().get(sym)
                target[sym]["close"] = h["avg_cost"] if h else 1.0
        for st, dec, reason in selection["decisions"]:
            self.db.execute(
                "INSERT INTO signals(as_of,symbol,strategy,momentum,vol60,liq_p25,spread_p90,decision,reason) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (as_of, st["symbol"], "B", st["momentum"], st["vol"], st["liq_p25"],
                 st["spread_bps"], dec, reason))

        # ---- execution book ----
        P.section("ORDER EXECUTION (next session rules)", "\U0001F4E6")
        pend_now = self.db.query(
            "SELECT * FROM pending_trades WHERE status='PENDING'")
        if pend_now:
            P.info(f"{len(pend_now)} queued trade instruction(s) cleared for execution now.")
        holdings = self.broker.holdings()
        trades_done = 0
        exits_outstanding = 0
        exits_failed = []       # audit v15 P0-3: a FAILED/REJECTED exit used
                                # to pass silently — the signal was then marked
                                # done and the exit NEVER retried
        # planned price for late-fill accounting (audit 2.9)
        planned_px = {}
        if today > exec_date:
            for r in self.db.query("SELECT symbol, close FROM price_daily WHERE date=?",
                                   (exec_date,)):
                planned_px[r["symbol"]] = r["close"]
        # 1) EXITS ALWAYS EXECUTE (audit 2.3).  Filter gates apply to ENTRIES
        #    only; a risk exit must never be blocked by a spread/liquidity gate.
        #    Large exits are SLICED to 15% of p25 per session and carried forward.
        for sym, tgt in sorted(target.items()):
            price = tgt["close"]
            h = holdings.get(sym)
            if not h:
                continue
            # user direction 2026-10-28: what the broker already has WORKING
            # as a sell order is not sellable a second time (live truth)
            avail = max(0.0, h["qty"] - self.broker.live_open_qty(sym, "SELL"))
            if avail <= 0:
                if self.broker.live_open_qty(sym, "SELL") > 0:
                    P.info(f"{sym}: a sell order is already working at the broker "
                           f"— not sending a second one.")
                continue
            have_val = h["qty"] * price
            tgt_val = tgt["weight"] * eq_before
            is_exit = tgt["weight"] <= 0.0
            if tgt_val >= have_val - 1.0:
                continue
            if not is_exit and NO_TRADE_BAND > 0 and have_val > 0:
                # audit 3.1: no-trade band cuts churn (exempt for exits)
                if abs(tgt_val - have_val) / max(tgt_val, have_val) < NO_TRADE_BAND:
                    continue
            sell_val = have_val - tgt_val
            if not is_exit and sell_val < MIN_TRADE_VALUE:
                continue
            qty = int(sell_val / price) if price > 0 else 0
            qty = min(qty, int(avail))
            if is_exit and qty >= avail:
                qty = int(avail)
            if qty <= 0:
                if self.broker.live_open_qty(sym, "SELL") > 0:
                    P.info(f"{sym}: remaining units are already queued at the "
                           f"broker — counts as handled.")
                continue
            # liquidity slice: never more than 15% of p25 daily value per session
            p25 = self.filters.liquidity_p25(sym, as_of)
            max_qty = int(POS_CAP_LIQ_FRAC * p25 / price) if price > 0 else 0
            qty_now = qty
            if is_exit and max_qty > 0 and qty > max_qty:
                qty_now = max_qty
                remain = qty - qty_now
                exits_outstanding += 1
                dup = self.db.one(
                    "SELECT id FROM pending_trades WHERE status='PENDING' AND symbol=? "
                    "AND signal_as_of=?", (sym, as_of))
                if not dup:
                    self.db.execute(
                        "INSERT INTO pending_trades(signal_as_of,exec_after,symbol,side,"
                        "target_qty,reason,status,created) VALUES(?,?,?,?,?,?,?,?)",
                        (as_of, today_str(), sym, "SELL", remain,
                         "exit sliced across sessions (15% of p25 per day)",
                         "PENDING", now_iso()))
                P.warn(f"SELL {sym}: {qty_now} of {qty} units today (liquidity slice); "
                       f"{remain} queued for the next session.")
            reason_txt = "exit/trim"
            # audit v15 P1 (2026-10-30): a held name being sold because it fell
            # out of today's top-N is a ROTATION, not a trend break — say which
            # (the 'why' column used to read 'exit/trim' for both, so the live
            # console looked like it was selling healthy trends for no reason).
            _trend_exit_syms = {st["symbol"] for st in selection["exits"]}
            if is_exit and sym not in _trend_exit_syms:
                _qtr, _qa, _qv = _quick_trend(self.db, sym)
                if _qtr is not None and _qtr > EXIT_THRESHOLD and _qa is not False:
                    reason_txt += (" | rotation: momentum fine "
                                   f"({fmt_pct(_qtr)}) but ranked out of the "
                                   f"top {MAX_HOLDINGS} — sells to follow the basket")
            if planned_px.get(sym):
                short = (price / planned_px[sym] - 1.0)
                reason_txt += f" | late-fill shortfall vs planned session: {fmt_pct(short)}"
            res = self.broker.execute(sym, "SELL", qty_now, price, as_of,
                                      reason=reason_txt,
                                      spread_bps=tgt["spread_bps"])
            self.broker.print_order_ticket(res, sym, tgt["name"], "SELL")
            # audit v15 P0-3: failed/rejected exits keep the signal OPEN
            if is_exit and res.get("status") in ("FAILED", "REJECTED"):
                exits_failed.append(sym)
            trades_done += 1
        # No rebalance buy loop: rotation exits above, then deploy_day()
        # spends the shared daily SIP allowance. This also holds at zero budget.
        if trades_done == 0:
            if SIP_MODE:
                # user run 2026-10-27 #2: on a fresh book this section printed
                # "already matches the plan" while invested 0% vs a 92% plan —
                # technically true for exits, misleading as a whole
                P.info("No exits to execute. Any eligible purchase is handled only "
                       "by the capped daily SIP path below.")
            else:
                P.info("No orders needed — portfolio already matches the plan.")

        # ---- snapshots ----
        cash = self.broker.cash()
        hv = self.broker.holdings_value()
        account_eq = cash + hv
        self.risk.update(account_eq, "cycle-end")
        self.risk.snapshot(today_str(), cash, hv, account_eq)
        if signal_closeable(exits_outstanding, exits_failed):
            # audit v4 L4: a re-derived (override) basket must ALSO satisfy the
            # current month-end key, else the next run re-executes the STALE
            # month-end signal and clobbers the fresh plan (loop forever).
            # audit v15 P0-3: a FAILED/REJECTED exit must ALSO keep the signal
            # open — the previous run book only watched sliced/queued exits,
            # so a broker error silently sealed the cycle "up to date".
            _done = as_of
            if as_of_override:
                _done = last_month_end_date(all_dates) if all_dates else as_of
            self.db.meta_set("last_trade_signal", _done)
        else:
            _bits = []
            if exits_outstanding:
                _bits.append(f"{exits_outstanding} exit order(s) are still "
                             f"sliced/queued")
            if exits_failed:
                _bits.append(f"{len(exits_failed)} exit(s) FAILED or were "
                             f"rejected ({', '.join(exits_failed)}) — fix the "
                             f"cause (see the order tickets above; EDIS/DDPI "
                             f"setup is the usual one on Upstox sells)")
            P.warn(f"{'; '.join(_bits)} — the {as_of} signal stays OPEN and "
                   f"retries until done.  Rerun 'python etf-momentum-screener.py "
                   f"trade' on the next session.")
        self.db.meta_set("last_trade_run", now_iso())
        # audit v11 P2-1: this is the state after the month-end SELLS — the
        # daily buying runs later (deploy_day under run_daily).  Title it so.
        P.section("UPSTOX ETF PORTFOLIO AFTER REBALANCE (before today's SIP)",
                  "\U0001F4CA")
        rows = []
        for sym, h in sorted(self.broker.holdings().items()):
            px = h.get("last_price", 0.0)
            val = h["qty"] * px
            pnl = (px - h["avg_cost"]) * h["qty"]
            nm = universe_map().get(sym, {}).get("name", sym)[:24]
            rows.append([sym, nm, fmt_num(h["qty"], 0), f"Rs {h['avg_cost']:.2f}",
                         f"Rs {px:.2f}", fmt_cr(val), fmt_pct(safe_div(pnl, h["avg_cost"] * h["qty"], 0.0))])
        if rows:
            P.table(["Symbol", "Name", "Units", "Avg Cost", "LTP", "Value", "P&L"], rows,
                    aligns=["<", "<", ">", ">", ">", ">", ">"])
        P.kv("Cash in account", fmt_cr(cash))
        P.kv("ETFs you hold (worth now)", fmt_cr(hv))
        P.kv("ETF portfolio market value", fmt_cr(hv), val_color=_C.GREEN)
        P.kv("Account invested fraction", fmt_pct(safe_div(hv, account_eq, 0.0)))
        dd = 1 - account_eq / self.risk.peak if self.risk.peak else 0.0
        P.kv("Drawdown from peak", fmt_pct(dd, signed=False),
             val_color=_C.RED if dd > 0.05 else _C.WHITE)
        P.kv("Risk state", self.risk.state)
        log(f"rebalance done: portfolio={hv:.2f} cash={cash:.2f} trades={trades_done}")
        return hv


# ==============================================================================
# SECTION 20 — STATUS / REPORT COMMANDS
# ==============================================================================

def cmd_status(db: Db):
    P.section("UPSTOX ETF PORTFOLIO / LIVE SIP", "\U0001F4B0")
    try:
        client = UpstoxClient(load_env())
    except Exception as e:
        P.err(f"Cannot load Upstox credentials: {e}")
        return
    broker = Broker(db, TaxEngine(db), client)
    if not broker.sync_live_book():
        P.err("Portfolio unavailable: refusing to present a local mirror as broker truth.")
        return
    risk = RiskEngine(db)
    cash, hv = broker.cash(), broker.holdings_value()
    account_eq = cash + hv
    P.kv("Trading mode", "LIVE — Upstox delivery holdings")
    P.kv("ETF portfolio market value", fmt_cr(hv), val_color=_C.GREEN)
    P.kv("Available broker cash (excluded)", fmt_cr(cash))
    P.kv("Daily SIP cap", fmt_rs(INVEST_PER_DAY))
    P.kv("Filled + reserved today", fmt_rs(broker.sip_committed()))
    P.kv("Unused SIP allowance", fmt_rs(broker.sip_remaining()))
    P.kv("Daily exit guard", "ON")
    P.kv("Account equity (risk only)", fmt_cr(account_eq))
    P.kv("Account peak (risk only)", fmt_cr(risk.peak))
    dd = max(1 - account_eq / risk.peak, 0.0) if risk.peak else 0.0
    P.kv("Account drawdown", fmt_pct(dd, signed=False), val_color=_C.RED if dd > 0.05 else _C.WHITE)
    P.kv("Risk state / last evaluated", f"{risk.state} / {db.meta_get('risk_last_eval', 'not yet')}")
    P.note("Portfolio = Upstox ETF quantity x broker market price. Cash and non-universe "
           "assets are excluded. Status does not move risk peaks or place orders.")
    rows = []
    for sym, h in sorted(broker.holdings().items()):
        px = h.get("last_price", 0.0)
        rows.append([sym, universe_map().get(sym, {}).get("name", sym)[:26],
                     fmt_num(h["qty"], 0), f"Rs {h['avg_cost']:.2f}", f"Rs {px:.2f}",
                     fmt_cr(h["qty"] * px), fmt_cr((px - h["avg_cost"]) * h["qty"])])
    if rows:
        P.sub("Open Upstox holdings")
        P.table(["Symbol", "Name", "Units", "Avg Cost", "Broker LTP", "Value", "Unrealised P&L"],
                rows, aligns=["<", "<", ">", ">", ">", ">", ">"])
    else:
        P.info("No ETF holdings yet. The first positive SIP allowance can start the portfolio.")
    P.sub("Recent live orders")
    orows = []
    for r in db.query("SELECT * FROM orders WHERE mode='LIVE' ORDER BY id DESC LIMIT 15"):
        orows.append([r["exec_date"], r["symbol"], r["side"], fmt_num(r["qty"], 0),
                      f"Rs {(r['price'] or 0):.2f}", fmt_cr(r["value"] or 0), r["status"],
                      (r["reason"] or "")[:42]])
    if orows:
        P.table(["Date", "Symbol", "Side", "Qty", "Price", "Filled value", "Status", "Why"],
                orows, aligns=["<", "<", "<", ">", ">", ">", "<", "<"])
    else:
        P.info("No live orders journaled yet.")
    tax = TaxEngine(db).year_tax_summary()
    if tax:
        P.sub("Tax summary by financial year")
        trows = [[fy, fmt_cr(d["stcg"]), fmt_cr(d["ltcg"]), fmt_cr(d.get("tax", 0))]
                 for fy, d in sorted(tax.items())]
        P.table(["FY", "STCG realised", "LTCG realised", "Tax accrued"], trows,
                aligns=["<", ">", ">", ">"])
        P.note(f"LTCG exemption Rs 1.25 L/yr applied | STCG {fmt_pct(STCG_RATE,0)} | "
               f"LTCG {fmt_pct(LTCG_RATE,2)} above exemption")
        # audit v10 (external lever 5): passive March harvest reminder.  Never an
        # automatic order — selling just for tax would churn the book; you decide
        # which long-term seat to lift and rebuy.
        _fy_now = financial_year(parse_date(today_str()))
        if parse_date(today_str()).month == 3 and _fy_now in tax:
            _room = LTCG_EXEMPTION - tax[_fy_now].get("ltcg", 0.0)
            if _room > 1000:
                P.note(f"LTCG harvest window: {fmt_cr(_room)} of the Rs 1.25 L "
                       f"long-term exemption is still unused this FY.  Any holding "
                       f"bought 12+ months ago and sitting on a profit can be SOLD "
                       f"AND RE-BOUGHT before 31-Mar to book that gain tax-free "
                       f"(losses realised in the same FY also net off).  This is a "
                       f"reminder, not an order — the engine never sells for tax "
                       f"on its own.")
    # discovery dates recap — one line; the full table lives under 'scan'
    dcnt = db.one("SELECT COUNT(*) AS c, MIN(discovered_on) AS f, "
                  "MAX(discovered_on) AS l FROM discovery_log")
    if dcnt and dcnt["c"]:
        P.kv("Discovery dates journaled",
             f"{dcnt['c']} funds (earliest {dcnt['f']}, latest {dcnt['l']})")
        P.note("Full discovery log:  python etf-momentum-screener.py scan")
    print_action_panel(db)


def _quick_trend(db: Db, symbol: str):
    """Indicative trend read for the action panel: (momentum, above_200dma, vol).

    Uses the same signal_momentum() as production on the newest ~300 bars.
    Light-weight: no liquidity/integrity checks — the binding signal and all
    gates are computed at the month-end rebalance."""
    rows = db.query("SELECT adj_close FROM price_daily WHERE symbol=? "
                    "ORDER BY date DESC LIMIT 300", (symbol,))
    if not rows:
        return None, None, None
    adj = [r["adj_close"] for r in reversed(rows)]
    mom = signal_momentum(adj, len(adj) - 1)
    above = None
    if len(adj) >= 200:
        seg = adj[-200:]
        above = adj[-1] >= sum(seg) / len(seg)
    vol = annualized_vol(adj, VOL_WINDOW) if len(adj) >= VOL_WINDOW + 1 else None
    return mom, above, vol


# Same idea for the annualized-vol print ( ceilings per category; display-only).
# Seen live 2026-10-27: MONQ50 printed +121% p.a. — no index fund of that
# category truly runs triple-digit vol; the spliced series inflates it.
VOL_PLAUSIBILITY = {"DEBT": 0.15, "Hybrid": 0.40, "GLOBAL INDICES": 0.50,
                    "COMMODITY": 0.60, "EQUITY": 0.75}


def vol_plausibility(symbol: str, vol) -> str:
    """'' when the annualized-vol print is plausible; a short flag otherwise."""
    if vol is None:
        return ""
    cat = universe_map().get(symbol, {}).get("category", "")
    cap = VOL_PLAUSIBILITY.get(cat)
    if cap is not None and vol > cap:
        # audit v8: premium swings on SEBI-frozen global ETFs inflate vol too
        if cat == "GLOBAL INDICES":
            return (f"high vol for its category (>{fmt_pct(cap, 0)} p.a.) — the "
                    f"SEBI-freeze premium swinging; sizing already shrinks it")
        return (f"IMPLAUSIBLE vol for its category (>{fmt_pct(cap, 0)} p.a.) — "
                f"verify adjusted data")
    return ""


def print_action_panel(db: Db):
    """Facts and next actions after a successful broker-backed status read."""
    try:
        P.section("SIP SUMMARY / NEXT ACTIONS", "\U0001F9ED")
        today = today_str()
        rows = db.query("SELECT side,status,COUNT(*) AS n,COALESCE(SUM(value),0) AS v "
                        "FROM orders WHERE exec_date=? AND mode='LIVE' GROUP BY side,status", (today,))
        if rows:
            for r in rows:
                P.info(f"{r['side']} {r['status']}: {r['n']} order(s), confirmed value {fmt_cr(r['v'])}")
        else:
            P.info("No live orders journaled today.")
        broker = Broker(db, TaxEngine(db))
        P.kv("SIP filled + reserved today", fmt_rs(broker.sip_committed()))
        P.kv("Unused allowance", fmt_rs(broker.sip_remaining()))
        basket = [x.strip() for x in (db.meta_get(SIP_BASKET_KEY, "") or "").split(",") if x.strip()]
        P.kv("Current SIP basket", ", ".join(basket) or "(not selected yet)")
        risky = []
        for r in db.query("SELECT symbol FROM positions WHERE qty>0"):
            if r["symbol"] in CASH_PROXY_SYMBOLS:
                continue
            mom, above, _ = _quick_trend(db, r["symbol"])
            if (mom is not None and mom <= EXIT_THRESHOLD) or above is False:
                risky.append(r["symbol"])
        if risky:
            P.warn("Exit rules currently flag: " + ", ".join(risky)
                   + ". DAILY_EXIT_GUARD is ON; the next confirmed live run checks/sells them.")
        else:
            P.ok("No held strategy ETF currently flags a momentum/DMA exit.")
        P.info(f"Next monthly selection: after {next_month_end_hint()}; stale lists refresh before buying.")
        if INVEST_PER_DAY <= 0:
            P.info("Buying is paused (INVEST_PER_DAY=0). Sells and rebalancing remain active.")
        else:
            P.info("Run again any time: only the unused daily allowance can buy. "
                   "Unconfirmed orders keep their reservation until reconciled.")
        n_nav = db.one("SELECT COUNT(*) AS c FROM nav_data")["c"]
        if not n_nav:
            P.warn("No NAV data: premium protection lacks a reference. Run load-data --fetch-nav.")
    except Exception as e:
        P.note(f"Action summary unavailable: {e}")


def cmd_report(db: Db):
    cmd_status(db)
    P.section("RISK EVENT HISTORY", "\U0001F6E1")
    rrows = []
    for r in db.query("SELECT * FROM risk_events ORDER BY id DESC LIMIT 20"):
        rrows.append([r["ts"], r["level"], fmt_pct(r["drawdown"]), r["action"], (r["detail"] or "")[:40]])
    if rrows:
        P.table(["Time", "Level", "Drawdown", "Action", "Detail"], rrows,
                aligns=["<", "<", ">", "<", "<"])
    else:
        P.info("No risk triggers recorded yet.")
    P.section("ACCOUNT PERFORMANCE SINCE INCEPTION", "\U0001F4C8")
    P.note("Historical snapshots use account equity (cash + ETFs) for risk/performance; "
           "the current SIP portfolio basis is holdings market value only.")
    snaps = db.query("SELECT * FROM portfolio_snapshots ORDER BY date")
    if len(snaps) >= 2:
        eqs = [s["equity"] for s in snaps]
        rets = [eqs[i] / eqs[i - 1] - 1 for i in range(1, len(eqs)) if eqs[i - 1] > 0]
        days = max((parse_date(snaps[-1]["date"]) - parse_date(snaps[0]["date"])).days, 1)
        years = days / 365.25
        cagr = (eqs[-1] / eqs[0]) ** (1 / max(years, 1e-9)) - 1 if eqs[0] > 0 else 0
        vol = stdev(rets, ddof=1) * math.sqrt(252) if len(rets) > 2 else 0
        peak = eqs[0]; mdd = 0
        for e in eqs:
            peak = max(peak, e)
            mdd = max(mdd, 1 - e / peak)
        P.kv("Snapshots", f"{len(snaps)} days")
        P.kv("Equity start -> now", f"{fmt_cr(eqs[0])} -> {fmt_cr(eqs[-1])}")
        P.kv("CAGR since inception", fmt_pct(cagr))
        P.kv("Annual volatility", fmt_pct(vol))
        P.kv("Max drawdown", fmt_pct(-mdd))
    else:
        P.info("Not enough snapshots yet — run the strategy for a few days.")
    P.note("Performance is measured from live daily portfolio snapshots since "
           "inception; this live-only engine has no offline backtest.")
    save_report(f"report_{TODAY.strftime('%Y%m%d')}.txt",
                f"TSMOM report {now_iso()}\nSee console output / database {DB_PATH}\n")


# ==============================================================================
# SECTION 21 — SELF TESTS (unit tests required by the rulebook)
# ==============================================================================

def cmd_selftest(db: Db):
    P.section("SELF TESTS / UNIT TESTS", "\u2705")
    # Offline tests: every order-path test injects a fake client. No test
    # constructs an authenticated network client or sends a real order.
    passed = failed = 0
    def check(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            P.ok(f"{name} {paint(detail, _C.GREY)}")
        else:
            failed += 1
            P.err(f"{name} {detail}")

    class _FakeLive:
        def __init__(self, hold=None, cash=1000000.0, details=None):
            self.hold, self._cash = hold or [], cash
            self.details = details if details is not None else {"status": "complete", "average_price": 100.0}
            self.placed = []
        def holdings(self): return list(self.hold)
        def positions(self): return []
        def orders_book(self): return []
        def funds_available(self): return self._cash
        def place_order(self, sym, key, side, qty, price, order_type, is_amo=False):
            self.placed.append((sym, key, side, qty, price, order_type, is_amo))
            return {"data": {"order_id": f"FAKE{len(self.placed)}"}}
        def order_details(self, oid):
            if isinstance(self.details, Exception): raise self.details
            d = dict(self.details); d.setdefault("filled_quantity", 0 if d.get("status") == "open" else None)
            if d.get("filled_quantity") is None:
                d["filled_quantity"] = next((x[3] for x in self.placed if x[0]), 0)
            return {"data": d}

    # 1) momentum formula (rulebook unit test: must be non-zero when prices differ)
    # construct explicit series: P(t-252)=1000, P(t-21)=1150
    series = [1000.0] * 231 + [1150.0] + [1150.0] * 21
    m = momentum_at(series, len(series) - 1, 252, 21)
    check("momentum example (1000 -> 1150 = +15%)",
          m is not None and abs(m - 0.15) < 1e-9, f"M={m}")
    series2 = [float(i) for i in range(300)]
    m2 = momentum_at(series2, 299, 252, 21)
    check("signal non-zero whenever prices differ", m2 is not None and m2 != 0.0, f"M={m2:.4f}")
    check("momentum None when history too short", momentum_at([1.0] * 100, 99) is None)

    # 2) thresholds
    check("entry: +3% > +2% -> ELIGIBLE_BUY", decide(0.03, False)[0] == "ELIGIBLE_BUY")
    check("entry: +1% -> CASH", decide(0.01, False)[0] == "CASH")
    check("hold zone: +1% while holding -> HOLD", decide(0.01, True)[0] == "HOLD")
    check("exit: -1% while holding -> EXIT", decide(-0.01, True)[0] == "EXIT")
    check("exit boundary: 0% while holding -> EXIT", decide(0.0, True)[0] == "EXIT")

    # 3) Ledoit-Wolf covariance
    rng = random.Random(1)
    X = [[rng.gauss(0, 0.01), rng.gauss(0, 0.02)] for _ in range(60)]
    Xd = demean(X)
    S, delta, cond = ledoit_wolf_cc(Xd)
    diag_ok = S[0][0] > 0 and S[1][1] > 0
    sym_ok = abs(S[0][1] - S[1][0]) < 1e-12
    check("LW covariance positive diagonal & symmetric", diag_ok and sym_ok,
          f"delta={delta:.3f} cond={cond:.2f}")
    check("LW shrinkage intensity within [0,1]", 0.0 <= delta <= 1.0)

    # 4) vol targeting caps: never leverage
    chosen = [{"symbol": "X", "vol": 0.20, "liq_p25": 1e12, "adj": [100.0 * (1 + rng.gauss(0, 0.01)) for _ in range(80)]},
              {"symbol": "Y", "vol": 0.30, "liq_p25": 1e12, "adj": [100.0 * (1 + rng.gauss(0, 0.01)) for _ in range(80)]}]
    fe = FilterEngine(db)
    pb = PortfolioBuilder(db, fe)
    sized = pb.size(chosen, 1_000_000.0)
    check("sizing: invested fraction <= 1", sized["invested"] <= 1.0 + 1e-9,
          f"invested={sized['invested']:.3f}")
    check("sizing: port vol <= target (scaled, annualised)", sized["port_vol"] <= VOL_TARGET + 1e-6,
          f"vol={sized['port_vol']:.4f}")
    check("sizing: reported vol is ANNUALISED (not daily)", sized["port_vol"] > 0.001,
          f"vol={sized['port_vol']:.4f} (a daily vol of 3 assets would be < 0.001)")

    # 5) charges math
    ch = calc_charges("BUY", 100_000.0)
    check("charges: GST applied on brokerage+exchange+sebi", ch["gst"] > 0,
          f"total={ch['total']:.2f} on Rs 1L")
    check("charges: round-trip friction in 0.01%-0.1% band",
          0.0001 <= (ch["total"] / 100_000.0) <= 0.001,
          f"{ch['total']/1000:.4f}%")

    # 6) slippage direction
    b = slippage_price("BUY", 100.0, 10.0)
    s = slippage_price("SELL", 100.0, 10.0)
    check("slippage: buy above ref, sell below ref", b > 100.0 > s, f"buy={b:.3f} sell={s:.3f}")

    # 7) tax lots (offline test DB)
    taxdb = Db(DATA_DIR / "selftest_tax.db")
    te = TaxEngine(taxdb)
    te.record_buy("TEST", "2025-01-01", 100.0, 10)
    out = te.record_sell("TEST", "2026-02-01", 130.0, 10)   # >12 months -> LTCG
    check("tax lots: >12m classified LTCG", out["lt_pnl"] > 0 and out["st_pnl"] == 0,
          f"lt={out['lt_pnl']:.0f}")
    te.record_buy("TEST", "2026-01-01", 100.0, 5)
    out2 = te.record_sell("TEST", "2026-03-01", 90.0, 5)    # <=12m -> STCG
    check("tax lots: <=12m classified STCG", out2["st_pnl"] < 0,
          f"st={out2['st_pnl']:.0f}")
    taxdb.close()
    try:
        os.remove(DATA_DIR / "selftest_tax.db")
    except Exception:
        pass

    # 8) month-end detection
    me = month_end_indices(["2026-01-01", "2026-01-02", "2026-02-02", "2026-02-03", "2026-03-02"])
    check("month-end detection", me == [1, 3, 4], f"indices={me}")

    # 9) Indian number formatting
    check("INR formatting", fmt_inr(1234567.5) == "12,34,567.50", fmt_inr(1234567.5))

    # 10) portfolio vol of known case
    v = portfolio_vol([0.5, 0.5], [[0.04, 0.0], [0.0, 0.04]])
    check("portfolio vol (equal weights, 20% assets, zero corr)", abs(v - 0.1414) < 1e-3, f"v={v:.4f}")

    # =========== REGRESSION TESTS FOR THE AUDIT FINDINGS ===========
    # A1: month-end must exclude an in-progress / truncated final month
    me = month_end_indices(["2026-08-28", "2026-08-31", "2026-09-01", "2026-09-15"])
    le = last_month_end_date(["2026-08-28", "2026-08-31", "2026-09-01", "2026-09-15"])
    check("audit A1: truncated final month is NOT a signal date", le == "2026-08-31", f"got {le}")
    le2 = last_month_end_date(["2026-08-28", "2026-08-31", "2026-09-30"])
    check("audit A1: complete month-end IS used", le2 == "2026-09-30", f"got {le2}")

    # A7: holding period is an anniversary test, not month subtraction
    check("audit A7: buy 2025-01-20 / sell 2026-01-25 is LTCG",
          held_more_than_months("2025-01-20", "2026-01-25", 12) is True)
    check("audit A7: buy 2025-01-20 / sell 2026-01-15 is STCG",
          held_more_than_months("2025-01-20", "2026-01-15", 12) is False)

    # A7: per-class tax rates
    t_gold_st, _ = tax_on_gain("COMMODITY", "STCG", 10000.0, 0.0)
    check("audit A7: gold STCG taxed at slab (30% + 4% cess)",
          abs(t_gold_st - 3000.0 * 1.04) < 1e-6, f"tax={t_gold_st}")
    t_gold_lt, used = tax_on_gain("COMMODITY", "LTCG", 10000.0, 125000.0)
    check("audit A7: gold LTCG gets NO 1.25L exemption",
          used == 0.0 and abs(t_gold_lt - 1250.0 * 1.04) < 1e-6)
    t_eq_lt, used2 = tax_on_gain("EQUITY", "LTCG", 10000.0, 125000.0)
    check("audit A7: equity LTCG exemption applies", abs(used2 - 10000.0) < 1e-6 and t_eq_lt == 0.0)

    # A8: STT — commodity/debt exempt, no STT on buy
    ch_g = calc_charges("SELL", 100000.0, category="COMMODITY")
    ch_e = calc_charges("SELL", 100000.0, category="EQUITY")
    ch_b = calc_charges("BUY", 100000.0, category="EQUITY")
    check("audit A8: no STT for gold/liquid/gilt", ch_g["stt"] == 0.0)
    check("audit A8: equity ETF STT 0.001% sell only",
          abs(ch_e["stt"] - 1.0) < 1e-9 and ch_b["stt"] == 0.0, f"{ch_e['stt']} / {ch_b['stt']}")

    # v13 #4: demat (DP) charge — flat on SELL only, never on BUY
    check("audit v13: demat (DP) charge applies per SELL order, not on BUY",
          abs(ch_e["dp"] - 15.93) < 1e-9 and ch_b["dp"] == 0.0
          and abs(ch_e["total"] - (ch_e["brokerage"] + ch_e["stt"] + ch_e["stamp"]
                                   + ch_e["exchange"] + ch_e["sebi"] + ch_e["ipft"]
                                   + ch_e["gst"] + 15.93)) < 1e-6,
          f"dp={ch_e['dp']}")

    # v13 #1: instrument-master misses are retried by ISIN (SBIBPB/SBINEQWETF)
    _dbi = Db(DATA_DIR / "selftest_isin.db")
    try:
        _fake_master = [
            {"trading_symbol": "XGARBAGE", "segment": "EQ", "exchange": "NSE",
             "instrument_key": "NSE_EQ|DUMMY1", "isin": "INE000000X1"},
            {"trading_symbol": "SBI ETF BSE PSU BK", "segment": "NSE_EQ",
             "exchange": "NSE", "instrument_key": "NSE_EQ|INF200KB1712",
             "isin": "INF200KB1712"},   # odd master name, right ISIN
        ]
        for _s in ("SBIBPB", "SBINEQWETF"):
            _dbi.execute("INSERT INTO etf_universe(symbol,name,status) "
                         "VALUES (?,?,'ACTIVE')", (_s, _s + " test"))
        _sc = Scanner(_dbi)
        _sc.verify_against_upstox(_fake_master)
        _row = _dbi.query("SELECT instrument_key FROM etf_universe WHERE symbol='SBIBPB'")
        _ok_isin = bool(_row) and (_row[0]["instrument_key"] or "") == "NSE_EQ|INF200KB1712"
        _row2 = _dbi.query("SELECT instrument_key FROM etf_universe WHERE symbol='SBINEQWETF'")
        _ok_noisin = bool(_row2) and not _row2[0]["instrument_key"]
        check("audit v13: ISIN fallback rescues SBIBPB; unknown ISIN stays empty (fail-safe)",
              _ok_isin and _ok_noisin, f"SBIBPB={_row[0]['instrument_key'] if _row else None}")
    finally:
        _dbi.close()
        try:
            os.remove(DATA_DIR / "selftest_isin.db")
        except Exception:
            pass

    # v14 P0/P1 regression tests ------------------------------------------------
    _dbf = Db(DATA_DIR / "selftest_v14.db")
    try:
        _taxf = TaxEngine(_dbf)
        _dbf.meta_set("cash_balance", "1000000")
        for _sym in ("NIFTYBEES", "BANKBEES"):
            _dbf.execute("INSERT OR REPLACE INTO etf_universe(symbol,name,category,index_family,status,instrument_key) "
                         "VALUES(?,?,'EQUITY',?,'ACTIVE',?)", (_sym, _sym, _sym, f"NSE_EQ|{_sym}"))
        _cw = _FakeLive(details=RuntimeError("verification endpoint down"))
        _bf2 = Broker(_dbf, _taxf, client=_cw)
        assert _bf2.sync_live_book()
        _r_un = _bf2.execute("NIFTYBEES", "BUY", 20, 100.0, reason="unverifiable fill")
        check("SIP: accepted but unverifiable order reserves worst-case notional, fabricates no fill",
              _r_un["status"] == "UNCONFIRMED" and not _dbf.one("SELECT qty FROM positions WHERE symbol='NIFTYBEES'")
              and _bf2.cash() == 1000000.0 and abs(_bf2.sip_committed() - 2004.0) < 1e-6)
        _cw2 = _FakeLive(details={"status": "open", "average_price": 0})
        _bf3 = Broker(_dbf, _taxf, client=_cw2)
        assert _bf3.sync_live_book()
        _r_lim = _bf3.execute("BANKBEES", "BUY", 20, 100.0, reason="limit test")
        check("SIP: LIMIT sends the buffered ceiling once and reserves it",
              _r_lim["status"] == "PENDING" and abs(_cw2.placed[0][4] - 100.2) < 1e-9
              and abs(_bf3.sip_committed() - 4008.0) < 1e-6, f"sent={_cw2.placed}")
        # c) sync_live_book is fail-closed when the funds API dies
        class _NoFundsClient:
            def holdings(self): return []
            def positions(self): return []
            def orders_book(self): return []
            def funds_available(self):
                raise RuntimeError("funds API dead")
        _bf4 = Broker(_dbf, _taxf, client=_NoFundsClient())
        check("v14 P0: broker sync fails closed when cash is unreadable "
              "(never trades on a stale wallet)",
              _bf4.sync_live_book() is False and not _bf4.live_book_ok()
              and _bf4.execute("NIFTYBEES", "BUY", 20, 100.0)["status"] == "REJECTED", "")
        # d) partially-filled broker orders count as OPEN (no duplicates)
        check("v14 P1: partially-filled broker orders are treated as open",
              "partially filled" in BROKER_OPEN_STATUSES,
              f"{sorted(BROKER_OPEN_STATUSES)[-3:]}")
        # e) prev_completed_session: Monday's previous completed day is Friday
        check("v14 P1: prev_completed_session(2026-10-26 Mon) = 2026-10-23 (Fri)",
              prev_completed_session("2026-10-26") == "2026-10-23",
              prev_completed_session("2026-10-26"))
        # f) GLOBAL LTCG report uses the configured 12.5%, not the 30% slab
        _dbf.execute("INSERT OR REPLACE INTO etf_universe(symbol,name,category,"
                     "index_family,status) VALUES('MOTEST','g','GLOBAL INDICES',"
                     "'G','ACTIVE')")
        _dbf.execute("INSERT INTO tax_lots(symbol,buy_date,buy_price,qty,"
                     "sell_date,sell_price,pnl,gain_type,tax) "
                     "VALUES('MOTEST','2024-01-01',100,100,'2025-06-01',150,"
                     "5000.0,'LTCG',0)")
        _sum = TaxEngine(_dbf).year_tax_summary()
        _g = _sum.get("2024-2025") or next((v for k, v in _sum.items()
                                            if v["by_class"].get("GLOBAL")), None)
        _gtax = _g["by_class"]["GLOBAL"]["tax"] if _g else None
        check("v14 P2: GLOBAL LTCG reported at the configured 12.5% (+4% cess), "
              "NOT the 30% slab",
              _gtax is not None and abs(_gtax - 5000.0 *
              tax_rules_for("GLOBAL INDICES")["ltcg"] * (1.0 + CESS_RATE)) < 1.0,
              f"tax={_gtax} expect={5000.0 * tax_rules_for('GLOBAL INDICES')['ltcg'] * (1.0 + CESS_RATE)}")
    finally:
        _dbf.close()
        try:
            os.remove(DATA_DIR / "selftest_v14.db")
        except Exception:
            pass

    # v15 P0/P1 regression tests ------------------------------------------------
    # P0-2: the SIP-mode orphan loop must NEVER sell a held name that is in
    # today's chosen basket (it used to sell the rebalance's own fresh picks).
    _tg = {"OLDX": {"weight": 0.0}}          # exits got weight 0 in target
    check("v15 P0-2: held+chosen kept, rotation sells, sleeves never sell",
          sip_orphan_should_sell("HOLDPICK", _tg, {"HOLDPICK"}, True) is False
          and sip_orphan_should_sell("DROPPED", _tg, {"HOLDPICK"}, True) is True
          and sip_orphan_should_sell("LIQUIDCASE", _tg, set(), True) is False
          and sip_orphan_should_sell("OLDX", _tg, set(), True) is False,
          "orphan gate")
    # P0-3: the month-end signal must stay open when any exit failed.
    check("v15 P0-3: failed exits keep the signal open for retry",
          signal_closeable(0, []) is True and signal_closeable(1, []) is False
          and signal_closeable(0, ["PHARMABEES"]) is False, "closeable gate")
    # P0-1: config sanity — the daily budget must be >= one minimum order.
    check("v15 P0-1: INVEST_PER_DAY >= MIN_TRADE_VALUE (else zero buys ever)",
          INVEST_PER_DAY >= MIN_TRADE_VALUE,
          f"INVEST_PER_DAY={INVEST_PER_DAY} MIN_TRADE_VALUE={MIN_TRADE_VALUE}")

    # A8: impact grows with order size (no more flat 2 bps)
    small = impact_bps(10_000.0, 1_000_000.0)
    big = impact_bps(500_000.0, 1_000_000.0)
    check("audit A8: square-root impact rises with participation", big > small,
          f"small={small:.1f}bps big={big:.1f}bps")

    # A6: spread wider than the limit must be rejected
    fe2 = FilterEngine(db)
    db.execute("INSERT INTO quotes(symbol,ts,ltp,bid,ask,spread_bps) VALUES(?,?,?,?,?,?)",
               ("SPREADY", now_iso(), 100.0, 99.0, 101.0, 200.0))
    db.execute("INSERT INTO price_daily(symbol,date,open,high,low,close,adj_close,volume,traded_value) "
               "VALUES(?,?,?,?,?,?,?,?,?)",
               ("SPREADY", today_str(), 100.0, 100.0, 100.0, 100.0, 100.0,
                1e6, 1e8))   # plenty of liquidity so the SPREAD gate is the one that fires
    ok, reason, _info = fe2.check_execution("SPREADY", today_str(), 1000.0)
    check("audit A6: oversized spread is rejected", ok is False and "spread" in reason, reason[:60])
    db.execute("DELETE FROM quotes WHERE symbol='SPREADY'")
    db.execute("DELETE FROM price_daily WHERE symbol='SPREADY'")

    # A3: cash proxies are excluded from the ranked pool
    dbsub = Db(DATA_DIR / "selftest_sel.db")
    fe3 = FilterEngine(dbsub)
    pb3 = PortfolioBuilder(dbsub, fe3)
    mk = lambda sym, mom: {"symbol": sym, "momentum": mom, "vol": 0.15, "liq_p25": 1e8,
                           "rows": [], "adj": [100.0] * 300,
                           "index_family": sym}
    # LIQUIDCASE has the highest momentum but is a cash proxy
    pool = [mk("LIQUIDCASE", 0.50), mk("NIFTYBEES", 0.10), mk("ITBEES", 0.08)]
    sel = pb3.select(pool, set(), equity=1e8)
    chosen_syms = [s["symbol"] for s in sel["chosen"]]
    check("audit A3: liquid-fund cash proxy never wins a slot",
          "LIQUIDCASE" not in chosen_syms and "NIFTYBEES" in chosen_syms,
          f"chosen={chosen_syms}")
    dbsub.close()
    try:
        os.remove(DATA_DIR / "selftest_sel.db")
    except Exception:
        pass

    # A4: illiquid ETF cannot take a top-3 slot (pre-ranking filter)
    dbsub2 = Db(DATA_DIR / "selftest_liq.db")
    fe4 = FilterEngine(dbsub2)
    pb4 = PortfolioBuilder(dbsub2, fe4)
    pool2 = [mk("THINETF", 0.60), mk("NIFTYBEES", 0.10), mk("ITBEES", 0.08)]
    pool2[0]["liq_p25"] = 1000.0        # absurdly illiquid
    sel2 = pb4.select(pool2, set(), equity=1e8)
    check("audit A4: illiquid ETF is filtered BEFORE ranking",
          all(s["symbol"] != "THINETF" for s in sel2["chosen"]),
          f"chosen={[s['symbol'] for s in sel2['chosen']]}")
    dbsub2.close()
    try:
        os.remove(DATA_DIR / "selftest_liq.db")
    except Exception:
        pass

    # A6: iNAV premium/discount gate actually rejects when NAV data exists
    fe5 = FilterEngine(db)
    db.execute("INSERT INTO price_daily(symbol,date,open,high,low,close,adj_close,volume,traded_value) "
               "VALUES(?,?,?,?,?,?,?,?,?)",
               ("NAVER", today_str(), 102.0, 102.0, 102.0, 102.0, 102.0, 1e6, 1e8))
    db.execute("INSERT INTO nav_data(symbol,date,nav) VALUES(?,?,?)",
               ("NAVER", today_str(), 100.0))     # 2% premium -> way beyond 0.40/0.75%
    ok5, reason5, _i5 = fe5.check_execution("NAVER", today_str(), 1000.0)
    check("audit A6: iNAV premium beyond band is rejected",
          ok5 is False and "iNAV" in reason5, reason5[:60])
    db.execute("DELETE FROM price_daily WHERE symbol='NAVER'")

    # N1: iNAV reference is the latest NAV on/before the date (<= 4 days old)
    db.execute("INSERT INTO price_daily(symbol,date,open,high,low,close,adj_close,volume,traded_value) "
               "VALUES(?,?,?,?,?,?,?,?,?)",
               ("NAVER", today_str(), 102.0, 102.0, 102.0, 102.0, 102.0, 1e6, 1e8))
    db.execute("INSERT INTO nav_data(symbol,date,nav) VALUES(?,?,?)",
               ("NAVER", date_str(parse_date(today_str()) - dt.timedelta(days=2)), 100.0))
    dev_late, _gap_late = fe5.inav_deviation("NAVER", today_str())
    check("N1: iNAV uses latest NAV within the staleness window",
          dev_late is not None and dev_late > 0.005, str(dev_late))
    db.execute("DELETE FROM nav_data WHERE symbol='NAVER'")
    db.execute("INSERT INTO nav_data(symbol,date,nav) VALUES(?,?,?)",
               ("NAVER", date_str(parse_date(today_str()) - dt.timedelta(days=10)), 100.0))
    check("N1: NAV older than NAV_MAX_STALE_DAYS is not trusted",
          fe5.inav_deviation("NAVER", today_str())[0] is None)
    db.execute("DELETE FROM nav_data WHERE symbol='NAVER'")
    db.execute("DELETE FROM price_daily WHERE symbol='NAVER'")

    # N2: NAV auto-create parsers (fixtures — no network)
    sample_navall = (
        "Mutual Fund India\r\n"
        "Open Ended Schemes(Exchange Traded Funds)\r\n"
        "Nippon India Mutual Fund\r\n"
        "102323;INF769K01010;-;Nippon India ETF Nifty BeES;280.670000;-;-;27-Oct-2026\r\n"
        "a category line with no fields\r\n"
        "999999;-;-;No ISIN scheme;12.34;-;-;27-Oct-2026\r\n"
        "888888;INF1111111111;-;Broken NAV;N.A.;-;-;27-Oct-2026\r\n")
    am = parse_amfi_navall(sample_navall)
    check("N2: NAVAll parser maps ISIN -> (scheme code, NAV, date)",
          len(am) == 1 and am["INF769K01010"]["code"] == "102323"
          and am["INF769K01010"]["date"] == "2026-10-27"
          and abs(am["INF769K01010"]["nav"] - 280.67) < 1e-9, str(am)[:80])
    # N2b: AMFI's NEW 8-column layout (Plan/Option inserted; the exact format of
    # the user's 2026-10-28 NAVAll download) must parse — the old parser read
    # "Direct Plan" as the NAV and produced 0 rows
    sample_navall_new = (
        "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;"
        "Scheme Name;Plan;Option;Net Asset Value;Date\r\n\r\n"
        "Open Ended Schemes(Other Scheme - ETF)\r\n\r\n"
        "Nippon India Mutual Fund\r\n\r\n"
        "102323;INF769K01010;-;Nippon India ETF Nifty BeES;Direct Plan;"
        "Growth Option;280.670000;27-Oct-2026\r\n"
        "102324;INF769K01011;-;No numeric NAV;Direct Plan;;N.A.;27-Oct-2026\r\n")
    am2 = parse_amfi_navall(sample_navall_new)
    check("N2b: NAVAll parser handles the new Plan/Option columns",
          len(am2) == 1 and abs(am2["INF769K01010"]["nav"] - 280.67) < 1e-9
          and am2["INF769K01010"]["code"] == "102323", str(am2)[:120])
    # N2c: the built-in scheme map covers every active ETF in the universe
    _act = {r["symbol"] for r in db.query(
        "SELECT symbol FROM etf_universe WHERE status='ACTIVE'")}
    _miss = sorted(s for s in _act if s not in NAV_SCHEME_CODES)
    check("N2c: built-in NAV scheme codes cover the whole active universe",
          not _miss and len(NAV_SCHEME_CODES) >= 100,
          f"missing={_miss[:8]} n_map={len(NAV_SCHEME_CODES)} active={len(_act)}")
    # N2e: user overlay file (stock-data/nav_urls.txt style) adds and overrides
    _f2 = DATA_DIR / "selftest_nav_urls.txt"
    _f2.write_text("# comment line\nTESTETF;999001;INF123400001;Test Fund;"
                   "https://api.mfapi.in/mf/999001;https://api.mfapi.in/mf/999001/latest\n"
                   "NIFTYBEES;999999\n")
    _eff = nav_scheme_codes_effective(path=_f2)
    check("N2e: stock-data nav_urls.txt overlay adds/overrides scheme codes",
          _eff.get("TESTETF") == "999001" and _eff.get("NIFTYBEES") == "999999"
          and _eff.get("GOLDBEES") == NAV_SCHEME_CODES.get("GOLDBEES"),
          f"TESTETF={_eff.get('TESTETF')} NIFTYBEES={_eff.get('NIFTYBEES')}")
    _f2.unlink()
    # N2d: the daily 'ok' lock must not count a thin-coverage day as success
    _ntmp = DATA_DIR / "selftest_navlock"
    _ntmp.mkdir(exist_ok=True)
    try:
        db.execute("INSERT OR REPLACE INTO etf_universe(symbol,name,status) VALUES('NAVLCKT','NAV lock test','ACTIVE')")
        db.meta_set("nav_autofetch_ok", today_str())
        _locked1 = not _nav_retry_allowed(db, folder=_ntmp)   # empty folder: lock set, must STILL retry
        _syms = [r["symbol"] for r in db.query(
            "SELECT symbol FROM etf_universe WHERE status='ACTIVE'")]
        for _s in _syms:
            (_ntmp / f"{_s}.csv").write_text("symbol,date,nav\nX,2026-01-01,1.0\n")
        _locked2 = not _nav_retry_allowed(db, folder=_ntmp)   # full coverage -> locked
        check("N2d: NAV ok-lock counts only full-coverage success",
              _locked1 is False and _locked2 is True,
              f"thin_retry_allowed={not _locked1} full_locked={_locked2}")
    finally:
        db.execute("DELETE FROM etf_universe WHERE symbol='NAVLCKT'")
        db.meta_set("nav_autofetch_ok", "")
        for _p in _ntmp.iterdir():
            _p.unlink()
        _ntmp.rmdir()
    hist = parse_mfapi_history(
        {"data": [{"date": "28-10-2026", "nav": "281.5"},
                  {"date": "26-10-2026", "nav": "279.0"},
                  {"date": "26-10-2026", "nav": "279.5"},
                  {"date": "25-10-2026", "nav": "junk"}]}, "2020-01-01")
    check("N2: mfapi parser -> ascending, de-duplicated, junk dropped",
          hist == [("2026-10-26", 279.5), ("2026-10-28", 281.5)], str(hist))
    check("N2: instrument_key -> ISIN extraction",
          isin_of("NSE_EQ|INF769K01010") == "INF769K01010"
          and isin_of("NSE_EQ|NIFTYBEES") == "" and isin_of("") == "")

    # N3: ensure_nav_csvs end-to-end with stubbed HTTP into a temp folder,
    #     then load_nav_data(folder=...) ingests it
    navtmp = DATA_DIR / "selftest_navdir"
    dbn = Db(DATA_DIR / "selftest_nav.db")
    dbn.execute("INSERT INTO etf_universe(symbol,name,instrument_key,status) "
                "VALUES('NVTST','Test Nifty BeES','NSE_EQ|INF769K01010','ACTIVE')")
    fake_get = lambda url, timeout=25: (
        (sample_navall, "") if "amfiindia" in url else
        (json.dumps({"data": [{"date": "26-10-2026", "nav": "279.0"}]}), "")
        if "mfapi.in/mf/102323" in url else
        ("", "offline"))
    res_nav = ensure_nav_csvs(dbn, folder=navtmp, force=True, http_get=fake_get)
    files_ok = (res_nav["files"] == 1 and (navtmp / "NVTST.csv").exists())
    n_loaded = DataManager(dbn, None).load_nav_data(folder=navtmp)
    row_nav = dbn.one("SELECT nav FROM nav_data WHERE symbol='NVTST' "
                      "ORDER BY date DESC LIMIT 1")
    check("N3: auto-create writes nav CSVs and the loader ingests them",
          files_ok and n_loaded >= 2 and row_nav is not None
          and row_nav["nav"] > 200, f"files={res_nav.get('files')} loaded={n_loaded}")

    # N4: AMFI dead -> conservative mfapi name-search fallback + diagnostics;
    #     ambiguous search results are REFUSED (a wrong NAV is worse than none)
    dbn.execute("INSERT INTO etf_universe(symbol,name,instrument_key,status) "
                "VALUES('NVTST2','Qorn Test Nasdaq Fifty','NSE_EQ|NVTST2','ACTIVE')")
    fake2 = lambda url, timeout=25: (
        ("", "URLError: <urlopen error 403 Forbidden>") if "amfiindia" in url else
        (json.dumps([{"schemeCode": 654321,
                      "schemeName": "Qorn Test Nasdaq Fifty ETF Direct Growth"}]), "")
        if "/search?" in url else
        (json.dumps({"data": [{"date": "26-10-2026", "nav": "15.25"}]}), "")
        if "mfapi.in/mf/654321" in url else
        ("", "offline"))
    res2 = ensure_nav_csvs(dbn, folder=navtmp, http_get=fake2)
    map2 = json.loads((Path(navtmp) / "_nav_map.json").read_text())
    check("N4: AMFI down -> search fallback resolves + diagnostics kept",
          res2["files"] == 1 and res2["via_search"] == 1
          and res2["diag"] and "403" in res2["diag"]
          and map2.get("NVTST2", {}).get("source") == "mfapi-search",
          f"{res2} / {map2.get('NVTST2')}")
    dbn.execute("INSERT INTO etf_universe(symbol,name,instrument_key,status) "
                "VALUES('NVTST3','Qorn Test Nasdaq Fifty','NSE_EQ|NVTST3','ACTIVE')")
    fake3 = lambda url, timeout=25: (
        ("", "offline") if "amfiindia" in url else
        (json.dumps([{"schemeCode": 1, "schemeName": "Qorn Test Nasdaq Fifty ETF A"},
                     {"schemeCode": 2, "schemeName": "Qorn Test Nasdaq Fifty ETF B"}]), "")
        if "/search?" in url else ("", "offline"))
    res3 = ensure_nav_csvs(dbn, folder=navtmp, http_get=fake3)
    check("N4: ambiguous search result is refused (no wrong mapping)",
          not (Path(navtmp) / "NVTST3.csv").exists() and "NVTST3" in res3["failed"],
          str(res3)[:120])
    dbn.close()

    # N5: Groww-first pipeline — ONE bulk call resolves symbols; the mirror
    #     history backfill is throttled and ABORTS after NAV_MAX_429 consecutive
    #     HTTP 429s (user's 2026-10-27 box hit exactly that wall), always keeping
    #     the latest-NAV row that was written first.
    _s1, _s2 = globals()["NAV_CALL_SLEEP_SEC"], globals()["HTTP_SLEEP"]
    globals()["NAV_CALL_SLEEP_SEC"], globals()["HTTP_SLEEP"] = 0.0, 0.0
    navtmp2 = DATA_DIR / "selftest_navdir2"
    dbn2 = Db(DATA_DIR / "selftest_nav2.db")
    for srow in [("NVTSTG", "Test Nifty BeES", "NSE_EQ|INF769K01010"),
                 ("NVTSTN", "Qorn Test Nasdaq Fifty", "NSE_EQ|NVTSTN"),
                 ("NVTSTX", "Xrate Gold Fund", "NSE_EQ|INFXRATE0001"),
                 ("NVTSTY", "Xrate Silver Mint", "NSE_EQ|INFXRATE0002")]:
        dbn2.execute("INSERT INTO etf_universe(symbol,name,instrument_key,status) "
                     "VALUES(?,?,?,'ACTIVE')", srow)
    groww_csv = (
        "scheme_code,isin,scheme_name,nav,date\n"
        "777001,INF769K01010,Test Nifty BeES ETF,281.75,2026-10-27\n"
        "777002,INFQ0RNAV002,Qorn Test Nasdaq Fifty ETF Direct Growth,15.6,2026-10-27\n"
        "777003,INFXRATE0001,Xrate Gold Fund ETF,62.2,2026-10-27\n"
        "777004,INFXRATE0002,Xrate Silver Mint ETF,77.1,26-10-2026\n"
        + "\n".join(f"9{i:05d},INF{i:09d},Padding Scheme {i} ETF,10.0,2026-10-27"
                    for i in range(1, MIN_GROWW_ROWS + 2)) + "\n")
    gi, gn = parse_groww_navall(groww_csv)
    check("N5: Groww parser — ISIN + name maps, DD-MM-YYYY date tolerated",
          gi is not None and gi["INF769K01010"]["code"] == "777001"
          and gi["INFXRATE0002"]["date"] == "2026-10-26"
          and "qorn test nasdaq fifty etf direct growth" in gn,
          str((gi or {}).get("INF769K01010")))
    check("N5: Groww parser rejects HTML stubs and tiny bodies",
          parse_groww_navall("<html><body>blocked</body></html>") == (None, None)
          and parse_groww_navall("scheme_code,isin,scheme_name,nav,date\n"
                                 "1,INF769K01010,X,10.0,2026-10-27\n") == (None, None))
    hist_calls = {"n": 0}

    def fake4(url, timeout=25):
        if "groww.in" in url:
            return groww_csv, ""
        if "mfapi.in/mf/" in url and "/search" not in url:
            hist_calls["n"] += 1
            return "", "HTTPError: 429 Client Error: Too Many Requests"
        return "", "offline"
    res4 = ensure_nav_csvs(dbn2, folder=navtmp2, force=True, http_get=fake4)
    map4 = json.loads((Path(navtmp2) / "_nav_map.json").read_text())
    g_rows = [ln for ln in (Path(navtmp2) / "NVTSTG.csv").read_text().splitlines()][1:]
    check("N5: Groww bulk resolves all 4 symbols (isin + name), 1-GET path",
          res4["files"] == 4 and res4["via_groww"] == 4 and res4["via_search"] == 0
          and map4["NVTSTN"]["source"] == "groww-name"
          and map4["NVTSTG"]["source"] == "groww-isin"
          and abs(float(g_rows[0].split(",")[2]) - 281.75) < 1e-9,
          f"{res4.get('files')}/{res4.get('todo')} via_groww={res4.get('via_groww')}")
    check("N5: HTTP-429 wall aborts history backfill but keeps latest-NAV rows",
          hist_calls["n"] == NAV_MAX_429 and "429" in res4["diag"]
          and (Path(navtmp2) / "NVTSTY.csv").exists()
          and len((Path(navtmp2) / "NVTSTY.csv").read_text().splitlines()) == 2,
          f"hist_calls={hist_calls['n']} diag={res4['diag'][:90]}")
    globals()["NAV_CALL_SLEEP_SEC"], globals()["HTTP_SLEEP"] = _s1, _s2

    # N6: cumulative broker fills and rejected unfilled orders reconcile once.
    dbnp = Db(DATA_DIR / "selftest_pend.db")
    try:
        dbnp.execute("INSERT OR REPLACE INTO etf_universe(symbol,name,status,instrument_key) "
                     "VALUES('NIFTYBEES','NB','ACTIVE','NSE_EQ|NIFTYBEES')")
        client_np = _FakeLive(details={"status": "open", "average_price": 0, "filled_quantity": 0})
        brkp = Broker(dbnp, TaxEngine(dbnp), client_np)
        assert brkp.sync_live_book()
        brkp.execute("NIFTYBEES", "BUY", 30, 100.0, reason="pending test")
        rownp = dbnp.one("SELECT * FROM orders WHERE id=(SELECT MAX(id) FROM orders)")
        client_np.details = {"status": "complete", "average_price": 101.5, "filled_quantity": 30}
        n_done = resolve_pending_live_orders(dbnp, brkp)
        st_ok = dbnp.one("SELECT status,price,filled_qty,reserved_value FROM orders WHERE id=?", (rownp["id"],))
        pos = dbnp.one("SELECT qty,avg_cost FROM positions WHERE symbol='NIFTYBEES'")
        check("SIP: pending order settles at broker average exactly once",
              n_done == 1 and st_ok["status"] == "FILLED" and st_ok["price"] == 101.5
              and pos["qty"] == 30 and pos["avg_cost"] == 101.5 and st_ok["reserved_value"] == 3045.0)
        cash_after = brkp.cash()
        check("SIP: resolver idempotent", resolve_pending_live_orders(dbnp, brkp) == 0 and brkp.cash() == cash_after)
    finally:
        dbnp.close()
        try: os.remove(DATA_DIR / "selftest_pend.db")
        except Exception: pass

    # N7: refresh_nav_csvs tops up a stale file from the bulk dump (one call),
    #     skips files already current, and never freshens unmapped symbols
    navtmp3 = DATA_DIR / "st_nav3"; navtmp3.mkdir(exist_ok=True)
    (navtmp3 / "RF1.csv").write_text(
        "symbol,date,nav\nRF1,2026-10-20,100.0000\nRF1,2026-10-21,100.4000\n",
        encoding="utf-8")
    (navtmp3 / "RF2.csv").write_text(
        "symbol,date,nav\nRF2,2026-10-21,55.5000\n", encoding="utf-8")
    (navtmp3 / "RF3.csv").write_text(
        "symbol,date,nav\nRF3,2026-10-21,7.2500\n", encoding="utf-8")
    (navtmp3 / "_nav_map.json").write_text(json.dumps({
        "RF1": {"isin": "INF0000000R1", "code": "905001", "source": "x"},
        "RF2": {"isin": "INF0000000R2", "code": "905002", "source": "x"}}),
        encoding="utf-8")
    bulk_navall = ("scheme_code,isin,scheme_name,nav,date\n"
                   "905001,INF0000000R1,Ref One,101.20,28-10-2026\n"
                   "905002,INF0000000R2,Ref Two,55.50,21-10-2026\n"
                   + "".join(f"9{10000+i},INF9{i:08d},Filler Fund {i},10.00,28-10-2026\n"
                             for i in range(1200)))   # >= the >=1000-row sanity gate
    fake_r = lambda url, timeout=25, verify=True: (
        (bulk_navall, "") if "groww" in url else ("", "offline"))
    n_ref = refresh_nav_csvs(dbn, folder=navtmp3, http_get=fake_r)
    rows1 = (navtmp3 / "RF1.csv").read_text(encoding="utf-8")
    rows2 = (navtmp3 / "RF2.csv").read_text(encoding="utf-8")
    rows3 = (navtmp3 / "RF3.csv").read_text(encoding="utf-8")
    # N8: inav_premium_block — a SIP-buy-scale check (audit F1/F6 v3): premium
    #     above band+lag-cushion BLOCKS, in-band passes, DISCOUNT never blocks
    dbn8 = Db(DATA_DIR / "selftest_prem.db")
    dbn8.execute("INSERT OR REPLACE INTO etf_universe(symbol,name,status,broad,category) "
                 "VALUES('PREM','Prem Fund','ACTIVE',0,'EQUITY')")
    dbn8.execute("INSERT INTO nav_data(symbol,date,nav) VALUES(?,?,?)",
                 ("PREM", today_str(), 100.0))
    fe8 = FilterEngine(dbn8)
    blk_hi, dev_hi, lim_hi = fe8.inav_premium_block(
        "PREM", today_str(), price=100.0 * (1 + INAV_DEV_SECTOR + 0.005))
    blk_ok, dev_ok, _ = fe8.inav_premium_block(
        "PREM", today_str(), price=100.0 * (1 + INAV_DEV_SECTOR - 0.005))
    blk_disc, dev_disc, _ = fe8.inav_premium_block("PREM", today_str(), price=98.5)
    check("N8: premium above band blocks; in-band passes; discount never blocks",
          blk_hi and dev_hi > lim_hi - 1e-12 and not blk_ok and not blk_disc
          and dev_disc < 0, f"hi={blk_hi} ok={blk_ok} disc={blk_disc}")
    dbn8.close()
    try:
        os.remove(DATA_DIR / "selftest_prem.db")
    except Exception:
        pass

    check("N7: bulk refresh tops up a stale file, leaves current+unmapped alone",
          n_ref == 1 and "2026-10-28,101.2000" in rows1
          and rows2.count("\n") == 2 and "2026-10-28" not in rows3,
          f"n={n_ref}\n{rows1}")
    try:
        for _fp in navtmp3.glob("*"):
            os.remove(_fp)
        os.rmdir(navtmp3)
    except Exception:
        pass
    globals()["NAV_CALL_SLEEP_SEC"], globals()["HTTP_SLEEP"] = _s1, _s2
    dbn2.close()
    for _db, _dir in [("selftest_nav.db", navtmp), ("selftest_nav2.db", navtmp2)]:
        try:
            os.remove(DATA_DIR / _db)
            for _fp in Path(_dir).glob("*"):
                os.remove(_fp)
            os.rmdir(_dir)
        except Exception:
            pass

    # A5: time-based release from the 20% cash lock
    dbr = Db(DATA_DIR / "selftest_risk.db")
    dbr.meta_set("equity_peak", "100000")
    dbr.meta_set("equity_peak_mode", "LIVE_SIP")
    re = RiskEngine(dbr, vol_target=0.08)   # rule semantics at the 8% rulebook target
    re.update(100000.0 * 0.75)          # -25% -> EXTREME lock
    check("audit A5: 25% drop locks to cash", re.all_cash is True)
    re.trigger_date = (parse_date(today_str()) - dt.timedelta(days=RISK_RELEASE_DAYS + 1)).isoformat()
    re.update(100000.0 * 0.75)          # no price recovery, but time passed
    check("audit A5: time-based release fires without recovery",
          re.all_cash is False and re.halved is False)
    dbr.close()
    try:
        os.remove(DATA_DIR / "selftest_risk.db")
    except Exception:
        pass

    # A2/#3: inverse-vol consistency — a 0.1% vol cash proxy would take 25%
    # if it were selectable; combined with A3's exclusion the check above covers
    # the ranking path.  Here: sizing of a tiny-vol asset stays capped.
    chosen3 = [mk("EQ1", 0.0), mk("EQ2", 0.0)]
    chosen3[0]["vol"] = 0.20; chosen3[0]["rows"] = [(f"2026-01-{d:02d}", 100.0 * (1 + 0.001 * d)) for d in range(1, 29)]
    chosen3[1]["vol"] = 0.001; chosen3[1]["rows"] = [(f"2026-01-{d:02d}", 100.0) for d in range(1, 29)]
    sized3 = pb.size(chosen3, 1_000_000.0)
    w_tiny = sized3["weights"].get("EQ2", 0.0)
    check("audit A3: near-zero-vol asset capped at 25%, not 100%",
          w_tiny <= 0.25 + 1e-9, f"w={w_tiny:.3f}")

    # ================= SECOND-AUDIT REGRESSIONS (Tuning audit) =================

    # 2.2: adjustments are IDEMPOTENT — recomputing must not compound factors
    dbsub3 = Db(DATA_DIR / "selftest_ca.db")
    dbsub3.execute(
        "INSERT INTO price_daily(symbol,date,open,high,low,close,adj_close,volume,traded_value) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("SPLITX", "2024-01-31", 200, 200, 200, 200, 200, 1, 1e7))
    dbsub3.execute(
        "INSERT INTO price_daily(symbol,date,open,high,low,close,adj_close,volume,traded_value) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("SPLITX", "2024-02-15", 100, 100, 100, 100, 100, 1, 1e7))   # raw halves
    dbsub3.execute(
        "INSERT OR REPLACE INTO corporate_actions(symbol,ex_date,factor,note) VALUES(?,?,?,?)",
        ("SPLITX", "2024-02-15", 2.0, "SYNTHETIC split 1:2 (factor 2.0 per convention)"))
    dbsub3.execute(
        "INSERT OR REPLACE INTO corporate_actions(symbol,ex_date,factor,note) VALUES(?,?,?,?)",
        ("SPLITX", "2024-02-15", 2.0, "SYNTHETIC split 1:2 (factor 2.0 per convention)"))
    dm3 = DataManager(dbsub3, None)
    dm3.recompute_adjustments("SPLITX")
    a1 = dbsub3.one("SELECT adj_close FROM price_daily WHERE symbol='SPLITX' AND date='2024-02-15'")["adj_close"]
    dm3.recompute_adjustments("SPLITX")      # second recompute must not compound
    dm3.recompute_adjustments("SPLITX")
    a2 = dbsub3.one("SELECT adj_close FROM price_daily WHERE symbol='SPLITX' AND date='2024-02-15'")["adj_close"]
    check("audit 2.2: factor 2.0 restates the halved price; recompute is idempotent",
          abs(a1 - 200.0) < 1e-6 and abs(a2 - a1) < 1e-9, f"adj={a1} then {a2}")
    dbsub3.close()
    try:
        os.remove(DATA_DIR / "selftest_ca.db")
    except Exception:
        pass

    # 2.7: loss netting — STCL offsets LTCG inside the class; cess applied
    dbsub4 = Db(DATA_DIR / "selftest_tax.db")
    dbsub4.execute("INSERT OR REPLACE INTO etf_universe(symbol,name,status,broad,category) "
                   "VALUES('GOLDBEES','Gold ETF','ACTIVE',0,'COMMODITY')")
    te4 = TaxEngine(dbsub4)
    te4.record_buy("GOLDBEES", "2024-04-01", 100.0, 100.0)     # FY 2024-25
    te4.record_buy("GOLDBEES", "2024-06-01", 200.0, 100.0)
    te4.record_sell("GOLDBEES", "2024-08-01", 150.0, 100.0)    # FIFO: +5000 ST
    # now force a loss on the second lot
    te4.record_sell("GOLDBEES", "2024-09-01", 100.0, 100.0)    # -10000 ST
    ys = te4.year_tax_summary()
    fy_row = ys.get("FY2025")          # Apr-2024 .. Mar-2025
    check("audit 2.7: same-year ST loss nets against ST gain before tax",
          fy_row is not None and abs(fy_row["stcg"] - (-5000.0)) < 1e-6
          and fy_row["tax"] == 0.0,
          f"row={fy_row}")
    dbsub4.close()
    try:
        os.remove(DATA_DIR / "selftest_tax.db")
    except Exception:
        pass

    # audit F8 (2026-10-28 v3): the incumbency BONUS knob was retired (dead
    # under the measured rotation rule); rotation is the incumbency mechanism.
    global VOL_BLEND_252

    # 3.2: vol blend damps the 60d estimate toward the 252d estimate
    save_vb = VOL_BLEND_252
    try:
        flat_then_noise = [100.0] * 200 + [100.0 * (1.0 + (0.05 if i % 2 else -0.05))
                                          for i in range(120)]
        VOL_BLEND_252 = 0.0
        v60 = annualized_vol(flat_then_noise, 60)
        VOL_BLEND_252 = 0.5
        vb = annualized_vol(flat_then_noise, 60)
        VOL_BLEND_252 = 0.0
        check("audit 3.2: vol blend damps the 60d estimate toward 252d",
              vb < v60 * 0.999, f"v60={v60:.4f} blend={vb:.4f}")
    finally:
        VOL_BLEND_252 = save_vb

    # 3.5: DD thresholds scale with the vol target
    dd10 = dd_thresholds(0.08)
    check("audit 3.5: DD thresholds scale with the vol target",
          abs(dd10[0] - DD_REVIEW_MULT * 0.08) < 1e-9
          and abs(dd10[1] - DD_SEVERE_MULT * 0.08) < 1e-9
          and abs(dd10[2] - DD_EXTREME_MULT * 0.08) < 1e-9,
          f"{dd10} vs mults {(DD_REVIEW_MULT, DD_SEVERE_MULT, DD_EXTREME_MULT)}")

    # --- audit 2026-10-19: data-integrity gate (synthetic/real splice detector) ---
    clean = [100.0 * (1.001 ** i) for i in range(300)]
    spliced = [3000.0 + i for i in range(200)] + [17.0 + 0.01 * i for i in range(100)]
    wild = clean[:240] + [clean[239] * (1 + 0.05 if i % 2 else 1 - 0.05)
                          for i in range(60)]
    check("audit 2026-10-19: clean series passes the integrity gate",
          series_integrity_issue(clean) == "")
    check("audit 2026-10-19: spliced series is flagged",
          "jump" in series_integrity_issue(spliced), series_integrity_issue(spliced))
    check("audit 2026-10-19: implausible vol is flagged",
          "volatility" in series_integrity_issue(wild), series_integrity_issue(wild))
    # gate in live selection: corrupt+held -> EXIT, corrupt+free -> BLOCKED
    dbsub8 = Db(DATA_DIR / "selftest_integ.db")
    fe8 = FilterEngine(dbsub8)
    pb8 = PortfolioBuilder(dbsub8, fe8)
    mk8 = lambda sym, mom, vals: {"symbol": sym, "momentum": mom, "vol": 0.15,
                                  "liq_p25": 1e8, "adj": list(vals), "index_family": sym,
                                  "rows": [(f"2026-{i//28+1:02d}-{i%28+1:02d}", v, v, 1e7)
                                           for i, v in enumerate(vals)]}
    pool8 = [mk8("SPLICEDHELD", 0.50, spliced), mk8("SPLICEDFREE", 0.50, spliced),
             mk8("CLEANONE", 0.05, clean)]
    sel8 = pb8.select(pool8, {"SPLICEDHELD"}, equity=1e8)
    dec8 = {st["symbol"]: dec for st, dec, _r in sel8["decisions"]}
    check("audit 2026-10-19: corrupt held name forced OUT, corrupt free name BLOCKED",
          dec8.get("SPLICEDHELD") == "EXIT" and dec8.get("SPLICEDFREE") == "BLOCKED"
          and dec8.get("CLEANONE") == "ELIGIBLE_BUY", f"{dec8}")
    dbsub8.close()
    try:
        os.remove(DATA_DIR / "selftest_integ.db")
    except Exception:
        pass
    # live-path regression: TradingCycle exposes its data manager (self.data)
    dbsub7 = Db(DATA_DIR / "selftest_data.db")
    check("hotfix: TradingCycle exposes its data manager",
          hasattr(TradingCycle(dbsub7), "data"))
    dbsub7.close()
    try:
        os.remove(DATA_DIR / "selftest_data.db")
    except Exception:
        pass

    # --- hotfix regression: live-path 200dma overlay on 4-column SQLite rows ---
    # (guards the overlay-vs-liquidity gate ordering on 4-col SQLite rows)
    dbsub6 = Db(DATA_DIR / "selftest_ov.db")
    fe6 = FilterEngine(dbsub6)
    pb6 = PortfolioBuilder(dbsub6, fe6)
    mk4 = lambda sym, mom, adj_path: {
        "symbol": sym, "momentum": mom, "vol": 0.15, "liq_p25": 1e8,
        "adj": adj_path, "index_family": sym,
        # THE CRASH SHAPE: 4-column rows (date, adj_close, close, traded_value)
        "rows": [(f"2026-{i//28+1:02d}-{i%28+1:02d}", a, a, 1e7)
                 for i, a in enumerate(adj_path)]}
    up = [50.0 + 0.25 * i for i in range(210)]        # rising -> above 200dma
    down = [100.0 - 0.25 * i for i in range(210)]     # falling -> below 200dma
    pool_ov = [
        mk4("HELDLOW", 0.05, down),    # holding, hold-zone momentum, below 200dma
        mk4("NEWLOW", 0.10, down),     # fresh buy candidate below 200dma
        mk4("NEWUP", 0.10, up),        # fresh buy candidate above 200dma
        mk4("THINUP", 0.10, up),       # above 200dma but absurdly illiquid
    ]
    pool_ov[3]["liq_p25"] = 1000.0
    sel_ov = pb6.select(pool_ov, {"HELDLOW"}, equity=1e8)
    decmap = {st["symbol"]: dec for st, dec, _r in sel_ov["decisions"]}
    check("hotfix: 4-col rows through 200dma overlay (no unpack crash)",
          decmap.get("HELDLOW") == "EXIT" and decmap.get("NEWLOW") == "SKIPPED"
          and decmap.get("NEWUP") == "ELIGIBLE_BUY",
          f"{decmap}")
    check("hotfix: liquidity gate still fires when overlay is ON",
          decmap.get("THINUP") == "SKIPPED", f"{decmap}")
    dbsub6.close()
    try:
        os.remove(DATA_DIR / "selftest_ov.db")
    except Exception:
        pass

    # --- Daily deployment mode (redesigned 2026-10-28: INVEST_PER_DAY) -------
    check("deploy: config is 5 ETFs, SIP mode on, budget engine on",
          MAX_HOLDINGS == 5 and SIP_MODE is True and INVEST_PER_DAY >= 0.0,
          f"MAX_HOLDINGS={MAX_HOLDINGS} SIP_MODE={SIP_MODE} "
          f"INVEST_PER_DAY={INVEST_PER_DAY}")
    # budget idempotency journal: same day tops up, next day resets, garbage=0
    _t0 = "2026-10-28"
    _mv = json.dumps({"date": _t0, "deployed": 3000.0, "orders": 2})
    a1, r1 = deploy_budget_remaining(_mv, _t0, 5000.0)
    a2, r2 = deploy_budget_remaining(_mv, "2026-10-29", 5000.0)
    a3, r3 = deploy_budget_remaining("not-json", _t0, 5000.0)
    a4, r4 = deploy_budget_remaining("", _t0, 0.0)
    check("deploy journal: same-day top-up, new-day reset, garbage reads zero",
          a1 == 3000.0 and r1 == 2000.0 and a2 == 0.0 and r2 == 5000.0
          and a3 == 0.0 and r3 == 5000.0 and a4 == 0.0 and r4 == 0.0,
          f"same-day=({a1},{r1}) next-day=({a2},{r2}) garbage=({a3},{r3}) "
          f"zero-amount=({a4},{r4})")
    # exit_rules_fire — the R12A daily-exit rule
    _flat = [100.0] * 300                      # flat: mom=0 -> fires (<=0)
    _rise = [100.0 + i * 0.1 for i in range(300)]   # uptrend, above 200-DMA
    _dipp = [100.0 + i * 0.1 for i in range(250)] + list(
        124.9 - i * 0.5 for i in range(60))    # sharp dip below 200-DMA,
                                               # long-run momentum still >0
    _ef = (exit_rules_fire(_flat), exit_rules_fire(_rise), exit_rules_fire(_dipp),
           exit_rules_fire([1.0] * 50))
    check("exit_rules_fire: flat fires, uptrend holds, DMA dive fires, short N/A",
          _ef == (True, False, True, False), f"got {_ef}")
    check("sip: SEVERE halves, EXTREME liquidates, REVIEW never sells",
          derisk_sell_qty(101, "SEVERE") == 50 and derisk_sell_qty(101, "EXTREME") == 101
          and derisk_sell_qty(101, "REVIEW") == 0 and derisk_sell_qty(1, "SEVERE") == 0
          and derisk_sell_qty(0, "EXTREME") == 0)
    check("sip: daily buys pause only in SEVERE/EXTREME",
          (not sip_buys_allowed("SEVERE")) and (not sip_buys_allowed("EXTREME"))
          and sip_buys_allowed("NORMAL") and sip_buys_allowed("REVIEW"))
    check("sip: quote session date parses ISO + epoch, rejects garbage",
          quote_session_date("2026-10-19T15:00:00.000+05:30") == "2026-10-19"
          and quote_session_date("2026-10-19T15:00:00+0530") == "2026-10-19"
          and quote_session_date(1792360800000) is not None
          and quote_session_date("garbage") is None and quote_session_date(None) is None)
    check("sip: quote_is_today is tri-state (unknown stays unknown)",
          quote_is_today("2026-10-19T15:00:00.000+05:30", "2026-10-19") is True
          and quote_is_today("2026-10-18T15:00:00.000+05:30", "2026-10-19") is False
          and quote_is_today(None, "2026-10-19") is None)
    dbsub11 = Db(DATA_DIR / "selftest_sip.db")
    try:
        dbsub11.meta_set("cash_balance", "100000")
        dbsub11.meta_set("equity_peak", "100000")
        dbsub11.execute(
            "INSERT INTO positions(symbol,qty,avg_cost,updated) VALUES(?,?,?,?)",
            ("NIFTYBEES", 100, 100.0, now_iso()))
        dbsub11.execute(
            "INSERT INTO price_daily(symbol,date,open,high,low,close,adj_close,"
            "volume,traded_value) VALUES(?,?,?,?,?,?,?,?,?)",
            ("NIFTYBEES", today_str(), 100.0, 100.0, 100.0, 100.0, 100.0, 1e6, 1e8))
        dbsub11.execute("INSERT OR REPLACE INTO etf_universe(symbol,name,status,instrument_key) "
                        "VALUES('NIFTYBEES','NB','ACTIVE','NSE_EQ|NIFTYBEES')")
        cyc11 = TradingCycle.__new__(TradingCycle)
        cyc11.db = dbsub11
        cyc11.broker = Broker(dbsub11, TaxEngine(dbsub11), _FakeLive(
            hold=[{"trading_symbol": "NIFTYBEES", "quantity": 100, "average_price": 100.0, "last_price": 101.0}],
            cash=100000.0, details={"status": "complete", "average_price": 101.0, "filled_quantity": 40}))
        assert cyc11.broker.sync_live_book()
        dbsub11.meta_set("last_trade_signal", cyc11.last_month_end())
        dbsub11.execute(
            "INSERT INTO pending_trades(signal_as_of,exec_after,symbol,side,"
            "target_qty,reason,status,created) VALUES(?,?,?,?,?,?,?,?)",
            ("2026-01-01", "2026-01-02", "NIFTYBEES", "SELL", 40,
             "test remainder", "PENDING", now_iso()))
        n_sip = cyc11.consume_pending_sells({"NIFTYBEES": 101.0})
        left = dbsub11.one("SELECT qty FROM positions WHERE symbol='NIFTYBEES'")["qty"]
        check("sip: queued SELL remainder executes against verified live holdings",
              n_sip == 1 and left == 60, f"consumed={n_sip} left={left}")
        dbsub11.meta_set(SIP_BASKET_KEY, "AAA,BBB,CCC")
        check("sip: basket meta roundtrip",
              dbsub11.meta_get(SIP_BASKET_KEY, "") == "AAA,BBB,CCC")
    finally:
        dbsub11.close()
        try:
            os.remove(DATA_DIR / "selftest_sip.db")
        except Exception:
            pass

    # --- Audit v4 (first LIVE run review, 28-Oct-2026) regression tests -----
    # L4: after a REFRESH run (basket re-derived mid-month, override date), the
    # closing write of "last_trade_signal" must land on the TRUE last
    # month-end — the key the NEXT run recomputes.  Writing the override date
    # instead left a permanent mismatch and the cycle re-executed every run.
    _l4_dates = ["2026-08-28", "2026-08-31", "2026-09-30", "2026-10-27", "2026-10-28"]
    _l4_written = last_month_end_date(_l4_dates, today="2026-10-28")   # run() write branch
    _l4_next_key = last_month_end_date(_l4_dates, today="2026-10-28")  # next run's check key
    check("L4: refresh run closes the signal at the true month-end (no re-run loop)",
          _l4_written == _l4_next_key == "2026-09-30" and _l4_written != "2026-10-28",
          f"written={_l4_written} next-run-key={_l4_next_key}")

    _ot9, _px9 = live_buy_order_px(100.0)
    check("SIP: buy is a buffered live LIMIT", _ot9 == "LIMIT" and abs(_px9 - 100.2) < 1e-9)

    # P2: SIP sizing scale = drawdown scale x bear-regime halving — the SIP
    # sizing call used to hard-code risk_scale=1.0, ignoring the bear halving
    dbsub12 = Db(DATA_DIR / "selftest_scale.db")
    try:
        dbsub12.meta_set("cash_balance", "1000000")
        dbsub12.meta_set("equity_peak", "1000000")
        dbsub12.execute(
            "INSERT INTO etf_universe(symbol,name,category,index_family,status) "
            "VALUES(?,?,?,?,?)", ("NIFTYBEES", "NB", "EQ", "Nifty 50", "ACTIVE"))
        _end12 = dt.date(2026, 10, 27)
        for i in range(210):     # price falls INTO today -> last bar below 200dma
            _d = _end12 - dt.timedelta(days=i)
            _px = 100.0 + 0.5 * i
            dbsub12.execute(
                "INSERT INTO price_daily(symbol,date,open,high,low,close,"
                "adj_close,volume,traded_value) VALUES(?,?,?,?,?,?,?,?,?)",
                ("NIFTYBEES", _d.isoformat(), _px, _px, _px, _px, _px, 1e6, 1e8))
        cyc12 = TradingCycle(dbsub12)
        _s_bear = cyc12._current_risk_scale("2026-10-27")
        check("P2: sizing risk scale combines drawdown AND bear-regime halving",
              abs(_s_bear - 0.5) < 1e-9, f"bear={_s_bear}")
    finally:
        dbsub12.close()
        try:
            os.remove(DATA_DIR / "selftest_scale.db")
        except Exception:
            pass

    # P1 (reworked, audit v7 28-Oct-2026): an IMPOSSIBLE close-vs-NAV gap
    # (>300%) is corrupt data — excluded outright; a REAL premium within
    # market-documented bounds stays visible but carries the buy-ban flag
    # (the measured global-ETF premium story — SEBI overseas freeze); a sane
    # gap is untouched.
    dbsub13 = Db(DATA_DIR / "selftest_inav.db")
    try:
        for _sym, _px, _nav, _cat in (("BADNAV", 450.0, 100.0, "EQ"),
                                      ("PREMGLOB", 130.0, 100.0, "GLOBAL INDICES"),
                                      ("GOODNAV", 101.0, 100.0, "EQ")):
            dbsub13.execute(
                "INSERT INTO etf_universe(symbol,name,category,index_family,status) "
                "VALUES(?,?,?,?,?)", (_sym, _sym, _cat, _sym, "ACTIVE"))
            for i in range(280):
                _d = (dt.date(2025, 9, 1) + dt.timedelta(days=i)).isoformat()
                dbsub13.execute(
                    "INSERT INTO price_daily(symbol,date,open,high,low,close,"
                    "adj_close,volume,traded_value) VALUES(?,?,?,?,?,?,?,?,?)",
                    (_sym, _d, _px, _px, _px, _px, _px, 1e6, 1e8))
        _asof13 = (dt.date(2025, 9, 1) + dt.timedelta(days=279)).isoformat()
        _navd13 = (dt.date(2025, 9, 1) + dt.timedelta(days=277)).isoformat()
        for _sym in ("BADNAV", "PREMGLOB", "GOODNAV"):
            dbsub13.execute("INSERT INTO nav_data(symbol,date,nav) VALUES(?,?,?)",
                            (_sym, _navd13, 100.0))
        _stat13 = TradingCycle(dbsub13).collect_stats(_asof13)
        _have13 = {s["symbol"]: s for s in _stat13}
        _po = _have13.get("PREMGLOB", {}).get("premium_over")
        check("P1: corrupt gap excluded; REAL premium stays visible but "
              "buy-banned; sane gap kept",
              "BADNAV" not in _have13 and "PREMGLOB" in _have13
              and "GOODNAV" in _have13
              and _po is not None and abs(_po[0] - 0.30) < 1e-9
              and "premium_over" not in _have13["GOODNAV"],
              f"have={sorted(_have13)} prem={_po}")
    finally:
        dbsub13.close()
        try:
            os.remove(DATA_DIR / "selftest_inav.db")
        except Exception:
            pass
    check("P1: tier ordering — corruption tier above the global premium cap "
          "above the domestic bands",
          INAV_CORRUPT_DEV > GLOBAL_ETF_PREMIUM_CAP
          > max(INAV_DEV_BROAD, INAV_DEV_SECTOR),
          f"corrupt={INAV_CORRUPT_DEV} global={GLOBAL_ETF_PREMIUM_CAP} "
          f"bands={INAV_DEV_BROAD}/{INAV_DEV_SECTOR}")
    # v7 classifier table + the global cap actually replaces the sector band
    check("v7: inav_tier — >300% or NAV scale jump = corrupt; >cap = premium; "
          "discounts never blocked",
          inav_tier(3.5, 0.0, 0.20) == "corrupt"
          and inav_tier(0.50, 0.25, 0.20) == "corrupt"   # one-step scale jump wins
          and inav_tier(0.30, 0.0, 0.20) == "premium"
          and inav_tier(0.15, 0.0, 0.20) == "ok"
          and inav_tier(-0.60, 0.0, 0.20) == "ok"        # discount: fine
          and inav_tier(-4.0, 0.0, 0.20) == "corrupt"    # absurd discount: data
          and inav_tier(None, 0.0, 0.20) == "unknown",
          "classifier paths broken")
    dbn13 = Db(DATA_DIR / "selftest_globprem.db")
    try:
        dbn13.execute("INSERT OR REPLACE INTO etf_universe(symbol,name,status,broad,category) "
                      "VALUES('GLOB','Glob Fund','ACTIVE',0,'GLOBAL INDICES')")
        dbn13.execute("INSERT INTO nav_data(symbol,date,nav) VALUES(?,?,?)",
                      ("GLOB", today_str(), 100.0))
        fe13 = FilterEngine(dbn13)
        _b21, _d21, _l21 = fe13.inav_premium_block("GLOB", today_str(), price=121.0)
        _b15, _d15, _l15 = fe13.inav_premium_block("GLOB", today_str(), price=115.0)
        _b95, _d95, _l95 = fe13.inav_premium_block("GLOB", today_str(), price=95.0)
        check("v7: GLOBAL INDICES names use the 20% premium cap, not the 0.75% band",
              abs(_l21 - GLOBAL_ETF_PREMIUM_CAP) < 1e-12
              and _b21 and abs(_d21 - 0.21) < 1e-9
              and not _b15 and abs(_d15 - 0.15) < 1e-9
              and not _b95 and _d95 < 0,
              f"21%={_b21}/{_l21} 15%={_b15} 95={_b95}")
    finally:
        dbn13.close()
        try:
            os.remove(DATA_DIR / "selftest_globprem.db")
        except Exception:
            pass

    dbsub14 = Db(DATA_DIR / "selftest_sync.db")
    try:
        dbsub14.meta_set("cash_balance", "1000000")
        class _BoomCash:
            def funds_available(self): raise RuntimeError("funds unavailable")
        bl14 = Broker(dbsub14, TaxEngine(dbsub14), _FakeLive(cash=500000.0))
        ok14 = bl14.sync_live_cash()
        bf14 = Broker(dbsub14, TaxEngine(dbsub14), _BoomCash())
        fail14 = bf14.sync_live_cash()
        check("SIP: cash sync succeeds from broker and fails closed on API error",
              ok14 and bl14.cash() == 500000.0 and not fail14 and not bf14.live_book_ok())
    finally:
        dbsub14.close()
        try: os.remove(DATA_DIR / "selftest_sync.db")
        except Exception: pass
    _src8 = Path(__file__).read_text(encoding="utf-8", errors="ignore")
    check("SIP: live-only configuration, permanent exit guard, no mode toggle",
          DAILY_EXIT_GUARD is True and SIP_MODE is True and INVEST_PER_DAY >= 0
          and "def live_order_ok" in _src8 and "PAPER_" + "TRADE" not in _src8
          and not hasattr(Broker, "accrue_cash_interest") and not hasattr(TradingCycle, "sleeve_sweep"))

    # --- Live SIP: Upstox holdings + order book are the source of truth;
    #     the local DB is only a mirror/journal. -----------------------------
    class _TruthClient:
        def funds_available(self): return 250000.0
        def holdings(self):
            return [{"trading_symbol": "ETFA", "quantity": 12, "t1_quantity": 2,
                     "average_price": 100.0, "last_price": 110.0},
                    {"trading_symbol": "ETFD", "quantity": 9, "t1_quantity": 0,
                     "average_price": 50.0, "last_price": 55.0}]   # unknown to the bot -> untouched
        def positions(self):
            return [{"trading_symbol": "ETFA", "buy_quantity": 1,
                     "sell_quantity": 0, "buy_price": 101.0, "last_price": 110.0},
                    {"trading_symbol": "ETFC", "buy_quantity": 3,
                     "sell_quantity": 0, "buy_price": 55.0, "last_price": 55.0}]
        def orders_book(self):
            return [{"trading_symbol": "ETFA", "transaction_type": "BUY",
                     "status": "open", "quantity": 5, "filled_quantity": 0},
                    {"trading_symbol": "ETFA", "transaction_type": "SELL",
                     "status": "trigger pending", "quantity": 6, "filled_quantity": 2},
                    {"trading_symbol": "ETFC", "transaction_type": "BUY",
                     "status": "complete", "quantity": 3, "filled_quantity": 3}]

    class _BoomBook:
        def funds_available(self): raise RuntimeError("offline test must not call network")
        def holdings(self): raise RuntimeError("offline test must not call network")
        def positions(self): raise RuntimeError("offline test must not call network")
        def orders_book(self): raise RuntimeError("offline test must not call network")

    _seed_truth = """
        INSERT INTO etf_universe(symbol,name,category,index_family,status)
        VALUES(?,?,?,?,?)"""
    dbsub15 = Db(DATA_DIR / "selftest_truth.db")
    try:
        for _sym in ("ETFA", "ETFB", "ETFC"):
            dbsub15.execute(_seed_truth, (_sym, _sym, "EQ", _sym, "ACTIVE"))
        # journal (stale, wrong on purpose): ETFB 7x90 + OLDJUNK 1x10 — the
        # broker holds NEITHER, so both must vanish after the sync
        dbsub15.execute("INSERT INTO positions(symbol,qty,avg_cost,updated) "
                        "VALUES(?,?,?,?)", ("ETFB", 7, 90.0, now_iso()))
        dbsub15.execute("INSERT INTO positions(symbol,qty,avg_cost,updated) "
                        "VALUES(?,?,?,?)", ("OLDJUNK", 1, 10.0, now_iso()))
        dbsub15.meta_set("cash_balance", "1000000")
        _tr15 = TaxEngine(dbsub15)
        _bl15 = Broker(dbsub15, _tr15, client=_TruthClient())

        # 1) booksync: holdings+positions+orderbook merged into the truth
        _ok15 = _bl15.sync_live_book()
        h15 = _bl15.holdings()
        _qa, _aa = (h15.get("ETFA") or {}).get("qty", 0), (h15.get("ETFA") or {}).get("avg_cost", 0)
        _qc = (h15.get("ETFC") or {}).get("qty", 0)
        _mirrored = (dbsub15.one("SELECT COUNT(1) c FROM positions WHERE qty>0")["c"]
                     == len(h15)
                     and dbsub15.one("SELECT qty FROM positions WHERE symbol='ETFA'")["qty"] == _qa
                     and dbsub15.one("SELECT qty FROM positions WHERE symbol='ETFB'") is None)
        check("live truth: broker holdings+positions decide what we own; journal "
              "mirror rewritten; foreign ETFs untouched",
              _ok15 is True and abs(_qa - 13.0) < 1e-9
              and abs(_aa - (12 * 100.0 + 1 * 101.0) / 13.0) < 1e-6
              and _qc == 3.0 and _mirrored
              and _bl15.cash() == 250000.0
              and dbsub15.one("SELECT qty FROM positions WHERE symbol='OLDJUNK'") is None
              and dbsub15.one("SELECT qty FROM positions WHERE symbol='ETFD'") is None,
              f"ETFA={_qa}@{_aa:.4f} ETFC={_qc} cash={_bl15.cash()}")

        # 2) open order quantities are visible for dup protection, and our own
        #    fills mirror into the live book immediately (shrinking the open qty)
        _ob = _bl15.live_open_qty("ETFA", "BUY")
        _sellable = _bl15.sellable_qty("ETFA")
        _bl15._apply_fill("ETFA", "BUY", 3, 110.0, 330.0, {"total": 0.0})
        _qa2 = _bl15.holdings()["ETFA"]["qty"]
        _sa2 = _bl15.sellable_qty("ETFA")
        _mir2 = dbsub15.one("SELECT qty FROM positions WHERE symbol='ETFA'")["qty"]
        check("live truth: open-order dup lock + own fills mirror instantly "
              "(open-order lock, book 13->16)",
              _ob == 5.0 and _sellable == 13.0 - 4.0 and _qa2 == 16.0
              and _sa2 == max(0.0, 16.0 - 4.0) and _mir2 == 16.0
              and _bl15.live_open_qty("ETFA", "BUY") == 5.0,
              f"ob={_ob} sellable={_sellable}->{_sa2} qty={_qa2}")

        # 4) unreadable broker => fail CLOSED: sync False, live_book None,
        #    positions mirror left exactly as it was
        _bf15 = Broker(dbsub15, _tr15, client=_BoomBook())
        _okf = _bf15.sync_live_book()
        _rowf = dbsub15.one("SELECT qty FROM positions WHERE symbol='ETFA'")["qty"]
        check("live truth: unreadable broker => fail-closed (no blind trading)",
              _okf is False and not _bf15.live_book_ok() and _rowf == 16.0,
              f"ok={_okf} book_ok={_bf15.live_book_ok()} ETFA still {_rowf}")
    finally:
        dbsub15.close()
        try:
            os.remove(DATA_DIR / "selftest_truth.db")
        except Exception:
            pass

    # --- Audit v5 (2026-10-28 comprehensive review) regression tests ---------
    # #1: iNAV compares the RAW close to the NAV, never the adjusted series
    dbsub16 = Db(DATA_DIR / "selftest_inavraw.db")
    try:
        _fe16 = FilterEngine(dbsub16)
        # adj_close is dividend-inflated 160 vs close 100; NAV is 100 ->
        # the comparison must read "no premium", not "+60%"
        dbsub16.execute(
            "INSERT INTO price_daily(symbol,date,open,high,low,close,adj_close,"
            "volume,traded_value) VALUES(?,?,?,?,?,?,?,?,?)",
            ("RAWCK", "2026-10-27", 100, 100, 100, 100.0, 160.0, 1e6, 1e8))
        dbsub16.execute("INSERT INTO nav_data(symbol,date,nav) VALUES(?,?,?)",
                        ("RAWCK", "2026-10-27", 100.0))
        _dev16, _gap16 = _fe16.inav_deviation("RAWCK", "2026-10-28")
        # and a genuinely mis-priced fund is still caught (+90% on raw close)
        dbsub16.execute(
            "INSERT INTO price_daily(symbol,date,open,high,low,close,adj_close,"
            "volume,traded_value) VALUES(?,?,?,?,?,?,?,?,?)",
            ("BADSPLIT", "2026-10-27", 190, 190, 190, 190.0, 190.0, 1e6, 1e8))
        dbsub16.execute("INSERT INTO nav_data(symbol,date,nav) VALUES(?,?,?)",
                        ("BADSPLIT", "2026-10-27", 100.0))
        _devb16, _ = _fe16.inav_deviation("BADSPLIT", "2026-10-28")
        check("v5#1: iNAV gate uses RAW close — dividend-adjusted series never "
              "fakes a premium; a real absurd gap is still caught",
              abs(_dev16) < 1e-9 and abs(_devb16 - 0.90) < 1e-6,
              f"raw-dev={_dev16} bad-dev={_devb16}")
    finally:
        dbsub16.close()
        try:
            os.remove(DATA_DIR / "selftest_inavraw.db")
        except Exception:
            pass

    # #3: one order per ETF per day — day_slices allocates the whole budget
    #     arithmetically, caps clip per-day, slack redistributes (no 3-pass
    #     fragmentation like the live run's 9 orders for 3 ETFs)
    _s3a = day_slices({"A": 150000, "B": 150000, "C": 150000}, 150000)
    _s3b = day_slices({"A": 100000, "B": 100000, "C": 100000}, 90000,
                      {"B": 20000})
    _s3c = day_slices({"A": 10000, "B": 10000}, 90000)      # gaps clip total
    _s3d = day_slices({"A": 99999, "B": 99999, "C": 99999}, 1500)  # true dust budget
    check("v5#3: day_slices — one slice/ETF, proportional, caps clip and "
          "redistribute, gaps clip; a budget under MIN_TRADE_VALUE idles "
          "(a merely UNSPLITTABLE budget concentrates per audit v11 P0-2 "
          "— covered by the v11 test below)",
          _s3a == {"A": 50000.0, "B": 50000.0, "C": 50000.0}
          and abs(sum(_s3b.values()) - 90000) < 1e-6 and _s3b["B"] == 20000.0
          and _s3b["A"] == _s3b["C"] == 35000.0
          and _s3c == {"A": 10000.0, "B": 10000.0} and _s3d == {},
          f"equal={_s3a} capped={_s3b} clipped={_s3c} dust={_s3d}")

    # --- audit v6 (2026-10-28 console review) --------------------------------
    # shared staleness counter — one rule for run(), run_daily(), deploy_day()
    dbsub19 = Db(DATA_DIR / "selftest_stale.db")
    try:
        # 8 market sessions after the signal date; weekend gap must not count
        for _d in ("2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22",
                   "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28"):
            dbsub19.execute(
                "INSERT INTO price_daily(symbol,date,open,high,low,close,"
                "adj_close,volume,traded_value) VALUES(?,?,?,?,?,?,?,?,?)",
                ("X", _d, 1, 1, 1, 1.0, 1.0, 1, 1))
        _s_old = signal_is_stale(dbsub19, "2026-10-16", "2026-10-28", limit=5)
        _s_fresh = signal_is_stale(dbsub19, "2026-10-26", "2026-10-28", limit=5)
        _s_none = signal_is_stale(dbsub19, "", "2026-10-28", limit=5)
        # the run() guard condition itself: override or force => never auto-redirect
        check("v6#2: one staleness rule — >limit session-days is stale, "
              "=limit is fresh, missing anchor never stale",
              _s_old == (True, 8) and _s_fresh == (False, 2) and _s_none == (False, 0),
          f"old={_s_old} fresh={_s_fresh} none={_s_none}")
    finally:
        dbsub19.close()
        try:
            os.remove(DATA_DIR / "selftest_stale.db")
        except Exception:
            pass

    # real market-session count
    dbsub22 = Db(DATA_DIR / "selftest_sessions.db")
    try:
        for _d in ("2026-10-28", "2026-10-29", "2026-10-30"):
            dbsub22.execute(
                "INSERT INTO price_daily(symbol,date,open,high,low,close,"
                "adj_close,volume,traded_value) VALUES(?,?,?,?,?,?,?,?,?)",
                ("X", _d, 1, 1, 1, 1.0, 1.0, 1, 1))
        _n1 = market_sessions_to(dbsub22, "2026-10-28", "2026-10-30")
        _n2 = market_sessions_to(dbsub22, "2026-10-30", "2026-10-30")
        check("v10: market-session counts remain accurate",
              _n1 == 2 and _n2 == 0, f"n1={_n1} n2={_n2}")
    finally:
        dbsub22.close()
        try:
            os.remove(DATA_DIR / "selftest_sessions.db")
        except Exception:
            pass

    # --- audit v11 (2026-10-28 console audit) --------------------------------
    # P0-1: sessions-to-replan must come from the CALENDAR, not the price table
    # (a start-of-day run has no future bars; the v10 helper always returned 0,
    # understating signal age).
    _exp = 0
    _d = parse_date("2026-10-28") + dt.timedelta(days=1)
    _hols_test = set()
    try:
        _hp = BASE_DIR / "nse_holidays.json"
        if _hp.exists():
            _hols_test = set(json.loads(_hp.read_text()))
    except Exception:
        pass
    while _d <= parse_date("2026-10-30"):
        if _d.weekday() < 5 and _d.isoformat() not in _hols_test:
            _exp += 1
        _d += dt.timedelta(days=1)
    _cal = sessions_to_calendar("2026-10-28", "2026-10-30")
    check("v11: sessions_to_calendar works on an EMPTY price table and equals "
          "the weekday-minus-holiday count (the v10 DB-count bug it fixes)",
          _cal == _exp and _cal >= 2, f"cal={_cal} expected={_exp}")

    # P0-2: a budget below MIN_TRADE_VALUE x basket size must CONCENTRATE on
    # the largest gap, not buy nothing (2026-10-28: 5000 over 3x 175k gaps
    # produced zero orders into a half-empty Rs 5.3 L plan).
    _c1 = day_slices({"A": 175000, "B": 175000, "C": 175000}, 5000)
    _c2 = day_slices({"A": 175000, "B": 175000, "C": 175000}, 5000,
                     caps={"A": 1000, "B": 1000})  # caps block: still buys C
    _c3 = day_slices({"A": 1000, "B": 900}, 5000)   # sub-min GAPS: still nothing
    check("v11: day_slices concentrates a too-small-to-split budget on the "
          "largest uncapped gap; sub-min gaps honestly stay unfilled",
          sum(_c1.values()) == 5000 and len(_c1) == 1
          and sum(_c2.values()) == 5000 and list(_c2) == ["C"]
          and _c3 == {},
          f"c1={_c1} c2={_c2} c3={_c3}")

    dbsub23 = Db(DATA_DIR / "selftest_peakmode.db")
    try:
        dbsub23.meta_set("equity_peak", "1000000")
        dbsub23.meta_set("equity_peak_mode", "LEGACY")
        re23 = RiskEngine(dbsub23)
        re23.update(300000.0, "migration")
        check("SIP: legacy fixed-capital peak rebases without a false crash",
              re23.peak == 300000.0 and re23.state == "NORMAL" and not re23.all_cash)
        re24 = RiskEngine(dbsub23)
        re24.update(210000.0, "real decline")
        check("SIP: a subsequent real account decline still triggers protection", re24.all_cash)
    finally:
        dbsub23.close()
        try: os.remove(DATA_DIR / "selftest_peakmode.db")
        except Exception: pass

    # audit v12 P0-2: freshness follows the last COMPLETED session, not a
    # 3-calendar-day shortcut.  Weekday math: Sunday evening must still count
    # Friday as pending-complete; Friday 17:00 counts Friday done.
    _lc1 = last_completed_session(dt.datetime(2026, 10, 25, 18, 0))   # Sun evening
    _lc2 = last_completed_session(dt.datetime(2026, 10, 23, 17, 0))   # Fri 17:00
    _lc3 = last_completed_session(dt.datetime(2026, 10, 23, 9, 0))    # Fri pre-close
    check("v12: last_completed_session — Sunday evening counts Friday as "
          "done; Friday post-close counts Friday; Friday morning counts "
          "Thursday (2026-10-23 = Fri, 2026-10-22 = Thu)",
          _lc1 == "2026-10-23" and _lc2 == "2026-10-23" and _lc3 == "2026-10-22",
          f"lc1={_lc1} lc2={_lc2} lc3={_lc3}")

    P.raw()
    if failed == 0:
        P.ok(f"ALL {passed} SELF TESTS PASSED \U0001F389")
    else:
        P.err(f"{failed} tests FAILED, {passed} passed.")
    return failed == 0


# ==============================================================================
# SECTION 22 — COMMANDS (scan / update / trade / monitor / reconcile)
# ==============================================================================

def ensure_dirs():
    for p in (DATA_DIR, LOG_DIR, CACHE_DIR, REPORT_DIR):
        p.mkdir(parents=True, exist_ok=True)


def cmd_scan(db: Db, try_live: bool = True, verbose: bool = True):
    scanner = Scanner(db)
    scanner.sync_universe()
    if try_live:
        try:
            env = load_env()
            client = UpstoxClient(env)
            P.info("Downloading NSE instrument master from Upstox to verify symbols...")
            instruments = client.download_instrument_master()
            by = scanner.verify_against_upstox(instruments)
            # audit v14 §3: count what actually got a key (the ISIN fallback
            # rescues exact-name misses), not just the raw name lookup.
            hits = db.one(
                "SELECT COUNT(*) AS c FROM etf_universe WHERE instrument_key "
                "IS NOT NULL AND instrument_key != ''")["c"]
            if hits > sum(1 for s in universe_map() if s in by):
                P.note(f"({hits - sum(1 for s in universe_map() if s in by)} "
                       f"matched via the ISIN fallback rather than the symbol name.)")
            P.ok(f"Instrument master matched {hits}/{len(ETF_UNIVERSE)} ETF symbols on NSE.")
        except Exception as e:
            P.warn(f"Could not reach Upstox instrument master ({e}).")
            P.note("Continuing with locally stored universe — discovery dates are still recorded.")
    if verbose:
        scanner.show_universe_table()
    else:
        scanner.show_universe_summary()


def cmd_update(db: Db, years: int = 11, force: bool = False, verbose: bool = True):
    try:
        env = load_env()
        client = UpstoxClient(env)
    except Exception as e:
        P.err(str(e))
        P.note("Showing coverage of the CACHED prices (used as-is while offline).")
        # coverage needs only the database — safe with no Upstox client
        if verbose:
            DataManager(db, None).coverage_table()
        else:
            DataManager(db, None).coverage_summary()
        return
    dm = DataManager(db, client)
    heal = force
    _synthetic_heal = db.meta_get("data_origin") == "SYNTHETIC"
    if _synthetic_heal:
        heal = True
        P.warn("Database holds SYNTHETIC demo rows mixed with prices. "
               "They will be PURGED and re-downloaded from Upstox — synthetic "
               "and real prices are NEVER merged (a spliced series produces "
               "nonsense like -99% momentum; see audit 2026-10-19).")
        # audit v14 P0 (2026-10-30): the purge only touched price_daily —
        # synthetic NAV rows and synthetic split factors SURVIVED, so future
        # real prices were adjusted by fake splits and the iNAV filter compared
        # real prices to fictional NAVs.  On a synthetic heal, clear ALL THREE.
        n_syn_ca = db.one("SELECT COUNT(*) AS c FROM corporate_actions "
                          "WHERE note LIKE 'SYNTHETIC%'")["c"]
        n_nav_rows = db.one("SELECT COUNT(*) AS c FROM nav_data")["c"]
        db.execute("DELETE FROM corporate_actions WHERE note LIKE 'SYNTHETIC%'")
        db.execute("DELETE FROM nav_data")
        P.note(f"Synthetic purge: {n_syn_ca} fake split factor(s) and "
               f"{n_nav_rows} synthetic NAV row(s) deleted (NAV re-fetches "
               f"from real files after the real download).")
    if heal and not db.meta_get("data_origin") == "SYNTHETIC":
        P.warn("--force-data: purging stored prices and re-downloading full history.")
    n_ca = dm.load_corporate_actions()
    n_nav = dm.load_nav_data()
    n_nav_cov = db.one("SELECT COUNT(DISTINCT symbol) AS c FROM nav_data")["c"]
    n_nav_act = db.one("SELECT COUNT(*) AS c FROM etf_universe WHERE status='ACTIVE'")["c"]
    if n_ca:
        P.ok(f"Loaded {n_ca} corporate actions — adj_close recomputed for history "
             f"(audit fix: stale factors no longer survive).")
    if n_nav:
        # audit §5 (2026-10-27): 'LIVE' without coverage is misleading when
        # only a handful of funds are mapped — say the coverage plainly
        P.ok(f"Loaded {n_nav} NAV points covering {n_nav_cov} of {n_nav_act} funds — "
             f"the iNAV premium/discount filter runs ONLY for mapped funds; the "
             f"other {max(n_nav_act - n_nav_cov, 0)} trade unchecked.")
        if n_nav_cov * 2 < n_nav_act:
            P.warn(f"iNAV coverage is thin ({n_nav_cov}/{n_nav_act}) — globally-listed "
                   f"⚑-flagged names are usually the ones in the unmapped set; the "
                   f"premium protection is effectively off for them until the NAV "
                   f"bootstrap maps them.")
    res_nav = None
    if NAV_AUTO_FETCH and _nav_retry_allowed(db) and n_nav_cov < n_nav_act:
        # run #8 (2026-10-28): the bootstrap used to fire ONLY at zero coverage,
        # so a partial map (13/111) was permanent — it now gap-fills whenever
        # the universe has more funds than the NAV store.  Success locks the
        # day; failure cools off NAV_FAIL_RETRY_HOURS (retry same session).
        try:
            write_nav_urls_listing()   # keep stock-data/nav_urls.txt fresh
            res_nav = ensure_nav_csvs(db)
        except Exception as e:
            res_nav = {"files": 0, "todo": -1, "failed": [], "diag": str(e),
                       "error": str(e)}
        if res_nav.get("files"):
            if not res_nav.get("failed"):
                # full coverage only — a thin 8/111 day must NOT lock retries
                db.meta_set("nav_autofetch_ok", today_str())
            db.meta_set("nav_autofetch_fail", "")
            P.ok(f"NAV files ready for {res_nav['files']} of "
                 f"{res_nav['todo']} funds"
                 + (f"; {res_nav['via_groww']} via the bulk feed"
                    if res_nav.get("via_groww") else "")
                 + (f"; {res_nav['via_static']} from the built-in verified list"
                    if res_nav.get("via_static") else "")
                 + (f"; {res_nav['via_search']} via name search"
                    if res_nav.get("via_search") else "")
                 + " — loading…")
            n_nav = dm.load_nav_data()
            if n_nav:
                _cov = db.one("SELECT COUNT(DISTINCT symbol) AS c FROM nav_data")["c"]
                n_nav_cov = _cov
                P.ok(f"Loaded {n_nav} NAV points — iNAV filter live for "
                     f"{_cov} of {n_nav_act} funds (the rest trade unchecked).")
            # partial-success diagnostics: when the bulk sources contributed
            # nothing, name why (the user's box saw AMFI match 0/87 ISINs)
            if res_nav.get("diag") and res_nav.get("files", 0) < res_nav.get("todo", 1):
                P.note(f"NAV fetch diagnostics: {res_nav['diag'][:300]}")
                if "CERTIFICATE_VERIFY_FAILED" in res_nav["diag"]:
                    P.note("TLS verification failed for the NAV hosts even after "
                           "the automatic unverified retry — this is usually "
                           "corporate AV/proxy interception that also BLOCKS the "
                           "body, or an upstream change. Fix at the source: stop "
                           "TLS interception / upgrade certifi. NAV_SSL_RELAX=True "
                           "is the manual always-off override (public reference "
                           "data only; the broker feed keeps full verification).")
            if res_nav.get("failed"):
                P.note(f"No NAV source mapped for: {', '.join(res_nav['failed'][:8])}"
                       + (" …" if len(res_nav["failed"]) > 8 else ""))
        else:
            db.meta_set("nav_autofetch_fail", now_iso())
    # run #8 (2026-10-28): bootstrapped NAV files used to FREEZE at creation
    # day — top them up from the bulk dump (one call) so the iNAV filter moves
    # with the market
    if NAV_AUTO_FETCH and n_nav:
        try:
            _n_ref = refresh_nav_csvs(db)
        except Exception:
            _n_ref = 0
        if _n_ref:
            n_nav = dm.load_nav_data()
            P.ok(f"NAV refresh: {_n_ref} fund file(s) topped up with the latest "
                 f"bulk NAV.")
        try:    # audit F6 v3: cached scheme mapping ages -> periodic re-verify
            _mp = CACHE_DIR.parent / "nav" / "_nav_map.json"
            _age = (parse_date(today_str())
                    - dt.datetime.fromtimestamp(_mp.stat().st_mtime, IST).date()).days
            if _age > 60:
                P.note(f"NAV scheme mapping is {_age} days old — re-run "
                       f"'python etf-momentum-screener.py load-data --fetch-nav' "
                       f"to re-verify it against the bulk dump.")
        except Exception:
            pass
    if not n_nav:
        tail = ""
        if res_nav is not None:
            tail = (f"  Auto-create attempt: {(res_nav.get('diag') or res_nav.get('error') or 'no scheme codes resolved')[:260]}"
                    f" — retry after {NAV_FAIL_RETRY_HOURS}h, or "
                    f"'python etf-momentum-screener.py load-data --fetch-nav'.")
        else:
            tail = "  Manual refresh: 'python etf-momentum-screener.py load-data --fetch-nav'."
        P.note("No NAV files found (stock-data/nav/*.csv with symbol,date,nav) — "
               "the iNAV filter will be skipped and the reason recorded." + tail)
    try:
        dm.update_all(years=years, force=heal, progress=verbose)
        if heal:
            db.meta_set("data_origin", "REAL")
            P.ok("Data origin set to REAL — synthetic rows are gone.")
    except UpstoxError as e:
        P.err(str(e))
        P.note("Update UPSTOX_ACCESS_TOKEN in env.txt (it expires daily) and rerun the update.")
    if verbose:
        dm.coverage_table()
    else:
        dm.coverage_summary()


def cmd_trade(db: Db, force_exec: bool = False, final_status: bool = True):
    """Live entry point, runnable any time: exits/rebalance always, buys only
    within the unused INVEST_PER_DAY allowance. Caller confirms real orders."""
    cyc = TradingCycle(db)
    cyc.run_daily(force_exec=force_exec, final_status=final_status)


def cmd_load_data(db: Db, fetch_nav: bool = False):
    """Load corporate actions + NAV CSVs from stock-data/ (audit bug #6).
    NAV files are AUTO-CREATED when absent (user ask 2026-10-27): ISINs from
    the journal -> AMFI NAVAll (scheme code + latest NAV) -> mfapi (history).
    --fetch-nav forces a rewrite of existing files."""
    P.section("LOADING CORPORATE ACTIONS & NAV DATA", "\U0001F4C4")
    dm = DataManager(db, UpstoxClient(load_env() if ENV_PATH.exists() else {}))
    folder = CACHE_DIR.parent / "nav"
    have_nav_files = any(folder.glob("*.csv"))
    if NAV_AUTO_FETCH and (fetch_nav or not have_nav_files):
        P.info((("Refreshing" if fetch_nav else "No NAV files found — creating")
                + " stock-data/nav/*.csv via Groww bulk NAV "
                f"(AMFI/mfapi fallbacks; history up to {NAV_HISTORY_DAYS} days)…"))
        try:
            res = ensure_nav_csvs(db, force=fetch_nav)
        except Exception as e:
            res = {"files": 0, "todo": -1, "failed": [], "error": str(e)}
        if res.get("files"):
            P.ok(f"NAV files {'refreshed' if fetch_nav else 'created'} for "
                 f"{res['files']} of {res['todo']} funds"
                 + (f"; {res['via_groww']} via Groww" if res.get("via_groww") else "")
                 + (f"; {res['via_search']} via name-search fallback"
                    if res.get("via_search") else "")
                 + " — mapping cache: stock-data/nav/_nav_map.json")
        if res.get("failed"):
            P.note(f"No NAV history mapped for: {', '.join(res['failed'][:8])}"
                   + (" …" if len(res["failed"]) > 8 else ""))
        elif res.get("todo", 0) > 0 and not res.get("files"):
            P.note(f"NAV auto-create failed: {(res.get('diag') or res.get('error') or 'no sources reachable')[:300]}")
            P.note("Either this machine is offline, or the upstream layout changed. "
                   "The console line above names each failing stage — paste it if "
                   "you want it fixed. Files stay absent; add them manually if needed.")
        if res.get("diag") and "CERTIFICATE_VERIFY_FAILED" in res.get("diag", ""):
            P.note("TLS verification failed for the NAV hosts even after the "
                   "automatic unverified retry (likely corporate AV/proxy "
                   "interception also mangling the body). Stop TLS interception "
                   "or trust the proxy CA; NAV_SSL_RELAX=True is the manual "
                   "override — NAV is public reference data, the broker feed "
                   "keeps full verification.")
    elif NAV_AUTO_FETCH and have_nav_files:
        try:
            _n_ref = refresh_nav_csvs(db)
        except Exception:
            _n_ref = 0
        if _n_ref:
            P.ok(f"NAV files topped up with the latest bulk NAV ({_n_ref} fund(s)).")
    n_ca = dm.load_corporate_actions()
    n_nav = dm.load_nav_data()
    P.ok(f"Corporate actions loaded: {n_ca}   |   NAV points loaded: {n_nav}")
    if n_nav:
        latest = db.one("SELECT MAX(date) AS d FROM nav_data")["d"]
        P.kv("NAV coverage", f"{db.one('SELECT COUNT(DISTINCT symbol) AS c FROM nav_data')['c']} "
                             f"funds, latest NAV date {latest}")
    else:
        P.info("Drop files into stock-data/corporate-actions/  (symbol,ex_date,factor,note)")
        P.info("and stock-data/nav/  (symbol,date,nav), then rerun this command.")


def cmd_navdiag(db: Db, symbols=None):
    """Audit v6 #1 (2026-10-28): a close-vs-NAV gap past the 25% absurd tier is
    a MAPPING fault to repair, not a market signal — this read-only diagnostic
    shows, per symbol, the mapped scheme code/name/ISIN, the last NAVs (and
    whether the NAV series itself jumped scale), the latest close, and the
    deviation the gate sees.  With no symbols given, lists every ACTIVE ETF
    currently over the absurd tier; with symbols, shows those."""
    P.section("NAV MAPPING DIAGNOSIS — is the right scheme feeding each ETF?", "\U0001F9EA")
    fe = FilterEngine(db)
    if not symbols:
        symbols = []
        for r in db.query("SELECT symbol FROM etf_universe WHERE status='ACTIVE' ORDER BY symbol"):
            try:
                dev, _g = fe.inav_deviation(r["symbol"], today_str())
                if dev is None:
                    continue
                _lim = (fe.inav_limit(r["symbol"])
                        + INAV_LAG_CUSHION * min(_g or 0, NAV_MAX_STALE_DAYS))
                if inav_tier(dev, nav_last_jump(db, r["symbol"]), _lim) != "ok":
                    symbols.append((r["symbol"], dev))
            except Exception:
                continue
        if not symbols:
            P.ok("Every ETF's price is within its allowed premium band of NAV "
                 "— the NAV mappings all look sane (the global-ETF premium cap "
                 f"is {fmt_pct(GLOBAL_ETF_PREMIUM_CAP)}).")
            return
        P.warn(f"{len(symbols)} ETF(s) currently sit outside their premium band:")
        symbols = [s for s, _d in symbols]
    rows = []
    for sym in [str(s).upper() for s in symbols]:
        row = nav_map_row(sym)
        navs = db.query("SELECT date, nav FROM nav_data WHERE symbol=? "
                        "ORDER BY date DESC LIMIT 3", (sym,))
        px = db.one("SELECT close, date FROM price_daily WHERE symbol=? "
                    "ORDER BY date DESC LIMIT 1", (sym,))
        dev, gap = fe.inav_deviation(sym, today_str())
        _nj = nav_last_jump(db, sym)
        jump = ""
        if _nj > 0.20 and len(navs) >= 2:
            jump = (f"NAV jumped {fmt_pct(_nj, 1)} in one step "
                    f"({navs[1]['date']}->{navs[0]['date']})")
        _lim = None
        if dev is not None:
            _lim = (fe.inav_limit(sym)
                    + INAV_LAG_CUSHION * min(gap or 0, NAV_MAX_STALE_DAYS))
        _t = inav_tier(dev, _nj, _lim)
        stale = ""
        if navs:
            try:
                _sd = (parse_date(today_str()) - parse_date(navs[0]["date"])).days
                if _sd > NAV_MAX_STALE_DAYS:
                    stale = f" [STALE NAV: {_sd}d — run update]"
            except Exception:
                pass
        verdict = {"corrupt": f"CORRUPT mapping (>{fmt_pct(INAV_CORRUPT_DEV,0)} "
                              f"gap or NAV scale jump) — excluded; fix the scheme code",
                   "premium": f"BUY-BANNED: real premium above the {fmt_pct(_lim or 0)} "
                              f"band — not bad data; unlocks under the cap",
                   "ok": "ok"}.get(_t, "no fresh NAV")
        rows.append([
            sym,
            row.get("code", "?") or "?",
            (row.get("name", "") or "?")[:30],
            (row.get("isin", "") or "?"),
            (fmt_rs(navs[0]["nav"]) + " " + navs[0]["date"]) if navs else "no NAV data",
            (fmt_rs(px["close"]) + " " + px["date"]) if px else "no prices",
            fmt_pct(dev) if dev is not None else "n/a",
            verdict + (" | " + jump if jump else "") + stale,
        ])
    P.table(["Symbol", "Scheme", "Scheme name", "ISIN", "Latest NAV",
             "Latest close", "Gap", "Verdict"], rows,
            aligns=["<", ">", "<", "<", ">", ">", ">", "<"])
    P.note("CORRUPT verdicts: re-check the scheme code in stock-data/nav_urls.txt "
           "against the fund's own NAV (AMFI/fund site), correct it, run 'update'.  "
           "BUY-BANNED verdicts are NOT data faults: the fund's NAV is real — the "
           "ETF trades above worth because SEBI's overseas-investment freeze blocks "
           "new units (BSE caution late-Sep-2026; price bands anchor to NAV from "
           "1-Apr-2027).  Buying auto-resumes when the premium cools.")


def cmd_reconcile(db: Db):
    """Read-only broker reconciliation plus pending-order resolution."""
    P.section("UPSTOX RECONCILIATION", "\U0001F50D")
    try:
        client = UpstoxClient(load_env())
    except Exception as e:
        P.err(str(e)); return
    broker = Broker(db, TaxEngine(db), client)
    before = {r["symbol"]: r["qty"] for r in db.query("SELECT symbol,qty FROM positions")}
    resolve_pending_live_orders(db, broker)
    if not broker.sync_live_book():
        P.err("Reconciliation failed; do not trade until Upstox is readable.")
        return
    after = broker.holdings()
    rows = []
    for sym in sorted(set(before) | set(after)):
        old, new = float(before.get(sym, 0)), float(after.get(sym, {}).get("qty", 0))
        rows.append([sym, fmt_num(old, 0), fmt_num(new, 0), "OK" if abs(old-new) < 0.5 else "UPDATED"])
    if rows:
        P.table(["Symbol", "Previous mirror", "Upstox truth", "Result"], rows,
                aligns=["<", ">", ">", "<"])
    P.ok(f"Upstox portfolio {fmt_cr(broker.holdings_value())}; cash {fmt_cr(broker.cash())}; "
         f"SIP reserved today {fmt_rs(broker.sip_committed())}.")


def cmd_monitor(db: Db):
    P.section("DAILY MONITOR", "\U0001F4E1")
    try:
        env = load_env()
        client = UpstoxClient(env)
    except Exception as e:
        P.err(str(e))
        return
    rows = db.query("SELECT symbol, instrument_key FROM etf_universe WHERE status='ACTIVE' ORDER BY symbol")
    ok = 0
    for r in rows:          # audit #6: every ETF, not just the first 40
        try:
            q = client.quote(r["instrument_key"])
            ltp = q.get("ltp")
            bid, ask = q.get("bid"), q.get("ask")
            spread = None
            if bid and ask and ltp:
                spread = abs(ask - bid) / ltp * 10000.0
                db.execute("INSERT INTO quotes(symbol,ts,ltp,bid,ask,spread_bps) VALUES(?,?,?,?,?,?)",
                           (r["symbol"], now_iso(), ltp, bid, ask, spread))
            P.raw(f"  {r['symbol']:<12} LTP Rs {ltp if ltp else 'n/a'}  "
                  f"bid {bid or '-'}  ask {ask or '-'}  "
                  f"spread {('%0.1f bps' % spread) if spread else '-'}")
            ok += 1
            time.sleep(HTTP_SLEEP)
        except UpstoxError as e:
            P.warn(f"{r['symbol']}: quote failed ({e})")
            if "RATE_LIMIT" in str(e) or "401" in str(e):
                P.note("Stopping quotes for now — same reason for every symbol. "
                       "Spread history will keep building up on later runs.")
                break
        except Exception as e:
            P.warn(f"{r['symbol']}: quote failed ({e})")
    broker = Broker(db, TaxEngine(db), client)
    if not broker.sync_live_book():
        P.err("Monitor could not verify Upstox holdings/cash; risk state not updated.")
        return
    risk = RiskEngine(db)
    hv, cash = broker.holdings_value(), broker.cash()
    account_eq = cash + hv
    ctx = risk.update(account_eq)
    risk.snapshot(today_str(), cash, hv, account_eq)
    P.kv("ETF portfolio market value", fmt_cr(hv))
    P.kv("Broker cash (separate)", fmt_cr(cash))
    P.kv("Account drawdown", fmt_pct(ctx["drawdown"]))
    P.kv("Risk state", ctx["state"])
    for level, msg in ctx["events"]:
        P.warn(f"RISK [{level}] {msg}")
    P.ok(f"Monitor complete — {ok} quotes snapshotted; no orders placed.")


# ==============================================================================
# SECTION 23 — CLI ENTRY POINT
# ==============================================================================


def main(argv=None):
    try:
        sys.stdout.reconfigure(line_buffering=True)   # live output even when piped
    except Exception:
        pass
    if os.name == "nt":
        # enable ANSI colour processing in Windows consoles — without this the
        # colour codes print as raw garbage like '←[96m' (seen in user runs)
        os.system("")
    ensure_dirs()
    setup_logging()
    parser = argparse.ArgumentParser(
        prog="etf-momentum-screener.py",
        description="Live Upstox ETF momentum SIP — strict daily investment cap")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "scan", "update", "trade", "monitor",
                                 "status", "report", "selftest", "live",
                                 "load-data", "reconcile", "navdiag"],
                        help="what to do (default: run = scan+update+daily SIP cycle+status)")
    parser.add_argument("extras", nargs="*",
                        help="extra positional args (e.g. symbols for 'navdiag')")
    parser.add_argument("--years", type=int, default=11, help="years of history to download")
    parser.add_argument("--force-exec", action="store_true",
                        help="execute at the latest price even if not the exact next session")
    parser.add_argument("--force-data", action="store_true",
                        help="purge stored prices and re-download full history "
                             "(heals synthetic/real splices)")
    parser.add_argument("--fetch-nav", action="store_true",
                        help="with 'load-data': force-refresh the auto-created "
                             "stock-data/nav/*.csv files from Groww/AMFI + mfapi")
    args = parser.parse_args(argv)

    db = Db()
    log(f"command={args.command}")
    try:
        if args.command == "selftest":
            if not cmd_selftest(db):
                raise SystemExit(1)
        elif args.command == "scan":
            P.banner()
            cmd_scan(db)
        elif args.command == "update":
            P.banner()
            cmd_scan(db, try_live=True)
            cmd_update(db, years=args.years, force=args.force_data)
        elif args.command == "trade":
            P.banner()
            if not live_order_ok(f"Live SIP cap {fmt_rs(INVEST_PER_DAY)}; exits/rebalance always active."):
                P.note("No orders placed. Use status for a read-only Upstox portfolio.")
                return
            cmd_trade(db, force_exec=args.force_exec)
        elif args.command == "monitor":
            P.banner()
            cmd_monitor(db)
        elif args.command == "navdiag":
            P.banner()
            cmd_navdiag(db, args.extras or None)
        elif args.command == "status":
            P.banner()
            cmd_status(db)
        elif args.command == "report":
            P.banner()
            cmd_report(db)
        elif args.command == "load-data":
            P.banner()
            cmd_load_data(db, fetch_nav=getattr(args, "fetch_nav", False))
        elif args.command == "reconcile":
            P.banner()
            cmd_reconcile(db)
        elif args.command == "live":
            P.banner()
            if not live_order_ok("Live SIP buys use LIMIT orders; exits use MARKET. Broker fills are reconciled."):
                P.note("No orders placed.")
                return
            cmd_trade(db, force_exec=args.force_exec)
        else:  # run
            P.banner()
            cmd_scan(db, verbose=False)
            cmd_update(db, years=args.years, force=args.force_data, verbose=False)
            prices = {r["symbol"] for r in db.query("SELECT DISTINCT symbol FROM price_daily")}
            if not prices:
                # audit v14 P0 (2026-10-30): this fallback once wrote SYNTHETIC
                # rows into the PRODUCTION database; a later live run could then
                # trade real money off fictional prices.  Removed — never invent
                # prices; point the user at a real data download instead.
                P.err("No downloaded prices in the database.  I will NOT invent "
                      "fake prices here (that pollutes the real journal).")
                P.note("To fill the database: fix the Upstox token "
                       "(env.txt) and run:  python etf-momentum-screener.py update")
                return
            if not live_order_ok(f"Live SIP cap {fmt_rs(INVEST_PER_DAY)}; zero means exits/rebalance only."):
                P.note("Orders declined; no risk locks or orders changed. Running read-only status.")
                cmd_status(db)
                return
            cmd_trade(db, force_exec=args.force_exec, final_status=False)
            cmd_status(db)
        P.raw()
        P.info(f"Database : {DB_PATH}")
        P.info(f"Logs     : {LOG_DIR}")
        P.info(f"Cache    : {CACHE_DIR}")
        P.info(f"Reports  : {REPORT_DIR}")
    except KeyboardInterrupt:
        P.warn("Stopped by user.")
    except Exception as e:
        P.err(f"Unexpected error: {e}")
        log(f"fatal: {e}", "ERROR")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()