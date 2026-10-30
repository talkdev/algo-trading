#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 T S M O M   —   Time Series Momentum on the Nifty ETF Universe
================================================================================
 Single-file, start-to-end implementation of the TSMOM algorithm.

 WHAT THIS PROGRAM DOES
 ----------------------
  1. Discovers a fixed universe of NSE ETFs (111 funds) and records the
     DISCOVERY DATE of every ETF the first time it is seen  -> SQLite DB.
  2. Downloads daily OHLCV history from the Upstox API (cached in stock-data/).
  3. Computes the TSMOM momentum signal:  M = P(t-21)/P(t-252) - 1
  4. Applies entry (+2%) / exit (0%) thresholds, liquidity, spread and iNAV
     filters.
  5. Builds portfolios three ways:
        Version A  = Pure TSMOM   (hold every qualifying ETF, equal-weighted)
        Version B  = TSMOM + Ranking (top MAX_HOLDINGS=5) <- traded by default
        Benchmark C= Ranking only (top MAX_HOLDINGS, no threshold)
  6. Sizes positions: inverse-volatility / equal-weight, dynamic liquidity cap,
     60-day Ledoit-Wolf (constant-correlation shrinkage) covariance, 15% annual
     portfolio volatility target (tested optimum — 8% costs 1.6pt CAGR, 20%
     adds drawdown for no CAGR), remainder in cash.
  7. Executes the NEXT trading session. MODE: PAPER (simulated) is the default
     — user direction 2026-10-28, second ask (PAPER_TRADE=True).  Real orders
     need PAPER_TRADE=False in the config AND a runtime y/n confirmation on
     the terminal (user direction 2026-10-29 — the --confirm flag is gone;
     interactive yes = the only way real orders leave); simulated fills use
     realistic slippage + full statutory charges.
  8. Tracks tax lots (STCG 20% / LTCG 12.5% above Rs 1.25 L), dividends,
     drawdown risk triggers (at the 15% vol target: 15% review / 22.5% halve /
     30% cash — they scale with VOL_TARGET) and re-entry.
  9. Backtests 10+ years with the last 20% held out-of-sample, parameter grid
     (lookback 126/189/252 x entry 0/1/2/3/5%), cost and cash-rate scenarios.

 HOW MONEY MOVES (the one knob that matters — 2026-10-28 redesign)
 -----------------------------------------------------------------
  * INVEST_PER_DAY in the config block below is the ONLY money that ever
    buys; it is a FLOOR.  Each run splits today's budget proportionally
    across the stored target-weight gaps; when the plan cannot fill before
    the next re-plan at the floor, today's budget is lifted to
    plan_gap / market-days-left so the picks actually land while fresh
    (backtest pace, user choice 2026-10-28).
      - 0  => ANALYSE + EXIT ONLY.  No accumulation buys ever; exit proceeds
              sit in cash and the invested fraction drifts to zero.  This is
              a deliberate cash-out mode, not a bug.
      - >0 => daily accumulation with autofill.  Re-running the same day is
              SAFE: the journal remembers what today already committed
              (settled AND broker-unconfirmed orders) and only tops up.
              A list older than STALE_REFRESH_DAYS (5 market days) is
              re-derived fresh from today's data before buying.
              Weekends/holidays: deploys at the last close (simulated fills —
              paper mode; in live mode treat those runs with restraint).
              Never double-orders: an open order blocks re-order of the same
              ETF today; hard-check failures (broken data / iNAV premium) drop
              a name from the plan after 3 days in a row.
  * The MONTH-END rebalance is SELL-ONLY: exits per the backtested rulebook
    (momentum <= 0, 200-DMA overlay, top-N rotation, data-integrity gate).
    It never buys; all buying funnels through INVEST_PER_DAY.
  * DAILY_EXIT_GUARD (default False): user-asked "exit the moment a holding
    breaks its rules".  BACKTESTED FIRST (R12A): immediate exits cost ~0.5pp
    CAGR and ~1.5pp OOS (whipsaw re-entries) for ~1.5pp better MaxDD — so it
    ships OFF.  Flip to True only if you prefer that tradeoff knowingly.
  * Every buy still faces: risk-state pause (SEVERE/EXTREME), trend guard
    (never accumulate what the exit rules would sell), the execution gate
    (liquidity / 15% of p25 / measured spread / fresh-quote iNAV premium with
    lag cushion), MIN_TRADE_VALUE, and available cash.

 SAFETY RULES THAT NEVER CHANGE
 ------------------------------
  * PAPER MODE IS THE DEFAULT (user request 2026-10-28, second ask):
    PAPER_TRADE=True, so plain runs SIMULATE with no real money.  Real orders
    need PAPER_TRADE=False in the config AND answering y to the run's own
    confirmation question (2026-10-29: --confirm removed on user direction).
  * Broker/quote TLS is always verified; only public NAV bulk data may fall
    back to an unverified channel after a clear warning (see NAV_SSL_RELAX).
  * Every threshold/frequency/rule above was backtested before it was set
    (experiments/REPORT_2026-10-27.md), not taken from theory.

 IN SIMPLE WORDS — THE FOUR QUESTIONS EVERYONE ASKS (2026-10-28)
 ---------------------------------------------------------------
 The strategy in one line: own the ETFs whose last ~12 months went clearly UP;
 leave the ones that stopped going up.  In charts: buy strength, quit weakness.

 Q1. WHEN DOES THE SCRIPT BUY AN ETF?   (all of these must be true)
     1. At the month-end check the ETF's last 12 months show a clear rise
        (above +2%), its price is above its 200-day average, its data is
        clean and it trades enough — and it ranks in the top 5 chosen ETFs.
     2. INVEST_PER_DAY is above 0 and today's budget is not fully spent yet
        (if you run the script twice, the second run only tops up what is left).
     3. It is STILL trending up right now — the script never buys more of a
        falling ETF; that money waits until the trend comes back.
     4. The order is sensible: at least Rs 2,000, at most 15% of what that
        ETF trades on a quiet day, not priced at a fat premium over its true
        value, and there is enough cash (with a 1% buffer for market noise).
     5. The market is not in a danger state — buys pause completely in the
        SEVERE / EXTREME safety levels.

 Q2. WHEN DOES THE SCRIPT SELL AN ETF?   (any one of these is enough)
     1. Month-end check says the trend is gone: the 12-month gain slipped to
        0 or below, OR the price fell under its 200-day average, OR the ETF
        dropped out of the top 5, OR its data looks broken.  Sold fully.
    2. Safety rules can fire ANY day, not just month-end: portfolio −15% from
       its peak warns (review), −22.5% sells half of every holding, −30%
       sells everything.  These beat the calendar by design.  (The levels are
       1x / 1.5x / 2x the 15% volatility target — edit VOL_TARGET to retune.)
     3. Small sells may wait up to a month if the holding is about to turn
        1 year old (much lower tax on the profit) — the script says so when
        it waits.
     4. An optional daily panic-exit exists (DAILY_EXIT_GUARD) but is OFF —
        testing showed it earns ~0.5% less per year (it sells dips that often
        bounce back); turn it on only if you prefer smoother but slower.

 Q3. HOW MUCH OF EACH ETF?   (decided at the month-end check)
     The 5 chosen ETFs share the buying pool.  Calmer, steadier ETFs get a
     bigger share; jumpy ones a smaller share.  Thinly-traded ETFs are capped
     so our orders stay small next to what the market trades.  The total
     amount invested is scaled so the portfolio swings about 15% a year —
     the level testing found most comfortable for returns vs sleepless nights.
     Whatever is not invested stays in cash and quietly earns interest.
     On other days, each run's buys push the portfolio a bit closer to these
     planned shares — never past them.

 Q4. HOW OFTEN?   BUYS / SELLS / THE REBALANCE
     * BUYS: every day you run the script — the budget (at least
       INVEST_PER_DAY, raised automatically when needed so the month's plan
       finishes before the next check) is spread over the chosen 5, whichever
       lags its plan most.  Risk state and trend guard can pause any or all
       of this.
     * SELLS: at each month-end check, PLUS any time the safety limits trip.
     * REBALANCE (picking the 5 ETFs + their shares): once per month, at the
       last trading day's close, executed on the next session.  If the list
       sits unused for more than 5 market days, it is re-picked fresh before
       any buy — the script never buys from an old list.
     Nothing here needs you to time anything — run the script whenever,
       even weekends; it handles the rest and never double-buys or
       double-orders an ETF.

 WHERE FILES LIVE
 ----------------
  Everything generated lives under ./stock-data/
      stock-data/tsmom.db        SQLite database (universe, prices, signals,
                                 order book, positions, tax lots, snapshots...)
      stock-data/logs/           run logs
      stock-data/cache/          downloaded instrument master, raw responses
      stock-data/reports/        saved text reports

 API KEYS
 --------
  Read from ./env.txt next to this file:
      UPSTOX_ACCESS_TOKEN=...
      UPSTOX_API_TOKEN=...
      UPSTOX_API_SECRET=...

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
    ledgered, so the slab haircut on the reinvested amount cannot be applied
    (TAX_SLAB_HAIRCUT_ON_DIVIDENDS documents intent). With raw Upstox prices
    dividends are absent entirely — price-only CAGR is UNDERSTATED, and the
    buy&hold benchmark uses the same series, so the strategy-vs-benchmark
    SPREAD stays symmetric (external audit F2.4/F9.8).
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
    snapshots into the quotes table; until then documented proxies are used
    (5 bps broad / 10 bps others, p90 x1.2 for adverse, x2.5 severe).
  * Survivorship bias: the universe is today's listed set. The backtest
    report prints an explicit survivorship caveat (real point-in-time CAGR
    would be LOWER).

 USAGE
 -----
  python etf-momentum-screener.py                # full pipeline: scan -> update -> trade -> status
  python etf-momentum-screener.py scan           # discover ETFs, show discovery dates
  python etf-momentum-screener.py update         # download / refresh price history
  python etf-momentum-screener.py trade          # one cycle (mode per PAPER_TRADE in config)
  python etf-momentum-screener.py monitor        # daily quote snapshot + risk check
  python etf-momentum-screener.py backtest       # 10y backtest, OOS split, parameter grid
  python etf-momentum-screener.py status         # portfolio + order book
  python etf-momentum-screener.py report         # full performance report
  python etf-momentum-screener.py demo           # synthetic data end-to-end demonstration
  python etf-momentum-screener.py selftest       # unit tests (momentum formula, cov, taxes...)
  python etf-momentum-screener.py live           # REAL orders via Upstox (asks y/n; danger!)

 No third-party libraries are required: standard library only.
================================================================================
"""

from __future__ import annotations

import argparse
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

# ==============================================================================
# SECTION 1 — CONFIGURATION (the full rulebook, all in one place)
# Values marked [TUNED 2026-10-18] came from 'python etf-momentum-screener.py tune' on REAL
# Upstox data (walk-forward excess vs the risk-matched benchmark; one change at
# a time; plateau rule; only improvements over baseline adopted).  Original
# rulebook values are kept in the comments.  Full ablation:
# stock-data/reports/tuned-config.txt
# ==============================================================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "stock-data"
DB_PATH = DATA_DIR / "tsmom.db"
LOG_DIR = DATA_DIR / "logs"
CACHE_DIR = DATA_DIR / "cache"
REPORT_DIR = DATA_DIR / "reports"
ENV_PATH = BASE_DIR / "env.txt"

# --- Trading mode -------------------------------------------------------------
PAPER_TRADE = False                 # PAPER BY DEFAULT (user direction 2026-10-28,
                                   #   reversing the same morning's live-by-default):
                                   #   True = every run SIMULATES orders (no real
                                   #   money).  Real trading needs BOTH: this set
                                   #   False AND a y at the run's own confirmation
                                   #   question (user direction 2026-10-29:
                                   #   --confirm flag replaced by runtime y/n).
                                   #   LIVE TRUTH (user direction 2026-10-28): in
                                   #   live mode the BROKER's holdings + order book
                                   #   decide what we own — the DB only mirrors
                                   #   them; unreadable broker => no orders at all.
                                   #   In paper mode the DB is the source of truth.
LIVE_ORDER_TYPE = "LIMIT"          # audit v4 §6/#9: live BUYS go as LIMIT at
                                   #   ask+0.2% buffer — no open-ended market cost
                                   #   tail. Sells stay MARKET (exit discipline).
LIVE_LIMIT_BUFFER = 0.002          # 0.2% over the reference price for live limits
LIVE_CASH_SLEEVE = True            # audit v5 #4 (2026-10-28, user said apply):
                                   #   live idle cash is swept into the liquid ETF
                                   #   weekly (worth ~2.8pp CAGR during bear regimes
                                   #   vs earning 0; the backtest & paper mode always
                                   #   had this).  Sleeve sells happen only to FUND
                                   #   real buys.  Set False to opt out (a plain
                                   #   note about idle cash is printed instead).
STARTING_CAPITAL = 5000             # dual role (user direction 2026-10-29):
                                   # (1) paper-mode starting cash, (2) the
                                   # amount you want deployed per run —
                                   # INVEST_PER_DAY below takes its floor from
                                   # here for BOTH paper and live.  In live the
                                   # rest of the money math still reads the
                                   # broker (cash sync, audit v4 L2).

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
                                 # Backtest caveat: nav_data stores no history, so
                                 # backtests never see premiums — measured CAGRs
                                 # implicitly include buying at premium (true in 2026,
                                 # when premiums persisted).  The premium-COLLAPSE tail
                                 # cannot be backtested; R18 bounds the historical drag
                                 # of banning the six globals outright.
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
CASH_RATE_PRETAX = 0.053           # RBI 91-day T-bill ~5.30%
SLAB_RATE = 0.30                   # assumed income-tax slab for dividends/interest
STCG_RATE = 0.20                   # <= 12 months
LTCG_RATE = 0.125                  # > 12 months, above exemption
LTCG_EXEMPTION = 125_000.0         # Rs 1.25 lakh per financial year
DIV_TDS_RATE = 0.10
DIV_TDS_THRESHOLD = 10_000.0

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
LOSS_CARRY_YEARS = 8               # unused capital losses carry forward
TAX_RULES = {
    "EQUITY":    {"stcg": 0.20,   "ltcg": 0.125, "exempt": 125_000.0, "months": 12},
    "GLOBAL":    {"stcg": 0.30,   "ltcg": 0.125, "exempt": 0.0,       "months": 12},
    "HYBRID":    {"stcg": 0.30,   "ltcg": 0.30,  "exempt": 0.0,       "months": 0},
    "COMMODITY": {"stcg": 0.30,   "ltcg": 0.125, "exempt": 0.0,       "months": 12},
    "DEBT":      {"stcg": 0.30,   "ltcg": 0.30,  "exempt": 0.0,       "months": 0},
}

# --- Cost scenarios for backtests ---------------------------------------------
COST_CASES = {
    "base":    {"spread_mult": 1.0,  "spread_stat": "median"},
    "adverse": {"spread_mult": 1.0,  "spread_stat": "p90"},
    "severe":  {"spread_mult": 2.5,  "spread_stat": "p90"},
}
CASH_CASES = {
    "0%":            0.0,
    "5.30% pretax":  0.053,
    "5.30% post-tax": 0.053 * (1 - SLAB_RATE),
}

# --- Parameter grid for Step 10 ------------------------------------------------
GRID_LOOKBACKS = [126, 189, 252]
GRID_ENTRIES = [0.00, 0.01, 0.02, 0.03, 0.05]
GRID_SKIPS = [0, 21, 42]           # audit sec 2.1: skip is a tested dimension now
BASELINE_PARAMS = {"lookback": 252, "skip": 21, "entry": 0.02, "exit": 0.00}

TRADE_STRATEGY = "B"               # which version the paper trader follows
A_EQUAL_WEIGHT = True              # Version A raw weights = equal (Step 5)
ROTATION_RULE = True               # drop incumbents ranked beyond top-N when a
                                   #   stronger eligible ETF exists
                                   # [TESTED 2026-10-27] False -> True via
                                   # experiments/run_variants.py (V13): full/OOS/
                                   # shifted windows all improve vs baseline,
                                   # turnover falls 0.54x -> 0.52x
TAX_SLAB_HAIRCUT_ON_DIVIDENDS = True   # haircut dividend reinvestment by slab rate
                                       # (applies where DIV cash distributions are
                                       #  explicitly modelled; price-only adj_close
                                       #  series carry no dividend cash to haircut —
                                       #  see the dividends footnote in reports)
TAX_BUCKET_DETAIL = True       # HYBRID/GLOBAL taxed at own rules in the FY settle
                               # (external audit F2.5 — legacy lumped them as EQUITY)
TAX_ON_CASH_ACCRUAL = True     # cash carry / liquid-sleeve gains are slab-taxed at
                               # settle (external audit F2.2 — was compounding pre-tax)
SETTLE_MODE = "quarterly"      # advance-tax realism (external audit F3.10/3.11):
                               # settle YTD buckets at quarter boundaries + FY roll
                               # instead of one annual hit on the FY's first day
SETTLE_QUARTER_MONTHS = (6, 9, 12, 3)   # Jun/Sep/Dec + Mar(=FY end) settlements
SPREAD_RECALIBRATED = True     # external audit F2.3: mid-tier ETF spreads are
                               # 10-25 bps in reality; old 5/10/20 proxy tiers read
                               # too tight.  New tiers below; harness knob flips it.
INTEG_MAX_DAILY = 0.40         # series-integrity gate ceilings (F2.6 candidates —
INTEG_MAX_VOL = 1.50           #   tightened settings are run as variants, not
INTEG_MAX_MOM = 3.00           #   silently adopted)
ROTATION_RANKFIRST = False     # external audit F3.2 variant: rank order beats
                               # incumbent priority in correlation conflicts
MIN_TRADE_FRAC = 0.0           # external audit 6.4 variant: min trade = max(
                               # MIN_TRADE_VALUE, this fraction of equity)
SECTOR_CAP_FRAC = 0.0          # external audit F10 (2026-10-28 v3) EXPERIMENT:
                               # combined cap on sectoral/thematic Indian ETF
                               # weights (0 disables until backtested — do not
                               # enable on theory).  Excess stays in cash.
                                       # when distributions are loaded as corporate
                                       # actions with note starting 'DIV'

# --- Audit fixes: cash proxies, selection quality, risk release ----------------
# Liquid/overnight/gilt/bond funds are CASH PROXIES: they win rankings on tiny
# vol and eat a top-3 slot. They are excluded from the ranked pool and may be
# used as the cash sleeve instead (audit bug #3).
CASH_PROXY_SYMBOLS = {"LIQUIDCASE", "LTGILTBEES", "EBBETF0430"}
ONE_PER_INDEX_FAMILY = True        # original rulebook rule
PAIRWISE_CORR_CAP = 0.70           # skip candidate if 120-day correlation with a
                                   #   selected ETF exceeds the cap (0 disables)
                                   # [TESTED 2026-10-27] 0.80 -> 0.70 via
                                   # experiments/run_variants.py on real data
                                   # (V0 vs V4/V13: CAGR 12.03->13.26, Sharpe
                                   # 0.70->0.84, MaxDD -17.8%->-13.6%, wins all
                                   #   5 windows incl. OOS; 0.75/0.65/0.60 worse)
CORR_WINDOW = 120                  # correlation lookback window in sessions
RANK_BY = "momentum"               # "momentum" or "risk_adjusted" (momentum/vol)
EXIT_OVERLAY_200DMA = True        # 200-day filter on ENTRIES AND EXITS (audit 2.5:
                                   #   applied to both so it cannot force round trips)
ENTRY_OVERLAY_MOM_FLOOR = None    # audit v5 #6: None = strict — ANY price below
                                   #   the 200-DMA skips a fresh entry.  A float
                                   #   would let a STRONG trend through a brief
                                   #   dip.  [MEASURED 2026-10-28, R13A/R13B:
                                   #   floor 0.05 -> CAGR 11.03 (OOS 11.03),
                                   #   MaxDD -21.5%; floor 0.10 -> 10.81 (OOS
                                   #   11.03), MaxDD -19.6%; both WORSE than strict
                                   #   everywhere.  The dip-skip IS the protection
                                   #   — rejects stay off by numbers, not theory.]
RISK_RELEASE_DAYS = 92             # release 15%/20% risk locks after ~3 months even
                                   #   without price recovery (peak resets)
CASH_ACCRUAL = True                # paper account earns the cash benchmark
CASH_SLEEVE_MODE = "LIQUID_ETF"    # "LIQUID_ETF": sleeve earns the real liquid
                                   #   ETF's returns; "TBILL_FLAT": flat proxy
CASH_SLEEVE_SYMBOL = "LIQUIDCASE"  # the liquid fund that represents the sleeve

# --- Tuning knobs (each has a default = current/conservative behaviour) --------
NO_TRADE_BAND = 0.20               # skip rebalancing a position when |tgt-have| /
                                   #   max(tgt,have) < band (exits ALWAYS trade)
MIN_TRADE_VALUE = 2_000.0          # skip dust trades; brokerage makes them poor value
MIN_LIQ_WEIGHT = 0.10              # audit 2.6: 15% x p25 must allow at least this
                                   #   fraction of portfolio equity, else no ranking
ENSEMBLE_LOOKBACKS = None          # None = single LOOKBACK; e.g. (63,126,252) ensemble
VOL_BLEND_252 = 0.30               # 0..1 weight of the 252d vol blended into 60d vol.
                                   #   [MEASURED 2026-10-28, audit v5 #8 /
                                   #   experiments/run_variants.py R13C on current
                                   #   data] 0.30: CAGR 11.80->12.46, Sharpe
                                   #   0.69->0.74, OOS CAGR 11.71->12.87, MaxDD
                                   #   flat (-14.29->-14.54).  0.50 was better OOS
                                   #   but worse Sharpe/MaxDD -> plateau rule picked
                                   #   the simpler 0.30.
REBALANCE_FREQ = "M"               # "M" monthly or "Q" quarterly (month-ends only)
REGIME_SCALING = True             # halve vol target when benchmark < 200-day average
                                   #   [RE-CONFIRMED 2026-10-28 on shifted regime:
                                   #   OFF -> CAGR 11.66, Sharpe 0.62, MaxDD -18.66,
                                   #   OOS 9.20 — protection is earning its keep]
EXECUTION_STYLE = "MARKET"         # "MARKET" (cross half-spread) or "LIMIT_MID"
                                   #   (rest at mid: impact only, paper-fill test)
LOT_SELECTION = "FIFO"             # "FIFO" or "HIFO" (HIFO realises least gain)
TAX_DEFER_EXITS = False            # tax-aware exit deferral hypothesis (explicit rule)
TAX_DEFER_MONTHS = 1               # ... defer only if lot is within N months of LTCG
TAX_DEFER_MOM_FLOOR = -0.02        # ... and momentum is only slightly negative

# --- audit v10 (external audit, 2026-10-28) — three new EXPERIMENT knobs -----
# All default False.  A default flips ONLY if the variant harness measures a
# clear win on the current binary (experiments/results_variants.json R20 rows,
# full + OOS windows); no theory-only changes.
SEAT_FILL_RELAXED = False          # fill empty basket seats from next-ranked names,
                                   # relaxing ONLY the correlation cap (family cap
                                   # and trend/liquidity gates still apply) — the
                                   # auditor's lever for the 3-of-5-seats problem
PARK_BENCH_TREND = False           # park the plan's uninvested weight in NIFTYBEES
                                   # while NIFTYBEES itself passes trend+overlay,
                                   # instead of leaving it in plain cash
DD_SLEEVE_SCALE = False            # scale drawdown triggers by the CURRENT invested
                                   # fraction (floor 0.35) — a half-cash book no
                                   # longer needs a ~43% holdings crash to 'halve'
AUTOFILL_MAX_SESSIONS = 3          # audit v12 leak-3 (2026-10-29), measured R22
                                   # on the live-box snapshot: autofill spread the
                                   # plan over ~21 sessions (11.51% -> 5.55% CAGR,
                                   # 69% -> 33% invested).  Capping the spread at 3
                                   # sessions recovers to 10.53%; OOS 13.03 vs
                                   # 13.62 (inside noise).  W4-W6 independently:
                                   # ~1pp CAGR per 10 sessions of lag.
REPLAN_TRICKLE_DAYS = 3            # near-replan guard: with the next month-end
                                   # re-plan this many market sessions away or fewer,
                                   # deploy only the INR floor (no autofill lift) —
                                   # a basket bought 1-2 sessions before it re-ranks
                                   # risks instant round-trip fees + STCG churn for
                                   # ~0.2pp of lag saved (exec-lag W4-W6 math)

# --- Daily deployment (user direction 2026-10-28 — the ONE master switch) ------
# INVEST_PER_DAY is the ONLY money that ever buys.  Every run deploys up to
# this amount across the target-plan gaps (each name's slice is PROPORTIONAL
# to how far it is below its target weight — the routing measured in the
# round-5 tranche A/B).  Every buy still faces: risk-state pause, trend
# guard (no accumulating what the rules would exit), the pre-trade
# execution gate (liquidity/spread/iNAV premium), minimum-ticket size and
# available cash.  Re-runs are safe: the journal tracks what today deployed.
#
#   INVEST_PER_DAY = 0   -> analysis + exits only.  No accumulation buys EVER.
#                           Exit proceeds pile up in cash; the invested
#                           fraction decays to zero over time.  (deliberate)
#
# The month-end rebalance is SELL-ONLY (SIP_MODE stays True): exits per the
# backtested rulebook (momentum <= 0, 200-DMA overlay, top-N rotation,
# data-integrity gate).  Whether a sale's proceeds are re-deployed is
# entirely INVEST_PER_DAY's decision — pure_amount semantics (user choice).
INVEST_PER_DAY = STARTING_CAPITAL  # rupees per RUN, journaled per calendar day.
                                   # user direction 2026-10-29: it tracks
                                   # STARTING_CAPITAL — one number to edit for
                                   # "how much do I invest each run".  audit
                                   # v11 P1-3 note (when floor < MIN_TRADE_VALUE
                                   # x MAX_HOLDINGS): a floor smaller than the
                                   # basket x min-order buys ONE name per run —
                                   # by design the trickle concentrates on the
                                   # largest gap, and the basket completes over
                                   # a few runs.  The AUTOFILL pace (budget =
                                   # max(floor, gap / sessions-to-re-plan)) is
                                   # what deploys the plan fast when the month
                                   # is young.
                                   #   FLOOR, not a hard cap (user choice 2026-10-28):
                                   #   if the plan cannot fill before the next
                                   #   re-plan at this floor, today's budget rises to
                                   #   pool_gap / market-days-left (audit v4 §2: flat
                                   #   pacing lagged the backtest ~180 sessions ≈ big
                                   #   CAGR drag). Set 0 = analyse+exit-only mode.
SIP_MODE = True                    # month-end rebalance is sell-only
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
                                   #   (mirrors the backtester's refresh_late knob)
SIP_RISK_PAUSE = ("SEVERE", "EXTREME")   # daily buys paused in these risk states
QUOTE_MAX_DEV = 0.03               # live LTP may deviate max 3% from the last
                                   #   close before the fill falls back to close
SIP_TREND_GUARD = True             # daily buy pauses for a name whose EXIT rules
                                   #   currently fire (momentum <= exit, or below
                                   #   the 200-DMA) — never accumulate what the
                                   #   strategy itself would exit
# DAILY_EXIT_GUARD — user ask 2026-10-28 ("exit losing holdings immediately,
# not at the rebalance").  Backtested BEFORE enabling (R12A_daily_exit):
# full 11.25%/0.67/-12.78 MaxDD/OOS 10.25 vs month-end exits 11.80%/0.69/
# -14.29/OOS 11.71 — immediate exits COST ~0.5pp CAGR (whipsaw re-entries) for
# ~1.5pp better MaxDD.  Numbers-first rule => default False; flip to True here
# if you knowingly prefer the smoother-but-slower profile.
DAILY_EXIT_GUARD = False

# --- Drawdown thresholds SCALE WITH THE VOL TARGET (audit: tuning section) -----
DD_REVIEW_MULT = 1.00              # e.g. 15% target -> 15% review
DD_SEVERE_MULT = 1.50              # ...            -> 22.5% halve
DD_EXTREME_MULT = 2.00             # ...            -> 30% cash

def dd_thresholds(vol_target: float = None):
    vt = VOL_TARGET if vol_target is None else vol_target
    return (DD_REVIEW_MULT * vt, DD_SEVERE_MULT * vt, DD_EXTREME_MULT * vt)


# --- Upstox API ----------------------------------------------------------------
UPSTOX_BASE = "https://api.upstox.com/v2"
UPSTOX_INSTRUMENTS = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
HTTP_SLEEP = 0.35                  # politeness delay between API calls
FETCH_CHUNK_DAYS = 1000            # historical fetch chunk; auto-halves on error
HTTP_RETRIES = 3

# ==============================================================================
# SECTION 2 — SMALL UTILITIES: dates, numbers, Indian money formatting
# ==============================================================================

TODAY = dt.date.today()


def now_iso() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return dt.date.today().isoformat()


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
        # audit v14 §3 (2026-10-30): "Paper-Trade Edition" sat under a LIVE
        # mode line — misleading.  The edition now follows the actual mode.
        _edition = "Paper-Trade Edition" if PAPER_TRADE else "LIVE-Trading Edition"
        self.raw(paint(f"  ||  {_edition}  |  Upstox Data  |  SQLite Journal", _C.CYAN, _C.BOLD))
        self.raw(paint("  " + "=" * (w - 4), _C.CYAN, _C.BOLD))
        self.raw(paint(f"  Today: {TODAY.strftime('%A, %d %B %Y')}   Mode: "
                       + ("PAPER TRADING (no real money)" if PAPER_TRADE else "LIVE TRADING"),
                     _C.YELLOW if PAPER_TRADE else _C.RED, _C.BOLD))
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

    def gauge(self, frac: float, width: int = 30, label: str = ""):
        frac = clamp(frac, 0.0, 1.0)
        filled = int(round(frac * width))
        bar = "█" * filled + "░" * (width - filled)
        self.raw("  [" + paint(bar, _C.GREEN if frac < 0.7 else _C.YELLOW) + f"] {frac*100:5.1f}%  {label}")


P = Printer()


# ==============================================================================
# SECTION 4 — LOGGING (everything under stock-data/logs/)
# ==============================================================================

_LOG_FILE = None


def setup_logging() -> Path:
    global _LOG_FILE
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_FILE = LOG_DIR / f"tsmom_{TODAY.strftime('%Y%m%d')}_{dt.datetime.now().strftime('%H%M%S')}.log"
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
    order_type TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    qty REAL, avg_cost REAL, updated TEXT);
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
CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, label TEXT, params TEXT, results TEXT, notes TEXT);
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
        self.conn.commit()

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


def is_sector_thematic(symbol: str) -> bool:
    """Indian sectoral/thematic exposure: EQUITY category outside the broad
    index families (audit F10 v3 — a basket can be 100% of these and still
    pass every family/correlation cap).  Used by the SECTOR_CAP_FRAC knob."""
    info = universe_map().get(symbol, {})
    return (info.get("category") == "EQUITY"
            and info.get("index_family") not in BROAD_FAMILIES)


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
    def _request(self, url: str, method: str = "GET", body=None, timeout=30):
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
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
        for attempt in range(HTTP_RETRIES):
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
                    if attempt == HTTP_RETRIES - 1:
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
        raise UpstoxError(f"Upstox request failed after {HTTP_RETRIES} tries: {last_err}")

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
        """
        Broker-side DAY POSITIONS (audit 2.9).  Upstox's /v2/portfolio/
        short-term-positions lists today's day-book positions and is tried
        first; the demat HOLDINGS endpoint is UNCONFIRMED for this app, so
        holdings stay ledger-based and `reconcile` is the safety net.
        """
        for path in ("/v2/portfolio/short-term-positions",
                     "/portfolio/positions", "/user/get-positions"):
            try:
                resp = self._request(f"{UPSTOX_BASE}{path}")
                data = resp.get("data")
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    return data.get("positions") or []
                return []
            except Exception:
                continue
        return []

    def funds_available(self):
        """Broker's REAL available cash (audit v4 L2): Upstox
        /user/get-funds-and-margin, equity segment -> available_margin as a
        float, or None on any failure.  The ledger cash was hard-started at
        STARTING_CAPITAL and never reconciled with the broker — live sizing
        sized against a constant.  This call fixes the source of truth."""
        for path in ("/user/get-funds-and-margin?segment=SEC",
                     "/v2/user/get-funds-and-margin?segment=SEC"):
            try:
                resp = self._request(f"{UPSTOX_BASE}{path}")
                data = resp.get("data") or {}
                eq = data.get("equity") or data.get("SEC") or data
                for key in ("available_margin", "availableMargin", "available"):
                    v = eq.get(key)
                    if v is not None:
                        return float(v)
            except Exception:
                continue
        return None

    def holdings(self):
        """Broker-side DELIVERY holdings (long-term portfolio) — the demat
        truth in live mode (user direction 2026-10-28: in live the BROKER is
        the source of truth, not the local DB).
        Raises on total failure so callers can fail CLOSED (never trade
        blind); returns [] when the book is truthfully empty."""
        last = None
        for path in ("/v2/portfolio/long-term-holdings",
                     "/portfolio/long-term-holdings"):
            try:
                resp = self._request(f"{UPSTOX_BASE}{path}")
                data = resp.get("data")
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    return data.get("holdings") or []
                return []
            except Exception as e:
                last = e
                continue
        raise RuntimeError(f"holdings fetch failed on all endpoints: {last}")

    def orders_book(self):
        """Broker-side TODAY'S ORDER BOOK — every order placed today with its
        broker-side status and traded quantity.  Same truth contract as
        holdings(): raise on failure, [] when empty."""
        last = None
        for path in ("/v2/order/retrieve-all", "/order/retrieve-all"):
            try:
                resp = self._request(f"{UPSTOX_BASE}{path}")
                data = resp.get("data")
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    return data.get("orders") or []
                return []
            except Exception as e:
                last = e
                continue
        raise RuntimeError(f"order book fetch failed on all endpoints: {last}")

    # ---------- live order (only used when PAPER mode is OFF) ----------
    def place_order(self, symbol: str, instrument_key: str, side: str,
                    qty: int, price: float, order_type: str = "MARKET") -> dict:
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
            "is_amo": False,
        }
        return self._request(f"{UPSTOX_BASE}/order/place", method="POST", body=body)


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
            # membership for long backtests
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
                           f"or after a data restore.  It only means backtest "
                           f"headline CAGR may read 3-5% optimistic until the "
                           f"journal has aged about a year; live trading is "
                           f"unaffected.  Tip: when backing up, copy the whole "
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


def _http_text(url: str, timeout: int = 20) -> str:
    """Text-only wrapper around _http_get ('' on failure, diagnostics dropped)."""
    return _http_get(url, timeout)[0]


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
    today = today_str()
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
            return (dt.datetime.now()
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
    min_date = date_str(dt.date.today() - dt.timedelta(days=NAV_HISTORY_DAYS))
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
        (regime, cash sleeve) matching the backtester's view of history."""
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
                    P.note("Update UPSTOX_ACCESS_TOKEN in env.txt (it expires daily), "
                           "or run 'python etf-momentum-screener.py demo' for an offline trial.")
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
    """
    Production momentum signal (audit 3.2).  With ENSEMBLE_LOOKBACKS set
    (e.g. (63, 126, 252)) this is the MEAN of the component lookbacks —
    a smoother signal; with None it is exactly the single LOOKBACK rule.
    Components without enough history are ignored; None if none available.
    """
    lookbacks = ENSEMBLE_LOOKBACKS or (lookback,)
    vals = [m for m in (momentum_at(adj, i, lb, skip) for lb in lookbacks)
            if m is not None]
    return (sum(vals) / len(vals)) if vals else None


def market_sessions_to(db, frm: str, to: str) -> int:
    """COUNT of downloaded market sessions in (frm, to] — audit v10: the old
    calendar-days x 5/7 guess mis-counted holidays and long weekends."""
    r = db.one("SELECT COUNT(DISTINCT date) AS n FROM price_daily "
               "WHERE date>? AND date<=?", (frm, to))
    return int((r or {"n": 0})["n"] or 0)


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


def offer_cash_sweep(db) -> None:
    """audit v12 return-leak-4 (2026-10-29 live audit): declining the strategy
    confirmation also skipped the risk-free LIQUIDCASE parking — an 'n' for
    orders is not an 'n' for earning the liquid rate on idle cash.  Offer the
    sweep as its OWN separately-confirmed action on any declined live run."""
    if PAPER_TRADE or not LIVE_CASH_SLEEVE:
        return
    try:
        cyc = TradingCycle(db)
        if not cyc.broker.sync_live_cash():
            P.warn("Cash sweep skipped — could not read the broker's cash "
                   "balance today.")
            return
        surplus = cyc.broker.cash() - 2.0 * INVEST_PER_DAY
        if surplus < 2 * MIN_TRADE_VALUE:
            return          # nothing meaningful to park; stay quiet
        if live_order_ok(f"Cash parking only: sweep idle cash "
                         f"(~{fmt_cr(surplus)}) into {CASH_SLEEVE_SYMBOL} — "
                         f"liquid fund, sold back automatically when buys "
                         f"need cash. No strategy orders will be placed by "
                         f"this confirmation."):
            cyc.sleeve_sweep()
    except Exception as e:
        P.warn(f"Cash-sweep check failed ({e}) — skipped; nothing else "
               f"affected.")


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
    now = now or dt.datetime.now()
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
    sessions left, the near-replan trickle fired on every run, and the
    autofill pace (audit v4, measured W4-W6) was unreachable all month."""
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


def deploy_sessions_left(days_left) -> int:
    """Audit v4 L6 (2026-10-28): never lift the daily budget to the whole plan
    in fewer than 3 market days — a one-day blast concentrates entry timing
    (whole-plan lump risk).  None/garbage counts as 0 -> 3.
    Audit v5 #5 proposed stretching to a fixed ~20-session window instead —
    REJECTED by measurement: exec-lag costs ~1pp CAGR per 10 sessions
    (experiments W4–W6), so deliberately adding ~17 sessions of lag would
    cost MORE (~1.5pp CAGR) than the entry-timing concentration it claims to
    avoid.  Fast-fill-to-plan stays; this floor only kills the degenerate
    1-day blast."""
    try:
        return max(3, int(days_left))
    except Exception:
        return 3


def live_buy_order_px(px: float):
    """(order_type, price) for a BUY slice.
    Audit v4 #9: live buys go as band-limited LIMIT orders at
    px x (1 + LIVE_LIMIT_BUFFER) so an ill-liquid market order can't tail
    an open-ended cost.  Paper mode stays MARKET (fills are modelled)."""
    if (not PAPER_TRADE) and LIVE_ORDER_TYPE == "LIMIT":
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
    slack.  Whatever can't fill today waits — tomorrow's autofill budget
    re-prices it."""
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


def autofill_budget(floor: float, pool_gap: float, sessions_left: int) -> float:
    """Today's deployment budget (user choice 2026-10-28, audit v4 §2):
    never below the INVEST_PER_DAY floor, but if the remaining plan cannot
    fill before the next re-plan at that floor, raise today's budget to
    pool_gap / market-days-left so the plan actually lands inside its own
    signal window.  Flat floor was measured ~1pp CAGR per 10 sessions of lag."""
    try:
        s = max(1, int(sessions_left))
    except Exception:
        s = 1
    return max(float(floor or 0.0), float(pool_gap or 0.0) / s)


def deploy_budget_remaining(meta_val: str, today: str, amount: float):
    """(already_deployed, remaining) for the per-day allocation budget, from
    the DEPLOY_DAY_KEY journal value.  Same-day partial deploys top up;
    a new day resets the budget; garbage/absent journals read zero."""
    try:
        d = json.loads(meta_val or "{}")
    except Exception:
        d = {}
    already = float(d.get("deployed", 0.0)) if d.get("date") == today else 0.0
    return already, max(0.0, float(amount or 0.0) - already)


def exit_rules_fire(adj):
    """The strategy's per-holding exit trigger as a pure function over an
    adjusted-close list (mirrors the backtester's sell condition and the R12A
    daily-exit kernel): momentum <= EXIT_THRESHOLD at the last bar, or the last
    close below its 200-DMA when EXIT_OVERLAY_200DMA is on.  False when the
    history is too short to judge.  Kept in step with backtester/sip guard."""
    if not adj or len(adj) < LOOKBACK + 1:
        return False
    t = len(adj) - 1
    mom = signal_momentum(adj, t)
    if mom is not None and mom <= EXIT_THRESHOLD:
        return True
    if EXIT_OVERLAY_200DMA and len(adj) >= 200:
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
    stats_at() passes raw SQLite rows (date, adj_close, close, traded_value)
    while the backtester builds (date, adj) pairs — never assume 2 columns.
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
    prev_date = None
    for r in rows:
        try:
            d = r["date"] if isinstance(r, sqlite3.Row) or isinstance(r, dict) else r[0]
            a = r["adj_close"] if isinstance(r, sqlite3.Row) or isinstance(r, dict) else r[1]
        except Exception:
            continue
        if prev is not None and prev and a and prev > 0:
            out[d] = a / prev - 1.0
        prev, prev_date = a, d
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
    today_d = parse_date(today) if today else dt.date.today()
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


class SignalBook:
    """One symbol's snapshot at a signal date."""

    def __init__(self, symbol, name, category, index_family, momentum, vol60,
                 liq_p25, spread_bps, bars_ok, decision, reason):
        self.symbol = symbol
        self.name = name
        self.category = category
        self.index_family = index_family
        self.momentum = momentum
        self.vol60 = vol60
        self.liq_p25 = liq_p25
        self.spread_bps = spread_bps
        self.bars_ok = bars_ok
        self.decision = decision      # ELIGIBLE_BUY / HOLD / EXIT / CASH / SKIPPED
        self.reason = reason


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
    t = parse_date(today) if today else dt.date.today()
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
            return date_str(dt.datetime.fromtimestamp(x).date())
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

    def order_value_ok(self, symbol: str, as_of: str, intended_value: float):
        """intended_order_value <= 15% x p25_daily_value (binding sizing rule)."""
        p25 = self.liquidity_p25(symbol, as_of)
        limit = POS_CAP_LIQ_FRAC * p25
        ok = intended_value <= limit
        return ok, p25, limit

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
        """Ranking key (audit sec 2.3): raw momentum, or momentum/vol."""
        mom = st["momentum"] if st["momentum"] is not None else -9.0
        if RANK_BY == "risk_adjusted":
            vol = st["vol"] if st["vol"] and st["vol"] > 0 else 1e-6
            return mom / vol
        return mom

    def select(self, stats_list: list, holding_symbols: set, strategy: str,
               entry: float = ENTRY_THRESHOLD, exit_thr: float = EXIT_THRESHOLD,
               equity: float = 0.0):
        """
        Step 5 selection.
          A: Pure TSMOM — every ETF with momentum > entry  (no holding cap)
          B: TSMOM + ranking — top MAX_HOLDINGS by momentum among qualifying
          C: Ranking only — top MAX_HOLDINGS by momentum, threshold ignored
        Filters applied before ranking (audit bugs #3/#4):
          - cash-proxy ETFs (liquid/overnight/gilt/bond) are never ranked
          - a full sleeve (15% x p25) must be >= MIN_LIQ_WEIGHT of equity
          - pairwise 120-day correlation cap against already-chosen ETFs
          - one ETF per index family (rulebook rule, optional)
        Incumbency: governed by the ROTATION rule — incumbents fall out only
        when their momentum rank decays beyond the rotation window (the old
        bounded bonus knob was retired; it was dead under rotation, F8 v3).
        """
        gate_eq = equity if equity and equity > 0 else CAPITAL   # sane default
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
                if EXIT_OVERLAY_200DMA and st.get("rows"):
                    # audit 2.5: overlay on ENTRIES and EXITS in the live path too
                    # (hotfix: rows may be 4-col SQLite rows, not (date, adj) pairs;
                    #  semantics identical to the backtester's overlay)
                    seg = [row_adj(r) for r in st["rows"]][-200:]
                    below = bool(seg) and seg[-1] < sum(seg) / len(seg)
                    if below and st["symbol"] in holding_symbols and dec in ("HOLD",):
                        dec, reason = "EXIT", ("exit overlay: price below the "
                                               "200-day average")
                    elif below and dec == "ELIGIBLE_BUY":
                        # audit v5 #6: with a momentum floor set, a STRONG trend
                        # in a brief 200-DMA dip is still buyable
                        if not (ENTRY_OVERLAY_MOM_FLOOR is not None
                                and (st["momentum"] or 0.0) >= ENTRY_OVERLAY_MOM_FLOOR):
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

        if strategy == "A":
            pool_a = sorted(eligible + holds, key=self.rank_metric, reverse=True)
            chosen = self._corr_cap(pool_a, MAX_HOLDINGS * 10)   # no holding cap
        else:
            pool = list(stats_list if strategy == "C" else (eligible + holds))
            pool = [st for st in pool if st["momentum"] is not None
                    and st["symbol"] not in CASH_PROXY_SYMBOLS
                    and self.filters.tradable_enough(st.get("liq_p25", 0.0), gate_eq)]
            pool.sort(key=self.rank_metric, reverse=True)
            self.last_pool = list(pool)
            chosen = self._pick_with_incumbency(pool, holding_symbols, MAX_HOLDINGS)

        if ROTATION_RULE and strategy in ("B", "C"):
            chosen = self._apply_rotation(pool,   # (F13: redundant ternary removed)
                                          holding_symbols, MAX_HOLDINGS)
        # audit v10: corr-relaxed seat fill (default off — pending R20I).
        # NOTE: strategy A builds pool_a, B/C build pool — the fill needs the
        # SAME pool the strict pass drew from (v10 first cut referenced `pool`
        # unconditionally -> NameError under strategy A)
        _fill_pool = pool_a if strategy == "A" else pool
        _fill_k = MAX_HOLDINGS * 10 if strategy == "A" else MAX_HOLDINGS
        chosen = self._seat_fill(chosen, _fill_pool, _fill_k)
        return {"chosen": chosen, "decisions": decisions, "exits": exits,
                "holds": holds, "eligible": eligible}

    def _pick_with_incumbency(self, ranked: list, holding_symbols: set, k: int) -> list:
        """Rank order + family/correlation caps.

        external audit F8 (2026-10-28 v3): the audit-2.4 bounded incumbency
        BONUS was applied here and then silently DISCARDED — with
        ROTATION_RULE on (the measured default), _apply_rotation replaced the
        pick entirely.  The rotation rule IS the incumbency mechanism
        (rank-first rotation was catatrophically worse — R9G).  The dead knob
        is retired; the function name is kept so call sites stay stable."""
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

    def _seat_fill(self, out: list, ranked_pool: list, k: int) -> list:
        """audit v10 (external, 2026-10-28): a strict correlation pass can leave
        basket seats EMPTY (3 of 5 on 28-Oct).  This fills the leftovers in rank
        order, relaxing ONLY the correlation cap — the one-per-family rule and
        every upstream trend/liquidity/premium gate still applied.  Default OFF;
        flips only on a measured harness win (R20 rows)."""
        if not SEAT_FILL_RELAXED or len(out) >= k:
            return out
        for st in ranked_pool:
            if len(out) >= k:
                break
            if st in out:
                continue
            fam = universe_map().get(st["symbol"], {}).get("index_family", st["symbol"])
            if ONE_PER_INDEX_FAMILY and any(
                    universe_map().get(s["symbol"], {}).get("index_family") == fam
                    for s in out):
                continue
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
        (external audit F3.2).  ROTATION_RANKFIRST=True flips that arbitration
        to strict rank order — harness variant, not silently adopted.
        """
        ranked = sorted(pool, key=self.rank_metric, reverse=True)
        rank_pos = {st["symbol"]: i + 1 for i, st in enumerate(ranked)}
        if ROTATION_RANKFIRST:
            out = []
            for st in ranked:
                if len(out) >= k:
                    break
                if (st["symbol"] in holding_symbols
                        and rank_pos.get(st["symbol"], 99) > 5):
                    continue          # rotation still drops stale incumbents
                fam = universe_map().get(st["symbol"], {}).get("index_family", st["symbol"])
                if ONE_PER_INDEX_FAMILY and any(
                        universe_map().get(s["symbol"], {}).get("index_family") == fam
                        for s in out):
                    continue
                if self._corr_ok(out + [st]):
                    out.append(st)
            return out
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

    def size(self, chosen: list, equity: float, risk_scale: float = 1.0,
             strategy: str = "B"):
        """
        Step 6 sizing:
          1. raw weights (Version A = equal-weighted per Step 5; B/C = inverse-volatility)
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
        # 1) raw weights — Step 5: Version A is equal-weighted; B/C use inverse-vol (Step 6)
        if strategy == "A":
            raw = [1.0 / n] * n
        else:
            # audit #3: same tiny-vol epsilon in live and backtest paths
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
        # SECTOR_CAP_FRAC (audit F10 v3 experiment): cap the COMBINED weight of
        # sectoral/thematic Indian ETFs; the excess stays in cash (no re-norm)
        if SECTOR_CAP_FRAC > 0:
            sec = [s for s in weights if is_sector_thematic(s)]
            tot = sum(weights[s] for s in sec)
            if tot > SECTOR_CAP_FRAC > 0:
                for s in sec:
                    weights[s] *= SECTOR_CAP_FRAC / tot
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

    External audit F2.3 (2026-10-27): the old 5/10/20 mid tiers read ~2x too
    tight vs real Indian mid/small ETF spreads (10-25 bps).  Recalibrated
    tiers below; SPREAD_RECALIBRATED=False restores the legacy table
    (experiments harness knob) so the CAGR delta is MEASURED, not asserted.
    """
    if SPREAD_RECALIBRATED:
        if liq_p25 >= 5e7:      # >= Rs 5 Cr/day p25
            base = 3.0
        elif liq_p25 >= 5e6:    # >= Rs 50 L/day
            base = 9.0
        elif liq_p25 >= 5e5:    # >= Rs 5 L/day
            base = 15.0
        else:
            base = 28.0
    else:
        if liq_p25 >= 5e7:
            base = 3.0
        elif liq_p25 >= 5e6:
            base = 5.0
        elif liq_p25 >= 5e5:
            base = 10.0
        else:
            base = 20.0
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
    if EXECUTION_STYLE == "LIMIT_MID":
        slip = impact
    else:
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
    if not TAX_BUCKET_DETAIL:
        return "EQUITY"      # legacy behaviour (pre-audit); harness knob
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


def tax_defer_blocks_exit(open_lot_dates, as_of: str, momentum: float) -> str:
    """
    TAX_DEFER_EXITS hypothesis (audit 3.3) — an EXPLICIT, testable rule:
    defer a non-risk exit when momentum is only slightly negative
    (>= TAX_DEFER_MOM_FLOOR) and the oldest still-open lot completes its
    LTCG anniversary within TAX_DEFER_MONTHS.  Returns '' to allow the exit.
    """
    if not TAX_DEFER_EXITS:
        return ""
    if momentum is not None and momentum < TAX_DEFER_MOM_FLOOR:
        return ""
    today = parse_date(as_of)
    for bd in open_lot_dates:
        anniv = ltcg_anniversary(bd, 12)
        days_left = (anniv - today).days
        if 0 < days_left <= int(TAX_DEFER_MONTHS * 31):
            return (f"tax defer: {days_left} days to LTCG anniversary ({anniv}); "
                    f"momentum {fmt_pct(momentum)} above floor "
                    f"{fmt_pct(TAX_DEFER_MOM_FLOOR)}")
    return ""


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
        """Lot match per LOT_SELECTION (audit 3.7); writes realised lots.
        Returns {realized_pnl, tax, lt_pnl, st_pnl}."""
        remaining = qty
        realized = 0.0
        st_pnl = 0.0
        lt_pnl = 0.0
        cat_row = self.db.one("SELECT category FROM etf_universe WHERE symbol=?", (symbol,))
        category = cat_row["category"] if cat_row else "EQUITY"
        order = ("buy_price DESC, buy_date" if LOT_SELECTION == "HIFO"
                 else "buy_date, id")   # HIFO realises the least gain (audit 3.7)
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
# SECTION 17 — PAPER BROKER / LIVE BROKER (order book + positions)
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
    """
    Fills orders either in PAPER mode (simulated, always default) or LIVE mode
    (real Upstox orders). Every fill lands in the orders table = order book.
    """

    def __init__(self, db: Db, tax: TaxEngine, client=None, paper: bool = PAPER_TRADE):
        self.db = db
        self.tax = tax
        self.client = client
        self.paper = paper
        # user direction 2026-10-28: in LIVE mode the broker's holdings +
        # order book are the source of truth; the DB only mirrors them.
        # _live_book None = not synced yet (sync_live_book() runs at the start
        # of every live run path, lazily on first holdings() call too).
        self._live_book = None          # {sym: {"qty": float, "avg_cost": float}}
        self._live_open = {}            # {sym: {"BUY": qty_open, "SELL": qty_open}}

    # ----- account state -----
    def sync_live_cash(self) -> bool:
        """Audit v4 L2: in LIVE mode the ledger cash is reset to the broker's
        real available margin at the start of each run.  Returns True when a
        live sync happened.  The rerun result is False (and the ledger value
        kept, loudly) when the API fails — better stale-than-blind."""
        if self.paper or self.client is None:
            return True
        try:
            avail = self.client.funds_available()
        except Exception:
            avail = None
        if avail is None:
            P.err("Could not read the broker's cash balance (funds API failed) — "
                  "keeping the ledger value. Sizes may be wrong; check Upstox "
                  "access before trusting today's orders.")
            return False
        ledger = self.cash()
        if abs(avail - ledger) > 1.0:
            P.note(f"Cash synced from the broker: ledger {fmt_cr(ledger)} -> "
                   f"Upstox available {fmt_cr(avail)}.")
        self.set_cash(avail)
        return True

    def cash(self) -> float:
        v = self.db.meta_get("cash_balance")
        return float(v) if v is not None else STARTING_CAPITAL

    def set_cash(self, v: float):
        self.db.meta_set("cash_balance", v)

    def sleeve_daily_return(self, as_of: str) -> float:
        """Per-day cash-sleeve return (audit 2.6).  LIQUID_ETF mode uses the
        real sleeve history; falls back to the flat T-bill proxy with a warning.
        Lives on Broker because Broker.accrue_cash_interest needs it (hotfix
        2026-10-19: it used to live only on TradingCycle -> AttributeError)."""
        if CASH_SLEEVE_MODE == "LIQUID_ETF":
            rows = self.db.query(
                "SELECT date, adj_close FROM price_daily WHERE symbol=? AND date<=? "
                "ORDER BY date DESC LIMIT 2", (CASH_SLEEVE_SYMBOL, as_of))
            if len(rows) == 2 and rows[1]["adj_close"]:
                return rows[0]["adj_close"] / rows[1]["adj_close"] - 1.0
            P.warn(f"{CASH_SLEEVE_SYMBOL} history missing — cash sleeve using flat "
                   f"{fmt_pct(CASH_RATE_PRETAX, 2)} (labelled proxy).")
        return (1.0 + CASH_RATE_PRETAX) ** (1.0 / 252.0) - 1.0

    def accrue_cash_interest(self):
        """Audit bug #5: the paper account must earn the cash benchmark —
        otherwise a 20% drawdown lock can literally never release.
        Audit v4 §3.4 (2026-10-28): in LIVE mode this fabricated cash the
        broker does not hold, inflating every later buy-size — skip it; live
        cash is the broker's balance."""
        if not self.paper:
            return 0.0
        if not CASH_ACCRUAL:
            return 0.0
        last = self.db.meta_get("last_interest_date")
        today = today_str()
        if last == today:
            return 0.0
        try:
            days = (parse_date(today) - parse_date(last)).days if last else 0
        except Exception:
            days = 0
        if days <= 0:
            self.db.meta_set("last_interest_date", today)
            return 0.0
        cash = self.cash()
        per_day = self.sleeve_daily_return(today)     # audit 2.6: real sleeve
        earned = cash * ((1.0 + per_day) ** days - 1.0)
        self.set_cash(cash + earned)
        self.db.meta_set("last_interest_date", today)
        self.db.meta_set("interest_earned_total",
                         float(self.db.meta_get("interest_earned_total", "0")) + earned)
        return earned

    def _book_symbol(self, raw: str) -> str:
        """Map a broker trading_symbol back onto our ETF universe symbol.
        Upstox symbols match ours on case-normalised form; a token lookup is
        the fallback for names the exchange changed."""
        sym = (raw or "").strip().upper()
        if not sym:
            return ""
        if self.db.one("SELECT symbol FROM etf_universe WHERE symbol=?", (sym,)):
            return sym
        return sym   # unknown to the universe -> adopted as-is, flagged later

    def sync_live_book(self) -> bool:
        """LIVE TRUTH SYNC (user direction 2026-10-28): in live mode the
        broker's HOLDINGS + POSITIONS + ORDER BOOK decide what we own — never
        the DB alone.  What this does, in order:
          1. pulls all three sources from Upstox;
          2. derives the current holding per symbol
             (delivery holdings + today's fills not yet settled);
          3. OVERWRITES the DB positions mirror with that truth
             (journal rows for shares the broker does not hold are dropped);
          4. records today's OPEN orders per symbol/side so no second order
             is layered on one still working at the broker;
          5. also syncs the cash from the broker's funds API (audit v4 L2).
        Returns False — and callers must place NO orders — when the broker
        cannot be read (no truth, no trading).  Paper mode: untouched, True."""
        if self.paper or self.client is None:
            return True
        try:
            hold_rows = self.client.holdings()
            pos_rows = self.client.positions()
            order_rows = self.client.orders_book()
        except Exception as e:
            P.err(f"Cannot read the broker book ({e}) — REFUSING to trade blind. "
                  f"In live mode Upstox is the source of truth: no sync, no orders. "
                  f"Check the token/network and rerun.")
            return False

        truth, openq = {}, {}
        known = {r["symbol"] for r in
                 self.db.query("SELECT symbol FROM etf_universe")}
        foreign = []

        # 1) delivery holdings: quantity + t1_quantity at average_price
        for r in hold_rows or []:
            sym = self._book_symbol(r.get("trading_symbol") or r.get("tradingsymbol")
                                    or r.get("symbol") or "")
            if not sym:
                continue
            try:
                q = float(r.get("quantity") or 0) + float(r.get("t1_quantity") or 0)
                avg = float(r.get("average_price") or r.get("avg_price") or 0)
            except (TypeError, ValueError):
                continue
            if sym not in known:
                foreign.append(sym)
                continue
            if q > 0:
                truth[sym] = {"qty": q, "avg_cost": avg}

        # 2) today's positions: buys/sells done TODAY not yet settled into
        #    holdings; blend buy price into the average
        for r in pos_rows or []:
            sym = self._book_symbol(r.get("trading_symbol") or r.get("tradingsymbol")
                                    or r.get("symbol") or "")
            if not sym or sym not in known:
                continue
            try:
                bought = float(r.get("buy_quantity") or 0)
                sold = float(r.get("sell_quantity") or 0)
                bpx = float(r.get("buy_price") or 0)
            except (TypeError, ValueError):
                continue
            net = bought - sold
            if net == 0:
                continue
            t = truth.setdefault(sym, {"qty": 0.0, "avg_cost": 0.0})
            new_q = t["qty"] + net
            if bought > 0 and bpx > 0:
                t["avg_cost"] = ((t["qty"] * t["avg_cost"] + bought * bpx)
                                 / new_q) if new_q > 0 else bpx
            t["qty"] = new_q
            if t["qty"] <= 0:
                truth.pop(sym, None)

        # 3) today's order book: open quantities per side (dup protection)
        for r in order_rows or []:
            sym = self._book_symbol(r.get("trading_symbol") or r.get("tradingsymbol")
                                    or r.get("symbol") or "")
            if not sym or sym not in known:
                continue
            st = str(r.get("status") or "").strip().lower()
            if st not in BROKER_OPEN_STATUSES:
                continue
            try:
                pend = float(r.get("quantity") or 0) - float(r.get("filled_quantity") or 0)
            except (TypeError, ValueError):
                continue
            if pend <= 0:
                continue
            side = str(r.get("transaction_type") or "").strip().upper()
            if side in ("BUY", "SELL"):
                openq.setdefault(sym, {"BUY": 0.0, "SELL": 0.0})[side] += pend

        # 4) overwrite the DB positions mirror with the broker truth
        existing = {r["symbol"]: r["qty"] for r in
                    self.db.query("SELECT symbol, qty FROM positions WHERE qty>0")}
        vanished = sorted(s for s in existing if s not in truth)
        for s in vanished:
            self.db.execute("DELETE FROM positions WHERE symbol=?", (s,))
        for sym, t in truth.items():
            self.db.execute(
                "INSERT OR REPLACE INTO positions(symbol,qty,avg_cost,updated) "
                "VALUES(?,?,?,?)", (sym, t["qty"], t["avg_cost"], now_iso()))

        self._live_book = {s: dict(t) for s, t in truth.items()}
        self._live_open = openq
        try:
            self.db.meta_set("live_open_orders", json.dumps(openq))
        except Exception:
            pass

        # plain-language report of what changed hands
        if vanished:
            P.warn("Journal cleaned to match the broker: "
                   + ", ".join(f"{s} (journal said {existing[s]:.0f}, broker holds 0)"
                               for s in vanished[:8])
                   + " — deleted from the portfolio.")
        newat = [s for s in truth if s not in existing]
        if newat:
            P.note("Broker holds ETF(s) the journal did not know — adopted: "
                   + ", ".join(f"{s} x{truth[s]['qty']:.0f}" for s in newat[:8]))
        # audit v14 P2 (2026-10-30): holdings adopted from the broker arrive
        # WITHOUT tax lots — a later sell used to journal zero P&L/tax.  Create
        # conservative lots (dated TODAY at the broker average => any profit is
        # taxed as short-term, never flattered), flag the symbol's age as
        # UNKNOWN, and block the 12-month tax-deferral carve-out for it.
        for s in newat:
            lots = self.db.one(
                "SELECT COUNT(*) AS c FROM tax_lots WHERE symbol=? AND qty>0 "
                "AND sell_date IS NULL", (s,))
            t = truth.get(s)
            if t and t["qty"] > 0 and (not lots or lots["c"] == 0):
                self.db.execute(
                    "INSERT INTO tax_lots(symbol,buy_date,buy_price,qty) "
                    "VALUES(?,?,?,?)", (s, today_str(), t["avg_cost"], t["qty"]))
                unk = set(json.loads(self.db.meta_get("tax_age_unknown", "[]")))
                unk.add(s)
                self.db.meta_set("tax_age_unknown", json.dumps(sorted(unk)))
                P.warn(f"{s}: no buy history in the journal — a conservative tax "
                       f"lot was created dated TODAY (any profit counts as "
                       f"short-term).  The 12-month tax-wait will NOT delay "
                       f"selling it.  If you know the real buy date, fix "
                       f"stock-data/tsmom.db table tax_lots.")
        if foreign:
            P.note(f"Broker holdings outside this bot's ETF universe "
                   f"({', '.join(sorted(set(foreign))[:8])}) are left untouched "
                   f"— not counted, never sold by this bot.")
        if openq:
            P.note("Open orders at the broker right now: "
                   + ", ".join(f"{s} {d['BUY']:.0f} to buy / {d['SELL']:.0f} to sell"
                               for s, d in sorted(openq.items())[:8]))
        P.ok(f"Broker book synced: {len(truth)} ETF holding(s) adopted from Upstox; "
             f"journal positions mirror updated.")
        # audit v14 P0 (2026-10-30): the funds-API failure used to be tolerated
        # here — trading continued on the STALE ledger cash (once a Rs 0.5 L
        # gap was seen on screen).  Cash unreadable -> fail-closed, no orders.
        if not self.sync_live_cash():
            P.err("Broker cash unreadable — REFUSING orders today: sizes are "
                  "computed off the real balance, and I cannot see it.  Fix the "
                  "Upstox token/network and rerun (analysis was still updated).")
            return False
        return True


    def live_book_ok(self) -> bool:
        """True when holdings decisions may trust the book.  Paper mode is
        always ok (DB is the truth there); live needs a synced broker book."""
        return bool(self.paper) or self._live_book is not None

    def live_open_qty(self, sym: str, side: str) -> float:
        """Broker-confirmed OPEN order quantity for symbol/side today (live).
        Zero in paper mode or when the book was never synced."""
        if self.paper or self._live_book is None:
            return 0.0
        return float((self._live_open.get(sym) or {}).get(side.upper(), 0.0))

    def sellable_qty(self, sym: str) -> float:
        """Holdings minus qty already committed to open SELL orders at the
        broker — the only amount that may be sold right now (live)."""
        h = self.holdings().get(sym)
        if not h:
            return 0.0
        return max(0.0, float(h["qty"]) - self.live_open_qty(sym, "SELL"))

    def holdings(self) -> dict:
        """User direction 2026-10-28: LIVE mode answers from the broker's
        book (synced at run start / lazily here); PAPER mode answers from the
        DB ledger.  If a live sync fails the run is halted before any order
        (see sync_live_book), so an empty dict here means 'truthfully flat'."""
        if not self.paper:
            if self._live_book is None:
                self.sync_live_book()
            return {s: dict(t) for s, t in (self._live_book or {}).items()}
        out = {}
        for r in self.db.query("SELECT * FROM positions WHERE qty>0"):
            out[r["symbol"]] = {"qty": r["qty"], "avg_cost": r["avg_cost"]}
        return out

    def holdings_value(self, prices: dict) -> float:
        v = 0.0
        for sym, h in self.holdings().items():
            px = prices.get(sym, h["avg_cost"])
            v += h["qty"] * px
        return v

    def equity(self, prices: dict) -> float:
        return self.cash() + self.holdings_value(prices)

    # ----- order flow -----
    def execute(self, symbol: str, side: str, qty: float, ref_price: float,
                signal_as_of: str = "", reason: str = "", spread_bps: float = 8.0,
                spread_mult: float = 1.0, order_type: str = "MARKET",
                limit_px: float = None) -> dict:
        # audit v14 P0 (2026-10-30): limit_px is the price actually SENT to the
        # broker for live LIMIT orders (deploy_day buffers it with
        # LIVE_LIMIT_BUFFER).  ref_price drives only the paper/ledger fill
        # simulation — before this fix the buffered limit price was fed back
        # through slippage_price(), doubling the slippage on every live buy.
        qty = int(qty)
        if qty <= 0:
            return {"status": "REJECTED", "reason": "quantity is zero"}
        mode = "PAPER" if self.paper else "LIVE"
        cat_row = self.db.one("SELECT category FROM etf_universe WHERE symbol=?", (symbol,))
        category = cat_row["category"] if cat_row else "EQUITY"
        liq_p25 = 0.0
        liq_row = self.db.one(
            "SELECT traded_value FROM price_daily WHERE symbol=? ORDER BY date DESC LIMIT 60",
            (symbol,))
        liq_vals = [r["traded_value"] for r in self.db.query(
            "SELECT traded_value FROM price_daily WHERE symbol=? ORDER BY date DESC LIMIT 60",
            (symbol,))]
        liq_p25 = percentile(liq_vals, LIQ_PCTL) or 0.0
        px = slippage_price(side, ref_price, spread_bps, spread_mult,
                            order_value=qty * ref_price, liq_p25=liq_p25)
        value = px * qty
        charges = calc_charges(side, value, category=category)
        # audit v14 P0: a LIVE order whose placement succeeded but whose fill
        # could NOT be verified used to keep the default FILLED and apply the
        # fill at the simulated price — fabricating positions/cash.  Live
        # orders now start UNCONFIRMED and turn FILLED only on a broker
        # "complete/filled" status; otherwise resolve_pending_live_orders()
        # settles them on the next run.
        status = "FILLED" if self.paper else "UNCONFIRMED"
        fill_note = "filled at simulated price (paper book)" if self.paper else ""
        if not self.paper and self.client is not None:
            key_row = self.db.one("SELECT instrument_key FROM etf_universe WHERE symbol=?", (symbol,))
            key = key_row["instrument_key"] if key_row else f"NSE_EQ|{symbol}"
            try:
                send_px = px
                if (order_type or "").upper() == "LIMIT" and limit_px:
                    send_px = float(limit_px)          # buffered cap, no extra slip
                resp = self.client.place_order(symbol, key, side, qty, send_px, order_type)
                oid = (resp.get("data") or {}).get("order_id", "")
                fill_note = f"live order id {oid}"
                # audit #9: reconcile the simulated fill with the broker's actual
                # fill (MARKET orders fill at the market, not at our simulation)
                try:
                    det = self.client.order_details(oid)
                    d = (det.get("data") or {})
                    avg = float(d.get("average_price") or 0)
                    if avg > 0:
                        px = avg
                        value = px * qty
                        charges = calc_charges(side, value, category=category)
                        fill_note += f"; broker avg fill Rs {avg:.2f}"
                    if d.get("status"):
                        fill_note += f"; broker status {d.get('status')}"
                        # audit 2.9: the LEDGER adopts the broker's fill status
                        bs = str(d.get("status")).strip().lower()
                        if bs in ("complete", "filled"):
                            status = "FILLED"
                        elif bs in ("open", "pending", "trigger pending", "queued"):
                            status = "PENDING"
                        elif bs in ("rejected", "cancelled", "expired"):
                            status = "REJECTED"
                        else:
                            status = "UNCONFIRMED"
                except Exception as e2:
                    fill_note += f"; could not verify fill ({e2})"
            except Exception as e:
                status = "FAILED"
                fill_note = f"live order failed: {e}"
                # audit v15 P0-4 (2026-10-30 console): Upstox delivery SELLS
                # die on "EDIS is not validated" (UDAPI100500) until the user
                # authorises CDSL TPIN/OTP — buried in a ticket, this looked
                # like a generic glitch.  Say it at volume the moment it fires.
                if side.upper() == "SELL" and ("UDAPI100500" in str(e)
                                               or "EDIS" in str(e).upper()):
                    P.err(f"SELL blocked on {symbol}: Upstox has no EDIS "
                          f"authorisation for this holding today.  "
                          f"Delivery sales need a daily CDSL EDIS check "
                          f"(TPIN/OTP) — fix once in the Upstox app "
                          f"(Profile -> Security -> Authorise sell / EDIS), "
                          f"and consider DDPI (one-time standing "
                          f"authorisation, removes the daily dance).  The "
                          f"signal stays OPEN and retries next run.")
        slip_cost = abs(px - ref_price) * qty
        self.db.execute(
            "INSERT INTO orders(ts,signal_as_of,exec_date,symbol,side,qty,price,value,"
            "slippage,charges,status,mode,order_type,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), signal_as_of, today_str(), symbol, side, qty, px, value,
             slip_cost, charges["total"], status, mode, order_type,
             (reason + " | " + fill_note).strip(" |")))
        self.db.execute(
            "INSERT INTO decisions(ts,phase,symbol,decision,reason) VALUES(?,?,?,?,?)",
            (now_iso(), "execution", symbol, f"{side} x{qty} @ Rs {px:.2f}", reason))
        if status == "FILLED":
            self._apply_fill(symbol, side, qty, px, value, charges)
        log(f"ORDER {mode} {side} {symbol} x{qty} @ {px:.2f} value={value:.0f} "
            f"charges={charges['total']:.2f} status={status}")
        return {"status": status, "price": px, "qty": qty, "value": value,
                "charges": charges, "slippage": slip_cost, "mode": mode,
                "reason": reason, "note": fill_note}

    def _apply_fill(self, symbol, side, qty, px, value, charges):
        # user direction 2026-10-28: a fill we placed THIS run must update the
        # broker-truth live book immediately (the next actual broker sync
        # still wins — Upstox stays the source of truth across runs)
        if not self.paper and self._live_book is not None:
            t = self._live_book.setdefault(symbol, {"qty": 0.0, "avg_cost": 0.0})
            if side.upper() == "BUY":
                nq = t["qty"] + qty
                t["avg_cost"] = ((t["qty"] * t["avg_cost"] + qty * px) / nq) if nq > 0 else px
                t["qty"] = nq
            else:
                t["qty"] = max(0.0, t["qty"] - min(qty, t["qty"]))
                if t["qty"] <= 0:
                    self._live_book.pop(symbol, None)
            _os = self._live_open.setdefault(symbol, {"BUY": 0.0, "SELL": 0.0})
            _os[side.upper()] = max(0.0, _os.get(side.upper(), 0.0) - qty)
        cash = self.cash()
        if side.upper() == "BUY":
            cash -= (value + charges["total"])
            row = self.db.one("SELECT qty, avg_cost FROM positions WHERE symbol=?", (symbol,))
            if row and row["qty"] > 0:
                new_qty = row["qty"] + qty
                new_avg = (row["qty"] * row["avg_cost"] + qty * px) / new_qty
                self.db.execute("UPDATE positions SET qty=?, avg_cost=?, updated=? WHERE symbol=?",
                                (new_qty, new_avg, now_iso(), symbol))
            else:
                self.db.execute("INSERT OR REPLACE INTO positions(symbol,qty,avg_cost,updated) "
                                "VALUES(?,?,?,?)", (symbol, qty, px, now_iso()))
            self.tax.record_buy(symbol, today_str(), px, qty)
        else:
            cash += (value - charges["total"])
            row = self.db.one("SELECT qty, avg_cost FROM positions WHERE symbol=?", (symbol,))
            have = row["qty"] if row else 0
            sell_qty = min(qty, have)
            self.tax.record_sell(symbol, today_str(), px, sell_qty)
            left = have - sell_qty
            if left > 0:
                self.db.execute("UPDATE positions SET qty=?, updated=? WHERE symbol=?",
                                (left, now_iso(), symbol))
            else:
                self.db.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
        self.set_cash(cash)

    def print_order_ticket(self, res: dict, symbol: str, name: str, side: str):
        if res.get("status") == "FILLED":
            icon = "\U0001F7E2" if side == "BUY" else "\U0001F534"
            verb = "BOUGHT" if side == "BUY" else "SOLD"
            P.decision(icon,
                       f"{verb} {res['qty']} units of {symbol} ({name}) at Rs {res['price']:.2f}",
                       f"amount {fmt_rs(res['value'])} | govt+broker fees {fmt_rs(res['charges']['total'])} "
                       f"| market noise cost {fmt_rs(res['slippage'])} | "
                       f"[{'paper — no real money' if res['mode'] == 'PAPER' else 'LIVE'}]",
                       _C.GREEN if side == "BUY" else _C.RED)
        else:
            # audit v4 L3: show the BROKER's message first — the strategy reason
            # is already known; what matters at rejection time is why Upstox said no
            P.decision("⚪", f"Order not done for {symbol}",
                       (res.get("note") or res.get("reason") or "not filled")
                       + (f"  (purpose: {res['reason']})"
                          if res.get("note") and res.get("reason") else ""),
                       _C.YELLOW)


# ==============================================================================
# SECTION 18 — RISK ENGINE (Step 9: drawdown triggers & re-entry)
# ==============================================================================

class RiskEngine:
    def __init__(self, db: Db, vol_target: float = None):
        self.db = db
        self.vol_target = float(vol_target if vol_target is not None else VOL_TARGET)
        self.state = db.meta_get("risk_state", "NORMAL")
        self.peak = float(db.meta_get("equity_peak", STARTING_CAPITAL))
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

    def update(self, equity: float, context: str = "",
               invested_frac: float = None) -> dict:
        """Daily monitoring of portfolio drawdown.  invested_frac (hv/eq) is used
        only when DD_SLEEVE_SCALE is on (audit v10 knob): drawdown triggers scale
        by the invested fraction so a half-cash book doesn't need a ~43%
        holdings crash to halve."""
        events = []
        # audit v12 P0-1 (2026-10-29 live audit): a PAPER-lived peak (seeded
        # from the Rs 10 L STARTING_CAPITAL) survives into LIVE mode; with a
        # Rs 3 L broker book that reads as a fabricated ~70% crash — past the
        # 30% EXTREME trigger — and would market-sell everything on the first
        # live confirmation.  The peak is meaningless across a paper/live
        # flip: rebase it (and release any derisk lock) whenever the mode
        # changes.  Stored once as meta equity_peak_mode.
        _mode_now = "PAPER" if PAPER_TRADE else "LIVE"
        _mode_stored = self.db.meta_get("equity_peak_mode", "")
        if _mode_stored != _mode_now:
            if _mode_stored:      # real flip; first-ever run just sets the tag
                P.warn(f"Money mode changed {_mode_stored}->{_mode_now}: risk "
                       f"peak rebased from {fmt_cr(self.peak)} to today's equity "
                       f"{fmt_cr(equity)} — a stale cross-mode peak would "
                       f"misread this run as a crash.")
                self.peak = max(equity, 1.0)
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
                    if (dt.date.today() - trig).days >= RISK_RELEASE_DAYS:
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
        if DD_SLEEVE_SCALE and invested_frac is not None:
            # audit v10: sleeve-aware triggers — effective thresholds shrink
            # with the invested fraction (floored at 0.35 to stop near-cash
            # noise re-triggering daily)
            _inv = max(invested_frac, 0.35)
            dd_review *= _inv
            dd_severe *= _inv
            dd_extreme *= _inv
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
             "PAPER" if PAPER_TRADE else "LIVE"))


# ==============================================================================
# SECTION 19 — TRADING CYCLE (live/paper loop: signal -> size -> execute)
# ==============================================================================

def resolve_pending_live_orders(db: Db, broker: "Broker") -> int:
    """External audit F9 (3.9): live orders that came back PENDING/UNCONFIRMED
    were journaled but never followed up — unnoticed, the next daily run
    would place fresh orders and DUPLICATE them.  Once per daily run (live
    mode only), re-poll each journaled open order against the broker and
    settle the ledger:
      complete/fill              -> apply the fill at the broker average price
      rejected/cancelled/expired -> mark REJECTED (cash untouched)
      still open                 -> leave for tomorrow's run
    Returns the number of orders resolved."""
    if broker.paper or broker.client is None:
        return 0
    try:
        rows = db.query("SELECT id, symbol, side, qty, price, value, reason "
                        "FROM orders WHERE status IN ('PENDING','UNCONFIRMED')")
    except Exception:
        return 0
    done = 0
    for r in rows:
        oid = re.search(r"live order id ([A-Za-z0-9_-]+)", r["reason"] or "")
        if not oid:
            continue
        try:
            det = broker.client.order_details(oid.group(1))
            d = (det or {}).get("data") or {}
            bs = str(d.get("status") or "").strip().lower()
        except Exception as e:
            P.note(f"Pending order {oid.group(1)} ({r['symbol']}): re-check failed "
                   f"({e}) — retrying tomorrow.")
            continue
        if bs in ("complete", "filled"):
            px = float(d.get("average_price") or 0) or r["price"]
            qty = int(d.get("filled_quantity") or d.get("quantity") or r["qty"])
            cat = universe_map().get(r["symbol"], {}).get("category", "EQUITY")
            val = px * qty
            ch = calc_charges(r["side"], val, category=cat)
            broker._apply_fill(r["symbol"], r["side"], qty, px, val, ch)
            db.execute("UPDATE orders SET status='FILLED', qty=?, price=?, value=?, "
                       "charges=? WHERE id=?",
                       (qty, px, val, ch["total"], r["id"]))
            P.ok(f"Pending live order resolved: {r['side']} {r['symbol']} x{qty} "
                 f"@ Rs {px:.2f} (broker fill applied to the ledger).")
            done += 1
        elif bs in ("rejected", "cancelled", "expired"):
            db.execute("UPDATE orders SET status='REJECTED' WHERE id=?", (r["id"],))
            P.warn(f"Pending live order {r['symbol']} came back {bs.upper()} — ledger "
                   f"marked REJECTED; the position was never applied.")
            done += 1
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
        self.broker = Broker(db, self.tax, self.client, paper=PAPER_TRADE)
        self.risk = RiskEngine(db)

    def sleeve_daily_return(self, as_of: str) -> float:
        """Per-day cash-sleeve return (audit 2.6) — Broker owns the single
        implementation (it does the interest accrual); kept callable here too."""
        return self.broker.sleeve_daily_return(as_of)

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
                                  TRADE_STRATEGY,
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
        hold_syms = {r["symbol"] for r in self.db.query(
            "SELECT symbol FROM positions WHERE qty>0")}
        # audit v14 P1 (2026-10-30): the re-derivation used to SIZE the stored
        # basket symbols directly, skipping selection filters (iNAV premium
        # block, 200-DMA overlay, correlation cap, one-per-index-family).  A
        # legacy journal could therefore size towards an ETF the selector
        # would have blocked.  Re-run the real selection and intersect.
        sel = self.builder.select(stats, hold_syms, TRADE_STRATEGY,
                                  equity=equity)
        sel_syms = {c["symbol"] for c in sel} if sel else set()
        chosen = [s for s in stats if s["symbol"] in basket
                  and s["symbol"] in sel_syms]
        dropped = sorted(set(basket) - {s["symbol"] for s in chosen})
        if dropped:
            P.warn("Stored plan names failed today's selection filters and are "
                   "excluded from sizing: " + ", ".join(dropped))
        if not chosen:
            return {}
        sized = self.builder.size(chosen, equity, risk_scale=1.0,
                                  strategy=TRADE_STRATEGY)
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

    # ---- idle-cash sleeve (audit v5 #4, live only) -----------------------
    # The backtest & paper mode park spare money in the liquid ETF;
    # leaving live cash at 0% cost ~2.8pp CAGR in the bear-regime print.
    # Two moves only: park cash weekly, sell back ONLY to fund real buys.
    # The sleeve is exempt from exits/exit-guard/derisk — it IS the cash.
    def sleeve_unsweep(self, need: float) -> float:
        """Sell just enough of the parked sleeve to fund today's buy budget."""
        if PAPER_TRADE or not LIVE_CASH_SLEEVE:
            return 0.0
        sx = CASH_SLEEVE_SYMBOL
        h = self.broker.holdings().get(sx)
        if not h or h["qty"] <= 0:
            return 0.0
        shortfall = need - self.broker.cash()
        if shortfall < 2 * MIN_TRADE_VALUE:
            return 0.0
        px = self.latest_prices().get(sx)
        if not px or px <= 0:
            P.warn(f"Cannot free cash from the {sx} sleeve — no price today.")
            return 0.0
        qty = int(min(self.broker.sellable_qty(sx), shortfall / px))
        if qty <= 0:
            return 0.0
        res = self.broker.execute(sx, "SELL", qty, px, today_str(),
                                  reason="fund today's buys from the idle-cash sleeve")
        self.broker.print_order_ticket(res, sx, "liquid/idle-cash sleeve", "SELL")
        if res.get("status") == "FILLED":
            freed = float(res.get("value") or qty * px)
            P.note(f"Sold {qty} units of the {sx} sleeve ({fmt_cr(freed)}) to "
                   f"fund today's buys — exactly what the backtest assumes "
                   f"spare money does.")
            return freed
        return 0.0

    def sleeve_sweep(self) -> int:
        """Park idle cash (beyond a 2-day budget buffer) in the liquid ETF,
        at most once a week."""
        if PAPER_TRADE or not LIVE_CASH_SLEEVE:
            return 0
        sx = CASH_SLEEVE_SYMBOL
        try:
            last = self.db.meta_get("last_sleeve_sweep", "")
            if last and (parse_date(today_str()) - parse_date(last)).days < 7:
                return 0
        except Exception:
            pass
        surplus = self.broker.cash() - 2.0 * INVEST_PER_DAY
        if surplus < 2 * MIN_TRADE_VALUE:
            return 0
        if self.broker.live_open_qty(sx, "BUY") > 0:
            P.note(f"A {sx} sweep order is already open at the broker — "
                   f"skipping today's sweep.")
            return 0
        px = self.latest_prices().get(sx)
        if not px or px <= 0:
            P.warn(f"Cannot sweep idle cash into {sx} — no price today.")
            return 0
        qty = int(surplus / px)
        if qty <= 0:
            return 0
        _otype, _px_send = live_buy_order_px(px)
        res = self.broker.execute(sx, "BUY", qty, px, today_str(),
                                  reason="weekly sweep: park idle cash in the liquid sleeve",
                                  order_type=_otype, limit_px=_px_send)
        self.broker.print_order_ticket(res, sx, "liquid/idle-cash sleeve", "BUY")
        if res.get("status") in ("FILLED", "PENDING", "UNCONFIRMED"):
            self.db.meta_set("last_sleeve_sweep", today_str())
            P.note(f"Parked {fmt_cr(qty * px)} of idle cash in {sx} (earns the "
                   f"liquid rate instead of nothing). It is sold back "
                   f"automatically whenever the day's buys need the cash.")
            return 1
        return 0

    def deploy_day(self) -> int:
        """One run of accumulation: deploy INVEST_PER_DAY across the target-plan
        gaps, slice sizes PROPORTIONAL to each name's distance from its target
        (user direction 2026-10-28 — the amount is the ONLY money that buys;
        the month-end rebalance is sell-only).  Idempotent per calendar day:
        the journal tracks today's deployed amount, not a binary flag, so a
        thinned run can top up but never double-deploy.  Runs at any hour —
        weekends/holidays deploy at the last close (user choice), loudly
        marked; guards, gates and fills unchanged from the SIP engine."""
        # audit v4 P3 (2026-10-28): live has no liquid sweep — idle cash earns
        # nothing. Backtests park leftovers in the liquid ETF; the gap is
        # inherent. Say so plainly, and offer the opt-in sweep instead of
        # silently mimicking the backtest (user decides).
        if (not PAPER_TRADE) and (not LIVE_CASH_SLEEVE):
            _idle = self.broker.cash()
            if _idle > 2 * INVEST_PER_DAY:
                P.note(f"Idle cash {fmt_cr(_idle)} earns nothing in live mode. "
                       f"Backtests park spare money in {CASH_SLEEVE_SYMBOL}. Set "
                       f"LIVE_CASH_SLEEVE=True to sweep weekly, or simply keep "
                       f"spare cash in a liquid fund at your broker.")
        P.section("TODAY'S BUYING — daily budget", "\U0001F4E6")
        basket = self.sip_basket()
        if INVEST_PER_DAY <= 0:
            if basket:
                P.kv("ETFs to buy into", ", ".join(basket))
            P.info("INVEST_PER_DAY is 0 — today the script only checks and "
                   "sells what must go; it buys nothing.  Exit money waits in "
                   "cash.  Raise the amount at the top of the script when you "
                   "want buying back on.")
            return 0
        if not basket:
            P.warn("No ETFs are chosen yet — the month-end check picks them "
                   "(today's run includes that check on a fresh start). "
                   "Nothing to buy today.")
            return 0
        P.kv("ETFs to buy into", ", ".join(basket))
        sig_date = self.db.meta_get(SIP_BASKET_DATE_KEY, "")
        # user choice 2026-10-28 (audit v4 §6): a list older than
        # STALE_REFRESH_DAYS market days is re-derived from TODAY's data
        # before any buy — never accumulate from a stale list
        if sig_date and sig_date != today_str():
            _stale_b, _age = signal_is_stale(self.db, sig_date)
            if _stale_b:
                P.note(f"The ETF list is {_age} market-days old "
                       f"(limit {STALE_REFRESH_DAYS}) — re-picking the list and "
                       f"plan from TODAY's data first. Out-of-list holdings get "
                       f"sold by the same run.")
                try:
                    # audit v5 #11: quiet — the discarded basket's tables are
                    # not re-printed over the fresh one
                    self.run(force_exec=True, as_of_override=today_str(), quiet=True)
                except Exception as _e:
                    P.warn(f"Fresh re-pick failed ({_e}) — falling back to the "
                           f"older list for today.")
                basket = self.sip_basket()
                sig_date = self.db.meta_get(SIP_BASKET_DATE_KEY, "") or sig_date
                if not basket:
                    P.warn("After the fresh re-pick nothing qualifies — no buys "
                           "today.")
                    return 0
        if sig_date:
            P.kv("List chosen on", f"{sig_date} (re-picked at the "
                 f"{next_month_end_hint()} month-end, or sooner if it goes stale)")
        if not sip_buys_allowed(self.risk.state):
            P.warn(f"Market safety level is {self.risk.state} — no new buying "
                   f"until the danger passes.")
            return 0
        prices = self.latest_prices()
        qmap, qspread, tflag = self._live_quotes(basket)
        session_today = None
        for f in tflag.values():
            if f is not None:
                session_today = f
                break
        if session_today is False:
            P.note("The market is closed today — the script still buys (at the "
                   "last traded price, simulated).  That is intended for paper "
                   "mode; be cautious doing this in live mode.")
        elif session_today is None:
            if parse_date(today_str()).weekday() >= 5:
                P.note("Weekend — buying at the LAST traded price (simulated).")
            else:
                try:
                    _hols = set(json.loads((BASE_DIR / "nse_holidays.json").read_text()))
                except Exception:
                    _hols = set()
                if today_str() in _hols:
                    P.note("Market holiday — buying at the LAST traded price "
                           "(simulated).")
                else:
                    P.warn("Could not confirm the market is open — assuming it "
                           "is (weekday).")
        if not qmap:
            P.warn("No live prices — buys are simulated at the last traded "
                   "price.")
        # per-day budget idempotency (replaces the binary last_sip_date lock,
        # added after the 2026-10-27 double-buy): amount-keyed so a thinned
        # morning run can top up instead of deadlocking the whole day
        dlog = {}
        try:
            dlog = json.loads(self.db.meta_get(DEPLOY_DAY_KEY, "") or "{}")
        except Exception:
            dlog = {}
        already = float(dlog.get("deployed", 0.0)) if dlog.get("date") == today_str() else 0.0
        # ---- budget deployment setup (INVEST_PER_DAY mode, 2026-10-28) ------
        eq0 = self.broker.equity(prices)
        holdings0 = self.broker.holdings()
        targets = self.sip_targets(basket, eq0)
        if not targets:
            P.warn("No target weights stored — run the monthly rebalance "
                   "(or 'trade --force-exec') to build the deployment plan. "
                   "No buys today.")
            return 0
        sig_d = self.db.meta_get(SIP_BASKET_DATE_KEY, "")
        P.info(f"Today's floor budget: {fmt_rs(INVEST_PER_DAY)} per run "
               f"(raised automatically if the plan can't fill in time).  The "
               f"plan is to hold about {fmt_cr(sum(targets.values()) * eq0)} in "
               f"ETFs ({fmt_pct(sum(targets.values()), 1)} of your money); the "
               f"rest stays in cash.")
        # Late-start transparency: fresh journals mid-cycle still say it plainly
        if sig_d:
            age = (self.db.one(
                "SELECT COUNT(DISTINCT date) AS n FROM price_daily "
                "WHERE date>? AND date<=?", (sig_d, today_str())) or {"n": 0})["n"]
            if age >= 10:
                try:
                    bdays = max(0, round((parse_date(next_month_end_hint())
                                          - parse_date(today_str())).days * 5 / 7))
                except Exception:
                    bdays = 0
                P.note(f"Heads up: this ETF list is {age} market-days old; the "
                       f"next month-end check is ~{bdays} market-days away "
                       f"({next_month_end_hint()}) and may pick a fresh list.  "
                       f"Today's buys still follow the CURRENT list, so nothing "
                       f"is wasted — falling names were already skipped above.")
        # ---- two-phase deployment: measure all gaps, then allocate the
        #     remaining budget PROPORTIONALLY to them (user choice 2026-10-28),
        #     with up to 3 redistribution passes for cap/gate leftovers --------
        # audit v4 §8.7: a name failing a HARD check (broken data / iNAV
        # premium) for DEPLOY_HARD_BLOCK_DAYS in a row drops OUT of the stored
        # plan until the next month-end re-plan — otherwise every run keeps
        # burning a SKIPPED row on a name that can never fill
        try:
            hard_blocks = json.loads(self.db.meta_get(DEPLOY_SKIP_KEY, "") or "{}")
        except Exception:
            hard_blocks = {}
        plan = {}                 # sym -> {"px":, "gap":} after all guards
        parked = []               # symbols whose buys the trend guard blocked
        hard_dropped = []         # names past the consecutive hard-block limit
        for sym in basket:
            try:
                # series() returns SQLite rows — flatten to plain floats for
                # both the integrity gate and the trend-guard math below
                raw = self.data.series(sym, today_str(), 300, "adj_close") or []
                adj = [float(r) if isinstance(r, (int, float)) else float(row_adj(r))
                       for r in raw]
                issue = series_integrity_issue(adj)
            except Exception:
                adj = []
                issue = ""
            if issue:
                P.warn(f"Not buying {sym} — its price records look broken "
                       f"({issue}); the script never trades on data it cannot "
                       f"trust.")
                hard_blocks[sym] = hard_blocks.get(sym, 0) + 1
                continue
            if SIP_TREND_GUARD and adj:
                # the monthly rules EXIT a holding whose trend has gone — never
                # add money to such a name
                mom = signal_momentum(adj, len(adj) - 1) if len(adj) >= LOOKBACK + 1 else None
                below_dma = False
                if EXIT_OVERLAY_200DMA and len(adj) >= 200:
                    seg = adj[-200:]
                    below_dma = adj[-1] < sum(seg) / len(seg)
                # audit v5 #6: same momentum floor as selection — a strong
                # trend's brief dip below the 200-DMA does not block the add
                _dip_buyable = (below_dma and ENTRY_OVERLAY_MOM_FLOOR is not None
                                and mom is not None and mom >= ENTRY_OVERLAY_MOM_FLOOR)
                if (mom is not None and mom <= EXIT_THRESHOLD) or (below_dma and not _dip_buyable):
                    why = (f"its 12-month trend is down to {fmt_pct(mom)}"
                           if mom is not None and mom <= EXIT_THRESHOLD
                           else "its price is below its 200-day average")
                    P.warn(f"Not buying {sym} — {why}.  Buying more of a falling "
                           f"ETF is exactly what the strategy forbids; the money "
                           f"waits until it recovers or the month-end check swaps "
                           f"it out.")
                    parked.append(sym)
                    continue
            # Fill pricing: a FRESH live quote is what a real market order pays
            # — paper fills at it even beyond QUOTE_MAX_DEV (external audit F3);
            # a STALE quote is never a fill source (run #4 basis artifact).
            # Premium protection itself = the iNAV gate below (F1 v3 wiring).
            ref_px = prices.get(sym)
            ltp = qmap.get(sym)
            px = ref_px
            if ltp:
                fresh = tflag.get(sym) is True
                dev = (ltp / ref_px - 1.0) if ref_px else 0.0
                if fresh:
                    px = ltp
                    if abs(dev) > QUOTE_MAX_DEV:
                        P.warn(f"{sym}: the live price {fmt_rs(ltp)} moved "
                               f"{fmt_pct(dev)} from the last close {fmt_rs(ref_px)} — "
                               f"buying at the LIVE price (a real order would pay "
                               f"the same).  The value-vs-fund check below decides "
                               f"if that premium is acceptable.")
                else:
                    P.warn(f"{sym}: the live price is old (not from today) — "
                           f"buying is simulated at the last close {fmt_rs(ref_px)}.")
            if not px:
                P.warn(f"Not buying {sym} — no price available.")
                continue
            w = targets.get(sym, 0.0)
            if hard_blocks.get(sym, 0) >= DEPLOY_HARD_BLOCK_DAYS:
                hard_dropped.append(sym)
                continue
            if w <= 0:
                P.info(f"Not buying {sym} — it is not in the month-end plan.")
                continue
            tgt_val = w * eq0
            cur_val = (holdings0.get(sym) or {}).get("qty", 0.0) * px
            gap = tgt_val - cur_val
            if gap < MIN_TRADE_VALUE:
                P.info(f"{sym}: already holds its full planned amount "
                       f"({fmt_cr(cur_val)} of {fmt_cr(tgt_val)}) — nothing to buy.")
                continue
            plan[sym] = {"px": px, "gap": gap}
        if not plan:
            P.ok("Nothing to buy — every chosen ETF is either falling (money "
                 "parked, see above) or already holds its full planned amount.")
        # audit v4 §2 (user choice: autofill): a flat small daily amount on a
        # large plan was measured as a permanent ~9-month deployment lag vs the
        # backtested next-session execution — ~1pp CAGR per 10 sessions of lag.
        # Budget = max(floor, remaining-plan / market-days-to-re-plan).
        pool0 = sum(v["gap"] for v in plan.values())
        # audit v11 P0-1: sessions to the re-plan come from the CALENDAR
        # (weekdays minus NSE holidays).  The v10 DB count was always 0 — the
        # price table has no future dates — which made the trickle fire every
        # day and the autofill unreachable for the whole month.  +1 = today.
        try:
            _mend = next_month_end_hint()
            _sessions_real = 1 + sessions_to_calendar(today_str(), _mend)
        except Exception:
            _sessions_real = 0
        sessions_left = deploy_sessions_left(_sessions_real or None)
        # audit v12 leak-3 (measured R22): never stretch the plan over more
        # than AUTOFILL_MAX_SESSIONS days — a ~21-session ramp cost ~5pp CAGR
        # on the live-box snapshot vs 3 sessions.  The trickle guard above
        # still wins near the re-plan (it caps the whole budget at the floor).
        sessions_left_eff = min(sessions_left, AUTOFILL_MAX_SESSIONS)
        # near-replan guard (auditor point 4): with the month-end re-plan only
        # REPLAN_TRICKLE_DAYS sessions away, buying the whole plan into a basket
        # that re-ranks in a day or two burns round-trip fees + 20.8% STCG on
        # rotated names.  Deploy just the floor until the fresh list.
        _trickle = 0 < _sessions_real <= REPLAN_TRICKLE_DAYS
        if _trickle:
            budget_today = INVEST_PER_DAY
        else:
            budget_today = autofill_budget(INVEST_PER_DAY, pool0, sessions_left_eff)
        if _trickle and plan:
            _sw = "session" if _sessions_real == 1 else "sessions"
            P.note(f"Month-end re-plan is only {_sessions_real} market {_sw} "
                   f"away — deploying just the daily floor {fmt_rs(INVEST_PER_DAY)} "
                   f"({fmt_cr(pool0)} of plan waits) so freshly-bought names aren't "
                   f"churned by next week's re-pick. Full-speed buying resumes "
                   f"with the new signal.")
        if (not _trickle) and budget_today > INVEST_PER_DAY and plan:
            P.note(f"Budget lifted to {fmt_rs(budget_today)} today: {fmt_cr(pool0)} "
                   f"still to buy, spread over ~{sessions_left_eff} market "
                   f"day(s) (audit v12: deployment pace is capped at "
                   f"{AUTOFILL_MAX_SESSIONS} days; ~{sessions_left} market days "
                   f"remain before the {next_month_end_hint()} re-plan).  "
                   f"(Your floor stays {fmt_rs(INVEST_PER_DAY)}/run once the "
                   f"plan is filled.)")
        # audit v5 #12 -> user direction 2026-10-29: the >10%-of-money pause
        # guards only AUTOFILL-LIFTED budgets.  Your declared daily amount
        # (INVEST_PER_DAY == STARTING_CAPITAL) deploys on the single runtime
        # y/n already asked up front — prompting again for your own setting
        # would double-confirm intent.  A lift above BOTH the floor and 10%
        # of equity asks once more before sending real orders.
        if (not PAPER_TRADE) and plan and budget_today > INVEST_PER_DAY \
                and budget_today > 0.10 * eq0:
            if not live_order_ok(f"Autofill lifted today's buys to "
                                 f"{fmt_cr(budget_today)} — above your "
                                 f"{fmt_rs(INVEST_PER_DAY)} floor AND above "
                                 f"10% of your money ({fmt_cr(eq0)})."):
                P.warn("Skipped — no buy orders today. Everything else "
                       "(sells/checks) still ran above.")
                return 0
        # audit v5 #10: live orders before the 09:15 open price off yesterday's
        # close — say so (the weekend/holiday note above only covers dead days)
        if not PAPER_TRADE:
            _now_t = dt.datetime.now().time()
            if (parse_date(today_str()).weekday() < 5
                    and dt.time(0, 0) <= _now_t < dt.time(9, 15)):
                P.warn("Market has NOT opened yet (before 09:15) — live orders "
                       "would price at yesterday's close. Safer to run after "
                       "the open bell.")
        remaining = max(0.0, budget_today - already)
        if plan and remaining < MIN_TRADE_VALUE:
            # audit v15 P0-1 (2026-10-30 console): when the budget is BELOW the
            # minimum order size the old line claimed "already fully invested"
            # — with ₹0 placed that was a lie masking a strategy that can
            # never buy.  Say which case it is.
            if already >= MIN_TRADE_VALUE:
                P.ok(f"Today's budget ({fmt_rs(budget_today)}) is already fully "
                     f"invested ({fmt_rs(already)} in {dlog.get('orders', 0)} "
                     f"order(s)) — nothing left to do; safe to run again anytime.")
            else:
                P.warn(f"Today's budget is {fmt_rs(budget_today)} — BELOW the "
                       f"minimum order size of {fmt_rs(MIN_TRADE_VALUE)}, so NO "
                       f"buy can happen today.  This is not 'already invested'; "
                       f"nothing was placed.  Raise STARTING_CAPITAL (your "
                       f"daily budget) to at least ₹{MIN_TRADE_VALUE} — better "
                       f"5-10x — if you intend daily deployment.")
                self.db.execute(
                    "INSERT INTO decisions(ts,phase,symbol,decision,reason) "
                    "VALUES(?,?,?,?,?)",
                    (now_iso(), "deploy", "-", "BUDGET_BELOW_MINIMUM",
                     f"budget {budget_today:.0f} < MIN_TRADE_VALUE "
                     f"{MIN_TRADE_VALUE:.0f}; zero orders possible"))
            return 0
        # audit v5 #4: if live cash can't cover today's buys, sell just enough
        # of the parked idle-cash sleeve first (matches backtest behaviour)
        if plan and remaining >= MIN_TRADE_VALUE:
            self.sleeve_unsweep(remaining)
        bought = 0
        deployed = 0.0
        # audit v5 #3 (2026-10-28): exactly ONE order per ETF per day.  The
        # old 3-pass loop fragmented the same budget into up to 3 orders per
        # symbol (9 orders for 3 ETFs on the live run) — 3x per-order charges
        # and a tripled effective liquidity-cap exposure.  day_slices() does
        # the whole proportional allocation arithmetically; the 15%-of-p25
        # liquidity rule is now a per-DAY cap.
        caps = {}
        for _s in plan:
            _p25 = self.filters.liquidity_p25(_s, today_str()) or 0.0
            if _p25 > 0:
                caps[_s] = POS_CAP_LIQ_FRAC * _p25
        slices = day_slices({s: v["gap"] for s, v in plan.items()}, remaining, caps)
        if not slices and plan:
            # audit v11 P0-3: name the REAL constraint.  After the P0-2
            # concentration fallback, reaching this line means one of: today's
            # budget was already used by an earlier run, every remaining gap is
            # below the minimum order, or today's per-name liquidity caps are
            # spent.  Do NOT blame the gaps themselves — a 175k gap is not dust.
            P.ok(f"Nothing was bought from the plan this run — either today's "
                 f"budget was already used by an earlier run, every remaining "
                 f"gap is below the {fmt_rs(MIN_TRADE_VALUE)} minimum order, or "
                 f"today's per-name liquidity caps are already spent (the "
                 f"reason lines above name the specific case).")
        for sym, slice_val in sorted(slices.items(), key=lambda kv: -kv[1]):
            if remaining < MIN_TRADE_VALUE:
                break
            it = plan[sym]
            if caps.get(sym) and slice_val >= caps[sym] - 1.0:
                P.info(f"{sym}: today's buy is capped at 15% of its quiet-day "
                       f"trading ({fmt_cr(caps[sym])}) — the rest waits for "
                       f"the next run (one slice per ETF per run; re-running "
                       f"today tops up the same names safely).")
            # audit v4 §3.5: an order placed by an EARLIER run today and
            # still open at the broker counts as committed — never stack a
            # second order on the same name the same day
            if self.db.one(
                    "SELECT COUNT(*) AS n FROM orders WHERE symbol=? AND "
                    "exec_date=? AND status IN ('PENDING','UNCONFIRMED')",
                    (sym, today_str()))["n"]:
                P.note(f"{sym}: an order from earlier today is still open — "
                       f"not ordering again.")
                continue
            px = it["px"]
            qty = int(slice_val / px)
            qty = min(qty, int(remaining / px))
            if qty <= 0 or qty * px < MIN_TRADE_VALUE:
                continue
            # audit v14 P1 (2026-10-30): in LIVE mode a buy needs TODAY's live
            # quote.  Falling back to the last close meant the LIMIT the
            # broker actually receives could be based on yesterday's price.
            if not PAPER_TRADE and tflag.get(sym) is not True:
                _wq = ("no live quote received" if sym not in tflag else
                       ("quote timestamp unreadable" if tflag.get(sym) is None
                        else "quote timestamp is not from today"))
                P.warn(f"Not buying {sym} today — {_wq}; live buys need a "
                       f"same-day Upstox quote.  (Paper mode allows the last "
                       f"close; live mode never.)")
                continue
            reason = f"daily budget buy ({fmt_rs(INVEST_PER_DAY)} per run)"
            # external audit F1 (2026-10-28 v3): every purchase passes the
            # full pre-trade gate (liquidity/spread/iNAV) + a fresh-quote
            # NAV premium check with a lag cushion
            ok_g, why_g, _gi = self.filters.check_execution(sym, today_str(), px * qty)
            if ok_g and tflag.get(sym):
                _blk, _dev, _lim = self.filters.inav_premium_block(
                    sym, today_str(), price=px)
                if _blk:
                    ok_g = False
                    # audit v4 P1: name the NAV, scheme and date so a bad
                    # mapping is instantly diagnosable
                    _navr = self.db.one(
                        "SELECT nav, date FROM nav_data WHERE symbol=? "
                        "ORDER BY date DESC LIMIT 1", (sym,))
                    _code = nav_scheme_codes_effective().get(sym, "?")
                    why_g = (f"buy price {fmt_rs(px)} is {fmt_pct(_dev)} above "
                             f"what the ETF is really worth (allowed: "
                             f"{fmt_pct(_lim)}) — too expensive today")
                    if _navr:
                        why_g += (f"  [NAV {fmt_rs(_navr['nav'])} of "
                                  f"{_navr['date']}, scheme {_code}]")
                    # audit v7: on global ETFs this is a REAL market premium
                    # (SEBI overseas-unit freeze), never a data bug — say so
                    if (universe_map().get(sym, {}).get("category", "")
                            .upper() == "GLOBAL INDICES"):
                        why_g += ("  This is the global-ETF premium freeze "
                                  "(SEBI cap on new overseas units) — real "
                                  "market gap, not bad data; it can snap shut "
                                  "overnight if limits rise, and price bands "
                                  "anchor to NAV from 1-Apr-2027.")
            if not ok_g:
                P.warn(f"Not buying {sym} today — {why_g}")
                if "really worth" in why_g:
                    # iNAV premium = hard block; liquidity/spread deferrals
                    # are transient and do not count toward plan removal
                    hard_blocks[sym] = hard_blocks.get(sym, 0) + 1
                self.db.execute(
                    "INSERT INTO orders(ts,signal_as_of,exec_date,symbol,side,qty,price,value,"
                    "slippage,charges,status,mode,order_type,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now_iso(), today_str(), today_str(), sym, "BUY", qty,
                     px, px * qty, 0, 0, "SKIPPED_FILTER",
                     "PAPER" if PAPER_TRADE else "LIVE", "MARKET", why_g))
                continue
            # user direction 2026-10-28: an OPEN buy order at the broker
            # is already working for this ETF — do not layer a second
            # one on top (the next sync sees its fill and shrinks the gap)
            _open_b = self.broker.live_open_qty(sym, "BUY")
            if _open_b > 0:
                P.info(f"Not re-buying {sym} today — a buy order for "
                       f"{_open_b:.0f} unit(s) is still open at the broker; "
                       f"any gap left after it fills is retried tomorrow.")
                continue
            need = px * qty * 1.01
            if self.broker.cash() < need:
                P.warn(f"Not buying {sym} — not enough free cash "
                       f"(needs {fmt_cr(need)}, have {fmt_cr(self.broker.cash())}).")
                continue
            # audit F7 (v3): per-tier spread fallback when no live quote
            _sp = qspread.get(sym)
            if _sp is None:
                _p25 = self.filters.liquidity_p25(sym, today_str()) or 0.0
                _br = self.db.one("SELECT broad FROM etf_universe WHERE symbol=?", (sym,))
                _sp = estimate_spread_bps(_p25, broad=bool(_br and _br["broad"]))
            # audit v4 #9: live buys are band-limited LIMIT orders
            # (MARKET tail-risk removed); paper stays modelled as before.
            # audit v14 P0: ref drives only the ledger simulation — the LIMIT
            # actually reaching the broker is px x (1+LIVE_LIMIT_BUFFER).
            _otype, _px_send = live_buy_order_px(px)
            res = self.broker.execute(sym, "BUY", qty, px, today_str(),
                                      reason=reason,
                                      spread_bps=_sp, order_type=_otype,
                                      limit_px=_px_send)
            self.broker.print_order_ticket(res, sym,
                                           universe_map().get(sym, {}).get("name", sym),
                                           "BUY")
            if res.get("status") == "FILLED":
                bought += 1
                fill_v = float(res.get("value") or 0.0) or (px * qty)
                deployed += fill_v
                remaining = max(0.0, remaining - fill_v)
                it["gap"] -= px * qty
                hard_blocks.pop(sym, None)
            elif res.get("status") in ("PENDING", "UNCONFIRMED"):
                # audit v4 §3.2/§7: in-flight money is committed money —
                # count the face value against today's budget NOW and never
                # order this name twice today; the next run's reconcile
                # settles or releases it
                pend_v = px * qty
                deployed += pend_v
                remaining = max(0.0, remaining - pend_v)
                it["gap"] -= pend_v
                hard_blocks.pop(sym, None)
                P.note(f"{sym}: order sent but broker has NOT confirmed yet "
                       f"— counting {fmt_rs(pend_v)} as spent for today "
                       f"(settled or released by tomorrow's reconcile).")
            # rejected/failed: nothing spent — the next run retries the gap
        # persist hard-block counters + drop names that crossed the limit
        # (also names pre-dropped today) from the stored plan until month-end
        try:
            self.db.meta_set(DEPLOY_SKIP_KEY,
                             json.dumps({s: n for s, n in hard_blocks.items() if n > 0}))
            offenders = sorted(set(
                s for s in list(hard_blocks) + hard_dropped
                if hard_blocks.get(s, 0) >= DEPLOY_HARD_BLOCK_DAYS))
            if offenders:
                _tk = json.loads(self.db.meta_get(SIP_TARGETS_KEY, "") or "{}")
                changed = False
                for s in offenders:
                    if s in _tk:
                        _tk.pop(s)
                        changed = True
                if changed:
                    self.db.meta_set(SIP_TARGETS_KEY, json.dumps(_tk))
                P.err(f"DROPPED from the plan until re-plan: {', '.join(offenders)} "
                      f"— hard checks (broken data / iNAV premium) failed "
                      f"{DEPLOY_HARD_BLOCK_DAYS} days in a row; verify the price "
                      f"series for these names.")
        except Exception:
            pass
        # journal the per-day budget so re-runs can only top up, never double
        try:
            self.db.meta_set(DEPLOY_DAY_KEY, json.dumps({
                "date": today_str(),
                "deployed": already + deployed,
                "orders": int(dlog.get("orders", 0)) + bought}))
        except Exception:
            pass
        # audit v5 #4: whatever cash is left idle beyond the buffer gets
        # parked in the liquid sleeve (weekly cadence inside sleeve_sweep)
        self.sleeve_sweep()
        P.ok(f"Today's buying done — {bought} order(s), {fmt_rs(deployed)} used "
             f"of today's {fmt_rs(budget_today)} budget "
             f"(today's total: {fmt_rs(already + deployed)}; floor stays "
             f"{fmt_rs(INVEST_PER_DAY)}/run).")

        # Consolidated "parked by the trend guard" accounting (user run
        # 2026-10-27 #2: 3 of 5 names blocked, Rs 6.4L of the plan quietly sat
        # in cash with no single line adding it up — now stated explicitly).
        # Valued on the POST-cycle equity at the same close basis the action
        # panel uses (user run #3: the two figures disagreed by ~1k because the
        # SIP line used pre-buy equity) — the two prints now reconcile.
        parked_names = list(parked)
        parked_val = 0.0
        if parked_names:
            eq2 = self.broker.equity(prices)
            pv = []
            for s in parked_names:
                held = (holdings0.get(s) or {}).get("qty", 0.0) * (prices.get(s) or 0.0)
                v = max(0.0, targets.get(s, 0.0) * eq2 - held)
                if v >= MIN_TRADE_VALUE:
                    pv.append((s, v))
            parked_val = sum(v for _s, v in pv)
            parked_names = [s for s, _v in pv]
        if parked_names:
            P.kv("Money parked (ETFs are falling)",
                 f"{fmt_cr(parked_val)} of the plan ({', '.join(parked_names)}) "
                 f"waits in cash — buying resumes if they start rising again, "
                 f"else the {next_month_end_hint()} month-end check swaps them out.")
        elif parked:
            P.kv("Money parked (ETFs are falling)",
                 f"{', '.join(parked)} — no new buying; these already hold "
                 f"their full planned amount anyway.")
        if bought:
            eq = self.broker.equity(prices)
            # audit §6: report against PRE-BUY equity (eq0 above), not after
            # today's purchases
            eq_pre = eq0 if eq0 > 0 else max(eq - deployed, 1.0)
            # audit v10 (external): on re-runs this line counted only the LAST
            # run (Rs 19k) while the DAY had already deployed Rs 1.36L — show
            # both, so resumptions never read as fresh full deployment
            _dt = self.db.one("SELECT COUNT(*) AS n, COALESCE(SUM(value),0) AS v "
                              "FROM orders WHERE exec_date=? AND side='BUY' "
                              "AND status='FILLED'", (today_str(),))
            _dn, _dv = (_dt["n"] or 0), (_dt["v"] or 0.0)
            _day = (f" | today across all runs: {fmt_cr(_dv)} in {_dn} order(s)"
                    if _dv > deployed + 1.0 else "")
            P.kv("Money invested today",
                 f"{fmt_rs(deployed)} across {bought} ETF(s) this run"
                 f" — {fmt_pct(safe_div(deployed, max(eq_pre, 1.0), 0.0))} of the "
                 f"{fmt_cr(eq_pre)} total you had before buying{_day}")
        elif parked_names:
            P.ok("Nothing was bought — the undelivered plan money is parked "
                 "because those ETFs are falling (above), not because the "
                 "plan is full.")
        else:
            P.ok("Nothing was invested this run — the specific reason is on "
                 "the lines above (budget used by an earlier run today, "
                 "positions already full, a filter/liquidity block, or the "
                 "remainder below the minimum order).")
        return bought

    def daily_exit_guard(self):
        """User ask 2026-10-28 — exit a holding the DAY its rules fire instead
        of waiting for the month-end rebalance.  BACKTESTED first (R12A_daily_
        exit): 11.25%/0.67/-12.78 MaxDD/OOS 10.25 vs month-end exits
        11.80%/0.69/-14.29/OOS 11.71 — immediate exits COST ~0.5pp CAGR and
        ~1.5pp OOS (whipsaw re-entries) buying ~1.5pp of MaxDD.  Numbers-first
        rule => DAILY_EXIT_GUARD defaults False; flip to True knowingly."""
        if not DAILY_EXIT_GUARD:
            return
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
        """One run: check safety limits -> month-end check + sells if due ->
        today's buying with the daily budget.  final_status=False suppresses
        the tail account block when the caller (the default 'run' command)
        prints the full status right after."""
        P.section("DAILY RUN — safety check, month-end sells if due, then today's buying", "\U0001F4C5")
        mode = "SIMULATED (paper trading — no real money)" if PAPER_TRADE else "REAL MONEY (live trading)"
        P.info(f"Mode: {mode}   |   Today: {today_str()}")
        # audit v14 P0/P1 (2026-10-30): two hard gates BEFORE any live order —
        #   (a) never trade real money off synthetic demo rows;
        #   (b) never trade off stale prices: the newest bar must reach the
        #       last completed NSE session (one session's grace for feeds that
        #       publish the previous close late).
        if not PAPER_TRADE:
            if self.db.meta_get("data_origin") == "SYNTHETIC":
                P.err("This database still contains SYNTHETIC demo data. "
                      "REFUSING real orders.  Heal it first: "
                      "python etf-momentum-screener.py update --force-data")
                return self.broker.equity(self.latest_prices())
            _dp_max = self.db.one("SELECT MAX(date) AS d FROM price_daily")["d"]
            _lcs = last_completed_session()
            if not _dp_max or _dp_max < prev_completed_session(_lcs):
                P.err(f"Price data looks stale (newest bar {_dp_max or 'none'}, "
                      f"last completed session {_lcs}) — REFUSING real orders. "
                      f"Run 'python etf-momentum-screener.py update' first "
                      f"(analysis below would also be stale, so the run stops).")
                return self.broker.equity(self.latest_prices())
            # audit v15 P0-1 (2026-10-30 console): a daily budget UNDER the
            # minimum order size can never place a single buy — the run used
            # to sail on and print 'already fully invested'.  Refuse orders
            # loudly; analysis continues when the user answers n anyway.
            if INVEST_PER_DAY < MIN_TRADE_VALUE:
                P.err(f"CONFIG ERROR: your daily budget (STARTING_CAPITAL = "
                      f"₹{INVEST_PER_DAY:.0f}) is BELOW the minimum order size "
                      f"₹{MIN_TRADE_VALUE:.0f} — the strategy is structurally "
                      f"unable to BUY anything; every buy will skip.  "
                      f"(Sells/exits still work.)  Raise STARTING_CAPITAL "
                      f"(₹5,000–10,000 is the tuned range) in the config block "
                      f"at the top of the script.")
        if not PAPER_TRADE:
            # audit F9: settle journaled PENDING/UNCONFIRMED live orders first —
            # otherwise today's run could duplicate them
            try:
                resolve_pending_live_orders(self.db, self.broker)
            except Exception as _e:
                P.warn(f"Pending-order reconcile failed ({_e}) — "
                       f"'python etf-momentum-screener.py reconcile' remains the manual check.")
            # user direction 2026-10-28: in LIVE mode the broker's holdings +
            # order book are the source of truth — holdings are ADOPTED from
            # Upstox (supersedes the audit v4 L2 diff-and-warn), the journal
            # mirror is rewritten, and cash comes from the funds API.  If the
            # broker cannot be read: NO ORDERS today (fail-closed).
            if not self.broker.sync_live_book():
                P.err("Broker truth unavailable — this was an analysis-only run "
                      "(no orders placed). Fix the Upstox token/network and rerun.")
                return self.broker.equity(self.latest_prices())
        prices = self.latest_prices()
        _eqd = self.broker.equity(prices)
        _hvd = self.broker.holdings_value(prices)
        ctx = self.risk.update(_eqd, "daily",
                               invested_frac=(_hvd / _eqd) if _eqd > 0 else None)
        for level, msg in ctx["events"]:
            P.warn(f"SAFETY ALERT ({level}): {msg}")
        self.apply_derisk(ctx, self.latest_prices())
        self.consume_pending_sells(self.latest_prices())
        as_of = self.last_month_end()
        if force_exec or self.db.meta_get("last_trade_signal", "") != as_of:
            # audit v5 #2 (2026-10-28): a DUE cycle whose signal is already
            # stale (> STALE_REFRESH_DAYS market days old) must not execute at
            # all — it ran fully, placed stale-exit orders, and was thrown
            # away seconds later by deploy_day's fresh re-pick.  Skip straight
            # to the TODAY re-pick; deploy_day's own staleness guard is the
            # safety net for the reverse path.
            _sig_stale, _age = (False, 0)
            if (not force_exec) and as_of:
                _sig_stale, _age = signal_is_stale(self.db, as_of)
            if _sig_stale:
                P.info(f"The {as_of} month-end signal is {_age} market-days old "
                       f"(limit {STALE_REFRESH_DAYS}) — skipping the stale cycle "
                       f"entirely and picking the list + plan from TODAY's data.")
                self.run(force_exec=True, as_of_override=today_str(), quiet=True)
            else:
                P.info(f"Month-end check for {as_of} is due — picking the 5 ETFs "
                       f"and selling what no longer qualifies.")
                self.run(force_exec=force_exec)
        else:
            nxt = next_month_end_hint()
            # audit v8 (2026-10-28): when a stale signal was discarded and the
            # basket re-picked fresh, say the FRESH date — showing the stale
            # month-end date contradicted the pick list right below
            _bd = self.db.meta_get(SIP_BASKET_DATE_KEY, "") or ""
            if _bd and _bd != as_of:
                P.ok(f"No month-end check today — the stale {as_of} signal was "
                     f"discarded and replaced by a fresh pick from {_bd}.")
            else:
                P.ok(f"No month-end check today — the {as_of} picks were "
                     f"already acted on.")
            P.info(f"Next check: after the {nxt} month-end close, acted on the "
                   f"next market day.")
        self.daily_exit_guard()
        self.deploy_day()
        if not PAPER_TRADE:
            # audit v4 §3.3: orders placed THIS run stay UNCONFIRMED for ~24h
            # otherwise; a short same-run re-poll usually catches the fill
            for _try in range(2):
                try:
                    if not resolve_pending_live_orders(self.db, self.broker):
                        break
                except Exception:
                    break
                time.sleep(3)
        prices = self.latest_prices()
        cash = self.broker.cash()
        hv = self.broker.holdings_value(prices)
        eq = cash + hv
        self.risk.snapshot(today_str(), cash, hv, eq)
        if final_status:
            # display overlay: today's fills marked at fill price (audit F3b);
            # the journal/snapshot above stays on last close
            prices_disp, marked = _fill_mark_overlay(self.db, prices)
            if marked:
                hv_d = self.broker.holdings_value(prices_disp)
                eq_d = cash + hv_d
            else:
                hv_d, eq_d = hv, eq
            P.section("ACCOUNT STATUS", "\U0001F4B0")
            P.kv("Cash on hand", fmt_cr(cash))
            P.kv("ETFs you hold (worth now)", fmt_cr(hv_d))
            P.kv("Your total (cash + ETFs)", fmt_cr(eq_d), val_color=_C.GREEN)
            P.kv("Risk state", self.risk.state)
            dd = max(1 - eq_d / self.risk.peak, 0.0) if self.risk.peak else 0.0
            P.kv("Drawdown from peak", fmt_pct(dd, signed=False))
            if marked:
                P.note(f"Marked at today's fill price (VWAP): {', '.join(marked)} — "
                       f"last-close marks return after tonight's close download.")
        log(f"daily cycle done: equity={eq:.2f} cash={cash:.2f}")
        return eq

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
    def print_signal_console(self, selection: dict, as_of: str, strategy: str):
        P.section(f"MONTH-END CHECK — which ETFs qualify as of {as_of}  (Strategy {strategy})", "\U0001F4AC")
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
        if strategy != "A":
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
        P.kv("Money planned for ETFs", fmt_cr(sized["invested"] * equity))
        P.kv("Money kept in cash", fmt_cr((1 - sized["invested"]) * equity))
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
        risk_scale=1.0 unconditionally, so live books ignored the bear
        halving that the backtest applies."""
        risk_scale = self.risk.risk_scale()
        if REGIME_SCALING:
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

    def run(self, force_exec: bool = False, strategy: str = TRADE_STRATEGY,
            as_of_override: str = None, quiet: bool = False):
        # audit v5 #11: quiet=True suppresses the full signal/plan tables —
        # used for in-run re-picks (stale-list refresh) whose panel was
        # otherwise printed twice, once for the discarded basket and again
        # for the fresh one.  Orders, journal and meta writes are NOT
        # affected; only the two biggest console panels are replaced by a
        # one-line summary.
        P.section("TSMOM TRADING CYCLE", "\U0001F680")
        mode = "PAPER TRADING (simulated — no real money)" if PAPER_TRADE else "LIVE TRADING"
        P.info(f"Mode: {mode}")
        if not PAPER_TRADE:
            # user direction 2026-10-28: the broker's holdings + order book
            # are the source of truth in live mode — sync (or reuse the cache
            # run_daily already made) and refuse to trade without them.
            self.broker.holdings()
            if not self.broker.live_book_ok():
                P.err("Broker truth unavailable — month-end cycle halted, "
                      "NO orders placed. Fix the Upstox token/network and rerun.")
                return self.broker.equity(self.latest_prices())
        interest = self.broker.accrue_cash_interest()
        if interest:
            P.info(f"Cash interest accrued: {fmt_rs(interest)} "
                   f"(cash earns {fmt_pct(CASH_RATE_PRETAX,2)} p.a. in the paper book)")
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
        prices = self.latest_prices()
        eq_before = self.broker.equity(prices)
        _hv0 = self.broker.holdings_value(self.latest_prices())
        risk_ctx = self.risk.update(eq_before, "cycle-start",
                                    invested_frac=(_hv0 / eq_before)
                                    if eq_before > 0 else None)
        for level, msg in risk_ctx["events"]:
            P.warn(f"RISK [{level}]: {msg}")
        if risk_ctx["all_cash"]:
            P.warn("Risk rules currently force 100% CASH — new buys are blocked until recovery.")
        stats = self.collect_stats(as_of)
        if not stats:
            P.err("No ETF has enough price history yet. Run: python etf-momentum-screener.py update")
            return
        holding_syms = set(self.broker.holdings().keys())
        selection = self.builder.select(stats, holding_syms, strategy,
                                        equity=self.broker.equity(self.latest_prices()))
        if quiet:
            P.info(f"Fresh basket picked quietly: "
                   f"{', '.join(s['symbol'] for s in selection['chosen']) or '(none — all cash)'} "
                   f"({len(selection['decisions'])} names scanned).")
        else:
            self.print_signal_console(selection, as_of, strategy)
        if SIP_MODE:
            # user 2026-10-19: rebalance SELLS exits only; the portfolio is
            # built by the daily SIP.  The chosen set is the daily buy list.
            self.db.meta_set(SIP_BASKET_KEY,
                             ",".join(s["symbol"] for s in selection["chosen"]))
            self.db.meta_set(SIP_BASKET_DATE_KEY, as_of)
            # 2026-10-27: also store the SIZED target weights — the daily
            # deployment converges toward them with each run's budget
            # (exec-lag A/B evidence: deployment lag costs ~1% CAGR per 10
            # sessions, so INVEST_PER_DAY should not be token-size)
            sized = self.builder.size(selection["chosen"], eq_before,
                                      risk_scale=self._current_risk_scale(as_of),
                                      strategy=strategy)
            # audit v10 PARK_BENCH_TREND (default off — pending R20J): the
            # plan's leftover weight rides NIFTYBEES while NIFTYBEES itself is
            # in an uptrend above its 200-DMA, instead of idling in cash
            if PARK_BENCH_TREND and "NIFTYBEES" not in sized["weights"]:
                _bs = next((s for s in stats if s["symbol"] == "NIFTYBEES"), None)
                if _bs is not None and (_bs["momentum"] or 0) > ENTRY_THRESHOLD:
                    _bseg = [row_adj(r) for r in (_bs.get("rows") or [])][-200:]
                    _bok = not _bseg or _bseg[-1] >= sum(_bseg) / len(_bseg)
                    _left = 1.0 - sum(sized["weights"].values())
                    if _bok and _left > 0.02:
                        sized["weights"]["NIFTYBEES"] = _left
                        P.note(f"Parking the plan's leftover {fmt_pct(_left)} in "
                               f"NIFTYBEES (it passes its own trend test) — it "
                               f"leaves automatically when its trend breaks.")
            self.db.meta_set(SIP_TARGETS_KEY, json.dumps(sized["weights"]))
            self.db.meta_set(DEPLOY_SKIP_KEY, "{}")   # fresh plan = fresh chances
            if sized["weights"] and not quiet:
                self.print_portfolio_plan(sized, eq_before)
            P.info(f"How it works: this month-end check only SELLS. Buying "
                   f"toward the plan above happens a little every day from the "
                   f"daily budget: {fmt_rs(INVEST_PER_DAY)} per run, shared "
                   f"across whichever ETFs lag their plan most.")
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
        else:
            sized = self.builder.size(selection["chosen"], eq_before,
                                      risk_scale=self._current_risk_scale(as_of),
                                      strategy=strategy)
            if not quiet:
                self.print_portfolio_plan(sized, eq_before)

        # ---- build target basket (execute at the LATEST traded price = next session) ----
        latest_px = self.latest_prices()
        target = {}
        # audit v15 P0-2 (2026-10-30): in SIP_MODE the target map never got
        # the CHOSEN symbols, so every held-and-still-picked ETF fell into
        # the orphan loop below with weight 0 -> the rebalance SOLD its own
        # #1/#2 picks (the live console showed SELL PHARMABEES/MOCAPITAL
        # minutes after re-picking them).  Held-and-chosen is kept, always.
        _chosen_syms = {st["symbol"] for st in selection["chosen"]}
        if not SIP_MODE:
            for st in selection["chosen"]:
                w = sized["weights"].get(st["symbol"], 0.0)
                target[st["symbol"]] = {"weight": w, "close": latest_px.get(st["symbol"], st["close"]),
                                        "spread_bps": st["spread_bps"], "name": st["name"]}
        # exits are mandatory sells
        for st in selection["exits"]:
            target.setdefault(st["symbol"], {"weight": 0.0,
                                             "close": latest_px.get(st["symbol"], st["close"]),
                                             "spread_bps": st["spread_bps"], "name": st["name"]})
            target[st["symbol"]]["weight"] = 0.0
        # audit bug #2: EVERY held symbol must appear in the target map — a held
        # ETF that fell out of the top-3 (or lost its data) is sold, exactly as
        # the backtester does.  Otherwise portfolios silently accumulate orphans.
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
                (as_of, st["symbol"], strategy, st["momentum"], st["vol"], st["liq_p25"],
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
            if is_exit and TAX_DEFER_EXITS:
                # audit v14 P2: broker-adopted holdings with UNKNOWN age must
                # not win the 12-month tax-wait (their lots are dated today;
                # the wait would freeze an exit for ~11 months on made-up age)
                _unk = set(json.loads(self.db.meta_get("tax_age_unknown", "[]")))
                open_dates = [] if sym in _unk else [r["buy_date"] for r in self.db.query(
                    "SELECT buy_date FROM tax_lots WHERE symbol=? AND qty>0 "
                    "AND sell_date IS NULL", (sym,))]
                if open_dates:
                    defer = tax_defer_blocks_exit(open_dates, as_of, 0.0)
                    if defer:
                        P.warn(f"SELL {sym} deferred this session — {defer}")
                        continue
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
        # 2) buys
        eq_now = self.broker.equity(self.latest_prices())
        for sym, tgt in sorted(target.items(), key=lambda kv: -kv[1]["weight"]):
            price = tgt["close"]
            h = self.broker.holdings().get(sym)
            have_val = (h["qty"] * price) if h else 0.0
            tgt_val = tgt["weight"] * eq_now
            if tgt_val > have_val + 1.0:
                buy_val = tgt_val - have_val
                if NO_TRADE_BAND > 0 and have_val > 0:
                    if abs(tgt_val - have_val) / max(tgt_val, have_val) < NO_TRADE_BAND:
                        continue
                if buy_val < MIN_TRADE_VALUE:
                    continue
                if self.risk.all_cash:
                    P.warn(f"BUY {sym} blocked — risk rules say 100% cash.")
                    continue
                ok, reason, info = self.filters.check_execution(sym, as_of, buy_val)
                if not ok:
                    P.warn(f"BUY {sym} deferred/skipped — {reason}")
                    self.db.execute(
                        "INSERT INTO orders(ts,signal_as_of,exec_date,symbol,side,qty,price,value,"
                        "slippage,charges,status,mode,order_type,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (now_iso(), as_of, today_str(), sym, "BUY", int(buy_val / max(price, 1e-9)),
                         price, buy_val, 0, 0, "SKIPPED_FILTER", "PAPER" if PAPER_TRADE else "LIVE",
                         "MARKET", reason))
                    continue
                qty = int(buy_val / price) if price > 0 else 0
                cash = self.broker.cash()
                max_qty = int(cash / (price * 1.002)) if price > 0 else 0
                qty = min(qty, max_qty)
                if qty <= 0:
                    P.warn(f"BUY {sym} skipped — not enough cash.")
                    continue
                res = self.broker.execute(sym, "BUY", qty, price, as_of,
                                          reason="trend signal buy",
                                          spread_bps=tgt["spread_bps"])
                self.broker.print_order_ticket(res, sym, tgt["name"], "BUY")
                trades_done += 1
        if trades_done == 0:
            if SIP_MODE:
                # user run 2026-10-27 #2: on a fresh book this section printed
                # "already matches the plan" while invested 0% vs a 92% plan —
                # technically true for exits, misleading as a whole
                P.info("No exits to execute — the rebalance only SELLS in SIP mode. "
                       "Buys toward the plan are handled by the daily SIP tranche "
                       "below.")
            else:
                P.info("No orders needed — portfolio already matches the plan.")

        # ---- snapshots ----
        prices = self.latest_prices()
        cash = self.broker.cash()
        hv = self.broker.holdings_value(prices)
        eq = cash + hv
        self.risk.update(eq, "cycle-end",
                         invested_frac=(hv / eq) if eq > 0 else None)
        self.risk.snapshot(today_str(), cash, hv, eq)
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
        P.section("PORTFOLIO AFTER MONTHLY REBALANCE (before today's buying)",
                  "\U0001F4CA")
        rows = []
        for sym, h in sorted(self.broker.holdings().items()):
            px = prices.get(sym, h["avg_cost"])
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
        P.kv("Your total (cash + ETFs)", fmt_cr(eq), val_color=_C.GREEN)
        P.kv("Invested fraction", fmt_pct(safe_div(hv, eq, 0.0)))
        dd = 1 - eq / self.risk.peak if self.risk.peak else 0.0
        P.kv("Drawdown from peak", fmt_pct(dd, signed=False),
             val_color=_C.RED if dd > 0.05 else _C.WHITE)
        P.kv("Risk state", self.risk.state)
        log(f"cycle done: equity={eq:.2f} cash={cash:.2f} trades={trades_done}")
        return eq


# ==============================================================================
# SECTION 20 — BACKTESTER (Step 10: 10y, OOS 20%, grid, cost & cash scenarios)
# ==============================================================================

class Backtester:
    """
    Event-driven monthly backtest on cached price data.
    Prices dict: symbol -> ascending [(date, adj_close, close, traded_value)]
    """

    def __init__(self, prices: dict, meta: dict, params: dict,
                 cost_case: str = "base", cash_rate: float = CASH_RATE_PRETAX,
                 strategy: str = "B", start_capital: float = STARTING_CAPITAL,
                 apply_tax: bool = True, risk_on: bool = True,
                 cash_sleeve: bool = None):
        self.prices = prices
        self.meta = meta            # symbol -> {name, category, index_family, broad}
        self.params = params        # {lookback, skip, entry, exit}
        self.cost_case = cost_case
        self.cash_rate = cash_rate
        self.strategy = strategy
        self.capital = start_capital
        self.apply_tax = apply_tax
        self.risk_on = risk_on
        # None = follow CASH_SLEEVE_MODE; False = force flat rate (cash-rate
        # scenario matrix); True = force sleeve with labelled fallback
        self.cash_sleeve = cash_sleeve

    # ---------- helpers ----------
    def _series(self, sym):
        return self.prices.get(sym, [])

    def run(self, start_date: str = None, end_date: str = None, verbose: bool = False):
        lookback = self.params.get("lookback", LOOKBACK)
        skip = self.params.get("skip", SKIP)
        entry = self.params.get("entry", ENTRY_THRESHOLD)
        exit_thr = self.params.get("exit", EXIT_THRESHOLD)
        exec_lag = max(1, int(self.params.get("exec_lag", 1)))
        # audit v12: live-deploy pacing experiment (1 = production lump)
        deploy_ramp = max(1, int(self.params.get("deploy_ramp", 1)))
        # EXPERIMENT knobs (round 4): mid-month guard-rail exits.
        # guard_freq = 0 (default) -> OFF, production behavior (exits only at
        # month-end rebalances).  N > 0 -> every N sessions, sell holdings that
        # currently breach the exit rules, filled at the NEXT session's close.
        # guard_mode: "both" (momentum<=exit OR below 200-DMA) / "mom" / "dma"
        guard_freq = int(self.params.get("guard_freq", 0))
        guard_mode = self.params.get("guard_mode", "both")
        # exec_guard = EXPERIMENT knob (default "none" = current backtest
        # semantics).  Mirrors the live SIP_TREND_GUARD on the monthly exec
        # path: a name selected at the month-end signal can be below its
        # 200-DMA / exit momentum line by the time orders fire (seen live
        # 2026-10-27: 3 of 5 basket names blocked, ~Rs 6.4L of the plan
        # idled).  This knob tests what to do with that freed budget:
        #   "park"         — skip the buy; freed weight idles in cash
        #   "redistribute" — freed buy value scales up trend-passing basket buys
        #   "substitute"   — freed value refills the basket from next-ranked
        #                    signal candidates that pass the trend check at
        #                    the exec date (signal-date ranking + stats)
        exec_guard = str(self.params.get("exec_guard", "none"))
        # refresh_late = EXPERIMENT knob (default 0 = off/production): if the
        # exec date is more than N trading sessions after the signal date,
        # RE-DERIVE basket + sizing from the exec date's data instead of
        # executing the stale month-end signal.  Motivated by external audit
        # F2: fresh/late journals build the book on a 27-day-old basket whose
        # names the trend guard then blocks (64% of the plan parked in the
        # 2026-10-27 paste).  Normal production never triggers it.
        refresh_late = int(self.params.get("refresh_late", 0))
        # renorm_excess = EXPERIMENT knob (default False = production rulebook
        # "no upward re-normalisation"): after the hard caps bind, distribute
        # the capped-out excess pro-rata to uncapped names (water-filling).
        # External audit F5: plan reached 92.1% invested / 11.5% vol vs the
        # 15% target partly because capped excess is NOT reallocated today.
        renorm_excess = bool(self.params.get("renorm_excess", False))
        # plaus_excl = EXPERIMENT knob (default "none"): exclude names whose
        # SIGNAL-DATE momentum or annualized-vol print exceeds the category
        # plausibility ceiling (the live ⚑ flags — MOM_PLAUSIBILITY /
        # VOL_PLAUSIBILITY) from selection.  Live-motivated 2026-10-27: MONQ50
        # (121% p.a. vol, +101% momentum print) and MASPTOP50 (65% p.a.) carry
        # spliced/unadjusted series yet remain selectable.  "vol"|"mom"|"both".
        # The hard >300% integrity gate is untouched either way.
        plaus_excl = str(self.params.get("plaus_excl", "none"))
        # min_trade_frac = EXPERIMENT knob (external audit 6.4): min trade grows
        # with equity — a Rs 2,000 ticket is fine on Rs 10 L but silly on Rs 1 Cr
        min_trade_frac = float(self.params.get("min_trade_frac", MIN_TRADE_FRAC))

        # stale_skip_days = EXPERIMENT knob (transcript audit 2026-10-27 §11.6):
        # when the execution happens more than N sessions after the signal
        # (fresh journal / missed month-end), NEW entries are suppressed — the
        # plan waits for the next month-end signal.  Exits and trims of held
        # names ALWAYS proceed (risk hygiene).  Default 0 = production deploys
        # the stored signal regardless of age (the trend guard is the re-check).
        stale_skip_days = int(self.params.get("stale_skip_days", 0))

        # daily_exit = EXPERIMENT (user ask 2026-10-28): held names re-checked
        # against the EXIT rules EVERY session (12-1 momentum <= threshold or
        # below the 200-DMA) and exited immediately instead of waiting for the
        # month-end.  params-only knob; the production default is set from the
        # R12 harness numbers, never from the ask itself.
        daily_exit = bool(self.params.get("daily_exit", False))

        def _min_trade(eq_now):
            return max(MIN_TRADE_VALUE, min_trade_frac * eq_now)

        spread_mult = COST_CASES[self.cost_case]["spread_mult"]
        spread_stat = COST_CASES[self.cost_case]["spread_stat"]

        # calendar
        all_dates = sorted({d for series in self.prices.values() for (d, _, _, _) in series})
        if not all_dates:
            return {"error": "no price data"}
        if start_date:
            all_dates = [d for d in all_dates if d >= start_date]
        if end_date:
            all_dates = [d for d in all_dates if d <= end_date]
        if len(all_dates) < 60:
            return {"error": "not enough calendar days after filtering"}
        date_index = {d: i for i, d in enumerate(all_dates)}

        # per-symbol index for fast lookup
        sym_dates = {}
        sym_adj = {}
        sym_close = {}
        sym_liq = {}
        for sym, series in self.prices.items():
            sd = [d for (d, _a, _c, _t) in series]
            first, last = sd[0], sd[-1]
            if last < all_dates[0] or first > all_dates[-1]:
                continue
            sym_dates[sym] = sd
            sym_adj[sym] = [a for (_d, a, _c, _t) in series]
            sym_close[sym] = [c for (_d, _a, c, _t) in series]
            sym_liq[sym] = [t for (_d, _a, _c, t) in series]

        def pos_at(sym, date):
            """index of last bar of sym at or before date, or None."""
            sd = sym_dates.get(sym)
            if not sd or date < sd[0]:
                return None
            lo, hi = 0, len(sd) - 1
            ans = None
            while lo <= hi:
                mid = (lo + hi) // 2
                if sd[mid] <= date:
                    ans = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            return ans

        def px_at(sym, date, field="close"):
            i = pos_at(sym, date)
            if i is None:
                return None
            return (sym_close if field == "close" else sym_adj)[sym][i]

        # month-end signal dates (quarterly mode keeps only quarter-ends)
        me_idx = month_end_indices(all_dates)
        signal_dates = [all_dates[i] for i in me_idx]
        signal_dates = [d for d in signal_dates if d > all_dates[0]]
        if REBALANCE_FREQ == "Q":
            signal_dates = [d for d in signal_dates
                            if parse_date(d).month in (3, 6, 9, 12)]
        signal_set = set(signal_dates)

        use_sleeve = (CASH_SLEEVE_MODE == "LIQUID_ETF"
                      if self.cash_sleeve is None else self.cash_sleeve)
        cash_fn = cash_return_series(self.prices, self.cash_rate, use_sleeve)
        self._cash_fn_flag = bool(getattr(cash_fn, "sleeve", False))
        cash = self.capital
        holdings = {}          # sym -> {qty, avg_cost}
        lots = []              # tax lots (in-memory)
        first_buy = {}         # symbol -> first purchase date (tax defer, audit 3.3)
        fy_buckets = {}        # (fy, class) -> {"st":x,"lt":y}  (audit #7 netting)
        current_fy = financial_year(parse_date(all_dates[0]))
        interest_earned = 0.0
        interest_unsettled = 0.0   # carry/sleeve gains accrued since last settle

        loss_carry = {}   # cls -> {"st":x, "lt":y, "years":n}  (audit 2.7)

        def settle_fy(fy, final=True):
            """
            Settle realised gains accrued so far, per class (audit 2.1/2.7):
              - STCL offsets STCG first, then LTCG; LTCL offsets LTCG only
              - unused losses carry forward up to LOSS_CARRY_YEARS (the year
                counter ages only at the FY roll — final=True)
              - per-class rates + exemption straight from TAX_RULES (external
                audit F2.5: HYBRID/GLOBAL now settle at their OWN rules)
              - 4% cess on the tax
              - external audit F2.2: cash carry / liquid-ETF sleeve gains
                accrue with no realisation event, so slab(+cess) tax on the
                accrual is settled here too instead of compounding pre-tax
              - SETTLE_MODE="quarterly" models advance tax (external audit
                F3.10/3.11): this runs at Jun/Sep/Dec/Mar quarter boundaries
                (several small hits) instead of one April hit per year
              - tax is DEDUCTED FROM CASH here (in-run) and must NOT be
                subtracted again in _summarize (audit 2.1 double-count)
            """
            nonlocal cash, tax_accrued, interest_unsettled
            if not self.apply_tax:
                fy_buckets.clear()
                interest_unsettled = 0.0
                return
            for key in [k for k in list(fy_buckets) if k[0] == fy]:
                cls = key[1]
                d = fy_buckets.pop(key)
                tr = TAX_RULES.get(cls, TAX_RULES["EQUITY"])
                st_rate, lt_rate, exempt = tr["stcg"], tr["ltcg"], tr["exempt"]
                st, lt = d["st"], d["lt"]
                # bring in carried-forward losses
                c = loss_carry.pop(cls, None)
                if c:
                    # ST carry: offsets ST then LT; LT carry: offsets LT only
                    if c.get("st"):
                        use = min(-c["st"], max(0.0, st))
                        st -= use
                        c["st"] += use
                        if c["st"] < 0:
                            use2 = min(-c["st"], max(0.0, lt))
                            lt -= use2
                            c["st"] += use2
                    if c.get("lt"):
                        use = min(-c["lt"], max(0.0, lt))
                        lt -= use
                        c["lt"] += use
                    if c["st"] < -1e-9 or c["lt"] < -1e-9:
                        if final:
                            c["years"] -= 1
                        if c["years"] > 0:
                            loss_carry[cls] = c
                # current-year set-off
                if st < 0 and lt > 0:           # STCL vs LTCG
                    use = min(-st, lt)
                    lt -= use
                    st += use
                tax = (max(0.0, st) * st_rate
                       + max(0.0, lt - exempt) * lt_rate) * (1.0 + CESS_RATE)
                tax_accrued += tax
                cash -= tax
                if st < -1e-9 or lt < -1e-9:
                    loss_carry[cls] = {"st": min(st, 0.0), "lt": min(lt, 0.0),
                                       "years": LOSS_CARRY_YEARS}
            if TAX_ON_CASH_ACCRUAL and interest_unsettled > 0:
                itax = interest_unsettled * SLAB_RATE * (1.0 + CESS_RATE)
                tax_accrued += itax
                cash -= itax
            interest_unsettled = 0.0
        equity_curve = []
        daily_invested = []
        monthly_returns = []
        turnover_vals = []
        cond_vals = []
        delta_vals = []
        reach_count = 0
        month_count = 0
        risk_events = {"REVIEW": 0, "SEVERE": 0, "EXTREME": 0, "RECOVERY": 0}
        peak = self.capital
        halved = False
        all_cash_mode = False
        costs_paid = 0.0                  # slippage + statutory charges (for 'before costs' view)
        tax_accrued = 0.0
        # (external audit F13: the old `regime_returns` placeholder dict and the
        #  `monthly_rets` list were computed and passed around but never read —
        #  both removed)
        category_returns = {}
        trades = 0
        skipped = 0
        spread_used_vals = []
        div_haircut_paid = 0.0

        def holdings_value(date):
            v = 0.0
            for sym, h in holdings.items():
                p = px_at(sym, date)
                if p is None:
                    p = h["avg_cost"]
                v += h["qty"] * p
            return v

        def dvol_at(sym, date):
            """Daily vol for impact scaling (audit 3.6)."""
            i = pos_at(sym, date)
            if i is None:
                return 0.01
            seg = sym_adj[sym][max(0, i - VOL_WINDOW):i + 1]
            v = annualized_vol(seg, VOL_WINDOW) if len(seg) > VOL_WINDOW else None
            return (v or 0.15) / math.sqrt(252.0)

        def do_sell(sym, qty, price, date, charges_mult=1.0):
            nonlocal cash, tax_accrued, costs_paid
            h = holdings.get(sym)
            if not h or qty <= 0:
                return 0.0
            qty = min(qty, h["qty"])
            ref = price
            sp = self._spread_for(sym, date, spread_stat) * spread_mult
            category = self.meta.get(sym, {}).get("category", "EQUITY")
            liq = self._liq_at(sym, date)
            exec_px = slippage_price("SELL", ref, sp, 1.0,
                                     daily_vol=dvol_at(sym, date),
                                     order_value=qty * ref, liq_p25=liq)
            value = exec_px * qty
            ch = calc_charges("SELL", value, category=category)
            costs_paid += ch["total"] + abs(exec_px - ref) * qty
            cash += value - ch["total"]
            # tax lots -> FY bucket for netted settlement (audit #7);
            # LOT_SELECTION chooses the match order (audit 3.7)
            remaining = qty
            realized = 0.0
            category = self.meta.get(sym, {}).get("category", "EQUITY")
            cls = gain_class(category)
            months_need = tax_rules_for(category)["months"]
            fy = financial_year(parse_date(date))
            open_lots = [l for l in lots if l["sym"] == sym and l["qty"] > 0]
            if LOT_SELECTION == "HIFO":
                open_lots.sort(key=lambda l: -l["price"])
            else:
                open_lots.sort(key=lambda l: l["date"])
            for lot in open_lots:
                if lot["sym"] != sym or lot["qty"] <= 0:
                    continue
                take = min(remaining, lot["qty"])
                pnl = (exec_px - lot["price"]) * take
                is_lt = held_more_than_months(lot["date"], date, months_need)
                b = fy_buckets.setdefault((fy, cls), {"st": 0.0, "lt": 0.0})
                b["lt" if is_lt else "st"] += pnl
                lot["qty"] -= take
                remaining -= take
                realized += pnl
                if remaining <= 0:
                    break
            h["qty"] -= qty
            if h["qty"] <= 1e-9:
                del holdings[sym]
            return value

        def do_buy(sym, qty, price, date):
            nonlocal cash, costs_paid
            if qty <= 0:
                return 0.0
            ref = price
            sp = self._spread_for(sym, date, spread_stat) * spread_mult
            category = self.meta.get(sym, {}).get("category", "EQUITY")
            liq = self._liq_at(sym, date)
            exec_px = slippage_price("BUY", ref, sp, 1.0,
                                     order_value=qty * ref, liq_p25=liq,
                                     daily_vol=dvol_at(sym, date))
            # audit 3.4: skip dust trades (brokerage makes them poor value)
            if qty * exec_px < MIN_TRADE_VALUE:
                return 0.0
            value = exec_px * qty
            ch = calc_charges("BUY", value, category=category)
            total = value + ch["total"]
            costs_paid += ch["total"] + abs(exec_px - ref) * qty
            if total > cash:
                qty = int(cash / (exec_px * 1.002))
                if qty <= 0:
                    return 0.0
                value = exec_px * qty
                ch = calc_charges("BUY", value)
                total = value + ch["total"]
            cash -= total
            h = holdings.get(sym)
            if h:
                nq = h["qty"] + qty
                h["avg_cost"] = (h["qty"] * h["avg_cost"] + qty * exec_px) / nq
                h["qty"] = nq
            else:
                holdings[sym] = {"qty": qty, "avg_cost": exec_px}
            lots.append({"sym": sym, "date": date, "price": exec_px, "qty": qty})
            first_buy.setdefault(sym, date)
            return value

        overlay = self.params.get("exit_overlay", None) or (
            "200dma" if EXIT_OVERLAY_200DMA else None)

        def rank_metric(st):
            mom = st["momentum"] if st["momentum"] is not None else -9.0
            if RANK_BY == "risk_adjusted":
                return mom / (st["vol"] if st["vol"] > 0 else 1e-6)
            return mom

        def corr_ok(cand):
            if PAIRWISE_CORR_CAP <= 0 or len(cand) < 2:
                return True
            new = cand[-1]
            for other in cand[:-1]:
                ra = date_returns(new["rows"])
                rb = date_returns(other["rows"])
                common = sorted(set(ra) & set(rb))[-CORR_WINDOW:]
                if len(common) >= 20:
                    xa = [ra[d] for d in common]
                    xb = [rb[d] for d in common]
                    ma, mb = mean(xa), mean(xb)
                    num = sum((a - ma) * (b - mb) for a, b in zip(xa, xb))
                    da = math.sqrt(sum((a - ma) ** 2 for a in xa))
                    db_ = math.sqrt(sum((b - mb) ** 2 for b in xb))
                    if abs(safe_div(num, da * db_, 0.0)) > PAIRWISE_CORR_CAP:
                        return False
            return True

        def pick(ranked, k):
            # audit F8 (v3): no incumbency bonus here either — rotation governs
            out = []
            for st in ranked:
                if len(out) >= k:
                    break
                if st in out:
                    continue
                fam = st["index_family"]
                if ONE_PER_INDEX_FAMILY and any(s["index_family"] == fam for s in out):
                    continue
                if corr_ok(out + [st]):
                    out.append(st)
            # audit v10 seat_fill (live mirror: PortfolioBuilder._seat_fill):
            # relax ONLY the correlation cap to fill empty seats, in rank order
            if self.params.get("seat_fill") and len(out) < k:
                for st in ranked:
                    if len(out) >= k:
                        break
                    if st in out:
                        continue
                    fam = st["index_family"]
                    if ONE_PER_INDEX_FAMILY and any(s["index_family"] == fam
                                                    for s in out):
                        continue
                    out.append(st)
            return out

        sched = self.params.get("param_schedule")   # [(from_date, params), ...]

        def active_params(date):
            """Params in force at 'date' (walk-forward schedule, audit 2.8)."""
            if not sched:
                return lookback, skip, entry, exit_thr
            cur = (lookback, skip, entry, exit_thr)
            for d0, pd in sched:
                if date >= d0:
                    cur = (pd.get("lookback", lookback), pd.get("skip", skip),
                           pd.get("entry", entry), pd.get("exit", exit_thr))
            return cur

        def target_weights(date, mode_holdings):
            lookback, skip, entry, exit_thr = active_params(date)
            """Compute signal & sizing at 'date' for the chosen strategy.
            Mirrors the live selection engine (audit bugs #3/#4): cash-proxy
            exclusion, pre-ranking liquidity filter, correlation cap."""
            stats = []
            for sym in sym_dates:
                i = pos_at(sym, date)
                if i is None:
                    continue
                adj = sym_adj[sym][:i + 1]
                if len(adj) < lookback + 5:
                    continue
                mom = signal_momentum(adj, len(adj) - 1, lookback, skip)
                if mom is None:
                    continue
                vol = annualized_vol(adj, VOL_WINDOW)
                if plaus_excl != "none":   # EXPERIMENT: plausibility gate
                    cat = self.meta.get(sym, {}).get("category", "")
                    if plaus_excl in ("mom", "both"):
                        cap = MOM_PLAUSIBILITY.get(cat)
                        if cap is not None and abs(mom) > cap:
                            continue
                    if plaus_excl in ("vol", "both") and vol:
                        cap = VOL_PLAUSIBILITY.get(cat)
                        if cap is not None and vol > cap:
                            continue
                liq = percentile(sym_liq[sym][max(0, i - LIQ_WINDOW + 1):i + 1], LIQ_PCTL) or 0.0
                rows = [(d, a) for d, a in
                        zip(sym_dates[sym][max(0, i - CORR_WINDOW - 5):i + 1],
                            sym_adj[sym][max(0, i - CORR_WINDOW - 5):i + 1])]
                stats.append({"symbol": sym, "momentum": mom, "vol": vol or 1e-6,
                              "liq_p25": liq, "adj": adj, "rows": rows,
                              "index_family": self.meta.get(sym, {}).get("index_family", sym)})
            holding_syms = set(holdings.keys())
            decs = []
            for st in stats:
                # audit 2026-10-19: same integrity gate as the live engine
                i_chk = pos_at(st["symbol"], date)
                issue = series_integrity_issue(
                    sym_adj[st["symbol"]][max(0, (i_chk or 0) - 259):(i_chk or 0) + 1])
                if issue:
                    if st["symbol"] in holding_syms:
                        dec, reason = "EXIT", f"data integrity: {issue} — exit and block re-entry"
                    else:
                        dec, reason = "SKIPPED", f"data integrity: {issue}"
                    decs.append((st, dec, reason))
                    continue
                dec, reason = decide(st["momentum"], st["symbol"] in holding_syms,
                                     entry=entry, exit_thr=exit_thr)
                # exit overlay (audit 2.5): 200-day average break forces EXITS
                # and blocks ENTRIES — no guaranteed round trips either way
                if overlay == "200dma":
                    i = pos_at(st["symbol"], date)
                    seg = sym_adj[st["symbol"]][max(0, i - 200):i + 1]
                    below = bool(seg) and seg[-1] < sum(seg) / len(seg)
                    if below and st["symbol"] in holding_syms and dec in ("HOLD",):
                        dec, reason = "EXIT", ("exit overlay: price below the "
                                               "200-day average")
                    elif below and dec == "ELIGIBLE_BUY":
                        # audit v5 #6: must mirror the live select() rule —
                        # a strong trend in a brief 200-DMA dip stays buyable
                        # when ENTRY_OVERLAY_MOM_FLOOR is set
                        if not (ENTRY_OVERLAY_MOM_FLOOR is not None
                                and (st["momentum"] or 0.0) >= ENTRY_OVERLAY_MOM_FLOOR):
                            dec, reason = ("SKIPPED",
                                           "entry overlay: price below the 200-day average")
                # audit 3.3: tax-aware exit deferral (momentum exits only —
                # overlay/risk exits still fire)
                if dec == "EXIT" and reason and reason.startswith("trend turned negative"):
                    fb = first_buy.get(st["symbol"])
                    if fb:
                        defer = tax_defer_blocks_exit([fb], date, st["momentum"] or 0.0)
                        if defer:
                            dec, reason = "HOLD", defer
                if st["symbol"] in CASH_PROXY_SYMBOLS and dec in ("ELIGIBLE_BUY", "HOLD"):
                    dec = "CASH_SLEEVE"
                elif dec == "ELIGIBLE_BUY" and (
                        (POS_CAP_LIQ_FRAC * st["liq_p25"]) / max(eq, 1.0) < MIN_LIQ_WEIGHT):
                    dec, reason = "SKIPPED", "too illiquid to trade (pre-ranking filter)"
                decs.append((st, dec, reason))
            exits = [st for st, dec, _r in decs if dec == "EXIT"]
            eligible = [st for st, dec, _r in decs if dec == "ELIGIBLE_BUY"]
            holds = [st for st, dec, _r in decs if dec == "HOLD"]
            if self.strategy == "A":
                chosen = [st for st in (eligible + holds) if corr_ok([st])]
                ranked = list(chosen)
            else:
                pool = list(stats if self.strategy == "C" else (eligible + holds))
                pool = [st for st in pool if st["symbol"] not in CASH_PROXY_SYMBOLS
                        and (POS_CAP_LIQ_FRAC * st["liq_p25"]) / max(eq, 1.0) >= MIN_LIQ_WEIGHT]
                pool.sort(key=rank_metric, reverse=True)
                chosen = pick(pool, MAX_HOLDINGS)
                # audit 2.5: rotation parity with the live engine
                if ROTATION_RULE and self.strategy in ("B", "C"):
                    rank_pos = {st["symbol"]: i + 1 for i, st in enumerate(pool)}
                    if ROTATION_RANKFIRST:
                        # F3.2 variant: caps arbitrate in strict rank order —
                        # an incumbent ranked <= 5 has no seat priority against
                        # a higher-ranked newcomer it conflicts with
                        out = []
                        for st in pool:
                            if len(out) >= MAX_HOLDINGS:
                                break
                            if (st["symbol"] in holding_syms
                                    and rank_pos.get(st["symbol"], 99) > 5):
                                continue
                            fam = st["index_family"]
                            if ONE_PER_INDEX_FAMILY and any(
                                    s["index_family"] == fam for s in out):
                                continue
                            if corr_ok(out + [st]):
                                out.append(st)
                    else:
                        out = [st for st in pool
                               if st["symbol"] in holding_syms
                               and rank_pos.get(st["symbol"], 99) <= 5]
                        for st in pool:
                            if len(out) >= MAX_HOLDINGS:
                                break
                            if st in out:
                                continue
                            fam = st["index_family"]
                            if ONE_PER_INDEX_FAMILY and any(
                                    s["index_family"] == fam for s in out):
                                continue
                            if corr_ok(out + [st]):
                                out.append(st)
                    chosen = out
                ranked = list(pool)
            return chosen, exits, stats, decs, ranked

        def size_chosen(chosen, date, eq_now, scale):
            """Inverse-vol -> hard caps -> [opt: capped-excess water-filling] ->
            covariance vol-target scaling.  Extracted so the signal branch and
            the exec-time refresh knob size EXACTLY the same way (external
            audit F10 discipline: one sizing engine, no live/backtest drift
            inside the backtester itself)."""
            nonlocal month_count, reach_count
            if not chosen:
                month_count += 1
                return {}
            n = len(chosen)
            if self.strategy == "A":
                raw = [1.0 / n] * n
            else:
                inv = [1.0 / s["vol"] if s["vol"] > 0 else 1.0 for s in chosen]
                ss = sum(inv)
                raw = [x / ss for x in inv]
            w = []
            caps = []
            for k, st in enumerate(chosen):
                cap = min(POS_CAP_ABS,
                          POS_CAP_LIQ_FRAC * st["liq_p25"] / eq_now if eq_now > 0 else 0.0)
                caps.append(cap)
                w.append(min(raw[k], cap))
            if renorm_excess:
                # EXPERIMENT (external audit F5): distribute capped-out excess
                # to uncapped names pro-rata to their raw inverse-vol weight
                for _ in range(10):
                    excess = sum(raw) - sum(w)
                    room = [k for k in range(n)
                            if w[k] < caps[k] - 1e-12 and raw[k] > 1e-12]
                    if excess <= 1e-9 or not room:
                        break
                    tot_raw = sum(raw[k] for k in room)
                    added = 0.0
                    for k in room:
                        inc = min(caps[k] - w[k], excess * raw[k] / tot_raw)
                        w[k] += inc
                        added += inc
                    if added <= 1e-12:
                        break
            # covariance — DATE-ALIGNED returns (audit bug #9)
            series_by_sym = {}
            for st in chosen:
                rows = []
                cnt = 0
                for d2 in reversed(all_dates[:all_dates.index(date) + 1]):
                    i2 = pos_at(st["symbol"], d2)
                    if i2 is None:
                        continue
                    rows.append((d2, sym_adj[st["symbol"]][i2]))
                    cnt += 1
                    if cnt > COV_WINDOW + 5:
                        break
                series_by_sym[st["symbol"]] = list(reversed(rows))
            X, _s, _d = aligned_return_matrix(series_by_sym, COV_WINDOW)
            Tt = len(X)
            if Tt >= 20:
                Xd = demean(X)
                Sigma, delta, cond = ledoit_wolf_cc(Xd)
                delta_vals.append(delta)
                cond_vals.append(cond)
            else:
                Sigma = [[0.0] * n for _ in range(n)]
                for k in range(n):
                    Sigma[k][k] = (chosen[k]["vol"] or 0.0) ** 2
            raw_vol = portfolio_vol(w, Sigma) * math.sqrt(252.0)  # annualised
            reg_mult = 1.0
            if REGIME_SCALING and bench_sym:
                bi2 = pos_at(bench_sym, date)
                if bi2 is not None and bi2 >= 200:
                    seg2 = sym_adj[bench_sym][bi2 - 200:bi2 + 1]
                    if seg2[-1] < sum(seg2) / len(seg2):
                        reg_mult = 0.5
            tgt_vol = VOL_TARGET * scale * reg_mult
            if raw_vol <= 0:
                s_scale = 1.0
            elif raw_vol <= tgt_vol:
                s_scale = 1.0          # accept LOWER risk (rulebook Step 6.5)
            else:
                s_scale = tgt_vol / raw_vol
                if tgt_vol > 0:
                    reach_count += 1   # vol target reachable inside the caps
            month_count += 1
            out_w = {chosen[k]["symbol"]: w[k] * s_scale for k in range(n)}
            # sector_cap_frac param (audit F10 v3): combined cap for sectoral/
            # thematic Indian ETFs — mirrors the SECTOR_CAP_FRAC live knob
            _sc = float(self.params.get("sector_cap_frac", SECTOR_CAP_FRAC))
            if _sc > 0:
                sec = [s for s in out_w if is_sector_thematic(s)]
                tot = sum(out_w[s] for s in sec)
                if tot > _sc > 0:
                    for s in sec:
                        out_w[s] *= _sc / tot
            # park_bench param (audit v10, external auditor lever 1c): the plan's
            # uninvested weight rides the benchmark ETF while the benchmark
            # itself passes trend + 200-DMA overlay — instead of idling in cash
            if (self.params.get("park_bench", False) and bench_sym
                    and bench_sym not in out_w and bench_sym in sym_adj):
                _left = 1.0 - sum(out_w.values())
                _bp = pos_at(bench_sym, date)
                if _left > 0.02 and _bp is not None:
                    _bm = signal_momentum(sym_adj[bench_sym], _bp, lookback, skip)
                    _bseg = sym_adj[bench_sym][max(0, _bp - 200):_bp + 1]
                    _bok = _bm is not None and _bm > entry
                    if _bok and EXIT_OVERLAY_200DMA and _bseg:
                        _bok = _bseg[-1] >= sum(_bseg) / len(_bseg)
                    if _bok:
                        out_w[bench_sym] = _left
            return out_w

        # benchmark series (first large ETF or equal basket) for regime tagging
        bench_sym = None
        for cand in ("NIFTYBEES", "HDFCSENSEX", "MONIFTY500"):
            if cand in sym_dates and len(sym_dates[cand]) > 250:
                bench_sym = cand
                break
        if bench_sym is None and sym_dates:
            bench_sym = max(sym_dates, key=lambda s: len(sym_dates[s]))

        # ---------------- main day loop ----------------
        prev_signal = None
        qtr_state = [-1]       # mutable quarter marker for quarterly settles
        exec_buffer = []       # (signal_date, weights, chosen, exits)
        lock_date = None       # when halved/all-cash engaged (time-based release)
        in_review = False      # review = STATE TRANSITIONS, not days (audit 2.9)
        for date in all_dates:
            # cash interest accrual (daily) + tax settlement (audit #7; external
            # audit F3.10/3.11: quarterly advance-tax-style settles instead of
            # one April hit — tax cash leaves when gains accrue, and the equity
            # curve no longer shows one artificial mega-drop per year)
            fy_now = financial_year(parse_date(date))
            if fy_now != current_fy:
                settle_fy(current_fy, final=True)
                current_fy = fy_now
                qtr_state[0] = -1
            if SETTLE_MODE == "quarterly":
                m_now = parse_date(date).month
                q_now = (m_now - 1) // 3
                if qtr_state[0] < 0:
                    qtr_state[0] = q_now
                elif q_now != qtr_state[0]:
                    qtr_state[0] = q_now
                    settler_months = {m % 12 + 1 for m in SETTLE_QUARTER_MONTHS}
                    if m_now in settler_months:  # first session after Jun/Sep/Dec/Mar ends
                        settle_fy(fy_now, final=(m_now == 4))
            daily_r = cash_fn(date)          # sleeve history or flat proxy
            before = cash
            cash *= (1.0 + daily_r)
            interest_earned += cash - before
            interest_unsettled += cash - before

            # ---- EXPERIMENT daily_exit: immediate exit sweep each session ---
            # A name exited here is also blocked from re-entering in today's
            # pending-rebalance execution (no same-day exit+buy whipsaw).
            _dx_today = set()
            if daily_exit and holdings:
                for _dx in list(holdings.keys()):
                    _i = pos_at(_dx, date)
                    if _i is None or _i < 260:
                        continue
                    _ad = sym_adj[_dx]
                    _mom = signal_momentum(_ad, _i)
                    _ma200 = sum(_ad[_i - 199:_i + 1]) / 200.0
                    if (_mom is not None and _mom <= EXIT_THRESHOLD) or _ad[_i] < _ma200:
                        do_sell(_dx, holdings.get(_dx, {}).get("qty", 0), _ad[_i], date)
                        trades += 1
                        _dx_today.add(_dx)

            # execute pending rebalance (next session by default; exec_lag is a
            # params-dict EXPERIMENT knob for deployment-speed A/B tests —
            # default 1 reproduces the production next-session rule exactly)
            if (exec_buffer
                    and date_index[date] >= date_index[exec_buffer[0][0]] + exec_lag):
                _ent = exec_buffer.pop(0)
                sig_date, weights, chosen, exits, ranked = _ent[:5]
                # audit v12 leak-3 EXPERIMENT deploy_ramp (default 1 = unchanged
                # production): emulate the live AUTOFILL pacing — buys deploy
                # the remaining gap / remaining sessions, so a ramp of R spreads
                # entries across R sessions exactly like gap/min(sessions,R).
                # Sells always execute in full at the first tranche.  Tranche
                # k>0 re-queues itself (domino: fires the next session).
                _k, _R = (_ent[5], _ent[6]) if len(_ent) > 6 else (0, 1)
                _R = max(1, _R)
                if _k == 0:
                    _orig_w = dict(weights)
                else:
                    _orig_w = _ent[7]
                if _k == 0 and daily_exit and _dx_today:
                    # same-day whipsaw guard: a name the daily exit just sold
                    # is not re-bought from the pending plan today
                    weights = {s: (0.0 if s in _dx_today else w)
                               for s, w in weights.items()}
                # ---- EXPERIMENT stale_skip_days: signal aged past N sessions ->
                # suppress NEW entries (exits/trims still run).  Alternative to
                # refresh_late's re-derive: WAIT for the next clean signal.
                if (_k == 0 and stale_skip_days > 0 and sig_date in date_index
                        and date_index[date] - date_index[sig_date] > stale_skip_days):
                    weights = {s: (w if s in holdings else 0.0)
                               for s, w in weights.items()}
                    _orig_w = dict(weights)
                # ---- EXPERIMENT refresh_late (external audit F2): a LATE
                # journal — e.g. a fresh start mid-month — re-derives basket +
                # weights from the exec date's data instead of deploying the
                # stale signal.  Default 0 = production (month-end signal
                # always governs the cycle).
                if (_k == 0 and refresh_late > 0 and sig_date in date_index
                        and date_index[date] - date_index[sig_date] > refresh_late):
                    _eq = cash + holdings_value(date)
                    _scale = 1.0
                    if self.risk_on:
                        if all_cash_mode:
                            _scale = 0.0
                        elif halved:
                            _scale = 0.5
                    chosen, exits, stats, decs, ranked = target_weights(
                        date, set(holdings.keys()))
                    weights = size_chosen(chosen, date, _eq, _scale)
                    for st in exits:
                        weights[st["symbol"]] = 0.0
                    cur_w = ({s: (h["qty"] * (px_at(s, date) or h["avg_cost"])) / _eq
                              for s, h in holdings.items()} if _eq > 0 else {})
                    turnover_vals.append(min(2.0, sum(
                        abs(weights.get(s, 0.0) - cur_w.get(s, 0.0))
                        for s in set(list(weights.keys()) + list(cur_w.keys())))))
                # audit v12 leak-3 deploy_ramp pacing: sells pass in full;
                # buys take remaining-gap / remaining-tranches (mirrors live
                # autofill's gap/min(sessions,R) arithmetic); <R tranches
                # re-queue for the next session
                if _R > 1:
                    _eqx = cash + holdings_value(date)
                    _curx = ({s: (h["qty"] * (px_at(s, date) or h["avg_cost"])) / _eqx
                              for s, h in holdings.items()} if _eqx > 0 else {})
                    _rem = _R - _k
                    weights = {}
                    for s, w in _orig_w.items():
                        c = _curx.get(s, 0.0)
                        weights[s] = w if w <= c else c + (w - c) / _rem
                    if _k + 1 < _R:
                        exec_buffer.append((sig_date, dict(_orig_w), chosen,
                                            exits, ranked, _k + 1, _R, _orig_w))
                # Step 7: re-check filters at execution time
                eq_now = cash + holdings_value(date)
                # sells
                for sym in list(holdings.keys()):
                    tgt_w = weights.get(sym, 0.0)
                    h = holdings[sym]
                    p = px_at(sym, date) or h["avg_cost"]
                    have_val = h["qty"] * p
                    tgt_val = tgt_w * eq_now
                    if tgt_val < have_val - 1.0:
                        is_exit = tgt_w <= 0.0
                        # audit 3.1: no-trade band — exits ALWAYS execute
                        if (not is_exit and NO_TRADE_BAND > 0 and have_val > 0
                                and abs(tgt_val - have_val) / max(tgt_val, have_val)
                                < NO_TRADE_BAND):
                            continue
                        sell_val = have_val - tgt_val
                        if not is_exit and sell_val < _min_trade(eq_now):
                            continue
                        qty = int(sell_val / p) if p > 0 else 0
                        qty = min(qty, int(h["qty"]))
                        if qty > 0:
                            v = do_sell(sym, qty, p, date)
                            trades += 1
                # buys
                eq_now = cash + holdings_value(date)
                # ---- EXPERIMENT: exec-date trend guard -----------------------
                # Default "none" reproduces the legacy monthly exec exactly.
                # With a policy set, build {sym: buy_value} from the planned
                # weights, drop names tripping the trend guard AT THE EXEC DATE
                # (the live SIP_TREND_GUARD analog), then park / redistribute /
                # substitute the freed budget.  Simplified policy test: the
                # substitute refill reuses the signal-date ranking/stats and
                # skips the one-per-family / correlation re-checks (noted in
                # experiments/REPORT_2026-10-27.md when adopted or rejected).
                buy_plan = None
                if exec_guard != "none":
                    gaps = {}
                    for sym, w in weights.items():
                        if w <= 0 or all_cash_mode:
                            continue
                        p = px_at(sym, date)
                        if p is None:
                            continue
                        have = holdings.get(sym)
                        have_val = (have["qty"] * p) if have else 0.0
                        tgt_val = w * eq_now
                        if tgt_val <= have_val + 1.0:
                            continue
                        g = tgt_val - have_val
                        if (NO_TRADE_BAND > 0 and have_val > 0
                                and abs(tgt_val - have_val) / max(tgt_val, have_val)
                                < NO_TRADE_BAND):
                            continue
                        if g >= _min_trade(eq_now):
                            gaps[sym] = g
                    tripped = []
                    for sym in list(gaps.keys()):
                        i = pos_at(sym, date)
                        if i is None:
                            continue
                        mom_x = signal_momentum(sym_adj[sym], i, lookback, skip)
                        below_x = False
                        if EXIT_OVERLAY_200DMA and i >= 199:
                            seg = sym_adj[sym][i - 199:i + 1]
                            below_x = seg[-1] < sum(seg) / len(seg)
                        if (mom_x is not None and mom_x <= exit_thr) or below_x:
                            tripped.append(sym)
                    freed = 0.0
                    for sym in tripped:
                        freed += gaps.pop(sym, 0.0)
                    if freed > 0:
                        if exec_guard == "redistribute" and gaps:
                            tot = sum(gaps.values())
                            mult = (tot + freed) / tot if tot > 0 else 1.0
                            gaps = {s: v * mult for s, v in gaps.items()}
                        elif exec_guard == "substitute" and ranked:
                            subs = []
                            for st in ranked:
                                if len(subs) >= len(tripped):
                                    break
                                sym2 = st["symbol"]
                                if sym2 in weights or sym2 in CASH_PROXY_SYMBOLS:
                                    continue
                                if (st["momentum"] or 0.0) <= entry:
                                    continue
                                i2 = pos_at(sym2, date)
                                if i2 is None:
                                    continue
                                mom2x = signal_momentum(sym_adj[sym2], i2,
                                                        lookback, skip)
                                below2 = False
                                if EXIT_OVERLAY_200DMA and i2 >= 199:
                                    seg = sym_adj[sym2][i2 - 199:i2 + 1]
                                    below2 = seg[-1] < sum(seg) / len(seg)
                                if mom2x is None or mom2x <= exit_thr or below2:
                                    continue
                                subs.append(st)
                            if subs:
                                inv = [1.0 / max(s["vol"] or 0.0, 1e-6) for s in subs]
                                ss = sum(inv)
                                for s2, iv in zip(subs, inv):
                                    cap2 = min(POS_CAP_ABS,
                                               POS_CAP_LIQ_FRAC * s2["liq_p25"] / eq_now
                                               if eq_now > 0 else 0.0)
                                    alloc = min(freed * iv / ss, cap2 * eq_now)
                                    if alloc >= _min_trade(eq_now):
                                        gaps[s2["symbol"]] = alloc
                        # exec_guard == "park": the freed value idles in cash
                    buy_plan = gaps
                loop_items = (sorted(buy_plan.items(), key=lambda kv: -kv[1])
                              if buy_plan is not None
                              else [(s, None) for s, _w in
                                    sorted(weights.items(), key=lambda kv: -kv[1])
                                    if _w > 0])
                for sym, planned in loop_items:
                    w = weights.get(sym, 0.0)
                    if all_cash_mode:
                        continue
                    if buy_plan is None and w <= 0:
                        continue
                    p = px_at(sym, date)
                    if p is None:
                        skipped += 1
                        continue
                    have = holdings.get(sym)
                    have_val = (have["qty"] * p) if have else 0.0
                    if planned is not None:
                        buy_val = planned
                    else:
                        tgt_val = w * eq_now
                        if tgt_val <= have_val + 1.0:
                            continue
                        buy_val = tgt_val - have_val
                        # audit 3.1: no-trade band + dust guard on entries
                        if (NO_TRADE_BAND > 0 and have_val > 0
                                and abs(tgt_val - have_val) / max(tgt_val, have_val)
                                < NO_TRADE_BAND):
                            continue
                        if buy_val < _min_trade(eq_now):
                            continue
                    # liquidity binding rule
                    i = pos_at(sym, date)
                    liq = percentile(sym_liq[sym][max(0, i - LIQ_WINDOW + 1):i + 1], LIQ_PCTL) or 0.0
                    if buy_val > POS_CAP_LIQ_FRAC * liq:
                        buy_val = POS_CAP_LIQ_FRAC * liq
                    if planned is not None and buy_val < _min_trade(eq_now):
                        continue   # planned buys re-check dust after the liq cap
                    qty = int(buy_val / p) if p > 0 else 0
                    if qty > 0:
                        v = do_buy(sym, qty, p, date)
                        if v > 0:
                            trades += 1
                        else:
                            skipped += 1
                # external audit F3.4: report the ACTUAL value-weighted spread
                # across the names traded this rebalance, not a NIFTYBEES stand-in
                tw_num, tw_den = 0.0, 0.0
                for s in set(list(weights.keys()) + list(holdings.keys())):
                    h = holdings.get(s)
                    curv = (h["qty"] * (px_at(s, date) or h["avg_cost"])) if h else 0.0
                    dw = weights.get(s, 0.0) - (curv / eq_now if eq_now else 0.0)
                    if abs(dw) > 0.001:
                        tw_num += abs(dw) * self._spread_for(s, date, spread_stat)
                        tw_den += abs(dw)
                if tw_den > 0:
                    spread_used_vals.append(tw_num / tw_den)

            # month-end signal -> schedule next-session execution
            if date in signal_set:
                if date != all_dates[-1]:
                    chosen, exits, stats, decs, ranked = target_weights(date, set(holdings.keys()))
                    eq_now = cash + holdings_value(date)
                    scale = 1.0
                    if self.risk_on:
                        if all_cash_mode:
                            scale = 0.0
                        elif halved:
                            scale = 0.5
                    # size via the shared engine (audit F10 discipline)
                    weights = size_chosen(chosen, date, eq_now, scale)
                    for st in exits:
                        weights[st["symbol"]] = 0.0
                    # external audit F3.3: turnover = Σ|Δw| (traded fraction),
                    # NOT Σ|w| (invested fraction — a held-unchanged portfolio
                    # is not turnover)
                    cur_w = ({s: (h["qty"] * (px_at(s, date) or h["avg_cost"])) / eq_now
                              for s, h in holdings.items()} if eq_now > 0 else {})
                    turnover_vals.append(min(2.0, sum(
                        abs(weights.get(s, 0.0) - cur_w.get(s, 0.0))
                        for s in set(list(weights.keys()) + list(cur_w.keys())))))
                    exec_buffer.append((date, weights, chosen, exits, ranked,
                                        0, deploy_ramp, dict(weights)))

            # ---- EXPERIMENT: mid-month guard-rail exits (default OFF) -------
            # Rationale (2026-10-27 live audit): holdings whose exit rules fire
            # mid-month were held bleeding until the next month-end.  With
            # guard_freq = N this sells them at the NEXT session's close.
            if (guard_freq > 0 and holdings and date != all_dates[-1]
                    and date not in signal_set
                    and date_index[date] % guard_freq == guard_freq - 1):
                nxt = all_dates[date_index[date] + 1]
                for sym in list(holdings.keys()):
                    i = pos_at(sym, date)
                    if i is None or i < 1:
                        continue
                    h = holdings[sym]
                    mom = momentum_at(sym_adj[sym], i, lookback, skip)
                    if mom is None:
                        continue
                    below = False
                    if EXIT_OVERLAY_200DMA and guard_mode in ("both", "dma") and i >= 199:
                        seg = sym_adj[sym][i - 199:i + 1]
                        below = sym_adj[sym][i] < sum(seg) / len(seg)
                    trip = False
                    if guard_mode in ("both", "mom") and mom <= exit_thr:
                        trip = True
                    if below:
                        trip = True
                    if trip:
                        p = px_at(sym, nxt)
                        if p:
                            v = do_sell(sym, int(h["qty"]), p, nxt)
                            if v > 0:
                                trades += 1

            # mark to market
            hv = holdings_value(date)
            eq = cash + hv
            if eq > peak:
                peak = eq
            dd = 1.0 - eq / peak if peak > 0 else 0.0
            if self.risk_on:
                dd_rev_m, dd_sev_m, dd_ext_m = dd_thresholds(VOL_TARGET)
                # audit v10 knob (external auditor point 6): with ~half the book
                # in cash, TOTAL-equity triggers need a ~43% holdings crash to
                # halve — scale thresholds by the current invested fraction
                # (floor 0.35 so a near-empty book doesn't trip on noise)
                if self.params.get("dd_sleeve_scale", False):
                    _inv = max(hv / eq, 0.35) if eq > 0 else 1.0
                    dd_rev_m *= _inv
                    dd_sev_m *= _inv
                    dd_ext_m *= _inv
                if dd >= dd_ext_m and not all_cash_mode:
                    all_cash_mode = True
                    lock_date = date
                    risk_events["EXTREME"] += 1
                    for sym in list(holdings.keys()):
                        p = px_at(sym, date) or holdings[sym]["avg_cost"]
                        do_sell(sym, int(holdings[sym]["qty"]), p, date)
                elif dd >= dd_sev_m and not halved and not all_cash_mode:
                    halved = True
                    lock_date = date
                    risk_events["SEVERE"] += 1
                if dd >= dd_rev_m and not in_review:
                    in_review = True          # audit 2.9: count EVENTS not days
                    risk_events["REVIEW"] += 1
                elif dd < dd_rev_m:
                    in_review = False
                # recovery: price band OR time-based release (audit 2.5 parity)
                time_release = (lock_date is not None and
                                (parse_date(date) - parse_date(lock_date)).days
                                >= RISK_RELEASE_DAYS)
                if (halved or all_cash_mode) and (dd <= REENTRY_BAND or time_release):
                    halved = False
                    all_cash_mode = False
                    risk_events["RECOVERY"] += 1
                    if dd > REENTRY_BAND:
                        peak = eq            # time-based: reset the slate
                    lock_date = None
            equity_curve.append((date, eq))
            daily_invested.append(safe_div(hv, eq, 0.0))
            # (external audit F13: benchmark regime tagging + monthly_rets were
            #  collected into never-read structures — removed)

        # audit 2.1: settle the final FY BEFORE the final curve point is used,
        # so equity_curve[-1] is already NET of all taxes
        settle_fy(current_fy, final=True)
        if equity_curve:
            last_date = equity_curve[-1][0]
            hv_now = holdings_value(last_date)
            equity_curve[-1] = (last_date, cash + hv_now)
        result = self._summarize(
            all_dates, equity_curve, daily_invested, turnover_vals,
            cond_vals, delta_vals, reach_count, month_count, risk_events,
            trades, skipped, tax_accrued, costs_paid, cash, holdings, px_at,
            spread_used_vals, lookback, entry, skip, exit_thr)
        result["interest_earned"] = interest_earned
        return result

    def _liq_at(self, sym, date):
        """25th-percentile daily traded value at 'date' (for impact sizing)."""
        series = self.prices.get(sym, [])
        vals = []
        for (d, _a, _c, t) in reversed(series):
            if d > date:
                continue
            vals.append(t)
            if len(vals) >= LIQ_WINDOW:
                break
        return percentile(vals, LIQ_PCTL) or 0.0

    def _spread_for(self, sym, date, spread_stat):
        """Liquidity-tiered spread estimate (audit #8: thin ETFs cost more)."""
        broad = self.meta.get(sym, {}).get("broad", False)
        base = estimate_spread_bps(self._liq_at(sym, date), broad)
        if spread_stat == "p90":
            return base * 1.2
        return base * 0.8

    def _summarize(self, all_dates, equity_curve, daily_invested,
                   turnover_vals, cond_vals, delta_vals, reach_count, month_count,
                   risk_events, trades, skipped, tax_accrued, costs_paid, cash, holdings,
                   px_at, spread_used_vals, lookback, entry, skip, exit_thr):
        if not equity_curve:
            return {"error": "empty backtest"}
        eqs = [e for (_d, e) in equity_curve]
        d0 = all_dates[0]
        d1 = all_dates[-1]
        years = max((parse_date(d1) - parse_date(d0)).days / 365.25, 1e-9)
        # audit 2.1: taxes are ALREADY deducted from cash in-run (settle_fy).
        # Subtracting tax_accrued again here was the double-count.  The curve's
        # final point is net of costs AND taxes; pre-tax figures come from
        # adding tax back (labelled) or from a separate no-tax run.
        final_after_tax = eqs[-1]                     # net of costs AND taxes
        final = final_after_tax
        final_after_cost = final_after_tax + tax_accrued     # pre-personal-tax
        final_before_costs = final_after_cost + costs_paid   # pre-cost too
        cagr_before_costs = (max(final_before_costs, 1.0) / self.capital) ** (1.0 / years) - 1.0
        cagr_after_costs = (max(final_after_cost, 1.0) / self.capital) ** (1.0 / years) - 1.0
        cagr_after_tax = (max(final_after_tax, 1.0) / self.capital) ** (1.0 / years) - 1.0
        cagr = cagr_after_tax
        # daily vol & STANDARD Sharpe = mean(excess)/std(excess) x sqrt(252)
        # (audit #9: (CAGR - rf)/vol is not a Sharpe ratio)
        rets = []
        for i in range(1, len(eqs)):
            if eqs[i - 1] > 0:
                rets.append(eqs[i] / eqs[i - 1] - 1.0)
        vol = stdev(rets, ddof=1) * math.sqrt(252.0) if len(rets) > 2 else 0.0
        rf_daily = (1.0 + self.cash_rate) ** (1.0 / 252.0) - 1.0
        excess = [r - rf_daily for r in rets]
        sharpe = (mean(excess) / stdev(excess, ddof=1) * math.sqrt(252.0)
                  if len(excess) > 2 and stdev(excess, ddof=1) > 0 else 0.0)
        # max drawdown & recovery
        peak = eqs[0]
        max_dd = 0.0
        dd_start = None
        recovery_days = 0
        worst_len = 0
        for i, e in enumerate(eqs):
            if e > peak:
                if dd_start is not None:
                    worst_len = max(worst_len, i - dd_start)
                peak = e
                dd_start = None
            else:
                if dd_start is None:
                    dd_start = i
                dd = 1.0 - e / peak
                if dd > max_dd:
                    max_dd = dd
        if dd_start is not None:
            worst_len = max(worst_len, len(eqs) - dd_start)
        recovery_days = worst_len
        avg_inv = mean(daily_invested) if daily_invested else 0.0
        cash_w = [1.0 - x for x in daily_invested]
        cash_p25 = percentile(cash_w, 25) or 0.0
        cash_p50 = percentile(cash_w, 50) or 0.0
        cash_p75 = percentile(cash_w, 75) or 0.0
        avg_turnover = mean(turnover_vals) if turnover_vals else 0.0
        avg_cond = mean(cond_vals) if cond_vals else 0.0
        avg_delta = mean(delta_vals) if delta_vals else 0.0
        reach_pct = safe_div(reach_count, month_count, 0.0)
        return {
            "start": d0, "end": d1, "years": years,
            "capital": self.capital, "final_equity": final_after_tax,
            "final_equity_after_cost": final_after_cost,
            "cagr_before_costs": cagr_before_costs,
            "cagr_after_cost": cagr_after_costs,
            "cagr_after_cost_tax": cagr_after_tax,
            "cagr_pretax": cagr_after_costs,
            "equity_curve": equity_curve,
            "invested_curve": list(zip([d for d, _e in equity_curve], daily_invested)),
            "volatility": vol, "sharpe": sharpe,
            "max_drawdown": max_dd, "recovery_days": recovery_days,
            "avg_invested": avg_inv, "cash_p25": cash_p25, "cash_p50": cash_p50,
            "cash_p75": cash_p75, "avg_turnover": avg_turnover,
            "avg_cond": avg_cond, "avg_delta": avg_delta,
            "vol_target_reachable_pct": reach_pct,
            "risk_events": {k: v for k, v in risk_events.items() if not k.startswith("_")},
            "trades": trades, "skipped": skipped, "tax_accrued": tax_accrued,
            "costs_paid": costs_paid,
            "months": month_count, "cost_case": self.cost_case,
            "cash_rate": self.cash_rate, "strategy": self.strategy,
            "cash_sleeve": getattr(self, "_cash_fn_flag", False),
            "params": {"lookback": lookback, "skip": skip, "entry": entry, "exit": exit_thr},
            "final_cash": cash, "final_holdings": dict(holdings),
            "avg_spread_bps": mean(spread_used_vals) if spread_used_vals else 0.0,
        }


# ==============================================================================
# SECTION 21 — REPORTING (beautiful console + saved text reports)
# ==============================================================================

def load_prices_from_db(db: Db) -> tuple:
    """prices dict + meta dict for the backtester."""
    prices = {}
    meta = {}
    for r in db.query("SELECT symbol, name, category, index_family, broad FROM etf_universe"):
        meta[r["symbol"]] = {"name": r["name"], "category": r["category"],
                             "index_family": r["index_family"], "broad": bool(r["broad"])}
    for sym in db.symbols_with_prices():
        rows = db.price_series(sym)
        prices[sym] = [(r["date"], r["adj_close"], r["close"], r["traded_value"] or 0.0)
                       for r in rows]
    return prices, meta


def print_backtest_result(res: dict, title: str = "BACKTEST RESULT"):
    if res.get("error"):
        P.err(f"Backtest could not run: {res['error']}")
        return
    P.section(title, "\U0001F9EA")
    p = res["params"]
    P.kv("Period", f"{res['start']}  ->  {res['end']}   ({res['years']:.1f} years)")
    P.kv("Strategy", {"A": "A — Pure TSMOM (all qualifying ETFs)",
                      "B": f"B — TSMOM + Ranking (top {MAX_HOLDINGS})",
                      "C": f"C — Ranking only (top {MAX_HOLDINGS}, no threshold)"}[res["strategy"]])
    P.kv("Parameters", f"lookback {p['lookback']} | skip {p['skip']} | "
                       f"entry {fmt_pct(p['entry'],0)} | exit {fmt_pct(p['exit'],0)}")
    P.kv("Cost scenario", f"{res['cost_case']}  (avg trade-weighted spread {res['avg_spread_bps']:.1f} bps)")
    P.note("Scope-honesty: this engine models the month-end rebalance cycle. "
           "The live path deploys with budget autofill (plan / market-days to "
           "re-plan, floor INVEST_PER_DAY) — engineered to match this engine's "
           "next-session pace (audit v4 §2 measured ~1pp CAGR per 10 sessions "
           "of lag; flat 5000/day would have lagged ~180 sessions). NOT modelled "
           "here: the live-only iNAV premium gate, the daily trend guard, and "
           "live execution statuses. Tuned knobs are in-sample choices (round-9 "
           "open item: true-holdout re-tune); the OOS split covers the signal "
           "grid only. Rotation, late-exec guards, plausibility-flag exclusion "
           "(P1/P2/P3: 11.43/11.53/11.17 vs 11.80 full, worse OOS) and sector "
           "caps were all MEASURED and rejected "
           "(R6-R10) — the knobs stay off on evidence, not on default.")
    if res.get("cash_sleeve"):
        P.kv("Cash assumption",
             f"real liquid-sleeve history ({CASH_SLEEVE_SYMBOL} daily returns)")
    else:
        P.kv("Cash assumption",
             f"{fmt_pct(res['cash_rate'],2)} per year (flat T-bill proxy)")

    P.raw()
    rows = [
        ["Starting capital", fmt_cr(res["capital"])],
        ["Final equity (after costs & taxes)", fmt_cr(res["final_equity"])],
        ["CAGR — before costs", fmt_pct(res["cagr_before_costs"])],
        ["CAGR — after costs (pre-tax add-back)", fmt_pct(res["cagr_after_cost"])],
        ["CAGR — after costs + taxes (net)", fmt_pct(res["cagr_after_cost_tax"])],
        ["Total costs paid (spread+charges)", fmt_cr(res.get("costs_paid", 0.0))],
        ["Cash interest earned (cash carry)", fmt_cr(res.get("interest_earned", 0.0))],
        ["Annual volatility", fmt_pct(res["volatility"])],
        ["Sharpe ratio (standard, daily excess vs cash)", f"{res['sharpe']:.2f}"],
        ["Maximum drawdown", fmt_pct(-res["max_drawdown"])],
        ["Longest underwater stretch", f"{res['recovery_days']} sessions"],
        ["Average invested fraction", fmt_pct(res["avg_invested"])],
        ["Cash weight p25 / p50 / p75",
         f"{fmt_pct(res['cash_p25'],0)} / {fmt_pct(res['cash_p50'],0)} / {fmt_pct(res['cash_p75'],0)}"],
        ["Avg monthly turnover Σ|Δw|", f"{res['avg_turnover']:.2f}x"],
        [f"Months {fmt_pct(VOL_TARGET, 0)} vol target reachable",
         fmt_pct(res["vol_target_reachable_pct"], 1)],
        ["Avg Ledoit-Wolf shrinkage", f"{res['avg_delta']:.3f}"],
        ["Avg covariance condition no.", f"{res['avg_cond']:,.1f}"],
        ["Total trades / skipped", f"{res['trades']} / {res['skipped']}"],
        ["Tax accrued (STCG+LTCG)", fmt_cr(res["tax_accrued"])],
    ]
    P.table(["Metric", "Value"], rows, aligns=["<", ">"])
    ev = res["risk_events"]
    P.sub("Risk trigger events (Step 9)")
    _dr, _ds, _dx = dd_thresholds()
    P.info(f"{fmt_pct(_dr,0)} reviews: {ev.get('REVIEW',0)}   |   "
           f"{fmt_pct(_ds,0)} halvings: {ev.get('SEVERE',0)}   |   "
           f"{fmt_pct(_dx,0)} cash-outs: {ev.get('EXTREME',0)}   |   "
           f"recoveries: {ev.get('RECOVERY',0)}")
    P.note("Drawdown triggers are risk controls, NOT guaranteed stop-losses — "
           "gaps and illiquid markets can produce larger losses.")
    # external audit 2026-10-27 (F11.1/F15): honesty block — what these numbers
    # do and do NOT establish
    P.sub("How to read these numbers (audit honesty notes)")
    P.note("“Pre-tax add-back” re-adds taxes at the END (they do not compound "
           "out of later years) — an upper bound, not a no-tax scenario.")
    P.note("Tuned knobs (vol target, corr cap, N=5, rotation, overlays) were "
           "picked on FULL history; the 80/20 OOS split only covers "
           "lookback/skip/entry/exit. Expect a live implementation to land "
           "~1.5–3pp CAGR below this headline (in-sample selection shrinkage).")
    P.note("Costs assume a discount broker (0.05% capped Rs 20/order). "
           "Dividends ride on the price-adjustment factors only when "
           "corporate-action files are loaded; the buy&hold benchmark uses the "
           "same series, so the strategy-minus-benchmark spread stays fair.")


def regime_category_report(prices: dict, meta: dict, equity_curve: list):
    """Step 10: performance by market regime and by ETF category."""
    P.section("PERFORMANCE BY MARKET REGIME & ETF CATEGORY", "🌍")
    # --- regime: bull/bear from benchmark 200-day trend ---
    bench = None
    for cand in ("NIFTYBEES", "HDFCSENSEX", "MONIFTY500"):
        if cand in prices and len(prices[cand]) > 250:
            bench = cand
            break
    if bench is None and prices:
        bench = max(prices, key=lambda s: len(prices[s]))
    if bench is None or not equity_curve:
        P.info("Not enough data for regime breakdown.")
        return
    bdates = [d for (d, _a, _c, _t) in prices[bench]]
    badj = [a for (_d, a, _c, _t) in prices[bench]]
    bpos = {}
    for i, d in enumerate(bdates):
        bpos[d] = i

    def regime_at(date):
        i = bpos.get(date)
        if i is None or i < 200:
            return None
        seg = badj[i - 200:i + 1]
        return "bull" if seg[-1] >= sum(seg) / len(seg) else "bear"

    # true month-end-to-month-end returns
    me_eq = {}     # month -> equity at its last session
    me_date = {}   # month -> date of its last session
    for d, e in equity_curve:
        mk = month_key(parse_date(d))
        me_eq[mk] = e
        me_date[mk] = d
    months = sorted(me_eq)
    monthly = []
    for i in range(1, len(months)):
        m0, m1 = months[i - 1], months[i]
        if me_eq[m0] > 0:
            monthly.append((m1, me_eq[m0], me_eq[m1] / me_eq[m0] - 1.0,
                            regime_at(me_date[m0])))
    buckets = {"bull": [], "bear": [], "unknown": []}
    for _mk, _eq, ret, reg in monthly:
        buckets[reg or "unknown"].append(ret)
    rows = []
    for reg in ("bull", "bear", "unknown"):
        rs = buckets[reg]
        if not rs:
            continue
        avg_m = mean(rs)
        rows.append([reg.upper(), str(len(rs)), fmt_pct(avg_m, 2),
                     fmt_pct((1 + avg_m) ** 12 - 1), fmt_pct(min(rs), 1)])
    P.sub("Portfolio monthly returns split by regime (benchmark 200-day trend)")
    P.table(["Regime", "Months", "Avg month", "Roughly / year", "Worst month"], rows,
            aligns=["<", ">", ">", ">", ">"])

    # --- category buy-and-hold monthly returns (equal-weight baskets) ---
    by_cat = {}
    for sym, series in prices.items():
        cat = meta.get(sym, {}).get("category", "OTHER")
        by_cat.setdefault(cat, []).append(sym)
    cat_rows = []
    for cat, syms in sorted(by_cat.items()):
        rets = []
        for sym in syms[:25]:
            s = prices[sym]
            m_end = {}
            for (d, a, _c, _t) in s:
                m_end[month_key(parse_date(d))] = (d, a)
            vals = sorted(m_end.items())
            for i in range(1, len(vals)):
                a0 = vals[i - 1][1][1]
                a1 = vals[i][1][1]
                if a0 and a1 and a0 > 0:
                    rets.append(a1 / a0 - 1.0)
        if rets:
            avg_m = mean(rets)
            cat_rows.append([cat, str(len(syms)), fmt_pct(avg_m, 2),
                             fmt_pct((1 + avg_m) ** 12 - 1)])
    P.sub("Equal-weight category baskets (buy & hold, monthly)")
    P.table(["Category", "ETFs", "Avg month", "Roughly / year"], cat_rows,
            aligns=["<", ">", ">", ">"])
    P.note("Category rows are passive baskets (context for the strategy), "
           "not TSMOM portfolio returns.")


def cash_return_series(prices: dict, cash_rate: float, use_sleeve=None):
    """
    Per-day cash return (audit 2.6).  In LIQUID_ETF mode the cash sleeve earns
    the REAL liquid-ETF's daily returns (own series, own costs and slab tax in
    the ledger); if its history is missing we fall back to the flat T-bill
    proxy WITH A WARNING — a constant 5.3% over 10 years is implausible and
    must never be silently pretended to be market data.
    Returns a callable fn(date) -> daily simple return.
    """
    if use_sleeve is None:
        use_sleeve = (CASH_SLEEVE_MODE == "LIQUID_ETF")
    flat = (1.0 + cash_rate) ** (1.0 / 252.0) - 1.0
    if use_sleeve:
        rows = sorted(((d, a) for (d, a, _c, _t) in prices.get(CASH_SLEEVE_SYMBOL, [])))
        if rows:
            rets = {}
            for i in range(1, len(rows)):
                if rows[i - 1][1]:
                    rets[rows[i][0]] = rows[i][1] / rows[i - 1][1] - 1.0

            def fn(d):
                return rets.get(d, flat)
            fn.sleeve = True
            return fn
        P.warn(f"{CASH_SLEEVE_SYMBOL} history not found — cash sleeve falls back "
               f"to the flat {fmt_pct(cash_rate, 2)} T-bill proxy (labelled).")

    def fn_flat(d):
        return flat
    fn_flat.sleeve = False
    return fn_flat


def benchmark_report(prices: dict, meta: dict, equity_curve: list,
                     strategy_name: str, cash_rate: float, avg_invested: float = 0.6):
    """Audit sec 2.4: buy-and-hold + risk-matched benchmark — without these you
    cannot tell whether the strategy adds anything over sitting in an index."""
    bench = None
    for cand in ("NIFTYBEES", "HDFCSENSEX", "MONIFTY500", "TOP100CASE"):
        if cand in prices and len(prices[cand]) > 250:
            bench = cand
            break
    if bench is None and prices:
        bench = max(prices, key=lambda s: len(prices[s]))
    if bench is None or not equity_curve:
        return
    P.section("BENCHMARKS — does the strategy add anything?", "\U0001F4CA")
    # benchmark adjusted-close values keyed by date, kept sorted for fast lookup
    bseries = sorted(((d, a) for (d, a, _c, _t) in prices[bench]))
    bdates = [d for (d, _a) in bseries]
    bvals = [a for (_d, a) in bseries]
    dates = [d for (d, _e) in equity_curve]
    eqs = [e for (_d, e) in equity_curve]
    start_eq = eqs[0]
    b_prev = None
    bh_eq = start_eq
    rm_eq = start_eq
    bh_curve = []
    rm_curve = []
    avg_invested_frac = clamp(avg_invested, 0.0, 1.0)
    cash_fn_b = cash_return_series(prices, cash_rate)   # same sleeve as strategy
    for d in dates:
        # last benchmark value at or before d (dates are ascending)
        lo, hi, ans = 0, len(bdates) - 1, None
        while lo <= hi:
            mid = (lo + hi) // 2
            if bdates[mid] <= d:
                ans = bvals[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        b = ans
        if b is None:
            bh_curve.append(bh_eq)
            rm_curve.append(rm_eq)
            continue
        if b_prev is not None and b_prev > 0:
            r = b / b_prev - 1.0
            bh_eq *= (1.0 + r)
            rm_eq *= (1.0 + avg_invested_frac * r +
                      (1.0 - avg_invested_frac) * cash_fn_b(d))
        b_prev = b
        bh_curve.append(bh_eq)
        rm_curve.append(rm_eq)

    def stats_of(curve):
        years = max((parse_date(dates[-1]) - parse_date(dates[0])).days / 365.25, 1e-9)
        cagr = (curve[-1] / start_eq) ** (1.0 / years) - 1.0
        peak, mdd = curve[0], 0.0
        for e in curve:
            peak = max(peak, e)
            mdd = max(mdd, 1.0 - e / peak)
        return cagr, mdd, curve[-1]

    # audit 2.9: benchmark rows are NET of statutory entry costs and final
    # LTCG (buy & hold realises nothing until the end, so LTCG applies once).
    # The drag lands ON the curves so CAGR/DD stay consistent with the final.
    entry_ch = calc_charges("BUY", start_eq)["total"]

    def net_ltcg(final_eq):
        gain = final_eq - start_eq
        if gain <= 0:
            return final_eq
        tax = max(0.0, gain - LTCG_EXEMPTION) * LTCG_RATE * (1.0 + CESS_RATE)
        return final_eq - tax

    if bh_curve:
        bh_curve[-1] = net_ltcg(bh_curve[-1] - entry_ch)
    if rm_curve:
        rm_curve[-1] = net_ltcg(rm_curve[-1] - entry_ch * avg_invested_frac)
    bh_cagr, bh_dd, bh_fin = stats_of(bh_curve)
    rm_cagr, rm_dd, rm_fin = stats_of(rm_curve)
    st_cagr, st_dd, st_fin = stats_of(eqs)
    rows = [
        [f"{strategy_name} (this strategy)", fmt_pct(st_cagr), fmt_pct(-st_dd), fmt_cr(st_fin)],
        [f"Buy & hold {bench} (net of cost + LTCG)", fmt_pct(bh_cagr), fmt_pct(-bh_dd), fmt_cr(bh_fin)],
        [f"Risk-matched {bench} ({fmt_pct(avg_invested_frac,0)} invested + cash, net)",
         fmt_pct(rm_cagr), fmt_pct(-rm_dd), fmt_cr(rm_fin)],
    ]
    P.table(["", "CAGR", "Max DD", "Final"], rows, aligns=["<", ">", ">", ">"])
    P.info(f"Strategy minus buy & hold: {fmt_pct(st_cagr - bh_cagr)} per year | "
           f"minus risk-matched: {fmt_pct(st_cagr - rm_cagr)} per year")
    P.note("Much of a low-vol TSMOM return can come from CASH CARRY (idle cash at "
           f"{fmt_pct(cash_rate,2)}). Compare against the risk-matched row above.")


def walk_forward(prices: dict, meta: dict, quick: bool = False):
    """
    Walk-forward validation (audit 2.8, cold-start fix).  External audit F9.4
    (2026-10-27): params are re-SELECTED per window from the 45-combo grid, but
    the tuned CONFIG knobs (vol target, corr cap, rotation, N) are fixed at
    their in-sample winners — the OOS windows are true holdout for the signal
    grid only, not for the config.
      1. per window, choose parameters on the TRAIN prefix only (selection
         runs — quick grid, cold start OK because they only pick parameters);
      2. run ONE continuous full-history backtest whose parameters switch at
         the window boundaries (a param schedule);
      3. report OOS performance as SEGMENTS OF THAT SINGLE CURVE — no fresh
         cold start per window, so nothing is lost at the seams.
    """
    all_dates = sorted({d for s in prices.values() for (d, _a, _c, _t) in s})
    me = month_end_indices(all_dates)
    me_dates = [all_dates[i] for i in me]
    train_m = 36
    test_m = 12
    if len(me_dates) < train_m + test_m + 2:
        P.warn("Not enough months for walk-forward (need 48+). Skipped.")
        return None
    P.section("WALK-FORWARD VALIDATION (36m train -> 12m test, rolled)", "\U0001F504")
    combos = ([(lb, en, sk) for lb in GRID_LOOKBACKS for en in GRID_ENTRIES for sk in GRID_SKIPS]
              if not quick else [(LOOKBACK, ENTRY_THRESHOLD, SKIP)])
    windows = []
    cursor = train_m
    while cursor + test_m - 1 < len(me_dates):
        train_end = me_dates[cursor - 1]
        test_start = me_dates[cursor]
        test_end = me_dates[cursor + test_m - 1]
        best = None
        for lb, en, sk in combos:
            bt = Backtester(prices, meta, {"lookback": lb, "skip": sk,
                                           "entry": en, "exit": EXIT_THRESHOLD},
                            cost_case="base", cash_rate=CASH_RATE_PRETAX,
                            strategy=TRADE_STRATEGY)
            r = bt.run(start_date=me_dates[0], end_date=train_end)
            if r.get("error"):
                continue
            score = r["sharpe"]
            if best is None or score > best[0]:
                best = (score, lb, en, sk)
        if best is None:
            break
        windows.append({"train_end": train_end, "test_start": test_start,
                        "test_end": test_end, "pick": best[1:]})
        cursor += test_m
    if not windows:
        P.warn("Walk-forward selection produced no windows.")
        return None

    # ---- ONE continuous run with a parameter schedule (audit 2.8) ----
    schedule = []
    for w in windows:
        lb, en, sk = w["pick"]
        schedule.append((w["test_start"],
                         {"lookback": lb, "skip": sk,
                          "entry": en, "exit": EXIT_THRESHOLD}))
    bt = Backtester(prices, meta, {"lookback": LOOKBACK, "skip": SKIP,
                                   "entry": ENTRY_THRESHOLD, "exit": EXIT_THRESHOLD,
                                   "param_schedule": schedule},
                    cost_case="base", cash_rate=CASH_RATE_PRETAX,
                    strategy=TRADE_STRATEGY)
    r = bt.run(start_date=me_dates[0], end_date=me_dates[-1])
    if r.get("error"):
        P.warn(f"walk-forward continuous run failed: {r['error']}")
        return None
    curve = r.get("equity_curve", [])

    def eq_at(d):
        best_e, best_d = None, None
        for dd, e in curve:
            if dd <= d and (best_d is None or dd >= best_d):
                best_d, best_e = dd, e
        return best_e

    def seg_stats(d1, d2):
        e1, e2 = eq_at(d1), eq_at(d2)
        if not e1 or not e2 or e1 <= 0:
            return None, None
        yrs = max((parse_date(d2) - parse_date(d1)).days / 365.25, 1e-9)
        return (e2 / e1) ** (1.0 / yrs) - 1.0, e2 / e1 - 1.0

    rows = []
    oos_growth = 1.0
    oos_years = 0.0
    for w in windows:
        lb, en, sk = w["pick"]
        cagr, tot = seg_stats(w["test_start"], w["test_end"])
        if cagr is None:
            continue
        rows.append([w["test_start"], w["test_end"], f"{lb}/{sk}", fmt_pct(en, 0),
                     fmt_pct(cagr), fmt_pct(tot)])
        oos_growth *= (1.0 + tot)
        oos_years += (parse_date(w["test_end"]) - parse_date(w["test_start"])).days / 365.25
    P.table(["Test from", "Test to", "Chosen lb/skip", "Entry",
             "CAGR (aft tax)", "Total ret"], rows,
            aligns=["<", "<", "<", ">", ">", ">"])
    if oos_years > 0 and rows:
        wf_cagr = oos_growth ** (1.0 / oos_years) - 1.0
        P.ok(f"Stitched walk-forward CAGR over {len(rows)} OOS windows "
             f"({oos_years:.1f} contiguous years of ONE continuous run): "
             f"{fmt_pct(wf_cagr)}")
        P.note("Each window's parameters were chosen strictly before its test "
               "period, and all windows share one equity curve — no cold-start "
               "months are lost between windows (audit 2.8).")
    return rows


def bootstrap_ci(equity_curve: list, draws: int = 1000, seed: int = 11):
    """Audit sec 2.5: bootstrap the monthly return series -> CAGR confidence band."""
    if not equity_curve or len(equity_curve) < 60:
        return None
    me_eq = {}
    for d, e in equity_curve:
        me_eq[month_key(parse_date(d))] = e
    months = sorted(me_eq)
    rets = [me_eq[months[i]] / me_eq[months[i - 1]] - 1.0
            for i in range(1, len(months)) if me_eq[months[i - 1]] > 0]
    if len(rets) < 12:
        return None
    rng = random.Random(seed)
    years = len(rets) / 12.0
    # audit 2.9: BLOCK bootstrap — 6-month blocks preserve serial dependence
    # (momentum returns are autocorrelated; iid resampling is far too narrow)
    block = 6
    cagrs = []
    for _ in range(draws):
        g = 1.0
        got = 0
        while got < len(rets):
            start = rng.randrange(max(1, len(rets) - block + 1))
            for j in range(block):
                if got >= len(rets):
                    break
                g *= (1.0 + rets[min(start + j, len(rets) - 1)])
                got += 1
        cagrs.append(g ** (1.0 / max(years, 1e-9)) - 1.0)
    cagrs.sort()
    lo = cagrs[int(0.05 * draws)]
    med = cagrs[int(0.50 * draws)]
    hi = cagrs[int(0.95 * draws)]
    P.section("BOOTSTRAP — 90% confidence band on CAGR (monthly resampling)", "\U0001F3B2")
    P.table(["5th pct (bad luck)", "Median", "95th pct (good luck)"],
            [[fmt_pct(lo), fmt_pct(med), fmt_pct(hi)]], aligns=[">", ">", ">"])
    P.note(f"Based on {len(rets)} monthly returns, {draws} block resamples "
           f"({block}-month blocks). Serial dependence is partly preserved; "
           "treat as a sanity band, "
           "not a promise.")
    return lo, med, hi


def period_report(prices: dict, equity_curve: list):
    """Audit sec 2.5: results by period + how many ETFs were actually eligible."""
    if not equity_curve:
        return
    P.section("RETURNS BY FINANCIAL YEAR + UNIVERSE DEPTH", "\U0001F4C5")
    me_eq = {}
    for d, e in equity_curve:
        me_eq[month_key(parse_date(d))] = e
    months = sorted(me_eq)
    # eligible count per FY (symbols with >= lookback bars by that date)
    rows = []
    fy_rets = {}
    for i in range(1, len(months)):
        m0, m1 = months[i - 1], months[i]
        r = me_eq[m1] / me_eq[m0] - 1.0 if me_eq[m0] > 0 else 0.0
        d1 = parse_date(m1 + "-28")
        fy = financial_year(d1)
        fy_rets.setdefault(fy, []).append(r)
    for fy in sorted(fy_rets):
        rs = fy_rets[fy]
        g = 1.0
        for x in rs:
            g *= (1.0 + x)
        rows.append([fy, str(len(rs)), fmt_pct(g - 1.0), fmt_pct(mean(rs), 2)])
    P.table(["Financial year", "Months", "Return", "Avg month"], rows,
            aligns=["<", ">", ">", ">"])
    # universe depth at start/middle/end
    all_dates = sorted({d for s in prices.values() for (d, _a, _c, _t) in s})
    for probe in (all_dates[5], all_dates[len(all_dates) // 2], all_dates[-1]):
        n = sum(1 for s, ser in prices.items()
                if len([1 for (d, _a, _c, _t) in ser if d <= probe]) >= MIN_HISTORY_BARS)
        P.info(f"ETFs with 252+ sessions of history by {probe}: {n}")
    P.note("Early years carry a thinner universe (many ETFs listed recently) — "
           "compare periods with that in mind.")


def run_full_backtest(db: Db, quick: bool = False):
    """Step 10: in-sample/OOS split, parameter grid, cost & cash scenarios."""
    prices, meta = load_prices_from_db(db)
    if not prices:
        P.err("No price data in the database yet. Run: python etf-momentum-screener.py update")
        return None
    all_dates = sorted({d for s in prices.values() for (d, _a, _c, _t) in s})
    split = all_dates[int(len(all_dates) * 0.8)]
    P.section("BACKTEST ENGINE — 80/20 IN-SAMPLE / OUT-OF-SAMPLE SPLIT", "\U0001F5C3")
    P.info(f"Data span: {all_dates[0]} -> {all_dates[-1]}  ({len(all_dates):,} sessions)")
    P.info(f"Parameter selection uses ONLY data before {split}.")
    P.note("The last 20% stays untouched while choosing parameters.")

    grid_lbs = GRID_LOOKBACKS if not quick else [LOOKBACK]
    grid_ents = GRID_ENTRIES if not quick else [ENTRY_THRESHOLD]
    grid_skips = GRID_SKIPS if not quick else [SKIP]
    rows = []
    best = None
    for lb in grid_lbs:
        for sk in grid_skips:
            for ent in grid_ents:
                bt = Backtester(prices, meta, {"lookback": lb, "skip": sk,
                                               "entry": ent, "exit": EXIT_THRESHOLD},
                                cost_case="base", cash_rate=CASH_RATE_PRETAX,
                                strategy=TRADE_STRATEGY)
                res = bt.run(end_date=split)
                if res.get("error"):
                    continue
                rows.append([str(lb), str(sk), fmt_pct(ent, 0),
                             fmt_pct(res["cagr_after_cost"]),
                             f"{res['sharpe']:.2f}", fmt_pct(-res["max_drawdown"]),
                             fmt_cr(res["final_equity"])])
                score = res["sharpe"]
                if best is None or score > best[0]:
                    best = (score, lb, sk, ent, res)
    P.sub("Parameter grid — IN-SAMPLE only (do not peek at OOS!) "
          "(skip is a tested dimension now)")
    P.table(["Lookback", "Skip", "Entry", "CAGR (after cost)", "Sharpe", "Max DD", "Final"],
            rows, aligns=[">", ">", ">", ">", ">", ">", ">"])
    if best is None:
        P.err("Grid produced no results — need more history.")
        return None
    _, best_lb, best_sk, best_ent, _ = best
    P.ok(f"Best in-sample setting: lookback={best_lb}, skip={best_sk}, "
         f"entry={fmt_pct(best_ent,0)} (highest Sharpe). Now LOCKED for out-of-sample.")
    P.note("Per the rulebook the baseline (252 / +2%) is also reported; "
           "thresholds are not changed based on out-of-sample results.")

    scenarios = []
    for cost_case in ("base", "adverse", "severe"):
        for cash_name, cash_rate in CASH_CASES.items():
            bt = Backtester(prices, meta, {"lookback": best_lb, "skip": best_sk,
                                           "entry": best_ent, "exit": EXIT_THRESHOLD},
                            cost_case=cost_case, cash_rate=cash_rate,
                            strategy=TRADE_STRATEGY, cash_sleeve=False)
            res_full = bt.run()
            bt2 = Backtester(prices, meta, {"lookback": best_lb, "skip": best_sk,
                                            "entry": best_ent, "exit": EXIT_THRESHOLD},
                             cost_case=cost_case, cash_rate=cash_rate,
                             strategy=TRADE_STRATEGY, cash_sleeve=False)
            res_oos = bt2.run(start_date=split)
            scenarios.append((cost_case, cash_name, res_full, res_oos))
    P.section("SCENARIO MATRIX — chosen params, full sample vs out-of-sample", "\U0001F3AF")
    # audit v14 §4: this matrix runs cash_sleeve=False while the headline run
    # uses the configured sleeve — say so, or the numbers look contradictory.
    P.note("This matrix runs WITHOUT the overnight cash sleeve "
           "(idle cash earns the flat daily rate), while the headline run above "
           f"uses the configured sleeve ({CASH_SLEEVE_MODE}).  A cell can "
           "therefore differ from the headline for the same params.  Fixed "
           "sweep set; re-running the headline family's sleeve variant is a "
           "separate experiment, not this table.")
    for cost_case, cash_name, rf, ro in scenarios:
        out_rows.append([
            cost_case, cash_name,
            fmt_pct(rf["cagr_after_cost_tax"]) if not rf.get("error") else "n/a",
            fmt_pct(ro["cagr_after_cost_tax"]) if not ro.get("error") else "n/a",
            f"{rf['sharpe']:.2f}" if not rf.get("error") else "n/a",
            fmt_pct(-rf["max_drawdown"]) if not rf.get("error") else "n/a",
        ])
    P.table(["Costs", "Cash rate", "CAGR full", "CAGR OOS", "Sharpe full", "Max DD full"],
            out_rows, aligns=["<", "<", ">", ">", ">", ">"])

    # the three strategy versions compared on the same base scenario
    P.section(f"STRATEGY COMPARISON (base costs, {'liquid-sleeve' if CASH_SLEEVE_MODE == 'LIQUID_ETF' else fmt_pct(CASH_RATE_PRETAX, 2) + ' cash'}) — what drives returns?", "\U0001F501")
    cmp_rows = []
    results_abc = {}
    for strat in ("A", "B", "C"):
        bt = Backtester(prices, meta, BASELINE_PARAMS, cost_case="base",
                        cash_rate=CASH_RATE_PRETAX, strategy=strat)
        r = bt.run()
        results_abc[strat] = r
        if not r.get("error"):
            cmp_rows.append([
                {"A": "A — Pure TSMOM", "B": "B — TSMOM + Ranking", "C": "C — Ranking only"}[strat],
                fmt_pct(r["cagr_after_cost_tax"]), f"{r['sharpe']:.2f}",
                fmt_pct(-r["max_drawdown"]), fmt_pct(r["avg_invested"]), fmt_cr(r["final_equity"]),
            ])
    # audit sec 2.1: exit overlay test (faster exits on 200-day average break)
    bt_ov = Backtester(prices, meta, dict(BASELINE_PARAMS, exit_overlay="200dma"),
                       cost_case="base", cash_rate=CASH_RATE_PRETAX, strategy=TRADE_STRATEGY)
    r_ov = bt_ov.run()
    if not r_ov.get("error"):
        cmp_rows.append([
            f"{TRADE_STRATEGY} + 200-day exit overlay",
            fmt_pct(r_ov["cagr_after_cost_tax"]), f"{r_ov['sharpe']:.2f}",
            fmt_pct(-r_ov["max_drawdown"]), fmt_pct(r_ov["avg_invested"]),
            fmt_cr(r_ov["final_equity"]),
        ])
    P.table(["Version", "CAGR", "Sharpe", "Max DD", "Avg Invested", "Final Equity"],
            cmp_rows, aligns=["<", ">", ">", ">", ">", ">"])
    P.note("The exit overlay row answers the audit's question: does a faster "
           "exit (price below 200-day average) beat the 21-session skip alone?")
    a, b, c = results_abc.get("A"), results_abc.get("B"), results_abc.get("C")
    if a and c and not a.get("error") and not c.get("error"):
        P.sub("Interpretation (rulebook rule)")
        if c["cagr_after_cost_tax"] > a["cagr_after_cost_tax"]:
            P.info("C > A  ->  the cross-sectional RANKING is generating the returns, "
                   "not the time-series TSMOM filter.")
        else:
            P.info("A > C  ->  the TSMOM trend FILTER is doing the work; "
                   "ranking is a drag.")
    P.note("Survivorship-bias caveat: this universe is today's listed ETF set. "
           "Delisted/merged funds are missing, so real point-in-time results would be "
           "LOWER. Bias direction: CAGR here is an OVER-estimate. Same honesty as the "
           "scanner's discovery-journal note — the gap is UNMEASURED on this data; "
           "treat the headline CAGR as up to ~3-5% optimistic until the journal has "
           "aged about a year.")
    P.note("Dividend treatment: raw Upstox prices carry no distributions, so nothing is "
           "compounded (conservative). Load distributions into corporate_actions with "
           "note='DIV...' and the reinvestment is haircut by the slab rate "
           f"({fmt_pct(SLAB_RATE,0)}) per the rulebook.")
    # save run
    label = f"grid-best lb={best_lb} entry={best_ent}"
    db.execute("INSERT INTO backtest_runs(ts,label,params,results,notes) VALUES(?,?,?,?,?)",
               (now_iso(), label,
                json.dumps({"lookback": best_lb, "entry": best_ent}),
                json.dumps({f"{sc}/{cn}": {"full_cagr": rf.get("cagr_after_cost_tax"),
                                           "oos_cagr": ro.get("cagr_after_cost_tax")}
                            for sc, cn, rf, ro in scenarios if not rf.get("error")}),
                "auto-run"))
    # detailed print of the headline run — headline cash assumption is the
    # 5.30% POST-TAX rate (audit #7: pre-tax cash with no interest tax is the
    # optimistic case and must not be the headline)
    headline_cash = CASH_CASES["5.30% post-tax"]
    bt = Backtester(prices, meta, {"lookback": best_lb, "skip": best_sk,
                                   "entry": best_ent, "exit": EXIT_THRESHOLD},
                    cost_case="base", cash_rate=headline_cash, strategy=TRADE_STRATEGY)
    full = bt.run()
    print_backtest_result(full, "HEADLINE BACKTEST — full sample (best params, base costs, cash sleeve per config)")
    bt2 = Backtester(prices, meta, {"lookback": best_lb, "skip": best_sk,
                                    "entry": best_ent, "exit": EXIT_THRESHOLD},
                     cost_case="base", cash_rate=headline_cash, strategy=TRADE_STRATEGY)
    oos = bt2.run(start_date=split)
    print_backtest_result(oos, "OUT-OF-SAMPLE — final 20% (never seen during tuning)")
    regime_category_report(prices, meta, full.get("equity_curve", []))
    benchmark_report(prices, meta, full.get("equity_curve", []),
                     "TSMOM-B (best params)", headline_cash,
                     avg_invested=full.get("avg_invested", 0.6))
    period_report(prices, full.get("equity_curve", []))
    bootstrap_ci(full.get("equity_curve", []))
    walk_forward(prices, meta, quick=quick)
    txt = []
    txt.append(f"TSMOM backtest report {now_iso()}\nparams={best_lb}/{best_ent}\n")
    for name, r in (("full", full), ("oos", oos)):
        if not r.get("error"):
            txt.append(f"{name}: cagr={r['cagr_after_cost_tax']:.4f} sharpe={r['sharpe']:.2f} "
                       f"maxdd={r['max_drawdown']:.4f}\n")
    save_report(f"backtest_{TODAY.strftime('%Y%m%d')}.txt", "".join(txt))
    return full


# ==============================================================================
# SECTION 22 — STATUS / REPORT COMMANDS
# ==============================================================================

def cmd_status(db: Db):
    P.section("ACCOUNT STATUS", "\U0001F4B0")
    try:
        env = load_env()
        client = UpstoxClient(env)
    except Exception:
        client = None
    prices = {}
    for r in db.query("SELECT p.symbol AS symbol, p.close AS close FROM price_daily p WHERE p.date=(SELECT MAX(date) FROM price_daily WHERE symbol=p.symbol)"):
        prices[r["symbol"]] = r["close"]
    prices, marked_today = _fill_mark_overlay(db, prices)
    broker = Broker(db, TaxEngine(db), client, paper=PAPER_TRADE)
    risk = RiskEngine(db)
    cash = broker.cash()
    hv = broker.holdings_value(prices)
    eq = cash + hv
    P.kv("Trading mode", "PAPER (simulated)" if PAPER_TRADE else "LIVE")
    P.kv("Cash on hand", fmt_cr(cash))
    P.kv("ETFs you hold (worth now)", fmt_cr(hv))
    P.kv("Your total (cash + ETFs)", fmt_cr(eq), val_color=_C.GREEN)
    # audit v14 §1/§3 (2026-10-30): "Invested +2.80%" mixed the cash sleeve
    # with market exposure and the "+" read like profit.  Split it: strategy
    # ETFs (the engine's real exposure) vs the overnight cash sleeve.
    _sleeve_v = 0.0
    for _s in CASH_PROXY_SYMBOLS:
        _h = broker.holdings().get(_s)
        if _h:
            _sleeve_v += _h["qty"] * prices.get(_s, _h["avg_cost"])
    _strat_v = max(0.0, hv - _sleeve_v)
    P.kv("Invested in strategy ETFs", fmt_pct(safe_div(_strat_v, eq, 0.0), signed=False))
    if _sleeve_v > 0:
        P.kv("Parked in overnight sleeve", fmt_pct(safe_div(_sleeve_v, eq, 0.0), signed=False)
             + f"  ({CASH_SLEEVE_SYMBOL} — counts as parked cash, not market exposure)")
    # audit v13 #5: the very first seed equals STARTING_CAPITAL until the
    # first confirmed run realises the peak — label that case so it doesn't
    # read as a phantom all-time-high next to the real total.
    _peak_seed = (not db.meta_get("risk_last_eval"))
    # audit v15 P1: catch up with a peak that was rebased on a paper->live
    # flip TODAY (v12 P0-1) — otherwise drawdown 0.00% next to a just-seeded
    # peak looks unexplained.
    _rb = db.meta_get("risk_peak_rebased_date", "")
    _peak_note = ("  (startup seed — updates from your real book "
                  "on the first confirmed run)" if _peak_seed
                  else "  (peak rebased on mode flip today)" if _rb == today_str()
                  else "")
    P.kv("Peak equity", fmt_cr(risk.peak) + _peak_note)
    dd = max(1 - eq / risk.peak, 0.0) if risk.peak else 0.0
    P.kv("Current drawdown", fmt_pct(dd, signed=False),
         val_color=_C.RED if dd > 0.05 else _C.WHITE)
    P.kv("Risk state", risk.state)
    # audit v12: a state printed without an evaluation date can hide a stale
    # engine (the 2026-10-29 console showed dd 69.96% beside NORMAL because
    # update() never ran on declined run).  Show when it last saw equity.
    _rle = db.meta_get("risk_last_eval", "")
    P.kv("Risk engine last evaluated", _rle or "not yet")
    if not _rle or _rle != today_str():
        P.note("Risk triggers are armed only on a run where you confirm "
               "orders (or in paper) — a plain status read does not move the "
               "engine's peak or locks.")
    rows = []
    for sym, h in sorted(broker.holdings().items()):
        px = prices.get(sym, h["avg_cost"])
        pnl = (px - h["avg_cost"]) * h["qty"]
        rows.append([sym, universe_map().get(sym, {}).get("name", sym)[:26],
                     fmt_num(h["qty"], 0), f"Rs {h['avg_cost']:.2f}", f"Rs {px:.2f}",
                     fmt_cr(h["qty"] * px), fmt_cr(pnl)])
    if rows:
        P.sub("Open positions")
        P.table(["Symbol", "Name", "Units", "Avg Cost", "LTP", "Value", "Unrealised P&L"],
                rows, aligns=["<", "<", ">", ">", ">", ">", ">"])
        if marked_today:
            P.note(f"Marked at today's fill price (VWAP): {', '.join(marked_today)} — "
                   f"last-close marks return after tonight's close download.")
    else:
        P.info("No open positions — the account is fully in cash.")
    _mode_now = "PAPER" if PAPER_TRADE else "LIVE"
    P.sub(f"Recent orders — {_mode_now} mode (order book)")
    orows = []
    _shown = 0
    for r in db.query("SELECT * FROM orders ORDER BY id DESC LIMIT 200"):
        # audit v12 (2026-10-29): paper and live rows share one DB — mixing
        # them made a PAPER buy sit next to the broker's live position like
        # one book.  Show only the current mode; history of the other mode
        # stays stored, just hidden with a count.
        if r["mode"] != _mode_now:
            continue
        if _shown >= 15:
            continue
        _shown += 1
        # audit v10 (external): SKIPPED/REJECTED rows were undiagnosable from
        # the console — the reason IS stored, now show it
        _rsn = (r["reason"] or "")[:42] if r["status"] not in ("FILLED",) else ""
        orows.append([r["exec_date"], r["symbol"], r["side"], fmt_num(r["qty"], 0),
                      f"Rs {(r['price'] or 0):.2f}", fmt_cr(r["value"] or 0),
                      r["status"], r["mode"], _rsn])
    if orows:
        P.table(["Date", "Symbol", "Side", "Qty", "Price", "Value", "Status", "Mode", "Why"],
                orows, aligns=["<", "<", "<", ">", ">", ">", "<", "<", "<"])
    else:
        P.info(f"Order book is empty in {_mode_now} mode — no trades have "
               f"been made yet.")
    _other = db.one("SELECT COUNT(*) AS c FROM orders WHERE mode!=?",
                    (_mode_now,))
    if _other and _other["c"]:
        P.note(f"({_other['c']} order row(s) from the OTHER mode "
               f"{'PAPER' if _mode_now=='LIVE' else 'LIVE'} are kept in the "
               f"database but hidden here — runs in one mode never act on the "
               f"other mode's book.)")
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
        _fy_now = financial_year(dt.date.today())
        if dt.date.today().month == 3 and _fy_now in tax:
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


def _fill_mark_overlay(db: Db, prices: dict):
    """Display-only: mark symbols traded TODAY at today's VWAP fill price
    (external audit F3b, 2026-10-27).  Without this, a name bought at a live
    quote shows an instant artificial day-1 P&L against last night's close
    (paste: PHARMABEES −2.2% on entry day).  Journal/risk math keeps the close
    basis — this only touches what the status screens PRINT.
    Returns (overlaid_prices, symbols_marked)."""
    try:
        ov = {}
        for r in db.query("SELECT symbol, SUM(value) AS v, SUM(qty) AS q "
                          "FROM orders WHERE exec_date=? AND status='FILLED' "
                          "GROUP BY symbol", (today_str(),)):
            if r["q"]:
                ov[r["symbol"]] = float(r["v"]) / float(r["q"])
        if ov:
            prices = {**prices, **ov}
        return prices, sorted(ov)
    except Exception:
        return prices, []


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


# Plausibility ceilings for a 12-1 momentum print, by category (display-only;
# the binding integrity gate stays the hard >300% block).  A fund of this
# category beyond the ceiling almost certainly has an unadjusted corporate
# action, a spliced series, or a premium/currency artifact — verify the data
# before trusting the signal.  Seen live 2026-10-27: MONQ50 printed +101%.
MOM_PLAUSIBILITY = {"DEBT": 0.25, "Hybrid": 0.60, "GLOBAL INDICES": 0.75,
                    "COMMODITY": 1.00, "EQUITY": 1.50}


def momentum_plausibility(symbol: str, mom) -> str:
    """'' when the momentum print is plausible; a short flag otherwise."""
    if mom is None:
        return ""
    cat = universe_map().get(symbol, {}).get("category", "")
    cap = MOM_PLAUSIBILITY.get(cat)
    if cap is not None and abs(mom) > cap:
        # audit v8: on the SEBI-frozen global ETFs a hot print is usually the
        # REAL premium expanding (MONQ50 +109% on 28-Oct-2026 = premium 18%->86%
        # on top of a Nasdaq rally), not broken data — say so plainly
        if cat == "GLOBAL INDICES":
            return (f"very hot for its category (>{fmt_pct(cap, 0)}) — mostly the "
                    f"real premium expanding (SEBI freeze), not bad data")
        return f"IMPLAUSIBLE for its category (>{fmt_pct(cap, 0)}) — verify adjusted data"
    return ""


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
    """
    End-of-run one-glance summary.  Everything below is DERIVED FROM THE
    JOURNAL (orders, meta marks, price bars), not from what happened to print
    earlier, so it stays truthful even when sections above were skipped.

      WHAT WAS DONE TODAY   — orders / rebalance / data refresh facts
      HOLDINGS TREND HEALTH — indicative 12-1 momentum + 200-DMA per holding
      FRONTRUNNERS          — strongest uptrending ETFs NOT in the SIP basket
      WHAT TO DO NEXT       — concrete actions and standing caveats
    """
    try:
        P.section("TODAY AT A GLANCE — WHAT WAS DONE & WHAT TO DO NEXT", "\U0001F9ED")
        today = today_str()

        # ---- 1. what was performed today ----------------------------------
        P.sub("What was performed today")
        # audit v4 §3.1: a status-blind SUM(value) let SKIPPED/UNCONFIRMED rows
        # report as "traded" (showed Rs 9,487 when ~Rs 2,423 actually settled)
        orders = db.query("SELECT side, status, COUNT(*) AS n, SUM(value) AS v FROM orders "
                          "WHERE exec_date=? GROUP BY side, status", (today,))
        if orders:
            by = {}
            for o in orders:
                by.setdefault(o["side"], {"FILLED": (0, 0.0), "OPEN": (0, 0.0),
                                          "NOT": (0, 0.0)})
                if o["status"] == "FILLED":
                    bucket = "FILLED"
                elif o["status"] in ("PENDING", "UNCONFIRMED"):
                    bucket = "OPEN"
                else:
                    bucket = "NOT"
                n_, v_ = by[o["side"]][bucket]
                by[o["side"]][bucket] = (n_ + o["n"], v_ + (o["v"] or 0.0))
            for side, b in by.items():
                fn, fv = b["FILLED"]
                on_, ov = b["OPEN"]
                nn, nv = b["NOT"]
                # audit v8 (2026-10-28): saying "real money" in PAPER mode
                # misleads — name the true thing in each mode
                _mw = "paper money (simulated)" if PAPER_TRADE else "real money"
                P.ok(f"{side}: {fn} order(s) SETTLED ({fmt_cr(fv)} {_mw})")
                if on_:
                    P.warn(f"{side}: {on_} order(s) open at the broker "
                           f"({fmt_cr(ov)} may still settle — counted at end of "
                           f"run / tomorrow)")
                if nn:
                    P.info(f"{side}: {nn} order(s) not done ({fmt_cr(nv)} "
                           f"skipped or rejected)")
        else:
            P.info("No orders executed today (rebalance not due / buying paused / "
                   "already done in an earlier run today).")
        last_sig = db.meta_get("last_trade_signal", "") or "(never)"
        all_dates = [r["date"] for r in db.query(
            "SELECT DISTINCT date FROM price_daily ORDER BY date")]
        due_as_of = last_month_end_date(all_dates) if all_dates else None
        if due_as_of:
            if last_sig == due_as_of:
                # audit v8 (2026-10-28): after a stale-signal re-pick, the
                # month-end key is satisfied by design (L4) while the basket
                # carries TODAY's date — show the basket date, else this line
                # contradicts "List chosen on" above it
                _bd = db.meta_get(SIP_BASKET_DATE_KEY, "") or ""
                if _bd and _bd != due_as_of:
                    P.ok(f"Monthly rebalance is up to date — the stale "
                         f"{due_as_of} signal was discarded; basket freshly "
                         f"re-picked on {_bd}.")
                else:
                    P.ok(f"Monthly rebalance is up to date (signal {due_as_of}).")
            else:
                P.warn(f"Monthly rebalance DUE: signal {due_as_of} has not been "
                       f"executed (last executed: {last_sig}).")
        latest_bar = all_dates[-1] if all_dates else "n/a"
        P.kv("Price data current through", latest_bar)

        # ---- 2. holdings trend health (indicative) ------------------------
        holdings = db.query("SELECT symbol, qty, avg_cost FROM positions WHERE qty > 0")
        basket = [s for s in (db.meta_get(SIP_BASKET_KEY, "") or "").split(",") if s]
        health_rows = []
        risky_names = []
        sleeve_rows = []
        for h in sorted(holdings, key=lambda r: r["symbol"]):
            # audit v14 §3: the overnight cash sleeve is exempt from momentum
            # exits — listing LIQUIDCASE here (possibly "AT RISK") is a false
            # alarm.  It gets its own one-liner instead.
            if h["symbol"] in CASH_PROXY_SYMBOLS:
                sleeve_rows.append(h)
                continue
            mom, above, vol = _quick_trend(db, h["symbol"])
            if mom is None:
                flag = paint("? no signal yet (short history)", _C.GREY)
                mom_s = "n/a"
            elif mom <= EXIT_THRESHOLD or above is False:
                flag = paint("AT RISK — would EXIT on current rules", _C.RED)
                mom_s = fmt_pct(mom)
                risky_names.append(h["symbol"])
            elif mom <= ENTRY_THRESHOLD + 0.03:
                flag = paint("weakening — watch near the exit line", _C.YELLOW)
                mom_s = fmt_pct(mom)
            else:
                flag = paint("healthy", _C.GREEN)
                mom_s = fmt_pct(mom)
            plaus = momentum_plausibility(h["symbol"], mom)
            if plaus:
                flag += "  |  " + paint("\u2691 " + plaus, _C.RED)
            vpl = vol_plausibility(h["symbol"], vol)
            if vpl:
                flag += "  |  " + paint("\u2691 " + vpl, _C.RED)
            dma = ("above" if above else ("below" if above is False else "n/a"))
            health_rows.append([h["symbol"],
                                universe_map().get(h["symbol"], {}).get("name", h["symbol"])[:28],
                                fmt_num(h["qty"], 0), mom_s, dma, flag])
        if health_rows:
            P.sub("Holdings — trend right now (today's close; the binding decision is the month-end one)")
            P.table(["Symbol", "Name", "Units", "12-1 momentum", "200-DMA", "Verdict"],
                    health_rows, aligns=["<", "<", ">", ">", "<", "<"])
        if sleeve_rows:
            P.sub("Cash sleeve (parked money — momentum rules do not apply)")
            _sv_rows = []
            for h in sleeve_rows:
                _lp = db.one("SELECT close FROM price_daily WHERE symbol=? "
                             "ORDER BY date DESC LIMIT 1", (h["symbol"],))
                _px = (_lp["close"] if _lp and _lp["close"] else h["avg_cost"])
                _sv_rows.append([h["symbol"],
                                 universe_map().get(h["symbol"], {}).get("name", h["symbol"])[:28],
                                 fmt_num(h["qty"], 0), fmt_cr(h["qty"] * _px),
                                 "overnight parking — swept back when buys need cash"])
            P.table(["Symbol", "Name", "Units", "Value", "Note"],
                    _sv_rows, aligns=["<", "<", ">", ">", "<"])

        # ---- 3. frontrunners not in the basket ------------------------------
        cands = []
        held = {h["symbol"] for h in holdings}
        for r in db.query("SELECT symbol FROM etf_universe WHERE status='ACTIVE'"):
            sym = r["symbol"]
            if sym in held or sym in basket or sym in CASH_PROXY_SYMBOLS:
                continue
            mom, above, vol = _quick_trend(db, sym)
            if mom is not None and mom > ENTRY_THRESHOLD and above is not False:
                cands.append((mom, sym, vol))
        cands.sort(reverse=True)
        if cands:
            # audit v9 (2026-10-28): mark premium-BLOCKED frontrunners — the
            # header's "joins the basket if top-5 at month-end" is a false
            # promise while the premium guard stands (MONQ50 +86% on 28-Oct)
            _fe = FilterEngine(db)
            P.sub("Frontrunners outside the basket (join the basket only if still "
                  f"top-{MAX_HOLDINGS} at the month-end signal AND clear of the "
                  f"premium guard)")
            for mom, sym, vol in cands[:3]:
                plaus = momentum_plausibility(sym, mom) or vol_plausibility(sym, vol)
                try:
                    _pb, _pdev, _plim = _fe.inav_premium_block(sym, today_str())
                except Exception:
                    _pb, _pdev, _plim = False, None, None
                if _pb:
                    plaus = ((plaus + "  |  ") if plaus else "") + (
                        f"BLOCKED by premium guard: {fmt_pct(_pdev)} over NAV "
                        f"(cap {fmt_pct(_plim)}) — the month-end signal cannot "
                        f"buy it until the premium cools")
                P.info(f"  {sym:<12} {fmt_pct(mom):>8}  "
                       f"{universe_map().get(sym, {}).get('name', sym)[:44]}"
                       + (f"   \u2691 {plaus}" if plaus else ""))

        # ---- 4. what to do next ---------------------------------------------
        P.sub("What to do next")
        if due_as_of and last_sig != due_as_of:
            P.warn(f"• Rebalance is DUE — it executes on the next trading session; "
                   f"keep the daily run enabled.")
        else:
            P.info(f"• Next rebalance signals at the {next_month_end_hint()} "
                   f"month-end — just keep running the script daily.")
        if risky_names:
            P.info(f"• {len(risky_names)} holding(s) flagged AT RISK "
                   f"({', '.join(risky_names)}) queue for exit at the "
                   f"{next_month_end_hint()} month-end rebalance. Mid-month "
                   f"guard-rail exits were backtested (round 4) and REDUCE returns "
                   f"on this universe — the monthly cadence is intentional.")
        n_nav = db.one("SELECT COUNT(*) AS c FROM nav_data")["c"]
        if not n_nav:
            P.warn("• iNAV premium/discount filter is OFF (no NAV data yet) — "
                   "global ETFs (MONQ50, MAFANG, MAHKTECH...) can trade above NAV. "
                   "The daily run auto-fetches stock-data/nav/*.csv — one bulk "
                   "Groww call (AMFI/mfapi fallbacks; success → done for the "
                   f"day; failure → retried after {NAV_FAIL_RETRY_HOURS}h); manual any time: "
                   "'python etf-momentum-screener.py load-data --fetch-nav'.")
        else:
            latest_nav = db.one("SELECT MAX(date) AS d FROM nav_data")["d"]
            try:
                _stal = (parse_date(today_str()) - parse_date(latest_nav)).days if latest_nav else 999
            except Exception:
                _stal = 999
            if _stal > NAV_MAX_STALE_DAYS:
                P.warn(f"• NAV data is STALE (latest {latest_nav}) — the iNAV filter "
                       f"trusts a NAV at most {NAV_MAX_STALE_DAYS} days old, so the "
                       f"filter may be inert. Refresh: 'python etf-momentum-screener.py "
                       f"load-data --fetch-nav'.")
        prices_now = {r["symbol"]: r["close"] for r in db.query(
            "SELECT p.symbol AS symbol, p.close AS close FROM price_daily p "
            "WHERE p.date=(SELECT MAX(date) FROM price_daily WHERE symbol=p.symbol)")}
        hv_close = sum(h["qty"] * prices_now.get(h["symbol"], h["avg_cost"]) for h in holdings)
        prices_disp, _marked_f = _fill_mark_overlay(db, prices_now)
        hv = sum(h["qty"] * prices_disp.get(h["symbol"], h["avg_cost"]) for h in holdings)
        cash_txt = db.meta_get("cash_balance", "") or db.meta_get("cash", "") or "0"
        # Trend-guard PARKED capital — recomputed live (not stored state) so the
        # panel stays truthful on days the SIP section prints nothing (weekend /
        # holiday / idempotent rerun).  User run 2026-10-27 #2: ~Rs 6.4L of the
        # plan quietly sat in cash behind three per-name WARN lines.
        parked_now, park_val = [], 0.0   # defaults so the invested-warning stays safe
        try:
            eq_park = float(cash_txt) + hv_close
            tgt = json.loads(db.meta_get(SIP_TARGETS_KEY, "") or "{}")
            held_val = {h["symbol"]: h["qty"] * prices_now.get(h["symbol"], h["avg_cost"])
                        for h in holdings}
            parked_now, park_val = [], 0.0
            for sym in basket:
                if not tgt.get(sym, 0.0) > 0:
                    continue
                mom2, above2, _v2 = _quick_trend(db, sym)
                if (mom2 is not None and mom2 <= EXIT_THRESHOLD) or above2 is False:
                    parked_now.append(sym)
                    park_val += max(0.0, tgt[sym] * eq_park - held_val.get(sym, 0.0))
            if parked_now and park_val >= MIN_TRADE_VALUE:
                P.warn(f"• ≈ {fmt_cr(park_val)} of the plan is PARKED by the "
                       f"trend guard ({', '.join(parked_now)} "
                       f"{'is' if len(parked_now) == 1 else 'are'} on exit rules). "
                       f"It deploys only if they recover; otherwise the "
                       f"{next_month_end_hint()} month-end signal re-plans the "
                       f"basket.")
        except Exception:
            pass
        try:
            eq = float(cash_txt) + hv
            # audit v14 §1 (2026-10-30): the under-deployment warning must read
            # STRATEGY exposure, not total-including-sleeve — otherwise one
            # accepted cash-sweep makes this warning vanish while the engine
            # is still 3% in the market.
            _sleeve_v = 0.0
            for _s in CASH_PROXY_SYMBOLS:
                _h = next((h for h in holdings if h["symbol"] == _s), None)
                if _h:
                    _sleeve_v += _h["qty"] * prices_now.get(_s, _h["avg_cost"])
            inv = safe_div(max(0.0, hv - _sleeve_v), eq, 0.0)
            # audit v15 P1 (2026-10-30 console): plan-vs-reality gap silently
            # persisted: book 2.8% invested against a 52.1% plan for days, with
            # no line naming it.  Count consecutive lagging days; after 3, warn.
            try:
                _tgt_raw = db.meta_get(SIP_TARGETS_KEY, "") or ""
                _tgt_inv = sum(float(v) for v in json.loads(_tgt_raw).values()) \
                    if _tgt_raw else 0.0
            except Exception:
                _tgt_inv = 0.0
            if _tgt_inv > 0.05 and inv < 0.25 * _tgt_inv:
                _lagd = db.meta_get("plan_lag_days", "0")
                _lagd = (int(_lagd) + 1) if db.meta_get("plan_lag_date") != today_str() \
                    else int(_lagd)
                db.meta_set("plan_lag_days", str(_lagd))
                db.meta_set("plan_lag_date", today_str())
                if _lagd >= 3:
                    P.warn(f"• Plan fill is {fmt_pct(inv)} invested against a "
                           f"{fmt_pct(_tgt_inv)} target plan — lagging for "
                           f"{_lagd} straight run-days.  Causes to check, in "
                           f"order: a declined live y/n, the budget floor "
                           f"below the minimum order size, a premium-guard "
                           f"block, or a month-end trickle day.")
            else:
                db.meta_set("plan_lag_days", "0")
            if eq > 0 and inv < 0.05 and SIP_MODE:
                if INVEST_PER_DAY <= 0:
                    P.warn(f"• Portfolio is only {fmt_pct(inv)} invested — "
                           f"INVEST_PER_DAY is 0, so nothing ever buys "
                           f"(analysis & exits mode). Set it in the README-config "
                           f"block at the top of the script to deploy.")
                elif parked_now and park_val >= MIN_TRADE_VALUE:
                    # external audit (run 2026-10-27): the generic "deployment
                    # should be engaging" line misfires when the REAL reason is
                    # the parked plan — say which it is
                    P.warn(f"• Portfolio is only {fmt_pct(inv)} invested — "
                           f"deployment IS running; the low figure is the trend "
                           f"guard parking {', '.join(parked_now)} "
                           f"(~{fmt_cr(park_val)} of the plan), not a stall.")
                else:
                    # audit v12: do not imply a malfunction — in LIVE mode a
                    # declined y/n is the most common cause of zero orders
                    P.warn(f"• Portfolio is only {fmt_pct(inv)} invested — the "
                           f"daily budget of {fmt_rs(INVEST_PER_DAY)} should be "
                           f"deploying now. If it is not: in LIVE mode a "
                           f"declined y/n confirmation means no orders today "
                           f"(by design, not a fault); otherwise check the "
                           f"risk state and the stored target plan.")
        except Exception:
            pass
        P.note("• Re-running the script today is safe: daily deployment is "
               "amount-journaled (tops up, never doubles) and the monthly "
               "rebalance tracks its signal date.")
    except Exception as e:
        P.note(f"(action panel unavailable: {e})")


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
    P.section("PERFORMANCE SINCE INCEPTION", "\U0001F4C8")
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
    note = ("Full rulebook reporting (10y backtest, OOS split, parameter grid, "
            "cost & cash scenarios) is produced by: python etf-momentum-screener.py backtest")
    P.note(note)
    save_report(f"report_{TODAY.strftime('%Y%m%d')}.txt",
                f"TSMOM report {now_iso()}\nSee console output / database {DB_PATH}\n")


# ==============================================================================
# SECTION 23 — SYNTHETIC DEMO DATA (so the whole engine can run end-to-end)
# ==============================================================================

def generate_synthetic_data(db: Db, years: int = 11, n_symbols: int = 28, seed: int = 7):
    """
    Creates realistic correlated ETF price history so every feature of the
    system (scanner, signals, backtest, paper trading) can be demonstrated
    even without live market access. Clearly tagged as SYNTHETIC in the DB.
    """
    rng = random.Random(seed)
    P.section("SYNTHETIC DATA GENERATOR (demo mode)", "\U0001F9EA")
    # audit 2026-10-19: NEVER write synthetic rows into a database that holds
    # real market data — the merge corrupts every series it touches
    n_real = db.one("SELECT COUNT(*) AS c FROM price_daily")["c"]
    if n_real and db.meta_get("data_origin") != "SYNTHETIC":
        P.err("REFUSING to generate synthetic prices: this database already "
              "holds REAL market data. Demo runs on its own database "
              "(stock-data/tsmom-demo.db).")
        return False
    P.warn("Generating SYNTHETIC prices — for demonstration only, clearly tagged in the DB.")
    universe = ETF_UNIVERSE[:n_symbols]
    n_days = int(years * 252)
    start = TODAY - dt.timedelta(days=int(years * 365.25))
    dates = []
    d = start
    while len(dates) < n_days:
        if d.weekday() < 5:
            dates.append(date_str(d))
        d += dt.timedelta(days=1)
    # market factor with regimes
    mkt = []
    regime = 0.00035
    for i in range(n_days):
        if i % 320 == 0:
            regime = rng.choice([0.00045, 0.0002, -0.00015, 0.00055])
        shock = rng.gauss(0, 0.009)
        mkt.append(regime + shock)
    for j, (sym, name, cat, fam) in enumerate(universe):
        beta = rng.uniform(0.5, 1.3) if cat == "EQUITY" else rng.uniform(0.05, 0.5)
        alpha = rng.gauss(0.00015, 0.00025)
        idio = 0.006 if cat == "EQUITY" else 0.003
        px = rng.uniform(20, 900)
        rows = []
        base_vol = rng.uniform(30_000, 2_000_000)
        for i in range(n_days):
            r = alpha + beta * mkt[i] + rng.gauss(0, idio)
            px = max(px * (1 + r), 2.0)
            vol_shares = max(100.0, base_vol * (1 + rng.gauss(0, 0.35)) / max(px, 1) * 10)
            hi = px * (1 + abs(rng.gauss(0, 0.004)))
            lo = px * (1 - abs(rng.gauss(0, 0.004)))
            op = px * (1 + rng.gauss(0, 0.002))
            rows.append({"symbol": sym, "date": dates[i], "open": op, "high": hi,
                         "low": lo, "close": px, "adj_close": px,
                         "volume": vol_shares, "traded_value": px * vol_shares})
        db.upsert_prices(sym, rows)
        # NAV series: close with small tracking noise + occasional dislocation so
        # the iNAV premium/discount filter is genuinely exercised (audit bug #6)
        nav_rows = []
        for i, r in enumerate(rows):
            noise = rng.gauss(0, 0.0015)
            if i % 157 == 0:
                noise += rng.choice([0.006, -0.006])   # dislocation > 0.40%/0.75%
            nav_rows.append((sym, r["date"], r["close"] * (1.0 + noise)))
        self_db = db
        self_db.conn.executemany(
            "INSERT INTO nav_data(symbol,date,nav) VALUES(?,?,?) "
            "ON CONFLICT(symbol,date) DO UPDATE SET nav=excluded.nav", nav_rows)
        self_db.conn.commit()
        # one synthetic 1:2 split on a few funds to exercise the adjustment
        # pipeline end-to-end (audit 2.2): the RAW prices really halve from the
        # ex-date and factor=2.0 restates them back up, so adj_close is smooth
        if j % 9 == 0 and len(rows) > 400:
            ex_i = 300
            ex = rows[ex_i]["date"]
            for k in range(ex_i, len(rows)):
                rows[k]["open"] *= 0.5
                rows[k]["high"] *= 0.5
                rows[k]["low"] *= 0.5
                rows[k]["close"] *= 0.5
                rows[k]["adj_close"] *= 0.5
            db.upsert_prices(sym, rows[ex_i:])
            self_db.execute(
                "INSERT OR REPLACE INTO corporate_actions(symbol,ex_date,factor,note) "
                "VALUES(?,?,?,?)",
                (sym, ex, 2.0, "SYNTHETIC split 1:2 (factor 2.0 per convention)"))
            dm = DataManager(db, None)
            dm.recompute_adjustments(sym)
        db.execute("UPDATE etf_universe SET first_seen=? WHERE symbol=? AND first_seen IS NULL",
                   (dates[0], sym))
        if j % 7 == 0:
            P.raw(f"  generated {sym:<12} {n_days} bars  (price now Rs {px:.2f})")
    db.meta_set("data_origin", "SYNTHETIC")
    P.ok(f"Synthetic history ready for {len(universe)} ETFs x {n_days} sessions.")


# ==============================================================================
# SECTION 24 — SELF TESTS (unit tests required by the rulebook)
# ==============================================================================

def cmd_selftest(db: Db):
    P.section("SELF TESTS / UNIT TESTS", "\u2705")
    # selftests always simulate — they must stay meaningful even when the
    # config runs paper by default (user direction 2026-10-28, second ask); the
    # harness pins paper ON for safety regardless of the config default
    _save_pt = globals()["PAPER_TRADE"]
    globals()["PAPER_TRADE"] = True
    passed = failed = 0
    def check(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            P.ok(f"{name} {paint(detail, _C.GREY)}")
        else:
            failed += 1
            P.err(f"{name} {detail}")

    # 1) momentum formula (rulebook unit test: must be non-zero when prices differ)
    adj = [1000.0] * 252 + [1150.0]
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

    # 7) tax lots (paper DB)
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
        # a) live order whose fill CANNOT be verified must stay UNCONFIRMED
        #    and must NOT move cash/positions
        class _DodgyClient:
            def place_order(self, sym, key, side, qty, price, order_type):
                return {"data": {"order_id": "V14A1"}}
            def order_details(self, oid):
                raise RuntimeError("verification endpoint down")
        _bf2 = Broker(_dbf, _taxf, client=_DodgyClient(), paper=False)
        _r_un = _bf2.execute("NIFTYBEES", "BUY", 10, 100.0, reason="v14 test")
        _pos_un = _dbf.one("SELECT qty FROM positions WHERE symbol='NIFTYBEES'")
        _cash_un = float(_dbf.meta_get("cash_balance", "0"))
        check("v14 P0: live order with unverifiable fill stays UNCONFIRMED and "
              "fabricates NOTHING (cash/book untouched)",
              _r_un["status"] == "UNCONFIRMED" and not _pos_un
              and abs(_cash_un - 1000000.0) < 1e-9,
              f"status={_r_un['status']} cash={_cash_un}")
        # b) live LIMIT sends the buffered price; the ledger price is the
        #    slippage-simulated one — no double slippage (v4 #9 + v14 P0)
        class _CrowClient:
            def place_order(self, sym, key, side, qty, price, order_type):
                self.sent = (price, order_type)
                return {"data": {"order_id": "V14B1"}}
            def order_details(self, oid):
                return {"data": {"average_price": "", "status": "open"}}
        _cw = _CrowClient()
        _bf3 = Broker(_dbf, _taxf, client=_cw, paper=False)
        _otv, _pxv = ("LIMIT", 100.20)
        globals()["PAPER_TRADE"] = False   # make live_buy_order_px honest
        try:
            _otv, _pxv = live_buy_order_px(100.0)
            _r_lim = _bf3.execute("BANKBEES", "BUY", 5, 100.0, reason="v14 limit",
                                  order_type=_otv, limit_px=_pxv)
        finally:
            globals()["PAPER_TRADE"] = True
        check("v14 P0: live LIMIT order sends ref*(1+buffer) exactly once (no "
              "slippage stacking on the limit price)",
              _otv == "LIMIT" and abs(_cw.sent[0] - 100.0 * (1 + LIVE_LIMIT_BUFFER)) < 0.005
              and _r_lim["status"] == "PENDING",
              f"sent={_cw.sent} status={_r_lim['status']}")
        # c) sync_live_book is fail-closed when the funds API dies
        class _NoFundsClient:
            def holdings(self): return []
            def positions(self): return []
            def orders_book(self): return []
            def funds_available(self):
                raise RuntimeError("funds API dead")
        _bf4 = Broker(_dbf, _taxf, client=_NoFundsClient(), paper=False)
        check("v14 P0: broker sync fails closed when cash is unreadable "
              "(never trades on a stale wallet)",
              _bf4.sync_live_book() is False, "")
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
    sel = pb3.select(pool, set(), "B", equity=1e8)
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
    sel2 = pb4.select(pool2, set(), "B", equity=1e8)
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
                   f"NIFTYBEES;999999\n")
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

    # N6 (external audit F9 / 3.9): live orders journaled PENDING/UNCONFIRMED
    #     are re-polled and settled — broker 'complete' applies the fill at the
    #     BROKER's average price; 'rejected' flips the ledger without touching cash
    dbnp = Db(DATA_DIR / "selftest_pend.db")
    taxp = TaxEngine(dbnp)
    dbnp.meta_set("cash_balance", "200000")
    dbnp.execute("INSERT INTO orders(ts,signal_as_of,exec_date,symbol,side,qty,price,"
                 "value,slippage,charges,status,mode,order_type,reason) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (now_iso(), today_str(), today_str(), "NIFTYBEES", "BUY", 10, 100.0,
                  1000.0, 0.0, 5.0, "PENDING", "LIVE", "MARKET",
                  "pend | live order id STUBOK1"))
    dbnp.execute("INSERT INTO orders(ts,signal_as_of,exec_date,symbol,side,qty,price,"
                 "value,slippage,charges,status,mode,order_type,reason) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (now_iso(), today_str(), today_str(), "GOLDBEES", "BUY", 5, 60.0,
                  300.0, 0.0, 2.0, "PENDING", "LIVE", "MARKET",
                  "pend | live order id STUBRJ1"))

    class _StubClient:
        def order_details(self, oid):
            if oid == "STUBOK1":
                return {"data": {"average_price": "101.50", "status": "complete",
                                 "filled_quantity": 10}}
            return {"data": {"status": "rejected"}}
    brkp = Broker(dbnp, taxp, _StubClient(), paper=False)
    n_done = resolve_pending_live_orders(dbnp, brkp)
    st_ok = dbnp.one("SELECT status, price FROM orders WHERE reason LIKE '%STUBOK1%'")
    st_rj = dbnp.one("SELECT status FROM orders WHERE reason LIKE '%STUBRJ1%'")
    pos = dbnp.one("SELECT qty, avg_cost FROM positions WHERE symbol='NIFTYBEES'")
    cash_after = float(dbnp.meta_get("cash_balance", "0"))
    check("N6: live PENDING orders re-polled — fill applied at broker avg, "
          "rejection marked, cash moved once",
          n_done == 2 and st_ok["status"] == "FILLED"
          and abs(st_ok["price"] - 101.5) < 1e-9
          and st_rj["status"] == "REJECTED"
          and pos is not None and pos["qty"] == 10
          and abs(pos["avg_cost"] - 101.5) < 1e-9
          and cash_after < 200000 - 1015.0 and cash_after > 200000 - 1100.0,
          f"done={n_done} ok={st_ok['status']} rj={st_rj['status']} "
          f"pos={None if pos is None else pos['qty']} cash={cash_after:.0f}")
    n_again = resolve_pending_live_orders(dbnp, brkp)
    check("N6: resolver is idempotent (no double-apply on a re-run)",
          n_again == 0 and abs(float(dbnp.meta_get("cash_balance", "0")) - cash_after) < 1e-6,
          f"again={n_again}")
    dbnp.close()
    try:
        os.remove(DATA_DIR / "selftest_pend.db")
    except Exception:
        pass

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
    re = RiskEngine(dbr, vol_target=0.08)   # rule semantics at the 8% rulebook target
    re.update(STARTING_CAPITAL * 0.75)          # -25% -> EXTREME lock
    check("audit A5: 25% drop locks to cash", re.all_cash is True)
    re.trigger_date = (dt.date.today() - dt.timedelta(days=RISK_RELEASE_DAYS + 1)).isoformat()
    re.update(STARTING_CAPITAL * 0.75)          # no price recovery, but time passed
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
    sized3 = pb.size(chosen3, 1_000_000.0, strategy="B")
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
    # under the measured rotation rule) — rotation-rule semantics are covered
    # by the R9G harness rejection instead of a synthetic re-enter test.
    global ENSEMBLE_LOOKBACKS, VOL_BLEND_252, LOT_SELECTION, EXECUTION_STYLE, TAX_DEFER_EXITS, EXIT_OVERLAY_200DMA

    # 3.3: tax-defer rule is explicit and testable
    save_td = TAX_DEFER_EXITS
    try:
        TAX_DEFER_EXITS = True
        far = tax_defer_blocks_exit(["2026-01-05"], "2026-06-30", -0.01)
        near = tax_defer_blocks_exit(["2025-07-20"], "2026-06-30", -0.01)
        deep = tax_defer_blocks_exit(["2025-07-20"], "2026-06-30", -0.05)
        check("audit 3.3: tax defer fires only near the LTCG anniversary and "
              "only for mild momentum", far == "" and near != "" and deep == "",
              f"far='{far[:20]}' near='{near[:20]}' deep='{deep[:20]}'")
    finally:
        TAX_DEFER_EXITS = save_td

    # 3.7: HIFO realises the highest-cost lot first (least gain)
    save_ls = LOT_SELECTION
    try:
        dbsub5 = Db(DATA_DIR / "selftest_hifo.db")
        dbsub5.execute("INSERT OR REPLACE INTO etf_universe(symbol,name,status,broad,category) "
                       "VALUES('X1','X','ACTIVE',1,'EQUITY')")
        te5 = TaxEngine(dbsub5)
        te5.record_buy("X1", "2024-04-01", 100.0, 10.0)
        te5.record_buy("X1", "2024-05-01", 200.0, 10.0)
        LOT_SELECTION = "HIFO"
        out_h = te5.record_sell("X1", "2024-08-01", 150.0, 10.0)
        LOT_SELECTION = "FIFO"
        dbsub5.execute("INSERT INTO tax_lots(symbol,buy_date,buy_price,qty) VALUES('X1','2024-04-01',100.0,10.0)")
        out_f = te5.record_sell("X1", "2024-08-01", 150.0, 10.0)
        check("audit 3.7: HIFO realises a loss where FIFO realises a gain",
              out_h["realized_pnl"] < 0 and out_f["realized_pnl"] > 0,
              f"hifo={out_h['realized_pnl']} fifo={out_f['realized_pnl']}")
        dbsub5.close()
        try:
            os.remove(DATA_DIR / "selftest_hifo.db")
        except Exception:
            pass
    finally:
        LOT_SELECTION = save_ls

    # 3.2: ensemble momentum = mean of components; vol blend sits between
    save_el, save_vb = ENSEMBLE_LOOKBACKS, VOL_BLEND_252
    try:
        series_e = [100.0 * (1.001 ** i) for i in range(300)]
        m_single = signal_momentum(series_e, 299, 252, 21)
        ENSEMBLE_LOOKBACKS = (126, 252)
        m_ens = signal_momentum(series_e, 299, 252, 21)
        m_126 = momentum_at(series_e, 299, 126, 21)
        check("audit 3.2: ensemble momentum averages its components",
              abs(m_ens - (m_126 + m_single) / 2.0) < 1e-9, f"{m_ens}")
        ENSEMBLE_LOOKBACKS = None
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
        ENSEMBLE_LOOKBACKS, VOL_BLEND_252 = save_el, save_vb

    # 3.5: LIMIT_MID does not cross the spread; scaled DD thresholds
    save_es = EXECUTION_STYLE
    try:
        px_m = slippage_price("BUY", 100.0, 20.0, order_value=1e5, liq_p25=1e7)
        EXECUTION_STYLE = "LIMIT_MID"
        px_l = slippage_price("BUY", 100.0, 20.0, order_value=1e5, liq_p25=1e7)
        check("audit 3.5: LIMIT_MID fills closer to mid than MARKET",
              px_l < px_m, f"market={px_m} limit={px_l}")
    finally:
        EXECUTION_STYLE = save_es
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
    global EXIT_OVERLAY_200DMA
    dbsub8 = Db(DATA_DIR / "selftest_integ.db")
    fe8 = FilterEngine(dbsub8)
    pb8 = PortfolioBuilder(dbsub8, fe8)
    mk8 = lambda sym, mom, vals: {"symbol": sym, "momentum": mom, "vol": 0.15,
                                  "liq_p25": 1e8, "adj": list(vals), "index_family": sym,
                                  "rows": [(f"2026-{i//28+1:02d}-{i%28+1:02d}", v, v, 1e7)
                                           for i, v in enumerate(vals)]}
    pool8 = [mk8("SPLICEDHELD", 0.50, spliced), mk8("SPLICEDFREE", 0.50, spliced),
             mk8("CLEANONE", 0.05, clean)]
    sel8 = pb8.select(pool8, {"SPLICEDHELD"}, "B", equity=1e8)
    dec8 = {st["symbol"]: dec for st, dec, _r in sel8["decisions"]}
    check("audit 2026-10-19: corrupt held name forced OUT, corrupt free name BLOCKED",
          dec8.get("SPLICEDHELD") == "EXIT" and dec8.get("SPLICEDFREE") == "BLOCKED"
          and dec8.get("CLEANONE") == "ELIGIBLE_BUY", f"{dec8}")
    dbsub8.close()
    try:
        os.remove(DATA_DIR / "selftest_integ.db")
    except Exception:
        pass
    # demo guard: generator refuses to write into a real database
    dbsub9 = Db(DATA_DIR / "selftest_demoguard.db")
    dbsub9.execute(
        "INSERT INTO price_daily(symbol,date,open,high,low,close,adj_close,volume,traded_value) "
        "VALUES('REALX','2026-01-01',1,1,1,1,1,1,1)")
    dbsub9.meta_set("data_origin", "REAL")
    ok_gen = generate_synthetic_data(dbsub9, years=1, n_symbols=2, seed=1)
    n_after = dbsub9.one("SELECT COUNT(*) AS c FROM price_daily")["c"]
    check("audit 2026-10-19: demo generator refuses to pollute a REAL database",
          ok_gen is False and n_after == 1, f"ret={ok_gen} rows={n_after}")
    dbsub9.close()
    try:
        os.remove(DATA_DIR / "selftest_demoguard.db")
    except Exception:
        pass
    # reset-paper restores a clean paper slate
    dbsub10 = Db(DATA_DIR / "selftest_reset.db")
    for t in ("positions", "orders"):
        try:
            dbsub10.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    dbsub10.execute(
        "INSERT INTO orders(ts,signal_as_of,exec_date,symbol,side,qty,price,value,slippage,"
        "charges,status,mode,order_type,reason) VALUES('t','t','t','X','BUY',1,1,1,0,0,'FILLED','PAPER','MARKET','x')")
    dbsub10.meta_set("risk_all_cash", "1")
    save_pt = globals()["PAPER_TRADE"]
    globals()["PAPER_TRADE"] = True
    try:
        cmd_reset_paper(dbsub10)
    finally:
        globals()["PAPER_TRADE"] = save_pt
    n_ord = dbsub10.one("SELECT COUNT(*) AS c FROM orders")["c"]
    cash_ok = (abs(float(dbsub10.meta_get("cash_balance")) - STARTING_CAPITAL) < 1e-6
               and abs(float(dbsub10.meta_get("cash")) - STARTING_CAPITAL) < 1e-6)
    check("audit 2026-10-19: reset-paper clears the journal and restores capital",
          n_ord == 0 and cash_ok, f"orders={n_ord} cash={dbsub10.meta_get('cash')}")
    dbsub10.close()
    try:
        os.remove(DATA_DIR / "selftest_reset.db")
    except Exception:
        pass

    # --- cash sleeve (audit 2.6): real sleeve returns with labelled fallback ---
    global CASH_SLEEVE_MODE
    save_csm = CASH_SLEEVE_MODE
    try:
        sleeve_rows = [(f"2026-01-{d+1:02d}", 100.0 * (1.01 ** d), 100.0 * (1.01 ** d), 1e7)
                       for d in range(10)]
        prices_sl = {"LIQUIDCASE": sleeve_rows}
        CASH_SLEEVE_MODE = "LIQUID_ETF"
        fn_s = cash_return_series(prices_sl, 0.053, True)
        check("audit 2.6: sleeve earns the liquid ETF's real returns",
              fn_s.sleeve and abs(fn_s("2026-01-05") - 0.01) < 1e-9,
              f"r={fn_s('2026-01-05'):.4f}")
        fn_f = cash_return_series(prices_sl, 0.053, False)
        flat_exp = (1.053) ** (1.0 / 252.0) - 1.0
        check("audit 2.6: flat mode uses the labelled T-bill proxy",
              (not fn_f.sleeve) and abs(fn_f("2026-01-05") - flat_exp) < 1e-12)
        fn_fb = cash_return_series({}, 0.053, True)   # history missing -> fallback
        check("audit 2.6: missing sleeve history falls back to flat (warned)",
              (not fn_fb.sleeve) and abs(fn_fb("2026-01-05") - flat_exp) < 1e-12)
        # live-path helpers exist and use the sleeve (regression: self.data crash)
        dbsub7 = Db(DATA_DIR / "selftest_sleeve.db")
        for r0 in sleeve_rows:
            dbsub7.execute(
                "INSERT INTO price_daily(symbol,date,open,high,low,close,adj_close,volume,traded_value) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("LIQUIDCASE", r0[0], r0[1], r0[1], r0[1], r0[1], r0[1], 1, 1e7))
        cyc7 = TradingCycle(dbsub7)
        sd = cyc7.sleeve_daily_return("2026-01-10")
        check("hotfix: TradingCycle.data exists; sleeve_daily_return reads history",
              hasattr(cyc7, "data") and abs(sd - 0.01) < 1e-9, f"r={sd:.4f}")
        dbsub7.close()
        try:
            os.remove(DATA_DIR / "selftest_sleeve.db")
        except Exception:
            pass
    finally:
        CASH_SLEEVE_MODE = save_csm

    # --- hotfix regression: live-path 200dma overlay on 4-column SQLite rows ---
    # (crashed with 'too many values to unpack' once EXIT_OVERLAY_200DMA was
    #  switched on; also guards the overlay-vs-liquidity gate ordering)
    save_ov = EXIT_OVERLAY_200DMA
    try:
        EXIT_OVERLAY_200DMA = True
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
        sel_ov = pb6.select(pool_ov, {"HELDLOW"}, "B", equity=1e8)
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
    finally:
        EXIT_OVERLAY_200DMA = save_ov

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
    # exit_rules_fire parity with the backtester kernel (R12A rule)
    _flat = [100.0] * 300                      # flat: mom=0 -> fires (<=0)
    _rise = [100.0 + i * 0.1 for i in range(300)]   # uptrend, above 200-DMA
    _dipp = [100.0 + i * 0.1 for i in range(250)] + list(
        124.9 - i * 0.5 for i in range(60))    # sharp dip below 200-DMA,
                                               # long-run momentum still >0
    _ef = (exit_rules_fire(_flat), exit_rules_fire(_rise), exit_rules_fire(_dipp),
           exit_rules_fire([1.0] * 50))
    check("exit_rules_fire: flat fires, uptrend holds, DMA dive fires, short N/A",
          _ef == (True, False, True, False), f"got {_ef}")
    # autofill budget: floor respected, large pool lifts pace, degenerate inputs
    _ab = (autofill_budget(5000.0, 90000.0, 20),      # 4500 under floor -> 5000
           autofill_budget(5000.0, 921000.0, 20),     # 46050 lifts
           autofill_budget(5000.0, 0.0, 20),          # empty pool -> floor
           autofill_budget(5000.0, 200000.0, 0),      # 0 days left := 1
           autofill_budget(0.0, 100000.0, 10))        # zero floor -> 10000
    check("autofill_budget: floor, lift, empty-pool, zero-days, zero-floor",
          _ab == (5000.0, 46050.0, 5000.0, 200000.0, 10000.0), f"got {_ab}")
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
        cyc11 = TradingCycle(dbsub11)
        dbsub11.meta_set("last_trade_signal", cyc11.last_month_end())
        dbsub11.execute(
            "INSERT INTO pending_trades(signal_as_of,exec_after,symbol,side,"
            "target_qty,reason,status,created) VALUES(?,?,?,?,?,?,?,?)",
            ("2026-01-01", "2026-01-02", "NIFTYBEES", "SELL", 40,
             "test remainder", "PENDING", now_iso()))
        n_sip = cyc11.consume_pending_sells({"NIFTYBEES": 101.0})
        left = dbsub11.one("SELECT qty FROM positions WHERE symbol='NIFTYBEES'")["qty"]
        check("sip: queued SELL remainder consumed on the next session",
              n_sip == 1 and left == 60, f"consumed={n_sip} left={left}")
        dbsub11.meta_set(SIP_BASKET_KEY, "AAA,BBB,CCC")
        check("sip: basket meta roundtrip",
              dbsub11.meta_get(SIP_BASKET_KEY, "") == "AAA,BBB,CCC")
        # regression: interest accrual calls sleeve_daily_return — it used to
        # live only on TradingCycle, so day-2 of any schedule crashed with
        # AttributeError ('Broker' object has no attribute 'sleeve_daily_return')
        dbsub11.meta_set("last_interest_date", "2026-01-01")
        before_cash = float(dbsub11.meta_get("cash_balance"))
        earned = cyc11.broker.accrue_cash_interest()
        check("hotfix: cash-interest accrual works on day 2 (Broker owns sleeve)",
              earned > 0 and float(dbsub11.meta_get("cash_balance")) > before_cash,
              f"earned={earned:.4f}")
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

    # L6: the autofill budget never blasts the whole plan in fewer than 3
    # market days (whole-plan one-day lump = concentrated entry timing)
    _l6 = (deploy_sessions_left(1), deploy_sessions_left(0),
           deploy_sessions_left(None), deploy_sessions_left("oops"),
           deploy_sessions_left(14))
    check("L6: deploy pace floored at >=3 market days, garbage-safe",
          _l6 == (3, 3, 3, 3, 14), f"got {_l6}")

    # #9: live buys are band-limited LIMIT orders; paper stays modelled MARKET
    _save_pt9 = globals()["PAPER_TRADE"]
    try:
        globals()["PAPER_TRADE"] = False
        _ot9, _px9 = live_buy_order_px(100.0)
    finally:
        globals()["PAPER_TRADE"] = _save_pt9
    _ot9p, _px9p = live_buy_order_px(100.0)   # selftest forces PAPER_TRADE=True
    check("#9: live buy = LIMIT at px+0.2% buffer; paper stays MARKET",
          _ot9 == "LIMIT" and abs(_px9 - 100.20) < 1e-9 and _ot9p == "MARKET"
          and _px9p == 100.0 and LIVE_ORDER_TYPE == "LIMIT"
          and 0 < LIVE_LIMIT_BUFFER <= 0.01,
          f"live={_ot9}@{_px9} paper={_ot9p}@{_px9p}")

    # P2: SIP sizing scale = drawdown scale x bear-regime halving — the SIP
    # sizing call used to hard-code risk_scale=1.0, ignoring the bear halving
    # the backtest applies (live/backtest parity break)
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
        _save_rs = globals()["REGIME_SCALING"]
        try:
            globals()["REGIME_SCALING"] = True
            _s_bear = cyc12._current_risk_scale("2026-10-27")
            globals()["REGIME_SCALING"] = False
            _s_flat = cyc12._current_risk_scale("2026-10-27")
        finally:
            globals()["REGIME_SCALING"] = _save_rs
        check("P2: sizing risk scale combines drawdown AND bear-regime halving",
              _s_flat == 1.0 and abs(_s_bear - 0.5) < 1e-9,
              f"bear={_s_bear} regime-off={_s_flat}")
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

    # L2: live cash comes from the broker's funds API; paper mode NEVER calls
    # it (regression: a bare live run sized every order off the hardcoded
    # paper ledger and sent 3x oversize orders)
    dbsub14 = Db(DATA_DIR / "selftest_sync.db")
    try:
        dbsub14.meta_set("cash_balance", "1000000")
        class _BoomClient:
            def funds_available(self):
                raise RuntimeError("paper mode must never call the funds API")
        class _FakeClient:
            def funds_available(self):
                return 500000.0
        _tax14 = TaxEngine(dbsub14)
        _bp = Broker(dbsub14, _tax14, client=_BoomClient(), paper=True)
        _r_paper = _bp.sync_live_cash()
        _bl = Broker(dbsub14, _tax14, client=_FakeClient(), paper=False)
        _r_live = _bl.sync_live_cash()
        _cash14 = _bl.cash()
        _bf = Broker(dbsub14, _tax14, client=_BoomClient(), paper=False)
        _r_fail = _bf.sync_live_cash()
        _cash14b = _bf.cash()
        check("L2: live ledger syncs from broker funds; paper never calls the API; "
              "API failure keeps the ledger",
              _r_paper is True and _r_live is True and _cash14 == 500000.0
              and _r_fail is False and _cash14b == 500000.0,
              f"paper={_r_paper} live={_r_live}->{_cash14} fail={_r_fail}->{_cash14b}")
    finally:
        dbsub14.close()
        try:
            os.remove(DATA_DIR / "selftest_sync.db")
        except Exception:
            pass

    # L1 -> 2026-10-29: every real-order path now confirms via the runtime
    # y/n in live_order_ok() (user direction; the --confirm flag is gone).
    # This test runs with PAPER_TRADE forced True, so only structural
    # properties of the order path are pinned here.
    check("L1: live order paths are guard-railed (LIMIT buys; live sleeve on "
          "since audit v5#4 — paper still untouched)",
          LIVE_ORDER_TYPE == "LIMIT" and LIVE_CASH_SLEEVE is True
          and 0 < LIVE_LIMIT_BUFFER <= 0.01,
          f"ORDER={LIVE_ORDER_TYPE} SLEEVE={LIVE_CASH_SLEEVE} "
          f"BUFFER={LIVE_LIMIT_BUFFER}")
    # audit v8 (2026-10-28, user direction): PAPER by default — a bare run must
    # simulate; going live needs the config flip AND --confirm (both guards
    # checked above stay conditional on PAPER_TRADE, so they arm correctly if
    # the user flips the config).  The harness itself forces PAPER_TRADE True,
    # so the only honest pin is the source line of the config assignment.
    _src8 = Path(__file__).read_text(encoding="utf-8", errors="ignore")
    # 2026-10-29: the USER deliberately flipped the value to live on their own
    # box (their call — task-#13 locked only that *I* never ship live-by
    # default).  Same day the --confirm CLI flag was replaced by the runtime
    # y/n (user direction).  What stays pinned is the SAFETY SHAPE: the paper/
    # live switch exists AND every live order path asks live_order_ok().
    check("v8/v12: paper/live config switch exists AND real orders are gated "
          "by the runtime y/n (live_order_ok) — the two-switch safety shape, "
          "not the value (user may run live on their own box)",
          "\nPAPER_TRADE = " in _src8
          and "def live_order_ok" in _src8
          and ("add_" + 'argument("' + "--conf" + 'irm"') not in _src8,
          "config switch, y/n gate, or --confirm removal regressed")

    # --- User direction 2026-10-28: in LIVE mode the BROKER's holdings +
    #     order book are the source of truth; in PAPER mode the DB is --------
    class _TruthClient:
        def funds_available(self): return 250000.0
        def holdings(self):
            return [{"trading_symbol": "ETFA", "quantity": 10, "t1_quantity": 2,
                     "average_price": 100.0},
                    {"trading_symbol": "ETFD", "quantity": 9, "t1_quantity": 0,
                     "average_price": 50.0}]   # unknown to the bot -> untouched
        def positions(self):
            return [{"trading_symbol": "ETFA", "buy_quantity": 1,
                     "sell_quantity": 0, "buy_price": 101.0},
                    {"trading_symbol": "ETFC", "buy_quantity": 3,
                     "sell_quantity": 0, "buy_price": 55.0}]
        def orders_book(self):
            return [{"trading_symbol": "ETFA", "transaction_type": "BUY",
                     "status": "open", "quantity": 5, "filled_quantity": 0},
                    {"trading_symbol": "ETFA", "transaction_type": "SELL",
                     "status": "trigger pending", "quantity": 6, "filled_quantity": 2},
                    {"trading_symbol": "ETFC", "transaction_type": "BUY",
                     "status": "complete", "quantity": 3, "filled_quantity": 3}]

    class _BoomBook:
        def funds_available(self): raise RuntimeError("paper must not call broker")
        def holdings(self): raise RuntimeError("paper must not call broker")
        def positions(self): raise RuntimeError("paper must not call broker")
        def orders_book(self): raise RuntimeError("paper must not call broker")

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
        _bl15 = Broker(dbsub15, _tr15, client=_TruthClient(), paper=False)

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
              "(open buy 5->2, book 13->16)",
              _ob == 5.0 and _sellable == 13.0 - 4.0 and _qa2 == 16.0
              and _sa2 == max(0.0, 16.0 - 4.0) and _mir2 == 16.0
              and _bl15.live_open_qty("ETFA", "BUY") == 2.0,
              f"ob={_ob} sellable={_sellable}->{_sa2} qty={_qa2}")

        # 3) paper mode: the DB is the truth, the broker client is NEVER called
        _bp15 = Broker(dbsub15, _tr15, client=_BoomBook(), paper=True)
        _okp = _bp15.sync_live_book()
        _hp = _bp15.holdings()
        check("live truth: paper mode ignores the broker and answers from the DB",
              _okp is True and set(_hp) >= {"ETFA"} and _bp15.live_book_ok()
              and _bp15.live_open_qty("ETFA", "BUY") == 0.0,
              f"paper holdings={sorted(_hp)}")

        # 4) unreadable broker => fail CLOSED: sync False, live_book None,
        #    positions mirror left exactly as it was
        _bf15 = Broker(dbsub15, _tr15, client=_BoomBook(), paper=False)
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

    # #4: live idle-cash sleeve — weekly sweep parks surplus; unsweep frees
    #     cash only to fund real buys; monthly cadence respected
    class _SleeveClient:
        def __init__(self, cash=250000.0, hold=None):
            self._cash, self._hold = cash, (hold or [])
            self.placed = []
        def funds_available(self): return self._cash
        def holdings(self): return list(self._hold)
        def positions(self): return []
        def orders_book(self): return []
        def place_order(self, *a, **k):
            self.placed.append((a, k)); return {"data": {"order_id": "T-1"}}
        def order_details(self, oid):
            return {"data": {"average_price": None, "status": "complete"}}

    dbsub17 = Db(DATA_DIR / "selftest_sleeve.db")
    try:
        dbsub17.execute(
            "INSERT INTO etf_universe(symbol,name,category,index_family,status) "
            "VALUES(?,?,?,?,?)", (CASH_SLEEVE_SYMBOL, "sleeve", "DEBT",
                                  "liquid", "ACTIVE"))
        dbsub17.execute(
            "INSERT INTO price_daily(symbol,date,open,high,low,close,adj_close,"
            "volume,traded_value) VALUES(?,?,?,?,?,?,?,?,?)",
            (CASH_SLEEVE_SYMBOL, today_str(), 100, 100, 100, 100.0, 100.0, 1e7, 1e8))
        dbsub17.meta_set("cash_balance", "900000")
        _tax17 = TaxEngine(dbsub17)
        _bk17 = Broker(dbsub17, _tax17, client=_SleeveClient(900000.0), paper=False)
        _bk17.sync_live_book()
        _cyc17 = TradingCycle.__new__(TradingCycle)
        _cyc17.db, _cyc17.broker, _cyc17.data = dbsub17, _bk17, DataManager(dbsub17, None)
        # the sleeve engine guards on the GLOBAL live/paper switch, which the
        # harness forces to paper — flip it for the live-behaviour test
        _save_pt17a = globals()["PAPER_TRADE"]
        globals()["PAPER_TRADE"] = False
        try:
            _s1 = _cyc17.sleeve_sweep()
            _cash_after = _bk17.cash()
            _hqty = _bk17.holdings().get(CASH_SLEEVE_SYMBOL, {}).get("qty", 0)
            _s2 = _cyc17.sleeve_sweep()      # same-day/weekly cadence blocks
            _pc_after_sweeps = len(_bk17.client.placed)
            # unsweep frees cash ONLY to fund buys: need 500k with cash low
            _freq = 500000.0
            _cash_before_u = _bk17.cash()
            _freed = _cyc17.sleeve_unsweep(_freq)
            _hqty2 = _bk17.holdings().get(CASH_SLEEVE_SYMBOL, {}).get("qty", 0)
        finally:
            globals()["PAPER_TRADE"] = _save_pt17a
        check("v5#4: weekly sweep parks idle cash in the liquid sleeve, once max",
              _s1 == 1 and _hqty > 0 and _cash_after < 900000
              and _s2 == 0 and _pc_after_sweeps == 1,
              f"sweep={_s1} sleeve_qty={_hqty} cash_left={_cash_after:.0f} "
              f"second={_s2} orders_then={_pc_after_sweeps}")
        check("v5#4: unsweep sells just enough sleeve to fund today's buys",
              _freed > 0 and _hqty2 < _hqty
              and _bk17.cash() > _cash_before_u
              and _hqty2 >= _hqty - (_freq / 100.0) - 2,
              f"freed={_freed:.0f} sleeve {_hqty}->{_hqty2}")
        # paper mode: sleeve engine fully inert
        _bp17 = Broker(dbsub17, _tax17, client=_SleeveClient(1.0), paper=True)
        _cyc17.broker = _bp17
        _save_pt17 = globals()["PAPER_TRADE"]
        globals()["PAPER_TRADE"] = True
        try:
            _pi = _cyc17.sleeve_sweep(), _cyc17.sleeve_unsweep(1e6)
        finally:
            globals()["PAPER_TRADE"] = _save_pt17
        check("v5#4: sleeve engine never fires in paper mode (DB is truth)",
              _pi == (0, 0.0) and len(_bp17.client.placed) == 0, f"paper={_pi}")
    finally:
        dbsub17.close()
        try:
            os.remove(DATA_DIR / "selftest_sleeve.db")
        except Exception:
            pass

    # #6: momentum-gated entry overlay — a strong trend's dip below the
    #     200-DMA is buyable when the floor is set; weak trends still skipped
    _save_mf = globals()["ENTRY_OVERLAY_MOM_FLOOR"]
    try:
        globals()["ENTRY_OVERLAY_MOM_FLOOR"] = 0.05
        dbsub18 = Db(DATA_DIR / "selftest_momfloor.db")
        try:
            pb18 = PortfolioBuilder(dbsub18, FilterEngine(dbsub18))
            _down = [100.0 - 0.25 * i for i in range(210)]      # below 200dma
            pool18 = [
                {"symbol": "STRONGDIP", "name": "x", "category": "EQUITY",
                 "index_family": "STRONGDIP", "momentum": 0.30, "vol": 0.20,
                 "liq_p25": 1e12,
                 "rows": [(f"2026-{i // 28 + 1:02d}-{i % 28 + 1:02d}", a, a, 1e7)
                          for i, a in enumerate(_down)]},
                {"symbol": "WEAKDIP", "name": "x", "category": "EQUITY",
                 "index_family": "WEAKDIP", "momentum": 0.03, "vol": 0.20,
                 "liq_p25": 1e12,
                 "rows": [(f"2026-{i // 28 + 1:02d}-{i % 28 + 1:02d}", a, a, 1e7)
                          for i, a in enumerate(_down)]},
            ]
            sel18 = pb18.select(pool18, set(), "B", equity=1e8)
            decs18 = {st["symbol"]: dec for st, dec, _r in sel18["decisions"]}
            check("v5#6: momentum floor lets a STRONG trend through a 200-DMA "
                  "dip but still skips weak ones",
                  decs18.get("STRONGDIP") == "ELIGIBLE_BUY"
                  and decs18.get("WEAKDIP") == "SKIPPED", f"{decs18}")
            globals()["ENTRY_OVERLAY_MOM_FLOOR"] = None
            sel18b = pb18.select(pool18, set(), "B", equity=1e8)
            decs18b = {st["symbol"]: dec for st, dec, _r in sel18b["decisions"]}
            check("v5#6: default (None) restores today's strict overlay",
                  decs18b.get("STRONGDIP") == "SKIPPED"
                  and decs18b.get("WEAKDIP") == "SKIPPED", f"{decs18b}")
        finally:
            dbsub18.close()
            try:
                os.remove(DATA_DIR / "selftest_momfloor.db")
            except Exception:
                pass
    finally:
        globals()["ENTRY_OVERLAY_MOM_FLOOR"] = _save_mf

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
        _redirects = ((None, False) and _s_old[0])
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

    # --- audit v10 (external audit 2026-10-28 night) -------------------------
    # seat fill: strict pass leaves seats; relaxed fills in rank order; the
    # family cap survives the relaxation
    dbsub20 = Db(DATA_DIR / "selftest_seatfill.db")
    try:
        bb20 = PortfolioBuilder(dbsub20, FilterEngine(dbsub20))
        _mk = lambda s, m, fam: {"symbol": s, "momentum": m, "index_family": fam,
                                 "vol": 0.2, "liq_p25": 1e7, "rows": []}
        _rank = [_mk("A", 0.50, "f1"), _mk("B", 0.40, "f2"), _mk("C", 0.30, "f3")]
        _save_sf = globals()["SEAT_FILL_RELAXED"]
        _save_um = globals()["universe_map"]
        # the helper resolves families via the registry -> point it at a tiny
        # synthetic map (A/D share family f1)
        globals()["universe_map"] = lambda: {
            "A": {"index_family": "f1"}, "B": {"index_family": "f2"},
            "C": {"index_family": "f3"}, "D": {"index_family": "f1"}}
        try:
            globals()["SEAT_FILL_RELAXED"] = False
            _strict = bb20._seat_fill([_rank[0]], _rank, 3)
            globals()["SEAT_FILL_RELAXED"] = True
            _relaxed = bb20._seat_fill([_rank[0]], _rank, 3)
            _famdup = [_mk("D", 0.20, "f1")]
            _relaxed_fam = bb20._seat_fill(list(_rank), _rank + _famdup, 5)
        finally:
            globals()["SEAT_FILL_RELAXED"] = _save_sf
            globals()["universe_map"] = _save_um
        check("v10: seat fill inert when off, fills in rank order when on, "
              "family cap survives the relaxation",
              [s["symbol"] for s in _strict] == ["A"]
              and [s["symbol"] for s in _relaxed] == ["A", "B", "C"]
              and [s["symbol"] for s in _relaxed_fam] == ["A", "B", "C"],
              f"strict={[s['symbol'] for s in _strict]} "
              f"relaxed={[s['symbol'] for s in _relaxed]} "
              f"fam={[s['symbol'] for s in _relaxed_fam]}")
    finally:
        dbsub20.close()
        try:
            os.remove(DATA_DIR / "selftest_seatfill.db")
        except Exception:
            pass

    # sleeve-scaled drawdown triggers (invested_frac halves the trigger depth)
    dbsub21 = Db(DATA_DIR / "selftest_ddsleeve.db")
    try:
        _save_ds = globals()["DD_SLEEVE_SCALE"]
        try:
            globals()["DD_SLEEVE_SCALE"] = True
            re21 = RiskEngine(dbsub21, vol_target=0.15)
            re21.peak = 1000000.0
            re21.update(860000.0, "test", invested_frac=0.5)
            # 14% total dd; thresholds scaled x max(0.5, 0.35) -> severe at
            # 11.25%, so a 14% dip must halve a half-cash book
            _t1 = re21.halved
            re22 = RiskEngine(dbsub21, vol_target=0.15)
            re22.state, re22.halved, re22.all_cash = "NORMAL", False, False
            re22.peak = 1000000.0
            re22.update(860000.0, "test", invested_frac=1.0)
            # fully invested -> classic depths: a 14% dip must NOT even review
            _t2 = re22.halved or re22.state != "NORMAL"
        finally:
            globals()["DD_SLEEVE_SCALE"] = _save_ds
        check("v10: sleeve scaling halves a half-cash book at 14% dd but leaves "
              "a fully-invested book on the classic depths",
              _t1 and not _t2, f"half-cash-halved={_t1} full-invested-moved={_t2}")
    finally:
        dbsub21.close()
        try:
            os.remove(DATA_DIR / "selftest_ddsleeve.db")
        except Exception:
            pass

    # real market-session count + near-replan trickle boundary
    dbsub22 = Db(DATA_DIR / "selftest_sessions.db")
    try:
        for _d in ("2026-10-28", "2026-10-29", "2026-10-30"):
            dbsub22.execute(
                "INSERT INTO price_daily(symbol,date,open,high,low,close,"
                "adj_close,volume,traded_value) VALUES(?,?,?,?,?,?,?,?,?)",
                ("X", _d, 1, 1, 1, 1.0, 1.0, 1, 1))
        _n1 = market_sessions_to(dbsub22, "2026-10-28", "2026-10-30")
        _n2 = market_sessions_to(dbsub22, "2026-10-30", "2026-10-30")
        _tri = (1 + _n1) <= REPLAN_TRICKLE_DAYS
        check("v10: sessions counted off the real market calendar (no 5/7 guess); "
              "near-replan trickle arms at the boundary",
              _n1 == 2 and _n2 == 0 and _tri,
              f"n1={_n1} n2={_n2} trickle={_tri}")
    finally:
        dbsub22.close()
        try:
            os.remove(DATA_DIR / "selftest_sessions.db")
        except Exception:
            pass

    # --- audit v11 (2026-10-28 console audit) --------------------------------
    # P0-1: sessions-to-replan must come from the CALENDAR, not the price table
    # (a start-of-day run has no future bars; the v10 helper always returned 0,
    # trapping every run in trickle mode and killing the autofill pace).
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

    # 2026-10-29 user direction: one number (STARTING_CAPITAL) is the daily
    # deploy amount in BOTH modes; INVEST_PER_DAY derives from it.
    check("v12: INVEST_PER_DAY tracks STARTING_CAPITAL (edit one number for "
          "'how much do I invest per run')",
          INVEST_PER_DAY == STARTING_CAPITAL,
          f"INVEST_PER_DAY={INVEST_PER_DAY} STARTING_CAPITAL={STARTING_CAPITAL}")

    # audit v12 P0-1: a cross-mode (paper->live) flip rebases the risk peak
    # and releases derisk locks; same-mode evaluation is untouched.
    dbsub23 = Db(DATA_DIR / "selftest_peakmode.db")
    try:
        _save_pt2 = globals()["PAPER_TRADE"]
        try:
            _re23 = RiskEngine(dbsub23, vol_target=0.15)
            _re23.peak = 1000000.0
            dbsub23.meta_set("equity_peak_mode", "PAPER")
            globals()["PAPER_TRADE"] = False       # user flips to live
            _ev23 = _re23.update(300000.0, "flip")
            _flip_ok = (_re23.peak == 300000.0 and not _re23.all_cash
                        and _re23.state == "NORMAL")
            # same-mode 70% crash still triggers EXTREME
            _re24 = RiskEngine(dbsub23, vol_target=0.15)
            _re24.peak = 1000000.0
            dbsub23.meta_set("equity_peak_mode", "LIVE")
            _re24.update(300000.0, "real crash")
            _crash_ok = _re24.all_cash and _re24.state == "EXTREME"
        finally:
            globals()["PAPER_TRADE"] = _save_pt2
        check("v12: paper->live flip rebases the risk peak (no false EXTREME "
              "sell-all), while a real same-mode 70% crash still goes all-cash",
              _flip_ok and _crash_ok,
              f"flip_peak_ok={_flip_ok} crash_all_cash={_crash_ok}")
    finally:
        dbsub23.close()
        try:
            os.remove(DATA_DIR / "selftest_peakmode.db")
        except Exception:
            pass

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
    globals()["PAPER_TRADE"] = _save_pt
    return failed == 0


# ==============================================================================
# SECTION 25 — CLI
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
            _age = (dt.date.today()
                    - dt.datetime.fromtimestamp(_mp.stat().st_mtime).date()).days
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
        P.note("Tip: for an offline end-to-end trial run  ->  python etf-momentum-screener.py demo")
    if verbose:
        dm.coverage_table()
    else:
        dm.coverage_summary()


def cmd_reset_paper(db: Db):
    """
    Heal a poisoned PAPER journal (audit 2026-10-19): when synthetic and real
    prices were mixed, fills, P&L and the risk lock are all fiction — the only
    honest fix is a clean paper slate.  Market data is kept.
    """
    P.section("RESET PAPER LEDGER", "\U0001F9ED")
    if not PAPER_TRADE:
        P.err("Refusing: PAPER_TRADE is False. Never wipe a LIVE journal automatically.")
        return
    P.warn("This ERASES the paper journal: positions, orders, pending trades, "
           "decisions, tax lots, snapshots, risk state, and the SIP "
           "basket/targets/tranche marks.")
    P.note("Kept: ETF universe, price history, corporate actions, NAV data.")
    for t in ("positions", "orders", "pending_trades", "decisions",
              "tax_lots", "snapshots", "risk_events"):
        try:
            db.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    for k in ("cash", "risk_state", "risk_halved", "risk_all_cash",
              "risk_trigger_date", "risk_count_REVIEW", "risk_count_SEVERE",
              "risk_count_EXTREME", "equity_peak", "last_trade_signal",
              "last_trade_run", "last_interest_date", "interest_earned_total",
              # SIP cycle state (audit 2026-10-27 #2): a stale stored target
              # plan / idempotency mark must not survive a reset — the fresh
              # journal re-derives them from the live signal
              SIP_BASKET_KEY, SIP_BASKET_DATE_KEY, SIP_TARGETS_KEY,
              DEPLOY_DAY_KEY, "last_sip_date"):
        try:
            db.execute("DELETE FROM meta WHERE key=?", (k,))
        except Exception:
            pass
    db.meta_set("cash_balance", str(float(STARTING_CAPITAL)))   # Broker reads this
    db.meta_set("cash", str(float(STARTING_CAPITAL)))           # legacy alias
    db.meta_set("equity_peak", str(float(STARTING_CAPITAL)))
    P.ok(f"Paper ledger reset — starting capital restored to "
         f"{fmt_rs(STARTING_CAPITAL)}. Run 'python etf-momentum-screener.py trade' for a fresh cycle.")


def cmd_trade(db: Db, force_exec: bool = False, final_status: bool = True):
    """Daily entry point (user schedules this at 3:00 PM): risk check, queued
    remainders, monthly rebalance when due (SIP: exits only), then SIP buys."""
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
    """Compare the internal ledger with the broker's positions (live mode safety)."""
    P.section("BROKER RECONCILIATION", "\U0001F50D")
    P.warn("LIVE safety check (audit #9): the ledger and the broker can drift apart. "
           "Always reconcile before trusting the account.")
    try:
        env = load_env()
    except Exception as e:
        P.err(str(e))
        return
    client = UpstoxClient(env)
    broker = Broker(db, TaxEngine(db), client, paper=PAPER_TRADE)
    mine = broker.holdings()
    try:
        theirs_list = client.positions()
    except Exception as e:
        P.err(f"Could not fetch broker positions: {e}")
        return
    theirs = {}
    for pos in theirs_list:
        sym = (pos.get("trading_symbol") or pos.get("symbol") or "").strip()
        qty = float(pos.get("quantity") or pos.get("net_quantity") or 0)
        if sym:
            theirs[sym] = qty
    syms = sorted(set(mine) | set(theirs))
    if not syms:
        P.info("No positions on either side.")
        return
    rows = []
    for s in syms:
        mq = mine.get(s, {}).get("qty", 0)
        bq = theirs.get(s, 0)
        flag = "OK" if abs(mq - bq) < 0.5 else "MISMATCH"
        rows.append([s, fmt_num(mq, 0), fmt_num(bq, 0),
                     paint(flag, _C.GREEN if flag == "OK" else _C.RED)])
    P.table(["Symbol", "Ledger qty", "Broker qty", "Status"], rows,
            aligns=["<", ">", ">", "<"])
    mism = sum(1 for r in rows if r[3].find("MISMATCH") >= 0 if isinstance(r[3], str))
    if mism:
        P.warn(f"{mism} mismatch(es). Resolve before placing new live orders.")
    else:
        P.ok("Ledger and broker agree.")


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
    prices = {}
    for r in db.query("SELECT p.symbol AS symbol, p.close AS close FROM price_daily p WHERE p.date=(SELECT MAX(date) FROM price_daily WHERE symbol=p.symbol)"):
        prices[r["symbol"]] = r["close"]
    broker = Broker(db, TaxEngine(db), client, paper=PAPER_TRADE)
    broker.accrue_cash_interest()
    risk = RiskEngine(db)
    eq = broker.equity(prices)
    hv = broker.holdings_value(prices)
    ctx = risk.update(eq, invested_frac=(hv / eq) if eq > 0 else None)
    risk.snapshot(today_str(), broker.cash(), hv, eq)
    P.kv("Equity", fmt_cr(eq))
    P.kv("Drawdown", fmt_pct(ctx["drawdown"]))
    P.kv("Risk state", ctx["state"])
    for level, msg in ctx["events"]:
        P.warn(f"RISK [{level}] {msg}")
    P.ok(f"Monitor complete — {ok} quotes snapshotted into the database (spread history builds up here).")



# =============================================================================
# SECTION 24 — TUNE (audit "Tuning for long-term profitability" sec 3)
# =============================================================================

class KnobContext:
    """Temporarily set module globals for one tuning run, restore after."""
    def __init__(self, **kw):
        self.kw = kw
        self.save = {}

    def __enter__(self):
        g = globals()
        for k, v in self.kw.items():
            self.save[k] = g.get(k)
            g[k] = v
        return self

    def __exit__(self, *a):
        g = globals()
        for k, v in self.save.items():
            g[k] = v


def _bench_daily_returns(prices: dict):
    """Same benchmark pick as benchmark_report; returns (symbol, {date: ret})."""
    bench = None
    for cand in ("NIFTYBEES", "HDFCSENSEX", "MONIFTY500", "TOP100CASE"):
        if cand in prices and len(prices[cand]) > 250:
            bench = cand
            break
    if bench is None and prices:
        bench = max(prices, key=lambda s: len(prices[s]))
    if bench is None:
        return None, {}
    rows = sorted(((d, a) for (d, a, _c, _t) in prices[bench]))
    rets = {}
    for i in range(1, len(rows)):
        if rows[i - 1][1]:
            rets[rows[i][0]] = rows[i][1] / rows[i - 1][1] - 1.0
    return bench, rets


def _tune_windows(prices: dict, train_m: int = 36, test_m: int = 12):
    """The same rolling windows walk-forward uses (fixed-params segmentation)."""
    all_dates = sorted({d for s in prices.values() for (d, _a, _c, _t) in s})
    me = month_end_indices(all_dates)
    me_dates = [all_dates[i] for i in me]
    wins = []
    cursor = train_m
    while cursor + test_m - 1 < len(me_dates):
        wins.append((me_dates[cursor], me_dates[cursor + test_m - 1]))
        cursor += test_m
    return wins


def _tune_eval(prices, meta, windows, bench_rets, params=None, label="", **gkw):
    """
    ONE fixed-params continuous run, segmented into the walk-forward windows.
    Objective (audit): after-tax, after-cost excess over the RISK-MATCHED
    benchmark per window.  Returns per-window rows + summary, or None.
    """
    t0 = time.time()
    with KnobContext(**gkw):
        bt = Backtester(prices, meta, dict(BASELINE_PARAMS, **(params or {})),
                        cost_case="base", cash_rate=CASH_RATE_PRETAX,
                        strategy=TRADE_STRATEGY)
        r = bt.run()
    if not r or r.get("error"):
        P.warn(f"  [{label}] run failed: {r.get('error') if r else 'no result'}")
        return None
    curve = r.get("equity_curve", [])
    if len(curve) < 200:
        return None
    inv = clamp(r.get("avg_invested", 0.6), 0.0, 1.0)
    cash_fn_t = cash_return_series(prices, CASH_RATE_PRETAX)  # same sleeve

    def eq_at(d):
        best_e, best_d = None, None
        for dd, e in curve:
            if dd <= d and (best_d is None or dd >= best_d):
                best_d, best_e = dd, e
        return best_e

    rows = []
    excesses = []
    n_pos = 0
    for (w1, w2) in windows:
        e1, e2 = eq_at(w1), eq_at(w2)
        if not e1 or not e2 or e1 <= 0:
            continue
        yrs = max((parse_date(w2) - parse_date(w1)).days / 365.25, 1e-9)
        st_cagr = (e2 / e1) ** (1.0 / yrs) - 1.0
        # risk-matched benchmark growth inside the window
        g = 1.0
        for d, br in bench_rets.items():
            if w1 < d <= w2:
                g *= (1.0 + inv * br + (1.0 - inv) * cash_fn_t(d))
        rm_cagr = g ** (1.0 / yrs) - 1.0
        ex = st_cagr - rm_cagr
        rows.append([w1, w2, fmt_pct(st_cagr), fmt_pct(rm_cagr), fmt_pct(ex)])
        excesses.append(ex)
        if ex > 0:
            n_pos += 1
    if not rows:
        return None
    mean_ex = sum(excesses) / len(excesses)
    pct_pos = n_pos / len(rows)
    out = {"label": label, "rows": rows, "excesses": excesses,
           "mean_excess": mean_ex,
           "pct_pos": pct_pos, "n": len(rows),
           "cagr": r.get("cagr_after_cost_tax", 0.0),
           "sharpe": r.get("sharpe", 0.0), "mdd": r.get("max_drawdown", 0.0),
           "turnover": r.get("avg_turnover", 0.0),
           "secs": time.time() - t0}
    return out


def _pick_plateau(family_label, results, baseline_metric):
    """
    Plateau rule (audit): prefer the SIMPLEST setting within 0.25% mean excess
    of the family's best; require >= 50% windows positive to beat baseline.
    """
    best = max(results, key=lambda x: x["mean_excess"])
    near = [x for x in results
            if x["mean_excess"] >= best["mean_excess"] - 0.0025]
    # simplest = fewest knobs away from baseline labels — approximate by first
    # listed that is near the top (list order is simplest -> most extreme)
    pick = near[0] if near else best
    return pick, best


def cmd_tune(db: Db, quick: bool = False):
    P.section("TUNING — one change at a time, walk-forward vs risk-matched", "\U0001F3AF")
    prices, meta = load_prices_from_db(db)
    if not prices:
        P.err("No price data — run 'update' or 'demo' first.")
        return
    windows = _tune_windows(prices)
    if len(windows) < 3:
        P.warn(f"Only {len(windows)} walk-forward windows — tuning needs 3+. "
               f"Use a longer history.")
        return
    bench, bench_rets = _bench_daily_returns(prices)
    P.info(f"Objective: mean after-tax excess over the risk-matched {bench} "
           f"across {len(windows)} walk-forward windows "
           f"({windows[0][0]} .. {windows[-1][1]}), fixed params per variant.")
    P.note("Each variant is ONE continuous run segmented into the same windows "
           "(audit 2.8) — no cold-start seams. Baseline first, then one knob "
           f"at a time. Plateau rule: simplest setting within 0.25% of family best.")
    # external audit F9.4/F9.5/F9.6 (2026-10-27): say what this procedure is NOT
    P.note("Honesty notes: these windows are SEGMENTS of one in-sample run — "
           "'walk-forward' here means sequential evaluation, not a re-fit per "
           "window; the plateau winners are therefore not OOS-validated. Knobs "
           "never searched: VOL/COV/CORR windows, LIQ window+pctl, IMPACT "
           "coefficient, MIN_TRADE_VALUE, INVEST_PER_DAY routing. The FULL "
           "(non-quick) run costs ~45 combos per window and can take hours.")

    # ---------------- baseline ----------------
    base = _tune_eval(prices, meta, windows, bench_rets, label="baseline")
    if base is None:
        P.err("Baseline run failed — cannot tune.")
        return
    P.table(["Variant", "Mean excess", "% windows+", "CAGR net", "Sharpe", "Max DD"],
            [[base["label"], fmt_pct(base["mean_excess"]),
              fmt_pct(base["pct_pos"], 0), fmt_pct(base["cagr"]),
              f"{base['sharpe']:.2f}", fmt_pct(-base["mdd"])]],
            aligns=["<", ">", ">", ">", ">", ">"])

    # ---------------- one-at-a-time ablations ----------------
    families = []
    families.append(("Vol target", [
        ("8% (rulebook)", dict(VOL_TARGET=0.08)),
        ("10%", dict(VOL_TARGET=0.10)),
        ("12%", dict(VOL_TARGET=0.12)),
        ("15%", dict(VOL_TARGET=0.15)),
    ]))
    families.append(("Holdings x sleeve cap", [
        ("3 x 25%", dict(MAX_HOLDINGS=3, POS_CAP_LIQ_FRAC=0.25)),
        ("4 x 25%", dict(MAX_HOLDINGS=4, POS_CAP_LIQ_FRAC=0.25)),
        ("5 x 25% (rulebook)", dict(MAX_HOLDINGS=5, POS_CAP_LIQ_FRAC=0.25)),
        ("6 x 25%", dict(MAX_HOLDINGS=6, POS_CAP_LIQ_FRAC=0.25)),
        ("5 x 20%", dict(MAX_HOLDINGS=5, POS_CAP_LIQ_FRAC=0.20)),
        ("5 x 17%", dict(MAX_HOLDINGS=5, POS_CAP_LIQ_FRAC=0.17)),
    ]))
    families.append(("No-trade band", [
        ("0% (always rebalance)", dict(NO_TRADE_BAND=0.0)),
        ("15%", dict(NO_TRADE_BAND=0.15)),
        ("20% (default)", dict(NO_TRADE_BAND=0.20)),
        ("25%", dict(NO_TRADE_BAND=0.25)),
    ]))
    families.append(("Momentum signal x skip", [
        ("single 252, skip 0", dict(ENSEMBLE_LOOKBACKS=None), dict(skip=0)),
        ("single 252, skip 21 (rulebook)", dict(ENSEMBLE_LOOKBACKS=None), dict(skip=21)),
        ("single 252, skip 42", dict(ENSEMBLE_LOOKBACKS=None), dict(skip=42)),
        ("ensemble 63/126/252, skip 0", dict(ENSEMBLE_LOOKBACKS=(63, 126, 252)), dict(skip=0)),
        ("ensemble 63/126/252, skip 21", dict(ENSEMBLE_LOOKBACKS=(63, 126, 252)), dict(skip=21)),
        ("ensemble 63/126/252, skip 42", dict(ENSEMBLE_LOOKBACKS=(63, 126, 252)), dict(skip=42)),
    ]))
    families.append(("Ranking key", [
        ("momentum", dict(RANK_BY="momentum")),
        ("risk-adjusted (mom/vol)", dict(RANK_BY="risk_adjusted")),
    ]))
    families.append(("Entry threshold", [
        ("0%", dict(), dict(entry=0.0)),
        ("2% (rulebook)", dict(), dict(entry=0.02)),
        ("4%", dict(), dict(entry=0.04)),
    ]))
    families.append(("Exit threshold", [
        ("0% (rulebook)", dict(), dict(exit=0.0)),
        ("-1%", dict(), dict(exit=-0.01)),
        ("-2%", dict(), dict(exit=-0.02)),
    ]))
    families.append(("200-day overlay (entries+exits)", [
        ("off (rulebook)", dict(EXIT_OVERLAY_200DMA=False)),
        ("on", dict(EXIT_OVERLAY_200DMA=True)),
    ]))
    families.append(("Pairwise correlation cap", [
        # audit v14 §4: production 0.70 was missing and 0.85 was mislabelled
        # "(default)".  0.60/0.65/0.75 stay OUT (ledger-rejected, measured).
        ("0.70 (default — adopted R1/V4)", dict(PAIRWISE_CORR_CAP=0.70)),
        ("0.80 (unexplored)", dict(PAIRWISE_CORR_CAP=0.80)),
        ("0.90 (unexplored)", dict(PAIRWISE_CORR_CAP=0.90)),
    ]))
    families.append(("Rebalance frequency", [
        ("monthly (rulebook)", dict(REBALANCE_FREQ="M")),
        ("quarterly", dict(REBALANCE_FREQ="Q")),
    ]))
    families.append(("Drawdown trigger multiples", [
        # audit v14 §4: labels were backwards — production IS 1.0/1.5/2.0
        # (=> 15%/22.5%/30% with VOL_TARGET=0.15); "rulebook" never was 1.25/…
        ("1.0/1.5/2.0 (default — 15%/22.5%/30%)", dict(DD_REVIEW_MULT=1.0, DD_SEVERE_MULT=1.5, DD_EXTREME_MULT=2.0)),
        ("wider 1.25/1.9/2.5", dict(DD_REVIEW_MULT=1.25, DD_SEVERE_MULT=1.9, DD_EXTREME_MULT=2.5)),
        ("widest 1.5/2.2/3.0", dict(DD_REVIEW_MULT=1.5, DD_SEVERE_MULT=2.2, DD_EXTREME_MULT=3.0)),
    ]))
    families.append(("Vol blend (60d + 252d)", [
        ("0% (60d only)", dict(VOL_BLEND_252=0.0)),
        ("30%", dict(VOL_BLEND_252=0.3)),
        ("50%", dict(VOL_BLEND_252=0.5)),
    ]))
    families.append(("Regime scaling (halve in bear)", [
        ("off (rulebook)", dict(REGIME_SCALING=False)),
        ("on", dict(REGIME_SCALING=True)),
    ]))
    families.append(("Cash sleeve", [
        ("liquid-ETF history", dict(CASH_SLEEVE_MODE="LIQUID_ETF")),
        ("flat T-bill proxy", dict(CASH_SLEEVE_MODE="TBILL_FLAT")),
    ]))
    families.append(("Tax-aware exit deferral", [
        ("off (rulebook)", dict(TAX_DEFER_EXITS=False)),
        ("on", dict(TAX_DEFER_EXITS=True)),
    ]))
    families.append(("Tax lot selection", [
        ("FIFO (rulebook)", dict(LOT_SELECTION="FIFO")),
        ("HIFO", dict(LOT_SELECTION="HIFO")),
    ]))
    families.append(("Execution style", [
        ("MARKET (cross spread)", dict(EXECUTION_STYLE="MARKET")),
        ("LIMIT_MID", dict(EXECUTION_STYLE="LIMIT_MID")),
    ]))

    if quick:
        families = families[:6]
        P.warn("--quick: tuning only the first 6 families.")

    winners = []
    fam_summaries = []
    for fam_label, variants in families:
        P.sub(fam_label)
        frows = []
        results = []
        for item in variants:
            if len(item) == 3:
                vlabel, gkw, pkw = item
            else:
                vlabel, gkw = item
                pkw = {}
            res = _tune_eval(prices, meta, windows, bench_rets, params=pkw,
                             label=vlabel, **gkw)
            if res is None:
                frows.append([vlabel, "run failed", "-", "-", "-", "-"])
                continue
            results.append(res)
            frows.append([res["label"], fmt_pct(res["mean_excess"]),
                          fmt_pct(res["pct_pos"], 0), fmt_pct(res["cagr"]),
                          f"{res['sharpe']:.2f}", fmt_pct(-res["mdd"])])
        P.table(["Variant", "Mean excess", "% windows+", "CAGR net", "Sharpe", "Max DD"],
                frows, aligns=["<", ">", ">", ">", ">", ">"])
        if results:
            pick, best = _pick_plateau(fam_label, results, base["mean_excess"])
            delta = pick["mean_excess"] - base["mean_excess"]
            flag = ("PICK" if delta > 0.0005 else
                    ("keep baseline" if delta > -0.0025 else
                     "keep baseline (family is WORSE)"))
            P.info(f"Plateau pick: {pick['label']}  (mean excess {fmt_pct(pick['mean_excess'])}, "
                   f"{fmt_pct(pick['pct_pos'],0)} windows positive) -> {flag}")
            fam_summaries.append((fam_label, pick, best))
            # winner = its globals/params relative to baseline defaults
            for item in variants:
                vlabel = item[0]
                if vlabel == pick["label"]:
                    gkw = item[1]
                    pkw = item[2] if len(item) == 3 else {}
                    if delta > 0.0005:
                        winners.append((fam_label, vlabel, gkw, pkw))
                    break

    # ---------------- combine winners ----------------
    if winners:
        P.section("COMBINED CONFIGURATION (family winners together)", "\U0001F9EA")
        cg = {}
        cp = {}
        wlabels = []
        for fam_label, vlabel, gkw, pkw in winners:
            cg.update(gkw)
            cp.update(pkw)
            wlabels.append(f"{fam_label}: {vlabel}")
        combined = _tune_eval(prices, meta, windows, bench_rets, params=cp,
                              label="combined", **cg)
        if combined:
            P.table(["Variant", "Mean excess", "% windows+", "CAGR net", "Sharpe", "Max DD"],
                    [[base["label"], fmt_pct(base["mean_excess"]),
                      fmt_pct(base["pct_pos"], 0), fmt_pct(base["cagr"]),
                      f"{base['sharpe']:.2f}", fmt_pct(-base["mdd"])],
                     [combined["label"], fmt_pct(combined["mean_excess"]),
                      fmt_pct(combined["pct_pos"], 0), fmt_pct(combined["cagr"]),
                      f"{combined['sharpe']:.2f}", fmt_pct(-combined["mdd"])]],
                    aligns=["<", ">", ">", ">", ">", ">"])
            P.sub("Combined vs baseline by walk-forward window")
            prows = []
            for i, row in enumerate(combined["rows"]):
                b = base["excesses"][i] if i < len(base["excesses"]) else None
                c = combined["excesses"][i]
                prows.append([row[0], row[1],
                              fmt_pct(b) if b is not None else "-", fmt_pct(c),
                              "+" if b is not None and c > b else "-"])
            P.table(["From", "To", "Base excess", "Combined excess", "Better?"],
                    prows, aligns=["<", "<", ">", ">", "<"])
            P.note("Plateaus, not peaks: a family counts as a win only if the "
                   "improvement survives the 0.25% plateau rule AND at least "
                   "half the windows are positive.")
            # write the recommendation
            report_path = DATA_DIR / "reports" / "tuned-config.txt"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with open(report_path, "w", encoding="utf-8") as f:
                f.write("TUNED CONFIGURATION (from tsmom.py tune)\n")
                f.write(f"baseline mean excess {base['mean_excess']:.4f} "
                        f"({base['pct_pos']:.0%} windows+)\n")
                f.write(f"combined mean excess {combined['mean_excess']:.4f} "
                        f"({combined['pct_pos']:.0%} windows+)\n\n")
                f.write("Winners (one change at a time):\n")
                for w in wlabels:
                    f.write(f"  - {w}\n")
                f.write("\nGlobal knobs to set:\n")
                for k, v in sorted(cg.items()):
                    f.write(f"  {k} = {v!r}\n")
                f.write("Params to set:\n")
                for k, v in sorted(cp.items()):
                    f.write(f"  {k} = {v!r}\n")
                f.write("\nPer-window (from | to | baseline excess | combined excess):\n")
                for i, row in enumerate(combined["rows"]):
                    b = base["excesses"][i] if i < len(base["excesses"]) else None
                    c = combined["excesses"][i]
                    f.write(f"  {row[0]} .. {row[1]}: "
                            f"{fmt_pct(b) if b is not None else '-'} -> {fmt_pct(c)}\n")
            P.ok(f"Recommendation written to {report_path}")
        else:
            P.warn("Combined run failed — keep the baseline configuration.")
    else:
        P.ok("No family beat the baseline by the plateau rule — the rulebook "
             "configuration stays as-is (that is a real result, not a failure).")


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
        prog="tsmom.py",
        description="TSMOM — Time Series Momentum on the Nifty ETF Universe (paper-trade first)")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "scan", "update", "trade", "monitor",
                                 "backtest", "status", "report", "demo", "selftest",
                                 "live", "load-data", "reconcile", "tune",
                                 "reset-paper", "navdiag"],
                        help="what to do (default: run = scan+update+daily SIP cycle+status)")
    parser.add_argument("extras", nargs="*",
                        help="extra positional args (e.g. symbols for 'navdiag')")
    parser.add_argument("--years", type=int, default=11, help="years of history to download")
    parser.add_argument("--force-exec", action="store_true",
                        help="execute at the latest price even if not the exact next session")
    parser.add_argument("--quick", action="store_true", help="faster backtest (smaller grid)")
    parser.add_argument("--seed", type=int, default=7, help="synthetic data seed")
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
            cmd_selftest(db)
        elif args.command == "scan":
            P.banner()
            cmd_scan(db)
        elif args.command == "update":
            P.banner()
            cmd_scan(db, try_live=True)
            cmd_update(db, years=args.years, force=args.force_data)
        elif args.command == "trade":
            P.banner()
            if not PAPER_TRADE and not live_order_ok():
                # audit v4 L1 -> 2026-10-29: runtime y/n replaces --confirm
                P.note("No real orders placed.  (Set PAPER_TRADE=True at the "
                       "top of the script to simulate instead.)")
                offer_cash_sweep(db)     # audit v12: risk-free parking ≠ strategy
                return
            cmd_trade(db, force_exec=args.force_exec)
        elif args.command == "monitor":
            P.banner()
            cmd_monitor(db)
        elif args.command == "navdiag":
            P.banner()
            cmd_navdiag(db, args.extras or None)
        elif args.command == "backtest":
            P.banner()
            run_full_backtest(db, quick=args.quick)
        elif args.command == "tune":
            P.banner()
            cmd_tune(db, quick=args.quick)
        elif args.command == "status":
            P.banner()
            cmd_status(db)
        elif args.command == "report":
            P.banner()
            cmd_report(db)
        elif args.command == "reset-paper":
            P.banner()
            cmd_reset_paper(db)
        elif args.command == "load-data":
            P.banner()
            cmd_load_data(db, fetch_nav=getattr(args, "fetch_nav", False))
        elif args.command == "reconcile":
            P.banner()
            cmd_reconcile(db)
        elif args.command == "demo":
            P.banner()
            # audit 2026-10-19: demo NEVER touches the real database —
            # synthetic rows spliced into real series corrupt every signal
            db2 = Db(DATA_DIR / "tsmom-demo.db")
            P.warn(f"Demo runs on its own database: {DATA_DIR / 'tsmom-demo.db'} "
                   f"(your real stock-data/tsmom.db is untouched).")
            generate_synthetic_data(db2, seed=args.seed)
            cmd_scan(db2, try_live=False)
            prices, meta = load_prices_from_db(db2)
            bt = Backtester(prices, meta, BASELINE_PARAMS, strategy="B")
            print_backtest_result(bt.run(), "DEMO BACKTEST (synthetic data) — Strategy B")
            cyc = TradingCycle(db2)
            cyc.run()
            cmd_status(db2)
        elif args.command == "live":
            if PAPER_TRADE:
                P.banner()
                P.warn("LIVE trading is disabled while PAPER_TRADE=True in the config.")
                P.note("This program runs in PAPER mode by default.")
                P.note("To send real orders: set PAPER_TRADE=False at the top of "
                       "etf-momentum-screener.py, then run any command — it will "
                       "ask you for a y/n confirmation before placing orders.")
                return
            globals()["PAPER_TRADE"] = False
            P.banner()
            if not live_order_ok("MARKET/LIMIT orders fill at the market; the "
                                 "broker fill is fetched and logged.  Run "
                                 "'reconcile' after trading to compare the "
                                 "ledger with the broker."):
                offer_cash_sweep(db)     # audit v12: risk-free parking ≠ strategy
                return
            cmd_trade(db, force_exec=args.force_exec)
        else:  # run
            P.banner()
            cmd_scan(db, verbose=False)
            cmd_update(db, years=args.years, force=args.force_data, verbose=False)
            prices = {r["symbol"] for r in db.query("SELECT DISTINCT symbol FROM price_daily")}
            if not prices:
                # audit v14 P0 (2026-10-30): this fallback wrote SYNTHETIC demo
                # rows into the PRODUCTION database; a later live run could
                # then trade real money off fictional prices.  Removed — point
                # the user at the isolated demo database instead.
                P.err("No downloaded prices in the database.  I will NOT invent "
                      "fake prices here (that pollutes the real journal).")
                P.note("To watch the engine end-to-end offline: "
                       "python etf-momentum-screener.py demo   (its own demo DB, "
                       "your real data is untouched)")
                P.note("To fill the real database: fix the Upstox token "
                       "(env.txt) and run:  python etf-momentum-screener.py update")
            if not PAPER_TRADE and not live_order_ok():
                # audit v4 L1 -> 2026-10-29: bare run in live mode asks y/n;
                # a NO means analyse/refresh/status only, no orders.
                # audit v12: say what did NOT happen — the risk engine and
                # position marks are only evaluated on a confirmed run.
                P.err("LIVE MODE is on (PAPER_TRADE=False): orders skipped "
                      "by your answer (analysis only this run — the risk "
                      "engine's peak/locks are NOT re-evaluated until you "
                      "answer y).")
                offer_cash_sweep(db)     # audit v12: separate risk-free confirm
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